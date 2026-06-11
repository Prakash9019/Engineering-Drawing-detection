#!/usr/bin/env python3
"""
step2_title_block.py — Title Block Extraction & Revision Intelligence Agent
============================================================================
CDCI P&ID Pipeline — Step 2  (Blueprint Layer 2 + Layer 3 + Layer 4)

What this does
--------------
  Call 1 (Gemini 2.5 Flash — full thumbnail):
    Locates and reads the title block (bottom-right corner, ~20% of drawing).
    Extracts: drawing_number, sheet, revision_code, title, contract_number,
              project_name, scale, orig_dwg_size, discipline, date,
              drawn_by, checked_by, approved_by, continuation_sheet.

  Call 2 (Gemini 2.5 Flash — revision table crop):
    Parses the revision history table (rows: REV | DESCRIPTION | DATE | BY).
    Determines: is_revision_drawing, revision_mode, extraction_scope.

  Programmatic (Layer 3 — Project Type Router):
    Routes drawing to COUNT_ONLY or FULL_EXTRACTION mode based on
    project prefix (LT/GT → count-only, LC/GC → full extraction).

  Programmatic (Layer 4 — Revision Intelligence):
    Rev 0 / Rev A  → NEW_DRAWING    (process entire drawing)
    Rev 1+ / Rev B+ → REVISION_DRAWING (check for clouds)
    Detects explicit revision notice text ("USE THIS DRAWING FOR
    CLOUDED AREAS ONLY") as a hard override → CLOUD_SCOPE_MODE.

Output — drawing_context.json fields added
------------------------------------------
  title_block: {
    drawing_number, sheet_number, revision_code, title, project_name,
    contract_number, scale, orig_dwg_size, discipline, issue_date,
    drawn_by, checked_by, approved_by, continuation_sheet,
    confidence, raw_ocr
  }
  revision_history: [ {rev, description, date, drawn_by}, ... ]
  is_revision_drawing: true | false
  revision_mode: NEW_DRAWING | REVISION_DRAWING | CLOUD_SCOPE_MODE
  extraction_scope: FULL_DRAWING | CLOUD_ONLY | CLOUD_PRIORITY
  project_mode: FULL_EXTRACTION | COUNT_ONLY | UNKNOWN
  revision_cloud_required: true | false    ← feeds Step 5A cloud filter
  title_block_confidence: 0.0-1.0

Usage
-----
  python step2_title_block.py input_drawing.jpg \\
      --out output/ --api-key YOUR_KEY

  # After Step 1 (uses drawing_context.json):
  python step2_title_block.py \\
      --context output/drawing_context.json --api-key YOUR_KEY

  # Debug: saves cropped regions
  python step2_title_block.py input_drawing.jpg \\
      --out output/ --api-key YOUR_KEY --debug
"""

import argparse
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import pytesseract

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Models ─────────────────────────────────────────────────────────────────────
GEMINI_MODEL    = "gemini-2.5-flash"
GEMINI_MAX_SIDE = 3000   # title block needs high res for small text

# ── Title block location heuristics ───────────────────────────────────────────
# Standard P&ID: title block bottom-right ~25% width, ~20% height
# We scan 3 candidate regions in priority order
TITLE_BLOCK_CANDIDATES = [
    {"name": "bottom_right",  "x0": 0.55, "y0": 0.80, "x1": 1.00, "y1": 1.00},
    {"name": "bottom_full",   "x0": 0.00, "y0": 0.82, "x1": 1.00, "y1": 1.00},
    {"name": "bottom_right_xl","x0": 0.45, "y0": 0.75, "x1": 1.00, "y1": 1.00},
]

# ── Revision table is typically LEFT of the title block ───────────────────────
REVISION_TABLE_REGION = {"x0": 0.55, "y0": 0.82, "x1": 0.86, "y1": 1.00}

# ── Revision notice region (contains "CLOUDED AREAS ONLY" text) ──────────────
REVISION_NOTICE_REGION = {"x0": 0.55, "y0": 0.70, "x1": 0.90, "y1": 0.85}

# ── Project mode prefixes (Layer 3) ───────────────────────────────────────────
COUNT_ONLY_PREFIXES   = {"LT", "GT", "LT-", "GT-"}
FULL_EXTRACT_PREFIXES = {"LC", "GC", "LC-", "GC-"}


# ═══════════════════════════════════════════════════════════════════════════════
# Image helpers
# ═══════════════════════════════════════════════════════════════════════════════

def crop_frac(img: np.ndarray, region: dict) -> tuple[np.ndarray, tuple]:
    """Crop fractional region. Returns (crop, (x0,y0,x1,y1) px)."""
    H, W = img.shape[:2]
    x0 = int(region["x0"] * W)
    y0 = int(region["y0"] * H)
    x1 = int(region["x1"] * W)
    y1 = int(region["y1"] * H)
    return img[y0:y1, x0:x1], (x0, y0, x1, y1)


def scale_img(img: np.ndarray, max_side: int = GEMINI_MAX_SIDE) -> np.ndarray:
    H, W = img.shape[:2]
    if max(H, W) <= max_side:
        return img
    scale = max_side / max(H, W)
    return cv2.resize(img, (int(W * scale), int(H * scale)),
                      interpolation=cv2.INTER_AREA)


def encode_jpeg(img: np.ndarray, quality: int = 92) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def clahe_enhance(img: np.ndarray) -> np.ndarray:
    """Apply CLAHE to improve contrast for OCR on faded title blocks."""
    gray  = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    enh   = clahe.apply(gray)
    return cv2.cvtColor(enh, cv2.COLOR_GRAY2BGR)


# ═══════════════════════════════════════════════════════════════════════════════
# Tesseract pre-pass — fast OCR to confirm title block location
# ═══════════════════════════════════════════════════════════════════════════════

def tesseract_title_block_ocr(crop: np.ndarray) -> str:
    """
    Quick Tesseract pass on the title block crop.
    Used to: (a) confirm we have the right region, (b) provide fallback
    text if Gemini call fails, (c) cross-check Gemini output.
    """
    enhanced = clahe_enhance(crop)
    gray     = cv2.cvtColor(enhanced, cv2.COLOR_BGR2GRAY)
    try:
        text = pytesseract.image_to_string(gray, config="--oem 3 --psm 6")
        return text.strip()
    except Exception as e:
        log.warning("Tesseract failed on title block: %s", e)
        return ""


def detect_title_block_region(img: np.ndarray) -> tuple[np.ndarray, dict, tuple]:
    """
    Find the title block by scanning candidate regions.
    Uses Tesseract to confirm presence of 'DWG NO' or 'DRAWING' keywords.
    Returns (crop, region_dict, px_coords).
    """
    kw_must = re.compile(
        r'DWG|DRAWING|TITLE|SCALE|SHEET|SHT|REV\b|REVISION', re.I)
    kw_bonus = re.compile(
        r'DWG NO|SHEET NO|SHT\.|CONT|CONTRACT|DATE|APPROV', re.I)

    best_crop   = None
    best_region = TITLE_BLOCK_CANDIDATES[0]
    best_coords = (0, 0, 0, 0)
    best_score  = -1

    for region in TITLE_BLOCK_CANDIDATES:
        crop, coords = crop_frac(img, region)
        text = tesseract_title_block_ocr(crop)
        score = len(kw_must.findall(text)) + len(kw_bonus.findall(text)) * 2
        log.debug("Region %s: score=%d keywords in OCR", region["name"], score)
        if score > best_score:
            best_score  = score
            best_crop   = crop
            best_region = region
            best_coords = coords

    log.info("Title block region: %s (keyword score=%d)",
             best_region["name"], best_score)
    return best_crop, best_region, best_coords


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini SDK
# ═══════════════════════════════════════════════════════════════════════════════

def _build_gemini_client(api_key: str):
    try:
        import google.genai as genai
        return genai.Client(api_key=api_key), "new"
    except Exception:
        pass
    try:
        import google.generativeai as gl
        gl.configure(api_key=api_key)
        return gl, "legacy"
    except Exception as e:
        raise RuntimeError(f"No Gemini SDK: {e}")


def _gemini_call(client, sdk: str, model: str,
                  img_bytes: bytes, prompt: str,
                  fallback: Optional[str] = None) -> str:
    def _call(m: str) -> str:
        if sdk == "new":
            from google.genai import types as gt
            resp = client.models.generate_content(
                model=m,
                contents=[
                    gt.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                    gt.Part.from_text(text=prompt),
                ],
            )
            return resp.text.strip()
        else:
            import google.generativeai as gl
            import PIL.Image as PILImage, io
            pil = PILImage.open(io.BytesIO(img_bytes))
            return gl.GenerativeModel(m).generate_content([prompt, pil]).text.strip()

    try:
        return _call(model)
    except Exception as e:
        if fallback:
            log.warning("%s failed (%s), trying %s", model, e, fallback)
            return _call(fallback)
        raise


def _parse_json(raw: str) -> dict:
    clean = raw.replace("```json", "").replace("```", "").strip()
    m = re.search(r'\{.*\}', clean, re.DOTALL)
    return json.loads(m.group(0) if m else clean)


# ═══════════════════════════════════════════════════════════════════════════════
# Call 1 — Title Block Extraction
# ═══════════════════════════════════════════════════════════════════════════════

_TITLE_BLOCK_PROMPT = """You are reading the title block of a P&ID engineering drawing.

The title block is the bordered table in the bottom-right of the image.
It contains drawing metadata.

Pre-extracted OCR text for reference (may have errors):
<ocr_text>
{ocr_text}
</ocr_text>

Extract ALL visible fields precisely. Return ONLY a JSON object (no markdown):
{{
  "drawing_number":     "e.g. 4224-MGDV-6-50-2004",
  "sheet_number":       "e.g. 001",
  "continuation_sheet": "e.g. 002 or null if none",
  "revision_code":      "e.g. C or 3 or A",
  "title_line_1":       "first line of drawing title",
  "title_line_2":       "second line or null",
  "title_line_3":       "third line or null",
  "title_line_4":       "fourth line or null",
  "project_name":       "project/facility name",
  "contract_number":    "contract number if visible",
  "client":             "client company name",
  "contractor":         "contractor company name",
  "scale":              "e.g. NTS or 1:200",
  "orig_dwg_size":      "e.g. A1 or A0",
  "discipline":         "e.g. P&ID or MECHANICAL or INSTRUMENTATION",
  "issue_date":         "latest issue date",
  "drawn_by":           "initials or name",
  "checked_by":         "initials or name",
  "approved_by":        "initials or name",
  "document_type":      "P&ID | PFD | ISOMETRIC | GA | OTHER",
  "confidence":         0.0-1.0,
  "fields_unclear":     ["list of field names you could not read clearly"]
}}

Extract exactly what is written. Do not invent or guess missing fields — use null."""


def extract_title_block(crop: np.ndarray, ocr_text: str,
                         client, sdk: str) -> dict:
    """Call 1: Gemini reads the full title block image."""
    enhanced  = clahe_enhance(crop)
    scaled    = scale_img(enhanced)
    img_bytes = encode_jpeg(scaled, quality=94)

    prompt = _TITLE_BLOCK_PROMPT.format(ocr_text=ocr_text[:1500])

    log.info("Call 1: Extracting title block with Gemini %s...", GEMINI_MODEL)
    raw = _gemini_call(client, sdk, GEMINI_MODEL, img_bytes, prompt)

    try:
        result = _parse_json(raw)
        result["raw_gemini_response"] = raw
        log.info(
            "Title block: dwg=%s  sht=%s  rev=%s  confidence=%.2f",
            result.get("drawing_number"), result.get("sheet_number"),
            result.get("revision_code"), result.get("confidence", 0),
        )
        return result
    except json.JSONDecodeError as e:
        log.warning("Title block JSON parse error: %s", e)
        return {
            "parse_error": str(e),
            "raw_gemini_response": raw,
            "confidence": 0.0,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Call 2 — Revision Table + Revision Notice
# ═══════════════════════════════════════════════════════════════════════════════

_REVISION_TABLE_PROMPT = """You are reading the revision history table and any revision notices
from a P&ID engineering drawing.

The revision table has columns: REV | DESCRIPTION | DATE | DRAWN BY | CHECKED | APPROVED.
There may also be a notice box saying something like
"USE THIS DRAWING FOR INFORMATION WITHIN THE CLOUDED AREAS ONLY".

Return ONLY a JSON object (no markdown):
{{
  "revision_history": [
    {{
      "rev":         "C",
      "description": "RE-ISSUED FOR CONSTRUCTION",
      "date":        "29-08-24",
      "drawn_by":    "SJ",
      "checked_by":  "VJD/MSG/SS",
      "approved_by": "BGS/SR"
    }}
  ],
  "current_revision":        "C",
  "current_issue_status":    "RE-ISSUED FOR CONSTRUCTION",
  "revision_notice_present": true,
  "revision_notice_text":    "exact text of the notice box, or null",
  "cloud_scope_only":        true,
  "source_drawing_reference": "e.g. MGDV-6-50-0011 SHT.001 REV.C or null",
  "confidence": 0.95
}}

Extract ALL revision rows, most recent first.
Set cloud_scope_only=true ONLY if the drawing explicitly says to use it
only within clouded areas (e.g. 'USE THIS DRAWING FOR...CLOUDED AREAS ONLY')."""


def extract_revision_table(img: np.ndarray, client, sdk: str) -> dict:
    """Call 2: Gemini reads revision table + revision notice region."""
    H, W = img.shape[:2]

    # Combine revision table + notice into one crop
    rt  = REVISION_TABLE_REGION
    rn  = REVISION_NOTICE_REGION
    x0  = int(min(rt["x0"], rn["x0"]) * W)
    y0  = int(min(rt["y0"], rn["y0"]) * H)
    x1  = int(max(rt["x1"], rn["x1"]) * W)
    y1  = int(max(rt["y1"], rn["y1"]) * H)

    crop      = img[y0:y1, x0:x1]
    enhanced  = clahe_enhance(crop)
    scaled    = scale_img(enhanced)
    img_bytes = encode_jpeg(scaled, quality=93)

    log.info("Call 2: Extracting revision table with Gemini %s...", GEMINI_MODEL)
    raw = _gemini_call(client, sdk, GEMINI_MODEL, img_bytes, _REVISION_TABLE_PROMPT)

    try:
        result = _parse_json(raw)
        result["raw_gemini_response"] = raw
        n_revs = len(result.get("revision_history", []))
        log.info(
            "Revision table: %d revisions | current=%s | cloud_scope=%s",
            n_revs,
            result.get("current_revision"),
            result.get("cloud_scope_only"),
        )
        return result
    except json.JSONDecodeError as e:
        log.warning("Revision table JSON parse error: %s", e)
        return {"parse_error": str(e), "raw_gemini_response": raw, "confidence": 0.0}


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 3 — Project Type Router (programmatic)
# ═══════════════════════════════════════════════════════════════════════════════

def determine_project_mode(drawing_number: str, title: str) -> dict:
    """
    Layer 3: Route to COUNT_ONLY or FULL_EXTRACTION based on
    project prefix in drawing number or title.

    LT/GT → COUNT_ONLY_MODE  (count tags, no full extraction)
    LC/GC → FULL_EXTRACTION_MODE
    Unknown → FLAG for human classification
    """
    candidates = [
        (drawing_number or "").upper().strip(),
        (title or "").upper().strip(),
    ]

    for text in candidates:
        for prefix in COUNT_ONLY_PREFIXES:
            if text.startswith(prefix) or f"-{prefix}-" in text:
                return {
                    "project_mode":        "COUNT_ONLY",
                    "project_prefix":      prefix,
                    "project_mode_reason": f"Drawing number/title starts with {prefix}",
                }
        for prefix in FULL_EXTRACT_PREFIXES:
            if text.startswith(prefix) or f"-{prefix}-" in text:
                return {
                    "project_mode":        "FULL_EXTRACTION",
                    "project_prefix":      prefix,
                    "project_mode_reason": f"Drawing number/title starts with {prefix}",
                }

    return {
        "project_mode":        "FULL_EXTRACTION",    # default: extract everything
        "project_prefix":      None,
        "project_mode_reason": "No LT/GT/LC/GC prefix found — defaulting to FULL_EXTRACTION",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 4 — Revision Intelligence Engine (programmatic)
# ═══════════════════════════════════════════════════════════════════════════════

# Revision codes that signal a NEW (first-issue) drawing
_NEW_DRAWING_CODES = {"0", "A", "00", "REV0", "REVA", "P0", "IFI", "IFA",
                       "P1", "IFR", "IFC-A", "AFC-A"}


def determine_revision_mode(rev_code: str,
                              cloud_scope_only: bool,
                              revision_notice_text: Optional[str]) -> dict:
    """
    Layer 4: Classify drawing as NEW, REVISION, or CLOUD_SCOPE.

    Decision tree (from Blueprint):
      1. cloud_scope_only flag OR notice text contains "CLOUDED AREAS"
         → CLOUD_SCOPE_MODE (highest priority — explicit override)
      2. rev_code in NEW_DRAWING_CODES (0, A, IFI, IFA...)
         → NEW_DRAWING
      3. anything else (B+, C+, 1+, 2+)
         → REVISION_DRAWING → cloud detection required
    """
    code = (rev_code or "").strip().upper()

    # 1. Explicit cloud-scope notice (hard override)
    notice_triggers_cloud = False
    if cloud_scope_only:
        notice_triggers_cloud = True
    if revision_notice_text:
        notice_upper = revision_notice_text.upper()
        if "CLOUDED AREAS" in notice_upper or "CLOUD" in notice_upper:
            notice_triggers_cloud = True

    if notice_triggers_cloud:
        return {
            "is_revision_drawing":      True,
            "revision_mode":            "CLOUD_SCOPE_MODE",
            "extraction_scope":         "CLOUD_ONLY",
            "revision_cloud_required":  True,
            "scope_reason": (
                "Explicit revision notice: drawing must be used only within clouded areas"
            ),
        }

    # 2. First-issue drawing
    if code in _NEW_DRAWING_CODES:
        return {
            "is_revision_drawing":      False,
            "revision_mode":            "NEW_DRAWING",
            "extraction_scope":         "FULL_DRAWING",
            "revision_cloud_required":  False,
            "scope_reason":             f"Revision code '{code}' is a first-issue revision",
        }

    # 3. Revision drawing (B+, C, 1, 2, ...)
    return {
        "is_revision_drawing":      True,
        "revision_mode":            "REVISION_DRAWING",
        "extraction_scope":         "CLOUD_PRIORITY",
        "revision_cloud_required":  True,
        "scope_reason": (
            f"Revision code '{code}' indicates a revision drawing — "
            "cloud detection required to determine extraction scope"
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def run_title_block_extraction(
    img_path:   str,
    out_dir:    str,
    api_key:    str,
    debug:      bool = False,
    drawing_context_path: Optional[str] = None,
) -> dict:

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    img = cv2.imread(img_path)
    if img is None:
        raise FileNotFoundError(f"Cannot read: {img_path}")
    H, W = img.shape[:2]
    log.info("Drawing: %dx%d", W, H)

    client, sdk = _build_gemini_client(api_key)
    log.info("Gemini client ready (%s SDK)", sdk)

    # ── Locate title block ────────────────────────────────────────────────────
    log.info("=== Locating title block ===")
    tb_crop, tb_region, tb_coords = detect_title_block_region(img)

    # Tesseract pre-pass for cross-checking
    ocr_text = tesseract_title_block_ocr(tb_crop)
    log.info("Tesseract pre-pass: %d chars", len(ocr_text))

    if debug:
        cv2.imwrite(str(out / "debug_title_block_crop.jpg"),
                    tb_crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        with open(str(out / "debug_title_block_ocr.txt"), "w") as f:
            f.write(ocr_text)

    # ── Call 1: Extract title block fields ────────────────────────────────────
    log.info("=== Call 1: Title Block Extraction ===")
    tb_data = extract_title_block(tb_crop, ocr_text, client, sdk)

    # ── Call 2: Extract revision table + notice ───────────────────────────────
    log.info("=== Call 2: Revision Table Extraction ===")
    rev_data = extract_revision_table(img, client, sdk)

    if debug:
        rt_x0 = int(min(REVISION_TABLE_REGION["x0"], REVISION_NOTICE_REGION["x0"]) * W)
        rt_y0 = int(min(REVISION_TABLE_REGION["y0"], REVISION_NOTICE_REGION["y0"]) * H)
        rt_x1 = int(max(REVISION_TABLE_REGION["x1"], REVISION_NOTICE_REGION["x1"]) * W)
        rt_y1 = int(max(REVISION_TABLE_REGION["y1"], REVISION_NOTICE_REGION["y1"]) * H)
        cv2.imwrite(str(out / "debug_revision_table_crop.jpg"),
                    img[rt_y0:rt_y1, rt_x0:rt_x1],
                    [cv2.IMWRITE_JPEG_QUALITY, 90])

    # ── Layer 3: Project mode routing ─────────────────────────────────────────
    log.info("=== Layer 3: Project Mode Router ===")
    dwg_num  = tb_data.get("drawing_number") or ""
    title    = " ".join(filter(None, [
        tb_data.get("title_line_1"),
        tb_data.get("title_line_2"),
        tb_data.get("title_line_3"),
        tb_data.get("title_line_4"),
    ]))
    proj_mode = determine_project_mode(dwg_num, title)
    log.info("Project mode: %s (%s)", proj_mode["project_mode"],
             proj_mode["project_mode_reason"])

    # ── Layer 4: Revision intelligence ────────────────────────────────────────
    log.info("=== Layer 4: Revision Intelligence ===")
    rev_code   = (tb_data.get("revision_code")
                  or rev_data.get("current_revision")
                  or "")
    cloud_flag = bool(rev_data.get("cloud_scope_only"))
    notice_txt = rev_data.get("revision_notice_text")
    rev_mode   = determine_revision_mode(rev_code, cloud_flag, notice_txt)
    log.info("Revision mode: %s | scope: %s | cloud_required: %s",
             rev_mode["revision_mode"],
             rev_mode["extraction_scope"],
             rev_mode["revision_cloud_required"])

    # ── Assemble full title block context ─────────────────────────────────────
    title_full = " | ".join(filter(None, [
        tb_data.get("title_line_1"),
        tb_data.get("title_line_2"),
        tb_data.get("title_line_3"),
        tb_data.get("title_line_4"),
    ]))

    tb_confidence = float(tb_data.get("confidence") or 0.0)
    if tb_confidence < 0.85:
        log.warning("Title block confidence %.2f < 0.85 — some fields may be incorrect",
                    tb_confidence)

    context = {
        # ── Title block fields ────────────────────────────────────────────────
        "title_block": {
            "drawing_number":     tb_data.get("drawing_number"),
            "sheet_number":       tb_data.get("sheet_number"),
            "continuation_sheet": tb_data.get("continuation_sheet"),
            "revision_code":      rev_code,
            "title":              title_full,
            "title_line_1":       tb_data.get("title_line_1"),
            "title_line_2":       tb_data.get("title_line_2"),
            "title_line_3":       tb_data.get("title_line_3"),
            "title_line_4":       tb_data.get("title_line_4"),
            "project_name":       tb_data.get("project_name"),
            "contract_number":    tb_data.get("contract_number"),
            "client":             tb_data.get("client"),
            "contractor":         tb_data.get("contractor"),
            "scale":              tb_data.get("scale"),
            "orig_dwg_size":      tb_data.get("orig_dwg_size"),
            "discipline":         tb_data.get("discipline"),
            "document_type":      tb_data.get("document_type"),
            "issue_date":         tb_data.get("issue_date"),
            "drawn_by":           tb_data.get("drawn_by"),
            "checked_by":         tb_data.get("checked_by"),
            "approved_by":        tb_data.get("approved_by"),
            "confidence":         tb_confidence,
            "fields_unclear":     tb_data.get("fields_unclear", []),
            "tb_region_used":     tb_region["name"],
            "tb_bbox_px":         list(tb_coords),
        },
        # ── Revision history ──────────────────────────────────────────────────
        "revision_history":         rev_data.get("revision_history", []),
        "current_revision":         rev_data.get("current_revision", rev_code),
        "current_issue_status":     rev_data.get("current_issue_status"),
        "revision_notice_present":  rev_data.get("revision_notice_present", False),
        "revision_notice_text":     notice_txt,
        "source_drawing_reference": rev_data.get("source_drawing_reference"),
        # ── Layer 3 + 4 routing ───────────────────────────────────────────────
        "project_mode":             proj_mode["project_mode"],
        "project_prefix":           proj_mode.get("project_prefix"),
        "is_revision_drawing":      rev_mode["is_revision_drawing"],
        "revision_mode":            rev_mode["revision_mode"],
        "extraction_scope":         rev_mode["extraction_scope"],
        "revision_cloud_required":  rev_mode["revision_cloud_required"],
        "scope_reason":             rev_mode["scope_reason"],
        "title_block_confidence":   tb_confidence,
    }

    # ── Write title_block_context.json ────────────────────────────────────────
    tb_path = str(out / "title_block_context.json")
    export  = {k: v for k, v in context.items()
               if k not in {"raw_gemini_response"}}
    with open(tb_path, "w") as f:
        json.dump(export, f, indent=2)
    log.info("✓ title_block_context.json → %s", tb_path)

    # ── Update drawing_context.json ───────────────────────────────────────────
    ctx_path = drawing_context_path or str(out / "drawing_context.json")
    if Path(ctx_path).exists():
        with open(ctx_path) as f:
            dctx = json.load(f)
    else:
        dctx = {"input_file": img_path, "image_size": [W, H]}

    dctx.update({
        "title_block_context_path": tb_path,
        "drawing_number":           context["title_block"]["drawing_number"],
        "sheet_number":             context["title_block"]["sheet_number"],
        "revision_code":            rev_code,
        "document_type":            context["title_block"]["document_type"],
        "discipline":               context["title_block"]["discipline"],
        "drawing_title":            title_full,
        "is_revision_drawing":      context["is_revision_drawing"],
        "revision_mode":            context["revision_mode"],
        "extraction_scope":         context["extraction_scope"],
        "revision_cloud_required":  context["revision_cloud_required"],
        "project_mode":             context["project_mode"],
        "title_block_confidence":   tb_confidence,
    })

    with open(ctx_path, "w") as f:
        json.dump(dctx, f, indent=2)
    log.info("✓ drawing_context.json updated → %s", ctx_path)

    return context


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Step 2: Title Block Extraction & Revision Intelligence")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("image",    nargs="?", help="Drawing image (JPG/PNG/TIFF)")
    g.add_argument("--context",           help="drawing_context.json from Step 1")
    parser.add_argument("--out",      default="output")
    parser.add_argument("--api-key",  help="Gemini API key")
    parser.add_argument("--debug",    action="store_true",
                        help="Save title block and revision table debug crops")
    args = parser.parse_args()

    api_key = (args.api_key
               or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY"))
    if not api_key:
        parser.error("Gemini API key required. Set GEMINI_API_KEY or --api-key")

    img_path = args.image
    ctx_file = None
    if args.context:
        ctx_file = args.context
        with open(args.context) as f:
            ctx = json.load(f)
        img_path = ctx.get("raster_path") or ctx.get("input_file")
        if not img_path:
            parser.error("drawing_context.json has no raster_path or input_file")
        log.info("Image from context: %s", img_path)

    result = run_title_block_extraction(
        img_path=img_path,
        out_dir=args.out,
        api_key=api_key,
        debug=args.debug,
        drawing_context_path=ctx_file,
    )

    tb = result["title_block"]

    print(f"\n=== Step 2 Complete — Title Block & Revision Intelligence ===")
    print(f"\n  Drawing Number  : {tb.get('drawing_number')}")
    print(f"  Sheet           : {tb.get('sheet_number')}  (cont. {tb.get('continuation_sheet')})")
    print(f"  Revision        : {tb.get('revision_code')}")
    print(f"  Title           : {result.get('title_block', {}).get('title_line_1')}")
    if tb.get("title_line_2"):
        print(f"                    {tb['title_line_2']}")
    print(f"  Discipline      : {tb.get('discipline')}")
    print(f"  Document Type   : {tb.get('document_type')}")
    print(f"  Issue Date      : {tb.get('issue_date')}")
    print(f"  Confidence      : {tb.get('confidence'):.2f}")
    if tb.get("fields_unclear"):
        print(f"  ⚠ Unclear fields: {', '.join(tb['fields_unclear'])}")

    print(f"\n  Revision History ({len(result.get('revision_history', []))} revisions):")
    for rev in result.get("revision_history", [])[:5]:
        print(f"    Rev {rev.get('rev','?')}: {rev.get('description','?')[:45]:45}  {rev.get('date','')}")

    print(f"\n  ── Layer 3 + 4 Routing ──")
    print(f"  Project mode    : {result.get('project_mode')}")
    print(f"  Revision mode   : {result.get('revision_mode')}")
    print(f"  Extraction scope: {result.get('extraction_scope')}")
    print(f"  Cloud required  : {result.get('revision_cloud_required')}")
    print(f"  Reason          : {result.get('scope_reason')}")

    if result.get("revision_notice_present"):
        print(f"\n  ⚠ REVISION NOTICE: {result.get('revision_notice_text','')[:80]}")

    print(f"\n  Output files:")
    print(f"    {args.out}/title_block_context.json")
    print(f"    {args.out}/drawing_context.json  (updated)")
    if args.debug:
        print(f"    {args.out}/debug_title_block_crop.jpg")
        print(f"    {args.out}/debug_revision_table_crop.jpg")
        print(f"    {args.out}/debug_title_block_ocr.txt")


if __name__ == "__main__":
    main()