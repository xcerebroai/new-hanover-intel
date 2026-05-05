"""build_leads.py — New Hanover County motivated-seller lead pipeline.

Tag-taxonomy build. Replaces the prior 6-pattern array on each lead with a
``tags[]`` array of operator-language strings. Tier comes from DISTRESS
TAG count only — owner-profile and derived tags are filters, not tier
contributors.

Output schema (top-level):

    { "header": {...}, "records": [...] }

Two-Truths invariant: ``header.tag_counts`` and ``header.tier_counts`` are
recomputed from ``records[]`` immediately before write. Mismatch raises and
exits non-zero — no file is written, no rotation occurs.

Diff invariant: ``new_count + newly_tagged_count + existing_count`` must
equal ``total_records``. First run after schema change archives the prior
``leads.previous.json`` (if it has the old "patterns" schema) and stamps
every record ``_diff_status = "existing"`` as a baseline.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
ARCHIVE_DIR = RAW_DIR / "archive"
OUT_PATH = PROJECT_ROOT / "data" / "leads.json"
PREV_PATH = PROJECT_ROOT / "data" / "leads.previous.json"
TAG_AUDIT_PATH = RAW_DIR / "tag_audit.json"

PARCEL_PATH = RAW_DIR / "property_owners_layer0.jsonl"
DELQ_PATH = RAW_DIR / "delinquent_tax.jsonl"
FCL_HTML_PATH = RAW_DIR / "nhc_foreclosures.jsonl"
ENERGOV_DEMO_PATH = RAW_DIR / "energov_permits_demolition.jsonl"
ENERGOV_FLOOD_PATH = RAW_DIR / "energov_permits_floodplain_development.jsonl"
ENERGOV_OCC_PATH = RAW_DIR / "energov_permits_occupancy_certification.jsonl"
STORMWATER_PATH = RAW_DIR / "nhc_stormwater.jsonl"
STARNEWS_NTC_PATH = RAW_DIR / "starnews_notice_to_creditors.jsonl"
STARNEWS_FCL_PATH = RAW_DIR / "starnews_foreclosures.jsonl"
ROD_FORECLOSURE_PATH = RAW_DIR / "rod_foreclosure.jsonl"
ROD_ESTATE_DEED_PATH = RAW_DIR / "rod_estate_deed.jsonl"
ROD_JUDGMENT_PATH = RAW_DIR / "rod_judgment.jsonl"
ROD_LIEN_PATH = RAW_DIR / "rod_lien.jsonl"
ROD_QUITCLAIM_PATH = RAW_DIR / "rod_quitclaim.jsonl"
ROD_DOT_PATH = RAW_DIR / "rod_deed_of_trust.jsonl"

# ---------- Tag taxonomy ----------
DISTRESS_TAGS = [
    "Foreclosure",
    "Tax Foreclosure",
    "Sheriff Sale",
    "Tax Delinquency",
    "Mechanics Lien",
    "Judgment",
    "Lis Pendens",
    "Estate / Probate",
    "Demolition Order",
    "Quitclaim",
    "Stormwater Issue",
]
OWNER_TAGS = [
    "Absentee Owner",
    "Out-of-State Owner",
    "Long-Term Ownership",
    "Free & Clear",
    "Senior Owner",
]
DERIVED_TAGS = ["Distressed Transfer", "Post-Estate Sale"]
ALL_TAGS = DISTRESS_TAGS + OWNER_TAGS + DERIVED_TAGS

TIER_HOT = "hot"
TIER_WARM = "warm"
TIER_ACTIVE = "active"

DELINQUENCY_MIN_DUE = 500.0
DISTRESSED_TRANSFER_DAYS = 730
POST_ESTATE_DAYS = 548  # 18 months
LONG_TERM_OWNERSHIP_YRS = 20

# Doc-code matchers (normalized: uppercase, punctuation removed, whitespace
# collapsed). Per spec — exact list, no improvisation.
MECHANICS_LIEN_CODES = {"ML", "MECH", "MECHANICSLIEN", "MECHANICLIEN"}
LIS_PENDENS_CODES = {"LP", "LISPEN", "LISPENDENS"}

# Estate/Probate ROD doc-types we accept. TRUSTEES DEED and SHERIFF DEED are
# court-ordered foreclosure-context deeds and are NOT decedent estate
# instruments — exclude them from this tag.
ESTATE_DEED_CODES = {
    "ADMINDEED", "ADMINISTRATORSDEED", "ADMINISTRATORDEED",
    "EXECDEED", "EXECUTORDEED", "EXECUTORSDEED",
    "EXTRXDEED", "EXECUTRIXDEED",
}

# Body-content keywords that have to appear in a starnews_foreclosures
# notice for it to count as a foreclosure. The starnews "foreclosures-
# sheriff-sales" category at Gannett occasionally cross-posts unrelated
# notices (NTC, CAMA permits, etc.) — content-filter to avoid misemission.
FORECLOSURE_BODY_KW = (
    "FORECLOSURE", "TRUSTEE", "TRUSTEES SALE", "TRUSTEE SALE",
    "NOTICE OF SALE", "SUBSTITUTE TRUSTEE", "POWER OF SALE",
    "SHERIFF SALE", "SHERIFF'S SALE", "SHERIFFS SALE",
)

NUM_RE = re.compile(r"^\s*(\d+)")
NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]")
ADDR_FROM_BODY_RE = re.compile(
    r"\b(\d{2,5}\s+[A-Z][A-Z0-9 .'-]+?(?:STREET|ST|AVENUE|AVE|ROAD|RD|"
    r"DRIVE|DR|LANE|LN|COURT|CT|CIRCLE|CIR|PLACE|PL|BOULEVARD|BLVD|"
    r"TRAIL|TRL|WAY|PARKWAY|PKWY|TERRACE|TER|HIGHWAY|HWY))\b"
)

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
NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}

# US 2-letter state abbreviations (excluded territories DC + PR included
# because they appear in mailing addresses).
US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC","PR",
}


# ---------- Normalization helpers ----------

def norm_doc_code(code: str) -> str:
    """Uppercase + strip punctuation + collapse whitespace.

    e.g. 'M/L' -> 'ML', 'LIS PENDENS' -> 'LISPENDENS'.
    """
    if not code:
        return ""
    s = code.upper()
    s = re.sub(r"[^A-Z0-9]", "", s)
    return s


def normalize_street(street_full: str) -> tuple[str, str]:
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
    """NHC OWN1 packs the full name into one field, typically
    "LASTNAME FIRSTNAME [MIDDLE] [SUFFIX]" for individuals.
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
    last = parts[0]
    first = parts[1] if len(parts) >= 2 else ""
    middle = " ".join(parts[2:]) if len(parts) >= 3 else ""
    return {"first_name": first, "middle_name": middle, "last_name": last,
            "suffix": suffix, "is_entity": False, "full_name": full}


def _f(v) -> float | None:
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


def _parse_iso_or_date(s: str) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


# ---------- Loaders ----------

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


def load_parcel_master(path: Path, log) -> tuple[dict, dict]:
    """Returns (by_pid, by_addr_key)."""
    by_pid: dict[str, dict] = {}
    by_addr: dict[tuple[str, str], list[str]] = defaultdict(list)
    log(f"[parcels] reading {path.name}...")
    if not path.exists():
        log(f"[!] parcel master missing: {path} — pipeline cannot proceed")
        return {}, {}
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
            absentee = bool(
                site_name and mail_name and (
                    site_name != mail_name or site_num != mail_num
                )
            )
            sale_dt = _parse_sale_date(r.get("SALE_DATE"))

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
                "sale_price": _f(r.get("SALE_PRICE")),
                "sale_instrument": (r.get("SALE_INSTRUMENT") or "").strip(),
                "sale_book": (r.get("SALE_BOOK") or "").strip(),
                "sale_page": (r.get("SALE_PAGE") or "").strip(),
                "apr_total": _f(r.get("APRTOT")),
                "apr_land": _f(r.get("APRLAND")),
                "apr_bldg": _f(r.get("APRBLDG")),
                "apr_taxyr": _i(r.get("APRVAL_TAXYR")),
            }
            by_pid[pid] = entry
            if site_num and site_name:
                by_addr[(site_num, site_name)].append(pid)
            n += 1
    log(f"[parcels] indexed {n:,} parcels  addr_keys={len(by_addr):,}")
    return by_pid, dict(by_addr)


def evaluate_senior_coverage(path: Path, log) -> bool:
    """Return True only if a senior/elderly exemption field exists with > 5%
    coverage. Otherwise log + return False so the Senior Owner tag is
    globally suppressed.
    """
    if not path.exists():
        return False
    n = 0
    populated = 0
    senior_field_seen = False
    candidates = ("EXEMPTION", "EXEMPTIONS", "EXEMPT_CODE", "EXEMPT", "SENIOR",
                  "ELDERLY", "AGE_EXEMPTION", "EXEMPT_TYPE")
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            n += 1
            for k in r:
                ku = k.upper()
                if any(c in ku for c in ("EXEMPT", "SENIOR", "ELDER")):
                    senior_field_seen = True
                    v = str(r[k] or "").upper()
                    if v and ("ELDER" in v or "SENIOR" in v or "OVER 65" in v
                              or "AGE" in v):
                        populated += 1
            if n >= 5000:
                break
    if not senior_field_seen:
        log("[senior] no senior/elderly field detected in PropertyOwners -- "
            "Senior Owner tag GLOBALLY SUPPRESSED")
        return False
    coverage = populated / n if n else 0.0
    if coverage <= 0.05:
        log(f"[senior] field coverage = {coverage*100:.2f}% (<=5%) -- "
            "Senior Owner tag GLOBALLY SUPPRESSED")
        return False
    log(f"[senior] field coverage = {coverage*100:.2f}% -- tag enabled")
    return True


# ---------- Tag emission ----------

def attach_tag(lead: dict, tag: str, signal: dict) -> None:
    if tag not in lead["tags"]:
        lead["tags"].append(tag)
    lead["signals"].setdefault(tag, []).append(signal)


def _get_lead(leads: dict, pid: str) -> dict:
    if pid not in leads:
        leads[pid] = {
            "pid": pid,
            "tags": [],
            "signals": {},
        }
    return leads[pid]


def emit_tags(by_pid: dict, by_addr: dict, senior_enabled: bool, log
              ) -> tuple[dict, dict, dict]:
    """Walk every signal source and emit tags per the deterministic spec
    table. Returns (leads, source_signal_counts, source_files_per_tag).

    Joins are STRICT per spec:
        1. Exact PID match preferred
        2. Address-key match if PID missing
        3. Owner/grantor-name-alone matches are DROPPED
    """
    leads: dict[str, dict] = {}
    source_signal_counts: Counter = Counter()
    sources_per_tag: dict[str, set[str]] = defaultdict(set)

    # ---- Tax Delinquency (delinquent_tax → PID) ----
    delq_attached = delq_filtered = delq_unmatched = 0
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
        for r in rows:
            attach_tag(lead, "Tax Delinquency", {
                "tag": "Tax Delinquency",
                "source": "nhc_delinquent_csv",
                "doc_type": "DELQ",
                "name": r.get("name"),
                "juris_code": r.get("juris_code"),
                "juris_description": r.get("juris_description"),
                "total_due": r.get("total_due"),
                "last_payment_date": r.get("last_payment_date"),
                "address": r.get("location_street"),
                "date": r.get("last_payment_date") or "",
                "join": "exact_pid",
            })
        source_signal_counts["Tax Delinquency"] += len(rows)
        sources_per_tag["Tax Delinquency"].add("delinquent_tax.jsonl")
    log(f"[Tax Delinquency] attached={delq_attached}  "
        f"unmatched_pid={delq_unmatched}  under_min={delq_filtered}")

    # ---- Tax Foreclosure (nhc_foreclosures → PID) ----
    fcl_attached = 0
    for r in load_jsonl(FCL_HTML_PATH):
        pid = (r.get("parcel") or "").strip()
        if not pid or pid not in by_pid:
            continue
        lead = _get_lead(leads, pid)
        attach_tag(lead, "Tax Foreclosure", {
            "tag": "Tax Foreclosure",
            "source": "nhc_foreclosures_html",
            "doc_type": "TAX_FC",
            "case_number": r.get("case_number"),
            "sale_date": r.get("sale_date"),
            "sale_time": r.get("sale_time"),
            "sale_location": r.get("sale_location"),
            "address": r.get("street_address"),
            "statute": r.get("statute"),
            "date": r.get("sale_date") or "",
            "join": "exact_pid",
        })
        source_signal_counts["Tax Foreclosure"] += 1
        fcl_attached += 1
    if fcl_attached:
        sources_per_tag["Tax Foreclosure"].add("nhc_foreclosures.jsonl")
    log(f"[Tax Foreclosure] attached={fcl_attached}")

    # ---- Demolition Order (energov_permits_demolition → PID) ----
    demo_attached = demo_unmatched = 0
    for r in load_jsonl(ENERGOV_DEMO_PATH):
        permit_type_norm = norm_doc_code(r.get("PERMIT_TYPE") or "")
        if "DEMO" not in permit_type_norm:
            continue
        pid = (r.get("PID") or "").strip()
        if not pid or pid not in by_pid:
            demo_unmatched += 1
            continue
        lead = _get_lead(leads, pid)
        attach_tag(lead, "Demolition Order", {
            "tag": "Demolition Order",
            "source": "nhc_energov_permits",
            "doc_type": "DEMO",
            "permit_number": r.get("PERMIT_NUMBER"),
            "permit_type": r.get("PERMIT_TYPE"),
            "work_class": r.get("WORK_CLASS"),
            "permit_status": r.get("PERMIT_STATUS"),
            "address": (r.get("STREET") or "").strip(),
            "date": _ms_iso(r.get("ISSUE_DATE")),
            "join": "exact_pid",
        })
        source_signal_counts["Demolition Order"] += 1
        demo_attached += 1
    if demo_attached:
        sources_per_tag["Demolition Order"].add("energov_permits_demolition.jsonl")
    log(f"[Demolition Order] attached={demo_attached}  unmatched_pid={demo_unmatched}")

    # ---- Stormwater Issue (nhc_stormwater → PID) ----
    sw_attached = 0
    for r in load_jsonl(STORMWATER_PATH):
        pid = (r.get("PID") or "").strip()
        if not pid or pid not in by_pid:
            continue
        lead = _get_lead(leads, pid)
        attach_tag(lead, "Stormwater Issue", {
            "tag": "Stormwater Issue",
            "source": "nhc_stormwater",
            "project": r.get("PROJECT"),
            "address": r.get("ADDRESS"),
            "status": r.get("STATUS"),
            "owner": r.get("OWNER"),
            "fees": r.get("FEES"),
            "date": _ms_iso(r.get("ISSUEDATE")),
            "join": "exact_pid",
        })
        source_signal_counts["Stormwater Issue"] += 1
        sw_attached += 1
    if sw_attached:
        sources_per_tag["Stormwater Issue"].add("nhc_stormwater.jsonl")
    log(f"[Stormwater Issue] attached={sw_attached}")

    # ---- Foreclosure + Sheriff Sale (starnews_foreclosures → addr-key) ----
    fcl_starnews_attached = sheriff_attached = sn_skipped_unrelated = 0
    sn_skipped_addr = 0
    for r in load_jsonl(STARNEWS_FCL_PATH):
        if not r.get("is_new_hanover"):
            continue
        body = r.get("body") or ""
        body_up = body.upper()
        # Content filter — Gannett's foreclosures-sheriff-sales category
        # cross-posts unrelated notices (NTC, CAMA permits).
        if not any(kw in body_up for kw in FORECLOSURE_BODY_KW):
            sn_skipped_unrelated += 1
            continue
        # Address join — extract candidate addresses from body, attempt
        # exact addr-key match. Only attach if exactly ONE PID matches
        # (per spec: ambiguous = drop).
        candidate_pids: set[str] = set()
        for m in ADDR_FROM_BODY_RE.finditer(body_up):
            num, name = normalize_street(m.group(0))
            if num and name:
                candidate_pids.update(by_addr.get((num, name), []))
        if len(candidate_pids) != 1:
            sn_skipped_addr += 1
            continue
        pid = next(iter(candidate_pids))
        is_sheriff = "SHERIFF" in body_up or "SHERIFF" in (r.get("title") or "").upper()

        lead = _get_lead(leads, pid)
        sig_payload = {
            "source": "starnews_foreclosures",
            "case_number": r.get("case_number"),
            "posted_date": r.get("posted_date"),
            "detail_url": r.get("detail_url"),
            "body": body[:400],
            "date": r.get("posted_date") or "",
            "join": "addr_exact",
        }
        attach_tag(lead, "Foreclosure", {**sig_payload, "tag": "Foreclosure"})
        source_signal_counts["Foreclosure"] += 1
        sources_per_tag["Foreclosure"].add("starnews_foreclosures.jsonl")
        fcl_starnews_attached += 1
        if is_sheriff:
            attach_tag(lead, "Sheriff Sale", {**sig_payload, "tag": "Sheriff Sale"})
            source_signal_counts["Sheriff Sale"] += 1
            sources_per_tag["Sheriff Sale"].add("starnews_foreclosures.jsonl")
            sheriff_attached += 1
    log(f"[Foreclosure/Sheriff] starnews fcl={fcl_starnews_attached}  "
        f"sheriff={sheriff_attached}  skipped_unrelated={sn_skipped_unrelated}  "
        f"skipped_addr_ambiguous={sn_skipped_addr}")

    # ---- Foreclosure (rod_foreclosure → ROD has no PID/address) ----
    # NHC Register of Deeds records carry no parcel ID and the description
    # field is subdivision+lot+block, not a street address. Per join safety
    # rules (name-alone is never enough), ROD signals are NOT attached
    # unless a stronger join is available. We log the row count and SKIP.
    rod_foreclosure_rows = load_jsonl(ROD_FORECLOSURE_PATH)
    if rod_foreclosure_rows:
        log(f"[rod_foreclosure] {len(rod_foreclosure_rows)} rows -- "
            f"NOT attached (no PID/address join available, owner-name alone "
            f"insufficient per join safety rules)")

    # ---- Estate / Probate ----
    # Sources:
    #   1. starnews_notice_to_creditors — decedent name match → owner-name
    #      alone, FAILS join safety. SKIP.
    #   2. rod_estate_deed — has TRUSTEES DEED rows (foreclosure post-sale,
    #      not probate) plus actual ADMIN/EXEC/EXTRX deeds. Even the real
    #      probate ones are name-alone. SKIP.
    ntc_rows = load_jsonl(STARNEWS_NTC_PATH)
    nhc_only = [r for r in ntc_rows if r.get("is_new_hanover")]
    if nhc_only:
        log(f"[Estate/Probate:NTC] {len(nhc_only)} NHC notice-to-creditors "
            f"rows -- NOT attached (decedent -> owner-name match alone, "
            f"insufficient per join safety rules)")
    rod_estate = load_jsonl(ROD_ESTATE_DEED_PATH)
    real_probate = sum(
        1 for r in rod_estate
        if norm_doc_code(r.get("doc_type_label") or "") in ESTATE_DEED_CODES
    )
    log(f"[Estate/Probate:ROD] {len(rod_estate)} rows total, "
        f"{real_probate} actual probate deeds (rest are TRUSTEES DEED "
        f"foreclosure-context) -- NOT attached (no PID/address join)")

    # ---- Mechanics Lien / Lis Pendens / Judgment / Quitclaim ----
    rod_lien = load_jsonl(ROD_LIEN_PATH)
    rod_judgment = load_jsonl(ROD_JUDGMENT_PATH)
    rod_quitclaim = load_jsonl(ROD_QUITCLAIM_PATH)
    log(f"[Mechanics Lien/Lis Pendens] rod_lien.jsonl: {len(rod_lien)} rows "
        f"-- NOT attached (no PID/address join)")
    log(f"[Judgment] rod_judgment.jsonl: {len(rod_judgment)} rows "
        f"-- NOT attached (governmental reverse-party + name-alone)")
    log(f"[Quitclaim] rod_quitclaim.jsonl: {len(rod_quitclaim)} rows "
        f"-- NOT attached (no file or no PID/address join)")

    # ---- Owner-profile tags ----
    # Computed per parcel from PropertyOwners. Only emitted on parcels
    # already in `leads` (i.e. that have at least one distress signal),
    # since we don't surface non-distress parcels.
    abs_count = oos_count = lto_count = sr_count = 0
    for pid, lead in leads.items():
        parcel = by_pid.get(pid)
        if not parcel:
            continue
        # Absentee Owner — both addresses normalized + comparable
        site_num, site_name = normalize_street(parcel.get("site_address") or "")
        mail_num, mail_name = normalize_street(parcel.get("mail_address") or "")
        site_city_norm = (parcel.get("site_city") or "").upper().strip()
        mail_city_norm = (parcel.get("mail_city") or "").upper().strip()
        if site_num and site_name and mail_num and mail_name:
            site_line = f"{site_num} {site_name} {site_city_norm}".strip()
            mail_line = f"{mail_num} {mail_name} {mail_city_norm}".strip()
            if site_line != mail_line:
                attach_tag(lead, "Absentee Owner", {
                    "tag": "Absentee Owner",
                    "site_addr": parcel.get("site_address"),
                    "site_city": parcel.get("site_city"),
                    "mail_addr": parcel.get("mail_address"),
                    "mail_city": parcel.get("mail_city"),
                    "mail_state": parcel.get("mail_state"),
                    "join": "self",
                })
                source_signal_counts["Absentee Owner"] += 1
                sources_per_tag["Absentee Owner"].add("property_owners_layer0.jsonl")
                abs_count += 1
        # Out-of-State Owner — valid 2-letter state and != NC
        ms = (parcel.get("mail_state") or "").upper().strip()
        if ms in US_STATES and ms != "NC":
            attach_tag(lead, "Out-of-State Owner", {
                "tag": "Out-of-State Owner",
                "mail_state": ms,
                "mail_city": parcel.get("mail_city"),
                "join": "self",
            })
            source_signal_counts["Out-of-State Owner"] += 1
            sources_per_tag["Out-of-State Owner"].add("property_owners_layer0.jsonl")
            oos_count += 1
        # Long-Term Ownership — years_owned >= 20 derived from sale_date
        sale_iso = parcel.get("sale_date_iso") or ""
        sale_dt = _parse_iso_or_date(sale_iso) if sale_iso else None
        if sale_dt:
            years = (datetime.now(timezone.utc) - sale_dt).days / 365.25
            if years >= LONG_TERM_OWNERSHIP_YRS:
                attach_tag(lead, "Long-Term Ownership", {
                    "tag": "Long-Term Ownership",
                    "sale_date": sale_iso,
                    "years_owned": round(years, 1),
                    "join": "self",
                })
                source_signal_counts["Long-Term Ownership"] += 1
                sources_per_tag["Long-Term Ownership"].add("property_owners_layer0.jsonl")
                lto_count += 1
        # Senior Owner — globally suppressed (no field coverage)
        if senior_enabled:
            # Only enable if evaluate_senior_coverage returned True
            pass  # would emit here based on parcel field; suppressed by default
    log(f"[owner-tags] absentee={abs_count} out_of_state={oos_count} "
        f"long_term={lto_count} senior={sr_count}")

    # Free & Clear suppressed: requires rod_deed_of_trust.jsonl which is
    # not present in the repo. Per spec: don't infer payoff status from
    # absent data.
    if not ROD_DOT_PATH.exists():
        log("[Free & Clear] rod_deed_of_trust.jsonl absent -- tag SUPPRESSED")

    # ---- Derived: Distressed Transfer + Post-Estate Sale ----
    distressed_xfer = post_estate = 0
    now = datetime.now(timezone.utc)
    for pid, lead in leads.items():
        parcel = by_pid.get(pid)
        if not parcel:
            continue
        sale_iso = parcel.get("sale_date_iso") or ""
        sale_dt = _parse_iso_or_date(sale_iso) if sale_iso else None
        sp = parcel.get("sale_price")
        tv = parcel.get("apr_total")
        if sale_dt and sp is not None:
            recent = (now - sale_dt).days <= DISTRESSED_TRANSFER_DAYS
            if recent and (
                (sp < 1000) or (tv and tv > 0 and (sp / tv) < 0.05)
            ):
                attach_tag(lead, "Distressed Transfer", {
                    "tag": "Distressed Transfer",
                    "sale_date": sale_iso,
                    "sale_price": sp,
                    "total_market_value": tv,
                    "join": "self",
                })
                source_signal_counts["Distressed Transfer"] += 1
                sources_per_tag["Distressed Transfer"].add("property_owners_layer0.jsonl")
                distressed_xfer += 1

        # Post-Estate Sale requires the Estate / Probate tag to be present.
        # Since Estate / Probate is not attached anywhere in this build
        # (per join safety), Post-Estate Sale will not fire.
        if "Estate / Probate" in lead["tags"] and sale_dt:
            estate_signals = lead["signals"].get("Estate / Probate") or []
            earliest_estate = None
            for s in estate_signals:
                est_str = s.get("posted_date") or s.get("recorded_date") or ""
                est_dt = _parse_iso_or_date(est_str) if est_str else None
                if est_dt and (earliest_estate is None or est_dt < earliest_estate):
                    earliest_estate = est_dt
            if earliest_estate:
                delta = (sale_dt - earliest_estate).days
                if 0 <= delta <= POST_ESTATE_DAYS:
                    attach_tag(lead, "Post-Estate Sale", {
                        "tag": "Post-Estate Sale",
                        "sale_date": sale_iso,
                        "earliest_estate": earliest_estate.isoformat(),
                        "join": "derived",
                    })
                    source_signal_counts["Post-Estate Sale"] += 1
                    sources_per_tag["Post-Estate Sale"].add("derived")
                    post_estate += 1
    log(f"[derived] distressed_transfer={distressed_xfer} post_estate={post_estate}")

    return leads, dict(source_signal_counts), dict(sources_per_tag)


def _ms_iso(v) -> str:
    dt = _ms_to_dt(v)
    return dt.isoformat() if dt else ""


# ---------- Tier + scoring ----------

def assign_tier(tags: list[str]) -> str:
    distress_count = sum(1 for t in tags if t in DISTRESS_TAGS)
    if distress_count >= 3:
        return TIER_HOT
    if distress_count == 2:
        return TIER_WARM
    if distress_count == 1:
        return TIER_ACTIVE
    return ""  # zero distress → drop


def build_lead_record(pid: str, parcel: dict, lead: dict) -> dict:
    tags = sorted(lead["tags"])
    distress_tags = [t for t in tags if t in DISTRESS_TAGS]
    tier = assign_tier(tags)
    parsed_owner = parse_owner_name(parcel.get("own1", ""))
    sale_iso = parcel.get("sale_date_iso") or ""
    sale_dt = _parse_iso_or_date(sale_iso) if sale_iso else None
    years_owned = None
    if sale_dt:
        years_owned = round((datetime.now(timezone.utc) - sale_dt).days / 365.25, 1)
    etax_url = (
        f"https://etax.nhcgov.com/pt/Datalets/Datalet.aspx?UseSearch=no"
        f"&pin={pid}&jur=NH&taxyr=2025"
    )
    return {
        "pid": pid,
        "tier": tier,
        "tags": tags,
        "distress_tag_count": len(distress_tags),
        "_diff_status": "existing",  # filled in later
        "owner_name": parcel.get("own1") or "",
        "owner_parsed": parsed_owner,
        "is_entity": parsed_owner.get("is_entity", False),
        "address": parcel.get("site_address") or "",
        "city": parcel.get("site_city") or "",
        "muni": parcel.get("muni") or "",
        "zoning": parcel.get("zoning") or "",
        "land_use_code": parcel.get("land_use_code") or "",
        "class_code": parcel.get("class_code") or "",
        "subdiv": parcel.get("subdiv") or "",
        "legal1": parcel.get("legal1") or "",
        "mail_address": parcel.get("mail_address") or "",
        "mail_city": parcel.get("mail_city") or "",
        "mail_state": parcel.get("mail_state") or "",
        "mail_zip": parcel.get("mail_zip") or "",
        "sfla": parcel.get("sfla"),
        "total_market_value": parcel.get("apr_total"),
        "apr_total": parcel.get("apr_total"),
        "apr_land": parcel.get("apr_land"),
        "apr_bldg": parcel.get("apr_bldg"),
        "apr_taxyr": parcel.get("apr_taxyr"),
        "sale_date": sale_iso,
        "sale_price": parcel.get("sale_price"),
        "sale_book": parcel.get("sale_book") or "",
        "sale_page": parcel.get("sale_page") or "",
        "sale_instrument": parcel.get("sale_instrument") or "",
        "years_owned": years_owned,
        "mapidkey": parcel.get("mapidkey") or "",
        "mapid": parcel.get("mapid") or "",
        "etax_url": etax_url,
        "signals": lead["signals"],
        "last_update": "",  # pipeline doesn't track per-lead update timestamps yet
    }


# ---------- Diff ----------

def is_old_or_missing_schema(prev_payload) -> bool:
    """Return True if the previous file is absent, malformed, or uses the
    pre-tag-taxonomy schema (records carrying ``patterns`` instead of
    ``tags``).
    """
    if not prev_payload:
        return True
    records = None
    if isinstance(prev_payload, dict):
        records = prev_payload.get("records")
        # New-schema header lives under ``header``; old-schema fields are
        # spread at top level. Either way, records[] is what matters.
    if not isinstance(records, list):
        return True
    if not records:
        return False  # empty record set is valid; baseline of 0 → 0
    sample = records[0]
    return "tags" not in sample


def compute_diff(records: list[dict], prev_records: list[dict] | None,
                 baseline: bool, log) -> tuple[int, int, int]:
    if baseline:
        for r in records:
            r["_diff_status"] = "existing"
        log(f"[diff] baseline established — {len(records)} records → 'existing'")
        return 0, 0, len(records)

    prev_by_pid: dict[str, dict] = {r["pid"]: r for r in (prev_records or [])
                                    if r.get("pid")}
    new_count = newly_tagged = existing = 0
    for r in records:
        pid = r["pid"]
        prev = prev_by_pid.get(pid)
        if prev is None:
            r["_diff_status"] = "new"
            new_count += 1
        else:
            prev_tags = sorted(prev.get("tags", []))
            cur_tags = sorted(r.get("tags", []))
            if prev_tags != cur_tags:
                r["_diff_status"] = "newly_tagged"
                newly_tagged += 1
            else:
                r["_diff_status"] = "existing"
                existing += 1
    log(f"[diff] new={new_count}  newly_tagged={newly_tagged}  existing={existing}")
    return new_count, newly_tagged, existing


# ---------- Output ----------

def two_truths_check(records: list[dict], header: dict) -> None:
    derived_tier: Counter = Counter()
    derived_tag: Counter = Counter()
    for r in records:
        derived_tier[r["tier"]] += 1
        for t in r["tags"]:
            derived_tag[t] += 1
    for k in (TIER_HOT, TIER_WARM, TIER_ACTIVE):
        if header["tier_counts"].get(k, 0) != derived_tier.get(k, 0):
            raise RuntimeError(
                f"Two-Truths violation: header.tier_counts[{k}]="
                f"{header['tier_counts'].get(k, 0)} != records-derived "
                f"{derived_tier.get(k, 0)}"
            )
    for t in header["tag_counts"]:
        if header["tag_counts"][t] != derived_tag.get(t, 0):
            raise RuntimeError(
                f"Two-Truths violation: header.tag_counts[{t}]="
                f"{header['tag_counts'][t]} != records-derived "
                f"{derived_tag.get(t, 0)}"
            )
    n = len(records)
    sumdiff = (header["new_count"] + header["newly_tagged_count"]
               + header["existing_count"])
    if sumdiff != n:
        raise RuntimeError(
            f"Diff invariant violation: new+newly_tagged+existing={sumdiff} "
            f"!= total_records={n}"
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


def write_tag_audit(records: list[dict], source_signal_counts: dict,
                     sources_per_tag: dict, suppressed: list[str],
                     total_records: int) -> None:
    import random
    by_tag: dict[str, list[str]] = defaultdict(list)
    for r in records:
        for t in r["tags"]:
            by_tag[t].append(r["pid"])
    audit = {"total_records": total_records, "tags": []}
    for tag in ALL_TAGS:
        leads_with = by_tag.get(tag, [])
        if not leads_with and tag not in suppressed:
            audit["tags"].append({
                "tag": tag, "lead_count": 0, "samples": [],
                "source_signal_count": source_signal_counts.get(tag, 0),
                "source_files": sorted(sources_per_tag.get(tag, [])),
                "status": "zero_count_dropped_from_render",
            })
            continue
        if tag in suppressed:
            audit["tags"].append({
                "tag": tag, "lead_count": 0, "samples": [],
                "source_signal_count": 0,
                "source_files": [],
                "status": "globally_suppressed",
            })
            continue
        n = len(leads_with)
        samples = random.sample(leads_with, min(5, n)) if n else []
        warn = (n / total_records) > 0.5 if total_records else False
        audit["tags"].append({
            "tag": tag, "lead_count": n, "samples": samples,
            "source_signal_count": source_signal_counts.get(tag, 0),
            "source_files": sorted(sources_per_tag.get(tag, [])),
            "warning": "fires on >50% of leads" if warn else None,
            "status": "active",
        })
    TAG_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    TAG_AUDIT_PATH.write_text(json.dumps(audit, indent=2, default=str),
                               encoding="utf-8")


def write_output(leads: dict, by_pid: dict, source_signal_counts: dict,
                 sources_per_tag: dict, suppressed: list[str], log) -> dict:
    # Build records — only those with at least one distress tag.
    records: list[dict] = []
    for pid, lead in leads.items():
        parcel = by_pid.get(pid, {})
        if not parcel:
            continue
        tier = assign_tier(lead["tags"])
        if not tier:
            continue
        rec = build_lead_record(pid, parcel, lead)
        records.append(rec)

    # Sort: tier rank desc, then distress tag count desc, then market value desc
    tier_rank = {TIER_HOT: 3, TIER_WARM: 2, TIER_ACTIVE: 1}
    records.sort(
        key=lambda r: (
            tier_rank.get(r["tier"], 0),
            r["distress_tag_count"],
            (r.get("apr_total") or 0),
        ),
        reverse=True,
    )

    # Diff against leads.previous.json
    prev_payload = None
    if PREV_PATH.exists():
        try:
            prev_payload = json.loads(PREV_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev_payload = None
    baseline = is_old_or_missing_schema(prev_payload)

    if baseline:
        # Archive old previous if it has the old schema
        if PREV_PATH.exists():
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            shutil.copy(PREV_PATH, ARCHIVE_DIR / f"leads.previous.{ts}.json")
            log(f"[archive] old leads.previous.json archived under data/raw/archive/")
        log("Diff baseline established.")
        prev_records = None
    else:
        prev_records = (prev_payload.get("records") if isinstance(prev_payload, dict) else None) or []
        log(f"[diff] previous file has {len(prev_records)} new-schema records")

    new_count, newly_tagged_count, existing_count = compute_diff(
        records, prev_records, baseline, log
    )

    # Two-Truths header
    tier_counts = Counter(r["tier"] for r in records)
    tag_counts: Counter = Counter()
    for r in records:
        for t in r["tags"]:
            tag_counts[t] += 1
    # Drop zero-count tags from header (per spec, zero-count tags are not rendered)
    tag_counts = {t: n for t, n in tag_counts.items() if n > 0}

    distress_attach_rates = {
        t: round(tag_counts.get(t, 0) * 100.0 / len(records), 2)
        for t in DISTRESS_TAGS if tag_counts.get(t, 0) > 0
    } if records else {}

    # Top combos — distinct distress-tag combinations only
    combo_counts: Counter = Counter()
    for r in records:
        distress = tuple(sorted(t for t in r["tags"] if t in DISTRESS_TAGS))
        if distress:
            combo_counts[distress] += 1

    header = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": _git_short_sha(),
        "county": "New Hanover",
        "state": "NC",
        "total_records": len(records),
        "tier_counts": dict(tier_counts),
        "tag_counts": tag_counts,
        "new_count": new_count,
        "newly_tagged_count": newly_tagged_count,
        "existing_count": existing_count,
        "distress_tag_attach_rates": distress_attach_rates,
        "top_tag_combos": [[list(c), n] for c, n in combo_counts.most_common(20)],
        "suppressed_tags": suppressed,
        "tier_rules": {
            "hot": "≥3 distress tags",
            "warm": "2 distress tags",
            "active": "1 distress tag",
            "dropped": "0 distress tags",
        },
        "tag_categories": {
            "distress": DISTRESS_TAGS,
            "owner": OWNER_TAGS,
            "derived": DERIVED_TAGS,
        },
    }

    # Two-Truths invariant — recompute and verify
    two_truths_check(records, header)

    # Tag audit (separate file, gitignored)
    write_tag_audit(records, source_signal_counts, sources_per_tag,
                    suppressed, len(records))

    # Write the new leads.json. Then snapshot it to leads.previous.json so
    # tomorrow's run can diff. This means PREV always holds the most recent
    # successful snapshot; after a baseline reset, today's data becomes
    # tomorrow's "previous" baseline so the next run does a real diff.
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"header": header, "records": records}
    OUT_PATH.write_text(json.dumps(payload, default=str), encoding="utf-8")
    shutil.copy(OUT_PATH, PREV_PATH)
    size_mb = OUT_PATH.stat().st_size / 1024 / 1024
    log(f"\n[output] wrote {len(records):,} records to {OUT_PATH} ({size_mb:.2f} MB)")
    log(f"[output] tier_counts: {dict(tier_counts)}")
    log(f"[output] tag_counts:  {tag_counts}")
    log(f"[output] diff: new={new_count} newly_tagged={newly_tagged_count} existing={existing_count}")
    log(f"[output] top combos (distress only): "
        f"{[(c, n) for c, n in combo_counts.most_common(5)]}")
    if size_mb > 50:
        log(f"[!] WARNING: leads.json exceeds 50MB GitHub cap")
    return payload


# ---------- Main ----------

def main() -> int:
    # Force UTF-8 stdout/stderr so unicode log strings (em dashes, arrows)
    # don't hit Windows cp1252 charmap errors. Python 3.7+ supports this.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Run join + tag emission, print summary, skip write.")
    args = ap.parse_args()

    def log(msg: str) -> None:
        print(msg, flush=True)

    by_pid, by_addr = load_parcel_master(PARCEL_PATH, log)
    if not by_pid:
        return 1

    senior_enabled = evaluate_senior_coverage(PARCEL_PATH, log)
    suppressed: list[str] = []
    if not senior_enabled:
        suppressed.append("Senior Owner")
    if not ROD_DOT_PATH.exists():
        suppressed.append("Free & Clear")

    leads, source_signal_counts, sources_per_tag = emit_tags(
        by_pid, by_addr, senior_enabled, log
    )
    log(f"[join] {len(leads):,} parcels with at least one signal attached")

    if args.dry_run:
        # Print summary only
        tag_counter: Counter = Counter()
        for lead in leads.values():
            for t in lead["tags"]:
                tag_counter[t] += 1
        log(f"\n[dry-run] tag counts: {dict(tag_counter)}")
        return 0

    write_output(leads, by_pid, source_signal_counts, sources_per_tag,
                 suppressed, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
