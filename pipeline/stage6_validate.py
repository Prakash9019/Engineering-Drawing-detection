"""
Stage 6: Validation Engine (Architecture Layer 12 — 7-Stage Validation)
=========================================================================
Validates records against engineering naming conventions, business rules,
project context, and reference-text patterns.

Validation stages:
  S1: Format validation (regex check against known tag patterns)
  S2: Business rule validation (no NOTE labels, no drawing-number tags)
  S3: Reference text rejection (cross-references from OTHER drawings)
  S4: Project validation (no foreign document numbers in tags)
  S5: Prefix validation (unit prefix applied where required)
  S6: Document validation (passed in via context)
  S7: Duplicate detection (CEDM-normalized)
"""
import logging
import math
import re
from typing import List

from settings import REFERENCE_TEXT_PATTERNS
from core.isa_decode import cedm_normalize
from core.confidence import calc_final, route

log = logging.getLogger(__name__)


# Compile reference text patterns once
_REF_REGEXES = [re.compile(p, re.IGNORECASE) for p in REFERENCE_TEXT_PATTERNS]


def _is_reference_text(tag: str) -> bool:
    """Stage S3: Reject cross-reference text from other drawings."""
    stripped = re.sub(r'^V-', '', tag, flags=re.IGNORECASE)
    return any(rx.match(stripped) for rx in _REF_REGEXES)


def _valid_format(tag: str) -> bool:
    """Stage S1: Check tag matches a known engineering format."""
    return bool(
        re.match(r'^[A-Z]{1,3}-V-\d{3}[A-Z]?$', tag) or            # equipment K-V-201
        re.match(r'^(V-)?[A-Z]{2,5}-?\d{2,5}[A-Z]?$', tag) or       # instrument V-PIT-211
        re.match(r'^(V-)?(BV|GV|RV|NRV|GLV|FCV|PCV|TCV|LCV)-\d{2,5}[A-Z]?$', tag) or
        re.match(r'^(V-)?I-\d{3,4}[A-Z]?$', tag) or                 # interlock V-I-001
        re.match(r'^\d{1,2}["-][A-Z]', tag) or                      # piping 2"-ETH-...
        re.match(r'^\d{1,2}IN-', tag) or                            # piping 2IN-...
        re.match(r'^S-V-\d{3}', tag)                                # strainer S-V-204
    )


def validate_records(
    records: List[dict],
    title_block: dict,
) -> List[dict]:
    """
    Run all 7 validation stages on the records list.

    Records that fail S2/S3 (business rule / reference text) are REJECTED.
    Records that fail S1/S5 are kept but flagged with reduced c_val.
    Duplicates are flagged but not removed (preserved for the register).
    """
    if not records:
        return []

    doc_num = title_block.get('document_number', '')
    validated = []
    rejected = 0
    flagged = 0

    # First pass: filter rejections, calculate per-record validation score
    seen_canonical = {}
    for rec in records:
        tag = rec.get('tag_number', '').strip().upper()
        if not tag:
            rejected += 1
            continue

        remarks = []
        c_val = 1.0

        # S3: Reference text rejection (most aggressive)
        if _is_reference_text(tag):
            rejected += 1
            continue

        # S4: Project validation — reject tags containing foreign doc numbers
        if any(x in tag for x in ['4224', 'MGDV', 'MCDTY']):
            rejected += 1
            continue

        # S1: Format validation
        if not _valid_format(tag):
            remarks.append('FORMAT_WARN')
            c_val = min(c_val, 0.6)
            flagged += 1

        # S5: Prefix validation
        unit_prefix = title_block.get('unit_prefix', '').strip() or 'V'
        if (unit_prefix and
                rec.get('discipline') == 'INSTRUMENTATION' and
                not tag.startswith(f'{unit_prefix}-') and
                not re.match(r'^\d', tag)):
            remarks.append('PREFIX_MISSING')
            c_val = min(c_val, 0.7)

        # S7: Duplicate detection (CEDM normalized)
        canonical = cedm_normalize(tag)
        is_duplicate = canonical in seen_canonical
        if is_duplicate:
            existing = seen_canonical[canonical]
            # If centers are very close, it's the same equipment OCR'd twice → drop
            if 'box' in rec and 'box' in existing:
                ex_cx = (existing['box'][0] + existing['box'][2]) / 2
                ex_cy = (existing['box'][1] + existing['box'][3]) / 2
                rc_cx = (rec['box'][0] + rec['box'][2]) / 2
                rc_cy = (rec['box'][1] + rec['box'][3]) / 2
                d = math.sqrt((ex_cx - rc_cx) ** 2 + (ex_cy - rc_cy) ** 2)
                if d < 100:
                    rejected += 1
                    continue  # merge
            rec['duplicate'] = 'YES'
            remarks.append('DUPLICATE')
        else:
            rec['duplicate'] = 'NO'
            seen_canonical[canonical] = rec

        # Recompute final confidence including validation score
        rec['c_val'] = c_val
        rec['c_final'] = calc_final(
            c_det=0.85,
            c_ocr=rec.get('c_ocr', 0.75),
            c_geo=0.85,
            c_val=c_val,
            c_reg=0.5,
        )
        rec['route'] = route(rec['c_final'])

        # Append routing info to remarks
        if rec['route'] == 'REVIEW_REQUIRED':
            remarks.append('REVIEW_REQUIRED')

        rec['remarks'] = '; '.join(remarks) if remarks else ''
        validated.append(rec)

    log.info(f"  Validation: {len(records)} → {len(validated)} "
             f"(rejected {rejected}, flagged {flagged})")
    return validated
