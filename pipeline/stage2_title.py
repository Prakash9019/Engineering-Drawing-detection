"""
Stage 2: Title Block Extraction (Architecture Layer 2)
========================================================
Extracts drawing metadata from the title block region using Gemini OCR.

Output: dict with document_number, revision, unit_prefix, drawing_title,
        date, status, client, contractor, etc.
"""
import logging
from typing import Optional

import numpy as np

from core.gemini_client import GeminiClient
from core.json_parser import parse_json

log = logging.getLogger(__name__)


TITLE_BLOCK_PROMPT = """Extract ALL metadata from this P&ID title block region.
Return ONLY a JSON object with these fields (use empty string if not visible):

{
  "document_number": "",
  "drawing_number": "",
  "drawing_title": "",
  "project_name": "",
  "unit_prefix": "",
  "sheet_number": "",
  "revision": "",
  "revision_description": "",
  "status": "",
  "date": "",
  "client": "",
  "contractor": "",
  "vendor": "",
  "scale": "",
  "discipline": "",
  "scope_note": ""
}

CRITICAL FIELDS:
- unit_prefix: The station/unit identifier. If equipment tags use "V-" prefix
  (e.g., V-PIT-211, V-BV-2243), set this to "V". Look for words like "STATION V".
- revision: The current revision letter/number (0, 1, 2, A, B, C, etc.)
- scope_note: If the drawing says "USE THIS DRAWING FOR INFORMATION WITHIN
  THE CLOUDED AREAS ONLY" or similar scope-limiting text, capture the exact text.

Return ONLY the JSON object, no markdown fences, no explanation."""


def extract_title_block(
    image: np.ndarray,
    gemini: GeminiClient,
) -> dict:
    """
    Extract title block metadata from the drawing.

    Crops the bottom-right region (typical title block location) and asks
    Gemini to extract structured metadata.

    Returns:
        dict with metadata fields. Empty dict on failure.
    """
    H, W = image.shape[:2]
    # Crop bottom-right region (typical title block location)
    tb_region = image[int(H * 0.78):H, int(W * 0.48):W]

    log.info(f"  Extracting title block from {tb_region.shape[1]}x{tb_region.shape[0]} region")
    raw = gemini.ask(TITLE_BLOCK_PROMPT, tb_region)
    data = parse_json(raw)

    if not isinstance(data, dict):
        log.warning("  Title block extraction returned non-dict response")
        return {}

    # Log key fields
    log.info(f"    document_number = {data.get('document_number', '?')}")
    log.info(f"    drawing_title   = {data.get('drawing_title', '?')[:60]}")
    log.info(f"    revision        = {data.get('revision', '?')}")
    log.info(f"    unit_prefix     = {data.get('unit_prefix', '?')}")
    log.info(f"    scope_note      = {(data.get('scope_note', '') or '(none)')[:80]}")

    return data
