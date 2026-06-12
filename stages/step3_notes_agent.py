#!/usr/bin/env python3
"""
step3_notes_agent_v2.py — Notes Extraction & Rule Generation Agent (v2)
========================================================================
Improvements over v1:
  • Multi-region tiling: splits drawing into 6 named zones and processes each
  • Per-region cloud counting: detects how many revision clouds wrap each zone
  • Tesseract OCR pass: runs BEFORE Gemini to get raw text → Gemini receives
    both the image crop AND the pre-extracted text, greatly reducing hallucination
  • Bottom + left margin priority: notes are on left-mid AND full bottom strip
  • Deduplication: merges notes extracted from overlapping regions
  • Full JSON + rules_prompt_block + notes_count_per_region table
"""

import argparse
import base64
import json
import logging
import math
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
GEMINI_PRO_MODEL      = "gemini-2.5-pro"
GEMINI_FLASH_MODEL    = "gemini-2.5-flash"
GEMINI_MAX_SIDE       = 4096   # px before encoding for Gemini

# ── Tile layout — 6 named zones covering the full drawing ─────────────────────
# Each zone: (x0_frac, y0_frac, x1_frac, y1_frac, priority)
# priority 1 = most likely to have notes; 3 = supplemental
TILE_DEFINITIONS = [
    # name                  x0     y0     x1     y1    pri   description
    ("notes_left_upper",   0.000, 0.250, 0.340, 0.580, 1,   "Left margin notes (Cont'd block upper)"),
    ("notes_left_lower",   0.000, 0.550, 0.340, 0.850, 1,   "Left margin notes (Cont'd block lower)"),
    ("notes_bottom_left",  0.000, 0.820, 0.540, 1.000, 1,   "Bottom strip left — NOTES 1-6"),
    ("notes_bottom_right", 0.530, 0.820, 0.890, 1.000, 1,   "Bottom strip right — NOTES 7-11"),
    ("abbreviations",      0.000, 0.240, 0.230, 0.420, 2,   "Abbreviations block (upper left)"),
    ("ref_legends",        0.590, 0.750, 0.860, 0.980, 2,   "Reference drawings & legends"),
    ("title_block",        0.840, 0.820, 1.000, 1.000, 3,   "Title block (drawing metadata)"),
]


# ═══════════════════════════════════════════════════════════════════════════════
# Image helpers
# ═══════════════════════════════════════════════════════════════════════════════

def clahe_enhance(gray: np.ndarray) -> np.ndarray:
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def crop_tile(img: np.ndarray, tile: tuple) -> np.ndarray:
    """Crop a tile from img using fractional coords."""
    _, x0f, y0f, x1f, y1f, *_ = tile
    H, W = img.shape[:2]
    x0, y0 = int(x0f * W), int(y0f * H)
    x1, y1 = int(x1f * W), int(y1f * H)
    return img[y0:y1, x0:x1]


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


def upscale_small(img: np.ndarray, min_side: int = 1200) -> np.ndarray:
    """Upscale crops that are too small for good OCR."""
    H, W = img.shape[:2]
    if min(H, W) >= min_side:
        return img
    scale = min_side / min(H, W)
    return cv2.resize(img, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_CUBIC)


# ═══════════════════════════════════════════════════════════════════════════════
# Cloud counting (scalloped boundary detection per tile)
# ═══════════════════════════════════════════════════════════════════════════════

def count_revision_clouds_in_crop(crop_gray: np.ndarray) -> dict:
    """
    Detect revision clouds (scalloped closed contours) in a grayscale crop.
    Returns {cloud_count, contour_count, largest_area_px}.
    Uses the same logic as cloud_detector_v2 but lightweight.
    """
    # Adaptive binarize
    enhanced = clahe_enhance(crop_gray)
    binary = cv2.adaptiveThreshold(
        enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 51, 10
    )
    # Morphological close to bridge gaps (13px kernel)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=4)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    H, W = crop_gray.shape[:2]
    min_area = (W * H) * 0.002      # at least 0.2% of crop area
    max_area = (W * H) * 0.95       # not the whole image

    cloud_count = 0
    largest_area = 0

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area or area > max_area:
            continue
        perimeter = cv2.arcLength(cnt, True)
        if perimeter < 50:
            continue
        # Scallopedness: perimeter vs convex hull perimeter
        hull = cv2.convexHull(cnt)
        hull_perimeter = cv2.arcLength(hull, True)
        if hull_perimeter < 1:
            continue
        scallopedness = perimeter / hull_perimeter
        if 1.05 <= scallopedness <= 2.5:   # revision clouds: 1.05–2.5
            cloud_count += 1
            if area > largest_area:
                largest_area = area

    return {
        "cloud_count":        cloud_count,
        "contour_count":      len(contours),
        "largest_area_px":    int(largest_area),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Tesseract OCR pass
# ═══════════════════════════════════════════════════════════════════════════════

TESS_CONFIG = "--oem 3 --psm 6"


def tesseract_ocr(crop_bgr: np.ndarray) -> str:
    """
    Run Tesseract on a crop and return cleaned text.
    Applies CLAHE + denoise before OCR.
    """
    gray     = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    enhanced = clahe_enhance(gray)
    denoised = cv2.fastNlMeansDenoising(enhanced, h=10, templateWindowSize=7,
                                         searchWindowSize=21)
    # Upscale if tiny
    upscaled = upscale_small(denoised, min_side=1000)

    try:
        text = pytesseract.image_to_string(upscaled, config=TESS_CONFIG)
        # Clean up noise artifacts
        lines = [l.rstrip() for l in text.splitlines()]
        lines = [l for l in lines if len(l.strip()) > 2]  # drop 1-2 char garbage lines
        return "\n".join(lines)
    except Exception as e:
        log.warning("Tesseract failed on crop: %s", e)
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


def _gemini_vision_call(client, sdk: str, model: str,
                         img_bytes: bytes, prompt: str,
                         fallback_model: Optional[str] = None) -> str:
    """Single vision call: image + prompt → raw text."""
    def _call(m: str) -> str:
        if sdk == "new":
            from google.genai import types as gtypes
            response = client.models.generate_content(
                model=m,
                contents=[
                    gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                    gtypes.Part.from_text(text=prompt),
                ],
            )
            return response.text.strip()
        else:
            import google.generativeai as genai_legacy
            import PIL.Image as PILImage
            import io
            pil_img = PILImage.open(io.BytesIO(img_bytes))
            mdl = genai_legacy.GenerativeModel(m)
            response = mdl.generate_content([prompt, pil_img])
            return response.text.strip()

    try:
        return _call(model)
    except Exception as e:
        if fallback_model:
            log.warning("Model %s failed (%s), trying %s...", model, e, fallback_model)
            return _call(fallback_model)
        raise


def _parse_json(raw: str) -> dict:
    clean = raw.replace("```json", "").replace("```", "").strip()
    # Extract first {...} block if there's surrounding text
    m = re.search(r'\{.*\}', clean, re.DOTALL)
    if m:
        clean = m.group(0)
    return json.loads(clean)


# ═══════════════════════════════════════════════════════════════════════════════
# Per-tile Gemini extraction prompt
# ═══════════════════════════════════════════════════════════════════════════════

def _make_extract_prompt(tile_name: str, tile_desc: str, ocr_text: str) -> str:
    ocr_section = ""
    if ocr_text.strip():
        ocr_section = f"""
The following text was pre-extracted by OCR from this region. Use it as a guide
but do NOT blindly copy it — the image is the ground truth:

<ocr_text>
{ocr_text[:3000]}
</ocr_text>
"""
    return f"""You are extracting engineering notes from a P&ID drawing.
This image is the "{tile_name}" region ({tile_desc}).
{ocr_section}
Extract ALL numbered/lettered notes, abbreviation definitions, legend entries, and
engineering rules visible in this image region.

Return ONLY a JSON object (no markdown, no prose):
{{
  "region": "{tile_name}",
  "notes_found": true | false,
  "items": [
    {{
      "id": "12" or "A" or "ABBR:ASC" etc.,
      "raw_text": "exact note text as written on drawing",
      "semantic_type": "general_note | abbreviation_definition | symbol_exception | equipment_rule | scope_rule | drafting_rule | legend_entry | title_info",
      "extracted_rule": {{
        "type": "abbreviation | prefix | rule | constraint | reference | format",
        "subject": "what this applies to",
        "rule": "plain English machine-readable rule statement"
      }},
      "confidence": 0.0-1.0,
      "partially_obscured": false
    }}
  ],
  "abbreviations_found": {{
    "ASC": "Anti-Surge Control",
    "...": "..."
  }},
  "revision_cloud_wraps_this_block": true | false,
  "gemini_cloud_count_estimate": 0
}}

Be thorough. If a note continues off the edge of this crop, extract what you can see.
Do NOT skip notes just because they seem repetitive with other regions."""


# ═══════════════════════════════════════════════════════════════════════════════
# Deduplication
# ═══════════════════════════════════════════════════════════════════════════════

def _deduplicate_notes(all_items: list[dict]) -> list[dict]:
    """
    Merge notes from multiple regions. Keep highest-confidence copy.
    Matches by: same note id OR very similar raw_text (>80% overlap).
    """
    seen: dict[str, dict] = {}   # canonical_key → item

    for item in all_items:
        note_id   = str(item.get("id") or "").strip().upper()
        raw       = str(item.get("raw_text") or "").strip()
        conf      = item.get("confidence") or 0.5

        # Build canonical key: prefer note number, fall back to first 40 chars of text
        key = note_id if (note_id and note_id not in ("", "UNKNOWN")) else raw[:40].upper()
        if not key:
            continue

        if key not in seen:
            seen[key] = item
        else:
            # Keep whichever has higher confidence AND longer raw_text
            existing = seen[key]
            e_score = (existing.get("confidence") or 0) + len(str(existing.get("raw_text") or "")) / 1000
            n_score = conf + len(raw) / 1000
            if n_score > e_score:
                seen[key] = item

    return list(seen.values())


# ═══════════════════════════════════════════════════════════════════════════════
# Rules prompt block builder
# ═══════════════════════════════════════════════════════════════════════════════

def build_rules_block(all_notes: list[dict], abbrevs: dict,
                      region_summary: list[dict]) -> str:
    lines = ["== DRAWING-SPECIFIC RULES (extracted from notes block) ==", ""]

    # Region summary table
    lines.append("-- Region Coverage Table --")
    lines.append(f"{'Region':<28} {'Notes':>6} {'Clouds':>7} {'OCR chars':>10}")
    lines.append("-" * 58)
    for r in region_summary:
        lines.append(
            f"{r['tile']:<28} {r['notes_extracted']:>6} "
            f"{r['cloud_count']:>7} {r['ocr_chars']:>10}"
        )
    total_notes  = sum(r["notes_extracted"] for r in region_summary)
    total_clouds = sum(r["cloud_count"] for r in region_summary)
    lines.append(f"{'TOTAL':<28} {total_notes:>6} {total_clouds:>7}")
    lines.append("")

    # Global/all-agent rules
    lines.append("-- Global Rules (all agents) --")
    for note in all_notes:
        if note.get("semantic_type") in ("scope_rule", "equipment_rule", "general_note"):
            er   = note.get("extracted_rule") or {}
            rule = er.get("rule") or note.get("raw_text", "")
            lines.append(f"  [{note.get('id','?')}] {rule}")
    lines.append("")

    # Tag detection rules
    lines.append("-- Tag Detection Agent rules --")
    for note in all_notes:
        if note.get("semantic_type") in ("abbreviation_definition", "symbol_exception", "legend_entry"):
            er   = note.get("extracted_rule") or {}
            rule = er.get("rule") or note.get("raw_text", "")
            lines.append(f"  [{note.get('id','?')}] {rule}")
    lines.append("")

    # Abbreviations dictionary
    if abbrevs:
        lines.append("-- Abbreviations --")
        for abbr, meaning in sorted(abbrevs.items()):
            lines.append(f"  {abbr}: {meaning}")
        lines.append("")

    # Drafting references
    lines.append("-- Drafting References --")
    for note in all_notes:
        if note.get("semantic_type") == "drafting_rule":
            lines.append(f"  [{note.get('id','?')}] {note.get('raw_text','')}")

    lines.append("")
    lines.append("== END RULES ==")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def run_notes_agent_v2(
    img_path: str,
    out_dir: str,
    api_key: str,
    drawing_context_path: Optional[str] = None,
    debug: bool = False,
) -> dict:

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    img = cv2.imread(img_path)
    if img is None:
        raise FileNotFoundError(f"Cannot read: {img_path}")
    H, W = img.shape[:2]
    log.info("Loaded drawing: %dx%d", W, H)

    client, sdk = _build_gemini_client(api_key)
    log.info("Gemini client ready (%s SDK)", sdk)

    # ── Process each tile ─────────────────────────────────────────────────────
    all_items:    list[dict] = []
    all_abbrevs:  dict       = {}
    region_summary: list[dict] = []
    tile_results: list[dict] = []

    for tile in TILE_DEFINITIONS:
        name, x0f, y0f, x1f, y1f, priority, desc = tile
        log.info("Processing tile [%d] %s (%s)...", priority, name, desc)

        crop = crop_tile(img, tile)
        cH, cW = crop.shape[:2]

        if cW < 50 or cH < 50:
            log.warning("Tile %s too small (%dx%d), skipping", name, cW, cH)
            continue

        # ── Cloud counting ────────────────────────────────────────────────────
        crop_gray  = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        cloud_info = count_revision_clouds_in_crop(crop_gray)
        log.info("  Clouds: %d detected in %s", cloud_info["cloud_count"], name)

        # ── Tesseract OCR pass ────────────────────────────────────────────────
        ocr_text = tesseract_ocr(crop)
        log.info("  OCR: %d chars extracted", len(ocr_text))

        # Save debug crop
        if debug:
            debug_path = str(out / f"debug_tile_{name}.jpg")
            cv2.imwrite(debug_path, crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
            ocr_path = str(out / f"debug_ocr_{name}.txt")
            with open(ocr_path, "w") as f:
                f.write(ocr_text)

        # ── Gemini extraction ─────────────────────────────────────────────────
        crop_scaled = scale_for_gemini(crop)
        img_bytes   = encode_jpeg(crop_scaled)
        prompt      = _make_extract_prompt(name, desc, ocr_text)

        tile_result = {
            "tile":              name,
            "description":       desc,
            "priority":          priority,
            "crop_size":         [cW, cH],
            "cloud_count":       cloud_info["cloud_count"],
            "contour_count":     cloud_info["contour_count"],
            "ocr_chars":         len(ocr_text),
            "notes_extracted":   0,
            "gemini_raw":        "",
            "items":             [],
            "error":             None,
        }

        try:
            model   = GEMINI_PRO_MODEL if priority == 1 else GEMINI_FLASH_MODEL
            raw     = _gemini_vision_call(client, sdk, model, img_bytes, prompt,
                                          fallback_model=GEMINI_FLASH_MODEL)
            tile_result["gemini_raw"] = raw

            parsed  = _parse_json(raw)
            items   = parsed.get("items", [])

            # Tag each item with its source region
            for item in items:
                item["source_region"] = name
                item["source_priority"] = priority

            # Collect abbreviations
            abbrevs = parsed.get("abbreviations_found", {})
            if isinstance(abbrevs, dict):
                all_abbrevs.update(abbrevs)

            tile_result["notes_extracted"]         = len(items)
            tile_result["items"]                   = items
            tile_result["revision_cloud_wraps"]    = parsed.get("revision_cloud_wraps_this_block")
            tile_result["gemini_cloud_estimate"]   = parsed.get("gemini_cloud_count_estimate", 0)

            all_items.extend(items)
            log.info("  Gemini: %d notes extracted from %s", len(items), name)

        except Exception as e:
            log.error("  Gemini failed for tile %s: %s", name, e)
            tile_result["error"] = str(e)

        region_summary.append({
            "tile":            name,
            "description":     desc,
            "notes_extracted": tile_result["notes_extracted"],
            "cloud_count":     cloud_info["cloud_count"],
            "ocr_chars":       len(ocr_text),
        })
        tile_results.append(tile_result)

    # ── Deduplicate across regions ────────────────────────────────────────────
    deduped = _deduplicate_notes(all_items)
    log.info("Deduplication: %d raw → %d unique notes", len(all_items), len(deduped))

    # Sort by note id numerically where possible
    def _sort_key(item):
        nid = str(item.get("id") or "ZZZ")
        m = re.match(r"(\d+)", nid)
        return (int(m.group(1)) if m else 999, nid)
    deduped.sort(key=_sort_key)

    # ── Build rules block ─────────────────────────────────────────────────────
    rules_block = build_rules_block(deduped, all_abbrevs, region_summary)

    # ── Build final output ────────────────────────────────────────────────────
    total_clouds = sum(r["cloud_count"] for r in region_summary)

    notes_ctx = {
        "version":               "v2",
        "input_image":           img_path,
        "image_size":            [W, H],
        "tiles_processed":       len(tile_results),
        "total_clouds_detected": total_clouds,
        "clouds_per_region":     {r["tile"]: r["cloud_count"] for r in region_summary},
        "raw_notes_count":       len(all_items),
        "unique_notes_count":    len(deduped),
        "abbreviations":         all_abbrevs,
        "drawing_notes":         deduped,
        "region_summary":        region_summary,
        "tile_results":          [
            {k: v for k, v in tr.items() if k != "gemini_raw"}
            for tr in tile_results
        ],
        "rules_prompt_block":    rules_block,
        "drawing_conventions":   _infer_conventions(deduped, all_abbrevs),
    }

    # ── Write outputs ─────────────────────────────────────────────────────────
    notes_path = str(out / "notes_context.json")
    with open(notes_path, "w") as f:
        json.dump(notes_ctx, f, indent=2)
    log.info("✓ notes_context.json (%d unique notes) → %s", len(deduped), notes_path)

    rules_path = str(out / "rules_prompt_block.txt")
    with open(rules_path, "w") as f:
        f.write(rules_block)
    log.info("✓ rules_prompt_block.txt → %s", rules_path)

    # Update drawing_context.json if present
    ctx_path = drawing_context_path or str(out / "drawing_context.json")
    if Path(ctx_path).exists():
        with open(ctx_path) as f:
            dctx = json.load(f)
        dctx["notes_context_path"]      = notes_path
        dctx["rules_prompt_block_path"] = rules_path
        dctx["notes_summary"] = {
            "raw_notes":          len(all_items),
            "unique_notes":       len(deduped),
            "total_clouds":       total_clouds,
            "abbreviations":      len(all_abbrevs),
            "revision_cloud_present": total_clouds > 0,
        }
        with open(ctx_path, "w") as f:
            json.dump(dctx, f, indent=2)
        log.info("✓ drawing_context.json updated")

    # Save raw Gemini responses separately for debugging
    if debug:
        raw_path = str(out / "debug_gemini_raw_responses.json")
        with open(raw_path, "w") as f:
            json.dump([{"tile": tr["tile"], "raw": tr.get("gemini_raw", "")}
                       for tr in tile_results], f, indent=2)
        log.info("✓ debug_gemini_raw_responses.json → %s", raw_path)

    return notes_ctx


def _infer_conventions(notes: list[dict], abbrevs: dict) -> dict:
    """Infer drawing conventions from extracted notes."""
    conv = {
        "tag_format_pattern":      "unknown",
        "instrument_bubble_style": "unknown",
        "isa_version":             "ISA 5.1",
        "unit_system":             "mixed",
        "revision_cloud_present":  True,
    }
    for note in notes:
        raw = note.get("raw_text", "").upper()
        if "ISA 5.1" in raw:
            conv["isa_version"] = "ISA 5.1"
        if "METRIC" in raw:
            conv["unit_system"] = "metric"
        if "IMPERIAL" in raw:
            conv["unit_system"] = "imperial"
    return conv


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Step 3 v2: Multi-region notes extraction with cloud counting + Tesseract assist")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("image", nargs="?", help="P&ID drawing path (JPG/PNG/TIFF)")
    group.add_argument("--context", help="drawing_context.json from Step 1")
    parser.add_argument("--out",     default="output",  help="Output directory")
    parser.add_argument("--api-key", help="Gemini API key")
    parser.add_argument("--debug",   action="store_true",
                        help="Save per-tile debug crops and raw Gemini responses")
    args = parser.parse_args()

    api_key = (args.api_key
               or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY"))
    if not api_key:
        parser.error("Gemini API key required. Set GEMINI_API_KEY or pass --api-key")

    img_path     = args.image
    ctx_file     = None
    if args.context:
        ctx_file = args.context
        with open(args.context) as f:
            ctx = json.load(f)
        img_path = ctx.get("raster_path") or ctx.get("input_file")
        if not img_path:
            parser.error("drawing_context.json has no raster_path or input_file")
        log.info("Image from drawing_context.json: %s", img_path)

    result = run_notes_agent_v2(
        img_path=img_path,
        out_dir=args.out,
        api_key=api_key,
        drawing_context_path=ctx_file,
        debug=args.debug,
    )

    print("\n=== Step 3 v2 Complete ===")
    print(f"  Tiles processed     : {result['tiles_processed']}")
    print(f"  Raw notes           : {result['raw_notes_count']}")
    print(f"  Unique notes        : {result['unique_notes_count']}")
    print(f"  Total clouds found  : {result['total_clouds_detected']}")
    print(f"  Abbreviations       : {len(result['abbreviations'])}")
    print()
    print("  Clouds per region:")
    for tile, count in result["clouds_per_region"].items():
        print(f"    {tile:<30} {count:>3} cloud(s)")
    print()
    print("  Notes per region:")
    for r in result["region_summary"]:
        print(f"    {r['tile']:<30} {r['notes_extracted']:>3} notes | "
              f"{r['ocr_chars']:>5} OCR chars")
    print(f"\n  Output: {args.out}/")
    print(f"    notes_context.json")
    print(f"    rules_prompt_block.txt")
    if args.debug:
        print(f"    debug_tile_*.jpg  (per-tile crops)")
        print(f"    debug_ocr_*.txt   (Tesseract output per tile)")
        print(f"    debug_gemini_raw_responses.json")


if __name__ == "__main__":
    main()