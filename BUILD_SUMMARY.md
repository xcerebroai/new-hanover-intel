# new-hanover-intel build summary — tag taxonomy rebuild

**Live dashboard:** https://xcerebroai.github.io/new-hanover-intel/
**Repo:** https://github.com/xcerebroai/new-hanover-intel
**Pages deployment:** **VERIFIED** (status `built` in 31 s, live URLs return HTTP 200)

---

## Tag taxonomy emitted

Schema is now `{ "header": {...}, "records": [...] }`. Every lead carries `tags[]` (operator-language strings), `tier`, `_diff_status`, and signals grouped by tag.

### Active tags + counts (3,765 total leads)

| Tag | Count | % of leads | Category |
|---|---|---|---|
| Absentee Owner | 2,020 | 53.6% | owner |
| Tax Delinquency | 1,807 | 48.0% | distress |
| Demolition Order | 1,664 | 44.2% | distress |
| Long-Term Ownership | 547 | 14.5% | owner |
| Distressed Transfer | 478 | 12.7% | derived |
| Stormwater Issue | 380 | 10.1% | distress |
| Out-of-State Owner | 358 | 9.5% | owner |
| Tax Foreclosure | 4 | 0.1% | distress |

> **Absentee Owner > 50%** is over the spec's warning threshold but is real-data reality on the distressed-parcel subset. Owner-profile tags are only computed on parcels that already have at least one distress signal — i.e. the universe is "distressed parcels," not all NHC parcels — and a high absentee rate among distressed parcels is expected (absentee correlates with delinquency, demolition, etc.). Matcher inspected; not loosened or tightened. Documented per spec.

### Suppressed tags

| Tag | Reason |
|---|---|
| **Senior Owner** | NHC PropertyOwners has no senior/elderly exemption field — coverage of any candidate field measured 0% across 5,000 sampled rows. Tag globally suppressed per the senior-suppression rule (≤5% coverage). |
| **Free & Clear** | `rod_deed_of_trust.jsonl` is not pulled by any scraper in this repo. The "no recent DoT found" inference would fire on every parcel with a sale_date — that's overfire, not "free & clear." Per spec, do not infer payoff status from absent data. Tag globally suppressed. |

### Zero-count tags (dropped from render)

These are real tags in the taxonomy but no lead currently fires them under the strict join rules:

| Tag | Why zero |
|---|---|
| Foreclosure | StarNews body content filter required foreclosure keywords; all 12 NHC starnews_foreclosures rows in the current pull failed that filter (Gannett's foreclosures-sheriff-sales category cross-posted NTC + CAMA permits this run). ROD foreclosure file absent. |
| Sheriff Sale | Subset of Foreclosure — also zero. |
| Mechanics Lien | NHC ROD `LIEN` records carry doc_type `LIS PENDENS` or `BANKRUPTCY` only — no `ML/MECH` codes in the current sample. Plus no PID/address join available. |
| Judgment | `rod_judgment.jsonl` has 2 rows, both with governmental reverse-party (CITY OF WILMINGTON) — already filtered. No remaining attached signals. |
| Lis Pendens | `rod_lien.jsonl` has 2 LIS PENDENS rows but no PID/address join (description = subdivision+lot+block, not street address). |
| Estate / Probate | StarNews NTC matches via decedent name → owner-name-only join (insufficient per join safety rules). `rod_estate_deed.jsonl` 14 rows but all are TRUSTEES DEED (foreclosure-context, not probate); 0 actual ADMIN/EXEC/EXTRX deeds. Plus no PID/address join. |
| Quitclaim | `rod_quitclaim.jsonl` is not produced by any scraper in this repo. |
| Post-Estate Sale | Requires Estate / Probate present (which is zero). |

> **None of these zeros indicate a pipeline bug.** They are honest outcomes of the strict join safety rules: name-alone matches are dropped; no PID is available on ROD records; no address is in ROD descriptions. The tag audit at `data/raw/tag_audit.json` documents source_signal_count for each.

---

## Tier distribution

| Tier | Count | Definition |
|---|---|---|
| **Hot** | **0** | ≥3 distinct distress tags |
| **Warm** | **90** | 2 distinct distress tags |
| **Active** | **3,675** | 1 distinct distress tag |

### Why Hot = 0

Per CHECK 2, the maximum distress stack achievable across all 3,765 records is **2** (no parcel currently overlaps 3+ distress tags). This is real-data reality, not a pipeline bug, confirmed by the tag audit:

- 4 active distress tags exist (Tax Delinquency, Tax Foreclosure, Demolition Order, Stormwater Issue) — 4 of the 11 possible distress tags.
- The 4 Tax Foreclosure parcels (the rarest distress tag) all overlap with Tax Delinquency = 2 distress tags = Warm.
- Demolition Order + Tax Delinquency overlap: 57 parcels = Warm.
- Demolition Order + Stormwater Issue overlap: 22 parcels = Warm.
- Stormwater + Tax Delinquency overlap: 7 parcels = Warm.
- No parcel has 3 of these simultaneously in the current data.

To produce Hot leads, either (a) source data needs to expand (e.g. unlock Wilmington EnerGov code cases, or `rod_foreclosure.jsonl` fetches succeed), or (b) the genuine triple-stacks just don't exist in NHC right now.

The tag audit at `data/raw/tag_audit.json` shows source_signal_count and source_files for every tag — honest provenance for Hot=0.

### Top distress combos

| Combo | Count |
|---|---|
| (Tax Delinquency only) | 1,739 |
| (Demolition Order only) | 1,585 |
| (Stormwater Issue only) | 351 |
| (Demolition Order, Tax Delinquency) | 57 |
| (Demolition Order, Stormwater Issue) | 22 |
| (Stormwater Issue, Tax Delinquency) | 7 |
| (Tax Delinquency, Tax Foreclosure) | 4 |

---

## Diff status — baseline reset

This run was a baseline reset (the previous `leads.previous.json` was the pre-tag-taxonomy schema):

- **new_count:** 0
- **newly_tagged_count:** 0
- **existing_count:** 3,765
- All records carry `_diff_status = "existing"`.

Old `leads.previous.json` archived to `data/raw/archive/leads.previous.<timestamp>.json`. Today's `leads.json` was snapshotted to `leads.previous.json` at the end of the run, so **tomorrow's pipeline run will compute a real diff** against today's data.

The dashboard's Today view shows: *"Diff baseline established. New leads appear here starting tomorrow."*

---

## Verification loop — 1 iteration, all checks PASS

Loop ran in a single iteration. **No fixes required during the loop** — every check passed on first run.

| Check | Status | Proof |
|---|---|---|
| 1. Two-Truths | ✓ PASS | tier + tag counts match exactly. records=3,765, total_tag_attachments=7,258. |
| 2. Tier sanity | ✓ PASS | tier_counts sum 3,765 = total. No zero-distress in records[]. Hot=0 confirmed by audit (max stack = 2 < 3). |
| 3. Tag distribution | ✓ PASS | All 8 tags ≥ 1 lead. Absentee Owner > 50% accepted (documented; matcher inspected, not loosened). |
| 4. Hot sample | ✓ PASS | No Hot leads to sample (Hot=0, accepted by CHECK 2). Note written to `data/raw/verify_hot_sample.txt`. |
| 5. Warm sample | ✓ PASS | 10/10 PASS. Every signal had a valid join method (`exact_pid`, `addr_exact`, `self`, or `derived`). Distress count = 2 on every Warm sample. Artifact: `data/raw/verify_warm_sample.txt`. |
| 6. Diff logic | ✓ PASS | new + newly_tagged + existing = 3,765 = total. Cross-checked against `leads.previous.json` (new schema). All records have valid `_diff_status`. |
| 7. Dashboard E2E | ✓ PASS | All 11 sub-checks: HTTP 200 on /, data/leads.json, methodology.html. Today default. 8 tag pills (cap 15 — total tags = 8). 4 tier pills. All Leads renders 1,000 rows (render cap). URL state updates to `?view=all`. 2,070 tag chips in table. Expand panel renders. Export button present. Methodology link present. Today empty-state shows "Diff baseline established." Screenshot saved to `docs/dashboard.png`. |
| 8. Final Two-Truths re-check | ✓ PASS | Counts unchanged. records=3,765, total_tag_attachments=7,258. |

---

## Git commits (this run)

| Hash | Message |
|---|---|
| `72094f8` | refactor(pipeline): tag taxonomy replaces 6-pattern stack + diff + verify |
| `a8b0dc5` | feat(dashboard): PropStream-style UX with Today and All Leads modes |
| `4b50d67` | chore(data): regenerate leads.json after taxonomy rebuild |

All 3 commits pushed to `origin/main`.

---

## Files NOT committed (per spec)

| Path | Reason |
|---|---|
| `data/raw/*.jsonl` | gitignored — raw scrapes never committed |
| `data/raw/tag_audit.json` | gitignored — audit artifact, regenerated each run |
| `data/raw/verify_hot_sample.txt` | gitignored — verification artifact |
| `data/raw/verify_warm_sample.txt` | gitignored — verification artifact |
| `data/raw/archive/leads.previous.<ts>.json` | gitignored — schema-migration archive |
| `docs/dashboard.png` | repo doesn't track screenshots — file exists locally at `docs/dashboard.png` from CHECK 7 verification but is intentionally uncommitted |

---

## Features omitted (repo lacked safe data or URL pattern)

- **`Foreclosure` tag from ROD** — `rod_foreclosure.jsonl` is not produced by any scraper in this repo (the BIS PHP scraper attempted it during the prior build but the NameDisplay endpoint times out on large entityID batches under load). Tag still fires from StarNews when body content matches foreclosure keywords; in this run, 0 starnews rows passed the body filter.
- **`Quitclaim` tag** — `rod_quitclaim.jsonl` is not produced.
- **`Estate / Probate` from ROD ADMIN/EXEC/EXTRX deeds** — the existing `rod_estate_deed.jsonl` was scraped with a query that included TRUSTEES DEED + SHERIFF DEED, contaminating the file with foreclosure-context deeds. Pipeline filters to actual probate codes, but 0 of the 14 rows match — the entire file is currently TRUSTEES DEED. Tag would still need a PID/address join even if real probate codes were present.
- **eTax URL pattern** — uses the existing `https://etax.nhcgov.com/pt/Datalets/Datalet.aspx?UseSearch=no&pin={PARID}&jur=NH&taxyr=2025` pattern from prior build. PID is rendered as a clickable link in the table.
- **County GIS link** — no public GIS parcel viewer URL pattern is established in this repo (NHC's `polaris3g`-style viewer doesn't exist for NHC). Omitted from row expand.
- **Free & Clear tag** — suppressed (no `rod_deed_of_trust.jsonl`).
- **Senior Owner tag** — suppressed (no exemption field in PropertyOwners).
- **Hot leads** — none exist in current data (max stack = 2). Real-data reality, not loosened to force.

---

## Anything left ambiguous

Nothing material. The build executed end-to-end with all 8 verification checks passing on a single iteration. The Hot=0 outcome is the only result that would benefit from human eyeballing — but it's confirmed by the audit and is consistent with the data sources available (only 4 active distress tags can fire; no current parcel intersects 3 of them).

The Absentee Owner > 50% rate on the distress-parcel subset is also documented but accepted as plausible: distressed parcels are disproportionately absentee, and the universe being filtered is exactly that subset.

If a human reviewer wants to push for Hot leads, the path is to unlock additional distress tags by:
1. Pulling `rod_foreclosure.jsonl` reliably (BIS PHP load issue) — would surface 11 distress sources instead of 4.
2. Building a Wilmington EnerGov SelfService scraper (CSRF/SPA) for city-issued code cases.
3. Or finding a feed for code-enforcement cases (currently no public dataset for NHC or Wilmington).

None of those are in scope for this run.

---

Two-Truths: PASS
Verification Loop: PASS
Dashboard: PASS
Deployment: PASS

---

## Live verification fix log

- **Diagnosis category:** **G — UNKNOWN** (live URL works correctly; reported symptom not reproducible)
- **Root cause (verbatim):** User reports "Loading leads..." indefinitely on the live URL, but Phase 1 diagnosis cannot reproduce the failure. Findings:

  | Check | Result |
  |---|---|
  | 1.1 deployed branch contents | `index.html`, `methodology.html`, `data/leads.json` all present on `origin/main` |
  | 1.2 live HTML | HTTP 200, 38,236 B, `Last-Modified: Tue, 05 May 2026 15:08:51 GMT`, `Cache-Control: max-age=600`, `X-Cache: MISS` |
  | 1.3 live JSON | HTTP 200, 6,558,536 B, valid `application/json; charset=utf-8` |
  | 1.4 Pages build | latest commit `aae845ad`, status `built`, duration 62 s, no error |
  | 1.5 deploy commit vs local | match exactly (`aae845ad8c27a003fd7a0582f18e3e50db3eb2e2`) |
  | 1.6 Playwright `?view=all` | tbody populated, **1,000 rows**, 0 console errors, 0 page errors, 0 failed requests, leads.json fetched in 3,523 ms |
  | 1.6 Playwright bare URL | tbody not populated with rows, but renders spec-compliant empty-state: *"Diff baseline established. New leads appear here starting tomorrow."* Same 0/0/0 error counts. |

  The bare URL default Today view is empty because the rebuild was a baseline reset — `new_count=0, newly_tagged_count=0, existing_count=3,765`. The empty-state message renders exactly as specified. The "Loading leads..." placeholder text in the initial HTML is replaced by JS within ~3 seconds of page load, so users would only see it during that brief window.

  None of the categorized failure modes (A–F) match the evidence:

  - A. JSON not deployed — false (HTTP 200, 6.55 MB)
  - B. Stale deploy — false (commit matches)
  - C. JS error — false (0 errors in real Chromium)
  - D. CORS / fetch path — false (fetch succeeds)
  - E. Slow JSON fetch — false (3.5 s, well under 30 s timeout)
  - F. Cache lag — false (`X-Cache: MISS`, fresh 200)

- **Fix applied:** None. Category G triggered the hard-rule STOP: *"If category is G, STOP. Do not invent a fix."* The dashboard is functioning per spec. The user's perception of failure is the spec-compliant baseline empty-state on the default Today view.

- **Final verification (Phase 3 ran as proof):**
  - **Live URL:** https://xcerebroai.github.io/new-hanover-intel/?view=all
  - **Row count rendered:** 1,000 (render cap; full tier=warm+active count is 3,765)
  - **First 5 addresses (cross-checked against PropertyOwners):**
    1. `R04200-001-025-000` — 1640 AIRPORT BLV — NEW HANOVER COUNTY
    2. `R02600-004-005-000` — 4500 BLUE CLAY RD — CAPE FEAR COMMUNITY COLLEGE
    3. `R03700-002-002-002` — 1200 PORTERS NECK RD — PLANTATION VILLAGE INC
    4. `R01700-001-001-000` — 3901 CASTLE HAYNE RD — NUCLEAR FUEL HOLDING CO INC
    5. `R07000-002-005-000` — 4126 RIVER RD — PROXIMITY WATERMARK LLC
  - **Mode toggle:** **PASS** — Today renders empty-state with `view=today` URL; All Leads renders 1000 rows with `view=all` URL; toggle clicks update both URL and table.
  - **Tag pill click:** **PASS** — clicking the "Tax Foreclosure" pill (count=4) reduces visible rows from 1000 → 4 as expected. The initial test bug (clicked "Absentee Owner" with count=2,020 — render cap masked the filter effect) was fixed by selecting the smallest-count pill.
  - **Console errors:** 0
  - **Page errors:** 0

- **UX observation (not a fix, just a note):** the bare-URL default Today view shows nothing useful on a baseline run. The spec literal calls Today the default mode, and the empty-state message is the prescribed behavior, but users landing on the bare URL during a baseline run see the spec-correct "Diff baseline established" message which can be misread as "still loading." This is the most plausible interpretation of the user's report. A future spec revision could either (a) auto-default to All Leads when `new+newly_tagged==0`, or (b) make the empty-state message more prominently link to All Leads. Neither change is in scope for this run.

- **Commits:**
  - `cdcaad7` chore(verify): live-URL diagnosis + verification scripts
  - `7a3ae9f` verify(live): post-deploy live URL verification proof
  - (this commit) docs: append live verification fix log to BUILD_SUMMARY.md
