"""
Stage 3: Notes Intelligence Engine (Architecture Layer 5)
==========================================================
Extracts engineering notes and converts them into structured rules:
  - Prefix rules (e.g., "all instruments use V- prefix")
  - Scope rules (e.g., "exclude items in clouded area")
  - Cross-references (e.g., "SEE DWG xxx")
  - Equipment naming conventions
  - Safety/exclusion notes
"""
import logging

import numpy as np

from core.gemini_client import GeminiClient
from core.json_parser import parse_json

log = logging.getLogger(__name__)


NOTES_PROMPT = """Read ALL engineering notes from this P&ID drawing's notes section.
For each numbered note, extract its content and classify its purpose.

Return ONLY this JSON structure:

{
  "notes": [
    {
      "note_number": "1",
      "text": "full note text exactly as written",
      "type": "GENERAL | PREFIX_RULE | VENDOR_RULE | SCOPE_RULE | EQUIPMENT_RULE | CROSS_REFERENCE | SAFETY"
    }
  ],
  "prefix_rules": [
    {"prefix": "V", "applies_to": "ALL | INSTRUMENT | VALVE | EQUIPMENT"}
  ],
  "scope_exclusions": [
    "text of any scope/exclusion note that limits what's in this drawing"
  ],
  "cross_references": [
    "SEE DWG XXX-001-1"
  ],
  "vendor_rules": [
    "text of any vendor-naming or supplier-specific rule"
  ]
}

Look carefully for:
- Unit/station prefixes (V- for Station V, etc.)
- Statements like "FOR INTERLOCK DETAILS, REFER TO ..."
- Equipment scope notes ("Existing X shall be removed", etc.)
- Vendor instructions ("New Y by Hitachi", etc.)

If a category has no entries, use an empty array.
Return ONLY the JSON object, no markdown fences."""


def extract_notes(
    image: np.ndarray,
    gemini: GeminiClient,
) -> dict:
    """
    Extract engineering notes and structured rules.

    Crops the notes region (typically bottom-left of drawing).

    Returns:
        dict with keys: notes, prefix_rules, scope_exclusions, cross_references, vendor_rules
    """
    H, W = image.shape[:2]
    # Notes are typically in the lower-left portion of the drawing
    notes_region = image[int(H * 0.58):int(H * 0.85), 0:int(W * 0.55)]

    log.info(f"  Extracting notes from {notes_region.shape[1]}x{notes_region.shape[0]} region")
    raw = gemini.ask(NOTES_PROMPT, notes_region, max_tokens=16000)
    data = parse_json(raw)

    if not isinstance(data, dict):
        log.warning("  Notes extraction returned non-dict response")
        return {
            "notes": [],
            "prefix_rules": [],
            "scope_exclusions": [],
            "cross_references": [],
            "vendor_rules": [],
        }

    notes = data.get('notes', [])
    prefix_rules = data.get('prefix_rules', [])
    log.info(f"    {len(notes)} notes, {len(prefix_rules)} prefix rules, "
             f"{len(data.get('scope_exclusions', []))} scope exclusions, "
             f"{len(data.get('cross_references', []))} cross-refs")

    return data
