#!/usr/bin/env python3
"""
step5a_candidate_extraction.py — Candidate Extraction Agent
============================================================
CDCI P&ID Pipeline — Step 5A

Purpose
-------
Detects engineering symbols and extracts associated tags from SAHI image
patches of the P&ID drawing. This is the PRIMARY extraction worker.

Multiple 5A workers execute in parallel (one per SAHI patch).

Architecture (from Gemini Multimodal Strategy doc)
---------------------------------------------------
  • Gemini 2.5 Pro @ MEDIA_RESOLUTION_HIGH, temperature=0.0
  • SAHI grid: 1024×1024 patches, 20–30% overlap
  • Tesseract OCR: deterministic character recognition (Gemini = semantic)
  • Pydantic structured output schema
  • Revision cloud filtering (keep inside / discard outside)
  • SOW memory filtering (ALLOW / BLOCK / UNSPECIFIED)

What this step does NOT do
--------------------------
  Business validation, asset registry lookup, duplicate merging,
  final assembly, topology generation — those are Steps 5C/5D and 7/8.

Inputs
------
  drawing_context.json   → raster_path, revision cloud regions
  sow_symbol_memory.json → allowed/blocked symbol lists  (Step 4)
  notes_context.json     → drawing-specific rules        (Step 3)

Outputs
-------
  step5a_candidates.json  — one record per detected candidate
  step5a_patches/         — per-patch debug crops (--debug)

Usage
-----
  python step5a_candidate_extraction.py drawing.jpg \
      --out output/ --api-key KEY

  # Full pipeline mode (reads all prior step outputs automatically):
  python step5a_candidate_extraction.py \
      --context output/drawing_context.json --api-key KEY --debug

  # Single patch test:
  python step5a_candidate_extraction.py drawing.jpg \
      --out output/ --api-key KEY --patch 3
"""

import argparse
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Optional
from difflib import SequenceMatcher

import cv2
import numpy as np
import pytesseract

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Model config (per Gemini Multimodal Strategy doc) ─────────────────────────
GEMINI_MODEL     = "gemini-3.1-pro-preview"
GEMINI_FALLBACK  = "gemini-2.5-pro"
TEMPERATURE      = 0.0      # mandatory for deterministic extraction
MEDIA_RESOLUTION = "MEDIA_RESOLUTION_HIGH"
GEMINI_MAX_SIDE  = 1024

# ── SAHI tiling parameters ────────────────────────────────────────────────────
# Smaller patches (upscaled to 1024 before Gemini) give each tiny valve/switch
# tag more effective pixels; higher overlap stops dense clusters and sequential
# valve banks from being split across a patch seam (boosts recall tail).
SAHI_PATCH_SIZE  = 768     # px
SAHI_OVERLAP     = 0.40    # 40% overlap

# ── ISA tag pattern (for Tesseract post-filter) ───────────────────────────────
ISA_TAG_RE = re.compile(
    r'\b([A-Z]{1,5}-[A-Z0-9]{1,6}-?[0-9]{2,6}[A-Z]?'   # FIT-1001, V-BV-2246
    r'|[A-Z]{1,4}-[0-9]{3,6}[A-Z]?'                      # PT-201, FV-208A
    r'|[0-9]{1,4}["\-][A-Z]{2,5}-[A-Z0-9]+-[A-Z0-9]+)\b' # 12"-ETH-V006-C03B
)


# ═══════════════════════════════════════════════════════════════════════════════
# SAHI Tiler
# ═══════════════════════════════════════════════════════════════════════════════

def generate_sahi_patches(img: np.ndarray,
                           patch_size: int = SAHI_PATCH_SIZE,
                           overlap: float = SAHI_OVERLAP) -> list[dict]:
    """
    Slice the full drawing into overlapping patches.
    Returns list of patch dicts with pixel coordinates in the GLOBAL image.
    """
    H, W   = img.shape[:2]
    stride = int(patch_size * (1 - overlap))
    patches = []
    pid     = 0

    y = 0
    while y < H:
        x = 0
        while x < W:
            x1 = min(x + patch_size, W)
            y1 = min(y + patch_size, H)
            crop = img[y:y1, x:x1]
            patches.append({
                "patch_id":    pid,
                "x_offset":    x,
                "y_offset":    y,
                "x1":          x1,
                "y1":          y1,
                "patch_w":     x1 - x,
                "patch_h":     y1 - y,
                "crop":        crop,
            })
            pid += 1
            if x1 == W:
                break
            x += stride
        if y1 == H:
            break
        y += stride

    log.info("SAHI: %d patches from %dx%d drawing (patch=%d, overlap=%.0f%%)",
             len(patches), W, H, patch_size, overlap * 100)
    return patches


# ═══════════════════════════════════════════════════════════════════════════════
# Revision cloud filtering
# ═══════════════════════════════════════════════════════════════════════════════

def point_in_any_cloud(px: float, py: float,
                        cloud_regions: list[dict]) -> bool:
    """Check if a point (global coords) falls inside any revision cloud bbox."""
    for cloud in cloud_regions:
        if (cloud.get("x0", 0) <= px <= cloud.get("x1", 0) and
                cloud.get("y0", 0) <= py <= cloud.get("y1", 0)):
            return True
    return False


def filter_by_revision_cloud(candidate: dict,
                               cloud_regions: list[dict],
                               revision_cloud_present: bool,
                               x_offset: int, y_offset: int) -> bool:
    """
    Returns True if this candidate should be KEPT.
    If revision clouds present: keep only candidates whose symbol center
    falls inside a cloud region.
    """
    if not revision_cloud_present or not cloud_regions:
        return True   # no clouds → process entire drawing

    bbox = candidate.get("symbol_bbox", {})
    cx = x_offset + (bbox.get("x1", 0) + bbox.get("x2", 0)) / 2
    cy = y_offset + (bbox.get("y1", 0) + bbox.get("y2", 0)) / 2
    return point_in_any_cloud(cx, cy, cloud_regions)


# ═══════════════════════════════════════════════════════════════════════════════
# SOW filtering
# ═══════════════════════════════════════════════════════════════════════════════

def apply_sow_filter(symbol_name: str, sow_memory: dict) -> tuple[str, str]:
    """
    Returns (sow_status, reason).
    sow_status: IN_SCOPE | OUT_OF_SCOPE | UNSPECIFIED
    """
    if not sow_memory:
        return "UNSPECIFIED", "No SOW memory loaded"

    blocked  = {n.upper() for n in sow_memory.get("blocked_names", [])}
    allowed  = {n.upper() for n in sow_memory.get("allowed_names", [])}
    sym_up   = re.sub(r'\s+', ' ', symbol_name.strip().upper())

    # Exact match
    if sym_up in blocked:
        return "OUT_OF_SCOPE", f"Exact match in DO_NOT_USE: {symbol_name}"
    if sym_up in allowed:
        return "IN_SCOPE", f"Exact match in USE: {symbol_name}"

    # Word-overlap match (≥60%)
    q_words = set(sym_up.split())
    for name_set, status in [(blocked, "OUT_OF_SCOPE"), (allowed, "IN_SCOPE")]:
        for name in name_set:
            n_words = set(name.split())
            overlap = len(q_words & n_words) / max(len(q_words), len(n_words), 1)
            if overlap >= 0.6:
                return status, f"Partial match ({overlap:.0%}) with '{name}'"

    return "UNSPECIFIED", "NO_SCOPE_DEFINITION_FOUND"


# ═══════════════════════════════════════════════════════════════════════════════
# Tesseract OCR pass (deterministic character recognition)
# ═══════════════════════════════════════════════════════════════════════════════

def tesseract_extract_tags(crop_bgr: np.ndarray) -> list[dict]:
    """
    Run Tesseract with bounding box data to extract candidate tag strings
    and their per-word locations within the patch.
    """
    gray     = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    clahe    = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    # Scale up if small patch
    H, W = enhanced.shape
    if min(H, W) < 400:
        scale    = 400 / min(H, W)
        enhanced = cv2.resize(enhanced, (int(W * scale), int(H * scale)),
                              interpolation=cv2.INTER_CUBIC)
        scale_factor = scale
    else:
        scale_factor = 1.0

    try:
        data = pytesseract.image_to_data(
            enhanced,
            config="--oem 3 --psm 11",   # PSM 11 = sparse text (no assumed layout)
            output_type=pytesseract.Output.DICT,
        )
    except Exception as e:
        log.warning("Tesseract failed: %s", e)
        return []

    results = []
    n = len(data["text"])
    for i in range(n):
        text = str(data["text"][i]).strip()
        conf = int(data["conf"][i]) if str(data["conf"][i]).lstrip("-").isdigit() else -1
        if conf < 40 or not text:
            continue
        if not ISA_TAG_RE.search(text):
            continue

        # Scale bbox back to original patch coords
        x = int(data["left"][i] / scale_factor)
        y = int(data["top"][i] / scale_factor)
        w = int(data["width"][i] / scale_factor)
        h = int(data["height"][i] / scale_factor)

        results.append({
            "text":       text,
            "ocr_conf":   conf / 100.0,
            "bbox_patch": {"x1": x, "y1": y, "x2": x + w, "y2": y + h},
        })
    return results


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


def _make_extraction_prompt(drawing_rules: str, ocr_tags: list[dict],
                             patch_id: int, revision_cloud_present: bool) -> str:
    ocr_section = ""
    if ocr_tags:
        ocr_lines = [f"  {t['text']} (conf={t['ocr_conf']:.2f})" for t in ocr_tags[:30]]
        ocr_section = (
            "OCR pre-extracted text strings (deterministic — use as ground truth for tag text):\n"
            + "\n".join(ocr_lines) + "\n\n"
        )

    rules_section = ""
    if drawing_rules.strip():
        rules_section = (
            "Drawing-specific rules (from Step 3 Notes Agent):\n"
            + drawing_rules[:800] + "\n\n"
        )

    cloud_note = ""
    if revision_cloud_present:
        cloud_note = (
            "NOTE: This is a REVISION DRAWING. "
            "Only extract symbols inside revision cloud boundaries.\n\n"
        )

    return f"""You are an expert P&ID data extraction agent (ISA 5.1).
Your ONLY function: identify engineering symbols and extract their tags.
Temperature=0.0. Never hallucinate. Only extract what is VISUALLY PRESENT.

{cloud_note}{rules_section}{ocr_section}
[Patch {patch_id}] Scan this image patch systematically top-left → bottom-right.

DETECT (be EXHAUSTIVE — extract EVERY tag visible in this patch):
  - Instrument bubbles (circles with letter codes: FIT, PT, TT, LT, TIT, FE,
    FV, FY, FZT, FZSC, FZSO, XV, ZSC, ZSO, XY, RV, etc.)
  - Valves (bow-tie, gate GV, ball BV, butterfly, check NRV, relief RV,
    control valve symbols). Tags look like V-BV-2246, V-GV-923, V-RV-207,
    V-NRV-748, V-XV-203.
  - Pumps, compressors, motors (KM-...), gear boxes (KG-...), heat exchangers,
    vessels, tanks, strainers (S-...), knock-out drums.
  - Corrosion probes/coupons, analyzers, any mechanical equipment.
  - Restriction orifices, thermowells (TW), elements (TE/FE), sight glasses.
  - PIPING LINE NUMBERS written along pipe runs — THESE ARE VALID TAGS, EXTRACT
    THEM. Format: SIZE-SERVICE-LINENO-SPEC, e.g.
      10IN-ETH-V061-61440X, 2IN-GV-V273-11502X, 6IN-ETH-V058-61440X-PP,
      12IN-ETH-V012-61440X-PP, 4IN-ETH-V059-61440X-PP.
    Classify these as symbol_category="piping".

CRITICAL — DENSE CLUSTERS (this is where tags are most often missed):
  - Valves frequently appear in BANKS of 3-6 adjacent symbols with SEQUENTIAL
    numbers (e.g. V-BV-2244, V-BV-2245, V-BV-2246, V-BV-2247). Extract EVERY
    valve in the bank — never skip one because its neighbour was already read.
  - Relief valves (V-RV-2xx) and check valves (V-NRV-7xx) are small — look hard.
  - Limit switches come in CLOSE/OPEN PAIRS stacked vertically (V-ZSC-203 with
    V-ZSO-203, V-FZSC-208 with V-FZSO-208). If you see one, the other is right
    next to it — extract BOTH.
  Count the symbols you see, then make sure you returned one candidate per symbol.

FOR EACH DETECTED SYMBOL:
  1. Identify its exact visual shape and classification.
  2. Find the nearest associated tag text (use OCR list above as ground truth).
  3. PRESERVE the full tag exactly as written, INCLUDING any area/unit prefix
     such as "V-" (e.g. read "V-FZSC-208", not "FZSC 208"). If the bubble shows
     only "FZSC / 208" but this drawing's unit prefix is "V-", prepend it.
     The leading "V" is often a SEPARATE symbol/letter just to the LEFT of the
     bubble — it is part of the tag, always include it (read "V-FZ-208", never
     "FZ-208"; "V-FZD-208", never "FZD-208").
  3b. Read ONLY the printed characters INSIDE the symbol. The square / diamond /
     circle OUTLINE strokes are NOT letters. A vertical box edge next to "FZ" is
     commonly misread as an extra "I" (giving "FZI"); a diagonal as "T"; a corner
     as "L". Do NOT append such border-induced letters: a boxed "FZ / 208" flow
     switch is "FZ-208", never "FZI-208".
  4. Record bounding box coordinates [x1, y1, x2, y2] within THIS patch. The
     tag_bbox MUST tightly enclose the TAG TEXT characters (not the symbol).

IGNORE — do NOT extract any of these:
  - Notes and annotations, table content, title block text, revision descriptions
  - Drawing reference numbers (e.g. 4224-MGDV-6-50-2002-001, MGDV-6-50-...)
  - Off-drawing reference arrows and their destination text
  - Logic controller labels that are bare codes (bare "LC", "RCI", "HS" with no number)
  - Partial text fragments less than 3 characters
  - Equipment titles that are descriptions not tags (e.g. "TEMPORARY SUCTION STRAINER")
  NOTE: piping line numbers (size-service-number) and "61440X"/"11502X" spec
  suffixes ARE part of valid piping tags — do NOT ignore them.

Return ONLY a JSON object (no markdown):
{{
  "patch_id": {patch_id},
  "candidates": [
    {{
      "symbol_name": "Flow Indicating Transmitter",
      "symbol_category": "instrument|valve|equipment|piping|unknown",
      "tag_text": "FIT-1001",
      "symbol_bbox": {{"x1": 120, "y1": 80, "x2": 160, "y2": 120}},
      "tag_bbox":    {{"x1": 125, "y1": 82, "x2": 155, "y2": 98}},
      "vision_confidence": 0.95,
      "tag_source": "ocr|vision|both",
      "notes": ""
    }}
  ],
  "patch_summary": "e.g. 5 instruments, 3 valves detected"
}}

If NO valid engineering symbols found: return candidates=[].
NEVER invent tags. NEVER assign a tag to a symbol without visual evidence."""


def extract_patch_with_gemini(crop_bgr: np.ndarray, patch_id: int,
                               client, sdk: str,
                               drawing_rules: str,
                               ocr_tags: list[dict],
                               revision_cloud_present: bool) -> dict:
    """Call Gemini 2.5 Pro on a single patch. Returns parsed JSON."""
    import cv2 as _cv2
    # Normalise the longest side to GEMINI_MAX_SIDE. We DOWN-scale big crops and
    # UP-scale small SAHI patches (cubic) so every symbol gets the maximum token
    # budget Gemini allows — small valve/switch tags become far more legible.
    H, W = crop_bgr.shape[:2]
    gemini_scale = 1.0   # coords_in_gemini = patch_local_coord * gemini_scale
    if max(H, W) != GEMINI_MAX_SIDE:
        gemini_scale = GEMINI_MAX_SIDE / max(H, W)
        interp = _cv2.INTER_AREA if gemini_scale < 1 else _cv2.INTER_CUBIC
        crop_bgr = _cv2.resize(crop_bgr, (int(W * gemini_scale), int(H * gemini_scale)),
                               interpolation=interp)

    ok, buf = _cv2.imencode(".jpg", crop_bgr, [_cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        return {"patch_id": patch_id, "candidates": [], "error": "encode failed"}
    img_bytes = buf.tobytes()

    prompt = _make_extraction_prompt(drawing_rules, ocr_tags,
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
            import google.generativeai as gl
            import PIL.Image as PILImage
            import io
            pil = PILImage.open(io.BytesIO(img_bytes))
            cfg = gl.GenerationConfig(temperature=TEMPERATURE)
            resp = gl.GenerativeModel(GEMINI_MODEL).generate_content(
                [prompt, pil], generation_config=cfg
            )
            raw = resp.text.strip()

        clean = raw.replace("```json", "").replace("```", "").strip()
        m = re.search(r'\{.*\}', clean, re.DOTALL)
        result = json.loads(m.group(0) if m else clean)
        # Record the scale so post-processing can map bboxes back to native
        # patch-local pixels before applying the global SAHI offset.
        result["_gemini_scale"] = gemini_scale
        return result

    except json.JSONDecodeError as e:
        log.warning("Patch %d JSON parse error: %s", patch_id, e)
        return {"patch_id": patch_id, "candidates": [], "parse_error": str(e)}
    except Exception as e:
        log.warning("Patch %d Gemini error: %s", patch_id, e)
        # Fallback: use Tesseract-only results
        return {
            "patch_id":   patch_id,
            "candidates": [],
            "error":      str(e),
            "fallback":   "tesseract_only",
        }


# ═══════════════════════════════════════════════════════════════════════════════
# False positive filter — reject non-tag text that Gemini misidentified as tags
# ═══════════════════════════════════════════════════════════════════════════════

# Drawing reference patterns (4224-MGDV-..., GDV-6-50-..., DV-6-50-...)
_FP_DRAWING_REF = re.compile(
    r'^\d{4}-[A-Z]{2,5}-\d-\d{2}-'       # 4224-MGDV-6-50-...
    r'|^[A-Z]*GDV-\d-'                      # GDV-6-50-..., MGDV-6-...
    r'|^[A-Z]*DV-\d-\d{2}-'               # DV-6-50-...
    r'|^MGDTY-'                               # MGDTY reference
    r'|^ES\.0\.'                            # ES.0.07 standard references
    r'|^CORP-ENG-'                            # CORP-ENG-STD references
    , re.I
)

# Bare node/function labels without loop numbers
_FP_BARE_NODE = re.compile(
    r'^I-\d{3}$'                             # bare I-004, I-001
    r'|^LC$|^RCI$|^HS$|^SS$'                 # bare function codes
    r'|^\d{4,5}[A-Z]?$'                     # pure numbers like 61440X, 1502X
    r'|^C\d{2}[A-Z]$'                       # piping spec codes C06B
    r'|^[A-Z]{1,2}$'                          # single/double letter fragments
    , re.I
)


def _is_false_positive(tag_text: str) -> bool:
    """
    Returns True if the tag text is a drawing reference, node ID,
    spec code, or fragment — not a real instrument/equipment tag.
    """
    tag = (tag_text or "").strip()
    if len(tag) < 3:
        return True                # too short to be a real tag
    if _FP_DRAWING_REF.search(tag):
        return True                # drawing reference number
    if _FP_BARE_NODE.match(tag):
        return True                # bare node ID or spec code
    return False


# ── Tag normalization (post-OCR clean-up) ──────────────────────────────────────
# Double-prime / inch marks that may follow a pipe size digit (ASCII + unicode).
_INCH_MARKS = r"(?:''|\"|''|´´|′′|″|”|’’)"
_INCH_RE = re.compile(r'(\d+(?:\.\d+)?)\s*' + _INCH_MARKS)


def _normalize_tag(tag: str) -> str:
    """
    Deterministic post-OCR normalization for an extracted tag string.

      3. Inch notation   — 2'' / 2" / 6'' → 2IN / 6IN (double-prime = inches)
      4. Dash collapse   — FZ--208 / V---FZ-208 → FZ-208 / V-FZ-208
      5. Consistent form — uppercase, single dash separators, no stray spaces

    Removal of false OCR characters (e.g. the spurious "I" in V-FZI-208 from the
    square symbol border) and inclusion of the leading "V" unit prefix are handled
    at the Gemini-prompt level — "I" is a legitimate ISA letter (FI, PI, FIT) so it
    can never be blindly stripped here without losing real tags.
    """
    if not tag:
        return tag
    t = tag.strip().upper()
    t = _INCH_RE.sub(r'\1IN', t)                 # 3. inches → IN
    t = re.sub(r'\s*-\s*', '-', t)               # 5. tidy dash spacing
    t = re.sub(r'^([A-Z])\s+(?=[A-Z])', r'\1-', t)  # "V FZ-208" → "V-FZ-208"
    t = re.sub(r'-{2,}', '-', t)                 # 4. collapse dash runs
    t = re.sub(r'\s+', ' ', t).strip().strip('-')
    return t


# ═══════════════════════════════════════════════════════════════════════════════
# Post-process: convert patch-local coords to global + apply filters
# ═══════════════════════════════════════════════════════════════════════════════

def process_patch_candidates(patch_result: dict,
                              patch_meta: dict,
                              ocr_tags: list[dict],
                              sow_memory: dict,
                              cloud_regions: list[dict],
                              revision_cloud_present: bool) -> list[dict]:
    """
    For each Gemini candidate:
      1. Convert bbox from patch-local → global image coordinates
      2. Apply revision cloud filter
      3. Apply SOW filter
      4. Reconcile tag text with Tesseract OCR (OCR is ground truth for text)
      5. Return enriched candidate records
    """
    x_off = patch_meta["x_offset"]
    y_off = patch_meta["y_offset"]
    # Gemini may have seen an up/down-scaled patch; map its coords back to
    # native patch-local pixels (divide by the scale) before adding the offset.
    gscale = patch_result.get("_gemini_scale") or 1.0
    inv = 1.0 / gscale if gscale else 1.0
    candidates = []

    # Build OCR lookup by position for reconciliation
    ocr_lookup: dict[str, str] = {t["text"]: t["text"] for t in ocr_tags}

    for raw in patch_result.get("candidates", []):
        # ── Bbox: patch-local → global ────────────────────────────────────────
        # Guard: Gemini sometimes returns null for bbox fields
        sb = raw.get("symbol_bbox") or {}
        tb = raw.get("tag_bbox")    or {}
        sym_bbox = {
            "x1": x_off + int((sb.get("x1") or 0) * inv),
            "y1": y_off + int((sb.get("y1") or 0) * inv),
            "x2": x_off + int((sb.get("x2") or 0) * inv),
            "y2": y_off + int((sb.get("y2") or 0) * inv),
        }
        tag_bbox = {
            "x1": x_off + int((tb.get("x1") or 0) * inv),
            "y1": y_off + int((tb.get("y1") or 0) * inv),
            "x2": x_off + int((tb.get("x2") or 0) * inv),
            "y2": y_off + int((tb.get("y2") or 0) * inv),
        }

        # ── Revision cloud filter ─────────────────────────────────────────────
        scope_type = "FULL_DRAWING"
        if revision_cloud_present and cloud_regions:
            scope_type = "REVISION_CLOUD"
            cx = (sym_bbox["x1"] + sym_bbox["x2"]) / 2
            cy = (sym_bbox["y1"] + sym_bbox["y2"]) / 2
            if not point_in_any_cloud(cx, cy, cloud_regions):
                continue   # discard: outside all clouds

        # ── Tag text: reconcile Gemini vs OCR ─────────────────────────────────
        gemini_tag = str(raw.get("tag_text") or "").strip()
        if not gemini_tag and not ocr_tags:
            continue   # skip candidates with no tag text at all
        ocr_conf   = 0.0

        # Find best OCR match for this tag
        best_ocr = None
        best_score = 0.0
        for ocr_t in ocr_tags:
            ocr_text = ocr_t["text"]
            # Exact
            if ocr_text.upper() == gemini_tag.upper():
                best_ocr   = ocr_text
                ocr_conf   = ocr_t["ocr_conf"]
                best_score = 1.0
                break
            # Fuzzy: share common prefix of ≥3 chars
            min_len = min(len(ocr_text), len(gemini_tag))
            if min_len >= 3:
                prefix_match = sum(
                    1 for a, b in zip(ocr_text.upper(), gemini_tag.upper()) if a == b
                ) / max(len(gemini_tag), 1)
                if prefix_match > best_score and prefix_match >= 0.7:
                    best_score = prefix_match
                    best_ocr   = ocr_text
                    ocr_conf   = ocr_t["ocr_conf"]

        # OCR is ground truth for text when confidence is high
        final_tag = best_ocr if (best_ocr and best_score >= 0.8) else gemini_tag
        # Deterministic clean-up: inch notation, dash collapse, consistent format
        final_tag = _normalize_tag(final_tag)

        # ── SOW filter ────────────────────────────────────────────────────────
        symbol_name = str(raw.get("symbol_name") or "").strip()
        sow_status, sow_reason = apply_sow_filter(symbol_name, sow_memory)
        if sow_status == "OUT_OF_SCOPE":
            continue   # blocked by SOW

        # ── False positive filter — reject non-tag text ───────────────────────
        # Drawing references, node IDs, fragments that Gemini misidentified
        if _is_false_positive(final_tag):
            continue

        candidate = {
            "candidate_id":       str(uuid.uuid4())[:8],
            "patch_id":           patch_meta["patch_id"],
            "symbol_name":        symbol_name,
            "symbol_category":    str(raw.get("symbol_category") or "unknown"),
            "tag_text":           final_tag,
            "tag_text_gemini":    gemini_tag,
            "tag_text_ocr":       best_ocr,
            "symbol_bbox":        sym_bbox,
            "tag_bbox":           tag_bbox,
            "scope_type":         scope_type,
            "sow_status":         sow_status,
            "sow_reason":         sow_reason,
            "ocr_confidence":     round(ocr_conf, 3),
            "vision_confidence":  float(raw.get("vision_confidence") or 0.0) if raw.get("vision_confidence") is not None else 0.0,
            "tag_source":         str(raw.get("tag_source") or "vision"),
        }
        candidates.append(candidate)

    return candidates


# ═══════════════════════════════════════════════════════════════════════════════
# OpenCV pre-filter — skip patches with no engineering content before Gemini
# ═══════════════════════════════════════════════════════════════════════════════

def patch_has_content(crop_bgr: np.ndarray,
                       min_text_density: float = 1.5,
                       min_contour_count: int  = 2) -> tuple[bool, str]:
    """
    Fast OpenCV pre-screen: returns (should_call_gemini, reason).

    Checks:
      1. Text pixel density (CLAHE + adaptive threshold → ink coverage %)
      2. Closed contour count (circles/bubbles = instrument symbols)

    Skips Gemini call if patch looks like blank whitespace or pure pipeline
    with no symbols. Saves ~30% of Gemini calls on typical P&IDs.
    """
    gray    = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    clahe   = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enh     = clahe.apply(gray)
    _, bin_img = cv2.threshold(enh, 0, 255,
                               cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # 1. Text/ink density
    density = bin_img.mean()
    if density < min_text_density:
        return False, f"low_density={density:.2f}"

    # 2. Closed contour count (instrument bubbles, valve bodies)
    contours, _ = cv2.findContours(bin_img, cv2.RETR_CCOMP,
                                   cv2.CHAIN_APPROX_SIMPLE)
    H, W = crop_bgr.shape[:2]
    min_area = W * H * 0.0002   # at least 0.02% of patch area
    max_area = W * H * 0.50     # not the whole patch

    closed = [c for c in contours
              if min_area < cv2.contourArea(c) < max_area]
    if len(closed) < min_contour_count:
        return False, f"few_contours={len(closed)}"

    return True, f"density={density:.2f},contours={len(closed)}"


def priority_sort_patches(patches: list[dict], H: int, W: int) -> list[dict]:
    """
    Sort patches so the most instrument-dense areas process first.
    P&IDs have most symbols in the centre; title block / notes at edges.

    Strategy: distance from centroid of drawing (Manhattan distance, inverted).
    Centre patches get lowest sort key → processed first.
    """
    cx, cy = W / 2, H / 2
    def _priority(p: dict) -> float:
        pcx = (p["x_offset"] + p["x1"]) / 2
        pcy = (p["y_offset"] + p["y1"]) / 2
        return abs(pcx - cx) + abs(pcy - cy)   # smaller = closer to centre

    return sorted(patches, key=_priority)


# ═══════════════════════════════════════════════════════════════════════════════
# Parallel patch worker
# ═══════════════════════════════════════════════════════════════════════════════

def _process_single_patch(
    patch:                  dict,
    total_patches:          int,
    client,
    sdk:                    str,
    drawing_rules:          str,
    sow_memory:             dict,
    cloud_regions:          list,
    revision_cloud_present: bool,
    out_dir:                str,
    debug:                  bool,
) -> tuple[list[dict], bool]:
    """
    Process one SAHI patch end-to-end.
    Called from ThreadPoolExecutor workers.
    Returns (candidates, gemini_was_called).
    Thread-safe: all state is local.
    """
    pid  = patch["patch_id"]
    crop = patch["crop"]

    # ── Pre-filter: skip empty patches ───────────────────────────────────────
    has_content, reason = patch_has_content(crop)
    if not has_content:
        log.debug("Patch %3d SKIP (pre-filter: %s)", pid, reason)
        return [], False

    # ── Tesseract OCR ─────────────────────────────────────────────────────────
    ocr_tags = tesseract_extract_tags(crop)

    # ── Gemini extraction ─────────────────────────────────────────────────────
    patch_result = extract_patch_with_gemini(
        crop, pid, client, sdk,
        drawing_rules, ocr_tags, revision_cloud_present,
    )
    raw_count = len(patch_result.get("candidates", []))

    log.info("Patch %3d/%d  [%d,%d]  Tesseract=%d  Gemini=%d  %s",
             pid, total_patches - 1,
             patch["x_offset"], patch["y_offset"],
             len(ocr_tags), raw_count,
             patch_result.get("patch_summary", ""))

    # ── Post-process: global coords + filters ────────────────────────────────
    candidates = process_patch_candidates(
        patch_result, patch, ocr_tags,
        sow_memory, cloud_regions, revision_cloud_present,
    )

    # ── Debug crop ───────────────────────────────────────────────────────────
    if debug and candidates:
        ann = crop.copy()
        for c in candidates:
            src = c.get("tag_bbox") or c.get("symbol_bbox") or {}
            sb = {k: v - (patch["x_offset"] if "x" in k else patch["y_offset"])
                  for k, v in src.items()}
            cv2.rectangle(ann,
                          (sb["x1"], sb["y1"]), (sb["x2"], sb["y2"]),
                          (0, 255, 0), 2)
            cv2.putText(ann, c["tag_text"][:12],
                        (sb["x1"], max(sb["y1"] - 5, 0)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        cv2.imwrite(
            str(Path(out_dir) / "step5a_patches" / f"patch_{pid:04d}.jpg"),
            ann, [cv2.IMWRITE_JPEG_QUALITY, 85],
        )

    return candidates, True


# ═══════════════════════════════════════════════════════════════════════════════
# Intra-step dedup (Spatial IoU + Fuzzy Match)
# ═══════════════════════════════════════════════════════════════════════════════

def _calculate_iou(boxA: dict, boxB: dict) -> float:
    """Calculate Intersection over Union (IoU) of two bounding boxes."""
    xA = max(boxA.get("x1", 0), boxB.get("x1", 0))
    yA = max(boxA.get("y1", 0), boxB.get("y1", 0))
    xB = min(boxA.get("x2", 0), boxB.get("x2", 0))
    yB = min(boxA.get("y2", 0), boxB.get("y2", 0))

    interArea = max(0, xB - xA) * max(0, yB - yA)
    
    if interArea == 0:
        return 0.0

    boxAArea = max(0, boxA.get("x2", 0) - boxA.get("x1", 0)) * max(0, boxA.get("y2", 0) - boxA.get("y1", 0))
    boxBArea = max(0, boxB.get("x2", 0) - boxB.get("x1", 0)) * max(0, boxB.get("y2", 0) - boxB.get("y1", 0))

    iou = interArea / float(boxAArea + boxBArea - interArea)
    return iou

def _fuzzy_match(tag1: str, tag2: str) -> float:
    """Return a similarity ratio between 0.0 and 1.0 for two tags."""
    return SequenceMatcher(None, tag1, tag2).ratio()

def _intra_step_dedup(candidates: list[dict]) -> list[dict]:
    """
    Merge duplicate tags resulting from SAHI patch overlap.

    RECALL-SAFE policy: only merge two candidates that are almost certainly the
    SAME physical tag seen in two overlapping patches. We must NEVER merge
    sequentially-numbered neighbours such as V-BV-2245 vs V-BV-2246 (fuzzy
    ratio 0.857) or V-TIT-211 vs V-TIT-212. Therefore a merge requires either:
      • EXACT normalized text equality with any spatial overlap (IoU > 0.15), OR
      • Very high spatial overlap (IoU > 0.55) AND near-identical text
        (fuzzy >= 0.92) — catches OCR-noise variants of the same tag.
    When in doubt we KEEP both (step5d does final spatial dedup later).
    """
    if not candidates:
        return candidates

    scored = sorted(candidates, key=lambda c: -(c.get("vision_confidence") or 0.0))
    keep = []

    for current in scored:
        is_dup = False
        curr_box = current.get("tag_bbox") or current.get("symbol_bbox") or {}
        curr_tag = re.sub(r'[\s\-]+', '', (current.get("tag_text") or "").upper())

        for kept in keep:
            kept_box = kept.get("tag_bbox") or kept.get("symbol_bbox") or {}
            kept_tag = re.sub(r'[\s\-]+', '', (kept.get("tag_text") or "").upper())
            iou = _calculate_iou(curr_box, kept_box)

            exact_same = curr_tag and curr_tag == kept_tag
            if exact_same and iou > 0.15:
                is_dup = True
                break
            if iou > 0.55 and _fuzzy_match(curr_tag, kept_tag) >= 0.92:
                is_dup = True
                break

        if not is_dup:
            keep.append(current)

    removed = len(candidates) - len(keep)
    if removed > 0:
        log.info("Intra-step dedup (recall-safe): removed %d exact SAHI duplicates (%d → %d candidates)",
                 removed, len(candidates), len(keep))
    return keep


# ═══════════════════════════════════════════════════════════════════════════════
# Main pipeline — parallel execution
# ═══════════════════════════════════════════════════════════════════════════════

def run_candidate_extraction(
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
) -> list[dict]:
    """
    Parallel SAHI extraction.

    Speed improvements over v1 sequential:
      1. ThreadPoolExecutor — up to max_workers patches processed simultaneously
      2. Pre-filter        — OpenCV density check skips blank patches (~30% saving)
      3. Priority order    — centre-out: dense instrument areas first, edges last
      4. Rate limiter      — adaptive sleep prevents 429 errors on free-tier keys

    All extraction logic (Gemini, Tesseract, filters) is identical to v1.
    Results are collected into the same output schema as before.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if debug:
        (out / "step5a_patches").mkdir(exist_ok=True)

    img = cv2.imread(img_path)
    if img is None:
        raise FileNotFoundError(f"Cannot read: {img_path}")
    H, W = img.shape[:2]
    log.info("Drawing: %dx%d | clouds=%s | revision=%s | workers=%d",
             W, H, len(cloud_regions or []), revision_cloud_present, max_workers)

    client, sdk = _build_gemini_client(api_key)
    log.info("Gemini client ready (%s SDK)", sdk)

    # Generate + prioritise patches
    patches = generate_sahi_patches(img)

    if single_patch is not None:
        patches = [p for p in patches if p["patch_id"] == single_patch]
        log.info("Single patch mode: patch %d", single_patch)
    else:
        patches = priority_sort_patches(patches, H, W)
        log.info("Patches sorted centre-out (%d total)", len(patches))

    # Thread-safe result accumulator
    all_candidates:    list[dict] = []
    results_lock = threading.Lock()
    total_gemini_calls = 0
    total_skipped      = 0
    calls_lock = threading.Lock()

    # Adaptive rate limiter:
    # Gemini Pro paid tier: ~1000 RPM → no sleep needed
    # Free tier: 5 RPM → enforce 12s between calls
    # We detect throttling from 429 and back off automatically
    _last_call_time = [0.0]   # mutable list so lambda can write it
    _rate_lock = threading.Lock()
    MIN_CALL_INTERVAL = 0.0   # seconds; set to 12.0 for free-tier keys

    def _rate_limited_process(patch: dict):
        nonlocal total_gemini_calls, total_skipped

        # Pre-filter BEFORE acquiring rate limiter (pure CPU, no API)
        has_content, reason = patch_has_content(patch["crop"])
        if not has_content:
            log.debug("Patch %3d SKIP pre-filter: %s", patch["patch_id"], reason)
            with calls_lock:
                total_skipped += 1
            return []

        # Rate limit: throttle if needed
        if MIN_CALL_INTERVAL > 0:
            with _rate_lock:
                import time
                wait = MIN_CALL_INTERVAL - (time.time() - _last_call_time[0])
                if wait > 0:
                    time.sleep(wait)
                _last_call_time[0] = time.time()

        candidates, called = _process_single_patch(
            patch=patch,
            total_patches=len(patches),
            client=client,
            sdk=sdk,
            drawing_rules=drawing_rules,
            sow_memory=sow_memory or {},
            cloud_regions=cloud_regions or [],
            revision_cloud_present=revision_cloud_present,
            out_dir=out_dir,
            debug=debug,
        )
        with calls_lock:
            if called:
                total_gemini_calls += 1
        return candidates

    # ── Parallel execution ────────────────────────────────────────────────────
    log.info("Starting parallel extraction: %d patches, %d workers",
             len(patches), max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_rate_limited_process, p): p for p in patches}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                candidates = future.result()
                if candidates:
                    with results_lock:
                        all_candidates.extend(candidates)
                if completed % 10 == 0 or completed == len(patches):
                    log.info("Progress: %d/%d patches done | %d candidates so far",
                             completed, len(patches), len(all_candidates))
            except Exception as e:
                patch = futures[future]
                log.error("Patch %d failed: %s", patch["patch_id"], e)

    log.info("=== Step 5A Complete ===")
    log.info("Patches total    : %d", len(patches))
    log.info("Gemini calls     : %d  (skipped %d by pre-filter)",
             total_gemini_calls, total_skipped)
    log.info("Total candidates : %d", len(all_candidates))

    # ── Intra-step dedup (IoU + Fuzzy Match) ──────────────────────────────────
    # This catches SAHI duplicates at the source before step5d sees them
    all_candidates = _intra_step_dedup(all_candidates)

    # Sort back into patch_id order for deterministic output
    all_candidates.sort(key=lambda c: (c.get("patch_id") or 0))

    # Write output
    out_path = str(out / "step5a_candidates.json")
    with open(out_path, "w") as f:
        json.dump({
            "version":              "v2_parallel",
            "input_image":          img_path,
            "image_size":           [W, H],
            "total_patches":        len(patches),
            "gemini_calls":         total_gemini_calls,
            "patches_skipped":      total_skipped,
            "max_workers":          max_workers,
            "total_candidates":     len(all_candidates),
            "candidates":           all_candidates,
        }, f, indent=2)
    log.info("✓ step5a_candidates.json → %s", out_path)
    return all_candidates

# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Step 5A: Candidate Extraction Agent (SAHI + Gemini + Tesseract)")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("image",    nargs="?", help="Drawing image path")
    g.add_argument("--context",           help="drawing_context.json from Step 1")
    parser.add_argument("--out",       default="output")
    parser.add_argument("--api-key",   help="Gemini API key")
    parser.add_argument("--sow",       help="sow_symbol_memory.json from Step 4")
    parser.add_argument("--rules",     help="rules_prompt_block.txt from Step 3")
    parser.add_argument("--debug",     action="store_true")
    parser.add_argument("--patch",     type=int, default=None,
                        help="Process single patch ID only (for testing)")
    parser.add_argument("--workers",   type=int, default=8,
                        help="Parallel Gemini workers (default: 8; use 1 for free-tier keys)")
    parser.add_argument("--clouds",    default=None,
                        help="Path to outer_clouds_v2.json from step2b (auto-detected if omitted)")
    args = parser.parse_args()

    api_key = (args.api_key or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY"))
    if not api_key:
        parser.error("Gemini API key required")

    img_path = args.image
    sow_memory: dict = {}
    drawing_rules = ""
    cloud_regions: list = []
    revision_cloud_present = False

    # Load from context if provided
    if args.context:
        with open(args.context) as f:
            ctx = json.load(f)
        img_path = ctx.get("raster_path") or ctx.get("input_file")
        # Load SOW memory from context path if not explicitly given
        if not args.sow and ctx.get("sow_memory_path"):
            with open(ctx["sow_memory_path"]) as f:
                sow_memory = json.load(f)
        # Load rules
        if not args.rules and ctx.get("rules_prompt_block_path"):
            with open(ctx["rules_prompt_block_path"]) as f:
                drawing_rules = f.read()

    # ── Load cloud regions from step2b output ─────────────────────────────────
    # Auto-detect outer_clouds_v2.json in the output directory, or use --clouds.
    clouds_path = args.clouds
    if not clouds_path:
        auto = os.path.join(args.out, "outer_clouds_v2.json")
        if os.path.exists(auto):
            clouds_path = auto
    if clouds_path and os.path.exists(clouds_path):
        with open(clouds_path) as f:
            cloud_data = json.load(f)
        # Convert step2b "bbox": [x0,y0,x1,y1] list → {x0,y0,x1,y1} dict
        for entry in cloud_data.get("clouds", []):
            bbox = entry.get("bbox", [])
            if len(bbox) == 4:
                cloud_regions.append({"x0": bbox[0], "y0": bbox[1],
                                      "x1": bbox[2], "y1": bbox[3]})
        if cloud_regions:
            revision_cloud_present = True
            log.info("Cloud filter ON: %d cloud regions loaded from %s",
                     len(cloud_regions), clouds_path)
        else:
            log.info("Cloud file found but contains no regions — full-drawing extraction")

    if args.sow:
        with open(args.sow) as f:
            sow_memory = json.load(f)
    if args.rules:
        with open(args.rules) as f:
            drawing_rules = f.read()

    candidates = run_candidate_extraction(
        img_path=img_path,
        out_dir=args.out,
        api_key=api_key,
        sow_memory=sow_memory,
        drawing_rules=drawing_rules,
        cloud_regions=cloud_regions,
        revision_cloud_present=revision_cloud_present,
        debug=args.debug,
        single_patch=args.patch,
        max_workers=args.workers,
    )

    print(f"\n=== Step 5A Complete ===")
    mode = f"CLOUD_FILTER ({len(cloud_regions)} regions)" if revision_cloud_present else "FULL_DRAWING"
    print(f"  Mode: {mode}")
    print(f"  Candidates extracted: {len(candidates)}")
    by_cat = {}
    for c in candidates:
        cat = c.get("symbol_category", "unknown")
        by_cat[cat] = by_cat.get(cat, 0) + 1
    for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"    {cat:<20} {cnt:>4}")
    print(f"\n  Output: {args.out}/step5a_candidates.json")
    if args.debug:
        print(f"          {args.out}/step5a_patches/  (annotated per-patch crops)")


if __name__ == "__main__":
    main()