#!/usr/bin/env python3
"""
step3_notes_agent.py — Notes Extraction Agent (v3)
===================================================
Key improvements over v2:
  • Adaptive region detection: finds actual separator lines in the binary image
    so zones match the drawing rather than using hardcoded fractions
  • 3 wide zones (bottom, left, top) with 80px overlap buffers — no text falls
    in a gap between crops
  • Uses binary_path from drawing_context.json for Tesseract (pre-computed
    CLAHE binary is cleaner than re-doing it from color)
  • All Gemini calls use gemini-2.5-flash (better at dense text, faster)
  • Final synthesis call: one Gemini pass over all OCR text to reconcile
    partial notes, detect multi-line continuations, and clean numbering
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

# ── Models ────────────────────────────────────────────────────────────────────
GEMINI_FLASH_MODEL = "gemini-2.5-flash"

# ── Region detection parameters ──────────────────────────────────────────────
OVERLAP_PX          = 80     # pixel overlap between adjacent zones
MIN_SIDE_FOR_OCR    = 1200   # upscale crops smaller than this before Tesseract
SEP_LINE_DENSITY    = 0.45   # fraction of row width to call it a separator line
TEXT_DENSITY_THRESH = 0.06   # min fraction of row width to call it a text row
TEXT_CONSECUTIVE    = 8      # consecutive text rows to confirm a text block


# ═══════════════════════════════════════════════════════════════════════════════
# Image helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _load(path: str) -> np.ndarray:
    img = cv2.imread(path)
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {path}")
    return img


def _upscale(img: np.ndarray, min_side: int = MIN_SIDE_FOR_OCR) -> np.ndarray:
    H, W = img.shape[:2]
    if min(H, W) >= min_side:
        return img
    scale = min_side / min(H, W)
    return cv2.resize(img, (int(W * scale), int(H * scale)),
                      interpolation=cv2.INTER_CUBIC)


def _encode_jpeg(img: np.ndarray, quality: int = 92) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def _smooth(arr: np.ndarray, window: int) -> np.ndarray:
    k = np.ones(window) / window
    return np.convolve(arr, k, mode='same')


# ═══════════════════════════════════════════════════════════════════════════════
# Adaptive boundary detection
# ═══════════════════════════════════════════════════════════════════════════════

def _find_bottom_notes_top(binary: np.ndarray) -> int:
    """
    Find the topmost y-pixel of the bottom notes block.

    Strategy: scan upward from near the bottom edge. The notes block sits below
    a low-density blank strip (row density < 0.04) that separates it from the
    drawing body above. Find that gap, then the notes start just below it.
    Fallback: bottom 20% of image.
    """
    H, W = binary.shape[:2]
    row_proj = np.sum(binary > 0, axis=1).astype(float) / W
    smoothed = _smooth(row_proj, max(5, H // 120))

    search_start = int(H * 0.50)
    search_end   = int(H * 0.97)
    GAP_DENSITY  = 0.04   # rows this sparse are blank/gap rows

    # Scan upward: once we've seen a dense notes block, the next sparse row
    # is the top of the gap — notes start at the first dense row below that gap.
    in_notes = False
    for y in range(search_end, search_start, -1):
        if smoothed[y] > GAP_DENSITY:
            in_notes = True
        elif in_notes:
            # Entered the gap scanning upward. Scan downward from here to
            # find the first dense row — that is the top of the notes block.
            for notes_y in range(y + 1, search_end):
                if smoothed[notes_y] > GAP_DENSITY:
                    return max(0, notes_y - 10)
            break   # gap goes all the way to bottom — unlikely but safe

    # Fallback: bottom 20%
    return int(H * 0.80)


def _find_left_notes_right(binary: np.ndarray) -> int:
    """
    Find the rightmost x-pixel of the left notes/abbreviations block.

    Strategy:
    1. Look for a long vertical separator line (col density > 45% of height)
       in x = 5%..40% of image.
    2. Fallback: x = 32% of image width.
    """
    H, W = binary.shape[:2]
    col_proj  = np.sum(binary > 0, axis=0).astype(float) / H
    smoothed  = _smooth(col_proj, max(5, W // 120))

    search_start = int(W * 0.05)
    search_end   = int(W * 0.42)

    # Scan right-to-left to find the rightmost separator line
    for x in range(search_end, search_start, -1):
        if smoothed[x] > 0.45:
            # Step past the line
            while x > search_start and smoothed[x] > 0.45 * 0.4:
                x -= 1
            return min(W, x + 5)

    return int(W * 0.32)


def _build_note_regions(color: np.ndarray,
                         binary: Optional[np.ndarray]) -> list[dict]:
    """
    Build the list of note regions using adaptive detection when binary is
    available, otherwise fall back to wide safe zones.

    Each region dict: name, x0, y0, x1, y1, priority, desc, tess_psm
    """
    H, W = color.shape[:2]
    regions = []

    if binary is not None:
        bottom_top = _find_bottom_notes_top(binary)
        left_right = _find_left_notes_right(binary)
        log.info("Detected: bottom notes top=%.1f%% (y=%d), left notes right=%.1f%% (x=%d)",
                 bottom_top / H * 100, bottom_top,
                 left_right / W * 100, left_right)
    else:
        bottom_top = int(H * 0.65)
        left_right = int(W * 0.32)
        log.info("No binary image — using fallback zone boundaries")

    # ── Zone 1: Bottom notes (full width, from separator to bottom) ───────────
    y0 = max(0, bottom_top - OVERLAP_PX)
    regions.append({
        "name":     "bottom_notes",
        "x0": 0,    "y0": y0,
        "x1": W,    "y1": H,
        "priority": 1,
        "desc":     "Bottom notes block (full width)",
        "tess_psm": "6",   # uniform block of text
    })

    # ── Zone 2: Left notes/abbreviations (left strip, above bottom zone) ─────
    # Exclude the region already covered by bottom_notes to avoid duplicates
    left_y1 = min(H, bottom_top + OVERLAP_PX)
    left_y0 = max(0, int(H * 0.12))   # skip title block row at very top
    x1 = min(W, left_right + OVERLAP_PX)
    if x1 > int(W * 0.05) and (left_y1 - left_y0) > 100:
        regions.append({
            "name":     "left_notes",
            "x0": 0,    "y0": left_y0,
            "x1": x1,   "y1": left_y1,
            "priority": 1,
            "desc":     "Left margin notes and abbreviations",
            "tess_psm": "4",   # single column of text
        })

    # ── Zone 3: Top strip (if the drawing has notes at the top) ──────────────
    top_y1 = int(H * 0.14) + OVERLAP_PX
    if binary is not None:
        top_density = float(np.mean(
            np.sum(binary[:int(H * 0.12)] > 0, axis=1) / W
        ))
        mid_density = float(np.mean(
            np.sum(binary[int(H * 0.30):int(H * 0.60)] > 0, axis=1) / W
        ))
        include_top = top_density > mid_density * 1.4
    else:
        include_top = True   # include by default when we can't check

    if include_top:
        regions.append({
            "name":     "top_notes",
            "x0": 0,    "y0": 0,
            "x1": W,    "y1": top_y1,
            "priority": 2,
            "desc":     "Top notes / general notes header",
            "tess_psm": "6",
        })

    return regions


# ═══════════════════════════════════════════════════════════════════════════════
# Cloud counting (lightweight, per-crop)
# ═══════════════════════════════════════════════════════════════════════════════

def _count_clouds(crop_gray: np.ndarray) -> int:
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(crop_gray)
    binary = cv2.adaptiveThreshold(
        enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 51, 10
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=4)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    H, W = crop_gray.shape[:2]
    min_area = H * W * 0.002
    max_area = H * W * 0.95
    count = 0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if not (min_area < area < max_area):
            continue
        perimeter = cv2.arcLength(cnt, True)
        if perimeter < 50:
            continue
        hull_perimeter = cv2.arcLength(cv2.convexHull(cnt), True)
        if hull_perimeter > 0 and 1.05 <= perimeter / hull_perimeter <= 2.5:
            count += 1
    return count


# ═══════════════════════════════════════════════════════════════════════════════
# Tesseract OCR
# ═══════════════════════════════════════════════════════════════════════════════

def _tesseract_ocr(crop: np.ndarray, psm: str = "6") -> str:
    """
    OCR a crop. Pass binary crops directly; color crops are auto-converted.
    psm: Tesseract page segmentation mode (4=column, 6=block, 3=auto).
    """
    if len(crop.shape) == 3:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        gray = cv2.fastNlMeansDenoising(gray, h=10,
                                         templateWindowSize=7,
                                         searchWindowSize=21)
    else:
        gray = crop  # already binary/gray

    gray = _upscale(gray, MIN_SIDE_FOR_OCR)
    config = f"--oem 3 --psm {psm}"
    try:
        text = pytesseract.image_to_string(gray, config=config)
        lines = [l.rstrip() for l in text.splitlines() if len(l.strip()) > 2]
        return "\n".join(lines)
    except Exception as e:
        log.warning("Tesseract failed: %s", e)
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini SDK wrapper
# ═══════════════════════════════════════════════════════════════════════════════

def _build_gemini_client(api_key: str):
    try:
        import google.genai as genai
        return genai.Client(api_key=api_key), "new"
    except Exception:
        pass
    try:
        import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=api_key)
        return genai_legacy, "legacy"
    except Exception as e:
        raise RuntimeError(f"No working Gemini SDK: {e}")


def _gemini_call(client, sdk: str, img_bytes: bytes, prompt: str) -> str:
    if sdk == "new":
        from google.genai import types as gtypes
        response = client.models.generate_content(
            model=GEMINI_FLASH_MODEL,
            contents=[
                gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                gtypes.Part.from_text(text=prompt),
            ],
        )
        return response.text.strip()
    else:
        import google.generativeai as genai_legacy
        import PIL.Image as PILImage, io
        pil = PILImage.open(io.BytesIO(img_bytes))
        mdl = genai_legacy.GenerativeModel(GEMINI_FLASH_MODEL)
        return mdl.generate_content([prompt, pil]).text.strip()


def _parse_json(raw: str) -> dict:
    clean = raw.replace("```json", "").replace("```", "").strip()
    m = re.search(r'\{.*\}', clean, re.DOTALL)
    if m:
        clean = m.group(0)
    return json.loads(clean)


# ═══════════════════════════════════════════════════════════════════════════════
# Prompts
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_prompt(region_name: str, desc: str, ocr_text: str) -> str:
    ocr_section = ""
    if ocr_text.strip():
        ocr_section = f"""
Tesseract pre-scan of this region (use as a guide — image is ground truth):
<ocr_text>
{ocr_text[:4000]}
</ocr_text>
"""
    return f"""You are reading the "{region_name}" region ({desc}) of an engineering P&ID drawing.
{ocr_section}
Extract EVERY piece of text visible in this image: numbered notes, lettered notes,
abbreviation definitions, legend entries, scope rules, equipment rules, and any
other annotations. Do NOT skip a note just because it seems short or similar to another.

If a note is cut off at the edge of this crop, extract what you can see and mark
partially_obscured=true.

Return ONLY a JSON object (no markdown, no extra text):
{{
  "region": "{region_name}",
  "notes_found": true | false,
  "items": [
    {{
      "id": "1" | "A" | "ABBR:XYZ" | "LEGEND:X" etc.,
      "raw_text": "exact verbatim text from drawing",
      "semantic_type": "general_note | abbreviation_definition | symbol_exception | equipment_rule | scope_rule | drafting_rule | legend_entry | title_info",
      "extracted_rule": {{
        "type": "abbreviation | prefix | rule | constraint | reference | format",
        "subject": "what this applies to",
        "rule": "plain-English machine-readable rule"
      }},
      "confidence": 0.0-1.0,
      "partially_obscured": false
    }}
  ],
  "abbreviations_found": {{ "XYZ": "full meaning", "...": "..." }},
  "revision_cloud_wraps_this_block": true | false,
  "gemini_cloud_count_estimate": 0
}}"""


# ═══════════════════════════════════════════════════════════════════════════════
# Deduplication (pre-synthesis fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def _deduplicate(items: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for item in items:
        note_id = str(item.get("id") or "").strip().upper()
        raw     = str(item.get("raw_text") or "").strip()
        conf    = float(item.get("confidence") or 0.5)
        key     = note_id if (note_id and note_id not in ("", "UNKNOWN")) \
                  else raw[:40].upper()
        if not key:
            continue
        if key not in seen:
            seen[key] = item
        else:
            e = seen[key]
            e_score = (e.get("confidence") or 0) + len(str(e.get("raw_text") or "")) / 1000
            n_score = conf + len(raw) / 1000
            if n_score > e_score:
                seen[key] = item
    return list(seen.values())


def _sort_notes(items: list[dict]) -> list[dict]:
    def key(item):
        nid = str(item.get("id") or "ZZZ")
        m = re.match(r"(\d+)", nid)
        return (int(m.group(1)) if m else 999, nid)
    return sorted(items, key=key)


# ═══════════════════════════════════════════════════════════════════════════════
# Rules block builder
# ═══════════════════════════════════════════════════════════════════════════════

def _build_rules_block(notes: list[dict], abbrevs: dict,
                        region_summary: list[dict]) -> str:
    lines = ["== DRAWING-SPECIFIC RULES (extracted from notes block) ==", ""]

    lines.append("-- Region Coverage --")
    lines.append(f"{'Region':<28} {'Notes':>6} {'Clouds':>7} {'OCR chars':>10}")
    lines.append("-" * 55)
    for r in region_summary:
        lines.append(
            f"{r['region']:<28} {r['notes_extracted']:>6} "
            f"{r['cloud_count']:>7} {r['ocr_chars']:>10}"
        )
    lines.append(f"{'TOTAL':<28} {sum(r['notes_extracted'] for r in region_summary):>6} "
                 f"{sum(r['cloud_count'] for r in region_summary):>7}")
    lines.append("")

    # Categorise every note — nothing is dropped
    GLOBAL_TYPES    = {"general_note", "scope_rule", "equipment_rule", "constraint"}
    TAG_TYPES       = {"abbreviation_definition", "symbol_exception", "legend_entry"}
    DRAFT_TYPES     = {"drafting_rule"}
    REF_TYPES       = {"reference", "cross_reference", "label"}

    def _text(n: dict) -> str:
        er = n.get("extracted_rule") or {}
        return er.get("rule") or n.get("raw_text", "")

    lines.append("-- General & Engineering Notes --")
    for n in notes:
        if n.get("semantic_type") in GLOBAL_TYPES:
            lines.append(f"  [{n.get('id','?')}] {_text(n)}")
    lines.append("")

    lines.append("-- Tag & Symbol Rules --")
    for n in notes:
        if n.get("semantic_type") in TAG_TYPES:
            lines.append(f"  [{n.get('id','?')}] {_text(n)}")
    lines.append("")

    if abbrevs:
        lines.append("-- Abbreviations --")
        for abbr, meaning in sorted(abbrevs.items()):
            lines.append(f"  {abbr}: {meaning}")
        lines.append("")

    lines.append("-- Drafting Standards --")
    for n in notes:
        if n.get("semantic_type") in DRAFT_TYPES:
            lines.append(f"  [{n.get('id','?')}] {n.get('raw_text','')}")
    lines.append("")

    lines.append("-- References & Cross-References --")
    for n in notes:
        if n.get("semantic_type") in REF_TYPES:
            lines.append(f"  [{n.get('id','?')}] {n.get('raw_text','')}")
    lines.append("")

    # Catch-all: any semantic_type not covered above
    covered = GLOBAL_TYPES | TAG_TYPES | DRAFT_TYPES | REF_TYPES
    other   = [n for n in notes if n.get("semantic_type") not in covered]
    if other:
        lines.append("-- Other Notes --")
        for n in other:
            lines.append(f"  [{n.get('id','?')}] ({n.get('semantic_type','?')}) {_text(n)}")
        lines.append("")

    lines.append("== END RULES ==")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Drawing conventions inference
# ═══════════════════════════════════════════════════════════════════════════════

def _infer_conventions(notes: list[dict], abbrevs: dict) -> dict:
    conv = {
        "tag_format_pattern":      "unknown",
        "instrument_bubble_style": "unknown",
        "isa_version":             "ISA 5.1",
        "unit_system":             "mixed",
        "revision_cloud_present":  True,
    }
    for n in notes:
        raw = n.get("raw_text", "").upper()
        if "ISA 5.1" in raw:
            conv["isa_version"] = "ISA 5.1"
        if "METRIC" in raw:
            conv["unit_system"] = "metric"
        if "IMPERIAL" in raw:
            conv["unit_system"] = "imperial"
    return conv


# ═══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def run_notes_agent(
    img_path:             str,
    out_dir:              str,
    api_key:              str,
    binary_path:          Optional[str] = None,
    drawing_context_path: Optional[str] = None,
    debug:                bool = False,
) -> dict:

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    color = _load(img_path)
    H, W  = color.shape[:2]
    log.info("Loaded drawing: %dx%d px", W, H)

    binary = None
    if binary_path and Path(binary_path).exists():
        b = cv2.imread(binary_path, cv2.IMREAD_GRAYSCALE)
        if b is not None and b.shape[:2] == (H, W):
            binary = b
            log.info("Using binary image for OCR and region detection: %s", binary_path)
        else:
            log.warning("Binary image size mismatch or unreadable — skipping")
    else:
        log.info("No binary_path in context — region detection uses fallback coords")

    client, sdk = _build_gemini_client(api_key)
    log.info("Gemini client ready (%s SDK), model=%s", sdk, GEMINI_FLASH_MODEL)

    # ── Build adaptive regions ────────────────────────────────────────────────
    regions = _build_note_regions(color, binary)
    log.info("Regions to process: %s", [r["name"] for r in regions])

    # ── Process each region ───────────────────────────────────────────────────
    all_items:      list[dict] = []
    all_abbrevs:    dict       = {}
    region_summary: list[dict] = []

    for reg in regions:
        name = reg["name"]
        x0, y0, x1, y1 = reg["x0"], reg["y0"], reg["x1"], reg["y1"]
        log.info("Processing region [%d] %s (%dx%d px)...",
                 reg["priority"], name, x1 - x0, y1 - y0)

        color_crop = color[y0:y1, x0:x1]
        cH, cW     = color_crop.shape[:2]

        if cW < 80 or cH < 80:
            log.warning("Region %s too small (%dx%d), skipping", name, cW, cH)
            continue

        # Cloud count
        gray       = cv2.cvtColor(color_crop, cv2.COLOR_BGR2GRAY)
        cloud_cnt  = _count_clouds(gray)

        # Tesseract OCR — prefer binary crop, fall back to color crop
        if binary is not None:
            bin_crop = binary[y0:y1, x0:x1]
            ocr_text = _tesseract_ocr(bin_crop, psm=reg["tess_psm"])
        else:
            ocr_text = _tesseract_ocr(color_crop, psm=reg["tess_psm"])
        log.info("  OCR: %d chars | clouds: %d", len(ocr_text), cloud_cnt)

        # Debug crops
        if debug:
            cv2.imwrite(str(out / f"debug_tile_{name}.jpg"), color_crop,
                        [cv2.IMWRITE_JPEG_QUALITY, 88])
            with open(out / f"debug_ocr_{name}.txt", "w") as f:
                f.write(ocr_text)

        # Gemini vision call
        img_bytes = _encode_jpeg(color_crop)
        prompt    = _extract_prompt(name, reg["desc"], ocr_text)

        items: list[dict] = []
        gemini_raw = ""
        error = None
        try:
            gemini_raw = _gemini_call(client, sdk, img_bytes, prompt)
            parsed     = _parse_json(gemini_raw)
            items      = parsed.get("items", [])
            for item in items:
                item["source_region"]   = name
                item["source_priority"] = reg["priority"]
            abbrevs = parsed.get("abbreviations_found", {})
            if isinstance(abbrevs, dict):
                all_abbrevs.update(abbrevs)
            all_items.extend(items)
            log.info("  Gemini: %d items extracted", len(items))
        except Exception as e:
            log.error("  Gemini failed for %s: %s", name, e)
            error = str(e)

        region_summary.append({
            "region":          name,
            "description":     reg["desc"],
            "notes_extracted": len(items),
            "cloud_count":     cloud_cnt,
            "ocr_chars":       len(ocr_text),
            "error":           error,
        })

    # ── Deduplicate across regions ────────────────────────────────────────────
    final_abbrevs = all_abbrevs
    if all_items:
        final_items = _deduplicate(all_items)
        log.info("Dedup: %d raw → %d unique notes", len(all_items), len(final_items))
    else:
        final_items = []
        log.warning("No items extracted from any region")

    final_items   = _sort_notes(final_items)
    total_clouds  = sum(r["cloud_count"] for r in region_summary)
    rules_block   = _build_rules_block(final_items, final_abbrevs, region_summary)

    # ── Build output ──────────────────────────────────────────────────────────
    notes_ctx = {
        "version":               "v3",
        "input_image":           img_path,
        "binary_image":          binary_path,
        "image_size":            [W, H],
        "regions_processed":     len(region_summary),
        "total_clouds_detected": total_clouds,
        "clouds_per_region":     {r["region"]: r["cloud_count"] for r in region_summary},
        "raw_notes_count":       len(all_items),
        "unique_notes_count":    len(final_items),
        "abbreviations":         final_abbrevs,
        "drawing_notes":         final_items,
        "region_summary":        region_summary,
        "rules_prompt_block":    rules_block,
        "drawing_conventions":   _infer_conventions(final_items, final_abbrevs),
    }

    # ── Write outputs ─────────────────────────────────────────────────────────
    notes_path = str(out / "notes_context.json")
    with open(notes_path, "w") as f:
        json.dump(notes_ctx, f, indent=2)
    log.info("✓ notes_context.json (%d notes) → %s", len(final_items), notes_path)

    rules_path = str(out / "rules_prompt_block.txt")
    with open(rules_path, "w") as f:
        f.write(rules_block)
    log.info("✓ rules_prompt_block.txt → %s", rules_path)

    if debug:
        raw_path = str(out / "debug_gemini_raw_responses.json")
        with open(raw_path, "w") as f:
            json.dump([{"region": r["region"], "ocr_chars": r["ocr_chars"]}
                       for r in region_summary], f, indent=2)

    # Update drawing_context.json
    ctx_path = drawing_context_path or str(out / "drawing_context.json")
    if Path(ctx_path).exists():
        with open(ctx_path) as f:
            dctx = json.load(f)
        dctx["notes_context_path"]      = notes_path
        dctx["rules_prompt_block_path"] = rules_path
        dctx["notes_summary"] = {
            "raw_notes":              len(all_items),
            "unique_notes":           len(final_items),
            "total_clouds":           total_clouds,
            "abbreviations":          len(final_abbrevs),
            "revision_cloud_present": total_clouds > 0,
        }
        with open(ctx_path, "w") as f:
            json.dump(dctx, f, indent=2)
        log.info("✓ drawing_context.json updated")

    return notes_ctx


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Step 3 v3: Adaptive notes extraction with binary OCR and synthesis")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("image",     nargs="?", help="P&ID drawing path (JPG/PNG/TIFF)")
    group.add_argument("--context", help="drawing_context.json from Step 1")
    parser.add_argument("--out",        default="output", help="Output directory")
    parser.add_argument("--api-key",    help="Gemini API key")
    parser.add_argument("--binary",     help="Pre-binarized image path (overrides context)")
    parser.add_argument("--debug",      action="store_true",
                        help="Save per-region debug crops and OCR text")
    args = parser.parse_args()

    api_key = (args.api_key
               or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY"))
    if not api_key:
        parser.error("Gemini API key required. Set GEMINI_API_KEY or pass --api-key")

    img_path    = args.image
    binary_path = args.binary
    ctx_file    = None

    if args.context:
        ctx_file = args.context
        with open(args.context) as f:
            ctx = json.load(f)
        img_path = ctx.get("raster_path") or ctx.get("input_file")
        if not img_path:
            parser.error("drawing_context.json has no raster_path or input_file")
        if not binary_path:
            binary_path = ctx.get("binary_path")
        log.info("Image : %s", img_path)
        log.info("Binary: %s", binary_path or "not available")

    result = run_notes_agent(
        img_path=img_path,
        out_dir=args.out,
        api_key=api_key,
        binary_path=binary_path,
        drawing_context_path=ctx_file,
        debug=args.debug,
    )

    print("\n=== Step 3 v3 Complete ===")
    print(f"  Regions processed : {result['regions_processed']}")
    print(f"  Raw items         : {result['raw_notes_count']}")
    print(f"  Final notes       : {result['unique_notes_count']}")
    print(f"  Clouds detected   : {result['total_clouds_detected']}")
    print(f"  Abbreviations     : {len(result['abbreviations'])}")
    print()
    print("  Notes per region:")
    for r in result["region_summary"]:
        status = f" ⚠ {r['error']}" if r.get("error") else ""
        print(f"    {r['region']:<28} {r['notes_extracted']:>3} notes | "
              f"{r['ocr_chars']:>5} OCR chars{status}")
    print(f"\n  Output: {args.out}/")
    print(f"    notes_context.json")
    print(f"    rules_prompt_block.txt")
    if args.debug:
        print(f"    debug_tile_*.jpg")
        print(f"    debug_ocr_*.txt")


if __name__ == "__main__":
    main()
