#!/usr/bin/env python3
"""
step5b_geometric_association.py — Geometric Association Agent
=============================================================
CDCI P&ID Pipeline — Step 5B

Purpose
-------
Transform isolated candidate records from Step 5A into CONNECTED
engineering entities through spatial reasoning.

This stage performs ONLY geometry — NO OCR, NO symbol detection,
NO validation, NO deduplication.

Architecture
------------
  • OpenCV line detection for leader lines and pipes
  • KDTree spatial indexing for nearest-neighbour association
  • Shapely containment reasoning (instrument → equipment)
  • Direction vectors for pipe connections
  • Input: step5a_candidates.json
  • Output: step5b_associations.json

Spatial relationships resolved
-------------------------------
  1. Leader Line Association:  Symbol → leader line → tag text
  2. Pipe Association:         Symbol → connected pipe (nearest line)
  3. Equipment Association:    Instrument → parent equipment
  4. Spatial Relationship:     Attached | Connected | Contained | Adjacent

Inputs
------
  step5a_candidates.json  — candidate records from Step 5A
  drawing image           — for line/pipe detection

Output Schema (per candidate)
------------------------------
  {
    candidate_id, tag_text, symbol_name,
    connected_pipe, connected_equipment,
    leader_line_detected, association_confidence,
    spatial_relationship, nearby_candidates
  }

Usage
-----
  python step5b_geometric_association.py \\
      --candidates output/step5a_candidates.json \\
      --image drawing.jpg --out output/

  python step5b_geometric_association.py \\
      --context output/drawing_context.json
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

# ── Association thresholds ─────────────────────────────────────────────────────
LEADER_LINE_MAX_DIST_PX = 80      # max px between tag bbox and symbol bbox edge
PIPE_ASSOC_RADIUS_PX    = 120     # KDTree radius for nearest pipe
EQUIPMENT_CONTAIN_PAD   = 30      # px padding for containment check
ADJACENT_THRESHOLD_PX   = 150     # px for "adjacent to" relationship
NEARBY_CANDIDATES_R     = 200     # px radius for listing nearby candidates

# ── Pipe detector tuning (CHANGE 1 + 2) ─────────────────────────────────────────
# A large morphological kernel is the single most important change: only
# continuous line runs of >=150px survive MORPH_OPEN, so instrument stems, text
# strokes and short noise fragments disappear automatically.
MORPH_KERNEL_H        = (150, 1)  # horizontal-pipe structuring element (was ~(40,1))
MORPH_KERNEL_V        = (1, 150)  # vertical-pipe structuring element   (was ~(1,40))

# Drawing-zone + geometry filters applied AFTER morphology.
PIPE_BORDER_MARGIN_PX = 200       # ignore segments within 200px of the image edge
PIPE_MIN_LENGTH_PX    = 250       # minimum real pipe length; <250px = text separator / abbreviation rules
PIPE_MAX_FRACTION     = 0.75      # reject if > 75% of the drawing dimension (border frame)
PIPE_TITLE_BLOCK_FRAC = 0.80      # bottom 20% of the sheet is the title block — exclude
PIPE_REF_PANEL_X_FRAC = 0.88      # right 12% is the reference-notes panel — exclude tables

# Hough diagonal detector (CHANGE 3): only real diagonal runs, not noise.
PIPE_HOUGH_MIN_LEN    = 150
PIPE_HOUGH_MAX_GAP    = 30

# Table-grid rejection: a dense parallel band of horizontal lines is a table
# (notes block / equipment list), not piping. Real pipes are horizontally isolated.
TABLE_GRID_Y_BAND_PX    = 15
TABLE_GRID_MIN_PARALLEL = 4


# ═══════════════════════════════════════════════════════════════════════════════
# Line / pipe detection from the drawing image
# ═══════════════════════════════════════════════════════════════════════════════

def _classify_pipe_type(length: float, ang_from_horizontal: float) -> str:
    """Type from geometry only (CHANGE 3). `ang_from_horizontal` in [0,90].
    Anything shorter than a real pipe run is a leader-line connector stub."""
    if length < PIPE_MIN_LENGTH_PX:
        return "leader_line"
    if ang_from_horizontal < 15:
        return "horizontal_pipe"
    if ang_from_horizontal > 75:
        return "vertical_pipe"
    return "diagonal_pipe"


def is_table_grid_line(seg: dict, all_horizontal_segs: list[dict],
                       Y_BAND: int = TABLE_GRID_Y_BAND_PX,
                       MIN_PARALLEL: int = TABLE_GRID_MIN_PARALLEL) -> bool:
    """
    A horizontal segment is a table grid line if >= MIN_PARALLEL other
    horizontal segments exist within Y_BAND pixels of the same y-coordinate.
    A real pipe run is isolated; a table grid is a dense parallel pattern.
    """
    y = (seg["y0"] + seg["y1"]) / 2
    same_band = [s for s in all_horizontal_segs
                 if s is not seg and abs((s["y0"] + s["y1"]) / 2 - y) < Y_BAND]
    return len(same_band) >= MIN_PARALLEL


def detect_pipes_and_lines(img_bgr: np.ndarray) -> list[dict]:
    """
    Detect process pipe lines using morphological line detection.
    Returns list of line dicts: {x0,y0,x1,y1,length,angle,type}.

    The number of segments rejected as borders / title-block / reference-panel
    is stashed on ``detect_pipes_and_lines.last_rejected_zones`` so callers can
    report it WITHOUT changing this function's return type (step5b2_hierarchy.py
    imports and calls this directly and expects a plain list).
    """
    gray     = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    clahe    = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    _, binary = cv2.threshold(enhanced, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    H, W = binary.shape

    # CHANGE 1 — large morphological kernels: only continuous >=150px runs survive.
    h_kern = cv2.getStructuringElement(cv2.MORPH_RECT, MORPH_KERNEL_H)
    horiz  = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kern)
    v_kern = cv2.getStructuringElement(cv2.MORPH_RECT, MORPH_KERNEL_V)
    vert   = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kern)

    lines_out: list[dict] = []
    rejected = 0   # CHANGE 2 — segments dropped in non-drawing zones

    def _extract_lines(mask: np.ndarray, orient: str, sink: list) -> None:
        nonlocal rejected
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)

            # ── CHANGE 2 — drawing-zone + geometry filters ──
            if orient == "h":
                if w < PIPE_MIN_LENGTH_PX:                 # too short
                    rejected += 1; continue
                if w > W * PIPE_MAX_FRACTION:              # full-width border frame
                    rejected += 1; continue
                if y < PIPE_BORDER_MARGIN_PX:              # top border
                    rejected += 1; continue
                if y + h > H * PIPE_TITLE_BLOCK_FRAC:      # title block / bottom border
                    rejected += 1; continue
                if (x + w / 2) > W * PIPE_REF_PANEL_X_FRAC and w > 500:
                    rejected += 1; continue               # reference-panel table lines
            else:  # vertical
                if x < PIPE_BORDER_MARGIN_PX:              # left border
                    rejected += 1; continue
                if x + w > W - PIPE_BORDER_MARGIN_PX:      # right border
                    rejected += 1; continue
                if h > H * PIPE_MAX_FRACTION:              # full-height border frame
                    rejected += 1; continue
                if h < PIPE_MIN_LENGTH_PX:                 # too short
                    rejected += 1; continue
                if y + h > H * PIPE_TITLE_BLOCK_FRAC:      # title block
                    rejected += 1; continue

            length = math.sqrt(w ** 2 + h ** 2)
            ang    = math.degrees(math.atan2(h, w))        # 0..90 for axis-aligned runs
            cx, cy = x + w // 2, y + h // 2
            sink.append({
                "x0":    x,     "y0":    y,
                "x1":    x + w, "y1":    y + h,
                "cx":    cx,    "cy":    cy,
                "length": round(length, 1),
                "angle":  round(ang, 1),
                "type":  _classify_pipe_type(length, ang),
            })

    # Horizontal segments are collected separately so the table-grid density
    # filter can run over the full set before any are accepted.
    horiz_segs: list[dict] = []
    _extract_lines(horiz, "h", horiz_segs)
    n_table_grid = 0
    for seg in horiz_segs:
        if is_table_grid_line(seg, horiz_segs):
            n_table_grid += 1                       # dense parallel band => table grid
        else:
            lines_out.append(seg)
    _extract_lines(vert, "v", lines_out)

    # Diagonal pipes using Probabilistic Hough (CHANGE 3: 150px floor, 30px gap).
    try:
        edges = cv2.Canny(enhanced, 50, 150)
        hough_lines = cv2.HoughLinesP(
            edges, 1, np.pi / 180,
            threshold=50, minLineLength=PIPE_HOUGH_MIN_LEN, maxLineGap=PIPE_HOUGH_MAX_GAP
        )
        if hough_lines is not None:
            for line in hough_lines:
                x0, y0, x1, y1 = line[0]
                length = math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2)
                angle = math.degrees(math.atan2(y1 - y0, x1 - x0))
                if abs(abs(angle) - 90) < 10 or abs(angle) < 10:
                    continue   # skip H/V already captured by morphology
                # zone filter: drop border / title-block diagonals
                xc, yc = (x0 + x1) / 2, (y0 + y1) / 2
                if (xc < PIPE_BORDER_MARGIN_PX or xc > W - PIPE_BORDER_MARGIN_PX
                        or yc < PIPE_BORDER_MARGIN_PX or yc > H * PIPE_TITLE_BLOCK_FRAC):
                    rejected += 1; continue
                ang_h = abs(angle)
                if ang_h > 90:
                    ang_h = 180 - ang_h
                lines_out.append({
                    "x0": int(x0), "y0": int(y0), "x1": int(x1), "y1": int(y1),
                    "cx": int((x0 + x1) // 2), "cy": int((y0 + y1) // 2),
                    "length": round(length, 1),
                    "angle":  round(angle, 1),
                    "type":   _classify_pipe_type(length, ang_h),
                })
    except Exception as e:
        log.warning("Hough lines failed: %s", e)

    detect_pipes_and_lines.last_rejected_zones = rejected
    detect_pipes_and_lines.last_table_grid_removed = n_table_grid
    n_pipe   = sum(1 for l in lines_out if "pipe" in l["type"])
    n_leader = sum(1 for l in lines_out if l["type"] == "leader_line")
    log.info("Pipe detection: %d segments (%d pipes, %d leaders); %d rejected in "
             "non-drawing zones, %d table-grid lines removed",
             len(lines_out), n_pipe, n_leader, rejected, n_table_grid)
    return lines_out


detect_pipes_and_lines.last_rejected_zones = 0
detect_pipes_and_lines.last_table_grid_removed = 0


# ═══════════════════════════════════════════════════════════════════════════════
# Geometric helpers
# ═══════════════════════════════════════════════════════════════════════════════

def bbox_center(bbox: dict) -> tuple[float, float]:
    return (
        (bbox.get("x1", 0) + bbox.get("x2", 0)) / 2,
        (bbox.get("y1", 0) + bbox.get("y2", 0)) / 2,
    )


def bbox_area(bbox: dict) -> float:
    w = abs(bbox.get("x2", 0) - bbox.get("x1", 0))
    h = abs(bbox.get("y2", 0) - bbox.get("y1", 0))
    return w * h


def dist_pt_to_segment(px: float, py: float,
                        x0: float, y0: float,
                        x1: float, y1: float) -> float:
    """Minimum distance from point (px,py) to line segment (x0,y0)→(x1,y1)."""
    dx, dy = x1 - x0, y1 - y0
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-9:
        return math.sqrt((px - x0) ** 2 + (py - y0) ** 2)
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / seg_len_sq))
    proj_x = x0 + t * dx
    proj_y = y0 + t * dy
    return math.sqrt((px - proj_x) ** 2 + (py - proj_y) ** 2)


def dist_bbox_to_bbox(a: dict, b: dict) -> float:
    """Approximate distance between two bounding boxes (edge to edge)."""
    ax1, ay1, ax2, ay2 = a.get("x1",0), a.get("y1",0), a.get("x2",0), a.get("y2",0)
    bx1, by1, bx2, by2 = b.get("x1",0), b.get("y1",0), b.get("x2",0), b.get("y2",0)
    dx = max(0, max(ax1, bx1) - min(ax2, bx2))
    dy = max(0, max(ay1, by1) - min(ay2, by2))
    return math.sqrt(dx * dx + dy * dy)


def bbox_contains(outer: dict, inner: dict, pad: int = 0) -> bool:
    """True if outer bbox fully contains inner bbox (with padding)."""
    return (outer.get("x1", 0) - pad <= inner.get("x1", 0) and
            outer.get("y1", 0) - pad <= inner.get("y1", 0) and
            outer.get("x2", 0) + pad >= inner.get("x2", 0) and
            outer.get("y2", 0) + pad >= inner.get("y2", 0))


# ═══════════════════════════════════════════════════════════════════════════════
# Association logic
# ═══════════════════════════════════════════════════════════════════════════════

def associate_candidates(candidates: list[dict],
                          lines: list[dict]) -> list[dict]:
    """
    For each candidate, determine:
      1. Leader line: is there a line connecting its tag_bbox to symbol_bbox?
      2. Connected pipe: nearest horizontal/vertical pipe line
      3. Connected equipment: is this instrument inside a larger equipment bbox?
      4. Spatial relationship: ATTACHED | CONNECTED | CONTAINED | ADJACENT
      5. Nearby candidates within radius
    Returns enriched candidate list.
    """
    # Build spatial index of candidate centers
    centers = np.array([bbox_center(c["symbol_bbox"]) for c in candidates],
                       dtype=np.float32) if candidates else np.zeros((0, 2))

    # Separate pipes from leader lines
    pipes   = [l for l in lines if "pipe" in l["type"]]
    leaders = [l for l in lines if l["type"] == "leader_line"]

    # Build equipment bbox list (larger symbols: vessels, tanks, compressors, heat exchangers)
    EQUIP_CATEGORIES = {"equipment", "vessel", "tank", "compressor",
                        "heat exchanger", "pump", "separator"}
    equip_candidates = [
        c for c in candidates
        if (c.get("symbol_category") or "").lower() in EQUIP_CATEGORIES
        or bbox_area(c.get("symbol_bbox", {})) > 3000   # large symbols
    ]

    results = []

    for i, cand in enumerate(candidates):
        sym_bbox = cand.get("symbol_bbox", {})
        tag_bbox = cand.get("tag_bbox",    {})
        scx, scy = bbox_center(sym_bbox)
        tcx, tcy = bbox_center(tag_bbox)

        # 1. Leader line detection
        leader_found   = False
        leader_dist    = float("inf")

        if tag_bbox and sym_bbox:
            # Check if direct tag↔symbol distance is small (implicit leader line)
            direct_dist = dist_bbox_to_bbox(sym_bbox, tag_bbox)
            if direct_dist <= LEADER_LINE_MAX_DIST_PX:
                leader_found = True
                leader_dist  = direct_dist

        # Also check detected leader lines
        if not leader_found:
            for line in leaders:
                d = dist_pt_to_segment(scx, scy,
                                       line["x0"], line["y0"],
                                       line["x1"], line["y1"])
                if d < 20 and d < leader_dist:
                    leader_found = True
                    leader_dist  = d

        # 2. Nearest pipe association
        connected_pipe   = ""
        min_pipe_dist    = float("inf")

        for pipe in pipes:
            d = dist_pt_to_segment(scx, scy,
                                   pipe["x0"], pipe["y0"],
                                   pipe["x1"], pipe["y1"])
            if d < min_pipe_dist and d <= PIPE_ASSOC_RADIUS_PX:
                min_pipe_dist  = d
                # Pipe doesn't have a tag — use centroid as identifier
                connected_pipe = f"PIPE@({pipe['cx']},{pipe['cy']})"

        # 3. Equipment containment (instrument → parent equipment)
        connected_equipment = ""
        for equip in equip_candidates:
            if equip["candidate_id"] == cand["candidate_id"]:
                continue
            eb = equip.get("symbol_bbox", {})
            if bbox_contains(eb, sym_bbox, pad=EQUIPMENT_CONTAIN_PAD):
                # Contained: pick the smallest containing equipment
                if not connected_equipment or bbox_area(eb) < bbox_area(
                    next((e["symbol_bbox"] for e in equip_candidates
                          if e["candidate_id"] == connected_equipment), eb)
                ):
                    connected_equipment = equip["candidate_id"]

        # 4. Spatial relationship classification
        if connected_equipment:
            spatial_rel = "CONTAINED_WITHIN"
        elif leader_found and leader_dist < 20:
            spatial_rel = "ATTACHED_TO"
        elif min_pipe_dist < 30:
            spatial_rel = "CONNECTED_TO"
        elif min_pipe_dist <= ADJACENT_THRESHOLD_PX or leader_dist <= ADJACENT_THRESHOLD_PX:
            spatial_rel = "ADJACENT_TO"
        else:
            spatial_rel = "ISOLATED"

        # 5. Nearby candidates (within radius)
        nearby = []
        if len(centers) > 1:
            for j, other in enumerate(candidates):
                if j == i:
                    continue
                dx = centers[j][0] - centers[i][0]
                dy = centers[j][1] - centers[i][1]
                dist = math.sqrt(dx * dx + dy * dy)
                if dist <= NEARBY_CANDIDATES_R:
                    nearby.append({
                        "candidate_id": other["candidate_id"],
                        "tag_text":     other.get("tag_text", ""),
                        "distance_px":  round(dist, 1),
                    })

        # Compute association confidence
        conf_factors = []
        if leader_found:
            conf_factors.append(0.9 - min(leader_dist / 200, 0.4))
        if connected_pipe:
            conf_factors.append(0.7 - min(min_pipe_dist / 300, 0.3))
        if connected_equipment:
            conf_factors.append(0.8)
        assoc_conf = round(max(conf_factors) if conf_factors else 0.3, 3)

        enriched = {**cand}
        enriched.update({
            "connected_pipe":         connected_pipe,
            "connected_equipment":    connected_equipment,
            "leader_line_detected":   leader_found,
            "leader_line_distance_px": round(leader_dist, 1) if leader_found else None,
            "pipe_distance_px":       round(min_pipe_dist, 1) if connected_pipe else None,
            "spatial_relationship":   spatial_rel,
            "association_confidence": assoc_conf,
            "nearby_candidates":      nearby[:5],   # top-5 by distance
        })
        results.append(enriched)

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def run_geometric_association(
    candidates_path: str,
    img_path: str,
    out_dir: str,
    debug: bool = False,
) -> list[dict]:

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    with open(candidates_path) as f:
        data = json.load(f)
    candidates = data.get("candidates", [])
    log.info("Loaded %d candidates from Step 5A", len(candidates))

    img = cv2.imread(img_path)
    if img is None:
        raise FileNotFoundError(f"Cannot read: {img_path}")
    H, W = img.shape[:2]
    log.info("Drawing: %dx%d", W, H)

    log.info("=== Detecting lines and pipes ===")
    lines = detect_pipes_and_lines(img)

    # Pipe-only debug overlay (key visual verification of the detector fix):
    # red = horizontal, blue = vertical, green = diagonal. Save at 2400px wide.
    pipe_dbg = img.copy()
    PIPE_COL = {"horizontal_pipe": (0, 0, 255),   # red  (BGR)
                "vertical_pipe":   (255, 0, 0),   # blue
                "diagonal_pipe":   (0, 255, 0)}   # green
    for l in lines:
        col = PIPE_COL.get(l["type"])
        if col is None:                            # skip leader_line stubs
            continue
        cv2.line(pipe_dbg, (int(l["x0"]), int(l["y0"])),
                 (int(l["x1"]), int(l["y1"])), col, 3)
    _sc = 2400 / W
    pipe_dbg_sm = cv2.resize(pipe_dbg, (2400, int(H * _sc)))
    pipe_dbg_path = str(out / "step5b_pipe_debug.jpg")
    cv2.imwrite(pipe_dbg_path, pipe_dbg_sm, [cv2.IMWRITE_JPEG_QUALITY, 85])
    log.info("✓ step5b_pipe_debug.jpg → %s", pipe_dbg_path)

    log.info("=== Running geometric association ===")
    enriched = associate_candidates(candidates, lines)

    # Summary
    rel_counts = {}
    for c in enriched:
        rel = c.get("spatial_relationship", "UNKNOWN")
        rel_counts[rel] = rel_counts.get(rel, 0) + 1

    log.info("Spatial relationship summary: %s",
             " | ".join(f"{k}={v}" for k, v in rel_counts.items()))
    log.info("Leader lines detected: %d",
             sum(1 for c in enriched if c.get("leader_line_detected")))
    log.info("Pipe connections:      %d",
             sum(1 for c in enriched if c.get("connected_pipe")))
    log.info("Equipment containment: %d",
             sum(1 for c in enriched if c.get("connected_equipment")))

    # Debug: annotated image with associations
    if debug:
        ann = img.copy()
        for c in enriched:
            sb  = c.get("symbol_bbox", {})
            tb  = c.get("tag_bbox",    {})
            col = (0, 255, 0) if c.get("leader_line_detected") else (0, 165, 255)
            if all(k in sb for k in ("x1","y1","x2","y2")):
                cv2.rectangle(ann, (sb["x1"],sb["y1"]), (sb["x2"],sb["y2"]), col, 2)
            if all(k in tb for k in ("x1","y1","x2","y2")) and tb != sb:
                cv2.rectangle(ann, (tb["x1"],tb["y1"]), (tb["x2"],tb["y2"]),
                              (255, 0, 0), 1)
                # Draw line between symbol and tag centers
                scx, scy = int((sb["x1"]+sb["x2"])/2), int((sb["y1"]+sb["y2"])/2)
                tcx, tcy = int((tb["x1"]+tb["x2"])/2), int((tb["y1"]+tb["y2"])/2)
                cv2.line(ann, (scx,scy), (tcx,tcy), (255,255,0), 1)
        scale = 1600 / W
        ann_sm = cv2.resize(ann, (1600, int(H * scale)))
        debug_path = str(out / "debug_5b_associations.jpg")
        cv2.imwrite(debug_path, ann_sm, [cv2.IMWRITE_JPEG_QUALITY, 85])
        log.info("✓ debug_5b_associations.jpg → %s", debug_path)

    # Write output (Step 5B schema only — slim)
    step5b_records = [
        {
            "candidate_id":           c["candidate_id"],
            "tag_text":               c.get("tag_text", ""),
            "symbol_name":            c.get("symbol_name", ""),
            "symbol_bbox":            c.get("symbol_bbox", {}),
            "connected_pipe":         c.get("connected_pipe", ""),
            "connected_equipment":    c.get("connected_equipment", ""),
            "leader_line_detected":   c.get("leader_line_detected", False),
            "spatial_relationship":   c.get("spatial_relationship", "ISOLATED"),
            "association_confidence": c.get("association_confidence", 0.0),
            "nearby_candidates":      c.get("nearby_candidates", []),
        }
        for c in enriched
    ]

    n_pipe_seg = sum(1 for l in lines if "pipe" in l["type"])
    n_leader   = sum(1 for l in lines if l["type"] == "leader_line")
    n_rejected = getattr(detect_pipes_and_lines, "last_rejected_zones", 0)
    n_grid     = getattr(detect_pipes_and_lines, "last_table_grid_removed", 0)

    out_path = str(out / "step5b_associations.json")
    with open(out_path, "w") as f:
        json.dump({
            "version":           "v1",
            "total_candidates":  len(enriched),
            "lines_detected":    len(lines),
            "pipe_segments_detected":  n_pipe_seg,   # real process pipes
            "leader_lines_detected":   n_leader,     # short connector stubs
            "segments_rejected_zones": n_rejected,   # borders / title block / ref panel
            "segments_rejected_grid":  n_grid,       # dense parallel table-grid bands
            "rel_summary":       rel_counts,
            "associations":      step5b_records,
            # Pass-through full enriched records for merge downstream
            "enriched_candidates": enriched,
        }, f, indent=2)
    log.info("✓ step5b_associations.json → %s", out_path)
    return enriched


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Step 5B: Geometric Association Agent")
    parser.add_argument("--candidates", help="step5a_candidates.json")
    parser.add_argument("--image",      help="Drawing image path")
    parser.add_argument("--context",    help="drawing_context.json")
    parser.add_argument("--out",        default="output")
    parser.add_argument("--debug",      action="store_true")
    args = parser.parse_args()

    img_path       = args.image
    candidates_path = args.candidates

    if args.context:
        with open(args.context) as f:
            ctx = json.load(f)
        img_path       = img_path       or ctx.get("raster_path") or ctx.get("input_file")
        candidates_path = candidates_path or str(Path(args.out) / "step5a_candidates.json")

    if not candidates_path:
        candidates_path = str(Path(args.out) / "step5a_candidates.json")

    enriched = run_geometric_association(candidates_path, img_path, args.out, args.debug)

    print(f"\n=== Step 5B Complete ===")
    print(f"  Candidates processed : {len(enriched)}")
    rel_counts = {}
    for c in enriched:
        r = c.get("spatial_relationship", "UNKNOWN")
        rel_counts[r] = rel_counts.get(r, 0) + 1
    for rel, cnt in sorted(rel_counts.items(), key=lambda x: -x[1]):
        print(f"    {rel:<22} {cnt:>4}")
    print(f"\n  Output: {args.out}/step5b_associations.json")


if __name__ == "__main__":
    main()
