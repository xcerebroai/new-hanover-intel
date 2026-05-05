"""build_leads.py — New Hanover County motivated-seller lead pipeline.

Joins all data/raw/*.jsonl signal feeds against the PropertyOwners master
parcel table and writes data/leads.json — the dashboard's input.

Sources joined (per RECON.md):
  property_owners_layer0       — parcel master (PARID is the global join key)
  delinquent_tax               — county delinquent-tax CSV (direct PARID)
  nhc_foreclosures             — county GS 105-374 schedule (direct PARID)
  energov_permits_demolition   — building-permit demolitions (direct PARID)
  energov_permits_floodplain_development
  energov_permits_occupancy_certification
  nhc_stormwater               — stormwater permits (direct PARID/PID)
  starnews_notice_to_creditors — probate notices (decedent → owner-name join)
  starnews_foreclosures        — foreclosure notices (address join)
  rod_<doctype>                — register of deeds (no PID — owner+address join)

Output schema follows the FRAMEWORK_SPEC §3 contract:
  generated_at, source_commit, total, tier_counts, pattern_counts,
  source_attach_counts, doc_type_counts, transfer_rule_counts,
  warm_tier_high_confidence_pct, top_pattern_combos, records[]

Tier from STACK DEPTH (count of distinct patterns) — never from score sum.
Two-Truths check runs before write: header counts must equal counts derived
from records[]; mismatch raises and exits non-zero.

The dashboard's matches(filters, lead) function reuses the patterns + flags
this pipeline emits — single source of truth for filter counts AND filter
results.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_PATH = PROJECT_ROOT / "data" / "leads.json"
PREV_PATH = PROJECT_ROOT / "data" / "leads.previous.json"

PARCEL_PATH = RAW_DIR / "property_owners_layer0.jsonl"
DELQ_PATH = RAW_DIR / "delinquent_tax.jsonl"
FCL_HTML_PATH = RAW_DIR / "nhc_foreclosures.jsonl"
ENERGOV_DEMO_PATH = RAW_DIR / "energov_permits_demolition.jsonl"
ENERGOV_FLOOD_PATH = RAW_DIR / "energov_permits_floodplain_development.jsonl"
ENERGOV_OCC_PATH = RAW_DIR / "energov_permits_occupancy_certification.jsonl"
STORMWATER_PATH = RAW_DIR / "nhc_stormwater.jsonl"
STARNEWS_NTC_PATH = RAW_DIR / "starnews_notice_to_creditors.jsonl"
STARNEWS_FCL_PATH = RAW_DIR / "starnews_foreclosures.jsonl"

ROD_DOCTYPES = [
    "deed", "quitclaim", "deed_of_trust", "assignment", "satisfaction",
    "foreclosure", "estate_deed", "judgment", "lien",
    "deed_of_gift", "separation",
]

PATTERNS = ["jfc", "tax", "estate", "code", "lien", "transfer"]
TIER_HOT = "hot"
TIER_WARM = "warm"
TIER_ACTIVE = "active"

SIGNAL_CAP_PER_PATTERN = 3
DELINQUENCY_MIN_DUE = 500.0  # filter administrative pennies

PARID_RE = re.compile(r"R\d{5}-\d{3}-\d{3}-\d{3}")

# Common surnames — used to gate decedent/grantor 2-word fallback matches.
# A "JONES JOHN" join against POLARIS would hit hundreds of parcels.
COMMON_SURNAMES = {
    "SMITH", "JOHNSON", "WILLIAMS", "BROWN", "JONES", "GARCIA", "MILLER",
    "DAVIS", "RODRIGUEZ", "MARTINEZ", "HERNANDEZ", "LOPEZ", "GONZALEZ",
    "WILSON", "ANDERSON", "THOMAS", "TAYLOR", "MOORE", "JACKSON", "MARTIN",
    "LEE", "PEREZ", "THOMPSON", "WHITE", "HARRIS", "SANCHEZ", "CLARK",
    "RAMIREZ", "LEWIS", "ROBINSON", "WALKER", "YOUNG", "ALLEN", "KING",
    "WRIGHT", "SCOTT", "TORRES", "NGUYEN", "HILL", "FLORES", "GREEN",
    "ADAMS", "NELSON", "BAKER", "HALL", "RIVERA", "CAMPBELL", "MITCHELL",
    "CARTER", "ROBERTS",
}

ENTITY_TOKENS = {
    "LLC", "L.L.C", "L L C", "INC", "INCORPORATED", "CORP", "CORPORATION",
    "CO.", "COMPANY", "TRUST", "TRUSTEE", "LP", "LLP", "L.P", "L.L.P",
    "PLLC", "PA", "P.A", "ASSOCIATES", "ASSOCIATION", "ASSOC", "FOUNDATION",
    "LIMITED", "PARTNERS", "PARTNERSHIP", "ESTATE OF",
}
LANDLORD_TOKENS = {
    "RENTAL", "RENTALS", "PROPERTIES", "PROPERTY", "HOLDINGS", "INVESTMENTS",
    "REALTY", "REAL ESTATE", "HOMES", "RE LLC", "REI", "GROUP",
}
NAME_NOISE = {
    "INC", "INCORPORATED", "LLC", "L L C", "LP", "L P", "PLLC",
    "CORP", "CORPORATION", "CO",
    "TRUST", "TRUSTEE", "REVOCABLE", "LIVING",
    "ETAL", "ET AL", "ET UX",
    "JR", "SR", "II", "III", "IV",
    "ESTATE", "DECEASED",
    "FAMILY", "IRREVOCABLE",
    "FOUNDATION",
}
NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}

NUM_RE = re.compile(r"^\s*(\d+)")
NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]")

STREET_SUFFIX = {
    "ST": "ST", "STREET": "ST",
    "AV": "AVE", "AVE": "AVE", "AVENUE": "AVE",
    "RD": "RD", "ROAD": "RD",
    "DR": "DR", "DRIVE": "DR",
    "LN": "LN", "LANE": "LN",
    "CT": "CT", "COURT": "CT",
    "CR": "CIR", "CIR": "CIR", "CIRCLE": "CIR",
    "PL": "PL", "PLACE": "PL",
    "BLVD": "BLVD", "BOULEVARD": "BLVD",
    "PKWY": "PKWY", "PARKWAY": "PKWY",
    "TER": "TER", "TERRACE": "TER",
    "WAY": "WAY", "WY": "WAY",
    "HWY": "HWY", "HIGHWAY": "HWY",
    "TRL": "TRL", "TRAIL": "TRL",
    "SQ": "SQ", "SQUARE": "SQ",
    "RUN": "RUN", "PT": "PT", "POINT": "PT",
}


# ----------------------------------------------------------------------
# Normalization helpers
# ----------------------------------------------------------------------

def normalize_owner(name: str) -> str:
    if not name:
        return ""
    n = NON_ALNUM_RE.sub(" ", name.upper())
    parts = [p for p in n.split() if p and p not in NAME_NOISE]
    return " ".join(parts)


def owner_keys(normalized: str) -> list[str]:
    """Build keys for fuzzy owner matching."""
    parts = [p for p in normalized.split() if p]
    if len(parts) < 2:
        return []
    keys: list[str] = []
    a, b = parts[0], parts[1]
    if len(a) + len(b) >= 8:
        keys.append(f"{a} {b}")
        keys.append(f"{b} {a}")
    if len(parts) >= 3:
        c = parts[2]
        keys.append(f"{a} {b} {c}")
        keys.append(f"{c} {b} {a}")
        keys.append(f"{c} {a} {b}")
        if len(a) + len(c) >= 8:
            keys.append(f"{a} {c}")
            keys.append(f"{c} {a}")
    return keys


def decedent_match_keys(decedent_normalized: str) -> list[str]:
    """Tighter than owner_keys — refuses to match on common-surname two-word
    fallbacks. Used for estate notice → owner joins where false positives
    multiply across thousands of parcels."""
    parts = [p for p in decedent_normalized.split() if p]
    if len(parts) < 2:
        return []
    keys: list[str] = []
    if len(parts) >= 3:
        keys.append(" ".join(parts[:3]))
        keys.append(f"{parts[2]} {parts[1]} {parts[0]}")
        keys.append(f"{parts[-1]} {parts[0]} {parts[1]}")
    last = parts[-1]
    first = parts[0]
    if len(last) >= 5 and last not in COMMON_SURNAMES:
        keys.append(f"{first} {last}")
        keys.append(f"{last} {first}")
    return list(dict.fromkeys(keys))


def normalize_street(street_full: str) -> tuple[str, str]:
    """Returns (street_number, normalized_streetname).
    Strips city/state/zip suffix and unit info.
    """
    if not street_full:
        return ("", "")
    s = street_full.upper()
    s = re.sub(r",\s*\w[\w\s]*,?\s*NC[\s,0-9-]*$", "", s)
    s = re.sub(r"\s+\d{5}(-\d{4})?$", "", s)
    s = re.sub(r"\s+(UNIT|APT|STE|SUITE|#)\s*\S+", "", s)
    m = NUM_RE.match(s)
    if not m:
        return ("", "")
    num = m.group(1)
    rest = s[m.end():].strip()
    rest = NON_ALNUM_RE.sub(" ", rest)
    tokens = [t for t in rest.split() if t]
    cut = -1
    for i, t in enumerate(tokens):
        if t in STREET_SUFFIX:
            cut = i
            break
    if cut >= 0:
        tokens = tokens[: cut + 1]
        tokens[-1] = STREET_SUFFIX[tokens[-1]]
    return (num, " ".join(tokens))


def is_entity_owner(owner: str) -> bool:
    if not owner:
        return False
    up = " " + owner.upper() + " "
    for tok in ENTITY_TOKENS:
        if " " + tok + " " in up or " " + tok + "." in up:
            return True
    return False


def is_landlord_entity(owner: str) -> bool:
    if not owner:
        return False
    up = owner.upper()
    return is_entity_owner(owner) and any(tok in up for tok in LANDLORD_TOKENS)


def parse_owner_name(own1: str) -> dict:
    """Split OWN1 (e.g. 'GOLASKI LORIEN ETAL', 'CITY OF WILMINGTON',
    'ABC HOLDINGS LLC') into first/middle/last/suffix/is_entity.

    NHC's PropertyOwners packs the full name into a single OWN1 field,
    typically "LASTNAME FIRSTNAME [MIDDLE] [SUFFIX]" for individuals.
    """
    full = (own1 or "").strip()
    if is_entity_owner(full):
        return {"first_name": "", "middle_name": "", "last_name": "",
                "suffix": "", "is_entity": True, "full_name": full}
    parts = [p for p in full.split() if p]
    suffix = ""
    if parts and parts[-1].rstrip(".").upper() in NAME_SUFFIXES:
        suffix = parts.pop().rstrip(".").upper()
    if not parts:
        return {"first_name": "", "middle_name": "", "last_name": "",
                "suffix": suffix, "is_entity": False, "full_name": full}
    last = parts[0]  # NHC OWN1 format: LAST FIRST MIDDLE
    first = parts[1] if len(parts) >= 2 else ""
    middle = " ".join(parts[2:]) if len(parts) >= 3 else ""
    return {"first_name": first, "middle_name": middle, "last_name": last,
            "suffix": suffix, "is_entity": False, "full_name": full}


def _f(v) -> float | None:
    """Tolerant float parse for fields that come in as strings."""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _ms_to_dt(v) -> datetime | None:
    if v is None or v == "":
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    if not (0 < n < 4_102_444_800_000):
        return None
    try:
        return datetime.fromtimestamp(n / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _parse_sale_date(v) -> datetime | None:
    """SALE_DATE comes in as 'YYYY-MM-DD HH:MM:SS' or epoch ms or empty."""
    dt = _ms_to_dt(v)
    if dt:
        return dt
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ----------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_parcel_master(path: Path, log) -> tuple[dict, dict, dict]:
    """Returns (by_pid, by_addr_key, by_owner_key)."""
    by_pid: dict[str, dict] = {}
    by_addr: dict[tuple[str, str], list[str]] = defaultdict(list)
    by_owner: dict[str, set[str]] = defaultdict(set)
    log(f"[parcels] reading {path.name}...")
    if not path.exists():
        log(f"[!] parcel master missing: {path} — pipeline cannot proceed")
        return {}, {}, {}
    n = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            pid = (r.get("PARID") or "").strip()
            if not pid or pid in by_pid:
                continue
            owner = (r.get("OWN1") or "").strip()
            site_addr = " ".join(filter(None, [
                str(r.get("ADRNO") or "").strip(),
                (r.get("ADRADD") or "").strip(),
                (r.get("ADRSTR") or "").strip(),
                (r.get("ADRSUF") or "").strip(),
            ])).strip()
            mail_addr = " ".join(filter(None, [
                str(r.get("OWNER_NUM") or "").strip(),
                (r.get("OWNER_STREET") or "").strip(),
                (r.get("OWNER_STREETTYPE") or "").strip(),
            ])).strip()
            site_num, site_name = normalize_street(site_addr)
            mail_num, mail_name = normalize_street(mail_addr)
            absentee = bool(site_name and mail_name and (site_name != mail_name or site_num != mail_num))

            sale_dt = _parse_sale_date(r.get("SALE_DATE"))
            sale_price = _f(r.get("SALE_PRICE"))
            apr_total = _f(r.get("APRTOT"))
            apr_land = _f(r.get("APRLAND"))
            apr_bldg = _f(r.get("APRBLDG"))

            entry = {
                "parid": pid,
                "mapid": (r.get("MAPID") or "").strip(),
                "mapidkey": (r.get("MAPIDKEY") or "").strip(),
                "own1": owner,
                "site_address": site_addr,
                "site_city": (r.get("CITYNAME") or "").strip(),
                "mail_address": mail_addr,
                "mail_city": (r.get("OWNER_CITY") or "").strip(),
                "mail_state": (r.get("OWNER_STATE") or "").strip(),
                "mail_zip": (r.get("OWNER_ZIP") or "").strip(),
                "absentee": absentee,
                "subdiv": (r.get("SUBDIV") or "").strip(),
                "legal1": (r.get("LEGAL1") or "").strip(),
                "muni": (r.get("MUNI") or "").strip(),
                "zoning": (r.get("ZONING") or "").strip(),
                "land_use_code": (r.get("LUC") or "").strip(),
                "class_code": (r.get("CLASS") or "").strip(),
                "sfla": _i(r.get("SFLA")),
                "sale_date_iso": sale_dt.isoformat() if sale_dt else "",
                "sale_price": sale_price,
                "sale_instrument": (r.get("SALE_INSTRUMENT") or "").strip(),
                "sale_book": (r.get("SALE_BOOK") or "").strip(),
                "sale_page": (r.get("SALE_PAGE") or "").strip(),
                "apr_total": apr_total,
                "apr_land": apr_land,
                "apr_bldg": apr_bldg,
                "apr_taxyr": _i(r.get("APRVAL_TAXYR")),
            }
            by_pid[pid] = entry
            if site_num and site_name:
                by_addr[(site_num, site_name)].append(pid)
            normalized = normalize_owner(owner)
            for k in owner_keys(normalized):
                if len(k) >= 4:
                    by_owner[k].add(pid)
            n += 1
    log(f"[parcels] indexed {n:,} parcels  addr_keys={len(by_addr):,}  owner_keys={len(by_owner):,}")
    return by_pid, dict(by_addr), dict(by_owner)


# ----------------------------------------------------------------------
# Lead aggregation
# ----------------------------------------------------------------------

def _get_lead(leads: dict, pid: str) -> dict:
    if pid not in leads:
        leads[pid] = {
            "pid": pid,
            "patterns": set(),
            "signals": {p: [] for p in PATTERNS},
            "flags": [],
            "doc_types": set(),
        }
    return leads[pid]


def join_signals(by_pid: dict, by_addr: dict, by_owner: dict, log) -> tuple[dict, dict]:
    """Walk every signal source, attach to PIDs. Returns (leads, source_counts)."""
    leads: dict[str, dict] = {}
    source_counts: dict[str, int] = defaultdict(int)

    # ---- delinquent_tax: direct PARID join ----
    delq_attached = delq_unmatched = delq_filtered = 0
    delq_by_pid: dict[str, list[dict]] = defaultdict(list)
    for r in load_jsonl(DELQ_PATH):
        pid = (r.get("parcel") or "").strip()
        if not pid or pid not in by_pid:
            delq_unmatched += 1
            continue
        due = r.get("total_due")
        if due is None or due < DELINQUENCY_MIN_DUE:
            delq_filtered += 1
            continue
        delq_by_pid[pid].append(r)
        delq_attached += 1
    for pid, rows in delq_by_pid.items():
        lead = _get_lead(leads, pid)
        lead["patterns"].add("tax")
        lead["doc_types"].add("DELQ")
        # Sum across multi-jurisdiction rows for this parcel
        total = sum((r.get("total_due") or 0) for r in rows)
        for r in rows:
            lead["signals"]["tax"].append({
                "source": "nhc_delinquent_csv",
                "doc_type": "DELQ",
                "name": r.get("name"),
                "juris_code": r.get("juris_code"),
                "juris_description": r.get("juris_description"),
                "total_due": r.get("total_due"),
                "last_payment_date": r.get("last_payment_date"),
                "address": r.get("location_street"),
                "date": r.get("last_payment_date") or "",
            })
        if total >= 5000:
            lead["flags"].append("delinquency_over_5k")
        if total >= 25000:
            lead["flags"].append("delinquency_over_25k")
        if len({r.get("juris_code") for r in rows}) > 1:
            lead["flags"].append("multi_juris_delinquent")
    source_counts["delinquent_tax"] = delq_attached
    log(f"[delinquent_tax] attached={delq_attached}  unmatched_pid={delq_unmatched}  "
        f"filtered_under_min={delq_filtered}")

    # ---- nhc_foreclosures (HTML schedule): direct PARID join ----
    fcl_attached = fcl_unmatched = 0
    for r in load_jsonl(FCL_HTML_PATH):
        pid = (r.get("parcel") or "").strip()
        if not pid or pid not in by_pid:
            fcl_unmatched += 1
            continue
        lead = _get_lead(leads, pid)
        lead["patterns"].add("tax")     # GS 105-374 is tax foreclosure → tax pattern
        lead["patterns"].add("jfc")     # also fires jfc — court-supervised sale
        lead["doc_types"].add("TAX_FC")
        lead["signals"]["tax"].append({
            "source": "nhc_foreclosures_html",
            "doc_type": "TAX_FC",
            "case_number": r.get("case_number"),
            "sale_date": r.get("sale_date"),
            "sale_time": r.get("sale_time"),
            "sale_location": r.get("sale_location"),
            "address": r.get("street_address"),
            "statute": r.get("statute"),
            "date": r.get("sale_date") or "",
        })
        lead["signals"]["jfc"].append({
            "source": "nhc_foreclosures_html",
            "doc_type": "TAX_FC",
            "case_number": r.get("case_number"),
            "sale_date": r.get("sale_date"),
            "address": r.get("street_address"),
            "date": r.get("sale_date") or "",
        })
        lead["flags"].append("imminent_tax_sale")
        fcl_attached += 1
    source_counts["nhc_foreclosures_html"] = fcl_attached
    log(f"[nhc_foreclosures] attached={fcl_attached}  unmatched_pid={fcl_unmatched}")

    # ---- energov_permits: direct PID/PARID join ----
    for path, doctype, sub_flag in [
        (ENERGOV_DEMO_PATH, "DEMO", "demolition_permit"),
        (ENERGOV_FLOOD_PATH, "FLOODPLAIN_DEV", "floodplain_dev_permit"),
        (ENERGOV_OCC_PATH, "OCC_CERT", None),
    ]:
        attached = unmatched = 0
        for r in load_jsonl(path):
            pid = (r.get("PID") or r.get("pid") or "").strip()
            if not pid or pid not in by_pid:
                unmatched += 1
                continue
            lead = _get_lead(leads, pid)
            # Demolition fires 'code' pattern; floodplain + occ are sub-flags only.
            if doctype == "DEMO":
                lead["patterns"].add("code")
                lead["doc_types"].add("DEMO")
                lead["signals"]["code"].append({
                    "source": "nhc_energov_permits",
                    "doc_type": "DEMO",
                    "permit_number": r.get("PERMIT_NUMBER"),
                    "permit_type": r.get("PERMIT_TYPE"),
                    "work_class": r.get("WORK_CLASS"),
                    "permit_status": r.get("PERMIT_STATUS"),
                    "issue_date_ms": r.get("ISSUE_DATE"),
                    "description": (r.get("DESCRIPTION") or "")[:300],
                    "address": (r.get("STREET") or "").strip(),
                    "valuation": r.get("VALUATION"),
                    "date": _ms_iso(r.get("ISSUE_DATE")),
                })
                if sub_flag:
                    lead["flags"].append(sub_flag)
            elif doctype == "FLOODPLAIN_DEV":
                # Sub-flag only; doesn't fire pattern alone.
                lead["flags"].append(sub_flag) if sub_flag else None
                lead["doc_types"].add("FLOODPLAIN_DEV")
            elif doctype == "OCC_CERT":
                lead["doc_types"].add("OCC_CERT")
            attached += 1
        source_counts[f"energov_{doctype.lower()}"] = attached
        log(f"[energov:{doctype}] attached={attached}  unmatched_pid={unmatched}")

    # ---- nhc_stormwater: direct PID join ----
    sw_attached = sw_unmatched = 0
    for r in load_jsonl(STORMWATER_PATH):
        pid = (r.get("PID") or "").strip()
        if not pid or pid not in by_pid:
            sw_unmatched += 1
            continue
        # Open / unfinaled stormwater permit is the relevant flag.
        status = (r.get("STATUS") or "").upper()
        is_open = "OPEN" in status or "REVIEW" in status or status == "" or "ACTIVE" in status
        lead = _get_lead(leads, pid)
        lead["doc_types"].add("STORMWATER")
        if is_open:
            lead["flags"].append("stormwater_permit_open")
        sw_attached += 1
    source_counts["nhc_stormwater"] = sw_attached
    log(f"[stormwater] attached={sw_attached}  unmatched_pid={sw_unmatched}")

    # ---- starnews_notice_to_creditors: decedent → owner-name join ----
    est_attached = est_unmatched = est_skipped = 0
    for r in load_jsonl(STARNEWS_NTC_PATH):
        if not r.get("is_new_hanover"):
            continue
        decedent = r.get("decedent_name") or ""
        norm = normalize_owner(decedent)
        keys = decedent_match_keys(norm)
        if not keys:
            est_skipped += 1
            continue
        candidate_pids: set[str] = set()
        for k in keys:
            candidate_pids |= by_owner.get(k, set())
        if not candidate_pids:
            est_unmatched += 1
            continue
        for pid in candidate_pids:
            lead = _get_lead(leads, pid)
            lead["patterns"].add("estate")
            lead["doc_types"].add("NTC")
            lead["signals"]["estate"].append({
                "source": "starnews_notice_to_creditors",
                "doc_type": "NTC",
                "decedent_name": decedent,
                "case_number": r.get("case_number"),
                "executor": r.get("executor_or_administrator"),
                "claim_deadline": r.get("claim_deadline"),
                "posted_date": r.get("posted_date"),
                "detail_url": r.get("detail_url"),
                "date": r.get("posted_date") or "",
            })
            est_attached += 1
    source_counts["starnews_notice_to_creditors"] = est_attached
    log(f"[starnews:NTC] attached={est_attached}  unmatched_owner={est_unmatched}  "
        f"skipped_unsafe={est_skipped}")

    # ---- starnews_foreclosures: address-string match (best effort) ----
    sf_attached = sf_unmatched = 0
    for r in load_jsonl(STARNEWS_FCL_PATH):
        if not r.get("is_new_hanover"):
            continue
        body = r.get("body") or ""
        # Look for address-like patterns in the body and attempt address-key join.
        # NHC trustee notices typically say "the property described as <addr>".
        candidate_addrs = re.findall(
            r"\b(\d{2,5}\s+[A-Z][A-Z .'-]+?(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|"
            r"LANE|LN|COURT|CT|CIRCLE|CIR|PLACE|PL|BOULEVARD|BLVD|TRAIL|TRL|"
            r"WAY|PARKWAY|PKWY))\b",
            body.upper(),
        )
        candidate_pids: set[str] = set()
        for addr in candidate_addrs:
            num, name = normalize_street(addr)
            if num and name:
                for pid in by_addr.get((num, name), []):
                    candidate_pids.add(pid)
        if not candidate_pids:
            sf_unmatched += 1
            continue
        for pid in candidate_pids:
            lead = _get_lead(leads, pid)
            lead["patterns"].add("jfc")
            lead["doc_types"].add("FCL_NOTICE")
            lead["signals"]["jfc"].append({
                "source": "starnews_foreclosures",
                "doc_type": "FCL_NOTICE",
                "case_number": r.get("case_number"),
                "posted_date": r.get("posted_date"),
                "detail_url": r.get("detail_url"),
                "body": (r.get("body") or "")[:500],
                "date": r.get("posted_date") or "",
            })
            sf_attached += 1
    source_counts["starnews_foreclosures"] = sf_attached
    log(f"[starnews:FCL] attached={sf_attached}  unmatched_addr={sf_unmatched}")

    # ---- ROD per-doctype: address-key + owner-key join ----
    # ROD records carry no PID; we use the description column (which often
    # carries subdivision + lot) plus the reverse_party (which is grantor or
    # grantee depending on the entity's role) to attach.
    rod_pattern_map = {
        "foreclosure":   "jfc",
        "estate_deed":   "estate",
        "judgment":      "lien",
        "lien":          "lien",
        "quitclaim":     "transfer",
        "deed_of_gift":  "transfer",
        "separation":    "transfer",
        "satisfaction":  None,  # sub-flag, no pattern fire
        "deed":          None,  # informational baseline; no pattern fire
        "deed_of_trust": None,
        "assignment":    None,
    }
    for slug, pattern in rod_pattern_map.items():
        path = RAW_DIR / f"rod_{slug}.jsonl"
        attached = unmatched = skipped_entity = skipped_govt = 0
        # Govt/agency reverse-parties that explode to thousands of city-owned
        # or DOT-owned parcels when joined to OWN1 — never the motivated
        # seller. Skip these.
        govt_substr = (
            "CITY OF ", "TOWN OF ", "COUNTY OF ", "STATE OF ",
            "DEPARTMENT OF", "UNITED STATES", "SECRETARY OF",
            "INTERNAL REVENUE", "U.S. ", "USA",
            "NORTH CAROLINA", "BOARD OF EDUC",
            "WILMINGTON HOUSING",
        )
        for r in load_jsonl(path):
            party = r.get("reverse_party") or ""
            party_up = party.upper()
            # Skip governmental plaintiffs — they own thousands of parcels.
            if any(g in party_up for g in govt_substr):
                skipped_govt += 1
                continue
            # Skip corporate / entity reverse-parties (banks, lenders, HOAs).
            # Their OWN1 matches would explode to all entity-owned parcels.
            if is_entity_owner(party):
                skipped_entity += 1
                continue
            party_norm = normalize_owner(party)
            candidate_pids: set[str] = set()
            for k in owner_keys(party_norm):
                if len(k) >= 4:
                    candidate_pids |= by_owner.get(k, set())
            if not candidate_pids:
                unmatched += 1
                continue
            for pid in candidate_pids:
                lead = _get_lead(leads, pid)
                doc_label = (r.get("doc_type_label") or "").upper().strip() or slug.upper()
                lead["doc_types"].add(doc_label)
                if pattern:
                    lead["patterns"].add(pattern)
                    lead["signals"][pattern].append({
                        "source": "nhc_rod",
                        "doc_type": doc_label,
                        "instrument_number": r.get("instrument_number"),
                        "recorded_date": r.get("recorded_date"),
                        "book_code": r.get("book_code"),
                        "book_number": r.get("book_number"),
                        "page_number": r.get("page_number"),
                        "description": r.get("description"),
                        "reverse_party": party,
                        "detail_url": r.get("detail_url"),
                        "date": r.get("recorded_date") or "",
                    })
                else:
                    if slug == "satisfaction":
                        lead["flags"].append("rod_satisfaction_filed")
                attached += 1
        source_counts[f"rod_{slug}"] = attached
        log(f"[rod:{slug:14s}] attached={attached:>5}  unmatched={unmatched:>5}  "
            f"skip_entity={skipped_entity:>4}  skip_govt={skipped_govt:>4}")

    # ---- transfer rule (PropertyOwners-derived): nominal sale or post-estate sale ----
    transfer_rule_counts = {"quitclaim_rod": 0, "estate_deed_rod": 0,
                            "nominal_sale": 0, "post_estate_sale": 0,
                            "deed_of_gift_rod": 0, "separation_rod": 0}
    # Tally pre-existing transfer signals from ROD into the rule counter.
    for pid, lead in leads.items():
        for s in lead["signals"].get("transfer", []):
            src = s.get("source") or ""
            doc = (s.get("doc_type") or "").upper()
            if src == "nhc_rod":
                if "QCD" in doc or "QUITCLAIM" in doc:
                    transfer_rule_counts["quitclaim_rod"] += 1
                elif "GIFT" in doc:
                    transfer_rule_counts["deed_of_gift_rod"] += 1
                elif "SEP" in doc:
                    transfer_rule_counts["separation_rod"] += 1
        for s in lead["signals"].get("estate", []):
            if (s.get("doc_type") or "").upper() in {"ADMIN DEED", "EXEC DEED",
                                                       "EXTRX DEED", "COMMR DEED"}:
                transfer_rule_counts["estate_deed_rod"] += 1
                break

    pol_transfer = 0
    now = datetime.now(timezone.utc)
    for pid, lead in list(leads.items()):
        parcel = by_pid.get(pid)
        if not parcel:
            continue
        sale_iso = parcel.get("sale_date_iso") or ""
        sale_dt = None
        if sale_iso:
            try:
                sale_dt = datetime.fromisoformat(sale_iso)
            except ValueError:
                sale_dt = None
        sp = parcel.get("sale_price")
        tv = parcel.get("apr_total")
        recent = bool(sale_dt) and (now - sale_dt).days <= 730

        nominal = False
        if recent and sp is not None:
            if sp < 1000 or (tv and tv > 0 and sp / tv < 0.05):
                nominal = True
        if nominal:
            lead["patterns"].add("transfer")
            lead["flags"].append("nominal_consideration_recent_sale")
            lead["signals"]["transfer"].append({
                "source": "property_owners_sale",
                "doc_type": "NOMINAL_SALE",
                "sale_date": sale_iso,
                "sale_price": sp,
                "apr_total": tv,
                "date": sale_iso,
            })
            transfer_rule_counts["nominal_sale"] += 1
            pol_transfer += 1
            continue

        # Estate-then-sale: estate notice posted before a sale within 18 mo.
        estate_signals = lead["signals"].get("estate") or []
        if sale_dt and estate_signals:
            for s in estate_signals:
                est_str = s.get("posted_date") or ""
                est_dt = None
                for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
                    try:
                        est_dt = datetime.strptime(est_str, fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
                if not est_dt:
                    continue
                delta = (sale_dt - est_dt).days
                if 0 <= delta <= 548:
                    lead["patterns"].add("transfer")
                    lead["flags"].append("post_estate_recent_sale")
                    lead["signals"]["transfer"].append({
                        "source": "property_owners_sale",
                        "doc_type": "POST_ESTATE_SALE",
                        "sale_date": sale_iso,
                        "estate_posted": est_str,
                        "date": sale_iso,
                    })
                    transfer_rule_counts["post_estate_sale"] += 1
                    pol_transfer += 1
                    break
    log(f"[transfer:property_owners] fired={pol_transfer}")

    return leads, dict(source_counts), transfer_rule_counts


def _ms_iso(v) -> str:
    dt = _ms_to_dt(v)
    return dt.isoformat() if dt else ""


# ----------------------------------------------------------------------
# Lead-record assembly + scoring
# ----------------------------------------------------------------------

def score_lead(lead: dict, parcel: dict) -> dict:
    patterns = sorted(lead["patterns"])
    stack_count = len(patterns)
    if stack_count >= 3:
        tier = TIER_HOT
    elif stack_count == 2:
        tier = TIER_WARM
    else:
        tier = TIER_ACTIVE

    score = 0
    if "jfc" in patterns: score += 25
    if "tax" in patterns: score += 20
    if "estate" in patterns: score += 20
    if "code" in patterns: score += 12
    if "lien" in patterns: score += 12
    if "transfer" in patterns: score += 10

    flags = list(lead["flags"])
    if parcel.get("absentee"):
        flags.append("absentee_owner")
        score += 5
    apr_total = parcel.get("apr_total") or 0
    if apr_total >= 500_000:
        flags.append("value_over_500k")
        score += 4
    if "demolition_permit" in flags:
        score += 8

    return {
        "patterns": patterns,
        "stack_count": stack_count,
        "tier": tier,
        "raw_score": score,
        "flags": sorted(set(flags)),
    }


def compute_derived(parcel: dict, lead: dict) -> dict:
    today = datetime.now(timezone.utc)
    sale_iso = parcel.get("sale_date_iso") or ""
    sale_dt = None
    if sale_iso:
        try:
            sale_dt = datetime.fromisoformat(sale_iso)
        except ValueError:
            sale_dt = None
    sp = parcel.get("sale_price")
    tv = parcel.get("apr_total")

    out: dict = {}
    if sp is not None and tv and tv > 0 and sp > 0:
        out["estimated_equity_pct"] = round(max(0.0, min(100.0, (1.0 - sp / tv) * 100.0)), 1)
    else:
        out["estimated_equity_pct"] = None
    if sale_dt:
        out["years_owned"] = round((today - sale_dt).days / 365.25, 1)
    else:
        out["years_owned"] = None
    out["is_absentee"] = bool(parcel.get("absentee"))
    own = parcel.get("own1") or ""
    out["is_entity"] = is_entity_owner(own)
    if out["is_absentee"] and not out["is_entity"]:
        out["is_likely_landlord"] = True
    elif is_landlord_entity(own):
        out["is_likely_landlord"] = True
    else:
        out["is_likely_landlord"] = False
    out["is_homestead"] = False  # NHC PropertyOwners doesn't expose exemptions
    out["is_senior"] = False
    out["is_disabled_veteran"] = False
    out["is_disabled"] = False
    out["is_likely_inherited"] = False  # need year_built — not in source feed

    distress = (
        len(lead.get("patterns", set())) * 10
        + min(20, len(lead.get("flags", [])))
        + (5 if out["is_absentee"] else 0)
    )
    out["distress_score"] = distress
    return out


def _trim_signals(signals: dict) -> dict:
    out = {}
    for k, lst in signals.items():
        if not lst:
            continue
        ordered = sorted(lst, key=lambda s: str(s.get("date") or ""), reverse=True)
        out[k] = ordered[:SIGNAL_CAP_PER_PATTERN]
    return out


def build_lead_record(pid: str, parcel: dict, lead: dict) -> dict:
    s = score_lead(lead, parcel)
    derived = compute_derived(parcel, lead)
    parsed_owner = parse_owner_name(parcel.get("own1", ""))
    pin = parcel.get("mapidkey") or ""
    etax_url = (
        f"https://etax.nhcgov.com/pt/Datalets/Datalet.aspx?UseSearch=no"
        f"&pin={pid}&jur=NH&taxyr=2025"
    )
    return {
        "pid": pid,
        "mapid": parcel.get("mapid") or "",
        "mapidkey": pin,
        "etax_url": etax_url,
        "address": parcel.get("site_address") or "",
        "city": parcel.get("site_city") or "",
        "owner": parcel.get("own1") or "",
        "owner_parsed": parsed_owner,
        "is_entity": derived["is_entity"],
        "mail_address": parcel.get("mail_address") or "",
        "mail_city": parcel.get("mail_city") or "",
        "mail_state": parcel.get("mail_state") or "",
        "mail_zip": parcel.get("mail_zip") or "",
        "subdiv": parcel.get("subdiv") or "",
        "legal1": parcel.get("legal1") or "",
        "muni": parcel.get("muni") or "",
        "zoning": parcel.get("zoning") or "",
        "land_use_code": parcel.get("land_use_code") or "",
        "class_code": parcel.get("class_code") or "",
        "sfla": parcel.get("sfla"),
        "apr_total": parcel.get("apr_total"),
        "apr_land": parcel.get("apr_land"),
        "apr_bldg": parcel.get("apr_bldg"),
        "apr_taxyr": parcel.get("apr_taxyr"),
        "sale_date_iso": parcel.get("sale_date_iso") or "",
        "sale_price": parcel.get("sale_price"),
        "sale_instrument": parcel.get("sale_instrument") or "",
        "sale_book": parcel.get("sale_book") or "",
        "sale_page": parcel.get("sale_page") or "",
        "estimated_equity_pct": derived["estimated_equity_pct"],
        "years_owned": derived["years_owned"],
        "is_absentee": derived["is_absentee"],
        "is_likely_landlord": derived["is_likely_landlord"],
        "is_homestead": derived["is_homestead"],
        "is_senior": derived["is_senior"],
        "distress_score": derived["distress_score"],
        "patterns": s["patterns"],
        "stack_count": s["stack_count"],
        "tier": s["tier"],
        "raw_score": s["raw_score"],
        "flags": s["flags"],
        "doc_types": sorted(lead.get("doc_types") or set()),
        "signals": _trim_signals(lead["signals"]),
        # legacy alias
        "absentee": derived["is_absentee"],
    }


def two_truths_check(records: list[dict], header_tier: dict, header_pattern: dict) -> None:
    derived_tier = Counter(r["tier"] for r in records)
    derived_pattern: Counter = Counter()
    for r in records:
        for p in r["patterns"]:
            derived_pattern[p] += 1
    for k in (TIER_HOT, TIER_WARM, TIER_ACTIVE):
        if header_tier.get(k, 0) != derived_tier.get(k, 0):
            raise RuntimeError(
                f"Two-Truths violation: header tier_counts[{k}]={header_tier.get(k,0)} "
                f"!= records-derived {derived_tier.get(k,0)}"
            )
    for p in PATTERNS:
        if header_pattern.get(p, 0) != derived_pattern.get(p, 0):
            raise RuntimeError(
                f"Two-Truths violation: header pattern_counts[{p}]={header_pattern.get(p,0)} "
                f"!= records-derived {derived_pattern.get(p,0)}"
            )


def _git_short_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def write_output(leads: dict, by_pid: dict, source_counts: dict,
                 transfer_rule_counts: dict, log) -> dict:
    records = []
    for pid, lead in leads.items():
        if not lead["patterns"]:
            continue
        parcel = by_pid.get(pid, {})
        if not parcel:
            continue
        records.append(build_lead_record(pid, parcel, lead))

    tier_rank = {TIER_HOT: 3, TIER_WARM: 2, TIER_ACTIVE: 1}
    records.sort(key=lambda r: (tier_rank[r["tier"]], r["stack_count"], r["raw_score"]),
                 reverse=True)

    tier_counts = Counter(r["tier"] for r in records)
    pattern_counts: Counter = Counter()
    doc_type_counts: Counter = Counter()
    combo_counts: Counter = Counter()
    for r in records:
        for p in r["patterns"]:
            pattern_counts[p] += 1
        for d in r["doc_types"]:
            doc_type_counts[d] += 1
        if r["stack_count"] >= 2:
            combo = tuple(sorted(r["patterns"]))
            combo_counts[combo] += 1

    # warm tier high-confidence: warm + at least one of (demolition,
    # absentee, multi-juris delinquency)
    high_conf_warm = sum(
        1 for r in records
        if r["tier"] == TIER_WARM and (
            "demolition_permit" in r["flags"]
            or r.get("is_absentee")
            or "multi_juris_delinquent" in r["flags"]
            or "imminent_tax_sale" in r["flags"]
        )
    )
    warm_total = tier_counts.get(TIER_WARM, 0)
    high_conf_pct = round(high_conf_warm * 100.0 / warm_total, 1) if warm_total else 0.0

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": _git_short_sha(),
        "county": "New Hanover",
        "state": "NC",
        "total": len(records),
        "tier_counts": dict(tier_counts),
        "pattern_counts": dict(pattern_counts),
        "source_attach_counts": source_counts,
        "doc_type_counts": dict(doc_type_counts),
        "transfer_rule_counts": transfer_rule_counts,
        "warm_tier_high_confidence_pct": high_conf_pct,
        "top_pattern_combos": [[list(c), n] for c, n in combo_counts.most_common(10)],
        "patterns_legend": {
            "jfc": "Judicial Foreclosure (Power of Sale + ROD foreclosure deeds)",
            "tax": "Tax distress (delinquent CSV + GS 105-374 schedule)",
            "estate": "Probate / Estate (StarNews NTC + ROD estate deeds)",
            "code": "Code / Demolition (EnerGov demolition permits — NH gap doc'd)",
            "lien": "Recorded Lien / Civil Judgment (ROD JDGMT + LIEN + LIS PENS)",
            "transfer": "Distressed Conveyance (QCD + nominal sale + post-estate)",
        },
        "tier_rules": {
            "hot": "stack_count >= 3",
            "warm": "stack_count == 2",
            "active": "stack_count == 1",
        },
        "records": records,
    }

    # Two-Truths invariant
    two_truths_check(records, payload["tier_counts"], payload["pattern_counts"])

    # Rotate previous → leads.previous.json
    if OUT_PATH.exists():
        OUT_PATH.replace(PREV_PATH)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, default=str), encoding="utf-8")
    size_mb = OUT_PATH.stat().st_size / 1024 / 1024
    log(f"\n[output] wrote {len(records):,} leads to {OUT_PATH} ({size_mb:.2f} MB)")
    log(f"[output] tier_counts:    {dict(tier_counts)}")
    log(f"[output] pattern_counts: {dict(pattern_counts)}")
    log(f"[output] high_conf_warm: {high_conf_warm} / {warm_total} ({high_conf_pct}%)")
    log(f"[output] top combos:     {payload['top_pattern_combos'][:3]}")
    if size_mb > 50:
        log(f"[!] WARNING: leads.json exceeds 50MB GitHub cap")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip writing leads.json; print the summary only.")
    args = ap.parse_args()

    def log(msg: str) -> None:
        print(msg, flush=True)

    by_pid, by_addr, by_owner = load_parcel_master(PARCEL_PATH, log)
    if not by_pid:
        return 1

    leads, source_counts, transfer_rule_counts = join_signals(by_pid, by_addr, by_owner, log)
    log(f"[join] {len(leads):,} parcels with at least one signal attached")

    if args.dry_run:
        log("[dry-run] skipping write")
        return 0

    write_output(leads, by_pid, source_counts, transfer_rule_counts, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
