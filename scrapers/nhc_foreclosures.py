"""nhc_foreclosures.py — NHC GS 105-374 tax-foreclosure schedule scraper.

The county publishes its active tax-foreclosure sale schedule as a static
CMS page at https://www.nhcgov.com/345/Foreclosures. Low volume (typically
4-12 active parcels per quarterly auction), but very high signal — these
parcels are days from losing ownership.

Output: data/raw/nhc_foreclosures.jsonl — one row per (parcel × case) pair.

Snapshot semantics: each refresh overwrites the JSONL fully. State file
tracks the page's content hash so we can detect "unchanged" runs.

Schema (canonicalized):
  parcel              "R########-###-###" — joins to PropertyOwners.PARID
  street_address      free-text
  case_number         civil case # like "25CV004024-640"
  sale_date           ISO YYYY-MM-DD
  sale_time           HH:MM (local)
  sale_location       courthouse address
  statute             "GS 105-374" (NHC handles in-house, not GS 105-375)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

DEFAULT_URL = "https://www.nhcgov.com/345/Foreclosures"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 "
    "(+contact: infinitygauntletllc@gmail.com)"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
SLUG = "nhc_foreclosures"

PARCEL_RE = re.compile(r"R\d{5}-\d{3}-\d{3}-\d{3}")
CASE_RE = re.compile(r"\d{2}CV[A-Z]*\d{4,}-\d+")
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2},?\s+\d{4}",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\d{1,2}:\d{2}\s*(AM|PM)", re.IGNORECASE)


def _http_get(url: str, retries: int = 3, timeout: int = 60) -> str:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,*/*",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}/{retries}] {e} — sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GET failed after {retries} retries: {url} ({last_err})")


class _TextExtractor(HTMLParser):
    """Strip HTML to clean lines preserving paragraph breaks."""

    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
        if tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        out = "".join(self._chunks)
        # collapse intra-line whitespace; preserve newlines for line-based parsing
        out = re.sub(r"[ \t]+", " ", out)
        out = re.sub(r"\n[ \t]+", "\n", out)
        out = re.sub(r"\n{2,}", "\n\n", out)
        return out.strip()


def parse_records(html: str) -> tuple[list[dict], dict]:
    """Position-based parser: split the HTML into case-bounded slices, then
    extract distinct (parcel, case) pairs from each slice.

    The /345/Foreclosures page is CMS-edited and the layout shifts whenever
    a new case is added. The reliable invariant is that case numbers
    (e.g. `23CVS000524-640`, `25CV004024-640`) appear as headings BEFORE
    each block, and every PARID after a case heading and before the next
    one belongs to that case.
    """
    parser = _TextExtractor()
    parser.feed(html)
    text = parser.text()

    diag = {
        "page_text_len": len(text),
        "case_count": 0,
        "parcel_count": 0,
        "sale_dates": [],
    }
    records: list[dict] = []

    case_marks = [(m.start(), m.end(), m.group(0)) for m in CASE_RE.finditer(text)]
    diag["case_count"] = len({c for _, _, c in case_marks})

    if not case_marks:
        return [], diag

    # Page layout: each block lists the parcels FIRST, then the case header
    # AFTER. Strategy: for each unique PARID, attach to the first case marker
    # that appears at-or-after the PARID's first occurrence. Property-card
    # links lower in the page repeat PARIDs and would otherwise mis-attach.
    parcel_first_pos: dict[str, int] = {}
    for m in PARCEL_RE.finditer(text):
        pid = m.group(0)
        if pid not in parcel_first_pos:
            parcel_first_pos[pid] = m.start()

    # Address blurbs come from the parcel-detail bullets near the property
    # card link, not the bare list at the top of each block. Scan lines
    # like "View the <address> property card" — that's the canonical address.
    addr_by_pid: dict[str, str] = {}
    addr_re = re.compile(r"View the (.+?) property card", re.IGNORECASE)
    for line in text.splitlines():
        m_addr = addr_re.search(line)
        if not m_addr:
            continue
        m_par = PARCEL_RE.search(line)
        if not m_par:
            continue
        addr_by_pid.setdefault(m_par.group(0), m_addr.group(1).strip())

    # Build per-case sale metadata by slicing forward from each case mark to
    # the next one (the metadata text — "Sale Date / Sale Time / Location"
    # — appears under the case header).
    for i, (cstart, cend, case_no) in enumerate(case_marks):
        next_start = case_marks[i + 1][0] if i + 1 < len(case_marks) else len(text)
        meta_slice = text[cend:next_start]
        m_date = DATE_RE.search(meta_slice)
        if m_date:
            d = _normalize_date(m_date.group(0))
            if d and d not in diag["sale_dates"]:
                diag["sale_dates"].append(d)

    for pid, pos in parcel_first_pos.items():
        case_no, sale_date, sale_time, sale_loc = "", "", "", ""
        for cstart, cend, c in case_marks:
            if cstart >= pos:
                case_no = c
                meta_end = next(
                    (s for s, _, _ in case_marks if s > cstart), len(text)
                )
                meta_slice = text[cend:meta_end]
                m_date = DATE_RE.search(meta_slice)
                sale_date = _normalize_date(m_date.group(0)) if m_date else ""
                m_time = TIME_RE.search(meta_slice)
                sale_time = m_time.group(0).upper() if m_time else ""
                for line in meta_slice.splitlines():
                    if "Princess" in line and "Wilmington" in line:
                        sale_loc = line.strip()
                        break
                break
        if not case_no:
            continue
        records.append({
            "parcel": pid,
            "street_address": addr_by_pid.get(pid, ""),
            "case_number": case_no,
            "sale_date": sale_date,
            "sale_time": sale_time,
            "sale_location": sale_loc,
            "statute": "GS 105-374",
            "_source": "nhc_foreclosures_html",
        })
        diag["parcel_count"] += 1

    return records, diag


def _normalize_date(s: str) -> str:
    s = s.strip().rstrip(",")
    for fmt in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s  # fall back to raw


def install_signal_handler() -> dict:
    flag = {"stop": False}

    def handler(signum, frame):
        flag["stop"] = True

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, handler)
    except (AttributeError, ValueError):
        pass
    return flag


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"last_hash": None, "rows": 0, "last_run_at": None}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def run(args: argparse.Namespace) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / f"{SLUG}.jsonl"
    state_path = RAW_DIR / f"{SLUG}.state.json"

    if args.reset:
        for p in (out_path, state_path):
            if p.exists():
                p.unlink()
                print(f"[reset] removed {p.name}")

    install_signal_handler()
    state = load_state(state_path)

    print(f"[i] url:   {args.url}")
    print(f"[i] out:   {out_path}")

    html = _http_get(args.url)
    page_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    if page_hash == state.get("last_hash") and not args.force:
        print(f"[skip] page hash unchanged — use --force to re-parse anyway")
        return 0

    records, diag = parse_records(html)
    print(f"[i] cases: {diag['case_count']}  parcels: {diag['parcel_count']}  dates: {diag['sale_dates']}")
    if args.limit and args.limit > 0:
        records = records[: args.limit]

    fetched_at = datetime.now(timezone.utc).isoformat()
    tmp = out_path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in records:
            r["_fetched_at"] = fetched_at
            fh.write(json.dumps(r) + "\n")
    tmp.replace(out_path)

    state["last_hash"] = page_hash
    state["rows"] = len(records)
    save_state(state_path, state)
    print(f"[done] wrote {len(records):,} rows to {out_path.name}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NHC tax-foreclosure schedule scraper.")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--reset", action="store_true")
    p.add_argument("--since", default="",
                   help="(Reserved — page is a full-snapshot source.)")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
