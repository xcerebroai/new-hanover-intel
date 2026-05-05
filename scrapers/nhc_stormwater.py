"""nhc_stormwater.py — NHC stormwater permits scraper.

Sub-signal source. Pulls all stormwater permits from the county's
ArcGIS Server (~1,116 records). Carries `PID`, `PIN`, `MAPID`, `MAPIDKEY`,
plus `EnergovNumber` to cross-link permits feed. Useful for flagging
parcels with active stormwater non-compliance — typically commercial
properties that have lapsed maintenance.

Endpoint:
    https://gis.nhcgov.com/server/rest/services/Layers/Stormwater_Impervious/FeatureServer/1

Resume model: OBJECTID cursor; idempotent restart from last_objectid.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SERVICE_ROOT = "https://gis.nhcgov.com/server/rest/services"
DEFAULT_FOLDER = "Layers"
DEFAULT_SERVICE = "Stormwater_Impervious"
DEFAULT_LAYER = 1
PAGE_SIZE = 2000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 "
    "(+contact: infinitygauntletllc@gmail.com)"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
SLUG = "nhc_stormwater"


def _http_get_json(url: str, retries: int = 4, timeout: int = 60) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}/{retries}] {e} — sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GET failed after {retries} retries: {url} ({last_err})")


def fetch_total(layer_url: str) -> int:
    return int(_http_get_json(f"{layer_url}/query?where=1%3D1&returnCountOnly=true&f=json").get("count", 0))


def fetch_page(layer_url: str, oid_field: str, last_oid: int, page_size: int) -> list[dict]:
    where = urllib.parse.quote(f"{oid_field} > {last_oid}")
    url = (
        f"{layer_url}/query?where={where}"
        f"&outFields=*&orderByFields={oid_field}+ASC"
        f"&resultRecordCount={page_size}&returnGeometry=false&f=json"
    )
    return _http_get_json(url).get("features", [])


def install_signal_handler() -> dict:
    flag = {"stop": False}

    def handler(signum, frame):
        if flag["stop"]:
            sys.exit(130)
        flag["stop"] = True

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, handler)
    except (AttributeError, ValueError):
        pass
    return flag


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"last_objectid": 0, "total_fetched": 0, "last_run_at": None}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def run(args: argparse.Namespace) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    layer_url = (
        f"{args.service_root.rstrip('/')}/{args.folder}/{args.service}"
        f"/FeatureServer/{args.layer}"
    )
    out_path = RAW_DIR / f"{SLUG}.jsonl"
    state_path = RAW_DIR / f"{SLUG}.state.json"

    if args.reset:
        for p in (out_path, state_path):
            if p.exists():
                p.unlink()
                print(f"[reset] removed {p.name}")

    print(f"[i] layer: {layer_url}")
    meta = _http_get_json(f"{layer_url}?f=json")
    oid_field = meta.get("objectIdField") or "OBJECTID"
    layer_max = int(meta.get("maxRecordCount") or PAGE_SIZE)
    page_size = min(args.page_size, layer_max)

    total = fetch_total(layer_url)
    print(f"[i] name:  {meta.get('name')}")
    print(f"[i] total: {total:,}  page: {page_size}")

    state = load_state(state_path)
    last_oid = int(state["last_objectid"])
    fetched = int(state["total_fetched"])
    if last_oid > 0:
        print(f"[i] resume: last_objectid={last_oid} already_fetched={fetched:,}")

    flag = install_signal_handler()
    pages = this_run = 0
    t0 = time.time()
    run_target = args.limit if args.limit and args.limit > 0 else None

    with out_path.open("a", encoding="utf-8") as fh:
        while True:
            if flag["stop"]:
                break
            feats = fetch_page(layer_url, oid_field, last_oid, page_size)
            if not feats:
                print("[i] empty page — done")
                break
            for f in feats:
                attrs = f.get("attributes", {})
                oid = attrs.get(oid_field)
                if oid is None:
                    continue
                attrs["_source"] = SLUG
                fh.write(json.dumps(attrs, default=str) + "\n")
                if oid > last_oid:
                    last_oid = oid
                fetched += 1
                this_run += 1
                if run_target is not None and this_run >= run_target:
                    break
            fh.flush()
            pages += 1
            state["last_objectid"] = last_oid
            state["total_fetched"] = fetched
            save_state(state_path, state)
            elapsed = time.time() - t0
            rate = this_run / elapsed if elapsed > 0 else 0
            print(f"[+] page {pages:>3}  oid<={last_oid:>10}  total={fetched:>5,}/{total:,}  {rate:5.0f} rec/s")
            if run_target is not None and this_run >= run_target:
                print(f"[i] hit --limit {run_target}")
                break

    save_state(state_path, state)
    print(f"[done] this_run={this_run:,} total={fetched:,} oid<={last_oid}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NHC stormwater permits scraper.")
    p.add_argument("--service-root", default=DEFAULT_SERVICE_ROOT)
    p.add_argument("--folder", default=DEFAULT_FOLDER)
    p.add_argument("--service", default=DEFAULT_SERVICE)
    p.add_argument("--layer", type=int, default=DEFAULT_LAYER)
    p.add_argument("--page-size", type=int, default=PAGE_SIZE)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--since", default="",
                   help="(Reserved — stormwater feed has no useful date for incremental.)")
    p.add_argument("--reset", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
