#!/usr/bin/env python3
"""
step5a_live_annotator.py — Live Tag Detection + Instant Bbox Annotation
========================================================================
CDCI P&ID Pipeline — Step 5A (v2, separate file)

What this does differently from step5a_candidate_extraction.py
--------------------------------------------------------------
Detect + Draw happens in ONE step, per patch, as it runs.
As soon as a patch finishes Gemini extraction, the bboxes are drawn
on the FULL drawing canvas immediately — no second visualizer pass needed.

You can watch the annotated drawing build up patch by patch in real time.

Architecture
------------
  • Same SAHI grid, same Gemini calls, same Tesseract, same filters
  • One shared in-memory canvas (full drawing copy)
  • Each worker: detect → filter → draw bbox ON canvas → save candidates
  • After all patches: save final annotated full-res image + tiles
  • Also saves per-patch annotated crops

Bbox drawing rules (fixed from v1 issues)
------------------------------------------
  1. Box drawn around TAG TEXT (tag_bbox), not symbol bubble (symbol_bbox)
  2. Clean thin border (2px at full res)  — drawing remains readable
  3. Tag text as white-on-colour pill — small, tight, no dark overlay
  4. NO category prefix, NO confidence numbers on label
  5. Same tag text within 400px → only ONE box drawn (intra-step dedup)
  6. Drawing references / node IDs / spec codes → filtered out, no box

Colour scheme
-------------
  instrument → GREEN   (0,200,0)
  valve      → ORANGE  (0,120,255)
  equipment  → PURPLE  (200,0,200)
  piping     → CYAN    (200,200,0)
  unknown    → GREY    (128,128,128)

Outputs
-------
  step5a_live_candidates.json       — cleaned candidate records (JSON)
  step5a_live_annotated.jpg         — full drawing with all bboxes drawn
  step5a_live_annotated_fullres.jpg — full resolution version
  step5a_live_tiles/                — zoomable tile grid (2000×2000px each)
  step5a_live_patches/              — per-patch annotated crops (--debug)

Usage
-----
  # Same interface as step5a_candidate_extraction.py:
  python step5a_live_annotator.py drawing.jpg \\
      --out output/ --api-key KEY --workers 8

  python step5a_live_annotator.py \\
      --context output/drawing_context.json --api-key KEY

  python step5a_live_annotator.py drawing.jpg \\
      --out output/ --api-key KEY --patch 19   # single patch test
"""

import argparse
import json
import logging
import math
import os
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import pytesseract

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Models ─────────────────────────────────────────────────────────────────────
GEMINI_MODEL    = "gemini-2.5-pro"
GEMINI_FALLBACK = "gemini-2.5-flash"
TEMPERATURE     = 0.0
GEMINI_MAX_SIDE = 1024

# ── SAHI parameters ────────────────────────────────────────────────────────────
SAHI_PATCH_SIZE = 1024
SAHI_OVERLAP    = 0.25

# ── Dedup distance: same tag within this px radius → one box only ──────────────
INTRA_DEDUP_DIST = 400.0

# ── Bbox colour palette (BGR for OpenCV) ───────────────────────────────────────
CAT_COLOURS = {
    "instrument": (  0, 200,   0),   # green
    "valve":      (  0, 120, 255),   # orange
    "equipment":  (200,   0, 200),   # purple
    "piping":     (200, 200,   0),   # cyan
    "unknown":    (128, 128, 128),   # grey
}

# ── ISA tag regex ──────────────────────────────────────────────────────────────
ISA_TAG_RE = re.compile(
    r'\b([A-Z]{1,5}-[A-Z0-9]{1,6}-?[0-9]{2,6}[A-Z]?'
    r'|[A-Z]{1,4}-[0-9]{3,6}[A-Z]?'
    r'|[0-9]{1,4}["\-][A-Z]{2,5}-[A-Z0-9]+-[A-Z0-9]+)\b'
)

# ── False positive patterns ────────────────────────────────────────────────────
_FP_DRAWING_REF = re.compile(
    r'^\d{4}-[A-Z]{2,5}-\d-\d{2}-'
    r'|^[A-Z]*GDV-\d-'
    r'|^[A-Z]*DV-\d-\d{2}-'
    r'|^MGDTY-'
    r'|^ES\.0\.'
    r'|^CORP-ENG-',
    re.I
)
_FP_BARE_NODE = re.compile(
    r'^I-\d{3}$'
    r'|^LC$|^RCI$|^HS$|^SS$'
    r'|^\d{4,5}[A-Z]?$'
    r'|^C\d{2}[A-Z]$'
    r'|^[A-Z]{1,2}$',
    re.I
)


def _is_false_positive(tag: str) -> bool:
    tag = (tag or "").strip()
    if len(tag) < 3:
        return True
    if _FP_DRAWING_REF.search(tag):
        return True
    if _FP_BARE_NODE.match(tag):
        return True
    return False


# ── Tag normalization (post-OCR clean-up) ──────────────────────────────────────
# Double-prime / inch marks that may appear after a pipe size digit. Includes the
# straight ASCII forms ("" and ") plus the unicode prime / quote variants that
# OCR and copy-paste frequently emit.
_INCH_MARKS = r"(?:''|\"|''|´´|′′|″|”|’’)"
_INCH_RE = re.compile(r'(\d+(?:\.\d+)?)\s*' + _INCH_MARKS)


def _normalize_tag(tag: str) -> str:
    """
    Deterministic post-OCR normalization for an extracted tag string.

    Applies, in order:
      3. Inch notation   — 2'' / 2" / 6'' → 2IN / 6IN (double-prime = inches)
      4. Dash collapse   — FZ--208 / V---FZ-208 → FZ-208 / V-FZ-208
      5. Consistent form — uppercase, single dash separators, no stray spaces

    NOTE: removal of false OCR characters (e.g. the spurious "I" in V-FZI-208 that
    comes from the square symbol border) and inclusion of the leading "V" unit
    prefix are handled at the Gemini-prompt level — "I" is a legitimate ISA letter
    (FI, PI, FIT) so it can never be blindly stripped here without losing real tags.
    """
    if not tag:
        return tag
    t = tag.strip().upper()
    # 3. Inch notation → IN
    t = _INCH_RE.sub(r'\1IN', t)
    # 5. Normalize separators: collapse whitespace around dashes into a dash
    t = re.sub(r'\s*-\s*', '-', t)
    # join a leading unit letter that lost its dash, e.g. "V FZ-208" → "V-FZ-208"
    t = re.sub(r'^([A-Z])\s+(?=[A-Z])', r'\1-', t)
    # 4. Collapse runs of dashes into one
    t = re.sub(r'-{2,}', '-', t)
    # tidy: drop leading/trailing dashes and collapse remaining whitespace
    t = re.sub(r'\s+', ' ', t).strip().strip('-')
    return t


# ═══════════════════════════════════════════════════════════════════════════════
# SAHI tiler (identical to step5a)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_sahi_patches(img: np.ndarray,
                           patch_size: int = SAHI_PATCH_SIZE,
                           overlap: float = SAHI_OVERLAP) -> list[dict]:
    H, W   = img.shape[:2]
    stride = int(patch_size * (1 - overlap))
    patches, pid = [], 0
    y = 0
    while y < H:
        x = 0
        while x < W:
            x1 = min(x + patch_size, W)
            y1 = min(y + patch_size, H)
            patches.append({
                "patch_id":  pid,
                "x_offset":  x, "y_offset":  y,
                "x1":        x1, "y1":        y1,
                "patch_w":   x1 - x, "patch_h": y1 - y,
                "crop":      img[y:y1, x:x1],
            })
            pid += 1
            if x1 == W: break
            x += stride
        if y1 == H: break
        y += stride
    log.info("SAHI: %d patches from %dx%d (patch=%d, overlap=%.0f%%)",
             len(patches), W, H, patch_size, overlap * 100)
    return patches


def priority_sort_patches(patches: list[dict], H: int, W: int) -> list[dict]:
    cx, cy = W / 2, H / 2
    return sorted(patches, key=lambda p: (
        abs((p["x_offset"] + p["x1"]) / 2 - cx) +
        abs((p["y_offset"] + p["y1"]) / 2 - cy)
    ))


# ═══════════════════════════════════════════════════════════════════════════════
# Tesseract OCR (identical to step5a)
# ═══════════════════════════════════════════════════════════════════════════════

def tesseract_extract_tags(crop_bgr: np.ndarray) -> list[dict]:
    gray     = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    clahe    = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    H, W = enhanced.shape
    if min(H, W) < 400:
        scale    = 400 / min(H, W)
        enhanced = cv2.resize(enhanced, (int(W * scale), int(H * scale)),
                              interpolation=cv2.INTER_CUBIC)
        sf = scale
    else:
        sf = 1.0
    try:
        data = pytesseract.image_to_data(
            enhanced, config="--oem 3 --psm 11",
            output_type=pytesseract.Output.DICT,
        )
    except Exception as e:
        log.warning("Tesseract failed: %s", e)
        return []
    results = []
    for i in range(len(data["text"])):
        text = str(data["text"][i]).strip()
        conf = int(data["conf"][i]) if str(data["conf"][i]).lstrip("-").isdigit() else -1
        if conf < 40 or not text or not ISA_TAG_RE.search(text):
            continue
        x = int(data["left"][i] / sf);   y = int(data["top"][i] / sf)
        w = int(data["width"][i] / sf);  h = int(data["height"][i] / sf)
        results.append({"text": text, "ocr_conf": conf / 100.0,
                         "bbox_patch": {"x1": x, "y1": y, "x2": x+w, "y2": y+h}})
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini SDK (identical to step5a)
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


def _make_extraction_prompt(drawing_rules: str, ocr_tags: list[dict],
                             patch_id: int, revision_cloud_present: bool) -> str:
    ocr_section = ""
    if ocr_tags:
        ocr_lines = [f"  {t['text']} (conf={t['ocr_conf']:.2f})" for t in ocr_tags[:30]]
        ocr_section = ("OCR pre-extracted text (deterministic — use as ground truth):\n"
                       + "\n".join(ocr_lines) + "\n\n")
    rules_section = ""
    if drawing_rules.strip():
        rules_section = ("Drawing-specific rules:\n"
                         + drawing_rules[:800] + "\n\n")
    cloud_note = ""
    if revision_cloud_present:
        cloud_note = "NOTE: REVISION DRAWING — only extract inside revision cloud boundaries.\n\n"

    return f"""You are an expert P&ID data extraction agent (ISA 5.1).
Temperature=0.0. Never hallucinate. Only extract what is VISUALLY PRESENT.

{cloud_note}{rules_section}{ocr_section}
[Patch {patch_id}] Scan top-left → bottom-right.

DETECT:
  - Instrument bubbles (circles: FIT, PT, TT, LT, FCV, etc.)
  - Valves (bow-tie, gate, ball, butterfly, control valve)
  - Pumps, compressors, heat exchangers, vessels, tanks
  - Corrosion probes/coupons, analyzers, mechanical equipment
  - Restriction orifices, thermowells, sight glasses

FOR EACH SYMBOL: find the tag text visually nearest to it (leader line or direct label).
Record BOTH the symbol bounding box AND the tag text bounding box separately.
The tag_bbox must be around the actual text letters — not the symbol shape.

READING THE TAG TEXT — avoid these common mistakes:
  - Read ONLY the printed letters/digits INSIDE the symbol. The square, diamond
    or circle OUTLINE strokes are NOT characters. A vertical box edge next to
    "FZ" is often misread as an extra "I" (giving "FZI"); a diagonal as "T"; a
    corner as "L". Do NOT append such border-induced letters. e.g. a flow switch
    reading "FZ / 208" inside a boxed diamond is "FZ-208", never "FZI-208".
  - LEADING UNIT PREFIX: many tags have a separate "V" symbol/letter immediately
    to the LEFT of the instrument bubble. This "V" is the unit/area prefix and is
    PART of the tag. Always include it: read "V-FZ-208", not "FZ-208"; read
    "V-FZD-208", not "FZD-208". If this drawing's unit prefix is "V-" and the
    bubble shows only "FZ / 208", prepend it → "V-FZ-208".
  - Pipe sizes use inches: write the double-prime as IN (2'' → 2IN, 6'' → 6IN).
  - Use a single dash between segments (PREFIX-CODE-NUMBER), never "--".

IGNORE — do NOT extract:
  - Notes and annotations
  - Drawing reference numbers (e.g. 4224-MGDV-6-50-2002-001)
  - Off-drawing reference arrows and destination text
  - Bare node codes without loop numbers (bare I-004, I-001, LC, RCI)
  - Piping spec codes (e.g. C06B, 61440X)
  - Pipe size notations (2\", 12\"x10\", 600#)
  - Text fragments under 4 characters
  - Equipment descriptions that are titles not tags

Return ONLY a JSON object (no markdown):
{{
  "patch_id": {patch_id},
  "candidates": [
    {{
      "symbol_name": "Flow Indicating Transmitter",
      "symbol_category": "instrument|valve|equipment|piping|unknown",
      "tag_text": "FIT-207",
      "symbol_bbox": {{"x1": 120, "y1": 80, "x2": 160, "y2": 120}},
      "tag_bbox":    {{"x1": 165, "y1": 82, "x2": 210, "y2": 98}},
      "vision_confidence": 0.95,
      "tag_source": "ocr|vision|both"
    }}
  ],
  "patch_summary": "5 instruments, 3 valves detected"
}}

If NO valid tags found: return candidates=[].
NEVER assign a tag to a symbol without visual evidence."""


def _gemini_extract(crop_bgr: np.ndarray, patch_id: int, client, sdk: str,
                    drawing_rules: str, ocr_tags: list[dict],
                    revision_cloud_present: bool) -> dict:
    H, W = crop_bgr.shape[:2]
    if max(H, W) > GEMINI_MAX_SIDE:
        scale = GEMINI_MAX_SIDE / max(H, W)
        crop_bgr = cv2.resize(crop_bgr, (int(W * scale), int(H * scale)))
    ok, buf = cv2.imencode(".jpg", crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        return {"patch_id": patch_id, "candidates": [], "error": "encode failed"}
    img_bytes = buf.tobytes()
    prompt    = _make_extraction_prompt(drawing_rules, ocr_tags,
                                        patch_id, revision_cloud_present)
    try:
        if sdk == "new":
            from google.genai import types as gt
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    gt.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                    gt.Part.from_text(text=prompt),
                ],
                config=gt.GenerateContentConfig(temperature=TEMPERATURE),
            )
            raw = resp.text.strip()
        else:
            import google.generativeai as gl, PIL.Image as PILImage, io
            pil = PILImage.open(io.BytesIO(img_bytes))
            cfg = gl.GenerationConfig(temperature=TEMPERATURE)
            raw = gl.GenerativeModel(GEMINI_MODEL).generate_content(
                [prompt, pil], generation_config=cfg).text.strip()

        clean = raw.replace("```json", "").replace("```", "").strip()
        m = re.search(r'\{.*\}', clean, re.DOTALL)
        return json.loads(m.group(0) if m else clean)
    except json.JSONDecodeError as e:
        return {"patch_id": patch_id, "candidates": [], "parse_error": str(e)}
    except Exception as e:
        return {"patch_id": patch_id, "candidates": [], "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# SOW + cloud filters (identical to step5a)
# ═══════════════════════════════════════════════════════════════════════════════

def _point_in_cloud(px: float, py: float, cloud_regions: list[dict]) -> bool:
    for c in cloud_regions:
        if c.get("x0", 0) <= px <= c.get("x1", 0) \
                and c.get("y0", 0) <= py <= c.get("y1", 0):
            return True
    return False


def _apply_sow(symbol_name: str, sow_memory: dict) -> str:
    if not sow_memory:
        return "UNSPECIFIED"
    blocked = {n.upper() for n in sow_memory.get("blocked_names", [])}
    allowed = {n.upper() for n in sow_memory.get("allowed_names", [])}
    sym_up  = re.sub(r'\s+', ' ', symbol_name.strip().upper())
    if sym_up in blocked:
        return "OUT_OF_SCOPE"
    if sym_up in allowed:
        return "IN_SCOPE"
    q_words = set(sym_up.split())
    for name_set, status in [(blocked, "OUT_OF_SCOPE"), (allowed, "IN_SCOPE")]:
        for name in name_set:
            n_words = set(name.split())
            if len(q_words) > 0 and len(q_words & n_words) / max(len(q_words), len(n_words)) >= 0.6:
                return status
    return "UNSPECIFIED"


# ═══════════════════════════════════════════════════════════════════════════════
# Live bbox drawing on shared canvas
# ═══════════════════════════════════════════════════════════════════════════════

def _draw_bbox_on_canvas(canvas: np.ndarray, candidate: dict,
                          img_w: int, draw_lock: threading.Lock) -> None:
    """
    Draw one bounding box + tag label directly on the full-drawing canvas.
    Called immediately after each candidate is accepted — LIVE drawing.

    Box is drawn around tag_bbox (the text location).
    Falls back to symbol_bbox if tag_bbox is empty/zero-size.
    """
    # Choose bbox: tag text location first
    tb = candidate.get("tag_bbox") or {}
    sb = candidate.get("symbol_bbox") or {}

    def _valid(b):
        return (b.get("x2", 0) > b.get("x1", 0)
                and b.get("y2", 0) > b.get("y1", 0))

    bbox = tb if _valid(tb) else sb
    if not _valid(bbox):
        return

    x1, y1 = int(bbox["x1"]), int(bbox["y1"])
    x2, y2 = int(bbox["x2"]), int(bbox["y2"])
    H_img, W_img = canvas.shape[:2]

    # Clamp to image bounds
    x1 = max(0, min(x1, W_img - 1));  x2 = max(0, min(x2, W_img - 1))
    y1 = max(0, min(y1, H_img - 1));  y2 = max(0, min(y2, H_img - 1))
    if x2 <= x1 or y2 <= y1:
        return

    cat    = (candidate.get("symbol_category") or "unknown").lower()
    colour = CAT_COLOURS.get(cat, CAT_COLOURS["unknown"])
    tag    = (candidate.get("tag_text") or "").strip()

    # Scale constants relative to image width (reference: 8000px)
    scale      = W_img / 8000
    box_thick  = max(2, int(scale * 2))
    lbl_scale  = max(0.35, scale * 0.52)

    with draw_lock:
        # ── Thin coloured border ───────────────────────────────────────────────
        cv2.rectangle(canvas, (x1, y1), (x2, y2), colour, box_thick)

        # ── Tag label pill ─────────────────────────────────────────────────────
        if tag:
            font = cv2.FONT_HERSHEY_SIMPLEX
            (lw, lh), baseline = cv2.getTextSize(tag, font, lbl_scale, 1)
            pad = max(2, int(scale * 3))

            # Position: top-left inside the box
            lx = x1 + pad
            ly = y1 + lh + pad

            # Clamp label
            if lx + lw + pad > W_img:
                lx = max(0, W_img - lw - pad * 2)
            if ly + baseline > H_img:
                ly = max(lh + pad, y1 - pad)

            # Filled colour pill background
            bx0 = max(0, lx - pad)
            by0 = max(0, ly - lh - pad)
            bx1 = min(W_img, lx + lw + pad)
            by1 = min(H_img, ly + baseline + 1)
            cv2.rectangle(canvas, (bx0, by0), (bx1, by1), colour, -1)

            # White text on coloured pill
            cv2.putText(canvas, tag, (lx, ly),
                        font, lbl_scale, (255, 255, 255), 1, cv2.LINE_AA)


# ═══════════════════════════════════════════════════════════════════════════════
# Process one patch: detect → filter → draw → return candidates
# ═══════════════════════════════════════════════════════════════════════════════

def _process_patch_live(
    patch:                  dict,
    total:                  int,
    client,
    sdk:                    str,
    drawing_rules:          str,
    sow_memory:             dict,
    cloud_regions:          list,
    revision_cloud_present: bool,
    canvas:                 np.ndarray,
    draw_lock:              threading.Lock,
    out_dir:                Path,
    debug:                  bool,
) -> list[dict]:
    """
    Full per-patch pipeline:
      1. Tesseract OCR
      2. Gemini extraction
      3. Per-candidate: coord conversion + filters + LIVE bbox draw
      4. (optional) save annotated patch crop
    """
    pid       = patch["patch_id"]
    crop      = patch["crop"]
    x_off     = patch["x_offset"]
    y_off     = patch["y_offset"]

    # ── Tesseract ──────────────────────────────────────────────────────────────
    ocr_tags = tesseract_extract_tags(crop)

    # ── Gemini ─────────────────────────────────────────────────────────────────
    result    = _gemini_extract(crop, pid, client, sdk,
                                 drawing_rules, ocr_tags, revision_cloud_present)
    raw_count = len(result.get("candidates", []))
    log.info("Patch %3d/%d [%d,%d] Tess=%d Gemini=%d | %s",
             pid, total - 1, x_off, y_off,
             len(ocr_tags), raw_count,
             result.get("patch_summary", ""))

    candidates_out: list[dict] = []

    for raw in result.get("candidates", []):
        # ── Bbox: local → global ───────────────────────────────────────────────
        sb = raw.get("symbol_bbox") or {}
        tb = raw.get("tag_bbox")    or {}
        sym_bbox = {
            "x1": x_off + int(sb.get("x1") or 0),
            "y1": y_off + int(sb.get("y1") or 0),
            "x2": x_off + int(sb.get("x2") or 0),
            "y2": y_off + int(sb.get("y2") or 0),
        }
        tag_bbox = {
            "x1": x_off + int(tb.get("x1") or 0),
            "y1": y_off + int(tb.get("y1") or 0),
            "x2": x_off + int(tb.get("x2") or 0),
            "y2": y_off + int(tb.get("y2") or 0),
        }

        # ── Revision cloud filter ──────────────────────────────────────────────
        scope_type = "FULL_DRAWING"
        if revision_cloud_present and cloud_regions:
            scope_type = "REVISION_CLOUD"
            cx = (sym_bbox["x1"] + sym_bbox["x2"]) / 2
            cy = (sym_bbox["y1"] + sym_bbox["y2"]) / 2
            if not _point_in_cloud(cx, cy, cloud_regions):
                continue

        # ── Reconcile tag text: OCR is ground truth ────────────────────────────
        gemini_tag = str(raw.get("tag_text") or "").strip()
        if not gemini_tag and not ocr_tags:
            continue

        best_ocr, best_score, ocr_conf = None, 0.0, 0.0
        for ot in ocr_tags:
            ot_text = ot["text"]
            if ot_text.upper() == gemini_tag.upper():
                best_ocr, ocr_conf, best_score = ot_text, ot["ocr_conf"], 1.0
                break
            min_len = min(len(ot_text), len(gemini_tag))
            if min_len >= 3:
                pm = sum(1 for a, b in zip(ot_text.upper(), gemini_tag.upper())
                         if a == b) / max(len(gemini_tag), 1)
                if pm > best_score and pm >= 0.7:
                    best_score, best_ocr, ocr_conf = pm, ot_text, ot["ocr_conf"]

        final_tag = best_ocr if (best_ocr and best_score >= 0.8) else gemini_tag
        # Deterministic clean-up: inch notation, dash collapse, consistent format
        final_tag = _normalize_tag(final_tag)

        # ── SOW filter ─────────────────────────────────────────────────────────
        symbol_name = str(raw.get("symbol_name") or "").strip()
        sow_status  = _apply_sow(symbol_name, sow_memory)
        if sow_status == "OUT_OF_SCOPE":
            continue

        # ── False positive filter ──────────────────────────────────────────────
        if _is_false_positive(final_tag):
            continue

        candidate = {
            "candidate_id":      str(uuid.uuid4())[:8],
            "patch_id":          pid,
            "symbol_name":       symbol_name,
            "symbol_category":   str(raw.get("symbol_category") or "unknown"),
            "tag_text":          final_tag,
            "tag_text_gemini":   gemini_tag,
            "tag_text_ocr":      best_ocr,
            "symbol_bbox":       sym_bbox,
            "tag_bbox":          tag_bbox,
            "scope_type":        scope_type,
            "sow_status":        sow_status,
            "ocr_confidence":    round(ocr_conf, 3),
            "vision_confidence": float(raw.get("vision_confidence") or 0.0)
                                  if raw.get("vision_confidence") is not None else 0.0,
            "tag_source":        str(raw.get("tag_source") or "vision"),
        }
        candidates_out.append(candidate)

        # ── LIVE: draw bbox on shared canvas immediately ───────────────────────
        _draw_bbox_on_canvas(canvas, candidate,
                             canvas.shape[1], draw_lock)

    # ── Debug: save annotated patch crop ───────────────────────────────────────
    if debug and candidates_out:
        patch_dir = out_dir / "step5a_live_patches"
        patch_dir.mkdir(exist_ok=True)
        ann = crop.copy()
        for c in candidates_out:
            # Local coords for patch crop
            src = c.get("tag_bbox") or c.get("symbol_bbox") or {}
            lx1 = int(src.get("x1", 0)) - x_off
            ly1 = int(src.get("y1", 0)) - y_off
            lx2 = int(src.get("x2", 0)) - x_off
            ly2 = int(src.get("y2", 0)) - y_off
            if lx2 > lx1 and ly2 > ly1:
                cat    = (c.get("symbol_category") or "unknown").lower()
                colour = CAT_COLOURS.get(cat, CAT_COLOURS["unknown"])
                cv2.rectangle(ann, (lx1, ly1), (lx2, ly2), colour, 2)
                cv2.putText(ann, c["tag_text"][:15],
                            (lx1 + 2, ly1 + 14),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                            (255, 255, 255), 1, cv2.LINE_AA)
        cv2.imwrite(str(patch_dir / f"patch_{pid:04d}.jpg"),
                    ann, [cv2.IMWRITE_JPEG_QUALITY, 85])

    return candidates_out


# ═══════════════════════════════════════════════════════════════════════════════
# Intra-step dedup (same as step5a — runs after all patches)
# ═══════════════════════════════════════════════════════════════════════════════

def _intra_dedup(candidates: list[dict]) -> list[dict]:
    """Merge same exact tag text within INTRA_DEDUP_DIST px. Keep highest confidence."""
    if not candidates:
        return candidates
    groups: dict[str, list[int]] = {}
    for i, c in enumerate(candidates):
        tag = re.sub(r'[\s\-]+', '', (c.get("tag_text") or "").upper())
        if tag:
            groups.setdefault(tag, []).append(i)

    keep: set[int] = set(range(len(candidates)))

    for tag, idxs in groups.items():
        if len(idxs) < 2:
            continue
        scored = sorted(idxs,
                        key=lambda i: -(candidates[i].get("vision_confidence") or 0))
        kept_centers: list[tuple[float, float]] = []
        for idx in scored:
            c    = candidates[idx]
            bbox = c.get("tag_bbox") or c.get("symbol_bbox") or {}
            cx   = (bbox.get("x1", 0) + bbox.get("x2", 0)) / 2
            cy   = (bbox.get("y1", 0) + bbox.get("y2", 0)) / 2
            dup  = any(math.sqrt((cx - kx)**2 + (cy - ky)**2) < INTRA_DEDUP_DIST
                       for kx, ky in kept_centers)
            if dup:
                keep.discard(idx)
            else:
                kept_centers.append((cx, cy))

    removed = len(candidates) - len(keep)
    if removed:
        log.info("Intra-step dedup: %d → %d (removed %d nearby duplicates)",
                 len(candidates), len(keep), removed)
    return [candidates[i] for i in sorted(keep)]


# ═══════════════════════════════════════════════════════════════════════════════
# Tile exporter
# ═══════════════════════════════════════════════════════════════════════════════

def _export_tiles(img: np.ndarray, out_dir: Path,
                  tile_size: int = 2000, prefix: str = "tile") -> list[str]:
    H, W  = img.shape[:2]
    rows  = math.ceil(H / tile_size)
    cols  = math.ceil(W / tile_size)
    paths = []
    tile_dir = out_dir / "step5a_live_tiles"
    tile_dir.mkdir(exist_ok=True)
    for r in range(rows):
        for c in range(cols):
            y0, x0 = r * tile_size, c * tile_size
            y1, x1 = min(y0 + tile_size, H), min(x0 + tile_size, W)
            tile = img[y0:y1, x0:x1].copy()
            cv2.putText(tile, f"R{r+1}C{c+1} [{x0},{y0}]",
                        (10, 40), cv2.FONT_HERSHEY_SIMPLEX,
                        1.2, (80, 80, 255), 2)
            path = str(tile_dir / f"{prefix}_R{r+1:02d}C{c+1:02d}.jpg")
            cv2.imwrite(path, tile, [cv2.IMWRITE_JPEG_QUALITY, 90])
            paths.append(path)
    log.info("Tiles: %dx%d grid → %d tiles in %s", rows, cols, len(paths), tile_dir)
    return paths


# ═══════════════════════════════════════════════════════════════════════════════
# Legend + stats banner on final canvas
# ═══════════════════════════════════════════════════════════════════════════════

def _draw_legend_and_banner(canvas: np.ndarray,
                             total: int, counts: dict) -> None:
    H, W   = canvas.shape[:2]
    scale  = W / 8000
    fscale = max(0.4, scale * 0.55)
    pad    = max(10, int(scale * 15))

    legend = [
        ("Instrument", CAT_COLOURS["instrument"]),
        ("Valve",      CAT_COLOURS["valve"]),
        ("Equipment",  CAT_COLOURS["equipment"]),
        ("Piping",     CAT_COLOURS["piping"]),
        ("Unknown",    CAT_COLOURS["unknown"]),
    ]

    # Legend box top-right
    font    = cv2.FONT_HERSHEY_SIMPLEX
    sw      = int(scale * 25)
    row_h   = int(fscale * 40) + pad
    box_w   = int(scale * 200)
    box_h   = row_h * len(legend) + pad * 2
    bx      = W - box_w - pad
    by      = pad

    overlay = canvas.copy()
    cv2.rectangle(overlay, (bx - 5, by), (W - pad + 5, by + box_h),
                  (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.7, canvas, 0.3, 0, canvas)

    for i, (label, col) in enumerate(legend):
        y = by + pad + i * row_h + row_h // 2
        cv2.rectangle(canvas, (bx, y - sw//2), (bx + sw, y + sw//2), col, -1)
        cv2.putText(canvas, label, (bx + sw + pad, y + int(fscale * 10)),
                    font, fscale, (230, 230, 230), 1, cv2.LINE_AA)

    # Bottom stats banner
    banner_h = max(30, int(scale * 45))
    cv2.rectangle(canvas, (0, H - banner_h), (W, H), (20, 20, 20), -1)
    stat_text = (f"Total tags: {total}  |  "
                 + "  ".join(f"{k.capitalize()}: {v}" for k, v in counts.items() if v))
    cv2.putText(canvas, stat_text, (pad * 2, H - banner_h // 3),
                font, fscale, (220, 220, 50), 1, cv2.LINE_AA)


# ═══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def run_live_annotator(
    img_path:               str,
    out_dir:                str,
    api_key:                str,
    sow_memory:             Optional[dict] = None,
    drawing_rules:          str = "",
    cloud_regions:          Optional[list] = None,
    revision_cloud_present: bool = False,
    debug:                  bool = False,
    single_patch:           Optional[int] = None,
    max_workers:            int = 8,
    tile_size:              int = 2000,
    overview_width:         int = 3200,
) -> dict:

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    img = cv2.imread(img_path)
    if img is None:
        raise FileNotFoundError(f"Cannot read: {img_path}")
    H, W = img.shape[:2]
    log.info("Drawing: %dx%d | revision=%s | clouds=%d | workers=%d",
             W, H, revision_cloud_present, len(cloud_regions or []), max_workers)

    client, sdk = _build_gemini_client(api_key)
    log.info("Gemini client ready (%s SDK)", sdk)

    # ── Shared annotated canvas + thread lock ──────────────────────────────────
    canvas    = img.copy()
    draw_lock = threading.Lock()

    # ── Generate + prioritise patches ─────────────────────────────────────────
    patches = generate_sahi_patches(img)
    if single_patch is not None:
        patches = [p for p in patches if p["patch_id"] == single_patch]
    else:
        patches = priority_sort_patches(patches, H, W)

    log.info("Processing %d patches...", len(patches))

    # ── Parallel execution ─────────────────────────────────────────────────────
    all_candidates: list[dict] = []
    results_lock   = threading.Lock()
    gemini_calls   = 0
    calls_lock     = threading.Lock()

    def _worker(patch: dict) -> list[dict]:
        return _process_patch_live(
            patch=patch, total=len(patches),
            client=client, sdk=sdk,
            drawing_rules=drawing_rules,
            sow_memory=sow_memory or {},
            cloud_regions=cloud_regions or [],
            revision_cloud_present=revision_cloud_present,
            canvas=canvas, draw_lock=draw_lock,
            out_dir=out, debug=debug,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_worker, p): p for p in patches}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                result = future.result()
                if result:
                    with results_lock:
                        all_candidates.extend(result)
                with calls_lock:
                    gemini_calls += 1
                if completed % 10 == 0 or completed == len(patches):
                    log.info("Progress: %d/%d patches | %d candidates so far",
                             completed, len(patches), len(all_candidates))
            except Exception as e:
                log.error("Patch %d error: %s", futures[future]["patch_id"], e)

    # ── Intra-step dedup ───────────────────────────────────────────────────────
    before = len(all_candidates)
    all_candidates = _intra_dedup(all_candidates)
    all_candidates.sort(key=lambda c: c.get("patch_id") or 0)

    # ── Category counts ────────────────────────────────────────────────────────
    from collections import Counter
    cat_counts = dict(Counter(c.get("symbol_category", "unknown")
                               for c in all_candidates))

    log.info("=== Step 5A Live Complete ===")
    log.info("Gemini calls  : %d", gemini_calls)
    log.info("Candidates    : %d (after dedup from %d)", len(all_candidates), before)
    for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
        log.info("  %-15s %d", cat, cnt)

    # ── Add legend + stats to canvas ───────────────────────────────────────────
    _draw_legend_and_banner(canvas, len(all_candidates), cat_counts)

    # ── Save annotated full-res image ──────────────────────────────────────────
    fullres_path = str(out / "step5a_live_annotated_fullres.jpg")
    cv2.imwrite(fullres_path, canvas, [cv2.IMWRITE_JPEG_QUALITY, 88])
    log.info("✓ step5a_live_annotated_fullres.jpg → %s", fullres_path)

    # ── Save overview (downscaled) ─────────────────────────────────────────────
    if W > overview_width:
        scale   = overview_width / W
        overview = cv2.resize(canvas, (overview_width, int(H * scale)),
                              interpolation=cv2.INTER_AREA)
    else:
        overview = canvas
    overview_path = str(out / "step5a_live_annotated.jpg")
    cv2.imwrite(overview_path, overview, [cv2.IMWRITE_JPEG_QUALITY, 88])
    log.info("✓ step5a_live_annotated.jpg (%dx%d) → %s",
             overview.shape[1], overview.shape[0], overview_path)

    # ── Save tiles ─────────────────────────────────────────────────────────────
    tile_paths = _export_tiles(canvas, out, tile_size=tile_size)

    # ── Write candidates JSON ──────────────────────────────────────────────────
    json_path = str(out / "step5a_live_candidates.json")
    with open(json_path, "w") as f:
        json.dump({
            "version":            "v2_live",
            "input_image":        img_path,
            "image_size":         [W, H],
            "total_patches":      len(patches),
            "gemini_calls":       gemini_calls,
            "total_candidates":   len(all_candidates),
            "category_counts":    cat_counts,
            "annotated_image":    overview_path,
            "fullres_image":      fullres_path,
            "tiles":              tile_paths,
            "candidates":         all_candidates,
        }, f, indent=2)
    log.info("✓ step5a_live_candidates.json (%d tags) → %s",
             len(all_candidates), json_path)

    return {
        "candidates":     all_candidates,
        "annotated_path": overview_path,
        "fullres_path":   fullres_path,
        "tiles":          tile_paths,
        "json_path":      json_path,
        "cat_counts":     cat_counts,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Step 5A Live Annotator — detect tags AND draw bboxes in one step")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("image",     nargs="?")
    g.add_argument("--context", help="drawing_context.json")
    parser.add_argument("--out",           default="output")
    parser.add_argument("--api-key",       help="Gemini API key")
    parser.add_argument("--sow",           help="sow_symbol_memory.json")
    parser.add_argument("--rules",         help="rules_prompt_block.txt")
    parser.add_argument("--workers",       type=int, default=8)
    parser.add_argument("--patch",         type=int, default=None)
    parser.add_argument("--tile-size",     type=int, default=2000)
    parser.add_argument("--overview-width",type=int, default=3200)
    parser.add_argument("--debug",         action="store_true",
                        help="Save per-patch annotated crops")
    args = parser.parse_args()

    api_key = (args.api_key or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY"))
    if not api_key:
        parser.error("Gemini API key required")

    img_path               = args.image
    sow_memory: dict       = {}
    drawing_rules          = ""
    cloud_regions: list    = []
    revision_cloud_present = False

    if args.context:
        with open(args.context) as f:
            ctx = json.load(f)
        img_path               = ctx.get("raster_path") or ctx.get("input_file")
        revision_cloud_present = ctx.get("revision_cloud_required", False)
        cloud_regions          = ctx.get("cloud_regions", [])
        if not args.sow and ctx.get("sow_memory_path"):
            with open(ctx["sow_memory_path"]) as f:
                sow_memory = json.load(f)
        if not args.rules and ctx.get("rules_prompt_block_path"):
            with open(ctx["rules_prompt_block_path"]) as f:
                drawing_rules = f.read()

    if args.sow:
        with open(args.sow) as f:
            sow_memory = json.load(f)
    if args.rules:
        with open(args.rules) as f:
            drawing_rules = f.read()

    result = run_live_annotator(
        img_path               = img_path,
        out_dir                = args.out,
        api_key                = api_key,
        sow_memory             = sow_memory,
        drawing_rules          = drawing_rules,
        cloud_regions          = cloud_regions,
        revision_cloud_present = revision_cloud_present,
        debug                  = args.debug,
        single_patch           = args.patch,
        max_workers            = args.workers,
        tile_size              = args.tile_size,
        overview_width         = args.overview_width,
    )

    print(f"\n=== Step 5A Live Annotator Complete ===")
    print(f"  Tags extracted : {len(result['candidates'])}")
    print()
    for cat, cnt in sorted(result['cat_counts'].items(), key=lambda x: -x[1]):
        print(f"    {cat:<18} {cnt:>4}")
    print()
    print(f"  Output files:")
    print(f"    {args.out}/step5a_live_candidates.json")
    print(f"    {args.out}/step5a_live_annotated.jpg          ← overview")
    print(f"    {args.out}/step5a_live_annotated_fullres.jpg  ← full resolution")
    print(f"    {args.out}/step5a_live_tiles/                 ← {len(result['tiles'])} zoom tiles")
    if args.debug:
        print(f"    {args.out}/step5a_live_patches/               ← per-patch crops")


if __name__ == "__main__":
    main()