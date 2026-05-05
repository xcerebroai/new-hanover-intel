# RECON — New Hanover County, NC Motivated Seller Intelligence

> Phase 1 reconnaissance. No code yet — this is the source map and the proposed pattern stack the build will follow.

New Hanover County (Wilmington, NC) sits inside the same NC legal regime as Mecklenburg, but the operational data infrastructure is materially different. The most important findings vs the Mecklenburg precedent:

- **The county delinquent-tax list is a directly-downloadable CSV** — not a published PDF, not a per-parcel lookup, not a paywalled vendor. 5,120 parcels with `Parcel`, `Total Due`, `Last Payment Date`, `Juris Code`. Monthly refresh. **Single most valuable signal source for this county.**
- **The county building-permits feed includes 475K records with 99.2% PID coverage**, including 2,247 demolition permits. This partially fills the gap left by the absence of a public code-enforcement dataset.
- **The Register of Deeds is a 2012-era PHP application from BIS**, not Aumentum/Manatron/Tyler/iDocMarket. **No login wall. No CAPTCHA. No anti-bot.** This is a 180° flip from Mecklenburg, where ROD was the hardest source to crack. Daily date-range search of all distress doc types is straightforward.
- **The ROD records carry no parcel ID.** Join key has to be (subdivision + lot/block) or (grantor name) or (address match). Lossier than POLARIS' direct PID join in the Mecklenburg pattern.
- **eCourts is gated by AWS WAF CAPTCHA** (5-minute re-challenge interval) — same blocker family as Mecklenburg's Tyler portal. Pivot to newspaper notice flow (Gannett-hosted Wilmington StarNews legals) — same architecture as the Mecklenburg → mecktimes.com workaround.
- **No public code-enforcement dataset exists** for either NH County or City of Wilmington. Demolition signal comes from building permits (`PERMIT_TYPE LIKE '%Demolition%'`). The Charlotte HNS pattern from Mecklenburg does not transfer.
- **Kania and RBCWB do not service NH tax foreclosures.** The Mecklenburg dual-firm pattern collapses — NHC handles GS 105-374 cases in-house through the Tax Department + Clerk of Superior Court. The county's own `/345/Foreclosures` HTML page is canonical.
- **City of Wilmington runs its own EnerGov instance** (`wilmingtonnc-energovweb.tylerhost.net`) which is gated behind a Tyler SPA + CSRF token — mid-effort Playwright scrape. **City-issued building permits + city code cases are NOT in the county's open feed.** Coverage gap documented.

---

## SOURCE INVENTORY

### 1. New Hanover County GIS — ArcGIS Server

| Field | Value |
|---|---|
| REST root | https://gis.nhcgov.com/server/rest/services/ |
| Open data hub | https://opendata.nhcgov.com/ |
| DCAT feed | https://opendata.nhcgov.com/api/feed/dcat-us/1.1.json |
| Server version | ArcGIS Server 11.3 |
| Auth | None on public folders (Layers, Thematic, Hosted) |
| Anti-bot | None — clean JSON with browser User-Agent header |
| Update frequency | Underlying data live; service refresh varies per layer (permits ~daily, parcels ~daily) |
| Notes | The `Dept` folder requires a token (internal staff). Public folders cover all the layers we need. |

**Authoritative parcel/owner table — `Layers/PropertyOwners/FeatureServer/0`**

This is the equivalent of Mecklenburg POLARIS — every parcel master record needed for downstream joins. ~115,345 owner records (one per owner card; exceeds the parcel count due to multi-owner cards).

Key fields: `PARID` (the join key, format `R#####-###-###-###`), `OWN1`, `ADRNO`/`ADRSTR`/`ADRSUF`/`CITYNAME` (site address), `OWNER_NUM`/`OWNER_STREET`/`OWNER_CITY`/`OWNER_STATE`/`OWNER_ZIP` (mail address), `APRLAND`/`APRBLDG`/`APRTOT` (assessed values), `SALE_DATE`/`SALE_PRICE`/`SALE_INSTRUMENT`/`SALE_BOOK`/`SALE_PAGE`, `MUNI` (`WM`/`CB`/`KB`/`WB`/null=unincorporated), `LUC`/`CLASS`, `XCOORD`/`YCOORD`, `LEGAL1`, `SUBDIV`, `ACRES`.

**PID format confirmed:** `R#####-###-###-###` (18 chars, including dashes; e.g. `R04813-022-013-000`). Already zero-padded by the source. Use `PARID` verbatim — no normalization required.

**Year built is NOT on this layer.** It would have to come from etax.nhcgov.com per-parcel scrape (out of scope for v1; documented as enrichment gap).

**Force multiplier for cross-NC porting**: NC OneMap (`services.nconemap.gov/secure/rest/services/NC1Map_Parcels/MapServer/1`) carries 5.9M parcels across all 100 NC counties on a standardized `parno`/`ownname`/`structyear`/`saledate`/`saledatetx` schema. NH's record count there (103,391) matches the local count to within 0.02%. **This is the porting baseline** — the same scraper retargeted to a different `cntyfips` value yields any other NC county's parcel master without per-county schema work.

### 2. New Hanover County Building Permits (EnerGov)

| Field | Value |
|---|---|
| REST | `https://gis.nhcgov.com/server/rest/services/Thematic/EnergovPermitsPlans/FeatureServer/0` (mirror at `Thematic/BuildingPermits/FeatureServer/0`) |
| Records | 475,786 |
| PID coverage | 99.2% (471,943 / 475,786 non-null) |
| Date range | 1975-12-30 → live |
| Update | Nightly ETL (per service description on Wilmington's parallel feed) |
| Auth | None |
| Demolition records | 2,247 (`PERMIT_TYPE` IN `'NHC Residential Demolition'`, `'NHC Commercial Demolition'`) |
| Floodplain dev | 609 |
| Occupancy certs | 706 |
| Coverage limit | County-issued permits only. **City-of-Wilmington-issued permits are NOT in this feed** — they live in Wilmington's own EnerGov which requires browser-session scrape. |

Schema highlights: `PERMIT_NUMBER`, `PERMIT_TYPE`, `WORK_CLASS`, `PERMIT_STATUS`, `APPLICATION_DATE`, `ISSUE_DATE`, `FINALED_DATE`, `EXPIRATION_DATE`, `DESCRIPTION`, `SQUARE_FEET`, `VALUATION`, `GENERAL_CONTRACTOR`, `PROJECT_CONTACT`, full address fields, `PID`, `mapidkey`, `Lat`/`Lon`.

**Distress filter:** `PERMIT_TYPE LIKE '%Demolition%' OR DESCRIPTION LIKE '%demo%'` for the `code` pattern (NH-adapted — no separate code-enforcement dataset exists).

### 3. NHC Delinquent Tax List (TIER-1 SIGNAL)

| Field | Value |
|---|---|
| Page | https://www.nhcgov.com/2877/Delinquent-Taxes |
| CSV download | https://www.nhcgov.com/DocumentCenter/View/11283/Delinquent_Taxpayers_Report_CSV |
| Excel download | https://www.nhcgov.com/DocumentCenter/View/11284/Delinqnet_Tax_Report_Excel |
| Records | 5,120 |
| Update | First business day of each month |
| Auth | None (use GET, NOT HEAD — CivicEngage returns 404 on HEAD) |
| Anti-bot | None |
| Schema | `Customer Account, Name 1, Juris Code, Juris Description, Location No., Location No. Suffix, Location Street, Location Apt., Property Code, Parcel, Total Due, Last Payment Date` |
| Range | 10 years of delinquencies, both current and back-tax |
| Notes | Multiple jurisdictions per parcel (`CB`/`WM`/`WB`/`FD`) — same parcel may appear twice (county + municipal). Dedupe by `Parcel`. Filter on `Total Due` ≥ $500 to remove administrative pennies. |

**The single highest-volume distress feed in this build.** Direct PID join via the `Parcel` column.

### 4. NHC Tax Foreclosure Schedule

| Field | Value |
|---|---|
| URL | https://www.nhcgov.com/345/Foreclosures |
| Format | Plain HTML (CMS-edited) |
| Update | Manual; refreshed when new sales scheduled |
| Active records (recon date) | 4 parcels across 2 case groups |
| Statute | GS 105-374 (judicial action). NHC may not run GS 105-375 In Rem at all. |
| Schema | parcel ID, street address, civil case number (e.g. `25CV004024-640`), sale date, sale time, courthouse location |
| Procedure | 5%-or-$750 deposit, 10-day upset bid, Commissioner's Deed |
| Auth | None |

**Low volume, very high signal.** A parcel on this list is days from losing ownership. Hot-tier promoter.

### 5. Register of Deeds — `search.newhanoverdeeds.com`

| Field | Value |
|---|---|
| Main | https://search.newhanoverdeeds.com/NameSearch.php?Accept=Accept |
| Backend | Custom PHP from Business Information Services (BIS) — vintage 2012-era jQuery, Apache + PHPSESSID |
| Auth | **None** — public search, public results, public image download |
| Anti-bot | **None observed.** 10 sequential requests at default rate returned 200, no 429, no challenge. |
| robots.txt | `Disallow: /` (advisory only per user instruction) |
| Search-result cap | 2,000 hits per query |
| Index lag | ~1 business day; banner reads "Instruments Verified Through: <date> <time>" |
| **PID exposure** | **None.** ROD records expose Subdivision/Lot/Block/Section/Phase/Map but NOT a parcel ID. Join must be address-, owner-name-, or subdivision+lot-based. **Lossier than the Mecklenburg PID-join pattern.** |
| Daily Notebook | `nontemp.php` returns last-24h filings before they hit the verified index |
| Bulk image archive | `bulkimage.php?file=inst_<MMDDYYYY>.zip` — daily ZIP of document images |

**Doc type taxonomy — high-signal subset (~30 of 369 codes):**

| Code | Full Name | Maps to pattern |
|---|---|---|
| `DEED` | DEED | transfer (baseline) |
| `D/T` | DEED OF TRUST | (lien instrument; informational, doesn't fire alone) |
| `QCD` | QUITCLAIM DEED | transfer |
| `ADMIN DEED` | ADMINISTRATORS DEED | estate (probate-driven sale) |
| `EXEC DEED` | EXECUTOR DEED | estate |
| `EXTRX DEED` | EXECUTRIX DEED | estate |
| `COMMR DEED` | COMMISSIONERS DEED | jfc (court-ordered sale) |
| `SHERIF DEED` | SHERIFF DEED | jfc/tax (sheriff sale deed) |
| `FCL DEED` | FORECLOSURE DEED | jfc (post-sale) |
| `FCL` | FORECLOSURE | jfc |
| `N/F` | NOTICE OF FORECLOSURE | jfc |
| `SUB TR` | SUBSTITUTION OF TRUSTEE | jfc (precursor) |
| `SUB TR DEED` | SUBSTITUTE TRUSTEES DEED | jfc (post-sale) |
| `RES SUB/TR` | RESIGNATION OF SUBSTITUTE TRUSTEE | jfc |
| `D/R` | DEED OF RELEASE | satisfaction (sub-flag) |
| `SAT` | SATISFACTION | satisfaction |
| `PSAT` | PARTIAL SATISFACTION | satisfaction |
| `JDGMT` | JUDGMENT | lien |
| `LIEN` | LIEN | lien (incl. tax/mechanics — type in description) |
| `LIS PENS` | LIS PENDENS | lien |
| `BKTCY` | BANKRUPTCY | lien |
| `ASGMT` | ASSIGNMENT | (informational; servicer transfer) |
| `DEED OF GIFT` | DEED OF GIFT | transfer (no consideration) |
| `SEP AGMT` | SEPARATION AGREEMENT | transfer (divorce co-signal with QCD) |
| `MEMO SEPR AGMT` | MEMORANDUM OF SEPARATION AGMT | transfer (divorce) |

**Notable absences vs other counties:** No distinct `STATE TAX LIEN`, `FEDERAL TAX LIEN`, `MECHANICS LIEN`, or `HOA LIEN` codes. All filed under generic `LIEN` (`LIEN`) or `JDGMT`; lien type is in the description free-text. **Plan: regex the description string** during pipeline ingest to assign sub-types.

**Daily refresh pattern:** POST to `NamePick.php` with `start_date=YYYY-1d`, `end_date=YYYY`, no name filters, ~30 high-signal `instType[InstCodes][...]` checkboxes. POST entityID list to `NameDisplay.php`. Parse rows. Daily volume well under the 2,000-record cap. Hit `nontemp.php` more frequently for fresher (still-unindexed) filings.

### 6. Newspaper Legal Notices (Gannett iPublish — Wilmington StarNews)

| Field | Value |
|---|---|
| Foreclosures | https://classifieds.gannettclassifieds.com/marketplace/wlm/category/legals/foreclosures-sheriff-sales |
| Notice to Creditors | https://classifieds.gannettclassifieds.com/marketplace/wlm/category/legals/notice-to-creditors |
| Public notices | https://classifieds.gannettclassifieds.com/marketplace/wlm/category/legals/public-notices |
| Govt public notices | https://classifieds.gannettclassifieds.com/marketplace/wlm/category/legals/govt-public-notices |
| Server | Apache (Gannett-hosted) |
| Anti-bot | **None** — clean 200 with browser UA, JSESSIONID cookie, no WAF challenge |
| Pagination | GET-based, page-number style; 25 or 50 per page |
| Statewide cross-check | https://www.ncnotices.com/ — ASP.NET WebForms with `__doPostBack` + viewstate (mid-effort), 12-month rolling retention |

Notice to Creditors record schema: title, post date (`MM/DD HH:MM AM`), reference code (`#LWLM…`), decedent full name, county, executor/administrator, **estate file number** (e.g. `26E000439-640` — embeds the `E` case-type prefix), creditor claim deadline.

Foreclosure record schema (when populated): trustee/substitute trustee, SP case number, property legal description + street address, sale date/time, courthouse location, opening bid.

This is the exact same pivot Mecklenburg made when eCourts was CAPTCHA-blocked: NC statute requires statutory publication of foreclosure sales and estate openings, so the data has to surface in print, and Gannett mirrors it online.

### 7. eCourts (Tyler Odyssey) — BLOCKED

| Field | Value |
|---|---|
| Portal | https://portal-nc.tylertech.cloud/ |
| Anti-bot | **AWS WAF CAPTCHA** — `x-amzn-waf-action: captcha`, re-challenge every 5 minutes for both anonymous and registered users. **Highest blocker level.** |
| Bulk export | None |
| RSS / API | None |
| New Hanover launch | Track 7, February 3, 2025 |

**Verdict:** Treat as last-resort, human-in-loop spot-check tool. Do not build pipeline around it. The newspaper-notice flow (#6) is the parallel feed.

### 8. Stormwater Permits

| Field | Value |
|---|---|
| REST | `https://gis.nhcgov.com/server/rest/services/Layers/Stormwater_Impervious/FeatureServer/1` |
| Records | 1,116 |
| PID join | Direct (`PID` + `PIN` + `MAPID/MAPIDKEY` + `EnergovNumber`) |
| Fields | `PROJECT, ADDRESS, ENGINEER, ACRES, FEES, SUBMITTED, ISSUEDATE, STATUS, OWNER, NOTES` |
| Update | Live |

Useful sub-signal — stormwater non-compliance correlates with neglected commercial parcels. Lower volume than EnergovPermits.

### 9. CFPUA — Cape Fear Public Utility Authority

| Field | Value |
|---|---|
| Lead Service Line Inventory | https://services.arcgis.com/UfH3YtFuVFnIN4Zz/arcgis/rest/services/ServiceLine_55e933cf56bf4001bc6c63f3b085c0e3/FeatureServer/0 |
| Records | 77,417 service points |
| Schema | `address, location, utilmaterial, custmaterial, leadconnector, leadsolder, copperwithlead, replacestatus, scheddate, custreplacedate, yearstructbuilt, sensitivepop, disadvantaged, buildingtype, Service_Area` |
| PID | None — address-based join only |
| Notes | Pre-1986 housing stock has higher lead-pipe rate; correlates with absentee/distressed owners. Sub-signal at best. |
| Utility shutoffs | **NOT publicly exposed** — PII-restricted. Public records request only. |

### 10. City of Wilmington — gated

| Source | URL | Status |
|---|---|---|
| EnerGov SelfService | https://wilmingtonnc-energovweb.tylerhost.net/apps/SelfService | **Gated** — Angular SPA, CSRF token + session cookie required. Mid-effort Playwright scrape. Includes Permits, Plans, Code Cases, Inspections, Licenses (city-issued). |
| EPLdata MapServer | https://gis.wilmingtonnc.gov/arcgis/rest/services/Permitting/EPLdata/MapServer | Open — but layers labeled "Code Enforcement"/"Zoning Enforcement" contain only **inspector territory polygons** (9 + 2 records), NOT case data. |
| geohub portal | https://geohub.wilmingtonnc.gov/hosting/rest/services | EnerGov / CityBldg / Engineering folders return `code 499 Token Required` |
| ArcGIS Hub | https://data-wilmingtonnc.opendata.arcgis.com/ | 404 (retired or moved) |

**Coverage gap acknowledged:** city-issued building permits + city code cases are not in this build's v1 feed. Phase 2+ enhancement: Playwright session-based scrape of the EnerGov SelfService portal.

### 11. Beach towns (Wrightsville Beach / Carolina Beach / Kure Beach) — gap

No public open data portals, no ArcGIS Hub, no AGOL org. Town-issued building permits are NOT in any open feed. NHC `EnergovPermits` covers county-side fire/health permits in those jurisdictions but not town-issued building permits.

### 12. etax.nhcgov.com — enrichment-only

| Field | Value |
|---|---|
| Platform | iasWorld Public Access (Tyler Tech) |
| Search | https://etax.nhcgov.com/pt/search/commonsearch.aspx?mode=owner |
| Detail (deep-link target) | https://etax.nhcgov.com/pt/Datalets/Datalet.aspx?UseSearch=no&pin={PIN}&jur=NH&taxyr={YEAR} |
| Auth | None (disclaimer-accept POST + viewstate per session) |
| Notes | Used for the dashboard's "open in county" deep-link. Not scraped for primary data — PropertyOwners feed already carries everything we need. |

### 13. Kania Law Firm / RBCWB — DROPPED

Both audited. **Neither services New Hanover.** Kania lists Davidson + Mecklenburg only; RBCWB Mecklenburg only. Mecklenburg-precedent dual-firm pattern does not transfer. NHC handles GS 105-374 cases in-house through the Tax Department + Clerk of Superior Court — `nhcgov.com/345/Foreclosures` is canonical.

### 14. Munis Self Service — SKIP

`newhanovercountynccss.munisselfservice.com` is per-parcel only behind a Tyler Portico OAuth gate with `__AntiXsrfToken`. The county's delinquent CSV (#3) is strictly superior — all 5,120 parcels in one file vs Munis' one-at-a-time UI behind a JS gate.

---

## ANTI-BOT / OPERATIONAL BLOCKERS — SUMMARY

| Source | Blocker level | Mitigation |
|---|---|---|
| NHC GIS REST | None | Real HTTP client with browser UA |
| NHC EnergovPermits | None | Same |
| NHC Delinquent CSV | None | GET, not HEAD (CivicEngage 404s on HEAD) |
| NHC Foreclosures HTML | None | Parse HTML, low volume |
| ROD (BIS PHP) | None | PHPSESSID via `requests.Session()`; polite ~1 req/sec |
| Gannett iPublish (StarNews) | None | Apache, plain HTML, no WAF |
| ncnotices.com | Medium | ASP.NET viewstate handling |
| Stormwater Permits | None | NHC REST |
| CFPUA LSLI | None | AGOL REST |
| etax (iasWorld) | Low | Disclaimer POST + viewstate (enrichment-only) |
| Munis | High | Skip — redundant with the CSV |
| eCourts (Tyler Odyssey) | **Highest** | AWS WAF CAPTCHA every 5 min — newspaper notice flow as parallel feed |
| Wilmington EnerGov SelfService | Medium-High | Tyler SPA with CSRF — Phase 2+ Playwright scrape |
| Code-enforcement public dataset | **N/A — does not exist** | Use building-permit demolitions as proxy; document as gap |

---

## DISTRESS FLAG → SOURCE MAPPING

| Distress flag | NH source(s) | Notes |
|---|---|---|
| Back taxes / delinquency | NHC Delinquent CSV | Direct PID join |
| Tax foreclosure | NHC Foreclosures HTML | Low-volume, hot-tier |
| Code violations | **None public** | Use demolition permits as proxy; document gap |
| Demolition orders | NHC EnergovPermits filtered to `'%Demolition%'` | 2,247 historical records |
| Water shutoff / utility disconnect | **None public** | PII-restricted; PRR only |
| Fire damage | **None public** | NFIRS bulk via FEMA OpenFEMA (annual) |
| Judgments | ROD `JDGMT` | No PID — name+address join |
| Mechanics liens | ROD `LIEN` (description regex) | NH does not have a separate code |
| Lis pendens | ROD `LIS PENS` | Foreclosure precursor |
| Foreclosure filings | StarNews + ROD `N/F`/`FCL`/`SUB TR` | StarNews is the live feed |
| Probate openings | StarNews Notice to Creditors | Captures decedent + executor + estate file # |
| Quitclaim transfers | ROD `QCD` | Heirship / divorce / partial-interest signal |

---

## PROPOSED 6-PATTERN STACK (NH-adapted)

The framework spec's core principle holds: **orthogonal pattern categories, not score inflation**. Tier comes from `stack_count`, not raw score sum. The same 6 buckets as Mecklenburg, with NH-specific source mapping:

### Pattern 1 — `jfc` — Judicial Foreclosure (Power of Sale)

Fires on:
- StarNews "Foreclosures - Sheriff Sales" notice within last 90 days
- ROD `N/F`, `FCL`, `FCL DEED`, `SUB TR`, `SUB TR DEED`, `RES SUB/TR` filings within last 18 months

Strength: highest single signal. A property in active foreclosure is the textbook motivated seller.

### Pattern 2 — `tax` — Tax Distress

Fires on:
- Owner appears on NHC Delinquent CSV with `Total Due` ≥ $500 (filter pennies)
- Parcel listed on NHC Foreclosures HTML active sale schedule
- Multi-juris delinquency (same parcel on county AND municipal) — sub-flag

Strength: highest *volume* signal. 5,120 candidates per refresh — most leads come from here.

### Pattern 3 — `estate` — Probate / Estate Opened

Fires on:
- StarNews Notice to Creditors within last 12 months, decedent name matches PARID owner (tight matcher: 3-word LAST+FIRST+MIDDLE preferred; 2-word fallback gated on surname not in top-50 common-surnames list)
- ROD `ADMIN DEED`, `EXEC DEED`, `EXTRX DEED` recorded in last 24 months

Strength: high when joined; low when estate has no NH parcel (those filter out via the join).

### Pattern 4 — `code` — Code Violation / Demolition

**NH-adapted (weaker than Mecklenburg's Charlotte feed):**
- Fires on NHC EnergovPermits `PERMIT_TYPE LIKE '%Demolition%'` issued in last 24 months
- Sub-flags: `commercial_demolition`, `residential_demolition`, `floodplain_dev_permit`, `stormwater_permit_open`

**Documented gap:** no proper code-enforcement case dataset exists for NH or City of Wilmington. The `code` pattern in this build is structurally weaker than Mecklenburg's. Honest acknowledgement in methodology.html.

### Pattern 5 — `lien` — Recorded Lien / Civil Judgment

Fires on:
- ROD `JDGMT`, `LIEN`, `LIS PENS`, `BKTCY` recorded in last 24 months
- Description regex extracts sub-type (`mechanics`, `tax`, `hoa`) since NH does not have separate codes

Strength: medium standalone, very high when stacked with `jfc` or `tax`. Address-match join (no PID).

### Pattern 6 — `transfer` — Distressed Conveyance

Fires on:
- ROD `QCD` (quitclaim) in last 24 months
- ROD `SEP AGMT` / `MEMO SEPR AGMT` (divorce co-signal) within 12 months of a `QCD` on the same parcel
- ROD `DEED OF GIFT` (no-consideration transfer)
- PropertyOwners-derived: recent sale (≤24mo) at nominal consideration (price < $1k OR price < 5% of `APRTOT`)
- PropertyOwners-derived: estate notice posted within 18 months prior to a parcel sale

Strength: medium standalone; functions primarily as a stack multiplier.

### Stack scoring rules (per FRAMEWORK_SPEC §3)

- One function — `matches(record)` — drives both filter counts and table content. No two-truths drift.
- `stack_count` = number of distinct pattern categories that fire on a parcel.
- Tier from `stack_count`:
  - **Hot** — stack_count ≥ 3
  - **Warm** — stack_count == 2
  - **Active** — stack_count == 1
- Sub-flags add raw_score within a tier but never promote.

---

## NEXT STEPS (Phase 2 — scrapers)

Priority order, fewest blockers first:

1. **Parcel master** — `polaris.py`-equivalent against `Layers/PropertyOwners/FeatureServer/0`. (Mecklenburg's `polaris.py` is the structural template.)
2. **EnergovPermits** — same ArcGIS REST pattern, filter for distress permit types.
3. **Delinquent CSV** — direct GET → CSV parse → JSONL.
4. **NHC Foreclosures HTML** — small HTML table parse.
5. **ROD per-doctype** — date-range search with PHPSESSID, paginate ≤2000/query, one JSONL per doc-type group.
6. **StarNews Notice to Creditors + Foreclosures** — Gannett iPublish HTML scrape, GET-paginated.
7. **Stormwater Permits** — ArcGIS REST.
8. **CFPUA LSLI** — sub-signal, low priority.

Phase 1 ends here. ⚡
