#!/usr/bin/env python3
"""
step5b2_hierarchy.py — Connectivity Graph & Hierarchy Builder (Phase 1)
=======================================================================
CDCI P&ID Pipeline — Step 5B2  (post-processor, NO API, NO new detection)

Purpose
-------
Turn the per-symbol, pairwise-local output of Step 5B into a fully
connected, navigable association + hierarchy NETWORK — without changing
Step 5A or 5B and without any Gemini call.

Phase 1 scope (UNDIRECTED — flow direction is Phase 2)
------------------------------------------------------
  1. Persist line_segments   — re-run 5B's CV line detector, stop discarding
  2. Spatial relations       — left/right/above/below/overlap/intersect (bbox math)
  3. Pipeline entities       — endpoint-snap + union-find on pipe segments
  4. Junction nodes          — points where >=3 segment endpoints meet
  5. Connectivity graph       — nodes (candidates + pipelines + junctions)
                                + UNDIRECTED edges
  6. Hierarchy               — root / parent_chain / ancestor / descendant /
                                siblings / root-to-leaf, via graph traversal
  7. Relationship class.     — by symbol_category + edge geometry

Inputs
------
  step5b_associations.json   — Step 5B output (candidates + enriched)
  drawing image              — to RE-DETECT line segments (5B persists only a count)

Output
------
  step5b2_hierarchy.json     — schema v2: keeps associations[] and
                               enriched_candidates[] byte-identical, adds
                               pipelines[], junctions[], graph{}, hierarchy[],
                               line_segments[], algorithms{}

Phase 2 scope (Track B — FLOW DIRECTION, this build)
----------------------------------------------------
  8. Arrowhead detection (CV)  — filled triangles adjacent to pipe segments
  9. Direction seeding         — arrowhead tip dir => directed pipeline edge
 10. Direction propagation     — BFS through pipeline+junction graph;
                                 equipment as boundary conditions
 11. Gemini fallback           — ONLY for still-unknown pipelines, gated on
                                 explicit user go-ahead (never auto-called)

This module does NOT modify step5b_associations.json.

# ─────────────────────────────────────────────────────────────────────────
# FUTURE TRACK C — Control-loop / signal-line hierarchy
# ~12 instruments (V-TIT-211, V-XS-239, V-TAHH-213, V-PAHH-213,
# V-TAHH-213/214, V-TE-211, V-TIT-213/214, LIT-206, V-LIT-206,
# T-202C, "213") connect via instrument signal lines (dashed), not
# process pipe. They belong to control-loop hierarchies
# (sensor→transmitter→controller→valve), not equipment hierarchies.
# Needs: signal-line detection (dashed line CV), control-loop
# entity extraction, separate hierarchy dimension.
# ─────────────────────────────────────────────────────────────────────────
"""

import argparse
import concurrent.futures
import copy as _copy_mod
import hashlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import cv2

# Reuse Step 5B's line detector verbatim — no duplicated CV logic.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from step5b_geometric_association import (          # noqa: E402
    detect_pipes_and_lines,
    bbox_center,
    bbox_area,
    dist_pt_to_segment,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Tuning (geometry only) ──────────────────────────────────────────────────
SNAP_TOL_PX        = 25     # endpoint-snap tolerance for union-find / junctions
JUNCTION_MIN_DEG   = 3      # segment-endpoints meeting at a point => junction
SYMBOL_PIPE_RADIUS = 60     # px: symbol edge within this of a pipeline => connected
EQUIP_PIPE_RADIUS  = 90     # px: equipment binds to EVERY pipeline within this (hub)
MOUNTED_ON_RADIUS  = 200    # px: instrument edge-gap to equipment => MOUNTED_ON edge
GAP_BRIDGE_PX      = 100    # px: 2nd-pass merge of pipeline endpoints; raised from 40 to bridge instrument-symbol gaps
MECH_TRAIN_GAP     = 700    # px: max edge-gap between drive-train equipment
MECH_TRAIN_ALIGN   = 90     # px: center offset on shared axis for alignment
MIN_PIPE_LEN       = 60     # px: ignore tiny pipe fragments when building pipelines

# ── Entity resolution (duplicate-detection merge, FIX 1) ─────────────────────
DUP_MAX_DIST_PX    = 1500   # px: same-tag detections whose centroids are farther
                            # apart than this are genuinely different instances of
                            # the same tag on different parts of the drawing — never
                            # merged. Different symbol_category is also never merged.

# ── Track B: flow-direction (arrowhead) tuning ──────────────────────────────
ARROWHEAD_MIN_AREA      = 200    # px²: smallest filled triangle to accept
ARROWHEAD_MAX_AREA      = 2000   # px²: largest filled triangle to accept
ARROWHEAD_PIPE_PROXIMITY = 30    # px: arrowhead must be within this of a pipe seg
ARROWHEAD_MIN_SOLIDITY  = 0.85   # filled (not open chevron)
ARROWHEAD_AR_MIN        = 1.0    # bounding-box aspect ratio range (triangle-ish)
ARROWHEAD_AR_MAX        = 1.8
CHECK_VALVE_MIN_AREA      = 200   # px²: smallest check-valve triangle to accept
CHECK_VALVE_SEAT_BAR_DIST = 15    # px: max apex→seat-bar distance to confirm ▷|
# Dead-end (dead-leg) topology pass: a pipeline with exactly ONE connected end
# (other end open) is oriented so flow runs toward the connected end. Only these
# connection types make the single end unambiguous enough to resolve.
DEAD_END_CONN_TYPES = frozenset({"junction", "equipment"})
STUB_MAX_SEG        = 3      # < this many segments + no candidate => noise stub (skip)

# ── Gemini flow fallback (category D ONLY, gated on --gemini-flow-fallback) ──
GEMINI_FLOW_MODEL      = "gemini-3.1-pro-preview"
GEMINI_FLOW_CLUSTER_PX = 1500   # greedy spatial-cluster radius over D-pipeline centers
GEMINI_FLOW_PAD_PX     = 200    # padding around a cluster's union bbox before cropping
GEMINI_FLOW_MAX_SIDE   = 1024   # longest side of the crop sent to Gemini
GEMINI_FLOW_TEMP       = 0.0    # deterministic
CAND_KINDS             = ("instrument", "valve", "piping", "equipment")

# ── Phase 1: Gemini instrument attachment (gated on --gemini-attach) ─────────
# For instruments/valves that the CV graph could not bind to equipment, ask
# Gemini what each connects to. One image crop per spatial cluster of such
# instruments. Cached by crop content hash (same pattern as gemini_flow_fallback).
GEMINI_ATTACH_MODEL        = GEMINI_FLOW_MODEL   # gemini-3.1-pro-preview
GEMINI_ATTACH_CLUSTER_PX   = 800     # greedy x-sorted cluster radius (centroid-to-centroid)
GEMINI_ATTACH_CROP_PAD_PX  = 500     # image-space padding around a cluster's union bbox before
                                     # cropping (raised from 250 → 500 so pipe routing to distant
                                     # equipment is visible in-frame)
GEMINI_ATTACH_MAX_SIDE     = 1024    # longest side of the crop sent to Gemini
GEMINI_ATTACH_TEMP         = 0.0     # deterministic
GEMINI_ATTACH_CONF_HIGH    = 0.85    # edge confidence for a Gemini "high" attachment
GEMINI_ATTACH_CONF_MEDIUM  = 0.65    # edge confidence for a Gemini "medium" attachment
# Rough pre-flight cost estimate (gemini-3.1-pro-preview; adjust if pricing changes).
# Same model as the flow fallback — figures are approximate, for the cost gate only.
GEMINI_ATTACH_EST_INPUT_TOK   = 1100   # ~1024px crop + prompt per call
GEMINI_ATTACH_EST_OUTPUT_TOK  = 350    # small JSON answer per call
GEMINI_PRO_USD_PER_MTOK_IN    = 2.00   # approx input price per 1e6 tokens
GEMINI_PRO_USD_PER_MTOK_OUT   = 12.00  # approx output price per 1e6 tokens

# ── Track C: signal-line (dashed) detection + control-loop hierarchy ─────────
# Proximity-gated STRICT path-probing (the "probe2" prototype). A signal edge is
# accepted only when an orthogonal path between two nearby bubbles is fully on a
# dashed line (>=1 dashed segment, none solid/empty). Real dash pixels required,
# so an instrument loses is_isolated only with genuine signal-line evidence.
# KNOWN LIMITS (see algorithms.signal_hierarchy): crossing dashes in dense
# regions over-merge into one cluster (capped at SIGNAL_LOOP_MAX_SIZE), and
# multi-bend routing (e.g. FIC-207's flow-transmitter inputs) is NOT recovered.
SIGNAL_MAX_PAIR_DIST = 850    # px: only probe instrument/valve pairs within this
SIGNAL_PROBE_STEP    = 4      # px: sample stride along a probe path
SIGNAL_PROBE_HALF    = 6      # px: perpendicular half-window to look for ink
SIGNAL_COV_MIN       = 0.12   # path ink coverage below this => empty (no line)
SIGNAL_COV_MAX       = 0.85   # coverage above this => solid (process pipe, skip)
SIGNAL_MIN_TRANS     = 4      # min ink<->gap transitions => dashed pattern
SIGNAL_MIN_PATHLEN   = 50     # px: shorter probe legs are "short" (skip, not blocking)
SIGNAL_THIRD_BUBBLE  = 40     # px: path passing this close to a 3rd bubble => indirect
SIGNAL_LOOP_MAX_SIZE = 15     # signal components larger than this = over-merge artifact
SPATIAL_WINDOW_PX  = 700    # only relate candidates within this center distance
SPATIAL_TOPK       = 5      # cap per-direction spatial lists
OVERLAP_FRAC       = 0.0    # >0 bbox intersection area => OVERLAPPING

# ── Symbol-size guards (pre-hierarchy filter, prevents text-mention detections
#    in notes / title blocks / tables from becoming hierarchy nodes)
MIN_SYMBOL_HEIGHT_PX      = 30   # image space — minimum bbox height for a real symbol
MIN_SYMBOL_WIDTH_PX       = 30   # image space — minimum bbox width for a real symbol
EQUIPMENT_MAX_ASPECT      = 3.5  # equipment bbox wider/taller than this ratio = text label
IS_LABEL_ONLY_CONF_CAP    = 0.4  # Gemini attachment confidence cap for label-only equipment
MOTOR_PROXY_RADIUS_PX     = 400  # px: MOTOR box within this of a filtered KM label → proxy

# Category rank for picking an undirected hierarchy root (higher = closer to root)
CATEGORY_RANK = {
    "equipment": 4,
    "piping":    3,
    "valve":     2,
    "instrument": 1,
}
PIPELINE_RANK = 3.5   # pipelines sit between equipment and valves
JUNCTION_RANK = 3.0


# ═══════════════════════════════════════════════════════════════════════════
# Union-Find
# ═══════════════════════════════════════════════════════════════════════════

class UnionFind:
    def __init__(self, n):
        self.p = list(range(n))
        self.r = [0] * n

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.r[ra] < self.r[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        if self.r[ra] == self.r[rb]:
            self.r[ra] += 1


# ═══════════════════════════════════════════════════════════════════════════
# 0a. Symbol-size pre-filter — remove text-mention candidates before graph
# ═══════════════════════════════════════════════════════════════════════════

def is_real_symbol(candidate, display_scale: float = 1.0) -> tuple:
    """Return (is_real, reason).

    A candidate is a real symbol if its symbol_bbox has physical extent on the
    drawing matching its symbol type. Text mentions that Gemini found in notes,
    title blocks, or tables typically have a tiny or zero-height bbox and are
    rejected here — before they can become false hierarchy nodes.

    display_scale: factor to convert bbox coords to image-space pixels.
    For step5b enriched_candidates (already in full-image pixel coords) use 1.0.
    """
    bbox = candidate.get("symbol_bbox") or {}
    x1 = bbox.get("x1", 0)
    y1 = bbox.get("y1", 0)
    x2 = bbox.get("x2", 0)
    y2 = bbox.get("y2", 0)

    scale = 1.0 / display_scale if display_scale else 1.0
    width_img  = (x2 - x1) * scale
    height_img = (y2 - y1) * scale

    # Rule 1: bbox must have physical extent in both axes
    if width_img < MIN_SYMBOL_WIDTH_PX or height_img < MIN_SYMBOL_HEIGHT_PX:
        return False, f"bbox too small ({width_img:.0f}x{height_img:.0f}px image space)"

    # Rule 2: equipment candidates must have substantial symbol area — real
    # equipment symbols (compressors, drums) are large boxes; a K-V-201 text
    # label in a title block is far too small regardless of width.
    if candidate.get("symbol_category") == "equipment":
        area_img = width_img * height_img
        if area_img < 5000:
            return False, (f"equipment bbox too small for real symbol "
                           f"({area_img:.0f}px² < 5000px²)")
        # Rule 3: very flat bbox = text label, not a real equipment symbol.
        # Real equipment symbols are near-square or at most 2:1; title-block
        # labels like "K-V-201" are 4:1 or wider (flat text row).
        aspect = max(width_img, height_img) / max(min(width_img, height_img), 1)
        if aspect > EQUIPMENT_MAX_ASPECT:
            return False, (f"equipment bbox too flat "
                           f"(aspect {aspect:.2f} > {EQUIPMENT_MAX_ASPECT}, "
                           f"likely text label not a symbol)")

    return True, "ok"


def _approx_zone(cx: float, cy: float) -> str:
    """Classify a candidate centroid into a drawing zone for filter reporting.
    Thresholds calibrated for the test drawing (9934×7017px layout)."""
    if cy < 400:
        return "title_block_top"
    if cy > 6500:
        return "bottom_tables"
    if cx < 800:
        return "notes_left"
    if cx > 9000:
        return "ref_panel_right"
    return "main_drawing"


# ═══════════════════════════════════════════════════════════════════════════
# 0. Entity resolution — merge duplicate detections BEFORE graph construction
#    (FIX 1: corruption source — duplicate tag_text became separate graph nodes,
#     corrupting siblings / parent resolution / validation)
# ═══════════════════════════════════════════════════════════════════════════

def _cand_center(c):
    bb = c.get("symbol_bbox") or {}
    if not bb:
        return None
    return ((bb["x1"] + bb["x2"]) / 2.0, (bb["y1"] + bb["y2"]) / 2.0)


def _union_bbox(boxes):
    xs1 = [b["x1"] for b in boxes if b]
    ys1 = [b["y1"] for b in boxes if b]
    xs2 = [b["x2"] for b in boxes if b]
    ys2 = [b["y2"] for b in boxes if b]
    if not xs1:
        return {}
    return {"x1": min(xs1), "y1": min(ys1), "x2": max(xs2), "y2": max(ys2)}


def resolve_canonical_entities(candidates, display_scale: float = 1.0):
    """Merge duplicate detections of the SAME tag into one canonical node BEFORE
    the graph is built, so duplicates never become separate graph/hierarchy nodes.

    Phase 0 (added): is_real_symbol() pre-filter discards candidates whose
    symbol_bbox has insufficient physical extent — these are text mentions
    detected in notes / title blocks / tables, not real drawn symbols. This
    runs BEFORE deduplication so false-geometry duplicates are not merged into
    real canonical nodes.

    Grouping (unchanged): by normalised tag_text (strip + uppercase for
    COMPARISON only — the surviving record keeps its original case). Within a
    tag group, members are clustered (union-find) and merged only when BOTH:
      • same symbol_category, AND
      • centroid-to-centroid distance <= DUP_MAX_DIST_PX.
    Same-tag detections farther than DUP_MAX_DIST_PX apart, or of a different
    symbol_category, are genuinely different instances and are kept separate.

    Each merged cluster collapses to the highest-vision_confidence candidate
    (the primary, whose candidate_id becomes the canonical node id). The primary
    receives the UNION bbox of the cluster and the union of sow_status values;
    every other member is remapped onto the primary.

    Returns (deduped_candidates, id_remap, n_merges, filter_stats).
    filter_stats has keys: n_filtered, zone_counts, filtered_records.
    id_remap maps every candidate_id → its canonical candidate_id (identity for
    survivors; filtered candidates are NOT in id_remap).
    """
    import copy

    # ── Phase 0: symbol-size pre-filter ──────────────────────────────────
    real_candidates = []
    filtered_ids = set()   # candidate_ids removed here — used below to clean refs
    filter_stats = {"n_filtered": 0, "zone_counts": {}, "filtered_records": []}
    for c in candidates:
        ok, reason = is_real_symbol(c, display_scale)
        if ok:
            real_candidates.append(c)
        else:
            filtered_ids.add(c["candidate_id"])
            bb = c.get("symbol_bbox") or {}
            cx = (bb.get("x1", 0) + bb.get("x2", 0)) / 2.0
            cy = (bb.get("y1", 0) + bb.get("y2", 0)) / 2.0
            tag = c.get("tag_text", "?")
            log.info("Filtered non-symbol candidate: %s at (%.0f,%.0f) — %s",
                     tag, cx, cy, reason)
            zone = _approx_zone(cx, cy)
            filter_stats["zone_counts"][zone] = (
                filter_stats["zone_counts"].get(zone, 0) + 1)
            filter_stats["filtered_records"].append({
                "tag": tag, "cx": round(cx), "cy": round(cy),
                "reason": reason, "zone": zone,
                "category": c.get("symbol_category", ""),
            })
    filter_stats["n_filtered"] = len(filter_stats["filtered_records"])
    if filter_stats["n_filtered"]:
        log.info("Symbol-size pre-filter: removed %d non-symbol candidates "
                 "(zone breakdown: %s)",
                 filter_stats["n_filtered"], filter_stats["zone_counts"])

    # ── Thing 1: label-only equipment recovery ────────────────────────────
    # An equipment tag filtered by aspect ratio with NO surviving real-symbol
    # instance re-enters the hierarchy as a label-only node (is_label_only=True).
    # Its Gemini attachment confidence is capped at IS_LABEL_ONLY_CONF_CAP so it
    # cannot corrupt high-confidence assignments.
    surviving_equip_tags = {
        (c.get("tag_text") or "").strip().upper()
        for c in real_candidates
        if c.get("symbol_category") == "equipment"
    }
    # Collect all flat-aspect equipment candidates that were filtered, grouped by tag
    flat_equip_by_tag = defaultdict(list)
    for c in candidates:
        if c["candidate_id"] not in filtered_ids:
            continue
        if c.get("symbol_category") != "equipment":
            continue
        bb = c.get("symbol_bbox") or {}
        w = (bb.get("x2", 0) - bb.get("x1", 0))
        h = (bb.get("y2", 0) - bb.get("y1", 0))
        aspect = max(w, h) / max(min(w, h), 1)
        if aspect <= EQUIPMENT_MAX_ASPECT:
            continue   # filtered for a different reason (size) — not a flat-label
        norm = (c.get("tag_text") or "").strip().upper()
        if norm:
            flat_equip_by_tag[norm].append(c)

    n_label_only = 0
    for norm, flat_group in flat_equip_by_tag.items():
        if norm in surviving_equip_tags:
            continue   # real symbol already exists — keep filter, drop the label
        # Pick the highest-confidence flat label to represent this equipment
        best = max(flat_group, key=lambda c: (c.get("vision_confidence") or 0.0))
        recovered = copy.deepcopy(best)
        recovered["is_label_only"] = True
        recovered["equipment_parent_confidence_cap"] = IS_LABEL_ONLY_CONF_CAP
        # Un-filter it: remove from filtered_ids and add to real_candidates
        filtered_ids.discard(recovered["candidate_id"])
        real_candidates.append(recovered)
        n_label_only += 1
        log.info("Kept label-only equipment: %s (no symbol box found) — "
                 "confidence capped at %.1f",
                 recovered.get("tag_text", norm), IS_LABEL_ONLY_CONF_CAP)
    if n_label_only:
        filter_stats["n_label_only_recovered"] = n_label_only

    # ── Thing 2: MOTOR-box → KM-V-201 proxy ──────────────────────────────
    # KM-V-201 is the motor tag for the compressor train. Step5a may detect
    # the MOTOR enclosure box without assigning the KM-V-201 tag (symbol_name
    # contains "MOTOR" but tag_text is empty or generic). If a filtered
    # KM-V-201 label exists and an untagged MOTOR box survives within
    # MOTOR_PROXY_RADIUS_PX of it, promote that box to tag "KM-V-201".
    filtered_km = [
        c for c in candidates
        if c["candidate_id"] in filtered_ids
        and (c.get("tag_text") or "").strip().upper() == "KM-V-201"
    ]
    if filtered_km and "KM-V-201" not in surviving_equip_tags:
        # centroid of the filtered label (use the first found)
        ref_bb = filtered_km[0].get("symbol_bbox") or {}
        ref_cx = (ref_bb.get("x1", 0) + ref_bb.get("x2", 0)) / 2.0
        ref_cy = (ref_bb.get("y1", 0) + ref_bb.get("y2", 0)) / 2.0
        for c in real_candidates:
            sname = (c.get("symbol_name") or "").lower()
            ttag = (c.get("tag_text") or "").strip()
            if "motor" not in sname:
                continue
            if ttag and ttag.upper() != "KM-V-201":
                continue   # already tagged as something else — don't override
            bb = c.get("symbol_bbox") or {}
            cx = (bb.get("x1", 0) + bb.get("x2", 0)) / 2.0
            cy = (bb.get("y1", 0) + bb.get("y2", 0)) / 2.0
            if math.hypot(cx - ref_cx, cy - ref_cy) <= MOTOR_PROXY_RADIUS_PX:
                old_tag = c.get("tag_text", "")
                c["tag_text"] = "KM-V-201"
                c["symbol_category"] = "equipment"
                c["is_motor_proxy"] = True
                log.info("MOTOR proxy: assigned KM-V-201 to MOTOR box "
                         "(was %r) at (%.0f,%.0f) — within %dpx of filtered label",
                         old_tag, cx, cy, MOTOR_PROXY_RADIUS_PX)
                # also remove any label-only KM-V-201 we may have just recovered
                real_candidates[:] = [
                    rc for rc in real_candidates
                    if not (rc.get("is_label_only")
                            and (rc.get("tag_text") or "").strip().upper() == "KM-V-201")
                ]
                break

    by_tag = defaultdict(list)
    untagged = []
    for c in real_candidates:
        norm = (c.get("tag_text") or "").strip().upper()
        if norm:
            by_tag[norm].append(c)
        else:
            untagged.append(c)

    deduped, id_remap, n_merges = [], {}, 0
    n_kept_distance = n_kept_category = 0

    for norm, members in by_tag.items():
        if len(members) == 1:
            c = copy.deepcopy(members[0])
            deduped.append(c)
            id_remap[c["candidate_id"]] = c["candidate_id"]
            continue

        # cluster members: merge edge iff (same category) AND (centroid <= DUP_MAX_DIST_PX)
        uf = UnionFind(len(members))
        ctrs = [_cand_center(m) for m in members]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                same_cat = (members[i].get("symbol_category") ==
                            members[j].get("symbol_category"))
                ci, cj = ctrs[i], ctrs[j]
                if ci is None or cj is None:
                    continue
                close = math.hypot(ci[0] - cj[0], ci[1] - cj[1]) <= DUP_MAX_DIST_PX
                if not same_cat:
                    n_kept_category += 1
                elif not close:
                    n_kept_distance += 1
                if same_cat and close:
                    uf.union(i, j)

        clusters = defaultdict(list)
        for i in range(len(members)):
            clusters[uf.find(i)].append(i)

        for idxs in clusters.values():
            group = [members[i] for i in idxs]
            primary = max(group, key=lambda c: (c.get("vision_confidence") or 0.0,
                                                 bbox_area(c.get("symbol_bbox") or {})))
            canon = copy.deepcopy(primary)
            if len(group) > 1:
                canon["symbol_bbox"] = _union_bbox(
                    [g.get("symbol_bbox") or {} for g in group])
                sow_vals = []
                for g in group:
                    sv = g.get("sow_status")
                    if sv and sv not in sow_vals:
                        sow_vals.append(sv)
                if sow_vals:
                    canon["sow_status"] = sow_vals[0]
                    canon["sow_status_merged"] = sow_vals
                canon["merged_from"] = [g["candidate_id"] for g in group]
                canon["merged_count"] = len(group)
                n_merges += 1
                log.info("Merged duplicate: %s (%d instances → 1 canonical)",
                         canon.get("tag_text", norm), len(group))
            deduped.append(canon)
            for g in group:
                id_remap[g["candidate_id"]] = canon["candidate_id"]

    for c in untagged:
        cc = copy.deepcopy(c)
        deduped.append(cc)
        id_remap[cc["candidate_id"]] = cc["candidate_id"]

    # Rewrite intra-candidate references so edges land on surviving nodes.
    # Also strip any reference to a filtered-out candidate (filtered_ids) so
    # build_graph never creates adjacency entries for non-existent nodes.
    for c in deduped:
        ce = c.get("connected_equipment")
        if ce in filtered_ids:
            c["connected_equipment"] = None   # filtered out — clear dangling ref
        elif ce in id_remap and id_remap[ce] != ce:
            c["connected_equipment"] = id_remap[ce]
        nbs = []
        for nb in (c.get("nearby_candidates") or []):
            nid = nb.get("candidate_id")
            if nid in filtered_ids:
                continue                      # drop reference to filtered symbol
            if nid in id_remap:
                nb = {**nb, "candidate_id": id_remap[nid]}
            if nb.get("candidate_id") != c["candidate_id"]:   # drop self-refs
                nbs.append(nb)
        if "nearby_candidates" in c:
            c["nearby_candidates"] = nbs

    log.info("Entity resolution: kept-separate guards fired "
             "(distance>%dpx: %d pairs, category-mismatch: %d pairs)",
             DUP_MAX_DIST_PX, n_kept_distance, n_kept_category)
    return deduped, id_remap, n_merges, filter_stats


# ═══════════════════════════════════════════════════════════════════════════
# 1. Persist line segments  (normalise endpoints by orientation)
# ═══════════════════════════════════════════════════════════════════════════

def build_line_segments(lines: list[dict]) -> list[dict]:
    """
    5B stores axis-aligned bounding rects. For a clean polyline we use the
    rect's medial axis: horizontal pipe -> (x0,cy)->(x1,cy); vertical pipe ->
    (cx,y0)->(cx,y1); leader lines keep their Hough endpoints.
    """
    segs = []
    for i, l in enumerate(lines):
        t = l["type"]
        if t == "horizontal_pipe":
            x0, y0, x1, y1 = l["x0"], l["cy"], l["x1"], l["cy"]
        elif t == "vertical_pipe":
            x0, y0, x1, y1 = l["cx"], l["y0"], l["cx"], l["y1"]
        else:  # leader_line
            x0, y0, x1, y1 = l["x0"], l["y0"], l["x1"], l["y1"]
        segs.append({
            "segment_id":  f"SEG-{i}",
            "x0": int(x0), "y0": int(y0), "x1": int(x1), "y1": int(y1),
            "length": l["length"], "angle": l["angle"], "type": t,
            "pipeline_id": None,
            "endpoints_node": [None, None],
        })
    return segs


# ═══════════════════════════════════════════════════════════════════════════
# 3+4. Pipelines (union-find) and junctions (endpoint degree)
# ═══════════════════════════════════════════════════════════════════════════

def _snap(pt):
    return (round(pt[0] / SNAP_TOL_PX), round(pt[1] / SNAP_TOL_PX))


def _proj_t(px, py, seg):
    """Parametric position of (px,py) projected onto seg, 0=start .. 1=end."""
    dx, dy = seg["x1"] - seg["x0"], seg["y1"] - seg["y0"]
    l2 = dx * dx + dy * dy
    if l2 < 1e-9:
        return 0.0
    return ((px - seg["x0"]) * dx + (py - seg["y0"]) * dy) / l2


def build_pipelines_and_junctions(segments: list[dict], equip_bboxes=None):
    """
    Pipe-type segments feed pipeline construction (leader lines kept separate).

    Junction-aware splitting (so one logical pipeline ends at every junction):
      • A snapped point is a JUNCTION if >= JUNCTION_MIN_DEG segments meet there,
        OR if any segment's endpoint lands on the INTERIOR of another (a tee — a
        junction even when only 2 segments are involved).
      • Union (merge into one pipeline) ONLY across non-junction, endpoint-to-
        endpoint contacts (collinear fragments, simple corners). Never merge
        through a junction → each run between junctions is its own pipeline.
      • Junctions are emitted as explicit connector nodes wired to every
        pipeline incident to them (done in build_graph).
    """
    pipe_idx = [i for i, s in enumerate(segments)
                if s["type"] in ("horizontal_pipe", "vertical_pipe")
                and s["length"] >= MIN_PIPE_LEN]
    uf = UnionFind(len(segments))

    # ── Pass 1: classify every endpoint↔segment contact ──────────────────
    endpoints = []
    for i in pipe_idx:
        s = segments[i]
        endpoints.append((i, 0, s["x0"], s["y0"]))
        endpoints.append((i, 1, s["x1"], s["y1"]))

    point_segs       = defaultdict(set)    # snapped pt -> incident seg indices
    point_interior   = defaultdict(bool)   # snapped pt -> endpoint-on-interior?
    end_contacts     = []                  # (i, j, snapped_pt) endpoint-to-endpoint

    for (i, slot, px, py) in endpoints:
        sp = _snap((px, py))
        point_segs[sp].add(i)
        for j in pipe_idx:
            if j == i:
                continue
            o = segments[j]
            if dist_pt_to_segment(px, py, o["x0"], o["y0"],
                                  o["x1"], o["y1"]) > SNAP_TOL_PX:
                continue
            point_segs[sp].add(j)
            t = _proj_t(px, py, o)
            end_tol = SNAP_TOL_PX / max(o["length"], 1.0)
            if t <= end_tol or t >= 1.0 - end_tol:
                end_contacts.append((i, j, sp))     # hits j's endpoint
            else:
                point_interior[sp] = True           # hits j's body => tee

    # ── Junction points: high degree OR any interior (tee) contact ───────
    junction_pts = {sp for sp, segs in point_segs.items()
                    if len(segs) >= JUNCTION_MIN_DEG or point_interior[sp]}

    # ── Pass 2: union only through NON-junction endpoint-to-endpoint links ─
    for (i, j, sp) in end_contacts:
        if sp not in junction_pts:
            uf.union(i, j)

    # ── Pass 2b: GAP BRIDGING (FIX 1) ─────────────────────────────────────
    # Merge components whose free endpoints sit within GAP_BRIDGE_PX of each
    # other across real drawing gaps (dashes, segmentation breaks). Guards:
    #   • skip endpoints already at a junction (junctions are real splits)
    #   • skip if the bridging gap crosses an equipment bbox
    equip_bboxes = equip_bboxes or []

    def _gap_crosses_equipment(ax, ay, bx, by):
        for eb in equip_bboxes:
            # sample the short gap; if any sample lands inside an equipment box, reject
            for f in (0.25, 0.5, 0.75):
                sx, sy = ax + (bx - ax) * f, ay + (by - ay) * f
                if (eb["x1"] <= sx <= eb["x2"] and eb["y1"] <= sy <= eb["y2"]):
                    return True
        return False

    # Free endpoints = segment endpoints not sitting on a junction point
    free_eps = []
    for (i, slot, px, py) in endpoints:
        if _snap((px, py)) in junction_pts:
            continue
        free_eps.append((i, px, py))

    bridge_merges = 0
    for a in range(len(free_eps)):
        ia, ax, ay = free_eps[a]
        for b in range(a + 1, len(free_eps)):
            ib, bx, by = free_eps[b]
            if uf.find(ia) == uf.find(ib):
                continue
            if math.hypot(ax - bx, ay - by) > GAP_BRIDGE_PX:
                continue
            if _gap_crosses_equipment(ax, ay, bx, by):
                continue
            uf.union(ia, ib)
            bridge_merges += 1

    log.info("Gap bridging (<=%dpx): %d component merges", GAP_BRIDGE_PX, bridge_merges)

    # ── Emit junction nodes ──────────────────────────────────────────────
    junctions = []
    jn_by_point = {}
    for sp in sorted(junction_pts, key=lambda p: -len(point_segs[p])):
        members = point_segs[sp]
        jid = f"JN-{len(junctions)}"
        jn_by_point[sp] = jid
        jtype = "CROSS" if len(members) >= 4 else "TEE"
        junctions.append({
            "junction_id": jid,
            "point": {"x": sp[0] * SNAP_TOL_PX, "y": sp[1] * SNAP_TOL_PX},
            "degree": len(members),
            "type": jtype,
            "connected_segments": sorted({segments[m]["segment_id"]
                                          for m in members}),
            "connected_pipelines": [],   # filled after pipelines are assigned
        })

    # Group segments into pipelines by union-find root
    groups = defaultdict(list)
    for i in pipe_idx:
        groups[uf.find(i)].append(i)

    pipelines = []
    for pl_i, (root, idxs) in enumerate(
            sorted(groups.items(), key=lambda kv: -len(kv[1]))):
        pid = f"PL-{pl_i:04d}"
        xs, ys = [], []
        seg_ids = []
        member_junctions = set()
        for i in idxs:
            s = segments[i]
            s["pipeline_id"] = pid
            seg_ids.append(s["segment_id"])
            xs += [s["x0"], s["x1"]]
            ys += [s["y0"], s["y1"]]
            for slot, pt in enumerate([(s["x0"], s["y0"]), (s["x1"], s["y1"])]):
                jid = jn_by_point.get(_snap(pt))
                if jid:
                    s["endpoints_node"][slot] = jid
                    member_junctions.add(jid)
        pipelines.append({
            "pipeline_id": pid,
            "segment_ids": seg_ids,
            "segment_count": len(seg_ids),
            "bbox": {"x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys)},
            "total_length_px": round(sum(segments[i]["length"] for i in idxs), 1),
            "intermediate_nodes": sorted(member_junctions),
            "branch_of": None,          # Phase 2 (needs direction)
            "child_pipelines": [],      # Phase 2
            "flow": {"direction": "UNKNOWN", "evidence": "none"},  # Phase 2
            "line_class": "process",
            "confidence": 0.6,
            "reasoning": f"{len(seg_ids)} segments merged through non-junction endpoint links; split at junctions",
        })

    # Wire junctions -> pipelines (explicit connectors, not blobs)
    seg_to_pipe = {sid: p["pipeline_id"] for p in pipelines for sid in p["segment_ids"]}
    for j in junctions:
        j["connected_pipelines"] = sorted({
            seg_to_pipe[sid] for sid in j["connected_segments"]
            if sid in seg_to_pipe
        })
    return pipelines, junctions


# ═══════════════════════════════════════════════════════════════════════════
# 2. Spatial relations  (pure bbox math)
# ═══════════════════════════════════════════════════════════════════════════

def _intersect_area(a, b):
    ix = max(0, min(a["x2"], b["x2"]) - max(a["x1"], b["x1"]))
    iy = max(0, min(a["y2"], b["y2"]) - max(a["y1"], b["y1"]))
    return ix * iy


def compute_spatial(cands: list[dict]) -> dict:
    """Return {candidate_id: {left_of:[], right_of:[], above:[], below:[],
    overlaps:[], intersects:[]}} using existing symbol bboxes."""
    out = {}
    boxes = [(c["candidate_id"], c.get("symbol_bbox", {}),
             bbox_center(c.get("symbol_bbox", {})), c.get("tag_text", ""))
            for c in cands]
    for cid, bb, ctr, _ in boxes:
        rel = {k: [] for k in
               ("left_of", "right_of", "above", "below", "overlaps", "intersects")}
        if not bb:
            out[cid] = rel
            continue
        for ocid, obb, octr, otag in boxes:
            if ocid == cid or not obb:
                continue
            dx = octr[0] - ctr[0]
            dy = octr[1] - ctr[1]
            dist = math.hypot(dx, dy)
            if dist > SPATIAL_WINDOW_PX:
                continue
            ia = _intersect_area(bb, obb)
            entry = {"candidate_id": ocid, "tag_text": otag,
                     "distance_px": round(dist, 1)}
            if ia > OVERLAP_FRAC:
                rel["overlaps"].append(entry)
                rel["intersects"].append(entry)
            # Directional: dominant axis (this is LEFT of neighbour => neighbour right)
            if abs(dx) >= abs(dy):
                (rel["right_of"] if dx < 0 else rel["left_of"]).append(entry)
            else:
                (rel["below"] if dy < 0 else rel["above"]).append(entry)
        for k in rel:
            rel[k] = sorted(rel[k], key=lambda e: e["distance_px"])[:SPATIAL_TOPK]
        out[cid] = rel
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 5. Connectivity graph
# ═══════════════════════════════════════════════════════════════════════════

def _min_dist_bbox_to_seg(bb, seg):
    """Distance from a symbol bbox (sampled on its perimeter centre/corners)
    to a segment."""
    pts = [bbox_center(bb),
           (bb["x1"], bb["y1"]), (bb["x2"], bb["y1"]),
           (bb["x1"], bb["y2"]), (bb["x2"], bb["y2"])]
    return min(dist_pt_to_segment(px, py,
                                  seg["x0"], seg["y0"], seg["x1"], seg["y1"])
               for px, py in pts)


# Instrument-prefix → measurement family. Used to guard MOUNTED_ON binding:
# flow instruments bind to PIPE not equipment; temp/pressure/level may bind
# to equipment; anything unrecognised falls through to geometry-only (current).
def _instrument_family(tag: str):
    t = (tag or "").upper().lstrip("V").lstrip("-")   # strip optional V- area prefix
    for pref in ("TIT", "TAHH", "TE", "TW", "TI", "T"):
        if t.startswith(pref):
            return "temperature"
    for pref in ("PAHH", "PIT", "PI"):
        if t.startswith(pref):
            return "pressure"
    for pref in ("LIT", "LI"):
        if t.startswith(pref):
            return "level"
    for pref in ("FIC", "FIT", "FI"):
        if t.startswith(pref):
            return "flow"
    return None


def _bbox_edge_gap(a, b):
    """Min gap between two bboxes (0 if overlapping)."""
    dx = max(0, max(a["x1"], b["x1"]) - min(a["x2"], b["x2"]))
    dy = max(0, max(a["y1"], b["y1"]) - min(a["y2"], b["y2"]))
    return math.hypot(dx, dy)


def build_graph(cands, pipelines, junctions, segments, spatial):
    """Nodes = candidates + pipelines + junctions. Undirected edges:
       symbol↔pipeline (geometry), equipment↔ALL nearby pipelines (hub),
       instrument↔equipment (MOUNTED_ON proximity + 5B containment),
       symbol↔symbol (ADJACENT from existing nearby), junction membership.
       Mechanical drive-train equipment are flagged (no shaft-line detection)."""
    seg_by_pipe = defaultdict(list)
    for s in segments:
        if s["pipeline_id"]:
            seg_by_pipe[s["pipeline_id"]].append(s)

    nodes = []
    for c in cands:
        nodes.append({
            "node_id": c["candidate_id"], "ref": c["candidate_id"],
            "kind": c.get("symbol_category", "unknown"),
            "tag_text": c.get("tag_text", ""), "bbox": c.get("symbol_bbox", {}),
        })
    for p in pipelines:
        nodes.append({"node_id": p["pipeline_id"], "ref": p["pipeline_id"],
                      "kind": "pipeline", "tag_text": p["pipeline_id"],
                      "bbox": p["bbox"]})
    for j in junctions:
        nodes.append({"node_id": j["junction_id"], "ref": j["junction_id"],
                      "kind": "junction", "tag_text": j["junction_id"],
                      "bbox": {"x1": j["point"]["x"] - 5, "y1": j["point"]["y"] - 5,
                               "x2": j["point"]["x"] + 5, "y2": j["point"]["y"] + 5}})

    edges = []
    eid = 0

    def add_edge(a, b, rel, cat, conf, ev):
        nonlocal eid
        edges.append({"edge_id": f"E-{eid}", "from": a, "to": b, "rel": rel,
                      "category": cat, "directed": False,
                      "confidence": round(conf, 3), "evidence": ev})
        eid += 1

    # junction ↔ pipeline (explicit connectors between pipeline entities)
    for j in junctions:
        for pid in j.get("connected_pipelines", []):
            add_edge(j["junction_id"], pid, "JUNCTION_OF", "physical",
                     0.8, f"degree-{j['degree']} {j['type']}")

    node_by_id = {n["node_id"]: n for n in nodes}
    equipment = [c for c in cands if c.get("symbol_category") == "equipment"]
    equip_pipes = {}   # equipment candidate_id -> set(pipeline_ids) it hubs

    # symbol ↔ pipeline
    #   • equipment: bind to EVERY pipeline within EQUIP_PIPE_RADIUS (hub)
    #   • everything else: nearest pipeline within SYMBOL_PIPE_RADIUS
    for c in cands:
        bb = c.get("symbol_bbox", {})
        if not bb:
            continue
        cat = c.get("symbol_category", "")
        if cat == "equipment":
            hub = set()
            for p in pipelines:
                dmin = min((_min_dist_bbox_to_seg(bb, s)
                            for s in seg_by_pipe[p["pipeline_id"]]),
                           default=float("inf"))
                if dmin <= EQUIP_PIPE_RADIUS:
                    hub.add(p["pipeline_id"])
                    add_edge(c["candidate_id"], p["pipeline_id"], "CONNECTED_TO",
                             "physical", 0.8 - dmin / (EQUIP_PIPE_RADIUS * 4),
                             f"equipment hub edge {round(dmin,1)}px")
            equip_pipes[c["candidate_id"]] = hub
        else:
            best_pid, best_d = None, SYMBOL_PIPE_RADIUS
            for p in pipelines:
                for s in seg_by_pipe[p["pipeline_id"]]:
                    d = _min_dist_bbox_to_seg(bb, s)
                    if d < best_d:
                        best_d, best_pid = d, p["pipeline_id"]
            if best_pid:
                rel = "MONITORS" if cat == "instrument" else "CONNECTED_TO"
                add_edge(c["candidate_id"], best_pid, rel, "physical",
                         0.85 - best_d / (SYMBOL_PIPE_RADIUS * 4),
                         f"symbol edge {round(best_d,1)}px from pipeline")

    # instrument ↔ equipment (direct proximity for the tight cluster that
    # sits beside equipment but whose leader line isn't a detectable pipe).
    # Guards (FIX 2):
    #   • nearest-equipment-wins (best_gap loop) — no binding if another is closer
    #   • flow instruments (FI/FIC/FIT) bind to PIPE, never to equipment here
    #   • temp/pressure/level may bind; unrecognised prefix → geometry-only
    for c in cands:
        if c.get("symbol_category") != "instrument":
            continue
        bb = c.get("symbol_bbox", {})
        if not bb:
            continue
        family = _instrument_family(c.get("tag_text", ""))
        if family == "flow":
            continue   # flow instruments associate with the pipe, not equipment
        best_eq, best_gap = None, MOUNTED_ON_RADIUS
        for e in equipment:
            gap = _bbox_edge_gap(bb, e.get("symbol_bbox", {}))
            if gap < best_gap:                       # nearest-equipment-wins guard
                best_gap, best_eq = gap, e["candidate_id"]
        if best_eq:
            ev = (f"instrument {round(best_gap,1)}px from equipment"
                  + (f" ({family})" if family else " (geometry-only)"))
            add_edge(c["candidate_id"], best_eq, "MOUNTED_ON", "functional",
                     0.75 - best_gap / (MOUNTED_ON_RADIUS * 4), ev)

    # instrument/valve ↔ equipment (existing 5B containment field)
    for c in cands:
        ce = c.get("connected_equipment")
        if ce:
            add_edge(c["candidate_id"], ce, "CONTAINED_WITHIN", "structural",
                     c.get("association_confidence", 0.8), "5B containment")

    # Mechanical drive train: equipment that are aligned + close but share NO
    # pipeline => coupled by a shaft we do NOT detect here. Flag for phase 3.
    for i in range(len(equipment)):
        for k in range(i + 1, len(equipment)):
            a, b = equipment[i], equipment[k]
            ba, bb_ = a.get("symbol_bbox", {}), b.get("symbol_bbox", {})
            if not ba or not bb_:
                continue
            ca, cb = bbox_center(ba), bbox_center(bb_)
            aligned = (abs(ca[1] - cb[1]) <= MECH_TRAIN_ALIGN or
                       abs(ca[0] - cb[0]) <= MECH_TRAIN_ALIGN)
            gap = _bbox_edge_gap(ba, bb_)
            shares_pipe = bool(equip_pipes.get(a["candidate_id"], set()) &
                               equip_pipes.get(b["candidate_id"], set()))
            if aligned and gap <= MECH_TRAIN_GAP and not shares_pipe:
                for nid in (a["candidate_id"], b["candidate_id"]):
                    n = node_by_id.get(nid)
                    if n:
                        n["unresolved_connection"] = True
                        n["connection_hint"] = "mechanical_shaft"

    # symbol ↔ symbol adjacency (existing 5B nearby_candidates)
    seen = set()
    for c in cands:
        for nb in (c.get("nearby_candidates") or []):
            key = tuple(sorted((c["candidate_id"], nb["candidate_id"])))
            if key in seen:
                continue
            seen.add(key)
            add_edge(c["candidate_id"], nb["candidate_id"], "ADJACENT_TO",
                     "spatial", 0.4, f"{nb.get('distance_px')}px apart")

    return {"nodes": nodes, "edges": edges}


# ═══════════════════════════════════════════════════════════════════════════
# Track B — Flow direction
# ═══════════════════════════════════════════════════════════════════════════

def detect_arrowheads(img_bgr, segments, exclude_bboxes=None):
    """Detect filled solid triangles (flow arrows) adjacent to pipe segments.
    Returns list of {x,y (tip), dir (unit vec tip points), segment_id, area}.

    NOTE (this drawing): arrowheads attach to pipes, so RETR_EXTERNAL merges them
    into the giant pipe-network contour and finds nothing. RETR_LIST recovers the
    triangle as its own contour. Triangles INSIDE a detected symbol bbox (valve
    check-glyphs, instrument internals) are excluded — those are symbol parts, not
    free flow arrows. Remaining triangles are still noisy (supports, signal-line
    arrows, text serifs); treat the seed set as low-precision."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST,
                                   cv2.CHAIN_APPROX_SIMPLE)
    pipe_segs = [s for s in segments if "pipe" in s["type"]]
    exclude_bboxes = exclude_bboxes or []

    def _inside_symbol(px, py):
        for b in exclude_bboxes:
            if (b["x1"] - 10 <= px <= b["x2"] + 10 and
                    b["y1"] - 10 <= py <= b["y2"] + 10):
                return True
        return False

    arrows = []
    seen_tips = []

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < ARROWHEAD_MIN_AREA or area > ARROWHEAD_MAX_AREA:
            continue
        hull = cv2.convexHull(cnt)
        hull_area = cv2.contourArea(hull)
        if hull_area <= 0:
            continue
        solidity = area / hull_area
        if solidity < ARROWHEAD_MIN_SOLIDITY:
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        ar = max(w, h) / max(min(w, h), 1)
        if not (ARROWHEAD_AR_MIN <= ar <= ARROWHEAD_AR_MAX):
            continue
        # Triangle apex (tip): vertex of the 3-vertex approximation farthest
        # from the midpoint of the opposite edge.
        approx = cv2.approxPolyDP(cnt, 0.08 * cv2.arcLength(cnt, True), True)
        if len(approx) != 3:
            continue
        pts = [tuple(p[0]) for p in approx]
        best_tip, best_d = None, -1.0
        for k in range(3):
            others = [pts[(k + 1) % 3], pts[(k + 2) % 3]]
            mid = ((others[0][0] + others[1][0]) / 2,
                   (others[0][1] + others[1][1]) / 2)
            d = math.hypot(pts[k][0] - mid[0], pts[k][1] - mid[1])
            if d > best_d:
                best_d, best_tip, best_mid = d, pts[k], mid
        tipx, tipy = best_tip
        dvec = (tipx - best_mid[0], tipy - best_mid[1])
        dlen = math.hypot(*dvec) or 1.0
        dvec = (dvec[0] / dlen, dvec[1] / dlen)

        # Nearest pipe segment within proximity
        best_seg, best_pd = None, ARROWHEAD_PIPE_PROXIMITY
        for s in pipe_segs:
            pd = dist_pt_to_segment(tipx, tipy, s["x0"], s["y0"], s["x1"], s["y1"])
            if pd < best_pd:
                best_pd, best_seg = pd, s
        if best_seg is None:
            continue
        # exclude triangles that are parts of detected symbols (valve/instr glyphs)
        cxm = (pts[0][0] + pts[1][0] + pts[2][0]) / 3
        cym = (pts[0][1] + pts[1][1] + pts[2][1]) / 3
        if _inside_symbol(cxm, cym):
            continue
        # dedupe near-identical tips
        if any(math.hypot(tipx - sx, tipy - sy) < 15 for sx, sy in seen_tips):
            continue
        seen_tips.append((tipx, tipy))
        arrows.append({
            "x": int(tipx), "y": int(tipy),
            "dir": dvec, "segment_id": best_seg["segment_id"],
            "pipeline_id": best_seg.get("pipeline_id"),
            "area": round(area, 1), "pipe_dist": round(best_pd, 1),
        })
    log.info("Arrowheads detected: %d (adjacent to pipes)", len(arrows))
    return arrows


def detect_check_valves(img_bgr, segments, exclude_bboxes=None):
    """Detect NPS check-valve glyphs ▷| — a solid/outlined triangle whose apex
    abuts a perpendicular seat-bar, the assembly sitting on a pipe axis.
    Flow direction = apex direction (apex points downstream).

    More specific than a bare arrowhead: the seat-bar + on-pipe gates reject
    support glyphs, signal-line arrows and text serifs. Returns list of
    {x,y (apex), dir, segment_id, pipeline_id, area, seat_dist}."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    H, W = binary.shape
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST,
                                   cv2.CHAIN_APPROX_SIMPLE)
    pipe_segs = [s for s in segments if "pipe" in s["type"]]
    exclude_bboxes = exclude_bboxes or []

    def _inside_symbol(px, py):
        for b in exclude_bboxes:
            if (b["x1"] - 10 <= px <= b["x2"] + 10 and
                    b["y1"] - 10 <= py <= b["y2"] + 10):
                return True
        return False

    def _seat_bar_at(ax, ay, dvec):
        """Perpendicular dark line within CHECK_VALVE_SEAT_BAR_DIST beyond apex?
        Returns (found, dist). Samples a span perpendicular to flow at offsets
        along the pointing axis; requires a contiguous foreground run >= 12px."""
        px, py = -dvec[1], dvec[0]            # perpendicular unit
        for L in range(0, CHECK_VALVE_SEAT_BAR_DIST + 1):
            cx, cy = ax + dvec[0] * L, ay + dvec[1] * L
            run = best = 0
            for t in range(-22, 23):          # ~44px span across the seat bar
                sx, sy = int(cx + px * t), int(cy + py * t)
                if 0 <= sx < W and 0 <= sy < H and binary[sy, sx] > 0:
                    run += 1
                    best = max(best, run)
                else:
                    run = 0
            if best >= 12:
                return True, L
        return False, None

    valves = []
    seen = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < CHECK_VALVE_MIN_AREA or area > ARROWHEAD_MAX_AREA:
            continue
        hull = cv2.convexHull(cnt)
        ha = cv2.contourArea(hull)
        if ha <= 0 or area / ha < ARROWHEAD_MIN_SOLIDITY:
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        ar = max(w, h) / max(min(w, h), 1)
        if not (ARROWHEAD_AR_MIN <= ar <= ARROWHEAD_AR_MAX):
            continue
        approx = cv2.approxPolyDP(cnt, 0.08 * cv2.arcLength(cnt, True), True)
        if len(approx) != 3:
            continue
        pts = [tuple(p[0]) for p in approx]
        best_tip, best_d, best_mid = None, -1.0, None
        for k in range(3):
            o = [pts[(k + 1) % 3], pts[(k + 2) % 3]]
            mid = ((o[0][0] + o[1][0]) / 2, (o[0][1] + o[1][1]) / 2)
            d = math.hypot(pts[k][0] - mid[0], pts[k][1] - mid[1])
            if d > best_d:
                best_d, best_tip, best_mid = d, pts[k], mid
        tipx, tipy = best_tip
        dvec = (tipx - best_mid[0], tipy - best_mid[1])
        dlen = math.hypot(*dvec) or 1.0
        dvec = (dvec[0] / dlen, dvec[1] / dlen)

        cxm = sum(p[0] for p in pts) / 3
        cym = sum(p[1] for p in pts) / 3
        if _inside_symbol(cxm, cym):
            continue
        # seat-bar gate (the | of ▷|)
        has_seat, seat_d = _seat_bar_at(tipx, tipy, dvec)
        if not has_seat:
            continue
        # on a pipe axis
        best_seg, best_pd = None, ARROWHEAD_PIPE_PROXIMITY
        for s in pipe_segs:
            pd = dist_pt_to_segment(cxm, cym, s["x0"], s["y0"], s["x1"], s["y1"])
            if pd < best_pd:
                best_pd, best_seg = pd, s
        if best_seg is None:
            continue
        if any(math.hypot(tipx - sx, tipy - sy) < 15 for sx, sy in seen):
            continue
        seen.append((tipx, tipy))
        valves.append({
            "x": int(tipx), "y": int(tipy), "dir": dvec,
            "segment_id": best_seg["segment_id"],
            "pipeline_id": best_seg.get("pipeline_id"),
            "area": round(area, 1), "seat_dist": seat_d,
            "pipe_dist": round(best_pd, 1),
        })
    log.info("Check-valve glyphs detected: %d (▷| on pipe)", len(valves))
    return valves


def _pipeline_axis(pipe, segments):
    """Two farthest-apart endpoints (end_A, end_B) of a pipeline's segments."""
    pts = []
    sset = set(pipe["segment_ids"])
    for s in segments:
        if s["segment_id"] in sset:
            pts.append((s["x0"], s["y0"]))
            pts.append((s["x1"], s["y1"]))
    if len(pts) < 2:
        b = pipe["bbox"]
        return (b["x1"], b["y1"]), (b["x2"], b["y2"])
    best = (pts[0], pts[1], -1)
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
            if d > best[2]:
                best = (pts[i], pts[j], d)
    return best[0], best[1]


def compute_flow_direction(pipelines, junctions, segments, cands,
                           arrowheads, check_valves=None, graph=None):
    """Seed pipeline flow from arrowheads + check-valve glyphs, propagate
    through junctions (BFS), apply equipment boundary conventions. Mutates
    pipelines in place (flow.direction, flow.evidence, inlet/outlet). Returns
    equipment up/down map.

    Direction model: each pipeline has geometric ends A and B (farthest apart).
      direction = 'forward'  => flow A -> B
                  'reverse'  => flow B -> A
    """
    check_valves = check_valves or []
    pl_by_id = {p["pipeline_id"]: p for p in pipelines}
    # geometric A/B ends per pipeline
    ends = {}
    for p in pipelines:
        A, B = _pipeline_axis(p, segments)
        ends[p["pipeline_id"]] = (A, B)
        p["_axis"] = {"A": A, "B": B}
        p["flow"] = {"direction": "unknown", "evidence": "none"}

    # ── Step 1 seeding: check-valves first (seat-bar gated → higher trust),
    #    then arrowheads. Each seed = apex/tip direction projected on pipe axis.
    seeded = {}
    seed_list = ([{"evidence": "check_valve", **cv} for cv in check_valves] +
                 [{"evidence": "arrowhead", **a} for a in arrowheads])
    for s in seed_list:
        pid = s["pipeline_id"]
        if not pid or pid not in pl_by_id:
            continue
        A, B = ends[pid]
        axis = (B[0] - A[0], B[1] - A[1])
        alen = math.hypot(*axis) or 1.0
        axis = (axis[0] / alen, axis[1] / alen)
        proj = s["dir"][0] * axis[0] + s["dir"][1] * axis[1]
        direction = "forward" if proj >= 0 else "reverse"
        if pid in seeded:
            if seeded[pid] != direction:
                pl_by_id[pid]["flow"]["evidence"] = "seed_conflict"
            continue   # keep first (check-valve wins over arrowhead)
        seeded[pid] = direction
        pl_by_id[pid]["flow"] = {"direction": direction, "evidence": s["evidence"]}

    # ── junction adjacency over pipelines ────────────────────────────────
    jn_by_id = {j["junction_id"]: j for j in junctions}

    def end_at_node(pid, node_point):
        """Return 'A' or 'B' — which pipeline end is nearest node_point."""
        A, B = ends[pid]
        return "A" if (math.hypot(A[0] - node_point[0], A[1] - node_point[1]) <=
                       math.hypot(B[0] - node_point[0], B[1] - node_point[1])) else "B"

    def downstream_end(pid):
        d = pl_by_id[pid]["flow"]["direction"]
        if d == "forward":
            return "B"
        if d == "reverse":
            return "A"
        return None

    # ── Step 2 propagation BFS through degree-2 junctions ────────────────
    dq = deque([pid for pid in seeded])
    while dq:
        pid = dq.popleft()
        dn_end = downstream_end(pid)
        if dn_end is None:
            continue
        for j in junctions:
            if pid not in j["connected_pipelines"]:
                continue
            jp = (j["point"]["x"], j["point"]["y"])
            others = [q for q in j["connected_pipelines"] if q != pid and q in pl_by_id]
            if j["degree"] > 2 or len(others) > 1:
                # ambiguous split/merge — mark junction, do not propagate through
                j["flow_type"] = "SPLIT_OR_MERGE"
                continue
            # which end of THIS pipe touches the junction?
            this_end = end_at_node(pid, jp)
            flows_into_jn = (this_end == dn_end)
            for q in others:
                if pl_by_id[q]["flow"]["direction"] != "unknown":
                    continue
                q_end = end_at_node(q, jp)
                # continuity: if flow enters junction, it leaves into q from q_end
                if flows_into_jn:
                    q_dir = "forward" if q_end == "A" else "reverse"
                else:
                    q_dir = "reverse" if q_end == "A" else "forward"
                pl_by_id[q]["flow"] = {"direction": q_dir, "evidence": "propagated"}
                dq.append(q)

    # ── Step 2b equipment boundary conventions ───────────────────────────
    COMPRESSORS = {"K-V-201", "KG-V-201", "KM-V-201"}
    equip = [c for c in cands if c.get("symbol_category") == "equipment"]
    equip_updown = {}
    for e in equip:
        eb = e.get("symbol_bbox", {})
        if not eb:
            continue
        ecx, ecy = bbox_center(eb)
        upstream, downstream = [], []
        tag = e.get("tag_text", "")
        for p in pipelines:
            # is this pipeline adjacent to the equipment? (one end near bbox)
            A, B = ends[p["pipeline_id"]]
            near_end = None
            for lab, pt in (("A", A), ("B", B)):
                if (eb["x1"] - EQUIP_PIPE_RADIUS <= pt[0] <= eb["x2"] + EQUIP_PIPE_RADIUS and
                        eb["y1"] - EQUIP_PIPE_RADIUS <= pt[1] <= eb["y2"] + EQUIP_PIPE_RADIUS):
                    near_end = (lab, pt)
                    break
            if not near_end:
                continue
            lab, pt = near_end
            # convention: decide IN/OUT by geometry of the connection point
            if tag in COMPRESSORS:
                is_out = pt[0] >= ecx          # right side = discharge OUT
            else:                              # drum / vessel
                is_out = pt[1] >= ecy + (eb["y2"] - eb["y1"]) * 0.15  # bottom = OUT
            if pl_by_id[p["pipeline_id"]]["flow"]["direction"] == "unknown":
                # orient so the equipment-end is upstream (IN) or downstream (OUT)
                if is_out:
                    pdir = "forward" if lab == "A" else "reverse"  # flow away from equip
                else:
                    pdir = "reverse" if lab == "A" else "forward"  # flow toward equip
                pl_by_id[p["pipeline_id"]]["flow"] = {
                    "direction": pdir, "evidence": "equipment_convention"}
            (downstream if is_out else upstream).append(p["pipeline_id"])
        equip_updown[e["candidate_id"]] = {
            "upstream_pipelines": upstream, "downstream_pipelines": downstream}

    # ── Step 2c dead-end (dead-leg) topology resolution ──────────────────
    # A pipeline with exactly ONE connected end (the other open) is a dead-leg/
    # stub: orient flow toward the connected end. Uses the SAME connectivity
    # definition as the graph (junction membership, equipment hub, CONNECTED_TO
    # candidates) so a pipeline with two real connections is never re-oriented
    # here — those stay for the Gemini fallback. Only fills still-'unknown'.
    n_dead_end = 0
    if graph is not None:
        cand_bbox = {c["candidate_id"]: c.get("symbol_bbox", {}) for c in cands}
        node_kind = {n["node_id"]: n.get("kind") for n in graph["nodes"]}
        pl_links = defaultdict(list)        # pipeline_id -> [(cand_id, rel, kind)]
        for e in graph["edges"]:
            f, t, rel = e["from"], e["to"], e["rel"]
            kf, kt = node_kind.get(f), node_kind.get(t)
            if kf == "pipeline" and kt in ("instrument", "valve", "piping", "equipment"):
                pl_links[f].append((t, rel, kt))
            elif kt == "pipeline" and kf in ("instrument", "valve", "piping", "equipment"):
                pl_links[t].append((f, rel, kf))

        for p in pipelines:
            pid = p["pipeline_id"]
            if pl_by_id[pid]["flow"]["direction"] != "unknown":
                continue
            links = pl_links.get(pid, [])
            has_cand = bool(links)
            # Exclude category-A noise (short stub, no candidate) and category-B
            # signal lines (only instrument+MONITORS) — same exclusions as triage,
            # so this pass resolves ONLY genuine dead-legs, never bleeds into noise.
            if p["segment_count"] < STUB_MAX_SEG and not has_cand:
                continue
            if has_cand and all(rel == "MONITORS" and kind == "instrument"
                                for _, rel, kind in links):
                continue
            A, B = ends[pid]
            end_conn = {"A": set(), "B": set()}      # end -> connection types present
            for j in junctions:
                if pid in j["connected_pipelines"]:
                    jp = (j["point"]["x"], j["point"]["y"])
                    end_conn[end_at_node(pid, jp)].add("junction")
            for cid, rel, kind in pl_links.get(pid, []):
                bb = cand_bbox.get(cid) or {}
                if not bb:
                    continue
                end = end_at_node(pid, bbox_center(bb))
                if kind == "equipment":
                    end_conn[end].add("equipment")
                elif rel == "CONNECTED_TO":
                    end_conn[end].add("candidate")
            connected_ends = [k for k, v in end_conn.items() if v]
            if len(connected_ends) != 1:
                continue
            ce = connected_ends[0]
            if not (end_conn[ce] & DEAD_END_CONN_TYPES):
                continue
            # flow toward the connected end: end A => reverse (B→A); end B => forward (A→B)
            pdir = "reverse" if ce == "A" else "forward"
            pl_by_id[pid]["flow"] = {"direction": pdir, "evidence": "topology_dead_end"}
            n_dead_end += 1
    log.info("Dead-end topology pass resolved %d pipelines", n_dead_end)

    # ── inlet/outlet nodes per pipeline ──────────────────────────────────
    for p in pipelines:
        d = p["flow"]["direction"]
        A, B = ends[p["pipeline_id"]]
        if d == "forward":
            p["inlet_point"], p["outlet_point"] = A, B
        elif d == "reverse":
            p["inlet_point"], p["outlet_point"] = B, A
        else:
            p["inlet_point"] = p["outlet_point"] = None
        p.pop("_axis", None)

    return equip_updown


def _build_gemini_client(api_key: str):
    """Same client pattern as step5a (new google.genai, legacy fallback)."""
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


def _build_pl_links(graph):
    """pipeline_id -> [(cand_id, rel, kind)] from candidate↔pipeline graph edges."""
    node_kind = {n["node_id"]: n.get("kind") for n in graph["nodes"]}
    pl_links = defaultdict(list)
    for e in graph["edges"]:
        f, t, rel = e["from"], e["to"], e["rel"]
        kf, kt = node_kind.get(f), node_kind.get(t)
        if kf == "pipeline" and kt in CAND_KINDS:
            pl_links[f].append((t, rel, kt))
        elif kt == "pipeline" and kf in CAND_KINDS:
            pl_links[t].append((f, rel, kf))
    return pl_links


def _nearer_end(A, B, pt):
    return "A" if (math.hypot(A[0] - pt[0], A[1] - pt[1]) <=
                   math.hypot(B[0] - pt[0], B[1] - pt[1])) else "B"


def select_category_d(pipelines, junctions, graph, segments, cands):
    """Return the still-'unknown' pipelines that are Gemini targets (category D):
    candidates attached AND both geometric ends connected. Uses the SAME
    connectivity definition as the dead-leg pass, so A/B noise and dead-legs
    are excluded here exactly as in the triage."""
    pl_links = _build_pl_links(graph)
    cand_bbox = {c["candidate_id"]: c.get("symbol_bbox", {}) for c in cands}
    out = []
    for p in pipelines:
        if p.get("flow", {}).get("direction") != "unknown":
            continue
        pid = p["pipeline_id"]
        links = pl_links.get(pid, [])
        if not links:                                   # no candidate => not D
            continue
        if all(rel == "MONITORS" and kind == "instrument"
               for _, rel, kind in links):              # signal line (cat B)
            continue
        A, B = _pipeline_axis(p, segments)
        end_conn = {"A": set(), "B": set()}
        for j in junctions:
            if pid in j["connected_pipelines"]:
                jp = (j["point"]["x"], j["point"]["y"])
                end_conn[_nearer_end(A, B, jp)].add("junction")
        for cid, rel, kind in links:
            bb = cand_bbox.get(cid) or {}
            if not bb:
                continue
            end = _nearer_end(A, B, bbox_center(bb))
            if kind == "equipment":
                end_conn[end].add("equipment")
            elif rel == "CONNECTED_TO":
                end_conn[end].add("candidate")
        if end_conn["A"] and end_conn["B"]:             # both ends connected
            out.append(p)
    return out


# direction word (Gemini) -> unit vector in image coords (y grows downward)
_FLOW_DIRVEC = {
    "left_to_right": (1.0, 0.0), "right_to_left": (-1.0, 0.0),
    "top_to_bottom": (0.0, 1.0), "bottom_to_top": (0.0, -1.0),
}


def _cluster_pipelines(pls, radius):
    """Greedy spatial clustering of pipelines by bbox-center proximity."""
    clusters = []
    for p in pls:
        b = p["bbox"]
        c = ((b["x1"] + b["x2"]) / 2.0, (b["y1"] + b["y2"]) / 2.0)
        for cl in clusters:
            if math.hypot(c[0] - cl["c"][0], c[1] - cl["c"][1]) < radius:
                cl["items"].append(p)
                break
        else:
            clusters.append({"c": c, "items": [p]})
    return [cl["items"] for cl in clusters]


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1 — Gemini instrument attachment (GATED on --gemini-attach)
# ═══════════════════════════════════════════════════════════════════════════

def _instrument_attach_targets(hierarchy):
    """Split instrument/valve hierarchy records into the two Gemini target groups.

      • unresolved — has graph edges (is_isolated False) but no equipment_parent
                     (couldn't reach equipment through traversal)
      • isolated   — zero edges of any kind (is_isolated True); no equipment_parent
                     either, and these need Gemini the most
    Records that already have an equipment_parent are never targets.
    """
    unresolved, isolated = [], []
    for h in hierarchy:
        if h.get("kind") not in ("instrument", "valve"):
            continue
        if h.get("equipment_parent"):
            continue
        if h.get("is_isolated"):
            isolated.append(h)
        else:
            unresolved.append(h)
    return unresolved, isolated


def _strip_area_prefix(tag):
    """Drop a leading area-prefix token (e.g. 'V-', 'K-', 'KM-', 'KG-')."""
    return re.sub(r"^[A-Z]+-", "", (tag or "").strip().upper())


# ── Equipment-tag validation (Fix 1: reject noise from Gemini attach) ────────
_EQUIPMENT_TAG_PATTERN = re.compile(
    r"^[A-Z]+-[A-Z]-?\d+[A-Z0-9-]*$", re.IGNORECASE
)
_EXCLUDED_EQUIPMENT_TOKENS = {"UNKNOWN", "NONE", ""}


def is_valid_equipment_tag(tag: str) -> bool:
    """Return True only if tag looks like a real P&ID equipment tag.

    Accepts: K-V-201, V-V-201, S-V-204, KM-V-201, V-BV-2248
    Rejects: '12IN-ETH-V012-61440X-PP', 'SB 6IN', '7-61440X', 'UNKNOWN', ''
    """
    if not tag:
        return False
    t = tag.strip().upper()
    if t in _EXCLUDED_EQUIPMENT_TOKENS:
        return False
    return bool(_EQUIPMENT_TAG_PATTERN.match(t))


def _match_tag(gemini_tag, candidate_keys):
    """Match a tag Gemini returned to one of ``candidate_keys`` (all uppercase),
    tolerating area-prefix differences (e.g. Gemini 'FV-208' → 'V-FV-208', or
    'V-201' → 'K-V-201'). Returns the matched candidate key, or None.

    Order: exact → both-stripped equality → suffix match (loosest, guarded to
    a stripped token of length ≥ 3 so trivial numeric tails don't over-match)."""
    g = (gemini_tag or "").strip().upper()
    if not g:
        return None
    keys = list(candidate_keys)
    if g in keys:
        return g
    gs = _strip_area_prefix(g)
    if not gs:
        return None
    for t in sorted(keys):                      # both stripped to the same core
        if _strip_area_prefix(t) == gs:
            return t
    if len(gs) >= 3:                            # suffix fallback (e.g. V-201→K-V-201)
        for t in sorted(keys):
            if t.endswith(gs):
                return t
    return None


def _cluster_instruments_by_x(items, radius):
    """Greedy spatial clustering of instruments (the algorithm specified for
    Phase 1): sort by x-coordinate, then add each instrument to the current
    cluster if its centroid is within ``radius`` of the cluster's running
    centroid, else start a new cluster. ``items`` carry a 'center' (x, y)."""
    ordered = sorted(items, key=lambda it: it["center"][0])
    clusters = []
    for it in ordered:
        if clusters:
            cl = clusters[-1]
            n = len(cl["items"])
            cx, cy = cl["sum_x"] / n, cl["sum_y"] / n
            if math.hypot(it["center"][0] - cx, it["center"][1] - cy) <= radius:
                cl["items"].append(it)
                cl["sum_x"] += it["center"][0]
                cl["sum_y"] += it["center"][1]
                continue
        clusters.append({"items": [it], "sum_x": it["center"][0],
                         "sum_y": it["center"][1]})
    return [cl["items"] for cl in clusters]


def gemini_instrument_attach(hierarchy, graph, cands, pipelines, segments, img,
                             api_key, out_dir, confirm=False, n_workers=8):
    """Phase 1: ask Gemini what equipment each unresolved/isolated instrument
    connects to, one image crop per spatial cluster.

    GATING: this function ALWAYS prints a pre-flight report (cluster count =
    number of Gemini calls, estimated cost, instruments being sent) BEFORE any
    API call. When ``confirm`` is False it returns immediately after that report
    (dry run, zero API calls). When ``confirm`` is True it crops + annotates +
    calls Gemini, caches by crop content hash, and adds GEMINI_ATTACHED edges to
    ``graph`` in place for high/medium-confidence attachments.

    Returns a report dict. Does NOT re-run the hierarchy — the caller does that
    after the new edges are added (Step 5)."""
    unresolved, isolated = _instrument_attach_targets(hierarchy)
    targets = unresolved + isolated
    log.info("Gemini attach targets: %d unresolved (edges, no equip parent) + "
             "%d isolated (zero edges) = %d total",
             len(unresolved), len(isolated), len(targets))

    cand_by_id = {c["candidate_id"]: c for c in cands}
    items = []
    for h in targets:
        c = cand_by_id.get(h["node_id"])
        bb = (c or {}).get("symbol_bbox") or {}
        ctr = _cand_center(c) if c else None
        if not bb or ctr is None:
            continue
        items.append({
            "node_id": h["node_id"], "tag_text": h.get("tag_text", ""),
            "bbox": bb, "center": ctr,
            "group": "isolated" if h.get("is_isolated") else "unresolved",
        })

    clusters = _cluster_instruments_by_x(items, GEMINI_ATTACH_CLUSTER_PX)
    n_calls = len(clusters)
    est_in = n_calls * GEMINI_ATTACH_EST_INPUT_TOK
    est_out = n_calls * GEMINI_ATTACH_EST_OUTPUT_TOK
    est_usd = (est_in / 1e6 * GEMINI_PRO_USD_PER_MTOK_IN +
               est_out / 1e6 * GEMINI_PRO_USD_PER_MTOK_OUT)

    # ── Pre-flight cost report (printed BEFORE any API call) ─────────────────
    print("\n=== Gemini instrument-attach PRE-FLIGHT (no API calls yet) ===")
    print(f"  unresolved (edges, no equip parent) : {len(unresolved)}")
    print(f"  isolated   (zero edges)             : {len(isolated)}")
    print(f"  total instruments being sent        : {len(items)}")
    print(f"  spatial clusters = Gemini calls     : {n_calls}")
    print(f"  est. tokens in / out                : ~{est_in} / ~{est_out}")
    print(f"  est. cost ({GEMINI_ATTACH_MODEL})   : ~${est_usd:.4f}  (approx)")

    report = {
        "n_unresolved": len(unresolved),
        "n_isolated": len(isolated),
        "n_sent": len(items),
        "n_clusters": n_calls,
        "est_cost_usd": round(est_usd, 4),
        "attachments_by_conf": {"high": 0, "medium": 0, "low": 0, "unknown": 0},
        "attachments_returned": 0,
        "edges_added": 0,
        "unknown_equipment_refs": [],
        "dry_run": not confirm,
    }
    if not confirm:
        print("  --> DRY RUN: Gemini NOT called. Re-run with --gemini-attach "
              "(without --gemini-attach-dry-run) to proceed.")
        return report

    if not api_key:
        raise RuntimeError("--gemini-attach requires --api-key / GEMINI_API_KEY")

    client, sdk = _build_gemini_client(api_key)
    H, W = img.shape[:2]
    pipe_segs = [s for s in segments if "pipe" in s["type"]]
    equip = [c for c in cands
             if c.get("symbol_category") == "equipment" and c.get("symbol_bbox")]
    equip_node_by_tag = {}
    for n in graph["nodes"]:
        if n.get("kind") == "equipment" and n.get("tag_text"):
            equip_node_by_tag.setdefault(n["tag_text"].strip().upper(), n["node_id"])

    # ── CHANGE 1: global equipment roster (so Gemini can name a parent that is
    #    NOT visible in a local crop — equipment is sparse + distant on this sheet) ──
    roster_lines = []
    for n in graph["nodes"]:
        if n.get("kind") != "equipment" or not n.get("tag_text"):
            continue
        bb = n.get("bbox") or {}
        if not bb:
            continue
        cx, cy = bbox_center(bb)
        side = "left" if cx < W / 2 else "right"
        vert = "top" if cy < H / 2 else "bottom"
        roster_lines.append(f"{n['tag_text']} — located {vert}-{side} of drawing")
    roster_text = "\n".join(roster_lines) if roster_lines else "(none detected)"

    cache_path = Path(out_dir) / "gemini_attach_cache.json"
    cache = {}
    if cache_path.exists():
        try:
            cache = json.load(open(cache_path))
        except Exception:
            cache = {}

    cache_lock = threading.Lock()

    crop_dir = Path(out_dir) / "gemini_attach_crops"
    crop_dir.mkdir(parents=True, exist_ok=True)

    prompt = (
        "This is a section of a P&ID engineering drawing.\n\n"
        "Red-circled instruments need parent equipment assignment.\n"
        "Blue rectangles are detected equipment.\n"
        "Gray lines are detected pipe segments.\n\n"
        "EQUIPMENT ON THIS DRAWING:\n"
        f"{roster_text}\n\n"
        "Use this list to name the parent equipment even if it is not visible "
        "in the crop. Reason from the pipe routing and instrument type to "
        "identify which equipment this instrument belongs to.\n\n"
        "For each red-circled instrument, answer:\n"
        "1. What equipment is it physically connected to? "
        "(via pipe, leader line, or direct mounting)\n"
        "2. If connected to a pipe, which direction does the pipe go to reach equipment?\n"
        "3. Confidence (high/medium/low)\n\n"
        'If you cannot determine the connection, say "unknown".\n\n'
        "Respond in JSON only:\n"
        "{\n"
        '  "attachments": [\n'
        "    {\n"
        '      "instrument_tag": "V-TIT-211",\n'
        '      "parent_equipment": "K-V-201",\n'
        '      "connection_type": "pipe" | "leader_line" | "mounted" | "unknown",\n'
        '      "confidence": "high" | "medium" | "low",\n'
        '      "reasoning": "connected via 2-inch line to compressor suction"\n'
        "    }\n"
        "  ]\n"
        "}"
    )

    # ── Per-cluster worker (runs in thread pool) ─────────────────────────────
    # Returns (ci, answer_or_None, tag_to_node, from_cache, error_str_or_None)
    # All mutations to shared state (cache, disk) go through cache_lock.
    # graph/report are mutated ONLY in the sequential apply phase after all
    # futures complete — no lock needed there.
    def _call_cluster(ci_cluster):
        ci, cluster = ci_cluster
        # Stagger starts to avoid a simultaneous burst on the first request
        time.sleep(ci * 0.1)

        # Build tag_to_node for this cluster (local — no shared state)
        tag_to_node = {}
        for it in cluster:
            if it["tag_text"]:
                tag_to_node.setdefault(it["tag_text"].strip().upper(), it["node_id"])

        # Tag-based cache key — stable across re-runs on the same drawing
        cluster_tags = sorted(it["tag_text"] for it in cluster if it["tag_text"])
        tag_key = "tag:" + hashlib.md5(
            json.dumps(cluster_tags, sort_keys=True).encode()).hexdigest()
        with cache_lock:
            if tag_key in cache:
                return ci, cache[tag_key], tag_to_node, True, None

        # Build annotated crop
        x1 = max(0, min(it["bbox"]["x1"] for it in cluster) - GEMINI_ATTACH_CROP_PAD_PX)
        y1 = max(0, min(it["bbox"]["y1"] for it in cluster) - GEMINI_ATTACH_CROP_PAD_PX)
        x2 = min(W, max(it["bbox"]["x2"] for it in cluster) + GEMINI_ATTACH_CROP_PAD_PX)
        y2 = min(H, max(it["bbox"]["y2"] for it in cluster) + GEMINI_ATTACH_CROP_PAD_PX)
        crop = img[y1:y2, x1:x2].copy()
        ch, cw = crop.shape[:2]
        if ch == 0 or cw == 0:
            return ci, None, tag_to_node, False, "empty crop"
        scale = (GEMINI_ATTACH_MAX_SIDE / max(ch, cw)
                 if max(ch, cw) > GEMINI_ATTACH_MAX_SIDE else 1.0)
        if scale != 1.0:
            crop = cv2.resize(crop, (int(cw * scale), int(ch * scale)))

        # capture loop vars for the transform helpers
        _x1, _y1, _scale = x1, y1, scale

        def _tx(px):
            return int((px - _x1) * _scale)

        def _ty(py):
            return int((py - _y1) * _scale)

        # gray lines for detected pipe segments in the region
        for s in pipe_segs:
            if (min(s["x0"], s["x1"]) > x2 or max(s["x0"], s["x1"]) < x1 or
                    min(s["y0"], s["y1"]) > y2 or max(s["y0"], s["y1"]) < y1):
                continue
            cv2.line(crop, (_tx(s["x0"]), _ty(s["y0"])),
                     (_tx(s["x1"]), _ty(s["y1"])), (150, 150, 150), 2)

        # blue rectangles for equipment detected within the padded region
        for e in equip:
            eb = e["symbol_bbox"]
            ec = bbox_center(eb)
            if not (x1 <= ec[0] <= x2 and y1 <= ec[1] <= y2):
                continue
            cv2.rectangle(crop, (_tx(eb["x1"]), _ty(eb["y1"])),
                          (_tx(eb["x2"]), _ty(eb["y2"])), (255, 0, 0), 2)
            cv2.putText(crop, e.get("tag_text", ""),
                        (_tx(eb["x1"]), max(0, _ty(eb["y1"]) - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)

        # red circles around each unresolved instrument + tag label
        for it in cluster:
            bb = it["bbox"]
            cx = _tx((bb["x1"] + bb["x2"]) / 2)
            cy = _ty((bb["y1"] + bb["y2"]) / 2)
            r = int(max(bb["x2"] - bb["x1"], bb["y2"] - bb["y1"]) / 2 * scale) + 8
            cv2.circle(crop, (cx, cy), max(r, 12), (0, 0, 255), 3)
            cv2.putText(crop, it["tag_text"], (cx + 10, cy),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

        ok, buf = cv2.imencode(".jpg", crop)
        if not ok:
            return ci, None, tag_to_node, False, "imencode failed"
        img_bytes = buf.tobytes()

        cv2.imwrite(str(crop_dir / f"cluster_{ci}.jpg"), crop)

        # Content-hash fallback cache (handles old entries written before tag-key)
        content_key = hashlib.sha256(img_bytes + prompt.encode()).hexdigest()
        with cache_lock:
            if content_key in cache:
                return ci, cache[content_key], tag_to_node, True, None

        # Gemini call with exponential-backoff retry on 429 / 503
        for attempt in range(3):
            try:
                if sdk == "new":
                    from google.genai import types as gt
                    resp = client.models.generate_content(
                        model=GEMINI_ATTACH_MODEL,
                        contents=[gt.Part.from_bytes(data=img_bytes,
                                                     mime_type="image/jpeg"),
                                  gt.Part.from_text(text=prompt)],
                        config=gt.GenerateContentConfig(
                            temperature=GEMINI_ATTACH_TEMP),
                    )
                    raw = resp.text.strip()
                else:
                    import google.generativeai as gl
                    import PIL.Image as PILImage
                    import io
                    pil = PILImage.open(io.BytesIO(img_bytes))
                    cfg = gl.GenerationConfig(temperature=GEMINI_ATTACH_TEMP)
                    resp = gl.GenerativeModel(GEMINI_ATTACH_MODEL).generate_content(
                        [prompt, pil], generation_config=cfg)
                    raw = resp.text.strip()

                clean = raw.replace("```json", "").replace("```", "").strip()
                m = re.search(r"\{.*\}", clean, re.DOTALL)
                answer = json.loads(m.group(0) if m else clean)

                with cache_lock:
                    cache[tag_key] = answer
                    cache[content_key] = answer
                    if len(cache) % 5 == 0:
                        try:
                            json.dump(cache, open(cache_path, "w"), indent=2)
                        except Exception:
                            pass

                return ci, answer, tag_to_node, False, None

            except Exception as e:
                err_str = str(e)
                if any(x in err_str for x in ("429", "503", "RESOURCE_EXHAUSTED",
                                               "overloaded")):
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    log.warning("Cluster %d rate-limited, retry in %ds (attempt %d/3)",
                                ci, wait, attempt + 1)
                    time.sleep(wait)
                else:
                    return ci, None, tag_to_node, False, err_str

        return ci, None, tag_to_node, False, "failed after 3 retries"

    # ── Parallel execution ────────────────────────────────────────────────────
    effective_workers = min(n_workers, n_calls) if n_calls else 1
    log.info("Gemini attach: running %d clusters with %d parallel workers",
             n_calls, effective_workers)

    cluster_results = [None] * n_calls  # (answer, tag_to_node, from_cache)
    completed_count = 0

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=effective_workers) as executor:
        future_to_ci = {
            executor.submit(_call_cluster, (ci, cluster)): ci
            for ci, cluster in enumerate(clusters)
        }
        for future in concurrent.futures.as_completed(future_to_ci):
            ci, answer, tag_to_node, from_cache, error = future.result()
            completed_count += 1
            status = "CACHE" if from_cache else ("ERR" if error else "LIVE")
            cluster_tags_preview = sorted(
                it["tag_text"] for it in clusters[ci] if it["tag_text"])[:5]
            log.info("Attach %d/%d [%s] cluster=%d  %d instr: %s",
                     completed_count, n_calls, status, ci,
                     len(clusters[ci]), ", ".join(cluster_tags_preview))
            if error:
                log.warning("Cluster %d failed: %s", ci, error)
            cluster_results[ci] = (answer, tag_to_node, from_cache)

    # Save cache once after all parallel calls complete
    try:
        json.dump(cache, open(cache_path, "w"), indent=2)
    except Exception:
        pass

    # ── Sequential result application (no thread safety needed) ──────────────
    eid = len(graph["edges"])
    attachments_all = []
    unknown_refs = []

    for ci, result in enumerate(cluster_results):
        if result is None:
            continue
        answer, tag_to_node, _from_cache = result
        if answer is None:
            continue

        for att in (answer.get("attachments") or []):
            attachments_all.append(att)
            conf_word = (att.get("confidence") or "unknown").lower()
            if conf_word not in report["attachments_by_conf"]:
                conf_word = "unknown"
            report["attachments_by_conf"][conf_word] += 1
            if conf_word not in ("high", "medium"):
                continue
            ikey = _match_tag(att.get("instrument_tag"), tag_to_node.keys())
            inode = tag_to_node.get(ikey) if ikey else None
            if not inode:
                log.info("Gemini attach: instrument tag not in this cluster: %s",
                         att.get("instrument_tag"))
                continue
            raw_equip = att.get("parent_equipment")
            if not is_valid_equipment_tag(raw_equip):
                log.info("Rejected invalid equipment tag: %s", raw_equip)
                continue
            ekey = _match_tag(raw_equip, equip_node_by_tag.keys())
            enode = equip_node_by_tag.get(ekey) if ekey else None
            if not enode:
                log.info("Gemini referenced unknown equipment: %s", raw_equip)
                unknown_refs.append(raw_equip)
                continue
            conf = (GEMINI_ATTACH_CONF_HIGH if conf_word == "high"
                    else GEMINI_ATTACH_CONF_MEDIUM)
            eq_cand = next((c for c in cands if c["candidate_id"] == enode), None)
            if eq_cand and eq_cand.get("is_label_only"):
                conf = min(conf, IS_LABEL_ONLY_CONF_CAP)
            graph["edges"].append({
                "edge_id": f"E-{eid}", "from": inode, "to": enode,
                "rel": "GEMINI_ATTACHED", "category": "functional",
                "directed": False, "confidence": conf,
                "evidence": f"gemini_vision: {att.get('reasoning', '')}",
            })
            eid += 1
            report["edges_added"] += 1

    report["attachments_returned"] = len(attachments_all)
    report["unknown_equipment_refs"] = unknown_refs
    log.info("Gemini attach: %d attachments returned, %d new GEMINI_ATTACHED "
             "edges, %d unknown-equipment refs",
             len(attachments_all), report["edges_added"], len(unknown_refs))
    return report


def gemini_flow_fallback(pipelines, junctions, graph, segments, cands,
                         img, api_key, out_dir):
    """GATED Gemini fallback — resolves flow direction for category-D pipelines
    only. Batches by spatial cluster (one image crop per cluster). Each crop is
    annotated with numbered pipeline polylines; Gemini returns a left/right/
    top/bottom answer per number, projected onto the pipeline axis -> forward/
    reverse. Sets flow.evidence='gemini_vision'. Cached by crop content hash.
    Returns count resolved. Mutates pipelines in place (incl inlet/outlet)."""
    targets = select_category_d(pipelines, junctions, graph, segments, cands)
    if not targets:
        log.info("Gemini flow fallback: no category-D pipelines — nothing to do")
        return 0
    clusters = _cluster_pipelines(targets, GEMINI_FLOW_CLUSTER_PX)
    log.info("Gemini flow fallback: %d category-D pipelines in %d clusters",
             len(targets), len(clusters))

    client, sdk = _build_gemini_client(api_key)
    pl_by_id = {p["pipeline_id"]: p for p in pipelines}
    seg_by_id = {s["segment_id"]: s for s in segments}
    H, W = img.shape[:2]

    cache_path = Path(out_dir) / "gemini_flow_cache.json"
    cache = {}
    if cache_path.exists():
        try:
            cache = json.load(open(cache_path))
        except Exception:
            cache = {}

    resolved = 0
    crop_dir = Path(out_dir) / "gemini_flow_crops"
    crop_dir.mkdir(parents=True, exist_ok=True)

    for ci, cluster in enumerate(clusters):
        # union bbox + padding -> crop window (clamped)
        x1 = max(0, min(p["bbox"]["x1"] for p in cluster) - GEMINI_FLOW_PAD_PX)
        y1 = max(0, min(p["bbox"]["y1"] for p in cluster) - GEMINI_FLOW_PAD_PX)
        x2 = min(W, max(p["bbox"]["x2"] for p in cluster) + GEMINI_FLOW_PAD_PX)
        y2 = min(H, max(p["bbox"]["y2"] for p in cluster) + GEMINI_FLOW_PAD_PX)
        crop = img[y1:y2, x1:x2].copy()
        ch, cw = crop.shape[:2]
        if ch == 0 or cw == 0:
            continue
        scale = GEMINI_FLOW_MAX_SIDE / max(ch, cw) if max(ch, cw) > GEMINI_FLOW_MAX_SIDE else 1.0
        if scale != 1.0:
            crop = cv2.resize(crop, (int(cw * scale), int(ch * scale)))

        # annotate each pipeline with a number + polyline; build prompt legend
        legend = []
        label_to_pid = {}
        axis_by_label = {}
        for k, p in enumerate(cluster, start=1):
            pid = p["pipeline_id"]
            label_to_pid[k] = pid
            A, B = _pipeline_axis(p, segments)
            axis_by_label[k] = (A, B)
            for sid in p["segment_ids"]:
                s = seg_by_id.get(sid)
                if not s:
                    continue
                pa = (int((s["x0"] - x1) * scale), int((s["y0"] - y1) * scale))
                pb = (int((s["x1"] - x1) * scale), int((s["y1"] - y1) * scale))
                cv2.line(crop, pa, pb, (0, 0, 255), 2)
            mx = int((bbox_center(p["bbox"])[0] - x1) * scale)
            my = int((bbox_center(p["bbox"])[1] - y1) * scale)
            cv2.putText(crop, str(k), (mx, my), cv2.FONT_HERSHEY_SIMPLEX,
                        1.2, (255, 0, 0), 3)
            ea = (int((A[0] - x1) * scale), int((A[1] - y1) * scale))
            eb = (int((B[0] - x1) * scale), int((B[1] - y1) * scale))
            legend.append(f"  Pipeline {k}: red line, ends at {ea} and {eb}")

        ok, buf = cv2.imencode(".jpg", crop)
        if not ok:
            continue
        img_bytes = buf.tobytes()

        prompt = (
            "You are a P&ID flow-direction analyst. The image shows pipeline "
            "segments drawn as RED lines, each labeled with a blue number.\n"
            "For EACH numbered pipeline, determine the process flow direction "
            "along that line using arrowheads, check valves, equipment inlets/"
            "outlets, and connectivity visible in the image.\n\n"
            "Numbered pipelines (coordinates are in this image's pixel space):\n"
            + "\n".join(legend) +
            "\n\nAnswer ONLY with JSON mapping each number to one of exactly: "
            '"left_to_right", "right_to_left", "top_to_bottom", '
            '"bottom_to_top", or "unknown" if you cannot tell.\n'
            'Example: {"1": "left_to_right", "2": "unknown"}'
        )

        # cache key = content hash of the crop + legend (deterministic)
        key = hashlib.sha256(img_bytes + prompt.encode()).hexdigest()
        if key in cache:
            answer = cache[key]
        else:
            try:
                if sdk == "new":
                    from google.genai import types as gt
                    resp = client.models.generate_content(
                        model=GEMINI_FLOW_MODEL,
                        contents=[gt.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                                  gt.Part.from_text(text=prompt)],
                        config=gt.GenerateContentConfig(temperature=GEMINI_FLOW_TEMP),
                    )
                    raw = resp.text.strip()
                else:
                    import google.generativeai as gl
                    import PIL.Image as PILImage
                    import io
                    pil = PILImage.open(io.BytesIO(img_bytes))
                    cfg = gl.GenerationConfig(temperature=GEMINI_FLOW_TEMP)
                    resp = gl.GenerativeModel(GEMINI_FLOW_MODEL).generate_content(
                        [prompt, pil], generation_config=cfg)
                    raw = resp.text.strip()
                clean = raw.replace("```json", "").replace("```", "").strip()
                m = re.search(r"\{.*\}", clean, re.DOTALL)
                answer = json.loads(m.group(0) if m else clean)
                cache[key] = answer
            except Exception as e:
                log.warning("Gemini flow cluster %d error: %s", ci, e)
                continue

        cv2.imwrite(str(crop_dir / f"cluster_{ci}.jpg"), crop)

        # map answers -> pipeline forward/reverse via axis projection
        for k_str, word in answer.items():
            try:
                k = int(k_str)
            except (ValueError, TypeError):
                continue
            pid = label_to_pid.get(k)
            if not pid or word not in _FLOW_DIRVEC:
                continue
            p = pl_by_id[pid]
            if p.get("flow", {}).get("direction") != "unknown":
                continue
            A, B = axis_by_label[k]
            ax = (B[0] - A[0], B[1] - A[1])
            alen = math.hypot(*ax) or 1.0
            ax = (ax[0] / alen, ax[1] / alen)
            dv = _FLOW_DIRVEC[word]
            proj = dv[0] * ax[0] + dv[1] * ax[1]
            direction = "forward" if proj >= 0 else "reverse"
            p["flow"] = {"direction": direction, "evidence": "gemini_vision"}
            if direction == "forward":
                p["inlet_point"], p["outlet_point"] = A, B
            else:
                p["inlet_point"], p["outlet_point"] = B, A
            resolved += 1

    try:
        json.dump(cache, open(cache_path, "w"), indent=2)
    except Exception:
        pass
    log.info("Gemini flow fallback resolved %d pipelines (evidence=gemini_vision)", resolved)
    return resolved


def detect_signal_edges(img, cands):
    """Track C: detect dashed signal-line edges between instrument/valve bubbles
    via proximity-gated strict path-probing. Returns a list of
    {a, b, evidence} dicts (a,b = candidate_ids). No new detection of symbols;
    only connects existing candidates where real dashed pixels form a path."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    binimg = cv2.threshold(gray, 0, 255,
                           cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    Hh, Ww = binimg.shape
    sig = [c for c in cands
           if c.get("symbol_category") in ("instrument", "valve")
           and c.get("symbol_bbox")]

    def center(bb):
        return ((bb["x1"] + bb["x2"]) / 2.0, (bb["y1"] + bb["y2"]) / 2.0)

    def in_bb(x, y, bb, pad=5):
        return bb["x1"] - pad <= x <= bb["x2"] + pad and bb["y1"] - pad <= y <= bb["y2"] + pad

    def ink_at(x, y, vert):
        xi, yi = int(x), int(y)
        if not (0 <= xi < Ww and 0 <= yi < Hh):
            return False
        for d in range(-SIGNAL_PROBE_HALF, SIGNAL_PROBE_HALF + 1):
            px, py = (xi, yi + d) if not vert else (xi + d, yi)
            if 0 <= px < Ww and 0 <= py < Hh and binimg[py, px] > 0:
                return True
        return False

    def seg_class(p0, p1, bbA, bbB, vert, others):
        L = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        if L < 1:
            return "short"
        n = int(L / SIGNAL_PROBE_STEP)
        if n < 3:
            return "short"
        samples = []
        for k in range(n + 1):
            t = k / n
            x = p0[0] + (p1[0] - p0[0]) * t
            y = p0[1] + (p1[1] - p0[1]) * t
            if in_bb(x, y, bbA) or in_bb(x, y, bbB):
                continue
            for ob in others:
                if in_bb(x, y, ob, SIGNAL_THIRD_BUBBLE):
                    return "blocked"
            samples.append(1 if ink_at(x, y, vert) else 0)
        if len(samples) < SIGNAL_MIN_PATHLEN / SIGNAL_PROBE_STEP:
            return "short"
        cov = sum(samples) / len(samples)
        trans = sum(1 for i in range(1, len(samples)) if samples[i] != samples[i - 1])
        if cov > SIGNAL_COV_MAX:
            return "solid"
        if cov < SIGNAL_COV_MIN:
            return "empty"
        return "dashed" if trans >= SIGNAL_MIN_TRANS else "weak"

    def probe_pair(a, b, others):
        bbA, bbB = a["symbol_bbox"], b["symbol_bbox"]
        ca, cb = center(bbA), center(bbB)
        c1, c2 = (cb[0], ca[1]), (ca[0], cb[1])
        paths = [
            [(ca, c1, False), (c1, cb, True)],     # horizontal then vertical
            [(ca, c2, True), (c2, cb, False)],     # vertical then horizontal
            [(ca, cb, abs(cb[0] - ca[0]) < abs(cb[1] - ca[1]))],   # straight
        ]
        for path in paths:
            segc = [seg_class(p0, p1, bbA, bbB, v, others) for (p0, p1, v) in path]
            if "dashed" in segc and all(s in ("dashed", "short") for s in segc):
                return True
        return False

    centers = [center(c["symbol_bbox"]) for c in sig]
    edges, seen = [], set()
    for i in range(len(sig)):
        for j in range(i + 1, len(sig)):
            a, b = sig[i], sig[j]
            ca, cb = centers[i], centers[j]
            if math.hypot(ca[0] - cb[0], ca[1] - cb[1]) > SIGNAL_MAX_PAIR_DIST:
                continue
            others = [c["symbol_bbox"] for k, c in enumerate(sig) if k != i and k != j]
            if probe_pair(a, b, others):
                key = tuple(sorted([a["candidate_id"], b["candidate_id"]]))
                if key not in seen:
                    seen.add(key)
                    edges.append({"a": key[0], "b": key[1], "evidence": "dashed_path_probe"})
    log.info("Track C: %d signal edges detected (strict path-probe)", len(edges))
    return edges


# ISA-5.1 function-letter roles for control-loop labelling
def _loop_role(tag):
    """Coarse role from the function letters of an ISA tag (after any V- prefix)."""
    t = (tag or "").upper().lstrip("V-").lstrip("-")
    letters = re.match(r"[A-Z]+", t)
    fl = letters.group(0) if letters else ""
    if "C" in fl:
        return "controller"
    if "T" in fl and ("IT" in fl or fl.endswith("T")):
        return "transmitter"
    if fl.endswith("E") or "E" in fl[:2]:
        return "element"
    if fl.startswith(("FV", "FCV", "PV", "TV", "LV", "ZV", "XV", "BV", "GV")):
        return "valve"
    return "other"


def build_control_loops(signal_edges, cands):
    """Track C: connected components over SIGNAL edges only = control loops.
    Components with > SIGNAL_LOOP_MAX_SIZE members are over-merge artifacts
    (crossing-dash false edges) and are emitted as 'unresolved_signal_cluster',
    NOT as control loops. Returns (control_loops, cluster_artifacts, node_to_loop)."""
    by_id = {c["candidate_id"]: c for c in cands}
    par = {}

    def find(x):
        par.setdefault(x, x)
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    nodes = set()
    for e in signal_edges:
        par[find(e["a"])] = find(e["b"])
        nodes.add(e["a"]); nodes.add(e["b"])
    comp = defaultdict(list)
    for n in nodes:
        comp[find(n)].append(n)

    loops, clusters, node_to_loop = [], [], {}
    comps = sorted(comp.values(), key=lambda c: (-len(c), min(c)))
    li = ci = 0
    for members in comps:
        members = sorted(members)
        tags = [by_id.get(m, {}).get("tag_text", "") for m in members]
        if len(members) > SIGNAL_LOOP_MAX_SIZE:
            cid = f"CLUSTER-{ci:02d}"; ci += 1
            clusters.append({"cluster_id": cid, "size": len(members),
                             "member_ids": members, "member_tags": tags,
                             "reason": "over_merge_artifact (crossing dashes in dense region)"})
            continue
        lid = f"CL-{li:02d}"; li += 1
        roles = {m: _loop_role(by_id.get(m, {}).get("tag_text", "")) for m in members}
        controllers = [by_id[m]["tag_text"] for m in members if roles[m] == "controller"]
        for m in members:
            node_to_loop[m] = lid
        loops.append({
            "loop_id": lid,
            "size": len(members),
            "controllers": controllers,
            "member_ids": members,
            "member_tags": tags,
            "roles": {by_id.get(m, {}).get("tag_text", m): roles[m] for m in members},
        })
    log.info("Track C: %d control loops (size<=%d), %d unresolved clusters",
             len(loops), SIGNAL_LOOP_MAX_SIZE, len(clusters))
    return loops, clusters, node_to_loop


def mark_directed_graph(graph, pipelines, junctions, cands, equip_updown):
    """Step 4: stamp directed flags + flow onto pipelines/edges/equipment.
    Returns evidence-source counts for the report."""
    pl_by_id = {p["pipeline_id"]: p for p in pipelines}
    node_by_id = {n["node_id"]: n for n in graph["nodes"]}

    def nearest_jn(point):
        if point is None:
            return None
        best, bd = None, SNAP_TOL_PX * 3
        for j in junctions:
            d = math.hypot(j["point"]["x"] - point[0], j["point"]["y"] - point[1])
            if d < bd:
                bd, best = d, j["junction_id"]
        return best

    # inlet/outlet junction nodes per pipeline + directed pipeline adjacency
    jn_in = defaultdict(list)    # junction_id -> pipelines whose INLET is here
    jn_out = defaultdict(list)   # junction_id -> pipelines whose OUTLET is here
    for p in pipelines:
        i_jn = nearest_jn(p.get("inlet_point"))
        o_jn = nearest_jn(p.get("outlet_point"))
        p["inlet_nodes"] = [i_jn] if i_jn else []
        p["outlet_nodes"] = [o_jn] if o_jn else []
        if i_jn:
            jn_in[i_jn].append(p["pipeline_id"])
        if o_jn:
            jn_out[o_jn].append(p["pipeline_id"])

    def downstream_pipes(pid):
        outs = pl_by_id[pid].get("outlet_nodes") or []
        res = []
        for jn in outs:
            res += jn_in.get(jn, [])
        return res

    # directed edges: any edge touching a direction-known pipeline
    for e in graph["edges"]:
        for endpt in (e["from"], e["to"]):
            p = pl_by_id.get(endpt)
            if p and p["flow"]["direction"] in ("forward", "reverse"):
                e["directed"] = True
                e["flow_direction"] = p["flow"]["direction"]
                e["flow_evidence"] = p["flow"]["evidence"]
                break

    # equipment upstream/downstream: pipelines + reachable equipment (directed)
    equip_nodes = {c["candidate_id"]: c for c in cands
                   if c.get("symbol_category") == "equipment"}
    pipe_to_equip = defaultdict(list)   # pipeline_id -> equipment candidate_ids touching it
    for cid, ud in equip_updown.items():
        for pid in ud["upstream_pipelines"] + ud["downstream_pipelines"]:
            pipe_to_equip[pid].append(cid)

    for cid, ud in equip_updown.items():
        n = node_by_id.get(cid)
        if not n:
            continue
        # walk downstream pipelines (bounded) to find reachable equipment
        down_eq, seen = set(), set()
        dq = deque(ud["downstream_pipelines"])
        while dq:
            pid = dq.popleft()
            if pid in seen:
                continue
            seen.add(pid)
            for eq in pipe_to_equip.get(pid, []):
                if eq != cid:
                    down_eq.add(eq)
            for nxt in downstream_pipes(pid):
                if nxt not in seen:
                    dq.append(nxt)
        n["downstream_pipelines"] = ud["downstream_pipelines"]
        n["upstream_pipelines"] = ud["upstream_pipelines"]
        n["downstream_equipment"] = sorted(down_eq)
        # upstream equipment = those whose downstream reaches this one
        up_eq = [oc for oc, oud in equip_updown.items()
                 if cid in node_by_id and oc != cid and
                 cid in (node_by_id.get(oc, {}) or {}).get("downstream_equipment", [])]
        n["upstream_equipment"] = sorted(up_eq)

    counts = {"arrowhead": 0, "propagated": 0, "equipment_convention": 0,
              "unknown": 0, "arrowhead_conflict": 0}
    for p in pipelines:
        ev = p["flow"]["evidence"] if p["flow"]["direction"] != "unknown" else "unknown"
        counts[ev] = counts.get(ev, 0) + 1
    return counts


def annotate_directed_hierarchy(hierarchy, graph, pipelines):
    """Add a directed_flow_path to each candidate that sits on a directed
    pipeline — source→…→sink read along flow — without disturbing the
    undirected parent_chain (kept for back-compat)."""
    pl_by_id = {p["pipeline_id"]: p for p in pipelines}
    node_by_id = {n["node_id"]: n for n in graph["nodes"]}
    # candidate -> pipeline it monitors/connects to (first physical edge)
    cand_pipe = {}
    for e in graph["edges"]:
        if e["category"] != "physical":
            continue
        for a, b in ((e["from"], e["to"]), (e["to"], e["from"])):
            if a in pl_by_id and node_by_id.get(b, {}).get("kind") not in (
                    "pipeline", "junction"):
                cand_pipe.setdefault(b, a)
    for rec in hierarchy:
        pid = cand_pipe.get(rec["node_id"])
        if pid and pl_by_id[pid]["flow"]["direction"] in ("forward", "reverse"):
            p = pl_by_id[pid]
            rec["directed_flow_path"] = {
                "on_pipeline": pid,
                "flow_direction": p["flow"]["direction"],
                "flow_evidence": p["flow"]["evidence"],
                "inlet_nodes": p.get("inlet_nodes", []),
                "outlet_nodes": p.get("outlet_nodes", []),
            }
        else:
            rec["directed_flow_path"] = None


# ═══════════════════════════════════════════════════════════════════════════
# 6. Hierarchy via undirected graph traversal
# ═══════════════════════════════════════════════════════════════════════════

def _node_rank(node):
    if node["kind"] == "pipeline":
        return PIPELINE_RANK
    if node["kind"] == "junction":
        return JUNCTION_RANK
    return CATEGORY_RANK.get(node["kind"], 0.5)


def build_hierarchy(graph, spatial):
    """Undirected components -> pick a root by (rank, degree, bbox area) ->
    BFS tree gives parent/child/ancestor/descendant/siblings/root-to-leaf.

    Track C / Decision A2: the rooting BFS uses ONLY non-signal edges, so the
    PROCESS hierarchy (parent_chain/root_system/ancestor_path) is unchanged by
    signal lines — control-loop membership lives separately in control_loops[].
    is_isolated, however, is computed from the FULL graph degree (process AND
    signal), so a signal-only instrument is correctly NOT isolated (Option A)."""
    nodes = {n["node_id"]: n for n in graph["nodes"]}
    adj = defaultdict(set)        # rooting/process adjacency — excludes signal
    full_adj = defaultdict(set)   # all edges — drives is_isolated

    # ── FIX 2/3: edge-derived maps for equipment_parent + parent provenance ──
    mounted_eq = defaultdict(list)   # node_id -> [(eq_node_id, conf, evidence)]
    pipe_equip = defaultdict(set)    # pipeline_id -> {equipment node_id touching it}
    pair_edges = defaultdict(list)   # frozenset(a,b) -> [edge, ...] (parent provenance)
    for e in graph["edges"]:
        full_adj[e["from"]].add(e["to"])
        full_adj[e["to"]].add(e["from"])
        f, t = e["from"], e["to"]
        pair_edges[frozenset((f, t))].append(e)
        kf = nodes.get(f, {}).get("kind")
        kt = nodes.get(t, {}).get("kind")
        # MOUNTED_ON (CV proximity) and GEMINI_ATTACHED (Phase 1 vision) are both
        # direct instrument→equipment bindings → strongest equipment_parent signal.
        if e.get("rel") in ("MOUNTED_ON", "GEMINI_ATTACHED"):
            if kt == "equipment":
                mounted_eq[f].append((t, e.get("confidence"), e.get("evidence")))
            elif kf == "equipment":
                mounted_eq[t].append((f, e.get("confidence"), e.get("evidence")))
        if kf == "pipeline" and kt == "equipment":
            pipe_equip[f].add(t)
        elif kt == "pipeline" and kf == "equipment":
            pipe_equip[t].add(f)
        if e.get("category") == "signal":
            continue
        adj[e["from"]].add(e["to"])
        adj[e["to"]].add(e["from"])

    def _resolve_equipment_parent(nid, par, anc):
        """Resolve the nearest EQUIPMENT parent for a node (FIX 2 + FIX 3 PartA).

        Priority (strongest physical evidence first):
          1. direct MOUNTED_ON edge to equipment  → edge confidence (~0.674+),
             edge evidence (instrument physically mounted on equipment).
          2. direct_parent is itself equipment     → parent-edge confidence.
          3. first equipment ancestor in parent_chain (reached via a pipeline /
             junction) → confidence 0.5, evidence='pipeline_traversal'.
          4. direct_parent is a pipeline that touches exactly ONE equipment
             (line-collapse — equipment need not be an ancestor; this is the
             repair logic moved out of step9) → 0.5, 'pipeline_traversal'.
          5. nothing → all None (do not guess).
        Returns (tag, node_id, confidence, evidence)."""
        # 1. direct MOUNTED_ON edge
        if mounted_eq.get(nid):
            eq_id, conf, ev = max(mounted_eq[nid], key=lambda x: (x[1] or 0.0))
            return (nodes.get(eq_id, {}).get("tag_text", ""), eq_id, conf,
                    ev or "MOUNTED_ON")
        # 2. direct_parent is equipment
        if par and nodes.get(par, {}).get("kind") == "equipment":
            edge = (pair_edges.get(frozenset((nid, par))) or [None])[0]
            conf = edge.get("confidence") if edge else None
            return nodes[par].get("tag_text", ""), par, conf, "direct_parent"
        # 3. first equipment ancestor in parent_chain (via pipeline/junction)
        for a in anc:
            if nodes.get(a, {}).get("kind") == "equipment":
                return nodes[a].get("tag_text", ""), a, 0.5, "pipeline_traversal"
        # 4. direct_parent pipeline → single touching equipment (line collapse)
        if par and nodes.get(par, {}).get("kind") == "pipeline":
            eqs = [e for e in pipe_equip.get(par, set()) if e != nid]
            if len(eqs) == 1:
                return (nodes[eqs[0]].get("tag_text", ""), eqs[0], 0.5,
                        "pipeline_traversal")
        return None, None, None, None

    def _parent_provenance(nid, par):
        """confidence + evidence of the graph edge connecting nid to direct_parent."""
        if not par:
            return None, None
        edge = (pair_edges.get(frozenset((nid, par))) or [None])[0]
        if edge:
            return edge.get("confidence"), edge.get("evidence")
        return None, None

    # connected components
    seen, components = set(), []
    for nid in nodes:
        if nid in seen:
            continue
        comp, dq = [], deque([nid])
        seen.add(nid)
        while dq:
            u = dq.popleft()
            comp.append(u)
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    dq.append(v)
        components.append(comp)

    parent = {}
    depth = {}
    root_of = {}
    children = defaultdict(list)

    for comp in components:
        # Guard: adjacency can contain stale IDs if any caller bypasses the
        # pre-filter; silently skip them rather than KeyError.
        comp = [nid for nid in comp if nid in nodes]
        if not comp:
            continue
        # root = best rank, then degree, then bbox area
        root = max(comp, key=lambda nid: (
            _node_rank(nodes[nid]), len(adj[nid]), bbox_area(nodes[nid]["bbox"])))
        # BFS tree from root
        parent[root] = None
        depth[root] = 0
        dq = deque([root])
        visited = {root}
        while dq:
            u = dq.popleft()
            for v in sorted(adj[u]):
                if v not in visited:
                    visited.add(v)
                    parent[v] = u
                    depth[v] = depth[u] + 1
                    children[u].append(v)
                    dq.append(v)
        for nid in comp:
            root_of[nid] = root

    def ancestor_path(nid):
        path, cur = [], parent.get(nid)
        while cur is not None:
            path.append(cur)
            cur = parent.get(cur)
        return path  # immediate -> root

    def descendants(nid):
        out, dq = [], deque(children[nid])
        while dq:
            u = dq.popleft()
            out.append(u)
            dq.extend(children[u])
        return out

    def root_to_leaf_chains(nid):
        """All downward chains from nid to its leaves."""
        chains = []
        def dfs(u, acc):
            if not children[u]:
                chains.append(acc[:])
                return
            for ch in children[u]:
                acc.append(ch)
                dfs(ch, acc)
                acc.pop()
        dfs(nid, [nid])
        return chains

    hierarchy = []
    for nid, n in nodes.items():
        if n["kind"] in ("pipeline", "junction"):
            continue   # hierarchy records are emitted for real candidates
        anc = ancestor_path(nid)
        par = parent.get(nid)
        sibs = [s for s in children.get(par, []) if s != nid] if par else []
        sp = spatial.get(nid, {})
        ep_tag, ep_id, ep_conf, ep_ev = _resolve_equipment_parent(nid, par, anc)
        p_conf, p_ev = _parent_provenance(nid, par)
        hierarchy.append({
            "node_id": nid,
            "tag_text": n["tag_text"],
            "kind": n["kind"],
            "root_system": root_of.get(nid),
            "direct_parent": par,
            "parent_confidence": p_conf,
            "parent_evidence": p_ev,
            # FIX 2/3: nearest equipment parent (self-contained, no step9 repair)
            "equipment_parent": ep_tag,
            "equipment_parent_id": ep_id,
            "equipment_parent_confidence": ep_conf,
            "equipment_parent_evidence": ep_ev,
            "parent_chain": anc,                 # immediate -> root
            "ancestor_path": anc,
            "children": children.get(nid, []),
            "descendant_nodes": descendants(nid),
            "siblings": sibs,
            "depth": depth.get(nid),
            "root_to_leaf": root_to_leaf_chains(nid),
            # Option A: isolated == zero edges of ANY kind (process OR signal).
            # A signal-only instrument has a signal edge => not isolated, even
            # though it is a singleton in the process rooting tree above.
            "is_isolated": not full_adj.get(nid),
            "spatial": {k: sp.get(k, []) for k in
                        ("left_of", "right_of", "above", "below",
                         "overlaps", "intersects")},
        })
    return hierarchy, components


# ═══════════════════════════════════════════════════════════════════════════
# Debug overlay (--debug-annotate) — VISUAL SANITY CHECK ONLY, not a stage
# ═══════════════════════════════════════════════════════════════════════════

def draw_debug_overlay(img, cands, segments, junctions, graph, out_path,
                       max_side=4000):
    """Render a single annotated JPG to visually confirm the three fixes:
       blue circles  = instruments
       orange squares= valves
       red rectangles= equipment (with tag label)
       red lines     = horizontal_pipe segments (used in pipeline construction)
       blue lines    = vertical_pipe segments (used in pipeline construction)
       yellow dots   = junction nodes
       green lines    = MOUNTED_ON edges (instrument → equipment)
    diagonal_pipe segments are NOT drawn — they are excluded from union-find
    pipeline construction, so showing them would misrepresent the pipe network.
    `cands` should be the CANONICAL (deduped) list so no duplicate symbol is
    drawn twice at the same location. Colours are BGR."""
    canvas = img.copy()

    # pipe segments actually used in pipeline construction:
    #   horizontal_pipe -> red, vertical_pipe -> blue, diagonal_pipe -> skipped
    for s in segments:
        if s["type"] == "horizontal_pipe":
            col = (0, 0, 255)        # red
        elif s["type"] == "vertical_pipe":
            col = (255, 0, 0)        # blue
        else:                        # diagonal_pipe / leader_line: not part of pipe net
            continue
        cv2.line(canvas, (int(s["x0"]), int(s["y0"])),
                 (int(s["x1"]), int(s["y1"])), col, 2)

    pos = {}   # candidate_id -> center (for MOUNTED_ON edges)
    for c in cands:
        bb = c.get("symbol_bbox") or {}
        if not bb:
            continue
        x1, y1, x2, y2 = int(bb["x1"]), int(bb["y1"]), int(bb["x2"]), int(bb["y2"])
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        pos[c["candidate_id"]] = (cx, cy)
        cat = c.get("symbol_category", "")
        if cat == "instrument":
            r = max(12, (x2 - x1) // 2)
            cv2.circle(canvas, (cx, cy), r, (255, 0, 0), 3)            # blue
        elif cat == "valve":
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 165, 255), 3)  # orange
        elif cat == "equipment":
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 0, 255), 4)    # red
            cv2.putText(canvas, c.get("tag_text", ""), (x1, max(14, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 0, 255), 3)

    # yellow junction dots
    for j in junctions:
        p = j["point"]
        cv2.circle(canvas, (int(p["x"]), int(p["y"])), 7, (0, 255, 255), -1)

    # green MOUNTED_ON edges
    n_mo = 0
    for e in graph["edges"]:
        if e.get("rel") != "MOUNTED_ON":
            continue
        a, b = pos.get(e["from"]), pos.get(e["to"])
        if a and b:
            cv2.line(canvas, a, b, (0, 200, 0), 3)
            n_mo += 1

    h, w = canvas.shape[:2]
    sc = max_side / max(h, w) if max(h, w) > max_side else 1.0
    if sc != 1.0:
        canvas = cv2.resize(canvas, (int(w * sc), int(h * sc)))
    cv2.imwrite(str(out_path), canvas)
    log.info("Debug overlay → %s  (%d symbols, %d MOUNTED_ON edges, scale=%.3f)",
             out_path, len(pos), n_mo, sc)


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def _eqp_count(hier):
    """(#instrument/valve records WITH equipment_parent, #instrument/valve total)."""
    inst = [h for h in hier if h.get("kind") in ("instrument", "valve")]
    have = [h for h in inst if h.get("equipment_parent")]
    return len(have), len(inst)


def run(assoc_path: str, img_path: str, out_dir: str,
        gemini_flow_fallback_on: bool = False, api_key: str = "",
        debug_annotate: bool = False,
        gemini_attach_on: bool = False, gemini_attach_dry_run: bool = False,
        gemini_attach_workers: int = 8):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    with open(assoc_path) as f:
        data = json.load(f)
    cands_all = data.get("enriched_candidates", [])
    log.info("Loaded %d enriched candidates from Step 5B", len(cands_all))

    # ── FIX 1: entity resolution BEFORE any graph/geometry construction ──
    # Phase 0: symbol-size pre-filter removes text-mention candidates (title
    # block, notes, tables) whose bbox has insufficient physical extent.
    # Phase 1: merge duplicate detections of the same tag into one canonical
    # node so a duplicate never becomes a separate graph/hierarchy node.
    # The graph, spatial, pipelines, flow and hierarchy are all built from this
    # filtered+deduped list. The byte-identical original list is preserved as
    # enriched_candidates pass-through (step7 joins any id, canonical or merged).
    log.info("=== FIX 1: entity resolution (symbol-size pre-filter + "
             "duplicate-detection merge) ===")
    cands, dup_remap, n_dup_merges, filter_stats = resolve_canonical_entities(
        cands_all, display_scale=1.0)
    log.info("Entity resolution: %d input → %d after symbol-size filter "
             "→ %d canonical (%d dup merges)",
             len(cands_all),
             len(cands_all) - filter_stats["n_filtered"],
             len(cands), n_dup_merges)

    img = cv2.imread(img_path)
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {img_path}")

    log.info("=== Re-detecting line segments (CV only) ===")
    lines = detect_pipes_and_lines(img)
    segments = build_line_segments(lines)

    log.info("=== Building pipelines + junctions (union-find) ===")
    equip_bboxes = [c.get("symbol_bbox", {}) for c in cands
                    if c.get("symbol_category") == "equipment" and c.get("symbol_bbox")]
    pipelines, junctions = build_pipelines_and_junctions(segments, equip_bboxes)

    # ─────────────────────────────────────────────────────
    # FUTURE: GEMINI PIPE VERIFICATION LAYER
    # Insert here after CV pipe fix is stable.
    # Ambiguous cases to send to Gemini:
    #   1. Pipe gaps where GAP_BRIDGE_PX closes them — confirm same line?
    #   2. Junction degree >= 3 — tee or crossing?
    #   3. Short segments near instrument bboxes — leader or pipe?
    #   4. Parallel segments < 50px apart — one pipe or two?
    # Do NOT add this until the CV detector produces clean segments.
    # Current blocker: step5b detect_pipes_and_lines producing noise.
    # ─────────────────────────────────────────────────────

    log.info("=== Spatial relations (bbox math) ===")
    spatial = compute_spatial(cands)

    log.info("=== Connectivity graph ===")
    graph = build_graph(cands, pipelines, junctions, segments, spatial)

    # ── Track C: signal-line (dashed) edges + control-loop hierarchy ─────
    log.info("=== Track C: signal-line detection (dashed path-probe) ===")
    signal_edges = detect_signal_edges(img, cands)
    _eid = len(graph["edges"])
    for se in signal_edges:
        graph["edges"].append({
            "edge_id": f"E-{_eid}", "from": se["a"], "to": se["b"],
            "rel": "SIGNAL_TO", "category": "signal", "directed": False,
            "confidence": 0.6, "evidence": se["evidence"],
        })
        _eid += 1
    control_loops, signal_clusters, node_to_loop = build_control_loops(signal_edges, cands)

    # ── Track B: flow direction (CV + propagation; NO Gemini) ────────────
    log.info("=== Track B: arrowhead + check-valve detection ===")
    symbol_bboxes = [c.get("symbol_bbox", {}) for c in cands if c.get("symbol_bbox")]
    arrowheads = detect_arrowheads(img, segments, exclude_bboxes=symbol_bboxes)
    check_valves = detect_check_valves(img, segments, exclude_bboxes=symbol_bboxes)
    log.info("=== Track B: direction seeding + propagation ===")
    equip_updown = compute_flow_direction(pipelines, junctions, segments,
                                          cands, arrowheads, check_valves,
                                          graph=graph)

    # ── GATED Gemini fallback: category-D pipelines only (explicit opt-in) ──
    if gemini_flow_fallback_on:
        if not api_key:
            raise RuntimeError("--gemini-flow-fallback requires --api-key / GEMINI_API_KEY")
        log.info("=== Track B: Gemini flow fallback (category D, gated) ===")
        gemini_flow_fallback(pipelines, junctions, graph, segments, cands,
                             img, api_key, out_dir)

    flow_dir = mark_directed_graph(graph, pipelines, junctions, cands, equip_updown)

    log.info("=== Hierarchy traversal ===")
    hierarchy, components = build_hierarchy(graph, spatial)

    # ── Phase 1: GATED Gemini instrument attachment (explicit opt-in) ────────
    # Find instruments/valves the CV graph couldn't bind to equipment, ask
    # Gemini what they connect to, add GEMINI_ATTACHED edges, then RE-RUN the
    # hierarchy so the new paths resolve equipment_parent.
    attach_report = None
    if gemini_attach_on or gemini_attach_dry_run:
        confirm = gemini_attach_on and not gemini_attach_dry_run
        if confirm and not api_key:
            raise RuntimeError("--gemini-attach requires --api-key / GEMINI_API_KEY")
        log.info("=== Phase 1: Gemini instrument attachment (gated) ===")
        eqp_before_have, eqp_total = _eqp_count(hierarchy)
        before_has = {h["node_id"]: bool(h.get("equipment_parent")) for h in hierarchy}
        attach_report = gemini_instrument_attach(
            hierarchy, graph, cands, pipelines, segments, img,
            api_key, out_dir, confirm=confirm,
            n_workers=gemini_attach_workers)
        attach_report["equip_parent_before"] = f"{eqp_before_have}/{eqp_total}"

        if confirm and attach_report.get("edges_added"):
            hierarchy, components = build_hierarchy(graph, spatial)
            eqp_after_have, _ = _eqp_count(hierarchy)
            attach_report["equip_parent_after"] = f"{eqp_after_have}/{eqp_total}"
            gained = [h for h in hierarchy
                      if h.get("equipment_parent") and not before_has.get(h["node_id"])]
            attach_report["n_gained_equipment_parent"] = len(gained)

            # ── Phase 1 report (the 7 data points) ──
            print("\n=== Phase 1: Gemini instrument-attach REPORT ===")
            print(f"  (1) unresolved+isolated sent : {attach_report['n_sent']} "
                  f"({attach_report['n_unresolved']} unresolved + "
                  f"{attach_report['n_isolated']} isolated)")
            print(f"  (2) Gemini calls (clusters)  : {attach_report['n_clusters']}")
            abc = attach_report["attachments_by_conf"]
            print(f"  (3) attachments returned     : {attach_report['attachments_returned']} "
                  f"(high={abc['high']} medium={abc['medium']} "
                  f"low={abc['low']} unknown={abc['unknown']})")
            print(f"  (4) new GEMINI_ATTACHED edges: {attach_report['edges_added']}")
            print(f"  (5) equipment_parent BEFORE  : {attach_report['equip_parent_before']}")
            print(f"      equipment_parent AFTER   : {attach_report['equip_parent_after']}"
                  f"   <-- THE KEY NUMBER")
            print(f"  (7) Gemini refs to equipment NOT in graph: "
                  f"{len(attach_report['unknown_equipment_refs'])} "
                  f"{attach_report['unknown_equipment_refs'][:10]}")
            print(f"  (6) up to 3 instruments that gained equipment_parent from Gemini:")
            for h in gained[:3]:
                print(json.dumps(h, indent=2))
        elif confirm:
            attach_report["equip_parent_after"] = attach_report["equip_parent_before"]
            attach_report["n_gained_equipment_parent"] = 0
            print("\n=== Phase 1: Gemini instrument-attach REPORT ===")
            print("  No high/medium attachments returned → 0 edges added; "
                  "hierarchy unchanged.")
            print(f"  equipment_parent: {attach_report['equip_parent_before']} (unchanged)")

    annotate_directed_hierarchy(hierarchy, graph, pipelines)

    n_connected = sum(1 for h in hierarchy if not h["is_isolated"])
    n_isolated = len(hierarchy) - n_connected

    payload = {
        "version": "v2",
        "total_candidates": data.get("total_candidates"),
        "lines_detected": data.get("lines_detected"),
        "rel_summary": data.get("rel_summary"),
        # ---- byte-identical pass-through (5c/5d untouched) ----
        "associations": data.get("associations", []),
        "enriched_candidates": cands_all,
        # FIX 1: canonical (filtered + deduped) candidates used to build the graph
        "canonical_candidates": cands,
        "entity_resolution": {
            "n_input": len(cands_all),
            "n_after_symbol_filter": len(cands_all) - filter_stats["n_filtered"],
            "n_canonical": len(cands),
            "n_symbol_filter_removed": filter_stats["n_filtered"],
            "symbol_filter_zone_counts": filter_stats["zone_counts"],
            "symbol_filter_removed": filter_stats["filtered_records"],
            "n_merges": n_dup_merges,
            "dup_max_dist_px": DUP_MAX_DIST_PX,
            "min_symbol_height_px": MIN_SYMBOL_HEIGHT_PX,
            "min_symbol_width_px": MIN_SYMBOL_WIDTH_PX,
        },
        # ---- NEW v2 keys ----
        "line_segments": segments,
        "pipelines": pipelines,
        "junctions": junctions,
        "graph": graph,
        "hierarchy": hierarchy,
        "algorithms": {
            "line_persistence": "reuse step5b detect_pipes_and_lines; medial-axis endpoints",
            "pipeline_construction": f"union-find over pipe segments sharing snapped endpoints (tol={SNAP_TOL_PX}px, min_len={MIN_PIPE_LEN}px)",
            "junction_detection": f"snapped endpoint degree >= {JUNCTION_MIN_DEG}",
            "spatial_relations": f"bbox center delta + intersection within {SPATIAL_WINDOW_PX}px window",
            "graph_construction": f"symbol↔pipeline nearest within {SYMBOL_PIPE_RADIUS}px; 5B containment + nearby reused",
            "hierarchy": "undirected connected components; root=max(rank,degree,area); BFS tree -> parent/child/ancestor/descendant/siblings/root-to-leaf",
            "flow_direction": (f"Track B (undirected→directed): check-valve glyphs (▷| seat-bar gated, "
                               f"min_area={CHECK_VALVE_MIN_AREA}, seat<= {CHECK_VALVE_SEAT_BAR_DIST}px) + arrowheads "
                               f"(area {ARROWHEAD_MIN_AREA}-{ARROWHEAD_MAX_AREA}px², solidity>={ARROWHEAD_MIN_SOLIDITY}, "
                               f"<={ARROWHEAD_PIPE_PROXIMITY}px from pipe) -> seed (check-valve wins conflicts) -> "
                               "BFS propagate through degree-2 junctions (stop at degree>2 SPLIT/MERGE and equipment) -> "
                               "equipment boundary conventions (compressor L-in/R-out, vessel bottom-out) -> "
                               "dead-leg topology pass (single connected end => flow toward it; "
                               "category-A noise stubs and category-B signal lines excluded). "
                               "DETERMINISTIC layer = 51/389 directed (45 CV/propagation/convention + 6 topology_dead_end); "
                               "drawing lacks dense machine-readable flow. GATED Gemini fallback "
                               "(--gemini-flow-fallback) resolves category-D pipelines (candidates + both ends "
                               "connected) via ~6 spatial-cluster vision calls -> evidence='gemini_vision' "
                               f"(this file: {sum(1 for p in pipelines if p.get('flow',{}).get('evidence')=='gemini_vision')} resolved)."),
            "signal_hierarchy": (f"Track C (control loops): dashed signal lines detected by proximity-gated "
                               f"strict path-probing (pairs within {SIGNAL_MAX_PAIR_DIST}px; a path is a signal "
                               f"edge only if fully on a dashed line — coverage {SIGNAL_COV_MIN}-{SIGNAL_COV_MAX}, "
                               f">={SIGNAL_MIN_TRANS} ink/gap transitions, none solid). Edges added as "
                               f"category='signal' rel='SIGNAL_TO'; they fix is_isolated (Option A) but are EXCLUDED "
                               f"from process rooting BFS (Decision A2). control_loops[] = signal connected-components "
                               f"capped at {SIGNAL_LOOP_MAX_SIZE} members. "
                               f"KNOWN GAPS (deferred, out of scope): (1) crossing dashes in dense instrument regions "
                               f"over-merge into one component -> emitted as 'unresolved_signal_cluster' not a loop; "
                               f"(2) multi-bend signal routing (e.g. FIC-207's flow-transmitter inputs) is NOT "
                               f"recoverable by 2-segment probing — needs a dedicated multi-bend tracer; "
                               f"(3) mechanical shaft hierarchy K-V-201->GEAR->KM-V-201 needs manual annotation."),
        },
        "gemini_attach": attach_report,
        "arrowheads": arrowheads,
        "check_valves": check_valves,
        "control_loops": control_loops,
        "signal_clusters": signal_clusters,
        "flow_summary": flow_dir,
        "stats": {
            "n_segments": len(segments),
            "n_pipelines": len(pipelines),
            "n_junctions": len(junctions),
            "n_graph_nodes": len(graph["nodes"]),
            "n_graph_edges": len(graph["edges"]),
            "n_components": len(components),
            "n_candidates_connected": n_connected,
            "n_candidates_isolated": n_isolated,
            "n_arrowheads": len(arrowheads),
            "n_signal_edges": len(signal_edges),
            "n_control_loops": len(control_loops),
            "n_signal_clusters": len(signal_clusters),
            "flow_sources": flow_dir,
        },
    }

    # Primary output: hierarchy built from FULL extraction.
    out_path = str(out / "step5b2_hierarchy_full.json")
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    log.info("✓ step5b2_hierarchy_full.json → %s", out_path)

    # Backward-compatible alias — downstream stages that still look for the
    # plain name (and any older tooling) keep working unchanged.
    alias_path = str(out / "step5b2_hierarchy.json")
    with open(alias_path, "w") as f:
        json.dump(payload, f, indent=2)
    log.info("✓ step5b2_hierarchy.json (alias) → %s", alias_path)

    # ── Debug overlay (visual sanity check; uses CANONICAL deduped candidates) ──
    if debug_annotate:
        draw_debug_overlay(img, cands, segments, junctions, graph,
                           out / "step5b2_debug_overlay.jpg")

    # ── Console report (the 4 data points) ──
    print("\n=== Step 5B2 Phase 1 Complete ===")
    print(f"  --- Symbol-size pre-filter ---")
    print(f"  input candidates        : {len(cands_all)}")
    print(f"  non-symbol filtered out : {filter_stats['n_filtered']}  "
          f"(zone breakdown: {filter_stats['zone_counts']})")
    if filter_stats["filtered_records"]:
        for r in filter_stats["filtered_records"][:10]:
            print(f"    filtered: {r['tag']:20s}  ({r['cx']:5.0f},{r['cy']:5.0f})"
                  f"  [{r['zone']}]  {r['reason']}")
        if filter_stats["n_filtered"] > 10:
            print(f"    ... and {filter_stats['n_filtered'] - 10} more (see entity_resolution.symbol_filter_removed)")
    print(f"  after filter            : {len(cands_all) - filter_stats['n_filtered']}")
    print(f"  after dup merge         : {len(cands)}")
    print(f"  --- Graph & hierarchy ---")
    print(f"  line segments persisted : {len(segments)}")
    print(f"  (a) pipeline entities   : {len(pipelines)}")
    print(f"  (b) junction nodes      : {len(junctions)}")
    print(f"      graph nodes/edges   : {len(graph['nodes'])} / {len(graph['edges'])}")
    print(f"      components          : {len(components)}")
    print(f"  (d) connected / isolated: {n_connected} / {n_isolated}  "
          f"(of {len(hierarchy)} candidates)")
    print(f"\n  --- Track B: flow direction ---")
    print(f"  arrowheads / check-valves: {len(arrowheads)} / {len(check_valves)}")
    print(f"  pipelines by source     : check_valve={flow_dir.get('check_valve',0)} "
          f"arrowhead={flow_dir.get('arrowhead',0)} "
          f"propagated={flow_dir.get('propagated',0)} "
          f"equip_convention={flow_dir.get('equipment_convention',0)} "
          f"topology_dead_end={flow_dir.get('topology_dead_end',0)} "
          f"gemini_vision={flow_dir.get('gemini_vision',0)} "
          f"conflict={flow_dir.get('seed_conflict',0)}")
    _gemini_note = ("Gemini fallback RAN on category D"
                    if gemini_flow_fallback_on else "Gemini not called")
    print(f"  pipelines still UNKNOWN  : {flow_dir.get('unknown',0)} / {len(pipelines)}  "
          f"<-- POINT 5 GATE ({_gemini_note})")
    return payload


def _resolve_associations_path(requested: str) -> str:
    """Resolve the step5b associations input path.

    Hierarchy must always be built from FULL_DRAWING extraction output, so the
    default input is ``step5b_associations_full.json``. If that file does not
    exist, fall back to ``step5b_associations.json`` (which may be cloud-filtered)
    and emit a loud warning — an incomplete hierarchy is the symptom.
    """
    if os.path.exists(requested):
        return requested

    # If the caller asked for the *_full file (default) and it's missing,
    # fall back to the plain associations file in the same directory.
    if requested.endswith("step5b_associations_full.json"):
        fallback = requested.replace("step5b_associations_full.json",
                                     "step5b_associations.json")
        if os.path.exists(fallback):
            print("WARNING: step5b_associations_full.json not found.")
            print("Falling back to step5b_associations.json.")
            print("Hierarchy may be incomplete — input may be cloud-filtered.")
            print("Run step5b with --force-full-drawing step5a output to fix.")
            return fallback

    # Nothing matched — return the requested path so the open() error is explicit.
    return requested


def main():
    ap = argparse.ArgumentParser(description="Step 5B2: Hierarchy & Graph (Phase 1)")
    ap.add_argument("--associations", default="output/step5b_associations_full.json",
                    help="step5b associations input (default: FULL extraction; "
                         "falls back to step5b_associations.json with a warning)")
    ap.add_argument("--image")
    ap.add_argument("--context")
    ap.add_argument("--out", default="output")
    ap.add_argument("--gemini-flow-fallback", action="store_true",
                    help="GATED: resolve category-D pipeline flow direction via Gemini (~6 calls)")
    ap.add_argument("--gemini-attach", action="store_true",
                    help="GATED Phase 1: attach unresolved/isolated instruments to "
                         "equipment via Gemini vision (one call per spatial cluster)")
    ap.add_argument("--gemini-attach-dry-run", action="store_true",
                    help="Phase 1 cost gate: print cluster count + estimated cost + "
                         "instruments to send, then STOP without calling Gemini")
    ap.add_argument("--gemini-attach-workers", type=int, default=8,
                    help="Parallel workers for Gemini instrument attachment (default 8)")
    ap.add_argument("--api-key", help="Gemini API key (or GEMINI_API_KEY env)")
    ap.add_argument("--debug-annotate", action="store_true",
                    help="Write output/step5b2_debug_overlay.jpg (visual sanity check: "
                         "instruments/valves/equipment, pipes, junctions, MOUNTED_ON edges)")
    args = ap.parse_args()

    img_path = args.image
    if not img_path and args.context:
        with open(args.context) as f:
            ctx = json.load(f)
        img_path = ctx.get("raster_path") or ctx.get("input_file")
    if not img_path:
        img_path = "input_drawing.jpg"

    api_key = (args.api_key or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GEMINI_KEY") or "")

    assoc_path = _resolve_associations_path(args.associations)

    run(assoc_path, img_path, args.out,
        gemini_flow_fallback_on=args.gemini_flow_fallback, api_key=api_key,
        debug_annotate=args.debug_annotate,
        gemini_attach_on=args.gemini_attach,
        gemini_attach_dry_run=args.gemini_attach_dry_run,
        gemini_attach_workers=args.gemini_attach_workers)


if __name__ == "__main__":
    main()
