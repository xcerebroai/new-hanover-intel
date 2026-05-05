"""delinquent_tax.py — NHC delinquent tax CSV downloader.

The county publishes a 5,120-row CSV of all delinquent property tax accounts
on the first business day of each month. Direct download, no auth, no
CAPTCHA. CivicEngage's CMS returns 404 to HEAD requests but 200 to GET with
a browser UA — important detail.

URL: https://www.nhcgov.com/DocumentCenter/View/11283/Delinquent_Taxpayers_Report_CSV

Schema (from RECON.md):
  Customer Account, Name 1, Juris Code, Juris Description,
  Location No., Location No. Suffix, Location Street, Location Apt.,
  Property Code, Parcel, Total Due, Last Payment Date

Output: data/raw/delinquent_tax.jsonl — one JSONL row per CSV row, with
`_source: "nhc_delinquent_csv"` and `_fetched_at` timestamp added.

This source is a snapshot, not an append-only log — every refresh replaces
the file entirely. The state file records the source ETag / Last-Modified
so the harness can detect "unchanged since last refresh" and skip rewrite.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CSV_URL = (
    "https://www.nhcgov.com/DocumentCenter/View/11283/Delinquent_Taxpayers_Report_CSV"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 "
    "(+contact: infinitygauntletllc@gmail.com)"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
SLUG = "delinquent_tax"


def _http_get(url: str, retries: int = 3, timeout: int = 60) -> tuple[bytes, dict]:
    """GET with retry. Returns (body_bytes, headers_dict)."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/csv,application/octet-stream,*/*",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return body, hdrs
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}/{retries}] {e} — sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GET failed after {retries} retries: {url} ({last_err})")


def install_signal_handler() -> dict:
    flag = {"stop": False}

    def handler(signum, frame):
        if flag["stop"]:
            print("\n[!] second interrupt — exiting hard", file=sys.stderr)
            sys.exit(130)
        print("\n[!] interrupt — finishing current row then stopping...", file=sys.stderr)
        flag["stop"] = True

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, handler)
    except (AttributeError, ValueError):
        pass
    return flag


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"last_etag": None, "last_modified": None, "rows": 0, "last_run_at": None}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def parse_csv_to_records(body: bytes) -> list[dict]:
    """Parse the CSV body. Field-canonicalize, no scoring."""
    text = body.decode("utf-8-sig")  # tolerate BOM
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for row in reader:
        out.append({
            "customer_account": (row.get("Customer Account") or "").strip(),
            "name": (row.get("Name 1") or "").strip(),
            "juris_code": (row.get("Juris Code") or "").strip(),
            "juris_description": (row.get("Juris Description") or "").strip(),
            "location_number": (row.get("Location No.") or "").strip(),
            "location_suffix": (row.get("Location No. Suffix") or "").strip(),
            "location_street": (row.get("Location Street") or "").strip(),
            "location_apt": (row.get("Location Apt.") or "").strip(),
            "property_code": (row.get("Property Code") or "").strip(),
            "parcel": (row.get("Parcel") or "").strip(),
            "total_due": _parse_money(row.get("Total Due")),
            "last_payment_date": (row.get("Last Payment Date") or "").strip(),
            "_source": "nhc_delinquent_csv",
        })
    return out


def _parse_money(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip().replace("$", "").replace(",", "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def run(args: argparse.Namespace) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / f"{SLUG}.jsonl"
    state_path = RAW_DIR / f"{SLUG}.state.json"

    if args.reset:
        for p in (out_path, state_path):
            if p.exists():
                p.unlink()
                print(f"[reset] removed {p.name}")

    state = load_state(state_path)
    install_signal_handler()

    print(f"[i] url:    {args.url}")
    print(f"[i] out:    {out_path}")
    print(f"[i] state:  {state_path}")

    body, hdrs = _http_get(args.url)
    etag = hdrs.get("etag")
    last_mod = hdrs.get("last-modified")
    print(f"[i] etag:           {etag}")
    print(f"[i] last-modified:  {last_mod}")
    print(f"[i] body size:      {len(body):,} bytes")

    if (etag and state.get("last_etag") == etag) and not args.force:
        print(f"[skip] CSV unchanged since last fetch (etag match) — use --force to refresh anyway")
        return 0

    records = parse_csv_to_records(body)
    if args.limit and args.limit > 0:
        records = records[: args.limit]

    fetched_at = datetime.now(timezone.utc).isoformat()
    # Snapshot semantics — overwrite the JSONL fully on each refresh.
    tmp = out_path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in records:
            r["_fetched_at"] = fetched_at
            fh.write(json.dumps(r) + "\n")
    tmp.replace(out_path)

    state["last_etag"] = etag
    state["last_modified"] = last_mod
    state["rows"] = len(records)
    save_state(state_path, state)

    print(f"[done] wrote {len(records):,} rows to {out_path.name}")
    if records:
        sample = records[0]
        print(f"[sample] parcel={sample['parcel']!r} due={sample['total_due']} juris={sample['juris_code']!r}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NHC delinquent tax CSV scraper.")
    p.add_argument("--url", default=DEFAULT_CSV_URL,
                   help="Override the source URL (default: NHC DocumentCenter View 11283).")
    p.add_argument("--limit", type=int, default=0,
                   help="Cap rows written (0 = unlimited). For smoke testing only.")
    p.add_argument("--reset", action="store_true",
                   help="Delete existing JSONL + state and re-fetch.")
    p.add_argument("--since", default="",
                   help="(Reserved — CSV is a full-snapshot source; --since has no effect.)")
    p.add_argument("--force", action="store_true",
                   help="Re-fetch even when ETag matches the last successful run.")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
