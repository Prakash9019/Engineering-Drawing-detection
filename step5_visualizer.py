#!/usr/bin/env python3
"""
step5_visualizer.py — Bounding Box Visualizer & Human-in-the-Loop Review Tool
==============================================================================
CDCI P&ID Pipeline — Step 5 QA Output

Purpose
-------
Generates annotated overlay images from Step 5A + Step 5D outputs for:
  1. Human-in-the-loop verification of detected symbols
  2. Duplicate detection review (RED boxes = duplicates/discarded)
  3. Final clean output image (PRIMARY candidates only)

Outputs
-------
  viz_all_candidates.jpg        — All 5A detections coloured by category
  viz_duplicates_highlighted.jpg — RED = DISCARDED duplicates, GREEN = PRIMARY kept
  viz_final_clean.jpg           — Final PRIMARY-only output (post-dedup)
  viz_tiles/tile_RxC.jpg        — Zoomable tiles (1600×1600px each) of final output
  viz_summary.json              — Detection counts, duplicate rate, category breakdown

Colour scheme
-------------
  GREEN   #00C800  instrument  (FIT, PT, TT, LT, etc.)
  ORANGE  #FF7800  valve       (BV, GV, XV, FCV, etc.)
  PURPLE  #C800C8  equipment   (vessels, compressors, pumps)
  CYAN    #00C8C8  piping      (line designations)
  GREY    #808080  unknown
  RED     #FF0000  DUPLICATE / DISCARDED  ← human review required
  YELLOW  #FFFF00  UNSPECIFIED SOW status ← needs scope review

Usage
-----
  # After Step 5A only:
  python step5_visualizer.py \\
      --candidates output/step5a_candidates.json \\
      --image drawing.jpg --out output/

  # After full Step 5 pipeline (5A + 5D):
  python step5_visualizer.py \\
      --candidates output/step5a_candidates.json \\
      --deduped    output/step5d_deduped.json \\
      --image      drawing.jpg --out output/

  # With drawing_context.json (auto-detects all paths):
  python step5_visualizer.py \\
      --context output/drawing_context.json

  # Control tile size and output scale:
  python step5_visualizer.py \\
      --candidates output/step5a_candidates.json \\
      --image drawing.jpg --out output/ \\
      --tile-size 2000 --overview-width 3200
"""

import argparse
import json
import logging
import math
import os
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Colour palette (BGR for OpenCV) ───────────────────────────────────────────
COLOURS = {
    "instrument":  (  0, 200,   0),   # green
    "valve":       (  0, 120, 255),   # orange
    "equipment":   (200,   0, 200),   # purple
    "piping":      (200, 200,   0),   # cyan
    "unknown":     (128, 128, 128),   # grey
    # Status overrides
    "duplicate":   (  0,   0, 255),   # RED  — discarded duplicate
    "primary":     ( 50, 205,  50),   # lime green — kept primary
    "unspecified": (  0, 200, 200),   # yellow — SOW unspecified
    "out_of_scope":(  0,   0, 180),   # dark red
}

LEGEND_LABELS = {
    "instrument":   "Instrument (FIT/PT/TT/LT…)",
    "valve":        "Valve (BV/GV/XV/FCV…)",
    "equipment":    "Equipment (Vessel/Pump/Comp…)",
    "piping":       "Piping (Line designation)",
    "unknown":      "Unknown category",
    "duplicate":    "DUPLICATE — discarded (red = review)",
    "primary":      "PRIMARY — kept after dedup",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Drawing utilities
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_bbox(b: dict) -> Optional[tuple[int, int, int, int]]:
    """Return (x1,y1,x2,y2) or None if bbox is empty/zero-size."""
    if not b:
        return None
    x1 = int(b.get("x1") or 0)
    y1 = int(b.get("y1") or 0)
    x2 = int(b.get("x2") or 0)
    y2 = int(b.get("y2") or 0)
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def draw_candidate(
    img:        np.ndarray,
    cand:       dict,
    colour:     tuple,
    thickness:  int   = 4,
    font_scale: float = 1.8,
    label_bg:   bool  = True,
) -> None:
    """
    Draw symbol bbox + label on img in-place.
    Label format:  [CATEGORY]  TAG_TEXT
    """
    coords = _safe_bbox(cand.get("symbol_bbox") or {})
    if not coords:
        return
    x1, y1, x2, y2 = coords

    # ── Rectangle ─────────────────────────────────────────────────────────────
    cv2.rectangle(img, (x1, y1), (x2, y2), colour, thickness)

    # ── Label text ────────────────────────────────────────────────────────────
    tag    = str(cand.get("tag_text")       or "").strip()[:20]
    cat    = str(cand.get("symbol_category") or "?").upper()[:3]
    conf_v = cand.get("vision_confidence")  or 0.0
    conf_o = cand.get("ocr_confidence")     or 0.0
    label  = f"[{cat}] {tag}" if tag else f"[{cat}]"
    conf_label = f"v{conf_v:.2f}/o{conf_o:.2f}"

    font        = cv2.FONT_HERSHEY_SIMPLEX
    font_thick  = max(1, int(font_scale * 1.5))

    # Main label above box
    (lw, lh), baseline = cv2.getTextSize(label, font, font_scale, font_thick)
    lx = x1
    ly = max(y1 - 6, lh + 6)

    if label_bg:
        # Semi-opaque background rectangle for readability
        overlay = img.copy()
        cv2.rectangle(overlay,
                      (lx, ly - lh - baseline),
                      (lx + lw + 4, ly + baseline),
                      (20, 20, 20), -1)
        cv2.addWeighted(overlay, 0.55, img, 0.45, 0, img)

    cv2.putText(img, label, (lx + 2, ly),
                font, font_scale, colour, font_thick, cv2.LINE_AA)

    # Small confidence label below tag label
    (cw, ch), _ = cv2.getTextSize(conf_label, font, font_scale * 0.55, 1)
    cv2.putText(img, conf_label, (lx + 2, ly + ch + 4),
                font, font_scale * 0.55, (200, 200, 200), 1, cv2.LINE_AA)

    # ── Tag bbox (smaller inner box, dashed effect via dots) ──────────────────
    tb = _safe_bbox(cand.get("tag_bbox") or {})
    if tb and tb != coords:
        tx1, ty1, tx2, ty2 = tb
        # Draw as thin dotted box using line segments
        dash = 12
        for dx in range(tx1, tx2, dash * 2):
            cv2.line(img, (dx, ty1), (min(dx + dash, tx2), ty1),
                     colour, max(1, thickness // 2))
            cv2.line(img, (dx, ty2), (min(dx + dash, tx2), ty2),
                     colour, max(1, thickness // 2))
        for dy in range(ty1, ty2, dash * 2):
            cv2.line(img, (tx1, dy), (tx1, min(dy + dash, ty2)),
                     colour, max(1, thickness // 2))
            cv2.line(img, (tx2, dy), (tx2, min(dy + dash, ty2)),
                     colour, max(1, thickness // 2))


def draw_legend(img: np.ndarray, entries: dict[str, tuple],
                font_scale: float = 1.8) -> None:
    """
    Draw colour legend in top-right corner of the image.
    entries: {label_text: bgr_colour}
    """
    H, W    = img.shape[:2]
    font    = cv2.FONT_HERSHEY_SIMPLEX
    ft      = max(1, int(font_scale * 1.5))
    pad     = 20
    sw_size = 40   # colour swatch size

    # Measure all labels
    widths = [cv2.getTextSize(lbl, font, font_scale, ft)[0][0]
              for lbl in entries]
    max_w  = max(widths) + sw_size + pad * 3
    row_h  = int(font_scale * 30) + pad

    box_h  = row_h * len(entries) + pad * 2
    box_x  = W - max_w - pad
    box_y  = pad

    # Background
    overlay = img.copy()
    cv2.rectangle(overlay, (box_x - 10, box_y),
                  (W - pad + 10, box_y + box_h),
                  (15, 15, 15), -1)
    cv2.addWeighted(overlay, 0.75, img, 0.25, 0, img)

    for i, (lbl, col) in enumerate(entries.items()):
        y = box_y + pad + i * row_h + row_h // 2
        # Swatch
        cv2.rectangle(img,
                      (box_x, y - sw_size // 2),
                      (box_x + sw_size, y + sw_size // 2),
                      col, -1)
        # Label
        cv2.putText(img, lbl,
                    (box_x + sw_size + pad, y + int(font_scale * 10)),
                    font, font_scale, (230, 230, 230), ft, cv2.LINE_AA)


def draw_stats_banner(img: np.ndarray, stats: dict,
                       font_scale: float = 2.2) -> None:
    """Draw a stats banner at the bottom of the image."""
    H, W   = img.shape[:2]
    font   = cv2.FONT_HERSHEY_SIMPLEX
    ft     = max(2, int(font_scale * 1.5))
    banner_h = int(font_scale * 50)

    # Banner background
    cv2.rectangle(img, (0, H - banner_h), (W, H), (20, 20, 20), -1)

    text = "  |  ".join(f"{k}: {v}" for k, v in stats.items())
    cv2.putText(img, text, (20, H - int(banner_h * 0.3)),
                font, font_scale, (220, 220, 50), ft, cv2.LINE_AA)


# ═══════════════════════════════════════════════════════════════════════════════
# Tile exporter — splits large image into zoomable tiles
# ═══════════════════════════════════════════════════════════════════════════════

def export_tiles(img: np.ndarray, out_dir: Path,
                 tile_size: int = 2000,
                 prefix: str = "tile") -> list[str]:
    """
    Split annotated image into overlapping tiles for zoom-level inspection.
    Returns list of saved tile paths.
    """
    H, W    = img.shape[:2]
    rows    = math.ceil(H / tile_size)
    cols    = math.ceil(W / tile_size)
    paths   = []

    tile_dir = out_dir / "viz_tiles"
    tile_dir.mkdir(exist_ok=True)

    for r in range(rows):
        for c in range(cols):
            y0 = r * tile_size
            x0 = c * tile_size
            y1 = min(y0 + tile_size, H)
            x1 = min(x0 + tile_size, W)
            tile = img[y0:y1, x0:x1].copy()
            # Mark tile coordinates in corner
            cv2.putText(tile, f"R{r+1}C{c+1} [{x0},{y0}]",
                        (10, 50), cv2.FONT_HERSHEY_SIMPLEX,
                        1.2, (80, 80, 255), 2)
            path = str(tile_dir / f"{prefix}_R{r+1:02d}C{c+1:02d}.jpg")
            cv2.imwrite(path, tile, [cv2.IMWRITE_JPEG_QUALITY, 90])
            paths.append(path)

    log.info("Tiles: %dx%d grid → %d tiles in %s", rows, cols, len(paths), tile_dir)
    return paths


def save_overview(img: np.ndarray, path: str,
                  max_width: int = 3200) -> str:
    """Save a downscaled overview image."""
    H, W = img.shape[:2]
    if W > max_width:
        scale = max_width / W
        small = cv2.resize(img, (max_width, int(H * scale)),
                           interpolation=cv2.INTER_AREA)
    else:
        small = img
    cv2.imwrite(path, small, [cv2.IMWRITE_JPEG_QUALITY, 88])
    log.info("Overview saved: %s (%dx%d)", path, small.shape[1], small.shape[0])
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# Main visualization functions
# ═══════════════════════════════════════════════════════════════════════════════

def viz_all_candidates(
    img_bgr:    np.ndarray,
    candidates: list[dict],
    out_dir:    Path,
    tile_size:  int   = 2000,
    overview_w: int   = 3200,
) -> dict:
    """
    Render ALL Step 5A candidates coloured by category.
    Returns {output_paths, category_counts}.
    """
    canvas = img_bgr.copy()
    H, W   = canvas.shape[:2]

    # Dynamic scale based on image size
    font_scale = max(1.2, W / 4000)
    thickness  = max(3, W // 2000)

    cat_counts: dict[str, int] = {}

    for cand in candidates:
        cat    = str(cand.get("symbol_category") or "unknown").lower()
        colour = COLOURS.get(cat, COLOURS["unknown"])

        # SOW override
        sow = str(cand.get("sow_status") or "")
        if sow == "UNSPECIFIED":
            colour = COLOURS["unspecified"]
        elif sow == "OUT_OF_SCOPE":
            colour = COLOURS["out_of_scope"]

        draw_candidate(canvas, cand, colour,
                       thickness=thickness, font_scale=font_scale)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    # Legend
    legend_entries = {
        "Instrument":    COLOURS["instrument"],
        "Valve":         COLOURS["valve"],
        "Equipment":     COLOURS["equipment"],
        "Piping":        COLOURS["piping"],
        "Unknown":       COLOURS["unknown"],
        "SOW Unspecified": COLOURS["unspecified"],
        "SOW Out-of-Scope": COLOURS["out_of_scope"],
    }
    draw_legend(canvas, legend_entries, font_scale=font_scale * 0.8)

    stats = {"Total": len(candidates), **{k.upper()[:4]: v
             for k, v in cat_counts.items()}}
    draw_stats_banner(canvas, stats, font_scale=font_scale * 0.9)

    # Save overview
    overview_path = str(out_dir / "viz_all_candidates.jpg")
    save_overview(canvas, overview_path, max_width=overview_w)

    # Save full-res
    fullres_path = str(out_dir / "viz_all_candidates_fullres.jpg")
    cv2.imwrite(fullres_path, canvas, [cv2.IMWRITE_JPEG_QUALITY, 85])
    log.info("Full-res all-candidates: %s", fullres_path)

    # Tiles
    tile_paths = export_tiles(canvas, out_dir, tile_size, prefix="all")

    return {
        "overview":    overview_path,
        "fullres":     fullres_path,
        "tiles":       tile_paths,
        "cat_counts":  cat_counts,
    }


def viz_duplicates(
    img_bgr:  np.ndarray,
    deduped:  list[dict],
    out_dir:  Path,
    tile_size: int  = 2000,
    overview_w: int = 3200,
) -> dict:
    """
    Render duplicate detection results:
      GREEN box  = PRIMARY (kept)
      RED box    = DISCARDED duplicate
      Also draws IoU overlap lines between duplicate pairs.
    """
    canvas = img_bgr.copy()
    H, W   = canvas.shape[:2]
    font_scale = max(1.2, W / 4000)
    thickness  = max(3, W // 2000)

    primary_count   = 0
    discarded_count = 0

    # Build candidate_id → record map for drawing merge lines
    id_to_cand = {c["candidate_id"]: c for c in deduped}

    for cand in deduped:
        status = str(cand.get("duplicate_status") or "PRIMARY")

        if status == "PRIMARY":
            colour = COLOURS["primary"]
            primary_count += 1
        else:
            colour = COLOURS["duplicate"]   # RED
            discarded_count += 1

        draw_candidate(canvas, cand, colour,
                       thickness=thickness, font_scale=font_scale)

        # Draw merge arrows: DISCARDED → PRIMARY (shows which was merged into what)
        if status == "DISCARDED":
            merged_into = cand.get("merged_into")
            if merged_into and merged_into in id_to_cand:
                primary_c = id_to_cand[merged_into]
                sb_d = _safe_bbox(cand.get("symbol_bbox") or {})
                sb_p = _safe_bbox(primary_c.get("symbol_bbox") or {})
                if sb_d and sb_p:
                    # Centre of discarded → centre of primary
                    cx_d = (sb_d[0] + sb_d[2]) // 2
                    cy_d = (sb_d[1] + sb_d[3]) // 2
                    cx_p = (sb_p[0] + sb_p[2]) // 2
                    cy_p = (sb_p[1] + sb_p[3]) // 2
                    cv2.arrowedLine(canvas,
                                    (cx_d, cy_d), (cx_p, cy_p),
                                    (0, 60, 255),  # dark red arrow
                                    max(2, thickness - 1),
                                    tipLength=0.03)
                    # Label the arrow with IoU (if we can compute it)
                    mid_x = (cx_d + cx_p) // 2
                    mid_y = (cy_d + cy_p) // 2
                    cv2.putText(canvas, "DUP",
                                (mid_x, mid_y),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                font_scale * 0.7,
                                (0, 60, 255), max(1, thickness - 1),
                                cv2.LINE_AA)

    # Legend
    legend_entries = {
        "PRIMARY — kept":     COLOURS["primary"],
        "DISCARDED duplicate": COLOURS["duplicate"],
    }
    draw_legend(canvas, legend_entries, font_scale=font_scale * 0.8)

    total = primary_count + discarded_count
    dup_rate = discarded_count / max(total, 1) * 100
    draw_stats_banner(canvas, {
        "Total":      total,
        "PRIMARY":    primary_count,
        "DISCARDED":  discarded_count,
        f"Dup rate":  f"{dup_rate:.1f}%",
    }, font_scale=font_scale * 0.9)

    overview_path = str(out_dir / "viz_duplicates_highlighted.jpg")
    save_overview(canvas, overview_path, max_width=overview_w)

    fullres_path = str(out_dir / "viz_duplicates_fullres.jpg")
    cv2.imwrite(fullres_path, canvas, [cv2.IMWRITE_JPEG_QUALITY, 85])

    tile_paths = export_tiles(canvas, out_dir, tile_size, prefix="dup")

    return {
        "overview":       overview_path,
        "fullres":        fullres_path,
        "tiles":          tile_paths,
        "primary_count":  primary_count,
        "discarded_count": discarded_count,
        "duplicate_rate": round(dup_rate, 2),
    }


def viz_final_clean(
    img_bgr:    np.ndarray,
    final:      list[dict],
    out_dir:    Path,
    tile_size:  int   = 2000,
    overview_w: int   = 3200,
) -> dict:
    """
    Final clean output: PRIMARY candidates only, coloured by category.
    This is the deliverable image for client review.
    """
    canvas = img_bgr.copy()
    H, W   = canvas.shape[:2]
    font_scale = max(1.2, W / 4000)
    thickness  = max(3, W // 2000)

    cat_counts: dict[str, int] = {}
    val_counts  = {"PASS": 0, "WARN": 0, "FAIL": 0}

    for cand in final:
        cat    = str(cand.get("symbol_category") or "unknown").lower()
        colour = COLOURS.get(cat, COLOURS["unknown"])

        val = str(cand.get("validation_status") or "WARN")
        val_counts[val] = val_counts.get(val, 0) + 1

        # Visual indicator for validation status: dim the colour for FAIL
        if val == "FAIL":
            # Darken colour by 40%
            colour = tuple(int(c * 0.6) for c in colour)
        elif val == "WARN":
            pass  # keep colour, draw thinner box
            thickness_use = max(2, thickness - 1)
        else:
            thickness_use = thickness

        draw_candidate(canvas, cand, colour,
                       thickness=thickness_use if val != "FAIL" else thickness,
                       font_scale=font_scale)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    legend_entries = {
        "Instrument":  COLOURS["instrument"],
        "Valve":       COLOURS["valve"],
        "Equipment":   COLOURS["equipment"],
        "Piping":      COLOURS["piping"],
        "Unknown":     COLOURS["unknown"],
    }
    draw_legend(canvas, legend_entries, font_scale=font_scale * 0.8)

    draw_stats_banner(canvas, {
        "Final tags": len(final),
        "PASS":  val_counts["PASS"],
        "WARN":  val_counts["WARN"],
        "FAIL":  val_counts["FAIL"],
    }, font_scale=font_scale * 0.9)

    overview_path = str(out_dir / "viz_final_clean.jpg")
    save_overview(canvas, overview_path, max_width=overview_w)

    fullres_path = str(out_dir / "viz_final_clean_fullres.jpg")
    cv2.imwrite(fullres_path, canvas, [cv2.IMWRITE_JPEG_QUALITY, 88])

    tile_paths = export_tiles(canvas, out_dir, tile_size, prefix="final")

    return {
        "overview":    overview_path,
        "fullres":     fullres_path,
        "tiles":       tile_paths,
        "cat_counts":  cat_counts,
        "val_counts":  val_counts,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def run_visualizer(
    img_path:       str,
    candidates_path: Optional[str],
    deduped_path:   Optional[str],
    final_path:     Optional[str],
    out_dir:        str,
    tile_size:      int = 2000,
    overview_width: int = 3200,
) -> dict:

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Load drawing
    img = cv2.imread(img_path)
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {img_path}")
    H, W = img.shape[:2]
    log.info("Drawing: %dx%d", W, H)

    results = {}

    # ── Viz 1: All candidates (Step 5A output) ────────────────────────────────
    if candidates_path and Path(candidates_path).exists():
        with open(candidates_path) as f:
            data = json.load(f)
        candidates = data.get("candidates", [])
        log.info("=== Viz 1: All candidates (%d) ===", len(candidates))
        r1 = viz_all_candidates(img, candidates, out, tile_size, overview_width)
        results["all_candidates"] = r1
        log.info("✓ viz_all_candidates.jpg + %d tiles", len(r1["tiles"]))

    # ── Viz 2: Duplicate highlights (Step 5D output) ──────────────────────────
    if deduped_path and Path(deduped_path).exists():
        with open(deduped_path) as f:
            data = json.load(f)
        deduped = data.get("all_records", data.get("candidates", []))
        log.info("=== Viz 2: Duplicates (%d records) ===", len(deduped))
        r2 = viz_duplicates(img, deduped, out, tile_size, overview_width)
        results["duplicates"] = r2
        log.info("✓ viz_duplicates_highlighted.jpg | dup rate=%.1f%%",
                 r2["duplicate_rate"])

    # ── Viz 3: Final clean output (PRIMARY only) ──────────────────────────────
    src_final = final_path or str(out / "step5_final_output.json")
    if Path(src_final).exists():
        with open(src_final) as f:
            data = json.load(f)
        final = data.get("candidates", [])
        log.info("=== Viz 3: Final clean (%d PRIMARY candidates) ===", len(final))
        r3 = viz_final_clean(img, final, out, tile_size, overview_width)
        results["final_clean"] = r3
        log.info("✓ viz_final_clean.jpg + %d tiles", len(r3["tiles"]))
    elif candidates_path and Path(candidates_path).exists():
        # Fallback: treat all 5A candidates as final (no dedup ran yet)
        log.info("No step5_final_output.json — using 5A candidates as final")
        with open(candidates_path) as f:
            data = json.load(f)
        r3 = viz_final_clean(img, data.get("candidates", []),
                              out, tile_size, overview_width)
        results["final_clean"] = r3

    # ── Summary JSON ──────────────────────────────────────────────────────────
    summary = {
        "version":         "v1",
        "input_image":     img_path,
        "image_size":      [W, H],
        "tile_size":       tile_size,
        "overview_width":  overview_width,
        "outputs":         results,
    }
    summary_path = str(out / "viz_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    log.info("✓ viz_summary.json → %s", summary_path)

    return summary


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Step 5 Visualizer — bbox overlays for human review")
    parser.add_argument("--image",       help="Drawing image (JPG/PNG/TIFF)")
    parser.add_argument("--candidates",  help="step5a_candidates.json")
    parser.add_argument("--deduped",     help="step5d_deduped.json")
    parser.add_argument("--final",       help="step5_final_output.json")
    parser.add_argument("--context",     help="drawing_context.json (auto-detect all)")
    parser.add_argument("--out",         default="output")
    parser.add_argument("--tile-size",   type=int, default=2000,
                        help="Tile size in px (default: 2000)")
    parser.add_argument("--overview-width", type=int, default=3200,
                        help="Overview image width in px (default: 3200)")
    args = parser.parse_args()

    img_path        = args.image
    candidates_path = args.candidates
    deduped_path    = args.deduped
    final_path      = args.final
    out_dir         = args.out

    if args.context:
        with open(args.context) as f:
            ctx = json.load(f)
        img_path = img_path or ctx.get("raster_path") or ctx.get("input_file")

    # Auto-detect paths from output directory
    out_p = Path(out_dir)
    candidates_path = candidates_path or str(out_p / "step5a_candidates.json")
    deduped_path    = deduped_path    or str(out_p / "step5d_deduped.json")
    final_path      = final_path      or str(out_p / "step5_final_output.json")

    if not img_path:
        parser.error("--image or --context required")

    summary = run_visualizer(
        img_path        = img_path,
        candidates_path = candidates_path,
        deduped_path    = deduped_path,
        final_path      = final_path,
        out_dir         = out_dir,
        tile_size       = args.tile_size,
        overview_width  = args.overview_width,
    )

    print(f"\n=== Step 5 Visualizer Complete ===")
    for viz_name, viz_data in summary.get("outputs", {}).items():
        print(f"\n  [{viz_name}]")
        print(f"    Overview  : {viz_data.get('overview', '')}")
        print(f"    Full-res  : {viz_data.get('fullres', '')}")
        print(f"    Tiles     : {len(viz_data.get('tiles', []))} tiles in output/viz_tiles/")
        if "cat_counts" in viz_data:
            for k, v in viz_data["cat_counts"].items():
                print(f"      {k:<15} {v:>4}")
        if "duplicate_rate" in viz_data:
            print(f"    Dup rate  : {viz_data['duplicate_rate']}%  "
                  f"({viz_data.get('discarded_count',0)} discarded / "
                  f"{viz_data.get('primary_count',0)} kept)")

    print(f"\n  All outputs in: {out_dir}/")
    print(f"  viz_all_candidates.jpg        — all detections by category")
    print(f"  viz_duplicates_highlighted.jpg — RED=duplicate, GREEN=kept")
    print(f"  viz_final_clean.jpg           — final clean output for review")
    print(f"  viz_tiles/                    — zoomable tile grid")


if __name__ == "__main__":
    main()