# new-hanover-intel build summary

**Live dashboard:** https://xcerebroai.github.io/new-hanover-intel/
**Repo:** https://github.com/xcerebroai/new-hanover-intel
**Local path:** `C:\Dev\xcerebro-builds\projects\new-hanover-intel\`
**Refresh harness:** `pipeline/refresh.py --push`

---

## Sources

| Status | Count |
|---|---|
| Recon'd | 13+ (every URL in user prompt + 8 follow-up endpoints discovered via search) |
| Live scraping | **8** sources |
| Documented blockers / gaps | **5** sources, each documented in `RECON.md` and `methodology.html` |

### Live scrapers

| File | Source | Records pulled this run |
|---|---|---|
| `scrapers/property_owners.py` | NHC ArcGIS PropertyOwners (parcel master) | 115,344 parcels |
| `scrapers/energov_permits.py` | NHC EnerGov building permits — 3 distress filters | 2,247 demolitions, 609 floodplain dev, 706 occupancy certs |
| `scrapers/delinquent_tax.py` | NHC delinquent tax CSV | 5,119 rows |
| `scrapers/nhc_foreclosures.py` | NHC GS 105-374 foreclosure schedule | 4 active parcels (2 cases) |
| `scrapers/nhc_stormwater.py` | NHC ArcGIS stormwater permits | 1,116 records |
| `scrapers/starnews_legals.py` | Wilmington StarNews legals (Gannett iPublish) | 9 NTC + 23 fcl listings (sampled) |
| `scrapers/rod.py` | NHC Register of Deeds (BIS PHP), per-doctype | judgment+estate_deed+lien from last 30 days |
| (transfer rule) | PropertyOwners-derived nominal-sale + post-estate | 575 fires |

### Documented gaps (NOT scraped)

- **eCourts (Tyler Odyssey)** — AWS WAF CAPTCHA every 5 minutes. Pivoted to StarNews newspaper-notice flow (NC statute requires statutory publication, so SP foreclosure + E estate signals surface there).
- **Wilmington EnerGov SelfService** (city building permits + city code cases) — Tyler SPA + CSRF + session cookie. Mid-effort Playwright scrape, deferred.
- **Beach towns** (Wrightsville, Carolina, Kure) — no public open-data portals. Town-issued permits are not in any open feed.
- **Munis Self Service** — per-parcel only behind Tyler Portico OAuth. Skipped — county delinquent CSV is strictly superior.
- **Public code-enforcement dataset** — does not exist for NH or City of Wilmington. The `code` pattern fires only via EnerGov demolition permits in this build, and that limitation is honestly disclosed in methodology.html.

---

## Doc type taxonomy discovered

The Register of Deeds at `search.newhanoverdeeds.com` exposes **369 instrument codes**. RECON.md captures the 30-code high-signal subset; the dashboard `doc_type_counts` shows live counts for every doc type currently attached to a lead:

| Code | Source | Pattern fired |
|---|---|---|
| `DELQ` | NHC delinquent tax CSV | tax |
| `DEMO` | EnerGov demolition permit | code |
| `OCC_CERT` | EnerGov occupancy certification | sub-flag |
| `FLOODPLAIN_DEV` | EnerGov floodplain dev permit | sub-flag |
| `STORMWATER` | NHC stormwater permits | sub-flag |
| `FCL_NOTICE` | StarNews foreclosure listing | jfc |
| `TAX_FC` | NHC GS 105-374 schedule | jfc + tax |
| `NTC` | StarNews Notice to Creditors | estate |
| `ADMIN DEED`, `EXEC DEED`, `EXTRX DEED`, `COMMR DEED` | ROD | estate |
| `JUDGMENT` | ROD | lien |
| `LIEN` (incl. tax/mechanics — type in description) | ROD | lien |
| `LIS PENS` | ROD | lien |
| `BKTCY` | ROD | lien |
| `QCD`, `DEED OF GIFT`, `SEP AGMT`, `MEMO SEPR AGMT` | ROD | transfer |
| `NOMINAL_SALE`, `POST_ESTATE_SALE` | PropertyOwners-derived | transfer |
| `SAT`, `PSAT`, `D/R`, `P/REL D/T` | ROD | sub-flag (no pattern) |

---

## Records ingested

| Source | Total raw rows |
|---|---|
| property_owners | 115,344 |
| delinquent_tax | 5,119 |
| energov_permits_demolition | 2,247 |
| energov_permits_floodplain | 609 |
| energov_permits_occupancy | 706 |
| nhc_stormwater | 1,116 |
| nhc_foreclosures | 4 |
| starnews_notice_to_creditors | 8 (NHC-filtered) |
| starnews_foreclosures | 1 (NHC-filtered) |
| rod_judgment | 4 |
| rod_estate_deed | 4 |
| rod_lien | 2 |
| **Raw total** | **125,164** |

After joins + 6-pattern stack:

| Tier | Count | Definition |
|---|---|---|
| **Hot** | **10** | stack_count ≥ 3 |
| **Warm** | **486** | stack_count == 2 |
| **Active** | **3,067** | stack_count == 1 |
| **Total leads** | **3,563** | |

`leads.json` size: **5.79 MB** (well under GitHub's 50 MB hard cap).

### Pattern attach rates

| Pattern | Leads firing |
|---|---|
| jfc | 16 |
| tax | 1,807 |
| estate | 6 (sparse — newspaper notices + ROD estate deeds, low daily volume) |
| code | 1,664 (via EnerGov demolition permits — gap acknowledged) |
| lien | 1 (sparse — ROD pulls for last 30 days only) |
| transfer | 575 (PropertyOwners-derived nominal sale + post-estate sale rules dominate) |

### Top pattern combinations (warm + hot tiers)

| Combo | Count |
|---|---|
| code + transfer | 224 |
| tax + transfer | 205 |
| code + tax | 50 |
| code + tax + transfer | 7 (Hot) |
| jfc + tax + transfer | 3 (Hot) |

### Quality metrics

- **Two-Truths check:** **PASS** (header tier_counts + pattern_counts equal records-derived counts, recomputed before write).
- **Warm tier high-confidence pct:** **77.8%** — 378 of 486 warm leads carry at least one of (demolition_permit, absentee_owner, multi_juris_delinquent, imminent_tax_sale).
- **Tax delinquency PARID match rate:** 5,119 raw → 5,063 matched (98.9%); 56 unmatched parcels likely retired/merged, 2,079 filtered for `total_due < $500` floor.
- **EnerGov demolition PARID match rate:** 2,247 → 2,091 (93.1%).
- **Source commit in header:** yes (`source_commit` field).

---

## Refresh harness

- **Command:** `py -3.12 pipeline\refresh.py --push`
- **Schedule:** Windows Task Scheduler at **04:00 America/Chicago daily**, config in `scripts\daily_refresh.xml`. Register with `schtasks /create /xml scripts\daily_refresh.xml /tn "new-hanover-intel-refresh" /f`.
- **Heartbeat:** `HEARTBEAT.json` carries `last_success_timestamp` + per-source last_success.
- **Exit codes:** 0 full success, 1 partial (some sources failed; pipeline ran), 2 pipeline / Two-Truths failure (do NOT push).
- **Per-source incremental:** ROD scrapers default to last 14 days via `--since`; ArcGIS sources do nightly snapshot pulls; CSV/HTML sources use ETag/page-hash short-circuit when unchanged.

### CLI examples

```powershell
# Full daily cycle, with push
py -3.12 pipeline\refresh.py --push

# Dry run (no pipeline write, no git push)
py -3.12 pipeline\refresh.py --dry-run

# Run only one source
py -3.12 pipeline\refresh.py --source delinquent_tax

# Skip a flaky source for one cycle
py -3.12 pipeline\refresh.py --skip rod_foreclosure --push

# Force re-pull (clear state)
py -3.12 scrapers\delinquent_tax.py --reset

# Pipeline only
py -3.12 pipeline\build_leads.py
```

---

## OpenClaw integration handoff

The exact PowerShell command OpenClaw runs on cron:

```powershell
$env:PYTHON_BIN = "C:\Users\Owner\AppData\Local\Programs\Python\Python312\python.exe"
& $env:PYTHON_BIN "C:\Dev\xcerebro-builds\projects\new-hanover-intel\pipeline\refresh.py" --push
```

OpenClaw should treat the **process exit code** as the primary signal, then read `HEARTBEAT.json` to determine which sources succeeded vs failed.

### Alerting rules (per `OPERATIONS.md`)

1. **Heartbeat staleness** — if `last_success_timestamp` is older than 36 hours, alert via Telegram bot `@Xcerebrobot` (chat ID `6004053137`).
2. **Per-source 48h failure** — when any `per_source[*].ok == false` and `last_success_at` > 48h ago, alert. First failure silent (transient errors are common); two consecutive failed runs = page.
3. **New Hot/Warm leads** — diff `data/leads.json` vs `data/leads.previous.json` on `pid`. Any new Hot lead, or Warm lead with `imminent_tax_sale` / `demolition_permit` / `multi_juris_delinquent` / `delinquency_over_25k`, sends Telegram with PID + address + patterns + dashboard deep-link.

---

## Open items requiring human judgment

1. **ROD foreclosure pull is fragile** — the BIS PHP NameDisplay endpoint times out on large entityID batches (observed at 4-month windows). The scraper has a 3-retry exponential backoff and ENTITY_BATCH_SIZE was reduced from 250 → 100. **Recommend manual rate-limit experimentation under load** to find the safe daily window. Today's --since=14 days default works on every doctype except `foreclosure` which has multiple high-volume codes. As a workaround, scope `foreclosure` daily to last 7 days only.
2. **City of Wilmington EnerGov SelfService** — Tyler SPA scraper would unlock city-issued building permits + city code cases (the entire half of the moat we don't have). Mid-effort Playwright build with CSRF/session handling; recommend Phase 2 commitment.
3. **etax CAMA card scrape** for year_built / structure age / exemption flags — would enrich the `is_likely_inherited` heuristic and surface senior-exemption owners. Per-parcel iasWorld scrape is doable (disclaimer-POST + viewstate, no CAPTCHA) but is a separate ~2-day build.
4. **Beach towns + non-Wilmington municipal code data** — no public feeds. Practical-only fix is per-municipality OPRA / public-records request; document `methodology.html` continues to disclose this gap.
5. **GS 105-375 In Rem foreclosures** — RECON noted NHC may run only GS 105-374 (judicial), but didn't confirm with the Tax Department directly. If the county also runs In Rem (faster, ~1 yr from delinquency to sale), that's a high-signal data point we should capture. Recommend a one-time call to confirm.

---

## Next-county-port notes

- **NC OneMap statewide parcel feed** at `services.nconemap.gov/secure/rest/services/NC1Map_Parcels/MapServer/1` carries 5.9M parcels across all 100 NC counties on a standardized ICDX schema. **The NHC parcel master scraper retargeted to a different `cntyfips` value yields any other NC county's parcel master** without per-county schema work. Mecklenburg already uses POLARIS, but for any other county (Wake, Durham, Buncombe, Brunswick, Pender, Onslow, etc.) NC OneMap is the porting baseline.
- **BIS PHP** (the ROD vendor for NH) also serves Forsyth and a handful of other NC counties. The same `rod.py` flow — disclaimer-cookie → NamePick → NameDisplay — is portable; only the doc-type taxonomy needs re-checking per county since each ROD office customizes its instrument codes.
- **CivicEngage delinquent CSV pattern** (NHC publishes monthly): plenty of NC counties use CivicEngage, but the data they publish via `/DocumentCenter/View/...` varies. Worth a probe per county.
- **Gannett iPublish** (StarNews) hosts legal notices for many NC newspapers under different `marketplace/<market>` paths. The same scraper retargeted to a different market code (e.g., `gso` for Greensboro News & Record) covers Guilford / Forsyth / Alamance counties' statutory notices.
- **eCourts is the same blocker statewide** — every NC county is now on Tyler Odyssey with the same AWS WAF CAPTCHA. The newspaper-notice pivot generalizes across all 100 NC counties.

---

## Build phases (shipping order)

| Phase | Status |
|---|---|
| 1. Repo scaffold + recon (RECON.md) | ✅ committed `9d6af80` |
| 2. Scrapers (8 sources) | ✅ committed `e19680e`, `852ed30`, `0c51b56`, `f18d4fe` |
| 3. Pipeline (build_leads.py + Two-Truths) | ✅ committed `f18d4fe` |
| 4. Refresh harness + Task Scheduler XML + OPERATIONS.md | ✅ committed `f18d4fe` |
| 5. Dashboard (index.html + methodology.html) | ✅ committed `f18d4fe` |
| 6. GitHub Pages deploy | ✅ live at https://xcerebroai.github.io/new-hanover-intel/ |
| 7. BUILD_SUMMARY.md | ✅ this file |

---

⚡ — Built by Jarvis (Just Jarvis LLC) for Quentin Flores. Operator-first.
