"""property_owners.py — New Hanover County parcel master scraper.

Pulls the canonical owner/parcel/address/value table from the county's
on-prem ArcGIS Server (gis.nhcgov.com). This is the equivalent of POLARIS in
the Mecklenburg precedent — every other scraper joins back to this on the
PARID field (format `R#####-###-###-###`, ~115K records).

Default target: Layers/PropertyOwners/FeatureServer/0 (~115,345 owner records,
52 fields including PARID, OWN1, owner mail address, site address, sale
date/price/instrument, assessed values, jurisdiction code).

Resume model: an objectid cursor on `OBJECTID`. Persisted to a checkpoint
file; restart picks up from `OBJECTID > <last>`. Idempotent.

Output: JSONL, one parcel attribute row per line, written to data/raw/.
Geometry dropped — the join key is PARID, not lat/lon.
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
DEFAULT_SERVICE = "PropertyOwners"
DEFAULT_LAYER = 0
PAGE_SIZE = 2000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 "
    "(+contact: infinitygauntletllc@gmail.com)"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"


def _http_get_json(url: str, retries: int = 4, timeout: int = 60) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
            data = json.loads(payload)
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


def fetch_layer_metadata(layer_url: str) -> dict:
    return _http_get_json(f"{layer_url}?f=json")


def fetch_total_count(layer_url: str) -> int:
    d = _http_get_json(f"{layer_url}/query?where=1%3D1&returnCountOnly=true&f=json")
    return int(d.get("count", 0))


def fetch_page(layer_url: str, oid_field: str, last_oid: int, page_size: int) -> list[dict]:
    where = urllib.parse.quote(f"{oid_field} > {last_oid}")
    url = (
        f"{layer_url}/query"
        f"?where={where}"
        f"&outFields=*"
        f"&orderByFields={oid_field}+ASC"
        f"&resultRecordCount={page_size}"
        f"&returnGeometry=false"
        f"&f=json"
    )
    return _http_get_json(url).get("features", [])


def load_checkpoint(path: Path) -> dict:
    if not path.exists():
        return {"last_objectid": 0, "total_fetched": 0, "started_at": None, "last_run_at": None}
    return json.loads(path.read_text(encoding="utf-8"))


def save_checkpoint(path: Path, state: dict) -> None:
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def install_signal_handler() -> dict:
    flag = {"stop": False}

    def handler(signum, frame):
        if flag["stop"]:
            print("\n[!] second interrupt — exiting hard", file=sys.stderr)
            sys.exit(130)
        print("\n[!] interrupt — finishing current page then stopping...", file=sys.stderr)
        flag["stop"] = True

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, handler)
    except (AttributeError, ValueError):
        pass
    return flag


def run(args: argparse.Namespace) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    layer_url = _layer_url(args.service_root, args.folder, args.service, args.layer)
    slug = f"property_owners_layer{args.layer}"
    out_path = Path(args.out) if args.out else RAW_DIR / f"{slug}.jsonl"
    ckpt_path = RAW_DIR / f"{slug}.checkpoint.json"

    if args.reset:
        for p in (out_path, ckpt_path):
            if p.exists():
                p.unlink()
                print(f"[reset] removed {p.name}")

    print(f"[i] layer:  {layer_url}")
    meta = fetch_layer_metadata(layer_url)
    oid_field = meta.get("objectIdField") or "OBJECTID"
    layer_max = int(meta.get("maxRecordCount") or PAGE_SIZE)
    page_size = min(args.page_size, layer_max)

    total = fetch_total_count(layer_url)
    print(f"[i] name:   {meta.get('name')}")
    print(f"[i] oid:    {oid_field}")
    print(f"[i] total:  {total:,}")
    print(f"[i] page:   {page_size}")
    print(f"[i] out:    {out_path}")
    print(f"[i] ckpt:   {ckpt_path}")

    state = load_checkpoint(ckpt_path)
    if state["started_at"] is None:
        state["started_at"] = datetime.now(timezone.utc).isoformat()
    last_oid = int(state["last_objectid"])
    fetched = int(state["total_fetched"])
    if last_oid > 0:
        print(f"[i] resume: last_objectid={last_oid}  already_fetched={fetched:,}")

    run_target = args.limit if args.limit and args.limit > 0 else None
    flag = install_signal_handler()
    pages = 0
    this_run = 0
    t0 = time.time()

    with out_path.open("a", encoding="utf-8") as fh:
        while True:
            if flag["stop"]:
                break
            features = fetch_page(layer_url, oid_field, last_oid, page_size)
            if not features:
                print("[i] empty page — done")
                break
            for feat in features:
                attrs = feat.get("attributes", {})
                oid = attrs.get(oid_field)
                if oid is None:
                    continue
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
            save_checkpoint(ckpt_path, state)
            elapsed = time.time() - t0
            rate = this_run / elapsed if elapsed > 0 else 0
            print(
                f"[+] page {pages:>4}  oid<={last_oid:>10}  "
                f"this_run={this_run:>7,}  total={fetched:>7,}/{total:,}  "
                f"{rate:5.0f} rec/s"
            )
            if run_target is not None and this_run >= run_target:
                print(f"[i] hit --limit {run_target}")
                break

    save_checkpoint(ckpt_path, state)
    print(f"[done] this_run={this_run:,} total={fetched:,} oid<={last_oid}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NHC PropertyOwners parcel master scraper.")
    p.add_argument("--service-root", default=DEFAULT_SERVICE_ROOT)
    p.add_argument("--folder", default=DEFAULT_FOLDER)
    p.add_argument("--service", default=DEFAULT_SERVICE)
    p.add_argument("--layer", type=int, default=DEFAULT_LAYER)
    p.add_argument("--page-size", type=int, default=PAGE_SIZE)
    p.add_argument("--limit", type=int, default=0,
                   help="Cap rows fetched in THIS run (0 = unlimited).")
    p.add_argument("--out", default="")
    p.add_argument("--reset", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
