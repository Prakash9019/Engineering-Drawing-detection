"""
Stage 5: Tag Association & Engineering Enrichment (Arch Layers 8, 11)
=======================================================================
Takes raw detections and enriches them with:
  - Full tag number (with unit prefix applied)
  - Discipline classification
  - Engineering description (context-specific, not generic)
  - Equipment description (generic type)
  - Size/rating if visible
  - Confidence score
"""
import json
import logging
import re
import time
from typing import List, Optional

import numpy as np

from settings import GEMINI_DELAY_SEC
from core.gemini_client import GeminiClient
from core.json_parser import parse_json
from core.isa_decode import decode_isa, classify_discipline, cedm_normalize
from core.confidence import label_to_score, calc_final, route

log = logging.getLogger(__name__)


def _build_prompt(unit_prefix: str, drawing_title: str, chunk: List[dict]) -> str:
    """Build the enrichment prompt for a batch of detections."""
    return f"""You are an expert P&ID engineer. Drawing: {drawing_title}
Unit prefix: "{unit_prefix}" — all in-scope tags use "{unit_prefix}-" prefix.

For each detected object below, provide the complete tag-register entry.

RULES:

1. TAG NUMBER — apply "{unit_prefix}-" prefix to instruments and valves:
   • PIT-211 → V-PIT-211
   • BV-2243 → V-BV-2243
   • TIT-212 → V-TIT-212
   • Equipment tags K-V-201, KG-V-201, KM-V-201, S-V-204 keep as-is (no extra prefix)
   • Piping line numbers (e.g., 2"-ETH-V057-61440X) do NOT get prefix

2. DISCIPLINE (one of):
   • MECHANICAL: equipment (K-, KG-, S-), vessels (V-V-), valves (BV/GV/RV/NRV)
   • ELECTRICAL: motors (KM-)
   • INSTRUMENTATION: all instruments (PIT, TIT, FIT, PDI, etc.)
   • PIPING: line numbers

3. TAG DESCRIPTION — SPECIFIC, with location context:
   BAD:  "BALL VALVE" or "TEMPERATURE TRANSMITTER"
   GOOD: "BV,COMPRSR D/L VENT,K-V-201 TO FLARE HDR"
   GOOD: "TEMP IND TX,COMP SUCTION GAS TEMP,K-V-201"

4. EQUIPMENT DESCRIPTION — generic equipment type:
   "VALVE,BALL,2IN" or "TRANSMITTER,TEMPERATURE,INDICATING"

5. SIZE — if visible (2IN, 6IN, 1X2IN, 65BARG, ID620XLG1600MM)

6. CONFIDENCE — HIGH (clear symbol+tag) / MEDIUM (readable, ambiguous) / LOW (uncertain)

Detected objects (with bounding boxes for context):
{json.dumps(chunk, indent=1)}

Return ONLY this JSON array:
[
  {{
    "id": 1,
    "tag_number": "V-PIT-211",
    "discipline": "INSTRUMENTATION",
    "tag_description": "PRESSURE IND TX,COMP SUCTION,K-V-201",
    "equipment_description": "TRANSMITTER,PRESSURE,INDICATING",
    "size_rating": "",
    "confidence": "HIGH"
  }}
]"""


def _ensure_prefix(tag: str, unit_prefix: str) -> str:
    """Apply unit prefix to tags that need it, leave equipment/piping alone."""
    if not unit_prefix:
        return tag
    if tag.startswith(f'{unit_prefix}-'):
        return tag
    # Equipment tags — no extra prefix
    if re.match(r'^(K|KG|KM|S)-V-', tag):
        return tag
    # Piping — no prefix
    if re.match(r'^\d', tag):
        return tag
    # Standard instrument/valve tag
    if re.match(r'^[A-Z]{2,5}-?\d{2,4}', tag):
        return f'{unit_prefix}-{tag}'
    return tag


def associate_tags(
    detections: List[dict],
    image: np.ndarray,
    gemini: GeminiClient,
    title_block: dict,
    batch_size: int = 50,
) -> List[dict]:
    """
    Enrich raw detections with engineering descriptions.

    Sends the full image + batches of detection labels to Gemini for context-
    aware enrichment, then merges results with local ISA decoding fallback.

    Returns:
        List of fully populated record dicts.
    """
    if not detections:
        log.info("  No detections to associate")
        return []

    unit_prefix = title_block.get('unit_prefix', '').strip() or 'V'
    drawing_title = title_block.get('drawing_title', '')

    det_list = [
        {'id': i + 1, 'label': d['label'], 'box': d['box'],
         'symbol_type': d.get('symbol_type', '')}
        for i, d in enumerate(detections)
    ]

    enriched_by_id = {}
    for start in range(0, len(det_list), batch_size):
        chunk = det_list[start:start + batch_size]
        prompt = _build_prompt(unit_prefix, drawing_title, chunk)

        log.info(f"  Enriching batch [{start+1}-{start+len(chunk)}] of {len(det_list)}")
        raw = gemini.ask(prompt, image, max_tokens=16000)
        data = parse_json(raw)

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and 'id' in item:
                    enriched_by_id[item['id']] = item
        time.sleep(GEMINI_DELAY_SEC)

    # Build records merging Gemini enrichment + ISA fallback
    records = []
    for i, det in enumerate(detections):
        enr = enriched_by_id.get(i + 1, {})

        # Tag number with prefix applied
        raw_tag = enr.get('tag_number', det['label']).strip().upper()
        tag = cedm_normalize(raw_tag)
        tag = _ensure_prefix(tag, unit_prefix)

        # ISA fallback for descriptions
        isa = decode_isa(tag)
        tag_desc = enr.get('tag_description', '').strip()
        if not tag_desc:
            tag_desc = isa.get('description', tag)

        equip_desc = enr.get('equipment_description', '').strip()
        if not equip_desc:
            equip_desc = isa.get('description', '')

        # Discipline
        disc = enr.get('discipline', '').strip().upper()
        if not disc or disc == 'UNKNOWN':
            disc = classify_discipline(tag)

        # Confidence
        c_label = enr.get('confidence', 'MEDIUM')
        c_ocr = label_to_score(c_label)
        c_final = calc_final(c_det=0.85, c_ocr=c_ocr, c_geo=0.85, c_val=1.0, c_reg=0.5)

        records.append({
            'tag_number': tag,
            'discipline': disc,
            'tag_description': tag_desc,
            'equipment_description': equip_desc,
            'size_rating': enr.get('size_rating', '').strip(),
            'symbol_type': det.get('symbol_type', ''),
            'box': det['box'],
            'c_ocr': c_ocr,
            'c_final': c_final,
            'route': route(c_final),
            'duplicate': 'NO',
            'remarks': '',
        })

    log.info(f"  Assembled {len(records)} enriched records")
    return records
