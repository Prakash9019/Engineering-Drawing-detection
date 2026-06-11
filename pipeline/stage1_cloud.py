"""
Stage 1: Revision Cloud Detection — v6 (Recall Improvement)
==============================================================
Architecture unchanged. Improvements to recall within existing pipeline:

  Stage 1: OpenCV Scalloped Detection    (source of truth)
  Stage 2: OpenCV Scallop-Pattern Recovery (NEW: curvature-based)
  Stage 3: Gemini Candidate Generation   (candidates only)
  Stage 4: ROI OpenCV Refinement         (REJECT if no scallop found)
  Stage 5: Cloud Shape Validation Gate   (mandatory, no exceptions)
  Stage 6: Merge + Dedup + Exclusion
  Stage 7: Final Scope Mask

Changes in this version:
  - NEW: Curvature periodicity analysis (_scallop_periodicity)
  - NEW: Cloud Likelihood Score (_cloud_likelihood_score)
  - NEW: Stage 2 uses curvature-based filtering (not just area/scallopedness)
  - IMPROVED: Stage 4 uses cloud likelihood score
  - ADJUSTED: Thresholds reviewed and tuned for recall
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from settings import CLOUD_DILATE_PX, CLOUD_POLY_EPSILON, SAVE_DEBUG_IMAGES
from core.geometry import contour_to_polygon, make_polygon_mask, iou

log = logging.getLogger(__name__)

# ── Thresholds (AUDITED — balanced precision + recall) ──
SCALLOP_THRESHOLD      = 1.70    # REVERTED from 1.60 — prevents merged-text FPs
SCALLOP_REFINE_MIN     = 1.30    # kept — ROI refinement can be permissive
SCALLOP_MIN_AREA       = 40000   # REVERTED from 25000 — rejects small noise
SCALLOP_MAX_AREA_FRAC  = 0.20    # REVERTED from 0.25
SCALLOP_MAX_SOLIDITY   = 0.88    # kept — helps dense-interior clouds

# Cloud likelihood score thresholds
CLS_ACCEPT             = 0.55    # cloud likelihood ≥ this → accept
CLS_REVIEW             = 0.45    # RAISED from 0.35 — Stage 2 was too permissive

# Validation gate
VALIDATE_MIN_VERTICES  = 6       # REVERTED from 5 — rejects rectangles
VALIDATE_MIN_SCALLOP   = 1.30    # REVERTED from 1.20 — rejects smooth shapes
VALIDATE_MAX_ASPECT    = 8.0     # REVERTED from 10.0

# Exclusion zones (LEGEND RESTORED, notes y_min REVERTED)
EXCL_ZONES = {
    'title_block':   {'x_min': 0.55, 'y_min': 0.82, 'x_max': 1.00, 'y_max': 1.00},
    'notes_block':   {'x_min': 0.00, 'y_min': 0.78, 'x_max': 0.55, 'y_max': 1.00},
    'legend':        {'x_min': 0.55, 'y_min': 0.72, 'x_max': 0.80, 'y_max': 0.82},
    'border_top':    {'x_min': 0.00, 'y_min': 0.00, 'x_max': 1.00, 'y_max': 0.008},
    'border_bottom': {'x_min': 0.00, 'y_min': 0.995,'x_max': 1.00, 'y_max': 1.00},
    'border_left':   {'x_min': 0.00, 'y_min': 0.00, 'x_max': 0.008,'y_max': 1.00},
    'border_right':  {'x_min': 0.995,'y_min': 0.00, 'x_max': 1.00, 'y_max': 1.00},
}

GEMINI_MAX_CANDIDATES  = 25


class CloudDetectionResult:
    def __init__(self):
        self.polygons: List[np.ndarray] = []
        self.bounding_boxes: List[List[int]] = []
        self.mask: Optional[np.ndarray] = None
        self.coverage_pct: float = 0.0
        self.is_full_scope: bool = False
        self.detection_mode: str = "none"
        self.sources: List[str] = []

    def __len__(self): return len(self.polygons)

    def to_json(self) -> dict:
        return {
            'num_clouds': len(self.polygons), 'coverage_pct': round(self.coverage_pct, 2),
            'is_full_scope': self.is_full_scope, 'detection_mode': self.detection_mode,
            'bounding_boxes': self.bounding_boxes,
            'polygons': [p.tolist() for p in self.polygons], 'sources': self.sources,
        }


# ═══════════════════════════════════════════════════════════════
# CURVATURE ANALYSIS (Q2: periodic scallop detection)
# ═══════════════════════════════════════════════════════════════
def _compute_curvature(contour: np.ndarray, step: int = 7) -> np.ndarray:
    """
    Compute discrete curvature along a contour.
    
    Curvature at point i = angle between vectors (i-step→i) and (i→i+step).
    Revision clouds have rapid curvature oscillation (sign changes).
    Straight lines have near-zero curvature. Smooth curves have slow variation.
    """
    pts = contour.reshape(-1, 2).astype(np.float64)
    n = len(pts)
    if n < step * 3:
        return np.array([])

    curvatures = np.zeros(n)
    for i in range(n):
        p_prev = pts[(i - step) % n]
        p_curr = pts[i]
        p_next = pts[(i + step) % n]

        v1 = p_curr - p_prev
        v2 = p_next - p_curr

        cross = v1[0] * v2[1] - v1[1] * v2[0]
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        mag1 = np.sqrt(v1[0]**2 + v1[1]**2)
        mag2 = np.sqrt(v2[0]**2 + v2[1]**2)

        if mag1 > 0.5 and mag2 > 0.5:
            curvatures[i] = np.arctan2(cross, dot)

    return curvatures


def _scallop_periodicity(contour: np.ndarray) -> Tuple[float, int]:
    """
    Measure how periodic the curvature is along a contour.
    
    Returns:
        (periodicity_score, zero_crossings)
        
    Revision clouds: high periodicity (0.5-1.0), many zero crossings
    Straight lines: low periodicity (~0), few crossings
    Smooth arcs: low periodicity (~0), very few crossings
    Instrument bubbles: moderate, few crossings (one circle)
    """
    curv = _compute_curvature(contour)
    if len(curv) < 20:
        return 0.0, 0

    # Count zero crossings (sign changes in curvature)
    signs = np.sign(curv)
    signs[signs == 0] = 1  # treat zero as positive
    crossings = np.sum(np.abs(np.diff(signs)) > 0)

    # Normalize by contour length
    peri = cv2.arcLength(contour, True)
    if peri < 100:
        return 0.0, 0

    # Crossings per 100px of perimeter
    crossing_density = crossings / (peri / 100.0)

    # Revision clouds: ~2-8 crossings per 100px (periodic scallops)
    # Text/symbols: <1 or >15 (random noise)
    # Equipment: <2 (smooth curves)
    if 1.5 <= crossing_density <= 12.0:
        # Curvature variance — clouds have consistent oscillation amplitude
        curv_std = np.std(curv[curv != 0]) if np.any(curv != 0) else 0
        if curv_std > 0.05:
            periodicity = min(1.0, crossing_density / 6.0) * min(1.0, curv_std * 5.0)
        else:
            periodicity = 0.0
    else:
        periodicity = 0.0

    return periodicity, crossings


# ═══════════════════════════════════════════════════════════════
# CLOUD LIKELIHOOD SCORE (Q3: multi-factor scoring)
# ═══════════════════════════════════════════════════════════════
def _cloud_likelihood_score(contour: np.ndarray) -> Tuple[float, dict]:
    """
    Multi-factor Cloud Likelihood Score (CLS).
    
    Ranks: revision cloud vs text vs equipment vs pipe contour.
    
    Factors:
      1. Scallopedness (peri/hull_peri)     — weight 0.30
      2. Curvature periodicity              — weight 0.30
      3. Solidity (area/hull_area)          — weight 0.15
      4. Complexity (vertices after simplify)— weight 0.15
      5. Aspect ratio penalty               — weight 0.10
    
    Returns: (score 0-1, factor_dict)
    """
    cnt = contour.reshape(-1, 1, 2).astype(np.int32) if contour.ndim == 2 else contour
    area = cv2.contourArea(cnt)
    peri = cv2.arcLength(cnt, True)
    hull = cv2.convexHull(cnt)
    hull_peri = cv2.arcLength(hull, True)
    hull_area = cv2.contourArea(hull)

    if hull_peri < 50 or hull_area < 100 or peri < 100:
        return 0.0, {}

    # Factor 1: Scallopedness
    scallop = peri / hull_peri
    f_scallop = min(1.0, max(0.0, (scallop - 1.0) / 2.0))  # 1.0→0, 3.0→1.0

    # Factor 2: Curvature periodicity
    f_period, crossings = _scallop_periodicity(cnt)

    # Factor 3: Solidity (inverse — lower solidity = more concavities = more cloud-like)
    solidity = area / hull_area if hull_area > 0 else 1.0
    f_solidity = max(0.0, 1.0 - solidity)  # 0.2 solidity→0.8 score

    # Factor 4: Complexity (number of vertices after simplification)
    epsilon = 0.005 * peri
    approx = cv2.approxPolyDP(cnt, epsilon, True)
    n_vertices = len(approx)
    f_complexity = min(1.0, n_vertices / 30.0)  # 30+ vertices = max

    # Factor 5: Aspect ratio penalty
    x, y, w, h = cv2.boundingRect(cnt)
    ar = max(w / h, h / w) if w > 0 and h > 0 else 1
    f_aspect = max(0.0, 1.0 - (ar - 1.0) / 10.0)  # 1→1.0, 11→0.0

    # Weighted score
    score = (0.30 * f_scallop +
             0.30 * f_period +
             0.15 * f_solidity +
             0.15 * f_complexity +
             0.10 * f_aspect)

    factors = {
        'scallop': round(scallop, 2), 'f_scallop': round(f_scallop, 2),
        'periodicity': round(f_period, 2), 'crossings': crossings,
        'solidity': round(solidity, 2), 'f_solidity': round(f_solidity, 2),
        'vertices': n_vertices, 'f_complexity': round(f_complexity, 2),
        'aspect': round(ar, 1), 'f_aspect': round(f_aspect, 2),
        'CLS': round(score, 3),
    }
    return score, factors


# ═══════════════════════════════════════════════════════════════
# VALIDATION GATE (mandatory for ALL polygons)
# ═══════════════════════════════════════════════════════════════
def _validate_cloud_shape(poly: np.ndarray, image_shape: tuple) -> Tuple[bool, str]:
    H, W = image_shape[:2]
    if len(poly) < VALIDATE_MIN_VERTICES:
        return False, f"vertices {len(poly)}<{VALIDATE_MIN_VERTICES}"
    cnt = poly.reshape(-1, 1, 2).astype(np.int32)
    peri = cv2.arcLength(cnt, True)
    hull = cv2.convexHull(cnt)
    hull_peri = cv2.arcLength(hull, True)
    if hull_peri < 50: return False, "hull too small"
    scallop = peri / hull_peri
    if scallop < VALIDATE_MIN_SCALLOP:
        return False, f"scallopedness {scallop:.2f}<{VALIDATE_MIN_SCALLOP}"
    x, y, w, h = cv2.boundingRect(cnt)
    cx, cy = (x + w / 2) / W, (y + h / 2) / H
    for zn, z in EXCL_ZONES.items():
        if z['x_min'] <= cx <= z['x_max'] and z['y_min'] <= cy <= z['y_max']:
            return False, f"exclusion: {zn}"
    if w > 0 and h > 0:
        ar = max(w / h, h / w)
        if ar > VALIDATE_MAX_ASPECT: return False, f"aspect {ar:.1f}>{VALIDATE_MAX_ASPECT}"
    area = cv2.contourArea(cnt)
    if area < 500: return False, f"area too small"
    if area > W * H * 0.30: return False, f"area >30%"
    return True, f"OK (scallop={scallop:.2f})"


def _in_exclusion_zone(bbox: List[int], shape: tuple) -> bool:
    H, W = shape[:2]
    cx, cy = (bbox[0]+bbox[2])/2/W, (bbox[1]+bbox[3])/2/H
    return any(z['x_min']<=cx<=z['x_max'] and z['y_min']<=cy<=z['y_max']
               for z in EXCL_ZONES.values())


# ═══════════════════════════════════════════════════════════════
# STAGE 1: OpenCV Scalloped Detection (with CLS scoring)
# ═══════════════════════════════════════════════════════════════
def _stage1_opencv_scalloped(image: np.ndarray) -> List[dict]:
    H, W = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    log.info(f"  [S1] Scanning {len(contours)} contours...")

    max_area = W * H * SCALLOP_MAX_AREA_FRAC
    clouds = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < SCALLOP_MIN_AREA or area > max_area: continue
        peri = cv2.arcLength(cnt, True)
        hull = cv2.convexHull(cnt)
        hull_peri = cv2.arcLength(hull, True)
        hull_area = cv2.contourArea(hull)
        if hull_peri < 100 or hull_area < 100: continue
        scallop = peri / hull_peri
        solidity = area / hull_area

        # Two paths to acceptance:
        # Path A: Traditional scallopedness threshold
        # Path B: Cloud Likelihood Score (catches clouds with lower scallopedness)
        cls_score, cls_factors = _cloud_likelihood_score(cnt)

        accepted = False
        if scallop > SCALLOP_THRESHOLD and solidity < SCALLOP_MAX_SOLIDITY:
            accepted = True  # Path A
        elif cls_score >= CLS_ACCEPT:
            accepted = True  # Path B

        if not accepted: continue

        poly = contour_to_polygon(cnt, epsilon_frac=CLOUD_POLY_EPSILON)
        if len(poly) < 3: continue
        x, y, w, h = cv2.boundingRect(cnt)
        bbox = [x, y, x + w, y + h]
        if _in_exclusion_zone(bbox, (H, W)): continue
        valid, reason = _validate_cloud_shape(poly, (H, W))
        if not valid: continue

        clouds.append({
            'poly': poly, 'bbox': bbox,
            'desc': f'scallop={scallop:.2f},CLS={cls_score:.2f}',
            'source': 'opencv_scalloped', 'cls': cls_score,
        })

    clouds.sort(key=lambda c: -c['cls'])
    log.info(f"  [S1] Found {len(clouds)} validated clouds")
    return clouds


# ═══════════════════════════════════════════════════════════════
# STAGE 2: Scallop-Pattern Recovery (IMPROVED — curvature-based)
# ═══════════════════════════════════════════════════════════════
def _stage2_scallop_recovery(image: np.ndarray, existing: List[dict]) -> List[dict]:
    """
    Use Canny edges + curvature periodicity to find open/broken cloud boundaries.
    
    Key improvement: filters by SCALLOP PATTERN (curvature periodicity)
    instead of just area/scallopedness. This rejects text/instrument contours
    that pass scallopedness but lack periodic scallop patterns.
    """
    H, W = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)  # REVERTED from 40,120 — less permissive
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))  # REDUCED from 7x7
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=1)  # REDUCED from 3
    contours, _ = cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)

    existing_bboxes = [c['bbox'] for c in existing]
    min_area = SCALLOP_MIN_AREA * 0.4  # lower floor for recovery
    max_area = W * H * SCALLOP_MAX_AREA_FRAC

    recovered = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area or area > max_area: continue

        # Cloud Likelihood Score — the key filter
        cls_score, factors = _cloud_likelihood_score(cnt)
        if cls_score < CLS_REVIEW: continue  # below review threshold

        # Must have scallop periodicity signal
        if factors.get('periodicity', 0) < 0.25: continue  # RAISED from 0.15

        poly = contour_to_polygon(cnt, epsilon_frac=CLOUD_POLY_EPSILON)
        if len(poly) < 3: continue
        x, y, w, h = cv2.boundingRect(cnt)
        bbox = [x, y, x + w, y + h]
        if _in_exclusion_zone(bbox, (H, W)): continue
        if any(iou(bbox, eb) > 0.30 for eb in existing_bboxes): continue
        valid, reason = _validate_cloud_shape(poly, (H, W))
        if not valid: continue

        recovered.append({
            'poly': poly, 'bbox': bbox,
            'desc': f'CLS={cls_score:.2f},period={factors.get("periodicity",0):.2f}',
            'source': 'opencv_scallop_recovery', 'cls': cls_score,
        })

    log.info(f"  [S2] Scallop recovery: {len(recovered)} additional clouds")
    return recovered


# ═══════════════════════════════════════════════════════════════
# STAGE 3: Gemini Candidates (unchanged — candidates only)
# ═══════════════════════════════════════════════════════════════
def _stage3_gemini_candidates(image: np.ndarray, gemini, existing: List[dict]) -> List[List[int]]:
    from core.json_parser import parse_json
    H, W = image.shape[:2]
    scale = min(1.0, 4000 / max(H, W))
    small = cv2.resize(image, (int(W * scale), int(H * scale)))
    sh, sw = small.shape[:2]
    marked = small.copy()
    for c in existing:
        b = [int(v * scale) for v in c['bbox']]
        cv2.rectangle(marked, (b[0], b[1]), (b[2], b[3]), (0, 0, 255), 3)

    prompt = f"""This P&ID has revision clouds (scalloped/bumpy boundaries).
RED rectangles show already-detected clouds.
Find MISSING revision clouds — especially:
- Bottom-right control signal area
- Right-side piping connections
- Open/partial clouds at edges
- Small clouds around individual instruments
DO NOT propose title blocks, notes, legends, tables, borders.
Return: [{{"box":[x_min,y_min,x_max,y_max]}}]
Image: {sw}x{sh}px. If none missing: []"""

    log.info(f"  [S3] Gemini scan on {sw}x{sh}...")
    raw = gemini.ask(prompt, marked)
    items = parse_json(raw)
    if not isinstance(items, list): return []

    existing_bboxes = [c['bbox'] for c in existing]
    candidates = []
    for item in items:
        if not isinstance(item, dict) or 'box' not in item: continue
        box = item.get('box', [])
        if len(box) != 4: continue
        try: x0, y0, x1, y1 = [int(float(v)) for v in box]
        except: continue
        if all(0 <= v <= 1000 for v in [x0,y0,x1,y1]) and sw > 1000:
            yn,xn,yn2,xn2 = x0,y0,x1,y1
            x0,y0 = int(xn*sw/1000), int(yn*sh/1000)
            x1,y1 = int(xn2*sw/1000), int(yn2*sh/1000)
        ox0,oy0 = int(x0/scale), int(y0/scale)
        ox1,oy1 = int(x1/scale), int(y1/scale)
        ox0,oy0 = max(0,ox0), max(0,oy0)
        ox1,oy1 = min(W,ox1), min(H,oy1)
        if ox1-ox0 < 100 or oy1-oy0 < 100: continue
        cand = [ox0,oy0,ox1,oy1]
        if _in_exclusion_zone(cand, (H,W)): continue
        if any(iou(cand, eb) > 0.30 for eb in existing_bboxes): continue
        candidates.append(cand)
        if len(candidates) >= GEMINI_MAX_CANDIDATES: break

    log.info(f"  [S3] {len(candidates)} candidate regions")
    return candidates


# ═══════════════════════════════════════════════════════════════
# STAGE 4: ROI Refinement (IMPROVED — uses CLS scoring)
# ═══════════════════════════════════════════════════════════════
def _stage4_refine(image: np.ndarray, candidates: List[List[int]],
                   shape: tuple) -> List[dict]:
    H, W = shape[:2]
    validated = []
    for bbox in candidates:
        x0, y0, x1, y1 = bbox
        pad = 30
        rx0,ry0 = max(0,x0-pad), max(0,y0-pad)
        rx1,ry1 = min(W,x1+pad), min(H,y1+pad)
        roi = image[ry0:ry1, rx0:rx1]
        if roi.size == 0: continue

        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 40, 120)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _ = cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        best_poly = None; best_cls = 0
        for cnt in contours:
            area = cv2.contourArea(cnt)
            rw, rh = rx1-rx0, ry1-ry0
            if area < rw * rh * 0.03: continue
            cls_score, _ = _cloud_likelihood_score(cnt)
            if cls_score > best_cls:
                poly = contour_to_polygon(cnt, epsilon_frac=CLOUD_POLY_EPSILON)
                if len(poly) >= VALIDATE_MIN_VERTICES:
                    best_cls = cls_score
                    best_poly = poly.copy()
                    best_poly[:, 0] += rx0
                    best_poly[:, 1] += ry0

        # REJECT if no valid cloud contour found — no fallback rectangles
        if best_poly is None or best_cls < CLS_REVIEW:
            log.info(f"    Rejected {bbox}: CLS={best_cls:.2f}")
            continue
        valid, reason = _validate_cloud_shape(best_poly, (H, W))
        if not valid:
            log.info(f"    Rejected {bbox}: {reason}")
            continue

        x, y, w, h = cv2.boundingRect(best_poly.reshape(-1,1,2).astype(np.int32))
        validated.append({
            'poly': best_poly, 'bbox': [x, y, x+w, y+h],
            'desc': f'refined_CLS={best_cls:.2f}',
            'source': 'gemini_refined', 'cls': best_cls,
        })

    log.info(f"  [S4] Refined {len(validated)}/{len(candidates)} candidates")
    return validated


# ═══════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════
def detect_clouds(image: np.ndarray, debug_path: Optional[Path] = None,
                  gemini=None) -> CloudDetectionResult:
    if image is None or image.size == 0: raise ValueError("Empty image")
    H, W = image.shape[:2]
    log.info(f"  Cloud detection on {W}x{H}")
    result = CloudDetectionResult()

    log.info("  ═══ Stage 1: OpenCV Scalloped ═══")
    s1 = _stage1_opencv_scalloped(image)

    log.info("  ═══ Stage 2: Scallop-Pattern Recovery ═══")
    s2 = _stage2_scallop_recovery(image, s1)
    all_clouds = s1 + s2

    if gemini is not None:
        log.info("  ═══ Stage 3: Gemini Candidates ═══")
        candidates = _stage3_gemini_candidates(image, gemini, all_clouds)
        if candidates:
            time.sleep(3)
            log.info("  ═══ Stage 4: ROI Refinement ═══")
            s4 = _stage4_refine(image, candidates, (H, W))
            for r in s4:
                if not any(iou(r['bbox'], c['bbox']) > 0.30 for c in all_clouds):
                    all_clouds.append(r)

    if not all_clouds:
        log.warning("  No clouds → FULL SCOPE")
        result.is_full_scope = True; result.coverage_pct = 100.0; return result

    result.detection_mode = "opencv+gemini" if gemini else "opencv"
    result.polygons = [c['poly'] for c in all_clouds]
    result.bounding_boxes = [c['bbox'] for c in all_clouds]
    result.sources = [c['source'] for c in all_clouds]
    result.mask = make_polygon_mask((H, W), result.polygons, dilate_px=CLOUD_DILATE_PX)
    result.coverage_pct = (result.mask > 0).sum() / (H * W) * 100

    src_counts = {}
    for c in all_clouds: src_counts[c['source']] = src_counts.get(c['source'], 0) + 1
    log.info(f"  ═══ Final: {len(all_clouds)} clouds, coverage={result.coverage_pct:.1f}% ═══")
    for src, cnt in sorted(src_counts.items()): log.info(f"    {src}: {cnt}")

    if result.coverage_pct > 85:
        result.is_full_scope = True; result.coverage_pct = 100.0; return result

    if SAVE_DEBUG_IMAGES and debug_path is not None:
        debug_path.mkdir(parents=True, exist_ok=True)
        color_map = {
            'opencv_scalloped': (0, 0, 255),
            'opencv_scallop_recovery': (0, 165, 255),
            'gemini_refined': (255, 0, 0),
        }
        overlay = image.copy()
        for i, c in enumerate(all_clouds):
            color = color_map.get(c['source'], (128, 128, 128))
            cv2.polylines(overlay, [c['poly'].astype(np.int32)], True, color, 5)
            M = cv2.moments(c['poly'].astype(np.int32))
            if M['m00'] > 0:
                cv2.putText(overlay, f"{i+1}", (int(M['m10']/M['m00'])-10,
                            int(M['m01']/M['m00'])+5), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        cv2.imwrite(str(debug_path / "01_cloud_overlay.jpg"), overlay, [cv2.IMWRITE_JPEG_QUALITY, 88])
        cv2.imwrite(str(debug_path / "02_scope_mask.png"), result.mask)
        tinted = image.copy()
        tinted[result.mask == 0] = (tinted[result.mask == 0] * 0.3).astype(np.uint8)
        cv2.imwrite(str(debug_path / "03_scope_tinted.jpg"), tinted, [cv2.IMWRITE_JPEG_QUALITY, 85])
        log.info(f"  Debug: {debug_path}/")

    return result


if __name__ == "__main__":
    import logging as lg
    lg.basicConfig(level=lg.INFO, format="%(asctime)s  %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python pipeline/stage1_cloud.py <image> [--gemini]"); sys.exit(1)
    img = cv2.imread(sys.argv[1])
    if img is None: print(f"Cannot read: {sys.argv[1]}"); sys.exit(1)
    gem = None
    if "--gemini" in sys.argv:
        from core.gemini_client import GeminiClient; gem = GeminiClient()
    res = detect_clouds(img, debug_path=Path("debug"), gemini=gem)
    print(f"\nResult: {len(res)} clouds, {res.detection_mode}, {res.coverage_pct:.1f}%")
    for i, bb in enumerate(res.bounding_boxes):
        print(f"  Cloud {i+1}: {bb}  [{res.sources[i]}]")
    H, W = img.shape[:2]; final = img.copy()
    over = np.zeros_like(final)
    for poly in res.polygons: cv2.fillPoly(over, [poly.astype(np.int32)], (0,0,180))
    final = cv2.addWeighted(final, 1.0, over, 0.25, 0)
    for poly in res.polygons: cv2.polylines(final, [poly.astype(np.int32)], True, (0,0,255), 5)
    cv2.rectangle(final, (0,0), (W,45), (0,0,0), -1)
    cv2.putText(final, f"Clouds:{len(res)} | {res.detection_mode} | {res.coverage_pct:.1f}%",
                (10,32), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255,255,255), 2)
    out = str(Path(sys.argv[1]).stem) + "_clouds.jpg"
    cv2.imwrite(out, final, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"✓ {out}")