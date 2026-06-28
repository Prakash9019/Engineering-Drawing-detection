#!/usr/bin/env python3
"""
validate_full_vs_cloud.py — FULL→FILTER vs DIRECT_CLOUD extraction comparison
==============================================================================

PURPOSE
    Decide whether running step5a in FULL_DRAWING mode and then filtering the
    result to cloud scope as a *post-processing* step (Mode B, "FULL→FILTER")
    is a safe replacement for running step5a directly in CLOUD_FILTER mode
    (Mode A, "DIRECT_CLOUD").

    The risk being tested: does direct cloud extraction catch tags that
    full-drawing extraction misses (e.g. small symbols only resolved when the
    SAHI tiling is concentrated on the cloud region)? If so, the simplification
    is NOT safe and both modes must be kept.

WHAT IT DOES
    1. Filters the FULL candidates to cloud scope using the SAME point-in-polygon
       logic step5a uses for CLOUD_FILTER mode (symbol_bbox centre inside any
       cloud polygon). Call this set FULL_FILTERED.
    2. Compares FULL_FILTERED vs CLOUD_DIRECT by normalised tag identity:
         - critical    : in CLOUD_DIRECT but NOT in FULL_FILTERED  (DIRECT found, FULL missed)
         - extra        : in FULL_FILTERED but NOT in CLOUD_DIRECT  (FULL found, DIRECT missed)
         - agreed       : in both
    3. Reports tag text / bbox / confidence / symbol_category for every critical miss.
    4. (optional --register) computes recall & precision for each mode vs ground truth.
    5. Flags prefix_resolution_discrepancies: spatially co-located detections where
       the two modes resolve different tag text (e.g. BV-001 vs 13M2-BV-001).

OUTPUTS (in --out)
    validation_summary.json    — machine-readable metrics
    validation_report.txt      — human-readable summary
    critical_misses.json       — tags in CLOUD_DIRECT not in FULL_FILTERED
    prefix_discrepancies.json   — tag-text disagreements between the two modes

ACCEPTANCE THRESHOLD (the simplification gate)
    Recall                          >= 99%   (each mode, when --register provided)
    Critical tags missing           == 0     (CLOUD found, FULL_FILTERED missed)
    Prefix resolution discrepancies == 0

    If ALL THREE thresholds are met across the validation set (target 50-100
    drawings), the FULL→FILTER simplification is APPROVED and the DIRECT_CLOUD
    path may be removed. If ANY threshold is not met on ANY drawing, keep both
    extraction modes.

NOTE
    This script is read-only over the pipeline outputs — it never re-runs
    extraction and never mutates step5a/step5b/step5c/step5d output. It is safe
    to run as a smoke test on a single drawing even before a full validation set
    exists; with one drawing it simply reports that drawing's numbers.

USAGE
    python3 stages/validate_full_vs_cloud.py \
        --full-candidates  output/step5a_candidates_full.json \
        --cloud-candidates output/step5a_candidates_cloud.json \
        --cloud-regions    output/approved_clouds.json \
        [--register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx] \
        --out output/
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

# Acceptance gate (documented above; re-stated here for programmatic checks)
RECALL_THRESHOLD = 0.99
PREFIX_MATCH_DIST_PX = 150   # two detections within this centre distance are "the same physical tag"


# ─────────────────────────────────────────────────────────────────────────────
# Tag normalisation — identical semantics to eval_coverage.norm()
# ─────────────────────────────────────────────────────────────────────────────
def norm(t: str) -> str:
    """Uppercase, unify dashes/quotes, strip separators, collapse inch marker."""
    s = (t or "").upper()
    for d in ("—", "–", "―", "−"):
        s = s.replace(d, "-")
    s = re.sub(r'[\s\-"“”‘’`\']+', "", s)
    s = s.replace("IN", "")
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Cloud scope filter — SAME logic step5a uses for CLOUD_FILTER mode
# (symbol_bbox centre inside any cloud polygon; cv2.pointPolygonTest).
# ─────────────────────────────────────────────────────────────────────────────
def load_cloud_polygons(path: str):
    with open(path) as f:
        data = json.load(f)
    clouds = data.get("clouds") or data.get("outer_clouds") or []
    contours = []
    for c in clouds:
        poly = c.get("polygon")
        if poly and len(poly) >= 3:
            contours.append(np.array(poly, dtype=np.int32).reshape(-1, 1, 2))
    return contours, clouds


def bbox_center(cand: dict):
    b = cand.get("symbol_bbox") or cand.get("tag_bbox") or {}
    if not b:
        return None
    return ((b.get("x1", 0) + b.get("x2", 0)) / 2.0,
            (b.get("y1", 0) + b.get("y2", 0)) / 2.0)


def point_in_any_cloud(px, py, contours) -> bool:
    if cv2 is None:
        # Pure-python ray-casting fallback so the script still runs without cv2.
        for cnt in contours:
            pts = cnt.reshape(-1, 2)
            inside = False
            n = len(pts)
            j = n - 1
            for i in range(n):
                xi, yi = pts[i]
                xj, yj = pts[j]
                if ((yi > py) != (yj > py)) and \
                        (px < (xj - xi) * (py - yi) / (yj - yi + 1e-9) + xi):
                    inside = not inside
                j = i
            if inside:
                return True
        return False
    pt = (float(px), float(py))
    return any(cv2.pointPolygonTest(cnt, pt, False) >= 0 for cnt in contours)


def filter_to_cloud_scope(cands, contours):
    kept = []
    for c in cands:
        ctr = bbox_center(c)
        if ctr and point_in_any_cloud(ctr[0], ctr[1], contours):
            kept.append(c)
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# Register / ground-truth — same matching semantics as eval_coverage
# ─────────────────────────────────────────────────────────────────────────────
def load_ground_truth(xlsx: str):
    import openpyxl
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    ws = wb.worksheets[0]
    tags = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and len(row) > 2 and row[2]:
            tags.append(str(row[2]).strip())
    return tags


def _gt_match(gn: str, cand_norm: dict):
    """Exact normalised match, then bounded-substring match (eval_coverage rule)."""
    if gn in cand_norm:
        return True
    for cn in cand_norm:
        if gn and (gn in cn or cn in gn) and abs(len(gn) - len(cn)) <= 2:
            return True
    return False


def recall_precision(cands, gt_tags):
    gt_norm = {norm(t): t for t in gt_tags}
    cand_norm = {}
    for c in cands:
        cand_norm.setdefault(norm(c.get("tag_text", "")), []).append(c)

    found = [g for gn, g in gt_norm.items() if _gt_match(gn, cand_norm)]
    recall = len(found) / len(gt_tags) if gt_tags else 0.0

    # precision = fraction of detections that map onto some ground-truth tag
    gt_norm_set = set(gt_norm)
    correct = 0
    for c in cands:
        cn = norm(c.get("tag_text", ""))
        if cn in gt_norm_set or any(
                cn and (cn in g or g in cn) and abs(len(cn) - len(g)) <= 2
                for g in gt_norm_set):
            correct += 1
    precision = correct / len(cands) if cands else 0.0
    missing = sorted(set(gt_tags) - set(found))
    return {
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "found_count": len(found),
        "gt_count": len(gt_tags),
        "missing": missing,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Prefix-resolution discrepancies — spatially co-located, differently-resolved
# ─────────────────────────────────────────────────────────────────────────────
def find_prefix_discrepancies(full_filtered, cloud_direct, dist_px=PREFIX_MATCH_DIST_PX):
    out = []
    cloud_centers = [(c, bbox_center(c)) for c in cloud_direct]
    cloud_centers = [(c, ctr) for c, ctr in cloud_centers if ctr]
    for f in full_filtered:
        fc = bbox_center(f)
        if not fc:
            continue
        # nearest cloud detection
        best, best_d = None, dist_px + 1
        for c, cc in cloud_centers:
            d = abs(fc[0] - cc[0]) + abs(fc[1] - cc[1])
            if d < best_d:
                best, best_d = c, d
        if best is None or best_d > dist_px:
            continue
        ft, ct = (f.get("tag_text") or "").strip(), (best.get("tag_text") or "").strip()
        if ft.upper() == ct.upper():
            continue  # agree
        if norm(ft) == norm(ct):
            continue  # only formatting differs after normalisation — not a real disagreement
        # genuine disagreement
        a, b = norm(ft), norm(ct)
        kind = "prefix" if (a and b and (a.endswith(b) or b.endswith(a))) else "mismatch"
        out.append({
            "type": kind,
            "full_tag": ft,
            "cloud_tag": ct,
            "distance_px": round(best_d, 1),
            "full_bbox": f.get("symbol_bbox") or f.get("tag_bbox"),
            "cloud_bbox": best.get("symbol_bbox") or best.get("tag_bbox"),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
def cand_summary(c):
    return {
        "tag_text": c.get("tag_text"),
        "symbol_category": c.get("symbol_category"),
        "bbox": c.get("symbol_bbox") or c.get("tag_bbox"),
        "vision_confidence": c.get("vision_confidence"),
        "ocr_confidence": c.get("ocr_confidence"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full-candidates", required=True,
                    help="step5a_candidates_full.json (FULL_DRAWING extraction)")
    ap.add_argument("--cloud-candidates", required=True,
                    help="step5a_candidates_cloud.json (DIRECT CLOUD_FILTER extraction)")
    ap.add_argument("--cloud-regions", required=True,
                    help="approved_clouds.json or outer_clouds_v2.json")
    ap.add_argument("--register", help="ground-truth register .xlsx (optional)")
    ap.add_argument("--out", default="output", help="output directory for the report")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    full = json.load(open(args.full_candidates)).get("candidates", [])
    cloud = json.load(open(args.cloud_candidates)).get("candidates", [])
    contours, raw_clouds = load_cloud_polygons(args.cloud_regions)
    if not contours:
        print(f"ERROR: no cloud polygons in {args.cloud_regions}", file=sys.stderr)
        sys.exit(2)

    # 1. FULL → FILTER
    full_filtered = filter_to_cloud_scope(full, contours)

    # 2. set comparison by normalised tag
    ff_norm = {}
    for c in full_filtered:
        ff_norm.setdefault(norm(c.get("tag_text", "")), []).append(c)
    cd_norm = {}
    for c in cloud:
        cd_norm.setdefault(norm(c.get("tag_text", "")), []).append(c)

    critical_keys = [k for k in cd_norm if k and k not in ff_norm]   # CLOUD found, FULL missed
    extra_keys = [k for k in ff_norm if k and k not in cd_norm]       # FULL found, CLOUD missed
    agreed_keys = [k for k in cd_norm if k and k in ff_norm]

    # 3. critical detail
    critical_misses = []
    for k in critical_keys:
        for c in cd_norm[k]:
            critical_misses.append(cand_summary(c))

    extra_coverage = []
    for k in extra_keys:
        for c in ff_norm[k]:
            extra_coverage.append(cand_summary(c))

    # 4. register metrics
    register_metrics = None
    if args.register:
        gt = load_ground_truth(args.register)
        register_metrics = {
            "cloud_direct": recall_precision(cloud, gt),
            "full_filtered": recall_precision(full_filtered, gt),
            "full_unfiltered": recall_precision(full, gt),
            "missing_in_both": sorted(
                set(recall_precision(cloud, gt)["missing"])
                & set(recall_precision(full_filtered, gt)["missing"])),
        }

    # 5. prefix-resolution discrepancies
    prefix_disc = find_prefix_discrepancies(full_filtered, cloud)

    # ── Gate evaluation ──
    gate = {
        "critical_misses": len(critical_misses),
        "prefix_discrepancies": len(prefix_disc),
        "recall_ok": None,
        "passes": None,
    }
    if register_metrics:
        rc = register_metrics["cloud_direct"]["recall"]
        rf = register_metrics["full_filtered"]["recall"]
        gate["recall_ok"] = bool(rc >= RECALL_THRESHOLD and rf >= RECALL_THRESHOLD)
    gate["passes"] = (gate["critical_misses"] == 0
                      and gate["prefix_discrepancies"] == 0
                      and (gate["recall_ok"] is not False))

    summary = {
        "inputs": {
            "full_candidates": args.full_candidates,
            "cloud_candidates": args.cloud_candidates,
            "cloud_regions": args.cloud_regions,
            "register": args.register,
            "n_cloud_polygons": len(contours),
        },
        "counts": {
            "full_total": len(full),
            "full_filtered": len(full_filtered),
            "cloud_direct": len(cloud),
            "agreed": len(agreed_keys),
            "critical_misses_unique": len(critical_keys),
            "extra_coverage_unique": len(extra_keys),
        },
        "register_metrics": register_metrics,
        "gate": gate,
        "acceptance_threshold": {
            "recall_min": RECALL_THRESHOLD,
            "critical_misses_max": 0,
            "prefix_discrepancies_max": 0,
        },
    }

    # ── Write outputs ──
    with open(out / "validation_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    with open(out / "critical_misses.json", "w") as f:
        json.dump({"critical_misses": critical_misses,
                   "extra_coverage": extra_coverage}, f, indent=2)
    with open(out / "prefix_discrepancies.json", "w") as f:
        json.dump({"prefix_discrepancies": prefix_disc}, f, indent=2)

    report = _render_report(summary, critical_misses, prefix_disc)
    with open(out / "validation_report.txt", "w") as f:
        f.write(report)

    print(report)
    print(f"\nWrote: {out}/validation_summary.json, critical_misses.json, "
          f"prefix_discrepancies.json, validation_report.txt")


def _render_report(s, critical, prefix_disc) -> str:
    L = []
    L.append("=" * 70)
    L.append("FULL→FILTER  vs  DIRECT_CLOUD  —  extraction validation")
    L.append("=" * 70)
    c = s["counts"]
    L.append(f"  FULL total              : {c['full_total']}")
    L.append(f"  FULL_FILTERED (→cloud)  : {c['full_filtered']}")
    L.append(f"  CLOUD_DIRECT            : {c['cloud_direct']}")
    L.append(f"  Agreed (both)           : {c['agreed']}")
    L.append(f"  CRITICAL (cloud-only)   : {c['critical_misses_unique']}   "
             f"<-- must be 0 for simplification")
    L.append(f"  Extra coverage (full)   : {c['extra_coverage_unique']}")
    rm = s.get("register_metrics")
    if rm:
        L.append("")
        L.append("  -- vs register (ground truth) --")
        for mode in ("cloud_direct", "full_filtered", "full_unfiltered"):
            m = rm[mode]
            L.append(f"    {mode:<16} recall={m['recall']*100:5.1f}%  "
                     f"precision={m['precision']*100:5.1f}%  "
                     f"({m['found_count']}/{m['gt_count']})")
        if rm["missing_in_both"]:
            L.append(f"    missing in BOTH modes : {', '.join(rm['missing_in_both'])}")
    L.append("")
    L.append("  -- ACCEPTANCE GATE --")
    g = s["gate"]
    L.append(f"    critical misses        : {g['critical_misses']}  (max 0)")
    L.append(f"    prefix discrepancies   : {g['prefix_discrepancies']}  (max 0)")
    L.append(f"    recall >= 99% both     : {g['recall_ok']}")
    L.append(f"    >>> GATE PASSES        : {g['passes']}")
    if critical:
        L.append("")
        L.append("  CRITICAL MISSES (CLOUD found, FULL_FILTERED missed):")
        for m in critical[:50]:
            L.append(f"    {str(m['tag_text']):<18} {m['symbol_category']:<12} "
                     f"vc={m['vision_confidence']} bbox={m['bbox']}")
    if prefix_disc:
        L.append("")
        L.append("  PREFIX / RESOLUTION DISCREPANCIES:")
        for d in prefix_disc[:50]:
            L.append(f"    [{d['type']}] full='{d['full_tag']}' "
                     f"cloud='{d['cloud_tag']}' d={d['distance_px']}px")
    L.append("=" * 70)
    return "\n".join(L)


if __name__ == "__main__":
    main()
