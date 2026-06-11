"""
Cloud-Miss Diagnostic Mode — Evidence Gathering (no architecture change)
========================================================================
Purpose
-------
Prove WHERE information is lost for every revision cloud the deterministic
pipeline fails to detect. For each *missed* cloud we save the raw evidence and
auto-classify the failure into one of four mutually exclusive buckets:

    A = SIGNAL LOSS        boundary ink is gone after binarization
    B = FRAGMENTATION      ink exists, broken into pieces, no single big piece,
                           and naive closing does NOT reconstruct a valid cloud
    C = VALIDATION REJECT  a substantial contour exists but a gate rejects it
    D = MERGE FAILURE      multiple pieces exist that, once closed/unioned,
                           DO form a valid cloud — only the IoU merge misses it

This module changes NOTHING in the detection architecture. It re-uses the exact
binarization, contour extraction, acceptance logic, and validation gate from
`stage1_cloud.py` so the evidence reflects the real system, not a proxy.

Defining a "miss"
-----------------
A diagnostic needs a reference set of where clouds *should* be:
  1. --truth <file.json>   manual ground truth (authoritative).
                           Format: [{"box":[x0,y0,x1,y1]}, ...]  (full-res px)
  2. otherwise, Gemini Instance Oracle enumerates all revision clouds.
     (This also serves as the evaluation of the proposed Gemini-as-oracle role.)

A reference cloud is MISSED if no detected polygon matches it
(IoU > MATCH_IOU or detected-centroid inside the reference box).

IMPORTANT CAVEAT (printed in the report):
  - If the reference comes from Gemini, it is a *candidate* set, not truth.
    Saved crops let a human confirm each one is a real cloud.
  - The A/B/C/D label is a heuristic. The saved artifacts are the real evidence;
    the label is a hint that aggregates into statistics.

Usage
-----
    # Gemini oracle as reference (needs GOOGLE_API_KEY)
    python pipeline/diagnose_clouds.py drawing.jpg

    # Manual ground truth as reference (no API needed)
    python pipeline/diagnose_clouds.py drawing.jpg --truth clouds_truth.json

    # Choose output dir
    python pipeline/diagnose_clouds.py drawing.jpg --out debug_diag
"""
import os, sys, json, argparse, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from core.geometry import iou, contour_to_polygon
from core.json_parser import parse_json
from settings import CLOUD_POLY_EPSILON

# Re-use the REAL system internals so evidence reflects the live pipeline.
from pipeline.stage1_cloud import (
    detect_clouds,
    _cloud_likelihood_score,
    _validate_cloud_shape,
    _in_exclusion_zone,
    SCALLOP_THRESHOLD, SCALLOP_MIN_AREA, SCALLOP_MAX_AREA_FRAC,
    SCALLOP_MAX_SOLIDITY, CLS_ACCEPT, CLS_REVIEW, VALIDATE_MIN_VERTICES,
)

log = logging.getLogger(__name__)

# ── Diagnostic parameters (NOT detection thresholds — only classification) ──
MATCH_IOU        = 0.30   # reference matched to a detection if IoU exceeds this
REGION_PAD       = 60     # px padding around a reference box when cropping
MIN_FRAG_LEN     = 50     # px arc-length floor for a "real" fragment (vs speckle)
INK_MIN_FRAC     = 0.004  # below this foreground fraction → treat region as empty
MERGE_CLOSE_K    = 15     # kernel for the merge-reconstruction probe (close gaps)
MERGE_CLOSE_IT   = 3      # iterations for the merge probe


# ═══════════════════════════════════════════════════════════════════
# Reference set: manual truth OR Gemini instance oracle
# ═══════════════════════════════════════════════════════════════════
def _load_truth(path: str) -> List[List[int]]:
    with open(path) as f:
        data = json.load(f)
    boxes = []
    items = data if isinstance(data, list) else data.get('clouds', [])
    for it in items:
        if 'box' in it and len(it['box']) == 4:
            boxes.append([int(v) for v in it['box']])
        elif 'polygon' in it:
            p = np.array(it['polygon']).reshape(-1, 2)
            x0, y0 = p[:, 0].min(), p[:, 1].min()
            x1, y1 = p[:, 0].max(), p[:, 1].max()
            boxes.append([int(x0), int(y0), int(x1), int(y1)])
    return boxes


def _gemini_instance_oracle(image: np.ndarray, gemini) -> List[List[int]]:
    """
    Gemini as INSTANCE ORACLE: enumerate ALL revision clouds + count.
    Returns rough full-res boxes. NOT geometry — only 'how many / roughly where'.
    """
    H, W = image.shape[:2]
    scale = min(1.0, 4000 / max(H, W))
    small = cv2.resize(image, (int(W * scale), int(H * scale)))
    sh, sw = small.shape[:2]

    prompt = f"""You are an instance oracle for P&ID revision clouds.
A revision cloud has a scalloped / bumpy / cloud-like outline marking a revised area.
Count EVERY distinct revision cloud on this drawing and give each a rough box.
Do NOT include title blocks, notes, legends, tables, borders, equipment, or text.
One cloud = one entry, even if its outline is broken by pipes/symbols crossing it.
Return ONLY JSON: [{{"id":1,"box":[x_min,y_min,x_max,y_max]}}, ...]
Image is {sw}x{sh}px. If there are none: []"""

    log.info(f"  [oracle] Gemini instance enumeration on {sw}x{sh}...")
    raw = gemini.ask(prompt, small)
    items = parse_json(raw)
    if not isinstance(items, list):
        log.warning("  [oracle] non-list response; no reference clouds")
        return []

    boxes = []
    for it in items:
        if not isinstance(it, dict) or 'box' not in it:
            continue
        box = it['box']
        if len(box) != 4:
            continue
        try:
            x0, y0, x1, y1 = [float(v) for v in box]
        except (TypeError, ValueError):
            continue
        # handle 0..1000 normalized coords (Gemini convention)
        if all(0 <= v <= 1000 for v in (x0, y0, x1, y1)) and sw > 1000:
            x0, y0, x1, y1 = x0 * sw / 1000, y0 * sh / 1000, x1 * sw / 1000, y1 * sh / 1000
        # back to full-res
        bx = [int(x0 / scale), int(y0 / scale), int(x1 / scale), int(y1 / scale)]
        bx = [max(0, bx[0]), max(0, bx[1]), min(W, bx[2]), min(H, bx[3])]
        if bx[2] - bx[0] >= 40 and bx[3] - bx[1] >= 40:
            boxes.append(bx)
    log.info(f"  [oracle] {len(boxes)} reference clouds")
    return boxes


# ═══════════════════════════════════════════════════════════════════
# Acceptance predicate — replicate the live Stage-1 decision per contour
# ═══════════════════════════════════════════════════════════════════
def _evaluate_contour(cnt: np.ndarray, shape: Tuple[int, int]) -> dict:
    """
    Run a single contour through the EXACT Stage-1 acceptance + validation logic.
    Returns a verdict dict explaining why it would be accepted / rejected.
    """
    H, W = shape[:2]
    area = cv2.contourArea(cnt)
    peri = cv2.arcLength(cnt, True)
    hull = cv2.convexHull(cnt)
    hull_peri = cv2.arcLength(hull, True)
    hull_area = cv2.contourArea(hull)
    max_area = W * H * SCALLOP_MAX_AREA_FRAC

    v = {
        'area': float(area), 'arclen': float(peri),
        'scallop': None, 'solidity': None, 'cls': None,
        'accepted': False, 'reason': '', 'gate': '',
        'big_enough': area >= SCALLOP_MIN_AREA,
    }

    if area < SCALLOP_MIN_AREA:
        v['reason'] = 'area<floor'; v['gate'] = 'area_floor'; return v
    if area > max_area:
        v['reason'] = 'area>ceiling'; v['gate'] = 'area_ceiling'; return v
    if hull_peri < 100 or hull_area < 100:
        v['reason'] = 'hull too small'; v['gate'] = 'hull'; return v

    scallop = peri / hull_peri
    solidity = area / hull_area
    cls_score, _ = _cloud_likelihood_score(cnt)
    v.update(scallop=round(scallop, 3), solidity=round(solidity, 3),
             cls=round(cls_score, 3))

    path_a = scallop > SCALLOP_THRESHOLD and solidity < SCALLOP_MAX_SOLIDITY
    path_b = cls_score >= CLS_ACCEPT
    if not (path_a or path_b):
        v['reason'] = f'scallop={scallop:.2f}/solidity={solidity:.2f}/CLS={cls_score:.2f} fail accept'
        v['gate'] = 'acceptance_score'
        return v

    poly = contour_to_polygon(cnt, epsilon_frac=CLOUD_POLY_EPSILON)
    if len(poly) < 3:
        v['reason'] = 'degenerate poly'; v['gate'] = 'poly'; return v
    x, y, w, h = cv2.boundingRect(cnt)
    if _in_exclusion_zone([x, y, x + w, y + h], (H, W)):
        v['reason'] = 'exclusion zone'; v['gate'] = 'exclusion'; return v
    ok, reason = _validate_cloud_shape(poly, (H, W))
    if not ok:
        v['reason'] = reason; v['gate'] = 'validation_gate'; return v

    v['accepted'] = True
    v['reason'] = 'ACCEPT'
    return v


# ═══════════════════════════════════════════════════════════════════
# Per-region evidence extraction + A/B/C/D classification
# ═══════════════════════════════════════════════════════════════════
def _binarize_global(image: np.ndarray) -> np.ndarray:
    """EXACT binarization used by Stage 1 (global Otsu, inverted)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw


def _binarize_adaptive(image: np.ndarray) -> np.ndarray:
    """Comparison binarization (local adaptive) — tests the 'fixable by
    binarization' hypothesis for signal-loss cases."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, 31, 10)


def _diagnose_region(idx: int, box: List[int], image: np.ndarray,
                     bw_global: np.ndarray, bw_adapt: np.ndarray,
                     out_dir: Path) -> dict:
    H, W = image.shape[:2]
    x0, y0, x1, y1 = box
    rx0, ry0 = max(0, x0 - REGION_PAD), max(0, y0 - REGION_PAD)
    rx1, ry1 = min(W, x1 + REGION_PAD), min(H, y1 + REGION_PAD)
    reg_area = max(1, (rx1 - rx0) * (ry1 - ry0))

    crop      = image[ry0:ry1, rx0:rx1]
    crop_bin  = bw_global[ry0:ry1, rx0:rx1]
    crop_adpt = bw_adapt[ry0:ry1, rx0:rx1]

    ink_global   = float((crop_bin > 0).mean())
    ink_adaptive = float((crop_adpt > 0).mean())

    # Contours within the padded region (local connected components — same
    # view cv2.findContours gives the live pipeline).
    contours, _ = cv2.findContours(crop_bin, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    frags = []           # (contour_in_full_coords, arclen)
    for c in contours:
        al = cv2.arcLength(c, True)
        if al < MIN_FRAG_LEN:
            continue
        cf = c.copy()
        cf[:, 0, 0] += rx0
        cf[:, 0, 1] += ry0
        frags.append((cf, al))

    # Evaluate every fragment through the live acceptance logic.
    verdicts = [_evaluate_contour(c, (H, W)) for c, _ in frags]
    any_single_valid = any(v['accepted'] for v in verdicts)
    big_rejected = [v for v in verdicts
                    if v['big_enough'] and not v['accepted']
                    and v['gate'] in ('acceptance_score', 'validation_gate')]
    largest_area = max((v['area'] for v in verdicts), default=0.0)

    # Merge / reconstruction probe: union all fragment strokes, close gaps,
    # refit the largest contour, and test it through the SAME gate.
    merged_valid = False
    merged_verdict = None
    if len(frags) >= 1:
        m = np.zeros((ry1 - ry0, rx1 - rx0), dtype=np.uint8)
        for c, _ in frags:
            cc = c.copy(); cc[:, 0, 0] -= rx0; cc[:, 0, 1] -= ry0
            cv2.drawContours(m, [cc], -1, 255, 2)
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (MERGE_CLOSE_K, MERGE_CLOSE_K))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k, iterations=MERGE_CLOSE_IT)
        mc, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if mc:
            big = max(mc, key=cv2.contourArea)
            big[:, 0, 0] += rx0; big[:, 0, 1] += ry0
            merged_verdict = _evaluate_contour(big, (H, W))
            merged_valid = merged_verdict['accepted']

    n_frag = len(frags)

    # ── Classification decision tree (ordered) ──
    if n_frag == 0 or ink_global < INK_MIN_FRAC:
        label = 'A'
        note = 'signal absent after global Otsu'
        if ink_adaptive > ink_global * 2 and ink_adaptive > INK_MIN_FRAC:
            note += ' — adaptive recovers ink (binarization-fixable)'
    elif any_single_valid:
        label = 'C*'   # anomaly: a fully-valid contour exists yet wasn't detected
        note = 'a single contour PASSES all gates but pipeline missed it (integration/dedup bug)'
    elif big_rejected:
        label = 'C'
        g = big_rejected[0]
        note = f'substantial contour rejected by {g["gate"]}: {g["reason"]}'
    elif merged_valid:
        label = 'D'
        note = 'pieces exist; closing/union forms a VALID cloud — IoU merge misses it'
    else:
        label = 'B'
        note = f'{n_frag} fragments, largest area={largest_area:.0f}; naive closing does NOT reconstruct'

    # ── Save artifacts ──
    cdir = out_dir / f"miss_{idx:02d}_{label.replace('*','star')}"
    cdir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(cdir / "1_crop.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
    cv2.imwrite(str(cdir / "2_binary_global.png"), crop_bin)
    cv2.imwrite(str(cdir / "2b_binary_adaptive.png"), crop_adpt)

    # fragments overlay
    frag_img = cv2.cvtColor(crop_bin, cv2.COLOR_GRAY2BGR)
    for c, _ in frags:
        cc = c.copy(); cc[:, 0, 0] -= rx0; cc[:, 0, 1] -= ry0
        cv2.drawContours(frag_img, [cc], -1, (0, 255, 255), 2)
    cv2.imwrite(str(cdir / "3_fragments.jpg"), frag_img, [cv2.IMWRITE_JPEG_QUALITY, 90])

    # accepted (green) vs rejected (red)
    ar_img = crop.copy()
    for (c, _), v in zip(frags, verdicts):
        cc = c.copy(); cc[:, 0, 0] -= rx0; cc[:, 0, 1] -= ry0
        color = (0, 200, 0) if v['accepted'] else (0, 0, 230)
        cv2.drawContours(ar_img, [cc], -1, color, 2)
    cv2.imwrite(str(cdir / "4_accept_reject.jpg"), ar_img, [cv2.IMWRITE_JPEG_QUALITY, 90])

    # merge probe
    if merged_verdict is not None:
        cv2.imwrite(str(cdir / "5_merge_probe.png"), m)

    record = {
        'idx': idx, 'label': label, 'note': note, 'ref_box': box,
        'ink_global': round(ink_global, 5), 'ink_adaptive': round(ink_adaptive, 5),
        'n_fragments': n_frag, 'largest_frag_area': round(largest_area, 1),
        'any_single_valid': any_single_valid,
        'merged_valid': merged_valid,
        'merged_verdict': merged_verdict,
        'fragment_verdicts': verdicts,
        'artifacts': str(cdir),
    }
    with open(cdir / "verdict.json", 'w') as f:
        json.dump(record, f, indent=2)
    log.info(f"  miss #{idx} [{box}] → {label}: {note}")
    return record


# ═══════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════
def run_diagnosis(image: np.ndarray, out_dir: Path,
                  truth_path: Optional[str] = None, gemini=None) -> dict:
    H, W = image.shape[:2]
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Diagnostic on {W}x{H}")

    # 1. Reference set
    if truth_path:
        ref_boxes = _load_truth(truth_path)
        ref_source = f"manual_truth:{truth_path}"
    elif gemini is not None:
        ref_boxes = _gemini_instance_oracle(image, gemini)
        ref_source = "gemini_instance_oracle"
    else:
        raise ValueError("Need --truth <file> or a Gemini client for the oracle.")
    log.info(f"Reference clouds: {len(ref_boxes)} (source={ref_source})")

    # 2. Run the deterministic pipeline (OpenCV only — no Gemini refinement,
    #    so we diagnose the OpenCV signal path, not Gemini-recovered clouds).
    det = detect_clouds(image, debug_path=None, gemini=None)
    det_boxes = det.bounding_boxes
    log.info(f"Deterministic detections: {len(det_boxes)}")

    # 3. Match reference → detections
    missed = []
    for box in ref_boxes:
        bx0, by0, bx1, by1 = box
        matched = False
        for db in det_boxes:
            if iou(box, db) > MATCH_IOU:
                matched = True; break
            dcx, dcy = (db[0] + db[2]) / 2, (db[1] + db[3]) / 2
            if bx0 <= dcx <= bx1 and by0 <= dcy <= by1:
                matched = True; break
        if not matched:
            missed.append(box)
    log.info(f"Missed clouds: {len(missed)} / {len(ref_boxes)}")

    # 4. Diagnose each miss
    bw_global = _binarize_global(image)
    bw_adapt = _binarize_adaptive(image)
    records = [_diagnose_region(i + 1, box, image, bw_global, bw_adapt, out_dir)
               for i, box in enumerate(missed)]

    # 5. Aggregate statistics
    counts = {}
    for r in records:
        counts[r['label']] = counts.get(r['label'], 0) + 1

    summary = {
        'image_size': [W, H],
        'reference_source': ref_source,
        'n_reference': len(ref_boxes),
        'n_detected': len(det_boxes),
        'n_missed': len(missed),
        'recall_vs_reference': round(1 - len(missed) / max(1, len(ref_boxes)), 3),
        'failure_counts': counts,
        'legend': {
            'A': 'signal loss (boundary gone after binarization)',
            'B': 'fragmentation (pieces exist, naive closing fails to reconstruct)',
            'C': 'validation rejection (substantial contour killed by a gate)',
            'C*': 'anomaly: a fully-valid contour exists but pipeline missed it',
            'D': 'merge failure (pieces close into a valid cloud; IoU merge misses)',
        },
        'records': records,
    }
    with open(out_dir / "DIAGNOSIS_SUMMARY.json", 'w') as f:
        json.dump(summary, f, indent=2)

    # 6. Console report
    print("\n" + "═" * 64)
    print(" CLOUD-MISS DIAGNOSIS")
    print("═" * 64)
    print(f" Reference source : {ref_source}")
    print(f" Reference clouds : {len(ref_boxes)}")
    print(f" Detected (OpenCV): {len(det_boxes)}")
    print(f" Missed           : {len(missed)}  "
          f"(recall vs reference = {summary['recall_vs_reference']*100:.1f}%)")
    print("─" * 64)
    print(" FAILURE BREAKDOWN")
    order = ['A', 'B', 'C', 'C*', 'D']
    for k in order:
        if k in counts:
            print(f"   {k:2s} {summary['legend'][k]:<55s} {counts[k]}")
    for k, v in counts.items():
        if k not in order:
            print(f"   {k:2s} (other) {v}")
    print("─" * 64)
    print(" INTERPRETATION GUIDE")
    print("   A dominant → redesign BINARIZATION (front-end signal)")
    print("   B dominant → reconstruction GRAPH required")
    print("   C dominant → redesign VALIDATION gate")
    print("   D dominant → redesign MERGE (replace IoU with affinity/closure)")
    print("   C* present → integration/dedup bug — fix before re-architecting")
    print("═" * 64)
    print(f" Artifacts + per-miss verdicts: {out_dir}/")
    print(f" Summary JSON: {out_dir/'DIAGNOSIS_SUMMARY.json'}")
    if ref_source.startswith('gemini'):
        print("\n NOTE: reference is Gemini candidates, not ground truth.")
        print("       Open each miss_*/1_crop.jpg to confirm it is a real cloud")
        print("       before trusting the statistics.")
    print()
    return summary


def main():
    ap = argparse.ArgumentParser(description="Cloud-miss diagnostic (evidence only)")
    ap.add_argument("input", help="P&ID drawing image")
    ap.add_argument("--truth", help="Manual ground-truth JSON (boxes/polygons)")
    ap.add_argument("--out", default="debug_diag", help="Output directory")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    img = cv2.imread(args.input)
    if img is None:
        print(f"Cannot read: {args.input}", file=sys.stderr); sys.exit(1)

    gemini = None
    if not args.truth:
        from core.gemini_client import GeminiClient
        gemini = GeminiClient()

    run_diagnosis(img, Path(args.out), truth_path=args.truth, gemini=gemini)


if __name__ == "__main__":
    main()
