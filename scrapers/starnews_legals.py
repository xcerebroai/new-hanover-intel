"""starnews_legals.py — Wilmington StarNews legal notices scraper.

The Wilmington StarNews publishes statutory legal notices through Gannett's
classifieds platform at:
  https://classifieds.gannettclassifieds.com/marketplace/wlm/category/legals/<sub>

Sub-categories pulled (each writes its own JSONL):
  notice-to-creditors            -> data/raw/starnews_notice_to_creditors.jsonl
  foreclosures-sheriff-sales     -> data/raw/starnews_foreclosures.jsonl

These are the two NC-statute-required publications relevant to motivated
sellers: Notice to Creditors (estate openings, GS 28A-14-1) and foreclosure
sale notices (NC GS 45-21.16). The eCourts portal that holds the underlying
case data is AWS-WAF-CAPTCHA-gated; the newspaper publication is the
parallel feed (see RECON.md).

The platform is plain Apache, no Cloudflare WAF, no anti-bot — straight
GET-paginated HTML. Rate limit at 2 sec/request to be polite; no need
to throttle harder.

Each listing is `<div id="advert_NNN" class="list">`. The listing panel
shows a truncated body and post-date; the full body lives at
`/marketplace/wlm/advert/-Retail_NNN`. We fetch the detail page once per
new advert and extract structured fields (decedent name, county, file #,
executor) via regex from the post body.

NHC scope filter: only keep listings whose body mentions "New Hanover
County" — the wlm market also publishes notices for adjacent counties.

Resume model: state.json carries `seen_advert_ids` (set) and `last_run_at`.
Idempotent — re-running pulls only new ids.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://classifieds.gannettclassifieds.com"
DEFAULT_CATEGORIES = {
    "notice_to_creditors": f"{BASE}/marketplace/wlm/category/legals/notice-to-creditors",
    "foreclosures": f"{BASE}/marketplace/wlm/category/legals/foreclosures-sheriff-sales",
}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 "
    "(+contact: infinitygauntletllc@gmail.com)"
)
RATE_LIMIT_SEC = 2.0

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"

# Match the listing panel-body div for an advert id; capture the short body.
ADVERT_RE = re.compile(
    r'<div id="advert_(?P<id>\d+)"[^>]*class="list"[^>]*>(?P<panel>.*?)</div></div></div>',
    re.DOTALL,
)
TITLE_RE = re.compile(r'class="description[^"]*"[^>]*>([^<]+)', re.DOTALL)
TIME_RE = re.compile(r'<time datetime="([^"]+)"')
REFCODE_RE = re.compile(r"Refcode:\s*#?(\S+)")

# Detail pages embed the full body twice: once in <meta name="description">
# (truncated to ~250 chars by Gannett's HTML head template) and once in a
# JSON-LD <script type="application/ld+json"> block as the `description`
# field — and the JSON-LD copy is the ENTIRE notice text. We prefer the
# JSON-LD body; the in-DOM `<span class="description">` panel is a third
# copy that may exist but is less reliable.
JSON_LD_DESC_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>',
    re.DOTALL,
)
JSON_LD_FIELD_RE = re.compile(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL)
META_DESC_RE = re.compile(
    r'<meta name="description"\s+content="([^"]+)"',
    re.IGNORECASE,
)
# County mentions come in many forms; we want New Hanover.
COUNTY_RE = re.compile(r"New Hanover County", re.IGNORECASE)
# Estate file number — NC court file format YYE######-### (e.g. 26E000439-640)
ESTATE_FILE_RE = re.compile(r"\b\d{2}E\d{6,}-\d+\b")
# SP case (foreclosure) file format — sometimes seen in NC newspaper notices
SP_CASE_RE = re.compile(r"\b\d{2}SP\d{6,}-?\d*\b", re.IGNORECASE)
# Decedent — the body usually says "Estate of <NAME>, Deceased" or
# "the Estate of <NAME>"
DECEDENT_RE = re.compile(
    r"Estate of\s+([A-Z][A-Z][A-Z .,'-]+?)\s*[,(]\s*(?:Deceased|deceased)",
)
# Executor / Administrator — NC notices place the executor name on the line
# BEFORE the role label, e.g. "ROBERT LEE JOBE\nExecutor". Fall back to
# the colon form ("Executor: NAME") for rare cases.
EXEC_AFTER_RE = re.compile(
    r"([A-Z][A-Za-z .'-]{4,})[\r\n]+\s*(Executor|Executrix|Administrator|Administratrix|Co-Executor|Co-Administrator|Personal Representative)\b",
)
EXEC_BEFORE_RE = re.compile(
    r"(?:Executor|Executrix|Administrator|Administratrix|Personal Representative)[:\s]+"
    r"([A-Z][A-Z .'-]+?)\s*(?:[\r\n]|<|,\s|$)",
)
# Creditor claim deadline — "before the <date>"
CLAIM_DEADLINE_RE = re.compile(
    r"(?:on or before|before)\s+(?:the\s+)?(\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?[A-Z][a-z]+(?:,?\s+\d{4})?|"
    r"[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
)
# Strip simple HTML tags for body cleanup
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def _http_get(url: str, retries: int = 3, timeout: int = 45) -> str:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}/{retries}] {e} — sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GET failed after {retries} retries: {url} ({last_err})")


def _strip_html(s: str) -> str:
    s = TAG_RE.sub(" ", s)
    # Clean up common HTML entities + non-breaking weirdness
    s = (s.replace("&nbsp;", " ")
           .replace("&amp;", "&")
           .replace("&#8217;", "'")
           .replace("&#8220;", '"')
           .replace("&#8221;", '"')
           .replace("&#187;", "")
           .replace("�", " ")
           .replace(" ", " "))
    return WS_RE.sub(" ", s).strip()


def parse_listing_page(html: str) -> list[dict]:
    """Extract advert id + post date + truncated body from the listing page."""
    out = []
    for m in ADVERT_RE.finditer(html):
        adv_id = m.group("id")
        panel = m.group("panel")
        time_m = TIME_RE.search(panel)
        ref_m = REFCODE_RE.search(panel)
        out.append({
            "advert_id": adv_id,
            "posted": time_m.group(1) if time_m else "",
            "refcode": ref_m.group(1) if ref_m else "",
            "detail_url": f"{BASE}/marketplace/wlm/advert/-Retail_{adv_id}",
        })
    return out


def parse_detail(html: str) -> dict:
    body = ""
    # First choice: JSON-LD description (full, untruncated body).
    for blk in JSON_LD_DESC_RE.finditer(html):
        m = JSON_LD_FIELD_RE.search(blk.group(1))
        if m:
            raw = m.group(1)
            # Unescape JSON: \n, \", \\
            raw = (raw.replace('\\"', '"')
                      .replace("\\n", "\n")
                      .replace("\\r", "")
                      .replace("\\\\", "\\"))
            body = raw.replace("�", " ")
            break
    # Fallback: meta description (truncated but better than nothing).
    if not body or len(body) < 100:
        m = META_DESC_RE.search(html)
        if m:
            body = (m.group(1)
                       .replace("&nbsp;", " ")
                       .replace("&amp;", "&")
                       .replace("&#10;", "\n")
                       .replace("�", " "))

    decedent = ""
    m = DECEDENT_RE.search(body)
    if m:
        decedent = m.group(1).strip(" ,.")
    elif "Estate of" in body:
        # Fallback heuristic: take the substring after "Estate of" up to the
        # next punctuation.
        idx = body.find("Estate of")
        tail = body[idx + len("Estate of"):][:120]
        decedent = re.split(r"[,;()]| Deceased| deceased", tail, maxsplit=1)[0].strip(" ,.")

    case_no = ""
    m = ESTATE_FILE_RE.search(body)
    if m:
        case_no = m.group(0)
    if not case_no:
        m = SP_CASE_RE.search(body)
        if m:
            case_no = m.group(0).upper()

    executor = ""
    m = EXEC_AFTER_RE.search(body)
    if m:
        executor = m.group(1).strip(" ,.")
    if not executor:
        m = EXEC_BEFORE_RE.search(body)
        if m:
            executor = m.group(1).strip(" ,.")

    deadline = ""
    m = CLAIM_DEADLINE_RE.search(body)
    if m:
        deadline = m.group(1).strip()

    has_nhc = bool(COUNTY_RE.search(body))
    return {
        "body": body,
        "decedent_name": decedent,
        "case_number": case_no,
        "executor_or_administrator": executor,
        "claim_deadline": deadline,
        "is_new_hanover": has_nhc,
    }


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
        return {"seen": [], "last_run_at": None}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def parse_posted_iso(posted: str) -> str:
    """Convert Gannett's '2026-05-05 00:00:00.0' to 'YYYY-MM-DD'."""
    if not posted:
        return ""
    posted = posted.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(posted, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return posted


def scrape_category(slug: str, listing_url: str, since_date: str,
                    limit_new: int, reset: bool, state_path: Path,
                    out_path: Path, flag: dict) -> dict:
    if reset:
        for p in (out_path, state_path):
            if p.exists():
                p.unlink()
                print(f"[reset] removed {p.name}")
    state = load_state(state_path)
    seen = set(state.get("seen", []))

    print(f"\n=== {slug} ===")
    print(f"[i] url:   {listing_url}")
    print(f"[i] seen:  {len(seen):,} (carried from prior runs)")
    print(f"[i] out:   {out_path}")

    fresh = []
    page = 1
    while True:
        if flag["stop"]:
            break
        url = listing_url if page == 1 else f"{listing_url}/{page}"
        try:
            html = _http_get(url)
        except RuntimeError as e:
            print(f"[!] page {page} fetch failed: {e}", file=sys.stderr)
            break
        items = parse_listing_page(html)
        if not items:
            print(f"[i] page {page}: empty — done")
            break
        new_on_page = [it for it in items if it["advert_id"] not in seen]
        print(f"[+] page {page}: {len(items)} listings, {len(new_on_page)} new")
        if not new_on_page:
            # Hit a page where every listing is seen — stop, since the listings
            # are reverse-chronological.
            break
        fresh.extend(new_on_page)
        if limit_new > 0 and len(fresh) >= limit_new:
            fresh = fresh[:limit_new]
            break
        page += 1
        time.sleep(RATE_LIMIT_SEC)

    print(f"[i] fetched {len(fresh)} new listings; pulling detail...")
    appended = 0
    nhc_kept = 0
    nhc_dropped = 0
    fetched_at = datetime.now(timezone.utc).isoformat()
    with out_path.open("a", encoding="utf-8") as fh:
        for it in fresh:
            if flag["stop"]:
                break
            try:
                detail_html = _http_get(it["detail_url"])
            except RuntimeError as e:
                print(f"[!] detail {it['advert_id']} failed: {e}", file=sys.stderr)
                seen.add(it["advert_id"])
                continue
            d = parse_detail(detail_html)
            posted_iso = parse_posted_iso(it["posted"])
            if since_date and posted_iso and posted_iso < since_date:
                # Older than --since; we've walked past the cutoff. Mark seen
                # and break — older pages will all be older.
                seen.add(it["advert_id"])
                print(f"[i] hit --since cutoff {since_date} (advert {it['advert_id']} posted {posted_iso})")
                flag["stop"] = True
                break
            row = {
                "advert_id": it["advert_id"],
                "refcode": it["refcode"],
                "posted_date": posted_iso,
                "detail_url": it["detail_url"],
                "decedent_name": d["decedent_name"],
                "case_number": d["case_number"],
                "executor_or_administrator": d["executor_or_administrator"],
                "claim_deadline": d["claim_deadline"],
                "is_new_hanover": d["is_new_hanover"],
                "body": d["body"][:2000],  # cap body size
                "_source": f"starnews_{slug}",
                "_fetched_at": fetched_at,
            }
            # NHC filter — drop adjacent-county notices.
            if not d["is_new_hanover"]:
                seen.add(it["advert_id"])
                nhc_dropped += 1
                continue
            fh.write(json.dumps(row) + "\n")
            seen.add(it["advert_id"])
            appended += 1
            nhc_kept += 1
            time.sleep(RATE_LIMIT_SEC)

    state["seen"] = sorted(seen)
    save_state(state_path, state)
    return {"slug": slug, "appended": appended, "nhc_kept": nhc_kept, "nhc_dropped": nhc_dropped}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="StarNews legal notices scraper.")
    p.add_argument("--category", choices=list(DEFAULT_CATEGORIES.keys()) + ["all"], default="all")
    p.add_argument("--limit", type=int, default=0,
                   help="Cap NEW listings fetched per category this run (0 = unlimited).")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--since", default="",
                   help="Only keep listings posted on or after YYYY-MM-DD; cuts pagination short.")
    return p.parse_args()


def main() -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    args = parse_args()
    flag = install_signal_handler()
    targets = list(DEFAULT_CATEGORIES.keys()) if args.category == "all" else [args.category]
    summaries = []
    for slug in targets:
        if flag["stop"]:
            break
        url = DEFAULT_CATEGORIES[slug]
        out_path = RAW_DIR / f"starnews_{slug}.jsonl"
        state_path = RAW_DIR / f"starnews_{slug}.state.json"
        summaries.append(scrape_category(slug, url, args.since, args.limit, args.reset, state_path, out_path, flag))

    print("\n=== summary ===")
    for s in summaries:
        print(f"  {s['slug']:30s} appended={s['appended']:>4}  nhc_kept={s['nhc_kept']:>4}  nhc_dropped={s['nhc_dropped']:>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
