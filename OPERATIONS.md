# OPERATIONS — daily refresh integration

This document covers everything an operator (or OpenClaw) needs to know to keep
`new-hanover-intel` refreshing live every day.

## TL;DR

The harness:

```powershell
py -3.12 pipeline\refresh.py --push
```

runs every scraper in dependency order, regenerates `data/leads.json`, updates
`HEARTBEAT.json`, and pushes to `xcerebroai/new-hanover-intel`. Designed for
unattended cron / Task Scheduler execution.

Exit codes:

| Code | Meaning | Action |
|---|---|---|
| 0 | Full success | nothing — Telegram diff alert if new Hot/Warm leads |
| 1 | Partial — at least one scraper failed but pipeline ran | Telegram alert listing `failed_sources` |
| 2 | Pipeline / Two-Truths failure (or critical scraper failed) | **Page operator** — DO NOT ship unless investigated |

## How to register the Windows scheduled task

```powershell
schtasks /create /xml scripts\daily_refresh.xml /tn "new-hanover-intel-refresh" /f
```

Default trigger: 04:00 America/Chicago (Central) every day. The XML uses
`InteractiveToken` so the task runs under the logged-in user — adjust to a
service account if running on a server.

To verify: `schtasks /query /tn "new-hanover-intel-refresh" /v /fo list`.

To run it once now: `schtasks /run /tn "new-hanover-intel-refresh"`.

## How OpenClaw should call refresh.py

PowerShell, working dir = repo root:

```powershell
$env:PYTHON_BIN = "C:\Users\Owner\AppData\Local\Programs\Python\Python312\python.exe"
& $env:PYTHON_BIN "C:\Dev\xcerebro-builds\projects\new-hanover-intel\pipeline\refresh.py" --push
```

OpenClaw should treat the **process exit code** as the primary signal, and
read `HEARTBEAT.json` to determine which sources succeeded vs failed.

## HEARTBEAT.json

Written by `refresh.py` after every run. Schema:

```json
{
  "last_success_timestamp": "2026-05-05T10:00:00+00:00",
  "last_pipeline_at": "2026-05-05T10:00:30+00:00",
  "last_pipeline_ok": true,
  "failed_sources": [],
  "per_source": {
    "property_owners": {
      "ok": true,
      "elapsed_sec": 145.2,
      "last_attempt_at": "...",
      "last_success_at": "..."
    },
    "delinquent_tax": { ... }
  }
}
```

`last_success_timestamp` is only updated when the entire run succeeded
(zero failed sources). Use this as the staleness signal.

## Alerting rules

OpenClaw should:

1. **Heartbeat staleness:** if `last_success_timestamp` is older than 36
   hours, send Telegram message:

   > ⚠️ new-hanover-intel heartbeat stale — last full success was
   > `<timestamp>`. Failed sources from last run: `<failed_sources>`.

2. **Per-source failure:** when any source in `per_source[*].ok == false`
   has a `last_success_at` more than 48h ago, send:

   > ⚠️ new-hanover-intel source `<slug>` has been failing for
   > 48+ hours. Last good: `<timestamp>`. Investigate.

   First failure = silent (transient errors are common). Two consecutive
   failed runs = page.

3. **New Hot/Warm leads:** diff `data/leads.json` against
   `data/leads.previous.json` on `pid`. For each new lead with `tier ==
   "hot"` (or `tier == "warm"` and at least one of: `imminent_tax_sale`,
   `demolition_permit`, `multi_juris_delinquent`):

   > 🔥 NEW Hot lead — `<owner>` @ `<address>`
   > Patterns: `<patterns>`
   > Stack: `<stack_count>` | Score: `<raw_score>`
   > Dashboard: https://xcerebroai.github.io/new-hanover-intel/?pid=<pid>

## Telegram bot config

OpenClaw uses bot `@Xcerebrobot`. Default chat target for alerts is the
operator's user ID `6004053137`. Configure via environment:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=6004053137
```

## Manual operations

Run a specific scraper only (skip the rest):

```powershell
py -3.12 pipeline\refresh.py --source property_owners --source delinquent_tax
```

Skip a known-broken scraper (others run normally):

```powershell
py -3.12 pipeline\refresh.py --skip rod_quitclaim
```

Dry run (skip git push and skip pipeline write):

```powershell
py -3.12 pipeline\refresh.py --dry-run
```

Force a full re-pull of one source (clear its state):

```powershell
py -3.12 scrapers\delinquent_tax.py --reset
```

## Source SLAs and failure-mode notes

| Source | SLA | Common failure mode | Recovery |
|---|---|---|---|
| `property_owners` | Daily success — critical | ArcGIS 502/504, transient | Built-in retry (4x backoff). Persistent failure = file ticket with NHC IT. |
| `energov_permits_*` | Daily success | ArcGIS server occasionally 500s | Same as parcels. |
| `delinquent_tax` | Refresh on first business day of each month | CivicEngage sometimes returns HTML preview page instead of CSV | Detect via `body size < 50KB` and retry next day. |
| `nhc_foreclosures` | Snapshot — refresh hourly | Static HTML; rarely fails | If page hash unchanged, scraper exits 0 with no work. |
| `starnews_legals` | Daily | Gannett listing page rarely 500s | Built-in retry. |
| `rod_*` | Daily | BIS `Session state is not available` if PHPSESSID expires mid-run | Scraper re-seeds session at start of each invocation. |

## Known gaps (documented in `methodology.html`)

- No public code-enforcement dataset → `code` pattern fires only via
  EnerGov demolition permits.
- City of Wilmington EnerGov SelfService is gated by Tyler SPA + CSRF →
  city-issued building permits + city code cases NOT in v1.
- eCourts (Tyler Odyssey) is AWS-WAF-CAPTCHA-gated → judicial foreclosures
  + estate openings come exclusively from StarNews.
- Beach towns (Wrightsville, Carolina, Kure) have no public open-data feeds.

## Disaster recovery

If `data/leads.json` is corrupted or contains a Two-Truths violation:

1. The harness exits 2 and does NOT push.
2. `data/leads.previous.json` is still good — the rotation only happens
   AFTER the new file successfully writes (and Two-Truths passes).
3. Restore by: `cp data/leads.previous.json data/leads.json` and push manually.
4. Investigate the join logic before next run.

⚡ — Jarvis
