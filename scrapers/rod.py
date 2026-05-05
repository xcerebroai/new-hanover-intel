"""rod.py — New Hanover County Register of Deeds scraper.

Pulls real-estate filings from the BIS PHP search at
search.newhanoverdeeds.com. NO CAPTCHA, no login, fully public —
this is a 180° flip from the Mecklenburg Aumentum login wall.

Architecture per RECON.md:
1. GET /NameSearch.php?Accept=Accept to seed PHPSESSID.
2. POST /NamePick.php with date range + instType[InstCodes][CODE]=CODE.
   Returns a list of entityID checkboxes ("Names Found").
3. POST /NameDisplay.php with all entityID values + displaybutton button.
   Returns the actual filings table — 7 cells per row:
     [recording_date, RB-Book-Page, doc_type, description, reverse_party,
      cross_reference, image_links]
4. Dedupe rows by instrument_number (same filing appears once per party).
5. Write to data/raw/rod_<doctype_slug>.jsonl.

Doc types are configured per group below — high-signal subset of the 369
codes the system actually offers. Each invocation of the scraper handles
one doctype slug; refresh.py orchestrates the loop across all of them.

The 2000-record per-query cap matters for high-volume codes like DEED. The
scraper respects it by narrowing the date window per group: time-bounded
sources (weekly/monthly cycles) for high-volume types, full-window for
rare types. Default --since is "last 14 days" which gives daily refresh
plenty of overlap room.

CRITICAL: ROD records carry NO parcel ID. The pipeline joins via
(grantor name + situs address) or subdivision+lot+block matching against
PropertyOwners — see pipeline/build_leads.py.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests

BASE = "https://search.newhanoverdeeds.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 "
    "(+contact: infinitygauntletllc@gmail.com)"
)
RATE_LIMIT_SEC = 3.0  # polite per RECON.md
ENTITY_BATCH_SIZE = 100  # NameDisplay times out on larger batches under load

# Per-doctype groups. The slug becomes the JSONL filename suffix; the codes
# list is what gets sent as instType[InstCodes][CODE]=CODE on NamePick. One
# scraper invocation handles one slug.
DOCTYPE_GROUPS: dict[str, list[str]] = {
    "deed":            ["DEED"],
    "quitclaim":       ["QCD", "AGMNT & QCD", "REREC QCD"],
    "deed_of_trust":   ["D/T"],
    "assignment":      ["ASGMT"],
    "satisfaction":    ["SAT", "PSAT", "D/R", "P/REL D/T"],
    "foreclosure":     ["FCL", "FCL DEED", "N/F", "SUB TR", "SUB TR DEED",
                        "R/SUB TR DEED", "NOTICE SUB TR"],
    "estate_deed":     ["ADMIN DEED", "EXEC DEED", "EXTRX DEED",
                        "COMMR DEED", "SHERIF DEED", "TR DEED", "TRST DEED"],
    "judgment":        ["JDGMT", "JUDGMENT"],
    "lien":            ["LIEN", "LIS PENS", "BKTCY"],
    "deed_of_gift":    ["DEED OF GIFT"],
    "separation":      ["SEP AGMT", "MEMO SEPR AGMT"],
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"

# Regex helpers
ENTITY_RE = re.compile(r"""name\s*=\s*['"]entityID\[(\d+)\]['"]""")
NAMES_FOUND_RE = re.compile(r"(\d+)\s*Names\s*Found", re.IGNORECASE)
RECORDS_FOUND_RE = re.compile(r"(\d+)\s*Records\s*Found", re.IGNORECASE)
TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
INST_NUM_RE = re.compile(r"DetailScreen\.php\?inst_num=(\d+)")
BOOK_PAGE_RE = re.compile(r"^([A-Z]+)-(\d+)-(\d+)$")


def _strip(s: str) -> str:
    s = TAG_RE.sub(" ", s)
    s = html_lib.unescape(s)
    return WS_RE.sub(" ", s).strip()


def install_signal_handler() -> dict:
    flag = {"stop": False}

    def handler(signum, frame):
        if flag["stop"]:
            sys.exit(130)
        flag["stop"] = True
        print("\n[!] interrupt — saving state then stopping...", file=sys.stderr)

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, handler)
    except (AttributeError, ValueError):
        pass
    return flag


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"seen_inst": [], "last_run_at": None, "last_since": None}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def make_session(ua: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{BASE}/NameSearch.php?Accept=Accept",
        "Origin": BASE,
    })
    # Seed PHPSESSID
    s.get(f"{BASE}/NameSearch.php?Accept=Accept", timeout=30)
    return s


def _post_retry(s: requests.Session, url: str, data, retries: int = 3,
                 timeout: int = 90) -> requests.Response:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = s.post(url, data=data, timeout=timeout)
            r.raise_for_status()
            return r
        except (requests.exceptions.RequestException, ConnectionError) as e:
            last_err = e
            wait = 5 * (attempt + 1)
            print(f"  [retry {attempt+1}/{retries}] {type(e).__name__}: {e} — sleeping {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"POST failed after {retries} retries: {url} ({last_err})")


def post_namepick(s: requests.Session, codes: Iterable[str],
                   start: str, end: str) -> tuple[int, list[str]]:
    """POST NamePick.php for the given codes + date range. Returns
    (names_found_header, entity_id_list).
    """
    data: list[tuple[str, str]] = [
        ("search_type", "Standard"),
        ("entity_type", "both"),
        ("tor_last_name", ""),
        ("tor_first_name", ""),
        ("tee_last_name", ""),
        ("tee_first_name", ""),
        ("start_date", start),
        ("end_date", end),
        ("search_each", "on"),
    ]
    for code in codes:
        data.append((f"instType[InstCodes][{code}]", code))

    r = _post_retry(s, f"{BASE}/NamePick.php", data)
    text = r.text
    m = NAMES_FOUND_RE.search(text)
    names_found = int(m.group(1)) if m else 0
    ids = ENTITY_RE.findall(text)
    return names_found, ids


def post_namedisplay(s: requests.Session, entity_ids: list[str]) -> str:
    if not entity_ids:
        return ""
    data: list[tuple[str, str]] = [(f"entityID[{i}]", i) for i in entity_ids]
    data.append(("displaybutton", "Display Detail Listing"))
    r = _post_retry(s, f"{BASE}/NameDisplay.php", data)
    return r.text


def parse_rows(html: str) -> list[dict]:
    """Parse the NameDisplay results table into dict rows."""
    rows: list[dict] = []
    for tr_html in TR_RE.findall(html):
        if "DetailScreen.php" not in tr_html:
            continue
        m_inst = INST_NUM_RE.search(tr_html)
        if not m_inst:
            continue
        inst_num = m_inst.group(1)
        cells = [_strip(td) for td in TD_RE.findall(tr_html)]
        # Pad short rows to 7 cells
        cells = (cells + [""] * 7)[:7]
        date, bookpage, doc_type, description, reverse_party, cross_ref, _imgs = cells

        book_code = book_num = page_num = ""
        m_bp = BOOK_PAGE_RE.match(bookpage)
        if m_bp:
            book_code = m_bp.group(1)
            book_num = m_bp.group(2)
            page_num = m_bp.group(3)

        rows.append({
            "instrument_number": inst_num,
            "recorded_date": date,
            "book_code": book_code,
            "book_number": book_num,
            "page_number": page_num,
            "doc_type_label": doc_type,
            "description": description,
            "reverse_party": reverse_party,
            "cross_reference": cross_ref,
            "image_url_pdf": f"{BASE}/view_image.php?file={inst_num}&type=pdf",
            "image_url_tif": f"{BASE}/view_image.php?file={inst_num}&type=tif",
            "detail_url": f"{BASE}/DetailScreen.php?inst_num={inst_num}",
        })
    return rows


def scrape_doctype(slug: str, codes: list[str], since: str, until: str,
                   reset: bool, limit: int, flag: dict) -> dict:
    out_path = RAW_DIR / f"rod_{slug}.jsonl"
    state_path = RAW_DIR / f"rod_{slug}.state.json"

    if reset:
        for p in (out_path, state_path):
            if p.exists():
                p.unlink()
                print(f"[reset] removed {p.name}")

    state = load_state(state_path)
    seen = set(state.get("seen_inst", []))

    print(f"\n=== {slug}  codes={codes}  range={since}..{until} ===")
    print(f"[i] seen_inst: {len(seen):,}  out: {out_path.name}")

    s = make_session(DEFAULT_USER_AGENT)
    time.sleep(RATE_LIMIT_SEC)

    names_found, entity_ids = post_namepick(s, codes, since, until)
    print(f"[i] NamePick: {names_found} names found, {len(entity_ids)} entity IDs returned")
    if names_found >= 2000:
        print(f"[!] WARNING: hit 2000-name cap — narrow date range or split codes")

    if not entity_ids:
        save_state(state_path, state)
        return {"slug": slug, "names_found": names_found, "appended": 0,
                "duplicates": 0, "rows_total": 0}

    # Batch entity IDs through NameDisplay to stay under POST size limits.
    fetched_at = datetime.now(timezone.utc).isoformat()
    appended = 0
    dup = 0
    total_rows = 0
    with out_path.open("a", encoding="utf-8") as fh:
        for batch_start in range(0, len(entity_ids), ENTITY_BATCH_SIZE):
            if flag["stop"]:
                break
            batch = entity_ids[batch_start:batch_start + ENTITY_BATCH_SIZE]
            time.sleep(RATE_LIMIT_SEC)
            html_text = post_namedisplay(s, batch)
            rows = parse_rows(html_text)
            total_rows += len(rows)
            for row in rows:
                inst = row["instrument_number"]
                if inst in seen:
                    dup += 1
                    continue
                row["_source"] = "nhc_rod"
                row["_doctype_group"] = slug
                row["_codes_searched"] = codes
                row["_fetched_at"] = fetched_at
                fh.write(json.dumps(row) + "\n")
                seen.add(inst)
                appended += 1
                if limit > 0 and appended >= limit:
                    flag["stop"] = True
                    break
            print(f"[+] batch {batch_start // ENTITY_BATCH_SIZE + 1}: "
                  f"{len(rows)} rows, +{appended} new this run, dup={dup}")

    state["seen_inst"] = sorted(seen)
    state["last_since"] = since
    save_state(state_path, state)

    return {"slug": slug, "names_found": names_found, "appended": appended,
            "duplicates": dup, "rows_total": total_rows}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NHC Register of Deeds (BIS PHP) scraper.")
    p.add_argument(
        "--doctype",
        choices=list(DOCTYPE_GROUPS.keys()) + ["all"],
        default="all",
        help="Which doc-type group to pull (default: all).",
    )
    p.add_argument("--since", default="",
                   help="Start date mm/dd/yyyy or yyyy-mm-dd (default: 14d ago).")
    p.add_argument("--until", default="",
                   help="End date mm/dd/yyyy or yyyy-mm-dd (default: today).")
    p.add_argument("--limit", type=int, default=0,
                   help="Cap rows appended per doctype this run (0 = unlimited).")
    p.add_argument("--reset", action="store_true",
                   help="Clear output JSONL + state for selected groups.")
    return p.parse_args()


def _norm_date(s: str, default: str) -> str:
    s = s.strip()
    if not s:
        return default
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return s  # tolerate caller-known format


def main() -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    args = parse_args()
    today = datetime.now(timezone.utc)
    since = _norm_date(args.since, (today - timedelta(days=14)).strftime("%m/%d/%Y"))
    until = _norm_date(args.until, today.strftime("%m/%d/%Y"))

    targets = list(DOCTYPE_GROUPS.keys()) if args.doctype == "all" else [args.doctype]
    flag = install_signal_handler()
    summaries = []
    for slug in targets:
        if flag["stop"]:
            break
        s = scrape_doctype(slug, DOCTYPE_GROUPS[slug], since, until,
                           args.reset, args.limit, flag)
        summaries.append(s)

    print("\n=== summary ===")
    for s in summaries:
        print(f"  {s['slug']:18s} names={s['names_found']:>5}  rows={s['rows_total']:>6}  "
              f"appended={s['appended']:>5}  dup={s['duplicates']:>5}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
