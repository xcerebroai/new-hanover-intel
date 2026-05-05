"""diagnose_live.py — Playwright diagnosis of the live dashboard.

Loads https://xcerebroai.github.io/new-hanover-intel/?view=all in real
Chromium, captures all console messages, page errors, and network
responses, waits up to 30s for the table body to populate, and writes a
structured JSON diagnosis to data/raw/live_diagnosis.json.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIAG_PATH = PROJECT_ROOT / "data" / "raw" / "live_diagnosis.json"
LIVE_URL = sys.argv[1] if len(sys.argv) > 1 else "https://xcerebroai.github.io/new-hanover-intel/?view=all"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    from playwright.sync_api import sync_playwright

    diag = {
        "url": LIVE_URL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tbody_populated": False,
        "row_count": 0,
        "console_errors": [],
        "console_logs": [],
        "page_errors": [],
        "failed_requests": [],
        "leads_json_response": None,
        "tbody_text_snippet": "",
        "html_title": "",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        leads_json_resp = {"status": None, "size": None, "fetch_time_ms": None}

        def on_console(msg):
            entry = {"type": msg.type, "text": msg.text}
            if msg.type == "error":
                diag["console_errors"].append(entry)
            else:
                diag["console_logs"].append(entry)
            if len(diag["console_logs"]) > 50:
                diag["console_logs"] = diag["console_logs"][-50:]

        def on_pageerror(exc):
            diag["page_errors"].append(str(exc))

        def on_response(resp):
            url = resp.url
            try:
                if "leads.json" in url:
                    nonlocal_status = resp.status
                    try:
                        body = resp.body()
                        size = len(body)
                    except Exception:
                        size = None
                    leads_json_resp["status"] = nonlocal_status
                    leads_json_resp["size"] = size
                if resp.status >= 400:
                    diag["failed_requests"].append({
                        "url": url, "status": resp.status,
                        "error": None,
                    })
            except Exception as e:
                diag["failed_requests"].append({"url": url, "error": str(e)})

        def on_requestfailed(req):
            diag["failed_requests"].append({
                "url": req.url,
                "error": req.failure or "request failed (no detail)",
                "status": None,
            })

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)
        page.on("response", on_response)
        page.on("requestfailed", on_requestfailed)

        t0 = time.time()
        try:
            page.goto(LIVE_URL, wait_until="load", timeout=30000)
        except Exception as e:
            diag["page_errors"].append(f"goto failed: {e}")

        diag["html_title"] = page.title()

        # Wait up to 30 seconds for the tbody to populate with real rows
        # (i.e. tr.row-anchor elements, not the "Loading leads..." placeholder)
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('tbody tr.row-anchor').length > 0 "
                "|| document.querySelector('.empty-state') !== null",
                timeout=30000,
            )
        except Exception as e:
            diag["page_errors"].append(f"wait_for rows timed out: {type(e).__name__}: {e}")

        elapsed_ms = int((time.time() - t0) * 1000)
        leads_json_resp["fetch_time_ms"] = elapsed_ms

        rows = page.locator("tbody tr.row-anchor").count()
        diag["row_count"] = rows
        diag["tbody_populated"] = rows > 0

        # Snapshot the first 200 chars of tbody text for evidence
        try:
            tbody_text = page.locator("tbody").first.inner_text()
            diag["tbody_text_snippet"] = tbody_text[:300]
        except Exception:
            pass

        diag["leads_json_response"] = leads_json_resp

        browser.close()

    DIAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DIAG_PATH.write_text(json.dumps(diag, indent=2, default=str), encoding="utf-8")

    # Console summary
    print(f"=== Live diagnosis ===")
    print(f"  URL:                 {LIVE_URL}")
    print(f"  HTML title:          {diag['html_title']!r}")
    print(f"  tbody populated:     {diag['tbody_populated']}")
    print(f"  row count:           {diag['row_count']}")
    print(f"  page errors:         {len(diag['page_errors'])}")
    for e in diag["page_errors"][:5]:
        print(f"    - {e}")
    print(f"  console errors:      {len(diag['console_errors'])}")
    for e in diag["console_errors"][:5]:
        print(f"    - [{e['type']}] {e['text']}")
    print(f"  failed requests:     {len(diag['failed_requests'])}")
    for r in diag["failed_requests"][:5]:
        print(f"    - {r}")
    print(f"  leads.json fetched:  status={leads_json_resp['status']} size={leads_json_resp['size']} elapsed_ms={leads_json_resp['fetch_time_ms']}")
    print(f"  tbody snippet:       {diag['tbody_text_snippet']!r}")
    print(f"\n  diag saved: {DIAG_PATH}")
    return 0 if diag["tbody_populated"] else 1


if __name__ == "__main__":
    sys.exit(main())
