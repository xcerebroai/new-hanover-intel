# New Hanover Intel

A flat-file motivated-seller intelligence pipeline for **New Hanover County, NC** (Wilmington). Joins public-records signals (parcel master, building permits, delinquent tax CSV, county foreclosures, register of deeds, newspaper legal notices) on parcel ID (or address / owner-name fuzzy matching for sources without a PID) and surfaces properties where multiple distinct distress patterns stack on the same address. Output is a single static dashboard.

> **Status:** Phase 1 — recon complete. Build in progress. See `RECON.md` for the source map and adapted 6-pattern stack.

---

## How it works

1. **Multiple scrapers** in `scrapers/` fetch raw public data (NHC PropertyOwners parcel master, NHC EnergovPermits, NHC Delinquent Tax CSV, NHC Foreclosures HTML, NHC Stormwater Permits, ROD per-doctype, StarNews legal notices). Each writes JSONL to `data/raw/`.
2. **`pipeline/build_leads.py`** loads everything, joins on PID where available, falls back to address / owner-name fuzzy matching for ROD and StarNews, runs each parcel through the six-pattern stack (`jfc`, `tax`, `estate`, `code`, `lien`, `transfer`), computes derived fields (equity %, years owned, absentee, senior, likely-inherited, etc.), assigns a tier from the stack depth, and writes a single `data/leads.json`.
3. **`index.html`** is a single-file vanilla-JS dashboard that loads `data/leads.json`, applies live filters, expands per-row signal detail, and exports filtered sets to CSV ready for skip-trace upload.
4. **`pipeline/refresh.py`** is the daily-refresh harness — runs all scrapers in dependency order, regenerates `leads.json`, stages the diff, commits + pushes when `--push` is set. Designed to be called by OpenClaw on cron.

The two non-negotiable design rules: **tier comes from how many distinct patterns stack on a parcel** (never from raw score sum), and **filter counts are derived from the same `matches(lead)` function that builds the visible table**.

---

## Why daily-live data

The competitive moat against PropStream / DealMachine / PropertyRadar is freshness — they buy bulk monthly extracts; this stack hits source the day filings hit the docket. Daily refresh is the product, not a nice-to-have. See `OPERATIONS.md` for the OpenClaw integration.

---

## Layout

```
new-hanover-intel/
├── pipeline/
│   ├── build_leads.py         # joins + scoring + leads.json output
│   └── refresh.py             # daily refresh harness
├── scrapers/                  # one scraper per source — fetch only
├── scripts/
│   └── daily_refresh.xml      # Windows Task Scheduler config (4 AM Central)
├── data/
│   ├── raw/                   # gitignored — raw scraped JSONL
│   └── leads.json             # the deliverable; committed
├── docs/                      # screenshots
├── index.html                 # single-file dashboard
├── methodology.html           # data sources, scoring, limitations
├── RECON.md                   # source inventory + adapted 6-pattern stack
├── OPERATIONS.md              # daily refresh ops + OpenClaw integration
├── HEARTBEAT.json             # written by refresh.py — last_success per source
└── README.md                  # this file
```

---

⚡ — Built by Jarvis (Just Jarvis LLC) for Quentin Flores. Operator-first.
