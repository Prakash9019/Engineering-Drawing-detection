#!/usr/bin/env python3
"""
step7_cedm_normalizer.py — CEDM Normalization Engine
======================================================
CDCI P&ID Pipeline — Step 7  (Blueprint Layer 14)

Type: PROGRAMMATIC — No Gemini, No Claude

What this does
--------------
Transforms raw extracted tags into the Common Engineering Data Model
(CEDM) canonical format required for the final output register.

Five normalisation operations:
  1. Tag Text Normaliser
     Strips separators, uppercases, zero-pads where needed.
     P - 101  →  P-101   |  FIT.1001  →  FIT-1001
     p101     →  P-101   |  V-BV-2246 stays V-BV-2246

  2. Description Standardiser
     Maps raw symbol_name strings to Engineering Ontology terms.
     "Ball Valve" → "VALVE,BALL" (matches Annexure-4 style)

  3. Discipline Classifier
     Derives discipline from tag prefix and symbol category.
     FIT/PT/TT → INSTRUMENTATION | BV/GV/XV → MECHANICAL | LINE → PIPING

  4. Canonical ID Generator
     SHA-256 hash of (project_id + canonical_tag) → 12-char canonical_id.
     Stable across re-runs; used as primary key in PostgreSQL.

  5. DOC STATUS Mapper
     Maps revision code → DOC STATUS string matching Annexure-4 format.
     "C" → "RE-ISSUED FOR CONSTRUCTION-SCOPE CLOUD"
     "A" → "APPROVED FOR CONSTRUCTION"

Inputs
------
  step5_final_output.json  OR  step5d_deduped.json
  drawing_context.json     (for doc metadata)
  title_block_context.json (for drawing_number, sheet, revision)

Output
------
  step7_cedm_output.json   — canonical records, feeds step8
  Each record has all 15 Annexure-4 fields populated.

Usage
-----
  python step7_cedm_normalizer.py \\
      --final output/step5_final_output.json \\
      --out   output/

  python step7_cedm_normalizer.py \\
      --context output/drawing_context.json
"""

import argparse
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── DOC STATUS lookup (revision code → status string) ─────────────────────────
DOC_STATUS_MAP: dict[str, str] = {
    "C":   "RE-ISSUED FOR CONSTRUCTION-SCOPE CLOUD",
    "B":   "RE-ISSUED FOR CONSTRUCTION",
    "A":   "APPROVED FOR CONSTRUCTION",
    "2":   "ISSUED FOR APPROVAL",
    "1":   "ISSUED FOR HAZOP",
    "0":   "ISSUED FOR COMMENTS",
    "IFC": "ISSUED FOR CONSTRUCTION",
    "IFA": "ISSUED FOR APPROVAL",
    "IFH": "ISSUED FOR HAZOP",
    "IFR": "ISSUED FOR REVIEW",
    "IFI": "ISSUED FOR INFORMATION",
    "AFC": "APPROVED FOR CONSTRUCTION",
    "P1":  "PRELIMINARY",
    "P2":  "PRELIMINARY REV 2",
    "3":   "RE-ISSUED FOR CONSTRUCTION",
    "4":   "RE-ISSUED FOR CONSTRUCTION",
    "D":   "DRAFT",
}

# ── Discipline lookup (tag prefix → discipline) ────────────────────────────────
_DISCIPLINE_MAP: list[tuple[set[str], str]] = [
    # Instrumentation
    ({"FIT","FT","FE","FG","FCV","FV","FY","FZT","FZSC","FZSO","FZY",
      "PT","PIT","PI","PS","PSV","PDI","DPIT",
      "TT","TIT","TE","TW","TG","TCV",
      "LT","LIT","LG","LS","LV","LI","LC","LY",
      "AT","AE","AI","CT","CP","CC","DIT",
      "ZIT","ZSC","ZSO","ZS","ZY","ZLC",
      "HS","HSO","HSC","SS","XV","XY","XS"},
     "INSTRUMENTATION"),
    # Mechanical valves
    ({"BV","GV","NRV","SDV","ESDV","RV","PSV","MOV","SV","PCV","FCV",
      "CV","QEV","WV","DBBV","PV","NV","HV"},
     "MECHANICAL"),
    # Electrical
    ({"M","KM","MOT"},
     "ELECTRICAL"),
    # Mechanical equipment
    ({"K","KG","V","E","P","S","TK","B","H","MX","EJ","FA","STR"},
     "MECHANICAL"),
    # Piping / line designations
    ({"LINE","ETH","GAS","OIL","WAT","STM","AIR","N2","H2","CO2"},
     "PIPING"),
]

# ── Engineering Ontology — symbol_name → CEDM description style ───────────────
_ONTOLOGY: list[tuple[re.Pattern, str, str]] = [
    # (pattern, EQUIPMENT DESCRIPTION style, TAG DESCRIPTION prefix)
    (re.compile(r"ball valve",         re.I), "VALVE,BALL",               "BV"),
    (re.compile(r"gate valve",         re.I), "VALVE,GATE",               "GV"),
    (re.compile(r"globe valve",        re.I), "VALVE,GLOBE",              "XV"),
    (re.compile(r"butterfly valve",    re.I), "VALVE,BUTTERFLY",          "XV"),
    (re.compile(r"needle valve",       re.I), "VALVE,NEEDLE",             "NV"),
    (re.compile(r"plug valve",         re.I), "VALVE,PLUG",               "PV"),
    (re.compile(r"non.?return",        re.I), "VALVE,CHECK",              "NRV"),
    (re.compile(r"check valve",        re.I), "VALVE,CHECK",              "NRV"),
    (re.compile(r"emergency shutdown", re.I), "VALVE,EMERGENCY SHUTDOWN", "ESDV"),
    (re.compile(r"shutdown valve",     re.I), "VALVE,SHUTDOWN",           "SDV"),
    (re.compile(r"on.?off valve",      re.I), "VALVE,ON/OFF",             "XV"),
    (re.compile(r"solenoid valve",     re.I), "VALVE,SOLENOID",           "SV"),
    (re.compile(r"motor operated",     re.I), "VALVE,MOTOR OPERATED",     "MOV"),
    (re.compile(r"flow control valve", re.I), "VALVE,CONTROL,FLOW",       "FCV"),
    (re.compile(r"pressure control",   re.I), "VALVE,CONTROL,PRESSURE",   "PCV"),
    (re.compile(r"temp.*control",      re.I), "VALVE,CONTROL,TEMPERATURE","TCV"),
    (re.compile(r"safety relief",      re.I), "VALVE,RELIEF",             "SRV"),
    (re.compile(r"pressure safety",    re.I), "VALVE,RELIEF,PRESSURE",    "PSV"),
    (re.compile(r"choke valve",        re.I), "VALVE,CHOKE",              "CV"),
    (re.compile(r"3.way solenoid",     re.I), "VALVE,SOLENOID,3-WAY",     "SV"),
    (re.compile(r"flow.*transmitter.*indicating|flow indicating.*transmit",re.I), "TRANSMITTER,FLOW,INDICATING", "FIT"),
    (re.compile(r"flow transmitter",   re.I), "TRANSMITTER,FLOW",         "FT"),
    (re.compile(r"pressure.*transmitter.*indicating|pressure indicating.*transmit",re.I),"TRANSMITTER,PRESSURE,INDICATING","PIT"),
    (re.compile(r"pressure transmitter",re.I),"TRANSMITTER,PRESSURE",     "PT"),
    (re.compile(r"temp.*transmitter.*indicating|temp.*indicating.*transmit",re.I),"TRANSMITTER,TEMPERATURE,INDICATING","TIT"),
    (re.compile(r"temp.*transmitter",  re.I), "TRANSMITTER,TEMPERATURE",  "TT"),
    (re.compile(r"level.*transmitter.*indicating|level.*indicating.*transmit",re.I),"TRANSMITTER,LEVEL,INDICATING","LIT"),
    (re.compile(r"level transmitter",  re.I), "TRANSMITTER,LEVEL",        "LT"),
    (re.compile(r"differential pressure.*indicating",re.I),"TRANSMITTER,DIFFERENTIAL PRESSURE,INDICATING","DPIT"),
    (re.compile(r"corrosion transmitter",re.I),"TRANSMITTER,CORROSION",   "CT"),
    (re.compile(r"density.*transmitter",re.I),"TRANSMITTER,DENSITY,INDICATING","DIT"),
    (re.compile(r"position transmitter",re.I),"TRANSMITTER,POSITION",     "ZIT"),
    (re.compile(r"flow element",       re.I), "ELEMENT,FLOW",             "FE"),
    (re.compile(r"level element",      re.I), "ELEMENT,LEVEL",            "LE"),
    (re.compile(r"temp.*element",      re.I), "ELEMENT,TEMPERATURE",      "TE"),
    (re.compile(r"analyzer element",   re.I), "ELEMENT,ANALYZER",         "AE"),
    (re.compile(r"thermowell",         re.I), "THERMOWELL",               "TW"),
    (re.compile(r"pressure gauge",     re.I), "GAUGE,PRESSURE",           "PG"),
    (re.compile(r"temperature gauge",  re.I), "GAUGE,TEMPERATURE",        "TG"),
    (re.compile(r"flow gauge",         re.I), "GAUGE,FLOW",               "FG"),
    (re.compile(r"level gauge",        re.I), "GAUGE,LEVEL",              "LG"),
    (re.compile(r"differential pressure gauge",re.I),"GAUGE,DIFFERENTIAL PRESSURE","DPG"),
    (re.compile(r"level switch",       re.I), "SWITCH,LEVEL",             "LS"),
    (re.compile(r"limit switch.*close",re.I), "SWITCH,LIMIT,CLOSE",       "LSC"),
    (re.compile(r"limit switch.*open", re.I), "SWITCH,LIMIT,OPEN",        "LSO"),
    (re.compile(r"limit switch",       re.I), "SWITCH,LIMIT",             "LS"),
    (re.compile(r"pressure switch",    re.I), "SWITCH,PRESSURE",          "PS"),
    (re.compile(r"hand switch.*open",  re.I), "SWITCH,HAND,OPEN",         "HSO"),
    (re.compile(r"hand switch.*close", re.I), "SWITCH,HAND,CLOSE",        "HSC"),
    (re.compile(r"selector switch",    re.I), "SWITCH,SELECTOR",          "SS"),
    (re.compile(r"i/p converter",      re.I), "I/P CONVERTOR",            "IP"),
    (re.compile(r"corrosion coupon",   re.I), "CORROSION COUPON",         "CC"),
    (re.compile(r"corrosion probe",    re.I), "CORROSION PROBE",          "CP"),
    (re.compile(r"restriction orifice",re.I), "ORIFICE,RESTRICTION",      "RO"),
    (re.compile(r"sight glass",        re.I), "SIGHT GLASS",              "SG"),
    (re.compile(r"centrifugal pump",   re.I), "PUMP,CENTRIFUGAL",         "P"),
    (re.compile(r"reciprocating pump", re.I), "PUMP,RECIPROCATING",       "P"),
    (re.compile(r"pump",               re.I), "PUMP",                     "P"),
    (re.compile(r"compressor",         re.I), "COMPRESSOR,GAS",           "K"),
    (re.compile(r"gearbox|gear box",   re.I), "GEARBOX",                  "KG"),
    (re.compile(r"motor",              re.I), "MOTOR",                    "M"),
    (re.compile(r"heat exchanger",     re.I), "HEAT EXCHANGER",           "E"),
    (re.compile(r"air cooler",         re.I), "COOLER,AIR",               "E"),
    (re.compile(r"vessel|separator",   re.I), "VESSEL,SEPARATOR",         "V"),
    (re.compile(r"tank",               re.I), "TANK",                     "TK"),
    (re.compile(r"filter|strainer",    re.I), "STRAINER",                 "S"),
    (re.compile(r"analyser|analyzer",  re.I), "ANALYSER",                 "AT"),
    (re.compile(r"pig launcher",       re.I), "PIG LAUNCHER",             "PL"),
    (re.compile(r"pig receiver",       re.I), "PIG RECEIVER",             "PR"),
    (re.compile(r"flame arrester",     re.I), "FLAME ARRESTER",           "FA"),
    (re.compile(r"blower",             re.I), "BLOWER",                   "BL"),
    (re.compile(r"piping|line",        re.I), "PIPING",                   "LINE"),
]


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Tag Text Normaliser
# ═══════════════════════════════════════════════════════════════════════════════

# Common OCR character confusions
_OCR_FIXES = str.maketrans({
    "\u2013": "-",   # en-dash → hyphen
    "\u2014": "-",   # em-dash → hyphen
    "\u2212": "-",   # minus   → hyphen
    "\u00A0": " ",   # NBSP    → space
    "—":      "-",
    "–":      "-",
    "\t":     " ",
})

_SEPARATOR_RE = re.compile(r"[\s./_\\]+")   # space, dot, slash, underscore → hyphen


def normalise_tag(raw_tag: str) -> tuple[str, list[str]]:
    """
    Normalise raw tag text to CEDM canonical form.
    Returns (canonical_tag, list_of_transformations_applied).

    Rules:
      1. Strip leading/trailing whitespace
      2. Apply OCR character fixes (en-dash → hyphen)
      3. Uppercase
      4. Replace non-hyphen separators with hyphens (dots, spaces, slashes)
      5. Collapse multiple hyphens to single
      6. Remove illegal characters (keep A-Z, 0-9, hyphen, double-quote for pipes)
      7. Strip leading/trailing hyphens
    """
    if not raw_tag:
        return "", ["empty_input"]

    tag = raw_tag.strip()
    transforms = []

    # OCR character fixes
    fixed = tag.translate(_OCR_FIXES)
    if fixed != tag:
        transforms.append(f"ocr_char_fix: {repr(tag)} → {repr(fixed)}")
        tag = fixed

    # Inch marker → IN for pipe sizes (10" / 10” → 10IN), matching Annexure-4
    # style. Only when preceded by a digit so we never touch instrument tags.
    inch_fixed = re.sub(r'(\d)\s*["”“]', r'\1IN', tag)
    if inch_fixed != tag:
        transforms.append(f"inch_normalised: {repr(tag)} → {repr(inch_fixed)}")
        tag = inch_fixed

    # Uppercase
    upper = tag.upper()
    if upper != tag:
        transforms.append("uppercased")
        tag = upper

    # Replace separators
    sep_fixed = _SEPARATOR_RE.sub("-", tag)
    if sep_fixed != tag:
        transforms.append(f"separators_normalised: {repr(tag)} → {repr(sep_fixed)}")
        tag = sep_fixed

    # Collapse multiple hyphens
    collapsed = re.sub(r"-{2,}", "-", tag)
    if collapsed != tag:
        transforms.append("collapsed_hyphens")
        tag = collapsed

    # Remove illegal characters — keep: A-Z 0-9 - " (inch symbol for pipe sizes)
    cleaned = re.sub(r'[^A-Z0-9\-"]', "", tag)
    if cleaned != tag:
        transforms.append(f"illegal_chars_removed: {repr(tag)} → {repr(cleaned)}")
        tag = cleaned

    # Strip leading/trailing hyphens
    stripped = tag.strip("-")
    if stripped != tag:
        transforms.append("leading_trailing_hyphens_stripped")
        tag = stripped

    if not transforms:
        transforms.append("no_change")

    return tag, transforms


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Discipline Classifier
# ═══════════════════════════════════════════════════════════════════════════════

def classify_discipline(canonical_tag: str, symbol_category: str,
                         symbol_name: str) -> str:
    """Derive DISCIPLINE from tag prefix and symbol info."""
    # Extract prefix: everything before the first digit or after last hyphen
    m = re.match(r'^([A-Z\-]{1,8})', canonical_tag)
    prefix_full = m.group(1).strip("-") if m else ""
    # Take the last alphabetic component as the instrument function code
    parts = prefix_full.split("-")
    codes_to_check = [parts[-1], parts[0], prefix_full]

    for codes, discipline in _DISCIPLINE_MAP:
        for code in codes_to_check:
            if code in codes:
                return discipline

    # Fallback by category
    cat = (symbol_category or "").lower()
    if cat in {"instrument"}:
        return "INSTRUMENTATION"
    if cat in {"valve", "equipment"}:
        return "MECHANICAL"
    if cat in {"piping"}:
        return "PIPING"

    # Fallback by pipe size pattern: 12"-ETH-V006
    if re.match(r'^\d+["\-]', canonical_tag):
        return "PIPING"

    return "MECHANICAL"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Description Standardiser
# ═══════════════════════════════════════════════════════════════════════════════

def standardise_description(symbol_name: str, canonical_tag: str,
                              registry_entry: Optional[dict] = None,
                              functional_context: str = "") -> tuple[str, str]:
    """
    Returns (tag_description, equipment_description) in CEDM format.
    Priority: registry lookup > functional_context > ontology mapping > fallback.
    """
    # Priority 1: registry lookup
    if registry_entry:
        reg_desc  = str(registry_entry.get("description") or "").strip()
        reg_equip = str(registry_entry.get("equipment_description") or "").strip()
        if reg_desc:
            return reg_desc.upper(), reg_equip.upper()

    # Priority 2: Gemini functional_context from step5a patch extraction
    fc = str(functional_context or "").strip()
    if fc:
        return fc.upper(), (symbol_name or canonical_tag).upper()

    # Priority 3: ontology mapping
    name = symbol_name or ""
    for pattern, equip_desc, prefix in _ONTOLOGY:
        if pattern.search(name):
            tag_desc = f"{prefix},{canonical_tag.split('-')[-1] if '-' in canonical_tag else canonical_tag}"
            return tag_desc.upper(), equip_desc.upper()

    # Fallback
    fallback_equip = (name or canonical_tag).upper()
    return canonical_tag.upper(), fallback_equip


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Canonical ID Generator
# ═══════════════════════════════════════════════════════════════════════════════

def make_canonical_id(project_id: str, canonical_tag: str,
                       drawing_number: str) -> str:
    """
    SHA-256 hash of project_id + canonical_tag + drawing_number.
    Returns uppercase 12-char hex prefix (stable primary key).
    """
    payload = f"{project_id}|{canonical_tag}|{drawing_number}"
    return hashlib.sha256(payload.encode()).hexdigest()[:12].upper()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. DOC STATUS Mapper
# ═══════════════════════════════════════════════════════════════════════════════

def map_doc_status(revision_code: str, issue_status: Optional[str] = None) -> str:
    """
    Map revision code to DOC STATUS string (Annexure-4 format).
    Falls back to issue_status from title block, then generic mapping.
    """
    if issue_status and len(issue_status.strip()) > 3:
        # Use the actual issue status from the title block (most accurate)
        return issue_status.strip().upper()

    code = (revision_code or "").strip().upper()
    return DOC_STATUS_MAP.get(code, f"REVISION {code}" if code else "ACTIVE")


# ═══════════════════════════════════════════════════════════════════════════════
# Registry enrichment helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_registry_entry(validation_details: list) -> Optional[dict]:
    """Pull the registry entry out of step5c validation_details."""
    for detail in (validation_details or []):
        if (detail.get("rule") == "REGISTRY"
                and detail.get("in_registry")
                and detail.get("registry_entry")):
            return detail["registry_entry"]
    return None


def _extract_validation_confidence(validation_details: list) -> float:
    """
    Compute C_val from step5c validation details.
    PASS=1.0, WARN=0.5, FAIL=0.0 per stage; take minimum (fail-fast).
    """
    stage_scores = []
    for d in (validation_details or []):
        p = d.get("pass")
        if p is True:
            stage_scores.append(1.0)
        elif p is False:
            stage_scores.append(0.0)
        else:
            stage_scores.append(0.5)   # WARN / None
    return min(stage_scores) if stage_scores else 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# Main normalisation
# ═══════════════════════════════════════════════════════════════════════════════

def normalise_candidate(cand: dict,
                         drawing_meta: dict,
                         project_id: str,
                         connectivity: Optional[dict] = None) -> dict:
    """
    Apply full CEDM normalisation to a single candidate record.
    Returns an enriched record with all 15 Annexure-4 fields.

    `connectivity` is the optional per-candidate dict from
    load_connectivity_map() (step5b2_hierarchy.json). When absent, no
    connectivity fields/remarks are added — behaviour is unchanged.
    """
    conn = connectivity or {}
    raw_tag            = str(cand.get("tag_text") or "")
    symbol_name        = str(cand.get("symbol_name") or "")
    symbol_category    = str(cand.get("symbol_category") or "")
    validation_details = cand.get("validation_details", [])
    registry_entry     = _extract_registry_entry(validation_details)

    # ── Step 1: Normalise tag ──────────────────────────────────────────────────
    canonical_tag, tag_transforms = normalise_tag(raw_tag)
    if not canonical_tag:
        canonical_tag = "UNKNOWN"
        tag_transforms.append("no_valid_tag")

    # ── Step 2: Descriptions ──────────────────────────────────────────────────
    tag_description, equip_description = standardise_description(
        symbol_name, canonical_tag, registry_entry,
        functional_context=str(cand.get("functional_context") or ""),
    )

    # ── Step 3: Discipline ────────────────────────────────────────────────────
    discipline = classify_discipline(canonical_tag, symbol_category, symbol_name)
    if registry_entry and registry_entry.get("discipline"):
        discipline = registry_entry["discipline"].strip().upper()

    # ── Step 4: Canonical ID ──────────────────────────────────────────────────
    drawing_number = drawing_meta.get("drawing_number", "UNKNOWN")
    canonical_id   = make_canonical_id(project_id, canonical_tag, drawing_number)

    # ── Step 5: DOC STATUS ────────────────────────────────────────────────────
    rev_code     = drawing_meta.get("revision_code", "")
    issue_status = drawing_meta.get("current_issue_status", "")
    doc_status   = map_doc_status(rev_code, issue_status)

    # ── Derivations for REMARKS field ─────────────────────────────────────────
    remarks_parts = []
    val_status = cand.get("validation_status", "WARN")
    val_reason = cand.get("validation_reason", "")
    sow_status = cand.get("sow_status", "UNSPECIFIED")

    if val_status == "FAIL":
        remarks_parts.append(f"VALIDATION_FAIL: {val_reason}")
    elif val_status == "WARN":
        remarks_parts.append(f"WARN: {val_reason}")

    if sow_status == "UNSPECIFIED":
        remarks_parts.append("NO_SCOPE_DEFINITION_FOUND")
    elif sow_status == "OUT_OF_SCOPE":
        remarks_parts.append("OUT_OF_SCOPE")

    if not registry_entry:
        remarks_parts.append("NOT_IN_REGISTER")

    if tag_transforms and tag_transforms != ["no_change"]:
        if any("ocr" in t or "illegal" in t or "sep" in t for t in tag_transforms):
            remarks_parts.append(f"TAG_NORMALISED_FROM: {raw_tag}")

    scope_type = cand.get("scope_type", "FULL_DRAWING")
    if scope_type == "REVISION_CLOUD":
        remarks_parts.append("WITHIN_REVISION_CLOUD")

    # ── Connectivity (step5b2 hierarchy) ──────────────────────────────────────
    cinfo = conn.get(cand.get("candidate_id", ""), {})
    parent_equip    = cinfo.get("parent_equipment") or cinfo.get("connected_equipment") or ""
    is_isolated     = bool(cinfo.get("is_isolated", False))
    connected_lines = int(cinfo.get("connected_lines", 0))
    flow_direction  = cinfo.get("flow_direction", "")
    flow_evidence   = cinfo.get("flow_evidence", "")
    control_loop    = cinfo.get("control_loop", "")
    if parent_equip:
        remarks_parts.append(f"PARENT_EQUIP: {parent_equip}")
    if is_isolated:
        remarks_parts.append("ISOLATED_DETECTION")
    if flow_direction:
        remarks_parts.append(f"FLOW: {flow_direction}")
    if control_loop:
        remarks_parts.append(f"CONTROL_LOOP: {control_loop}")

    # ── Confidence signal (C_val) ─────────────────────────────────────────────
    c_val = _extract_validation_confidence(validation_details)
    if not registry_entry:
        c_reg = 0.5
    else:
        c_reg = 1.0

    # ── Assemble CEDM record ──────────────────────────────────────────────────
    cedm = {
        # --- 15 Annexure-4 fields ---
        "SLNO":                 None,   # assigned at output time
        "DISCIPLINE":           discipline,
        "TAG NUMBER":           canonical_tag,
        "TAG DESCRIPTION":      tag_description,
        "EQUIPMENT DESCRIPTION":equip_description,
        "SIZE&RATING":          (registry_entry or {}).get("size_rating", ""),
        "DOCUMENT NUMBER":      drawing_meta.get("drawing_number", ""),
        "SHEET NO":             drawing_meta.get("sheet_number", ""),
        "REV":                  rev_code,
        "DRAWING REFERENCE":    drawing_meta.get("drawing_reference", ""),
        "DOCUMENT TITLE":       drawing_meta.get("drawing_title", ""),
        "DOC STATUS":           doc_status,
        "DATE":                 drawing_meta.get("issue_date", ""),
        "DUPLICATE STATUS":     "YES" if cand.get("duplicate_status") == "DISCARDED" else "NO",
        "REMARKS":              " | ".join(remarks_parts) if remarks_parts else "",

        # --- Pipeline metadata (not in Excel output but needed by step8) ---
        "_candidate_id":        cand.get("candidate_id", ""),
        "_canonical_id":        canonical_id,
        "_raw_tag":             raw_tag,
        "_canonical_tag":       canonical_tag,
        "_tag_transforms":      tag_transforms,
        "_symbol_name":         symbol_name,
        "_symbol_category":     symbol_category,
        "_symbol_bbox":         cand.get("symbol_bbox", {}),
        "_validation_status":   val_status,
        "_sow_status":          sow_status,
        "_in_registry":         registry_entry is not None,
        "_c_det":               float(cand.get("vision_confidence") or 0.0),
        "_c_ocr":               float(cand.get("ocr_confidence") or 0.0),
        "_c_geo":               float(cand.get("association_confidence") or 0.0),
        "_c_val":               c_val,
        "_c_reg":               c_reg,
        "_patch_id":            cand.get("patch_id"),
        "_scope_type":          scope_type,

        # --- Connectivity provenance (from step5b2_hierarchy.json, step8 reads these) ---
        "_hier_is_isolated":      is_isolated,
        "_hier_parent_equipment": parent_equip,
        "_hier_connected_lines":  connected_lines,
        "_hier_flow_direction":   flow_direction,
        "_hier_flow_evidence":    flow_evidence,
        "_hier_control_loop":     control_loop,
    }
    return cedm


# ═══════════════════════════════════════════════════════════════════════════════
# Connectivity / hierarchy loader  (step5b2_hierarchy.json — OPTIONAL)
# ═══════════════════════════════════════════════════════════════════════════════

def load_connectivity_map(out_dir: str,
                          hierarchy_path: Optional[str] = None) -> dict:
    """
    Read step5b2_hierarchy.json (if present) and return a per-candidate
    connectivity summary keyed by candidate_id:

        { candidate_id: {
            "is_isolated":         bool,   # no graph edge to any pipeline/equipment
            "parent_equipment":    str,    # tag of nearest equipment ancestor ('' if none)
            "connected_equipment": str,    # tag of directly-bound equipment ('' if none)
            "connected_lines":     int,    # # of pipelines this candidate links to
            "flow_direction":      str,    # 'upstream'|'downstream'|'' (relative to candidate)
            "flow_evidence":       str,    # how the pipeline direction was determined ('' if none)
            "control_loop":        str,    # Track C control-loop id ('' if none / over-merge cluster)
        } }

    Flow direction is read from the candidate's connected pipeline in
    step5b2_hierarchy.json (Track B). For the connected pipeline whose
    inlet/outlet endpoint the candidate sits nearest:
        candidate near OUTLET  -> flow goes TO it     -> 'downstream'
        candidate near INLET   -> flow comes FROM it  -> 'upstream'
    Only set when the pipeline direction is known (!= 'unknown') and the
    evidence is real (!= 'none'). All 51 directed pipelines qualify —
    including equipment_convention and topology_dead_end.

    This file is produced by step5b2_hierarchy.py, a side-branch post-processor
    off step5b. It is OPTIONAL: if absent, step7 behaves exactly as before and
    returns an empty map (all lookups fall back to neutral defaults).
    """
    path = hierarchy_path or str(Path(out_dir) / "step5b2_hierarchy.json")
    if not Path(path).exists():
        log.info("No step5b2_hierarchy.json — skipping connectivity enrichment")
        return {}

    with open(path) as f:
        H = json.load(f)

    hier  = H.get("hierarchy", [])
    enr   = {e["candidate_id"]: e for e in H.get("enriched_candidates", [])}
    graph = H.get("graph", {})

    # id → (kind, tag, bbox) for resolving ancestor / connected node ids
    id_kind = {n["node_id"]: n.get("kind", "")      for n in graph.get("nodes", [])}
    id_tag  = {n["node_id"]: n.get("tag_text", "")  for n in graph.get("nodes", [])}
    id_bbox = {n["node_id"]: n.get("bbox", {})      for n in graph.get("nodes", [])}

    # pipeline_id → flow + endpoints (Track B direction)
    pl_by_id = {p["pipeline_id"]: p for p in H.get("pipelines", [])}

    # candidate_id → control-loop id (Track C, valid loops only — size-capped;
    # over-merge clusters are NOT loops and are intentionally excluded)
    node_to_loop = {}
    for lp in H.get("control_loops", []):
        for mid in lp.get("member_ids", []):
            node_to_loop[mid] = lp["loop_id"]

    def _center(bb):
        if not bb:
            return None
        return ((bb["x1"] + bb["x2"]) / 2.0, (bb["y1"] + bb["y2"]) / 2.0)

    def _dist(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

    # candidate → set of pipelines it links to (MONITORS / CONNECTED_TO edges)
    from collections import defaultdict
    cand_lines: dict = defaultdict(set)
    for e in graph.get("edges", []):
        f_id, t_id = e.get("from"), e.get("to")
        kf, kt = id_kind.get(f_id), id_kind.get(t_id)
        if kf == "pipeline" and kt in ("instrument", "valve", "piping", "equipment"):
            cand_lines[t_id].add(f_id)
        elif kt == "pipeline" and kf in ("instrument", "valve", "piping", "equipment"):
            cand_lines[f_id].add(t_id)

    conn: dict = {}
    for h in hier:
        cid = h["node_id"]
        # nearest equipment ancestor (excluding the node itself)
        parent_equip = ""
        for anc in h.get("ancestor_path", []):
            if anc != cid and id_kind.get(anc) == "equipment":
                parent_equip = id_tag.get(anc, "")
                break
        # directly-bound equipment (enriched connected_equipment is a candidate id).
        # Only trust it when the resolved node is actually equipment-kind — step5b's
        # binding can point at a size label or instrument, which is not a parent.
        ce_id = (enr.get(cid, {}) or {}).get("connected_equipment", "")
        connected_equip = (id_tag.get(ce_id, "")
                           if ce_id and id_kind.get(ce_id) == "equipment" else "")

        # Flow direction (Track B): among the candidate's connected pipelines with
        # a KNOWN direction, pick the one whose inlet/outlet endpoint the candidate
        # is nearest, then label upstream/downstream relative to the candidate.
        flow_direction = ""
        flow_evidence  = ""
        cc = _center(id_bbox.get(cid, {}))
        if cc is not None:
            best = None   # (dist_to_nearest_endpoint, direction, evidence)
            for pid in cand_lines.get(cid, ()):
                p = pl_by_id.get(pid)
                if not p:
                    continue
                fl = p.get("flow", {}) or {}
                d, ev = fl.get("direction"), fl.get("evidence")
                if d in (None, "unknown") or ev in (None, "none"):
                    continue
                ip, op = p.get("inlet_point"), p.get("outlet_point")
                if not ip or not op:
                    continue
                di, do = _dist(cc, ip), _dist(cc, op)
                # near OUTLET => flow goes TO candidate => downstream; else upstream
                direction = "downstream" if do <= di else "upstream"
                dmin = min(di, do)
                if best is None or dmin < best[0]:
                    best = (dmin, direction, ev)
            if best is not None:
                flow_direction, flow_evidence = best[1], best[2]

        conn[cid] = {
            "is_isolated":         bool(h.get("is_isolated", False)),
            "parent_equipment":    parent_equip,
            "connected_equipment": connected_equip,
            "connected_lines":     len(cand_lines.get(cid, ())),
            "flow_direction":      flow_direction,
            "flow_evidence":       flow_evidence,
            "control_loop":        node_to_loop.get(cid, ""),
        }

    n_iso  = sum(1 for v in conn.values() if v["is_isolated"])
    n_eq   = sum(1 for v in conn.values() if v["parent_equipment"] or v["connected_equipment"])
    n_flow = sum(1 for v in conn.values() if v["flow_direction"])
    n_loop = sum(1 for v in conn.values() if v["control_loop"])
    log.info("Connectivity map: %d candidates (%d isolated, %d with equipment link, "
             "%d with flow direction, %d in a control loop)",
             len(conn), n_iso, n_eq, n_flow, n_loop)
    return conn


# ═══════════════════════════════════════════════════════════════════════════════
# Metadata loader
# ═══════════════════════════════════════════════════════════════════════════════

def load_drawing_meta(drawing_context_path: Optional[str],
                       out_dir: str) -> dict:
    """
    Load drawing metadata from drawing_context.json and/or
    title_block_context.json.  Returns a flat metadata dict.
    """
    meta = {
        "drawing_number":       "",
        "sheet_number":         "",
        "revision_code":        "",
        "drawing_title":        "",
        "drawing_reference":    "",
        "issue_date":           "",
        "current_issue_status": "",
    }

    ctx_path = drawing_context_path or str(Path(out_dir) / "drawing_context.json")
    if Path(ctx_path).exists():
        with open(ctx_path) as f:
            ctx = json.load(f)
        meta.update({
            "drawing_number":    ctx.get("drawing_number", ""),
            "sheet_number":      ctx.get("sheet_number", ""),
            "revision_code":     ctx.get("revision_code", ""),
            "drawing_title":     ctx.get("drawing_title", ""),
        })
        # Compose DRAWING REFERENCE like Annexure-4: DWG_NO-SHEET-REV
        dwg  = ctx.get("drawing_number", "")
        sht  = ctx.get("sheet_number", "")
        rev  = ctx.get("revision_code", "")
        if dwg:
            meta["drawing_reference"] = f"{dwg}-{sht}-{rev}".strip("-")

    # Override with title_block_context if available
    tb_path = str(Path(out_dir) / "title_block_context.json")
    if Path(tb_path).exists():
        with open(tb_path) as f:
            tb = json.load(f)
        tb_block = tb.get("title_block", tb)
        meta.update({
            "drawing_number":       tb_block.get("drawing_number") or meta["drawing_number"],
            "sheet_number":         tb_block.get("sheet_number")   or meta["sheet_number"],
            "revision_code":        tb_block.get("revision_code")  or meta["revision_code"],
            "drawing_title":        tb_block.get("title")          or meta["drawing_title"],
            "issue_date":           tb_block.get("issue_date", ""),
        })
        meta["current_issue_status"] = tb.get("current_issue_status", "")
        dwg  = meta["drawing_number"]
        sht  = meta["sheet_number"]
        rev  = meta["revision_code"]
        if dwg:
            meta["drawing_reference"] = f"{dwg}-{sht}-{rev}".strip("-")

    return meta


# ═══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def run_cedm_normalizer(
    final_path:          str,
    out_dir:             str,
    drawing_context_path:Optional[str] = None,
    project_id:          str           = "CDCI",
    hierarchy_path:      Optional[str] = None,
) -> list[dict]:

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Load input
    with open(final_path) as f:
        data = json.load(f)
    candidates = data.get("candidates",
                 data.get("validated_candidates",
                 data.get("all_records", [])))
    # Only PRIMARY records
    candidates = [c for c in candidates
                  if c.get("duplicate_status", "PRIMARY") == "PRIMARY"]
    log.info("Loaded %d PRIMARY candidates for CEDM normalisation", len(candidates))

    # Load drawing metadata
    meta = load_drawing_meta(drawing_context_path, out_dir)
    log.info("Drawing: %s  Sht=%s  Rev=%s",
             meta["drawing_number"], meta["sheet_number"], meta["revision_code"])

    # Load optional connectivity map (step5b2_hierarchy.json)
    connectivity = load_connectivity_map(out_dir, hierarchy_path)

    # Staleness guard: if a hierarchy was loaded but almost none of the current
    # candidates' ids appear in it, the hierarchy is from a PRIOR extraction
    # (step5a/5b re-ran → new candidate_ids). Enrichment would silently be ~0.
    if connectivity:
        matched = sum(1 for c in candidates
                      if c.get("candidate_id") in connectivity)
        if candidates and matched / len(candidates) < 0.5:
            log.warning("STALE hierarchy: only %d/%d candidates match "
                        "step5b2_hierarchy.json — connectivity enrichment will be "
                        "near-empty. Re-run step5b2_hierarchy.py on the current "
                        "step5b_associations.json.", matched, len(candidates))

    # Normalise all candidates
    cedm_records: list[dict] = []
    transform_counts: dict[str, int] = {}

    for i, cand in enumerate(candidates):
        record = normalise_candidate(cand, meta, project_id, connectivity)
        record["SLNO"] = i + 1
        cedm_records.append(record)

        # Tally transforms
        for t in record.get("_tag_transforms", []):
            key = t.split(":")[0]
            transform_counts[key] = transform_counts.get(key, 0) + 1

    # Dedup check: same canonical tag appearing more than once
    seen_canonical: dict[str, list[int]] = {}
    for i, r in enumerate(cedm_records):
        ct = r["_canonical_tag"]
        seen_canonical.setdefault(ct, []).append(i)

    dup_canonical = {tag: idxs for tag, idxs in seen_canonical.items()
                     if len(idxs) > 1}
    if dup_canonical:
        log.warning("Post-CEDM canonical duplicates: %d groups", len(dup_canonical))
        for tag, idxs in list(dup_canonical.items())[:5]:
            log.warning("  %s → records %s", tag, idxs)

    # Statistics
    discipl_counts: dict[str, int] = {}
    for r in cedm_records:
        d = r["DISCIPLINE"]
        discipl_counts[d] = discipl_counts.get(d, 0) + 1

    log.info("CEDM normalisation complete: %d records", len(cedm_records))
    log.info("Discipline breakdown: %s",
             " | ".join(f"{k}={v}" for k, v in discipl_counts.items()))
    log.info("Transforms applied: %s",
             " | ".join(f"{k}={v}" for k, v in transform_counts.items()
                        if k != "no_change")
             or "none")

    # Write output
    out_path = str(out / "step7_cedm_output.json")
    with open(out_path, "w") as f:
        json.dump({
            "version":           "v1",
            "step":              "7_CEDM",
            "record_count":      len(cedm_records),
            "drawing_number":    meta["drawing_number"],
            "discipline_counts": discipl_counts,
            "transform_counts":  transform_counts,
            "canonical_dup_groups": len(dup_canonical),
            "records":           cedm_records,
        }, f, indent=2)
    log.info("✓ step7_cedm_output.json → %s", out_path)
    return cedm_records


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Step 7: CEDM Normalisation Engine")
    parser.add_argument("--final",    help="step5_final_output.json or step5d_deduped.json")
    parser.add_argument("--context",  help="drawing_context.json")
    parser.add_argument("--out",      default="output")
    parser.add_argument("--project",  default="CDCI", help="Project ID for canonical hash")
    parser.add_argument("--hierarchy", help="step5b2_hierarchy.json (optional connectivity enrichment)")
    args = parser.parse_args()

    final_path = args.final or str(Path(args.out) / "step5_final_output.json")
    if not Path(final_path).exists():
        # try deduped
        final_path = str(Path(args.out) / "step5d_deduped.json")
    if not Path(final_path).exists():
        parser.error(f"No input file found. Pass --final or ensure step5 outputs exist in {args.out}/")

    records = run_cedm_normalizer(
        final_path=final_path,
        out_dir=args.out,
        drawing_context_path=args.context,
        project_id=args.project,
        hierarchy_path=args.hierarchy,
    )

    print(f"\n=== Step 7 Complete — CEDM Normalisation ===")
    print(f"  Records normalised : {len(records)}")
    dc: dict[str, int] = {}
    for r in records:
        d = r["DISCIPLINE"]
        dc[d] = dc.get(d, 0) + 1
    for disc, cnt in sorted(dc.items(), key=lambda x: -x[1]):
        print(f"    {disc:<20} {cnt:>4}")
    changed = sum(1 for r in records if r.get("_tag_transforms") != ["no_change"])
    print(f"  Tags normalised    : {changed}/{len(records)}")
    print(f"\n  Output: {args.out}/step7_cedm_output.json")


if __name__ == "__main__":
    main()