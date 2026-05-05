"""energov_permits.py — NHC building permits scraper (demolition focus).

Pulls demolition permits (the distress signal we actually care about) from the
county's EnerGov ArcGIS feed. Total feed has ~475K permits with 99.2% PID
coverage, of which ~2,247 are residential or commercial demolitions. Pulling
demolitions only keeps daily refreshes light.

Endpoint:
    https://gis.nhcgov.com/server/rest/services/Thematic/EnergovPermitsPlans/FeatureServer/0

Filter: PERMIT_TYPE LIKE '%Demolition%' OR WORK_CLASS LIKE '%Demolition%'

Output:
    data/raw/energov_permits_demolition.jsonl  — all demo permits
Optional via --include-floodplain / --include-occupancy: companion JSONLs
for floodplain dev permits and occupancy certifications.

Resume model: OBJECTID cursor on the filtered query. State persists last
OBJECTID, restart picks up from `OBJECTID > <last>`. Idempotent.

Output rows are tagged with `_source` and `_doctype` so the pipeline can
disambiguate. Geometry dropped — we join on PID + mapidkey.
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
DEFAULT_FOLDER = "Thematic"
DEFAULT_SERVICE = "EnergovPermitsPlans"
DEFAULT_LAYER = 0
PAGE_SIZE = 2000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 "
    "(+contact: infinitygauntletllc@gmail.com)"
)

# Map of doc-type slug -> ArcGIS WHERE clause + filter rationale.
# These are the high-signal subsets per RECON.md. The base feed is 475K
# permits; we pull only the slices that map to a distress pattern.
DOCTYPE_FILTERS: dict[str, str] = {
    "demolition": "(PERMIT_TYPE LIKE '%Demolition%' OR WORK_CLASS LIKE '%Demolition%')",
    "floodplain_development": "PERMIT_TYPE LIKE '%Floodplain Development%'",
    "occupancy_certification": "PERMIT_TYPE LIKE '%Occupancy Certification%'",
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"


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


def _layer_url(root: str, folder: str, service: str, layer: int) -> str:
    return f"{root.rstrip('/')}/{folder}/{service}/FeatureServer/{layer}"


def fetch_count(layer_url: str, where: str) -> int:
    url = (
        f"{layer_url}/query?where={urllib.parse.quote(where)}"
        f"&returnCountOnly=true&f=json"
    )
    return int(_http_get_json(url).get("count", 0))


def fetch_page(layer_url: str, oid_field: str, base_where: str,
               last_oid: int, page_size: int) -> list[dict]:
    where = f"({base_where}) AND {oid_field} > {last_oid}"
    url = (
        f"{layer_url}/query"
        f"?where={urllib.parse.quote(where)}"
        f"&outFields=*"
        f"&orderByFields={oid_field}+ASC"
        f"&resultRecordCount={page_size}"
        f"&returnGeometry=false"
        f"&f=json"
    )
    return _http_get_json(url).get("features", [])


def install_signal_handler() -> dict:
    flag = {"stop": False}

    def handler(signum, frame):
        if flag["stop"]:
            sys.exit(130)
        flag["stop"] = True
        print("\n[!] interrupt — finishing current page then stopping...", file=sys.stderr)

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, handler)
    except (AttributeError, ValueError):
        pass
    return flag


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def scrape_doctype(layer_url: str, oid_field: str, doctype: str, base_where: str,
                   limit: int, page_size: int, since: str, reset: bool,
                   shared_state: dict, flag: dict) -> dict:
    out_path = RAW_DIR / f"energov_permits_{doctype}.jsonl"
    if reset and out_path.exists():
        out_path.unlink()
        print(f"[reset] removed {out_path.name}")

    sub_state = shared_state.setdefault(doctype, {"last_objectid": 0, "total_fetched": 0})
    if reset:
        sub_state.update({"last_objectid": 0, "total_fetched": 0})

    where = base_where
    if since:
        # Filter on ISSUE_DATE >= --since (epoch ms). ArcGIS quirk: ISO timestamps
        # via TIMESTAMP literal also work but ms is robust.
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            since_ms = int(since_dt.timestamp() * 1000)
            where = f"({base_where}) AND ISSUE_DATE >= {since_ms}"
        except ValueError:
            print(f"[!] --since must be YYYY-MM-DD (got {since!r}) — ignoring", file=sys.stderr)

    print(f"\n=== doctype: {doctype} ===")
    print(f"[i] where: {where}")
    total = fetch_count(layer_url, where)
    print(f"[i] total_filtered: {total:,}")

    last_oid = int(sub_state["last_objectid"])
    fetched = int(sub_state["total_fetched"])
    if last_oid > 0:
        print(f"[i] resume: last_objectid={last_oid} already_fetched={fetched:,}")

    pages = 0
    this_run = 0
    t0 = time.time()
    run_target = limit if limit and limit > 0 else None

    with out_path.open("a", encoding="utf-8") as fh:
        while True:
            if flag["stop"]:
                break
            features = fetch_page(layer_url, oid_field, where, last_oid, page_size)
            if not features:
                print("[i] empty page — done")
                break
            for feat in features:
                attrs = feat.get("attributes", {})
                oid = attrs.get(oid_field)
                if oid is None:
                    continue
                attrs["_source"] = "nhc_energov_permits"
                attrs["_doctype"] = doctype
                fh.write(json.dumps(attrs, default=str) + "\n")
                if oid > last_oid:
                    last_oid = oid
                fetched += 1
                this_run += 1
                if run_target is not None and this_run >= run_target:
                    break
            fh.flush()
            pages += 1
            sub_state["last_objectid"] = last_oid
            sub_state["total_fetched"] = fetched
            elapsed = time.time() - t0
            rate = this_run / elapsed if elapsed > 0 else 0
            print(f"[+] page {pages:>3}  oid<={last_oid:>10}  this_run={this_run:>6,}  total={fetched:>6,}/{total:,}  {rate:5.0f} rec/s")
            if run_target is not None and this_run >= run_target:
                print(f"[i] hit --limit {run_target}")
                break

    return {"doctype": doctype, "this_run": this_run, "total": fetched, "last_oid": last_oid}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NHC EnerGov building-permits scraper (distress filters).")
    p.add_argument("--service-root", default=DEFAULT_SERVICE_ROOT)
    p.add_argument("--folder", default=DEFAULT_FOLDER)
    p.add_argument("--service", default=DEFAULT_SERVICE)
    p.add_argument("--layer", type=int, default=DEFAULT_LAYER)
    p.add_argument("--page-size", type=int, default=PAGE_SIZE)
    p.add_argument("--limit", type=int, default=0,
                   help="Cap rows per doctype this run (0 = unlimited).")
    p.add_argument("--since", default="",
                   help="Filter ISSUE_DATE >= YYYY-MM-DD (incremental refresh).")
    p.add_argument("--reset", action="store_true",
                   help="Clear all output JSONLs + state for selected doctypes.")
    p.add_argument("--doctype", choices=list(DOCTYPE_FILTERS.keys()) + ["all"], default="demolition",
                   help="Which distress filter to pull. Default: demolition (highest signal).")
    return p.parse_args()


def main() -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    args = parse_args()
    layer_url = _layer_url(args.service_root, args.folder, args.service, args.layer)
    state_path = RAW_DIR / "energov_permits.state.json"

    print(f"[i] layer: {layer_url}")
    meta = _http_get_json(f"{layer_url}?f=json")
    oid_field = meta.get("objectIdField") or "OBJECTID"
    layer_max = int(meta.get("maxRecordCount") or PAGE_SIZE)
    page_size = min(args.page_size, layer_max)

    if args.reset and state_path.exists():
        state_path.unlink()
        print(f"[reset] removed {state_path.name}")
    state = load_state(state_path)

    flag = install_signal_handler()
    targets = list(DOCTYPE_FILTERS.keys()) if args.doctype == "all" else [args.doctype]
    summaries = []
    for dt in targets:
        if flag["stop"]:
            break
        s = scrape_doctype(
            layer_url, oid_field, dt, DOCTYPE_FILTERS[dt],
            args.limit, page_size, args.since, args.reset,
            state, flag,
        )
        summaries.append(s)
        save_state(state_path, state)

    print("\n=== summary ===")
    for s in summaries:
        print(f"  {s['doctype']:25s} run={s['this_run']:>6,} total={s['total']:>6,} oid<={s['last_oid']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
