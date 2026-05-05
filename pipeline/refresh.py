"""refresh.py — daily refresh harness for new-hanover-intel.

Single command that:
  1. Runs every scraper in dependency order.
  2. Re-runs `pipeline/build_leads.py` to regenerate `data/leads.json`.
  3. Updates `HEARTBEAT.json` with last_success per source.
  4. With `--push`: stages + commits + pushes the refreshed artifacts.

Designed to be invoked by Windows Task Scheduler (see
`scripts/daily_refresh.xml`) or by OpenClaw on cron (see `OPERATIONS.md`).

Exit codes (per spec):
  0 — full success, every scraper + pipeline ran cleanly
  1 — partial: at least one scraper failed but pipeline completed
  2 — pipeline / Two-Truths failure (do NOT push when this happens)

CLI:
  python pipeline/refresh.py [--push] [--since-days N] [--source NAME] [--dry-run]

`--source` repeatable, runs only the named scraper (slug from SCRAPERS).
`--dry-run` skips git push and pipeline write — use for sanity checks.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
LEADS_PATH = PROJECT_ROOT / "data" / "leads.json"
HEARTBEAT_PATH = PROJECT_ROOT / "HEARTBEAT.json"
LOG_PATH = RAW_DIR / "refresh.log"
PYTHON = os.environ.get("PYTHON_BIN") or sys.executable


@dataclass
class ScraperSpec:
    slug: str
    args: list[str] = field(default_factory=list)
    # Whether this scraper supports --since (most do)
    supports_since: bool = True
    # If true, --since is in mm/dd/yyyy (rod) — else YYYY-MM-DD
    since_format: str = "%Y-%m-%d"
    # Critical: parcel master must succeed; if it fails the pipeline can't run.
    critical: bool = False
    description: str = ""


# Dependency order: parcel master first; everything else can run in any order
# but we keep them sequential to be polite to the upstream services.
SCRAPERS: list[ScraperSpec] = [
    ScraperSpec(
        slug="property_owners",
        args=["scrapers/property_owners.py"],
        supports_since=False,  # ArcGIS layer has no usable date filter for owners
        critical=True,
        description="NHC ArcGIS PropertyOwners parcel master (~115K records)",
    ),
    ScraperSpec(
        slug="energov_permits_demolition",
        args=["scrapers/energov_permits.py", "--doctype", "demolition"],
        description="EnerGov demolition permits (code/distress signal)",
    ),
    ScraperSpec(
        slug="energov_permits_floodplain",
        args=["scrapers/energov_permits.py", "--doctype", "floodplain_development"],
        description="EnerGov floodplain dev permits (sub-flag)",
    ),
    ScraperSpec(
        slug="energov_permits_occupancy",
        args=["scrapers/energov_permits.py", "--doctype", "occupancy_certification"],
        description="EnerGov occupancy certifications (sub-flag)",
    ),
    ScraperSpec(
        slug="nhc_stormwater",
        args=["scrapers/nhc_stormwater.py"],
        supports_since=False,
        description="Stormwater permits (sub-flag)",
    ),
    ScraperSpec(
        slug="delinquent_tax",
        args=["scrapers/delinquent_tax.py"],
        supports_since=False,
        description="NHC delinquent tax CSV (monthly snapshot)",
    ),
    ScraperSpec(
        slug="nhc_foreclosures",
        args=["scrapers/nhc_foreclosures.py"],
        supports_since=False,
        description="NHC GS 105-374 tax foreclosure schedule",
    ),
    ScraperSpec(
        slug="starnews_legals",
        args=["scrapers/starnews_legals.py"],
        description="StarNews Notice to Creditors + Foreclosures",
    ),
    # ROD per-doctype — each is a separate invocation. Pull only the
    # high-signal slices for daily refresh; baseline DEED/D-T/ASGMT do not
    # fire patterns and would burn the 2000-record cap fast.
    ScraperSpec(slug="rod_foreclosure",
                args=["scrapers/rod.py", "--doctype", "foreclosure"],
                since_format="%m/%d/%Y",
                description="ROD foreclosure (FCL/N/F/SUB TR variants)"),
    ScraperSpec(slug="rod_estate_deed",
                args=["scrapers/rod.py", "--doctype", "estate_deed"],
                since_format="%m/%d/%Y",
                description="ROD estate deeds (ADMIN/EXEC/EXTRX/COMMR/SHERIF)"),
    ScraperSpec(slug="rod_judgment",
                args=["scrapers/rod.py", "--doctype", "judgment"],
                since_format="%m/%d/%Y"),
    ScraperSpec(slug="rod_lien",
                args=["scrapers/rod.py", "--doctype", "lien"],
                since_format="%m/%d/%Y"),
    ScraperSpec(slug="rod_quitclaim",
                args=["scrapers/rod.py", "--doctype", "quitclaim"],
                since_format="%m/%d/%Y"),
    ScraperSpec(slug="rod_deed_of_gift",
                args=["scrapers/rod.py", "--doctype", "deed_of_gift"],
                since_format="%m/%d/%Y"),
    ScraperSpec(slug="rod_separation",
                args=["scrapers/rod.py", "--doctype", "separation"],
                since_format="%m/%d/%Y"),
    ScraperSpec(slug="rod_satisfaction",
                args=["scrapers/rod.py", "--doctype", "satisfaction"],
                since_format="%m/%d/%Y"),
]


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_heartbeat() -> dict:
    if not HEARTBEAT_PATH.exists():
        return {"last_success_timestamp": None, "per_source": {}, "failed_sources": []}
    return json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))


def save_heartbeat(beat: dict) -> None:
    HEARTBEAT_PATH.write_text(json.dumps(beat, indent=2), encoding="utf-8")


def run_scraper(spec: ScraperSpec, since_iso: str) -> tuple[bool, float, str]:
    args = [PYTHON, *spec.args]
    if spec.supports_since and since_iso:
        try:
            since_dt = datetime.strptime(since_iso, "%Y-%m-%d")
        except ValueError:
            since_dt = None
        if since_dt:
            formatted = since_dt.strftime(spec.since_format)
            args += ["--since", formatted]
    log(f"[scraper:{spec.slug}] -> {' '.join(args)}")
    t0 = time.time()
    try:
        proc = subprocess.run(args, cwd=str(PROJECT_ROOT), check=False,
                              capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired as e:
        elapsed = time.time() - t0
        log(f"[scraper:{spec.slug}] TIMEOUT after {elapsed:.1f}s")
        return False, elapsed, str(e)
    elapsed = time.time() - t0
    tail = (proc.stdout or "")[-1500:]
    err = (proc.stderr or "")[-1500:]
    log(f"[scraper:{spec.slug}] exit={proc.returncode} elapsed={elapsed:.1f}s")
    if proc.returncode != 0:
        log(f"[scraper:{spec.slug}] stderr tail:\n{err}")
        log(f"[scraper:{spec.slug}] stdout tail:\n{tail}")
    return proc.returncode == 0, elapsed, tail or err


def run_pipeline() -> tuple[bool, str]:
    args = [PYTHON, "pipeline/build_leads.py"]
    log(f"[pipeline] -> {' '.join(args)}")
    proc = subprocess.run(args, cwd=str(PROJECT_ROOT), check=False,
                          capture_output=True, text=True, timeout=600)
    out = (proc.stdout or "")
    if proc.returncode != 0:
        log(f"[pipeline] FAILED rc={proc.returncode}")
        log(f"[pipeline] stderr:\n{proc.stderr}")
        log(f"[pipeline] stdout tail:\n{out[-2000:]}")
        return False, out
    log(f"[pipeline] success — leads.json regenerated")
    return True, out


def git_push(commit_msg: str) -> bool:
    """Stage leads + heartbeat, commit, push. Returns True on success."""
    paths = ["data/leads.json", "data/leads.previous.json", "HEARTBEAT.json"]
    paths = [p for p in paths if (PROJECT_ROOT / p).exists()]
    if not paths:
        log("[git] nothing to commit")
        return True
    try:
        subprocess.run(["git", "add", *paths], cwd=str(PROJECT_ROOT), check=True,
                       capture_output=True, text=True)
        st = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(PROJECT_ROOT),
                            check=False, capture_output=True, text=True)
        if st.returncode == 0:
            log("[git] no staged changes — skipping commit")
            return True
        subprocess.run(["git", "commit", "-m", commit_msg],
                       cwd=str(PROJECT_ROOT), check=True,
                       capture_output=True, text=True)
        push = subprocess.run(["git", "push"], cwd=str(PROJECT_ROOT),
                              check=False, capture_output=True, text=True)
        if push.returncode != 0:
            log(f"[git] push failed: {push.stderr}")
            return False
        log("[git] pushed")
        return True
    except subprocess.CalledProcessError as e:
        log(f"[git] error: {e.stderr or e}")
        return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="new-hanover-intel daily refresh.")
    p.add_argument("--push", action="store_true",
                   help="git add + commit + push leads.json on success.")
    p.add_argument("--since-days", type=int, default=14,
                   help="Pass --since N days ago to scrapers that support it (default: 14).")
    p.add_argument("--source", action="append", default=[],
                   help="Run only the named scraper (slug). Repeatable.")
    p.add_argument("--skip", action="append", default=[],
                   help="Skip the named scraper. Repeatable.")
    p.add_argument("--dry-run", action="store_true",
                   help="Skip git push and pipeline write.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    since_iso = (datetime.now(timezone.utc) - timedelta(days=args.since_days)).strftime("%Y-%m-%d")
    log(f"=== refresh start  since={since_iso}  push={args.push}  dry_run={args.dry_run} ===")

    beat = load_heartbeat()
    failed_sources: list[str] = []
    parcel_failed = False

    selected = SCRAPERS
    if args.source:
        wanted = set(args.source)
        selected = [s for s in SCRAPERS if s.slug in wanted]
        log(f"[i] running only: {[s.slug for s in selected]}")
    if args.skip:
        skip = set(args.skip)
        selected = [s for s in selected if s.slug not in skip]

    for spec in selected:
        ok, elapsed, _ = run_scraper(spec, since_iso)
        beat.setdefault("per_source", {})[spec.slug] = {
            "ok": ok,
            "elapsed_sec": round(elapsed, 1),
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        }
        if ok:
            beat["per_source"][spec.slug]["last_success_at"] = beat["per_source"][spec.slug]["last_attempt_at"]
        else:
            failed_sources.append(spec.slug)
            if spec.critical:
                parcel_failed = True
                log(f"[!] critical scraper failed: {spec.slug} — pipeline cannot run")
                save_heartbeat(beat)
                return 2
        save_heartbeat(beat)

    pipeline_ok, pipeline_out = run_pipeline()
    if not pipeline_ok:
        log("[!] pipeline failed — Two-Truths violation or load error. NOT pushing.")
        beat["last_pipeline_at"] = datetime.now(timezone.utc).isoformat()
        beat["last_pipeline_ok"] = False
        save_heartbeat(beat)
        return 2

    # Update heartbeat with success
    beat["last_pipeline_at"] = datetime.now(timezone.utc).isoformat()
    beat["last_pipeline_ok"] = True
    if not failed_sources:
        beat["last_success_timestamp"] = beat["last_pipeline_at"]
    beat["failed_sources"] = failed_sources
    save_heartbeat(beat)

    # Git push
    if args.push and not args.dry_run:
        leads = json.loads(LEADS_PATH.read_text(encoding="utf-8")) if LEADS_PATH.exists() else {}
        tc = leads.get("tier_counts", {})
        msg = (f"chore(data): daily refresh "
               f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} - "
               f"{tc.get('hot', 0)} hot / {tc.get('warm', 0)} warm / {tc.get('active', 0)} active")
        push_ok = git_push(msg)
        if not push_ok:
            log("[!] push failed — heartbeat marked, manual push required")
            return 1

    log(f"=== refresh end  failed_sources={failed_sources} ===")
    return 0 if not failed_sources else 1


if __name__ == "__main__":
    sys.exit(main())
