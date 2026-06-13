#!/usr/bin/env python3
"""
step6_table_agent.py — Table Extraction Agent (Layer 9)
========================================================
CDCI P&ID Pipeline — Step 6

What this does
--------------
Detects and extracts ALL tabular structures from a P&ID drawing into
structured JSON. Tables on P&IDs include:

  • Tag List (Valves & Instruments) — most common, usually top-left
  • Equipment Schedule / Datasheet tables
  • Line List tables
  • Valve Schedule tables
  • Legend / Symbol tables

Pipeline (2 Gemini calls per table region):
  ┌──────────────────────────────────────────────────────────────┐
  │  Stage A: Table Region Detection (Gemini 2.5 Flash)          │
  │  Send 5 candidate tiles → get bboxes of all table regions    │
  └──────────────────────────┬───────────────────────────────────┘
                             │ per detected region
  ┌──────────────────────────▼───────────────────────────────────┐
  │  Stage B: Classical Grid Analysis (OpenCV)                   │
  │  Line detection → cell segmentation → Tesseract OCR          │
  └──────────────────────────┬───────────────────────────────────┘
                             │ raw OCR text + grid
  ┌──────────────────────────▼───────────────────────────────────┐
  │  Stage C: Structured Extraction (Gemini 2.5 Flash)           │
  │  Image + OCR text → JSON rows, headers, table_type           │
  └──────────────────────────┬───────────────────────────────────┘
                             │
  ┌──────────────────────────▼───────────────────────────────────┐
  │  Output: tables_context.json + drawing_context.json update   │
  └──────────────────────────────────────────────────────────────┘

Known P&ID table locations (priority order):
  1. Top-left  (0–20% Y, 0–80% X) — Tag lists, most common
  2. Top strip (0–12% Y, full X)  — Wide tag lists spanning full width
  3. Right margin (60–100% X)     — Equipment schedules, line lists
  4. Full scan                    — Fallback for unusual layouts

Usage
-----
    python step6_table_agent.py drawing.jpg --out output/ --api-key YOUR_KEY
    python step6_table_agent.py drawing.jpg --out output/ --api-key KEY --debug
    # Use context from Step 1:
    python step6_table_agent.py --context output/drawing_context.json --api-key KEY
    # Use notes rules from Step 3:
    python step6_table_agent.py drawing.jpg --out output/ \\
        --api-key KEY --rules output/rules_prompt_block.txt
"""

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import pytesseract

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Models ─────────────────────────────────────────────────────────────────────
GEMINI_FLASH_MODEL   = "gemini-2.5-flash"
GEMINI_PRO_MODEL     = "gemini-2.5-pro"
GEMINI_MAX_SIDE      = 4096

# ── Fallback scan tiles (used when binary not available for adaptive detection) ──
# (name, x0_frac, y0_frac, x1_frac, y1_frac, priority, description)
TABLE_SCAN_TILES_FALLBACK = [
    ("top_full",     0.000, 0.000, 1.000, 0.260, 1, "Full-width top 26% — primary tag list"),
    ("right_margin", 0.600, 0.000, 1.000, 0.800, 2, "Right margin: equipment schedules"),
    ("upper_half",   0.000, 0.000, 1.000, 0.500, 3, "Upper half: fallback scan"),
    ("full_drawing", 0.000, 0.000, 1.000, 1.000, 4, "Full drawing: last resort"),
]

# ── Table type taxonomy ─────────────────────────────────────────────────────────
TABLE_TYPES = {
    "tag_list":          "TAG LIST — Valves, Instruments, Equipment with tag numbers",
    "equipment_schedule":"EQUIPMENT SCHEDULE — Equipment specifications and parameters",
    "line_list":         "LINE LIST — Pipe line numbers, sizes, specs, service",
    "valve_schedule":    "VALVE SCHEDULE — Valve specifications and actuator data",
    "instrument_index":  "INSTRUMENT INDEX — Instrument tag, type, range, service",
    "legend":            "LEGEND — Symbol definitions and abbreviations",
    "nozzle_schedule":   "NOZZLE SCHEDULE — Equipment nozzle parameters",
    "revision_table":    "REVISION TABLE — Drawing revision history",
    "unknown":           "UNKNOWN — Table type could not be determined",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Image utilities
# ═══════════════════════════════════════════════════════════════════════════════

def clahe_enhance(gray: np.ndarray) -> np.ndarray:
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def crop_region(img: np.ndarray, x0f: float, y0f: float,
                x1f: float, y1f: float) -> tuple[np.ndarray, tuple]:
    """Crop fractional region. Returns (crop, (x0,y0,x1,y1) in px)."""
    H, W = img.shape[:2]
    x0, y0 = int(x0f * W), int(y0f * H)
    x1, y1 = int(x1f * W), int(y1f * H)
    return img[y0:y1, x0:x1], (x0, y0, x1, y1)


def scale_for_gemini(img: np.ndarray, max_side: int = GEMINI_MAX_SIDE) -> np.ndarray:
    H, W = img.shape[:2]
    if max(H, W) <= max_side:
        return img
    scale = max_side / max(H, W)
    return cv2.resize(img, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_AREA)


def encode_jpeg(img: np.ndarray, quality: int = 92) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def upscale_to(img: np.ndarray, min_short_side: int = 800) -> np.ndarray:
    H, W = img.shape[:2]
    short = min(H, W)
    if short >= min_short_side:
        return img
    scale = min_short_side / short
    return cv2.resize(img, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_CUBIC)


def scale_for_table(img: np.ndarray,
                    min_height: int = 700,
                    max_side: int = 7000) -> np.ndarray:
    """
    Scale a table crop for Gemini, ensuring minimum height for readability.
    Wide tables (tag lists) are typically short — upscale to min_height
    so small tag numbers are legible. Only downscale if truly oversized.
    """
    H, W = img.shape[:2]
    if H < min_height:
        scale = min_height / H
        nW, nH = int(W * scale), min_height
        if nW <= max_side:
            return cv2.resize(img, (nW, nH), interpolation=cv2.INTER_CUBIC)
        # Width would exceed max_side — scale to fit width instead
        scale = max_side / W
        return cv2.resize(img, (max_side, int(H * scale)), interpolation=cv2.INTER_CUBIC)
    if max(H, W) > max_side:
        scale = max_side / max(H, W)
        return cv2.resize(img, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_AREA)
    return img


def detect_table_bottom(img: np.ndarray, search_top_pct: float = 0.40) -> int:
    """
    Find the bottom y-pixel of the top table by locating the last
    dense horizontal line (row density > 25% of width) in the top
    search_top_pct fraction of the image.
    Returns the y-pixel just below the table, with a small buffer.
    """
    H, W = img.shape[:2]
    gray     = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    clahe    = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    _, binary = cv2.threshold(enhanced, 0, 255,
                               cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    search_end = int(H * search_top_pct)
    row_proj   = np.sum(binary > 0, axis=1).astype(float) / W
    k          = np.ones(3) / 3
    smoothed   = np.convolve(row_proj, k, mode='same')

    last_line_y = int(H * 0.06)   # minimum — skip tiny top strips
    for y in range(int(H * 0.02), search_end):
        if smoothed[y] > 0.25:    # dense row = table border or dense text row
            last_line_y = y

    buffer = max(30, int(H * 0.008))
    return min(H, last_line_y + buffer)


# ═══════════════════════════════════════════════════════════════════════════════
# Classical grid detection (OpenCV)
# ═══════════════════════════════════════════════════════════════════════════════

def detect_table_grid(crop_bgr: np.ndarray) -> dict:
    """
    Detect table grid structure using morphological line detection.
    Returns metrics about the grid to help confirm it's a table.
    """
    gray    = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    enhanced = clahe_enhance(gray)
    _, binary = cv2.threshold(enhanced, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    H, W = binary.shape

    # Horizontal lines: kernel width = 5% of image width, minimum
    h_len = max(int(W * 0.05), 30)
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (h_len, 1))
    horiz_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel)

    # Vertical lines: kernel height = 2% of image height
    v_len = max(int(H * 0.02), 15)
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_len))
    vert_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel)

    # Count distinct horizontal and vertical lines
    h_contours, _ = cv2.findContours(horiz_lines, cv2.RETR_EXTERNAL,
                                     cv2.CHAIN_APPROX_SIMPLE)
    v_contours, _ = cv2.findContours(vert_lines, cv2.RETR_EXTERNAL,
                                     cv2.CHAIN_APPROX_SIMPLE)

    # Filter to significant lines only (> 10% of dimension)
    sig_h = [c for c in h_contours if cv2.boundingRect(c)[2] > W * 0.10]
    sig_v = [c for c in v_contours if cv2.boundingRect(c)[3] > H * 0.05]

    # Grid score: tables have many horizontal + vertical intersections
    grid_score = len(sig_h) * len(sig_v)
    is_table   = len(sig_h) >= 2 and len(sig_v) >= 3

    # Get Y-positions of horizontal lines (sorted) = row boundaries
    row_ys = sorted([int(cv2.boundingRect(c)[1] + cv2.boundingRect(c)[3] / 2)
                     for c in sig_h])
    # Get X-positions of vertical lines = column boundaries
    col_xs = sorted([int(cv2.boundingRect(c)[0] + cv2.boundingRect(c)[2] / 2)
                     for c in sig_v])

    # Deduplicate nearby lines (within 5px)
    def _dedup(vals: list[int], gap: int = 5) -> list[int]:
        if not vals:
            return []
        out = [vals[0]]
        for v in vals[1:]:
            if v - out[-1] > gap:
                out.append(v)
        return out

    row_ys = _dedup(row_ys, gap=max(3, H // 80))
    col_xs = _dedup(col_xs, gap=max(3, W // 80))

    return {
        "is_table":        is_table,
        "grid_score":      grid_score,
        "h_line_count":    len(sig_h),
        "v_line_count":    len(sig_v),
        "row_boundaries":  row_ys,
        "col_boundaries":  col_xs,
        "estimated_rows":  max(0, len(row_ys) - 1),
        "estimated_cols":  max(0, len(col_xs) - 1),
    }


def tesseract_table_ocr(crop_bgr: np.ndarray) -> str:
    """
    OCR optimised for table content: upscale, CLAHE, PSM 6 (block of text).
    Returns cleaned multi-line string.
    """
    upscaled = upscale_to(crop_bgr, min_short_side=900)
    gray     = cv2.cvtColor(upscaled, cv2.COLOR_BGR2GRAY)
    enhanced = clahe_enhance(gray)
    denoised = cv2.fastNlMeansDenoising(enhanced, h=8)

    try:
        text = pytesseract.image_to_string(denoised, config="--oem 3 --psm 6")
        lines = [l.rstrip() for l in text.splitlines() if l.strip()]
        return "\n".join(lines)
    except Exception as e:
        log.warning("Tesseract OCR failed: %s", e)
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini SDK wrapper
# ═══════════════════════════════════════════════════════════════════════════════

def _build_gemini_client(api_key: str):
    try:
        import google.genai as genai
        client = genai.Client(api_key=api_key)
        return client, "new"
    except Exception:
        pass
    try:
        import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=api_key)
        return genai_legacy, "legacy"
    except Exception as e:
        raise RuntimeError(f"No working Gemini SDK: {e}")


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
            import PIL.Image as PILImage
            import io
            pil = PILImage.open(io.BytesIO(img_bytes))
            resp = gl.GenerativeModel(m).generate_content([prompt, pil])
            return resp.text.strip()
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
    if m:
        clean = m.group(0)
    return json.loads(clean)


# ═══════════════════════════════════════════════════════════════════════════════
# Stage A — Table region detection
# ═══════════════════════════════════════════════════════════════════════════════

_DETECT_PROMPT = """You are analyzing a region of a P&ID engineering drawing for table structures.

Identify ALL tabular structures visible. A table has:
- A header row with column names
- Multiple data rows below the header
- Grid lines (horizontal and/or vertical) separating cells

Common P&ID table types: TAG LIST, EQUIPMENT SCHEDULE, LINE LIST,
VALVE SCHEDULE, LEGEND, INSTRUMENT INDEX.

Return ONLY a JSON object (no markdown):
{
  "tables_found": true | false,
  "table_count": 0,
  "tables": [
    {
      "table_id": "T1",
      "table_type": "tag_list | equipment_schedule | line_list | valve_schedule | instrument_index | legend | revision_table | unknown",
      "title": "exact title text as written on drawing (e.g. TABLE-1: TAG LIST)",
      "location": {
        "x0_frac": 0.0,
        "y0_frac": 0.0,
        "x1_frac": 1.0,
        "y1_frac": 1.0,
        "description": "e.g. top-left corner, rows 1-8"
      },
      "estimated_rows": 6,
      "estimated_cols": 12,
      "has_header_row": true,
      "header_labels": ["SLOT #", "BV-XX1", "BV-XX2", "..."],
      "confidence": 0.9,
      "notes": "any observations"
    }
  ]
}

If no tables: set tables_found=false, tables=[]."""


def detect_table_regions(img: np.ndarray, client, sdk: str,
                         scan_tiles: list) -> list[dict]:
    """
    Stage A: scan candidate tiles and ask Gemini to locate table regions.
    Returns list of detected table dicts with absolute pixel coords.
    """
    H, W = img.shape[:2]
    all_tables: list[dict] = []
    seen_titles: set = set()

    for name, x0f, y0f, x1f, y1f, priority, desc in scan_tiles:
        crop, (cx0, cy0, cx1, cy1) = crop_region(img, x0f, y0f, x1f, y1f)
        cH, cW = crop.shape[:2]

        if cW < 100 or cH < 30:
            continue

        log.info("Scanning tile [%d] %s (%dx%d)...", priority, name, cW, cH)

        scaled    = scale_for_gemini(crop)
        img_bytes = encode_jpeg(scaled)

        try:
            raw    = _gemini_call(client, sdk, GEMINI_FLASH_MODEL, img_bytes,
                                  _DETECT_PROMPT)
            parsed = _parse_json(raw)
        except Exception as e:
            log.warning("Detection failed on tile %s: %s", name, e)
            continue

        if not parsed.get("tables_found"):
            log.info("  No tables in %s", name)
            continue

        for tbl in parsed.get("tables", []):
            title = (tbl.get("title") or "").strip()

            # Deduplicate: skip if same title already found in higher-priority tile
            title_key = re.sub(r'\s+', ' ', title.upper())
            if title_key and title_key in seen_titles:
                log.info("  Dedup: skipping duplicate '%s'", title)
                continue

            # Convert fractional coords (relative to tile) to absolute px
            loc   = tbl.get("location", {})
            tx0f  = loc.get("x0_frac", 0.0)
            ty0f  = loc.get("y0_frac", 0.0)
            tx1f  = loc.get("x1_frac", 1.0)
            ty1f  = loc.get("y1_frac", 1.0)

            # Absolute in tile, then back to full image
            abs_x0 = cx0 + int(tx0f * cW)
            abs_y0 = cy0 + int(ty0f * cH)
            abs_x1 = cx0 + int(tx1f * cW)
            abs_y1 = cy0 + int(ty1f * cH)

            # Pad 2% on each side to avoid clipping
            pad_x = int(0.01 * W)
            pad_y = int(0.01 * H)
            abs_x0 = max(0, abs_x0 - pad_x)
            abs_y0 = max(0, abs_y0 - pad_y)
            abs_x1 = min(W, abs_x1 + pad_x)
            abs_y1 = min(H, abs_y1 + pad_y)

            tbl["abs_bbox"]    = [abs_x0, abs_y0, abs_x1, abs_y1]
            tbl["source_tile"] = name
            tbl["source_priority"] = priority

            all_tables.append(tbl)
            if title_key:
                seen_titles.add(title_key)

            log.info("  Found: '%s' (%s) bbox=[%d,%d,%d,%d]",
                     title, tbl.get("table_type"), abs_x0, abs_y0, abs_x1, abs_y1)

    return all_tables


# ═══════════════════════════════════════════════════════════════════════════════
# Stage C — Structured extraction from a single table crop
# ═══════════════════════════════════════════════════════════════════════════════

def _make_extract_prompt(table_type: str, title: str,
                         estimated_rows: int, estimated_cols: int,
                         ocr_text: str, grid_info: dict,
                         rules_context: str) -> str:
    rules_section = ""
    if rules_context.strip():
        rules_section = f"""
The following drawing-specific rules apply to tag interpretation:
<drawing_rules>
{rules_context[:1500]}
</drawing_rules>
"""

    ocr_section = ""
    if ocr_text.strip():
        ocr_section = f"""
OCR pre-extracted text (use as guide; image is ground truth):
<ocr_text>
{ocr_text[:4000]}
</ocr_text>
"""

    grid_section = (
        f"Grid analysis: ~{grid_info.get('estimated_rows',0)} rows, "
        f"~{grid_info.get('estimated_cols',0)} cols, "
        f"h_lines={grid_info.get('h_line_count',0)}, "
        f"v_lines={grid_info.get('v_line_count',0)}"
    )

    return f"""You are extracting a complete table from a P&ID engineering drawing.

Table metadata:
  - Title: {title or 'Unknown'}
  - Type:  {table_type} ({TABLE_TYPES.get(table_type, '')})
  - Estimated: {estimated_rows} rows × {estimated_cols} cols
  - {grid_section}
{rules_section}{ocr_section}
Extract every row and column. For tag_list tables, each row is one SLOT
(a gas lift manifold slot or similar), and each column is one instrument
or valve type. The cell value is the actual tag number (e.g. BV-0001).

Return ONLY a JSON object (no markdown):
{{
  "table_id":   "T1",
  "table_type": "{table_type}",
  "title":      "exact title",
  "headers":    ["col1", "col2", "..."],
  "header_row_index": 0,
  "rows": [
    {{
      "row_index": 0,
      "row_label": "SLOT 19",
      "cells": {{
        "BV-XX1": "BV-0001",
        "BV-XX2": "BV-0002",
        "CC-XX1": "CC-0079",
        "FIT-XX1": "FIT-0018",
        "...": "..."
      }},
      "confidence": 0.95
    }}
  ],
  "all_tag_numbers": ["BV-0001", "BV-0002", "..."],
  "tag_type_summary": {{
    "BV": 12,
    "FIT": 6,
    "FCV": 6,
    "...": 0
  }},
  "table_notes": "any observations (partially obscured, merged cells, etc.)",
  "extraction_confidence": 0.0-1.0,
  "partially_extracted": false
}}

Be thorough — extract ALL rows and ALL columns. If a cell is empty or
illegible, use null. Never skip rows."""


def _extract_wide_table(crop: np.ndarray, table_meta: dict,
                         grid_info: dict, ocr_text: str,
                         rules_context: str, client, sdk: str) -> dict:
    """
    Extract a wide table (aspect ratio > 2.5) by splitting into left and right
    halves with a 5% overlap, extracting each at higher resolution, then merging.
    This ensures small tag numbers in all columns are legible.
    """
    cH, cW = crop.shape[:2]
    mid     = int(cW * 0.50)
    overlap = int(cW * 0.05)

    halves = [
        ("left",  crop[:, :mid + overlap]),
        ("right", crop[:, mid - overlap:]),
    ]

    half_results: list[dict] = []
    all_rows: dict[str, dict] = {}   # row_label → merged cells
    all_headers: list[str]    = []
    all_tags: list[str]       = []

    ttype = table_meta.get("table_type", "unknown")
    title = table_meta.get("title", "")

    for side, half_crop in halves:
        scaled    = scale_for_table(half_crop, min_height=700)
        img_bytes = encode_jpeg(scaled, quality=95)

        # OCR this half
        half_ocr = tesseract_table_ocr(half_crop)

        prompt = _make_extract_prompt(
            table_type     = ttype,
            title          = f"{title} [{side} half]",
            estimated_rows = grid_info.get("estimated_rows", 0),
            estimated_cols = max(1, grid_info.get("estimated_cols", 0) // 2),
            ocr_text       = half_ocr,
            grid_info      = grid_info,
            rules_context  = rules_context,
        )
        try:
            raw    = _gemini_call(client, sdk, GEMINI_FLASH_MODEL, img_bytes,
                                  prompt, fallback=GEMINI_PRO_MODEL)
            parsed = _parse_json(raw)
            half_results.append(parsed)

            # Collect headers
            for h in (parsed.get("headers") or []):
                if h and h not in all_headers:
                    all_headers.append(h)

            # Merge rows by row_label
            for row in (parsed.get("rows") or []):
                label = str(row.get("row_label") or row.get("row_index") or "")
                if label not in all_rows:
                    all_rows[label] = dict(row)
                else:
                    # Merge cells from this half into existing row
                    existing_cells = all_rows[label].get("cells") or {}
                    new_cells      = row.get("cells") or {}
                    existing_cells.update(new_cells)
                    all_rows[label]["cells"] = existing_cells

            all_tags.extend(parsed.get("all_tag_numbers") or [])
            log.info("  Wide table %s half: %d rows", side, len(parsed.get("rows", [])))
        except Exception as e:
            log.warning("  Wide table %s half failed: %s", side, e)

    # Build merged result
    merged_rows = sorted(all_rows.values(),
                         key=lambda r: (int(re.search(r'\d+', str(r.get("row_label","0"))).group())
                                        if re.search(r'\d+', str(r.get("row_label",""))) else 999))
    unique_tags = list(dict.fromkeys(all_tags))   # preserve order, deduplicate

    base = half_results[0] if half_results else {}
    return {
        "table_type":           ttype,
        "title":                title,
        "headers":              all_headers,
        "rows":                 merged_rows,
        "all_tag_numbers":      unique_tags,
        "tag_type_summary":     base.get("tag_type_summary", {}),
        "table_notes":          f"Wide table split into halves; {base.get('table_notes','')}",
        "extraction_confidence": base.get("extraction_confidence", 0.8),
        "partially_extracted":  base.get("partially_extracted", False),
        "wide_table_split":     True,
    }


def extract_table_contents(img: np.ndarray, table_meta: dict,
                            client, sdk: str,
                            rules_context: str = "") -> dict:
    """
    Stage B+C: OpenCV grid analysis + Gemini structured extraction.
    Returns fully structured table dict.
    """
    x0, y0, x1, y1 = table_meta["abs_bbox"]
    crop = img[y0:y1, x0:x1]
    cH, cW = crop.shape[:2]

    if cW < 30 or cH < 10:
        return {"error": f"Table crop too small: {cW}x{cH}"}

    log.info("Extracting table '%s' from crop %dx%d...",
             table_meta.get("title", "?"), cW, cH)

    # Stage B: classical grid analysis
    grid_info = detect_table_grid(crop)
    log.info("  Grid: %d rows × %d cols (score=%d, is_table=%s)",
             grid_info["estimated_rows"], grid_info["estimated_cols"],
             grid_info["grid_score"], grid_info["is_table"])

    # Stage B: Tesseract OCR
    ocr_text = tesseract_table_ocr(crop)
    log.info("  OCR: %d chars", len(ocr_text))

    # Stage C: Gemini structured extraction
    # Wide tables (tag lists) are split into left/right halves so each half
    # gets sufficient resolution for small tag numbers to be legible.
    aspect = cW / max(cH, 1)
    if aspect > 2.5 and cW > 2000:
        result = _extract_wide_table(crop, table_meta, grid_info, ocr_text,
                                     rules_context, client, sdk)
    else:
        scaled    = scale_for_table(crop)
        img_bytes = encode_jpeg(scaled, quality=95)
        prompt    = _make_extract_prompt(
            table_type     = table_meta.get("table_type", "unknown"),
            title          = table_meta.get("title", ""),
            estimated_rows = table_meta.get("estimated_rows",
                             grid_info["estimated_rows"]),
            estimated_cols = table_meta.get("estimated_cols",
                             grid_info["estimated_cols"]),
            ocr_text       = ocr_text,
            grid_info      = grid_info,
            rules_context  = rules_context,
        )
        try:
            raw    = _gemini_call(client, sdk, GEMINI_FLASH_MODEL, img_bytes,
                                  prompt, fallback=GEMINI_PRO_MODEL)
            result = _parse_json(raw)
        except json.JSONDecodeError as e:
            log.warning("JSON parse failed, returning partial: %s", e)
            result = {
                "table_type":  table_meta.get("table_type", "unknown"),
                "title":       table_meta.get("title", ""),
                "parse_error": str(e),
                "ocr_text":    ocr_text,
            }
        except Exception as e:
            log.error("Gemini extraction failed: %s", e)
            result = {"error": str(e), "ocr_text": ocr_text}

    # Enrich with grid metadata
    result["grid_analysis"]  = grid_info
    result["abs_bbox"]       = table_meta["abs_bbox"]
    result["source_tile"]    = table_meta.get("source_tile")
    result["ocr_char_count"] = len(ocr_text)
    result["gemini_model"]   = GEMINI_FLASH_MODEL

    # Summary stats
    rows = result.get("rows", [])
    tags = result.get("all_tag_numbers", [])
    log.info("  Extracted: %d rows, %d tags, confidence=%.2f",
             len(rows), len(tags),
             result.get("extraction_confidence", 0))

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Post-processing: flatten all tags into a master tag list
# ═══════════════════════════════════════════════════════════════════════════════

def build_master_tag_list(tables: list[dict]) -> list[dict]:
    """
    Flatten all extracted tables into a unified master tag list.
    Each entry: {tag_number, table_title, table_type, row_label,
                  column_header, confidence}
    Deduplicated by tag_number.
    """
    seen: dict[str, dict] = {}

    for table in tables:
        ttype  = table.get("table_type", "unknown")
        ttitle = table.get("title", "")
        rows   = table.get("rows", [])

        for row in rows:
            row_label = str(row.get("row_label") or "")
            row_conf  = row.get("confidence") or 0.8
            cells     = row.get("cells") or {}

            for col_header, tag_val in cells.items():
                if not tag_val or tag_val in (None, "null", "", "-", "—"):
                    continue
                tag_str = str(tag_val).strip()
                if not tag_str or len(tag_str) < 3:
                    continue

                entry = {
                    "tag_number":    tag_str,
                    "table_title":   ttitle,
                    "table_type":    ttype,
                    "row_label":     row_label,
                    "column_header": str(col_header),
                    "confidence":    row_conf,
                }

                # Keep the higher-confidence entry when deduplicating
                key = tag_str.upper()
                if key not in seen or row_conf > (seen[key].get("confidence") or 0):
                    seen[key] = entry

    master = sorted(seen.values(), key=lambda x: x["tag_number"])
    return master


def build_tag_type_summary(master_tags: list[dict]) -> dict:
    """Count tags by type prefix (BV, FIT, FCV, etc.)."""
    summary: dict[str, int] = {}
    for entry in master_tags:
        tag = entry["tag_number"]
        m = re.match(r'^([A-Z]{1,4})', tag.upper())
        if m:
            prefix = m.group(1)
            summary[prefix] = summary.get(prefix, 0) + 1
    return dict(sorted(summary.items(), key=lambda x: -x[1]))


# ═══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def run_table_agent(
    img_path: str,
    out_dir: str,
    api_key: str,
    drawing_context_path: Optional[str] = None,
    rules_context: str = "",
    debug: bool = False,
) -> dict:

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    img = cv2.imread(img_path)
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {img_path}")
    H, W = img.shape[:2]
    log.info("Loaded drawing: %dx%d", W, H)

    client, sdk = _build_gemini_client(api_key)
    log.info("Gemini client ready (%s SDK)", sdk)

    # ── Build adaptive scan tiles ──────────────────────────────────────────────
    # Detect the actual bottom of the top table so we crop exactly the right area.
    table_bottom_px  = detect_table_bottom(img)
    top_frac         = min(table_bottom_px / H + 0.03, 0.45)
    log.info("Adaptive table top boundary: y=%d (%.1f%% of image height)",
             table_bottom_px, table_bottom_px / H * 100)

    scan_tiles = [
        ("top_adaptive", 0.0, 0.0, 1.0, top_frac, 1,
         f"Full-width adaptive top ({top_frac*100:.0f}%) — primary table"),
        ("right_margin",  0.600, 0.000, 1.000, 0.800, 2, "Right margin: equipment schedules"),
        ("upper_half",    0.000, 0.000, 1.000, 0.500, 3, "Upper half: fallback scan"),
        ("full_drawing",  0.000, 0.000, 1.000, 1.000, 4, "Full drawing: last resort"),
    ]

    # ── Stage A: Detect table regions ─────────────────────────────────────────
    log.info("=== Stage A: Table Region Detection ===")
    detected_tables = detect_table_regions(img, client, sdk, scan_tiles)
    log.info("Stage A complete: %d table region(s) detected", len(detected_tables))

    if not detected_tables:
        log.warning("No tables detected — check drawing or try --debug")
        tables_ctx = {
            "version":          "v1",
            "input_image":      img_path,
            "image_size":       [W, H],
            "tables_detected":  0,
            "tables":           [],
            "master_tag_list":  [],
            "tag_type_summary": {},
        }
    else:
        # ── Stage B+C: Extract each table ─────────────────────────────────────
        log.info("=== Stage B+C: Grid Analysis + Structured Extraction ===")
        extracted: list[dict] = []

        for i, tbl_meta in enumerate(detected_tables):
            log.info("Processing table %d/%d: '%s'",
                     i + 1, len(detected_tables),
                     tbl_meta.get("title", "?"))
            result = extract_table_contents(img, tbl_meta, client, sdk, rules_context)

            if debug:
                x0, y0, x1, y1 = tbl_meta["abs_bbox"]
                crop_path = str(out / f"debug_table_{i+1}_{tbl_meta.get('table_type','unk')}.jpg")
                cv2.imwrite(crop_path, img[y0:y1, x0:x1], [cv2.IMWRITE_JPEG_QUALITY, 90])
                ocr_path  = str(out / f"debug_ocr_table_{i+1}.txt")
                with open(ocr_path, "w") as f:
                    f.write(result.get("ocr_text", "") if "ocr_text" in result
                            else f"OCR chars: {result.get('ocr_char_count',0)}")
                log.info("  Saved debug crops: %s", crop_path)

            extracted.append(result)

        # ── Build master tag list ──────────────────────────────────────────────
        master_tags    = build_master_tag_list(extracted)
        tag_summary    = build_tag_type_summary(master_tags)

        log.info("Master tag list: %d unique tags across %d tables",
                 len(master_tags), len(extracted))
        if tag_summary:
            log.info("Tag type summary: %s",
                     ", ".join(f"{k}={v}" for k, v in list(tag_summary.items())[:8]))

        # ── Save annotated image (bboxes of all detected tables) ──────────────
        if debug:
            annotated = img.copy()
            colours   = [(0, 255, 0), (255, 0, 0), (0, 0, 255), (255, 165, 0)]
            for i, tbl in enumerate(detected_tables):
                x0, y0, x1, y1 = tbl["abs_bbox"]
                col = colours[i % len(colours)]
                cv2.rectangle(annotated, (x0, y0), (x1, y1), col, 6)
                label = tbl.get("title", f"Table {i+1}")[:40]
                cv2.putText(annotated, label, (x0 + 10, y0 + 50),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.5, col, 3)
            scale   = 1600 / W
            ann_sm  = cv2.resize(annotated, (1600, int(H * scale)))
            ann_path = str(out / "debug_table_regions.jpg")
            cv2.imwrite(ann_path, ann_sm, [cv2.IMWRITE_JPEG_QUALITY, 85])
            log.info("Saved annotated overview: %s", ann_path)

        tables_ctx = {
            "version":           "v1",
            "input_image":       img_path,
            "image_size":        [W, H],
            "tables_detected":   len(detected_tables),
            "tables_extracted":  len(extracted),
            "tables":            extracted,
            "master_tag_list":   master_tags,
            "master_tag_count":  len(master_tags),
            "tag_type_summary":  tag_summary,
        }

    # ── Write tables_context.json ──────────────────────────────────────────────
    tables_path = str(out / "tables_context.json")
    with open(tables_path, "w") as f:
        json.dump(tables_ctx, f, indent=2)
    log.info("✓ tables_context.json → %s", tables_path)

    # ── Write master_tags.json (flat list — easy downstream consumption) ───────
    master_path = str(out / "master_tags.json")
    with open(master_path, "w") as f:
        json.dump(tables_ctx.get("master_tag_list", []), f, indent=2)
    log.info("✓ master_tags.json (%d tags) → %s",
             len(tables_ctx.get("master_tag_list", [])), master_path)

    # ── Update drawing_context.json ────────────────────────────────────────────
    ctx_path = drawing_context_path or str(out / "drawing_context.json")
    if Path(ctx_path).exists():
        with open(ctx_path) as f:
            dctx = json.load(f)
        dctx["tables_context_path"] = tables_path
        dctx["master_tags_path"]    = master_path
        dctx["tables_summary"] = {
            "tables_detected":  tables_ctx["tables_detected"],
            "total_tags":       len(tables_ctx.get("master_tag_list", [])),
            "tag_type_summary": tables_ctx.get("tag_type_summary", {}),
        }
        with open(ctx_path, "w") as f:
            json.dump(dctx, f, indent=2)
        log.info("✓ drawing_context.json updated")

    return tables_ctx


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Step 6: Table extraction agent — detects and parses all "
                    "tabular structures from a P&ID drawing")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("image", nargs="?", help="P&ID drawing (JPG/PNG/TIFF)")
    group.add_argument("--context", help="drawing_context.json from Step 1")

    parser.add_argument("--out",     default="output", help="Output directory")
    parser.add_argument("--api-key", help="Gemini API key")
    parser.add_argument("--rules",   help="Path to rules_prompt_block.txt from Step 3")
    parser.add_argument("--debug",   action="store_true",
                        help="Save debug crops, OCR text, annotated overview")
    args = parser.parse_args()

    api_key = (args.api_key
               or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY"))
    if not api_key:
        parser.error("Gemini API key required. Set GEMINI_API_KEY or pass --api-key")

    # Resolve image path
    img_path = args.image
    ctx_file = None
    if args.context:
        ctx_file = args.context
        with open(args.context) as f:
            ctx = json.load(f)
        img_path = ctx.get("raster_path") or ctx.get("input_file")
        if not img_path:
            parser.error("drawing_context.json has no raster_path or input_file")
        log.info("Image from drawing_context.json: %s", img_path)

    # Load drawing rules from Step 3 (optional)
    rules_context = ""
    if args.rules and Path(args.rules).exists():
        with open(args.rules) as f:
            rules_context = f.read()
        log.info("Loaded rules context: %d chars from %s", len(rules_context), args.rules)
    else:
        # Auto-detect from output dir
        auto_rules = Path(args.out) / "rules_prompt_block.txt"
        if auto_rules.exists():
            with open(auto_rules) as f:
                rules_context = f.read()
            log.info("Auto-loaded rules from %s (%d chars)", auto_rules, len(rules_context))

    result = run_table_agent(
        img_path=img_path,
        out_dir=args.out,
        api_key=api_key,
        drawing_context_path=ctx_file,
        rules_context=rules_context,
        debug=args.debug,
    )

    print("\n=== Step 6 Complete — Table Extraction ===")
    print(f"  Tables detected    : {result.get('tables_detected', 0)}")
    print(f"  Tables extracted   : {result.get('tables_extracted', result.get('tables_detected', 0))}")
    print(f"  Master tag count   : {result.get('master_tag_count', len(result.get('master_tag_list',[])))}")
    print()

    tag_sum = result.get("tag_type_summary", {})
    if tag_sum:
        print("  Tag type breakdown:")
        for prefix, count in list(tag_sum.items())[:12]:
            print(f"    {prefix:<8} {count:>4} tags")
    print()

    for i, tbl in enumerate(result.get("tables", [])):
        rows = tbl.get("rows", [])
        tags = tbl.get("all_tag_numbers", [])
        print(f"  Table {i+1}: '{tbl.get('title','?')}'")
        print(f"    Type:       {tbl.get('table_type','?')}")
        print(f"    Rows:       {len(rows)}")
        print(f"    Tags:       {len(tags)}")
        print(f"    Confidence: {tbl.get('extraction_confidence', '?')}")
        print()

    print(f"  Output files:")
    print(f"    {args.out}/tables_context.json  (full extraction)")
    print(f"    {args.out}/master_tags.json      (flat tag list)")
    if args.debug:
        print(f"    {args.out}/debug_table_*.jpg   (per-table crops)")
        print(f"    {args.out}/debug_table_regions.jpg (annotated overview)")


if __name__ == "__main__":
    main()
    