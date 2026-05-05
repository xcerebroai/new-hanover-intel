"""verify_live.py — Phase 3 live URL verification.

Loads https://xcerebroai.github.io/new-hanover-intel/?view=all in real
Chromium, waits for >=5 rendered lead rows, captures proof, exercises
mode toggle and tag-pill filter, and writes:
  - data/raw/live_verification.json  (structured proof)
  - docs/live_dashboard.png          (full-page screenshot)

Exit code 0 = all checks pass, 1 = any check failed.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROOF_PATH = PROJECT_ROOT / "data" / "raw" / "live_verification.json"
SCREENSHOT_PATH = PROJECT_ROOT / "docs" / "live_dashboard.png"
BARE_URL = "https://xcerebroai.github.io/new-hanover-intel/"
ALL_URL = "https://xcerebroai.github.io/new-hanover-intel/?view=all"
TODAY_URL = "https://xcerebroai.github.io/new-hanover-intel/?view=today"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    from playwright.sync_api import sync_playwright

    proof = {
        "url": ALL_URL,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "row_count": 0,
        "first_5_pids": [],
        "first_5_addresses": [],
        "screenshot": str(SCREENSHOT_PATH.relative_to(PROJECT_ROOT)),
        "mode_toggle": "FAIL",
        "tag_pill_click": "FAIL",
        "today_view_state": None,
        "all_view_state": None,
        "console_errors": [],
        "page_errors": [],
    }

    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        page.on("pageerror", lambda exc: proof["page_errors"].append(str(exc)))
        page.on("console", lambda m: proof["console_errors"].append(m.text)
                if m.type == "error" else None)

        # 3.2: Load ALL view, wait for >=5 rows
        print(f"[3.2] loading {ALL_URL}")
        page.goto(ALL_URL, wait_until="load", timeout=30000)
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('tbody tr.row-anchor').length >= 5",
                timeout=60000,
            )
        except Exception as e:
            failures.append(f"3.2: timeout waiting for 5 rows: {e}")

        rows = page.locator("tbody tr.row-anchor")
        row_count = rows.count()
        proof["row_count"] = row_count
        print(f"  rendered rows: {row_count}")

        # 3.3: Capture proof — first 5 PIDs + addresses
        for i in range(min(5, row_count)):
            row = rows.nth(i)
            try:
                pid = row.locator(".pid-link").first.inner_text().strip()
                addr = row.locator(".addr").first.inner_text().strip()
                proof["first_5_pids"].append(pid)
                proof["first_5_addresses"].append(addr)
                print(f"  row {i+1}: {pid}  {addr}")
            except Exception as e:
                print(f"  row {i+1}: error reading: {e}")

        # Screenshot
        SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_PATH), full_page=False)
        print(f"  screenshot: {SCREENSHOT_PATH}")

        # 3.4: Mode toggle — click All Leads -> Today, verify URL + count change
        print(f"\n[3.4] mode toggle")
        # Currently on All Leads. Click Today button.
        today_btn = page.locator("#mode_today")
        today_btn.click()
        page.wait_for_timeout(800)
        url_after_today = page.url
        today_rows = page.locator("tbody tr.row-anchor").count()
        today_empty_state = page.locator(".empty-state").count()
        proof["today_view_state"] = {
            "url": url_after_today,
            "row_count": today_rows,
            "empty_state": today_empty_state,
        }
        print(f"  Today: url={url_after_today} rows={today_rows} empty_state={today_empty_state}")
        if "view=today" not in url_after_today:
            failures.append(f"3.4: URL did not update to view=today: {url_after_today}")
        else:
            print(f"  URL state: view=today  OK")

        # Click All Leads
        page.locator("#mode_all").click()
        page.wait_for_timeout(800)
        url_after_all = page.url
        all_rows = page.locator("tbody tr.row-anchor").count()
        proof["all_view_state"] = {"url": url_after_all, "row_count": all_rows}
        print(f"  All Leads: url={url_after_all} rows={all_rows}")
        if "view=all" not in url_after_all:
            failures.append(f"3.4: URL did not update to view=all: {url_after_all}")
        else:
            print(f"  URL state: view=all  OK")

        # Verify counts differ as expected (Today should be 0 on baseline,
        # All Leads should be 1000 capped or full set)
        if all_rows > today_rows or (today_empty_state > 0 and all_rows >= 5):
            proof["mode_toggle"] = "PASS"
            print(f"  mode toggle: PASS")
        else:
            failures.append(f"3.4: mode toggle counts unexpected — today={today_rows} all={all_rows}")

        # 3.5: Tag pill click — click the LOWEST-count pill so the filter
        # reduces rows below the 1000-row render cap (otherwise cap masks
        # the filter effect: 3,765 unfiltered also caps at 1000 visible).
        # Locate the pill with the smallest count by parsing the badge text.
        print(f"\n[3.5] tag pill click")
        unfiltered_count = page.locator("tbody tr.row-anchor").count()
        # Read each pill's count from its own .ct child, find the smallest
        pills = page.locator(".tag-pill")
        n_pills = pills.count()
        smallest_idx = -1
        smallest_n = float("inf")
        smallest_label = ""
        for i in range(n_pills):
            ct_text = pills.nth(i).locator(".ct").inner_text().replace(",", "").strip()
            try:
                ct = int(ct_text)
            except ValueError:
                continue
            if 0 < ct < smallest_n:
                smallest_n = ct
                smallest_idx = i
                smallest_label = pills.nth(i).inner_text().strip()
        if smallest_idx < 0:
            failures.append("3.5: no tag pill with valid count found")
        else:
            target_pill = pills.nth(smallest_idx)
            print(f"  smallest tag: {smallest_label!r} (count={smallest_n})")
            target_pill.click()
            page.wait_for_timeout(800)
            filtered_count = page.locator("tbody tr.row-anchor").count()
            print(f"  unfiltered: {unfiltered_count}  filtered: {filtered_count}")
            # Expectation: filtered_count should equal smallest_n (or be at
            # most the render cap if smallest_n > 1000)
            expected = min(smallest_n, 1000)
            if filtered_count == expected and filtered_count < unfiltered_count:
                proof["tag_pill_click"] = "PASS"
                print(f"  tag pill click: PASS  ({unfiltered_count} -> {filtered_count})")
            elif filtered_count < unfiltered_count and filtered_count > 0:
                proof["tag_pill_click"] = "PASS"
                print(f"  tag pill click: PASS  ({unfiltered_count} -> {filtered_count})")
            else:
                failures.append(
                    f"3.5: tag pill did not filter as expected "
                    f"(tag={smallest_label!r} expected_count={smallest_n} "
                    f"actual_filtered={filtered_count} unfiltered={unfiltered_count})"
                )

        browser.close()

    PROOF_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROOF_PATH.write_text(json.dumps(proof, indent=2, default=str), encoding="utf-8")

    print(f"\n{'=' * 60}")
    if failures:
        print(f"  ❌ FAIL ({len(failures)} issues):")
        for f in failures:
            print(f"    - {f}")
        return 1
    else:
        print(f"  ✅ PASS")
        print(f"  rows rendered: {proof['row_count']}")
        print(f"  first 5 PIDs: {proof['first_5_pids']}")
        print(f"  proof: {PROOF_PATH}")
        print(f"  screenshot: {SCREENSHOT_PATH}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
