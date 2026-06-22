#!/usr/bin/env python3
"""
cloud_detector_v2.py — Combined 95%+ Outer Cloud Detection Pipeline

Architecture:
  P0  Pre-process: CLAHE + adaptive binarize
  P1  LOCALIZE: Gemini 2.5 Pro per-cloud bboxes (falls back to fragment-seeded if no key)
  P2  PER-CROP: morph close (13px) → RETR_EXTERNAL → approxPolyDP (NOT convexHull) → validate
  P3  STAGE-1: OpenCV scalloped detector for additional coverage
  P4  MERGE: IoU NMS + exclusion zones
  P5  NESTING: containment fraction → outer vs inner tag
  Out: overlay_v2.jpg, outer_clouds_v2.json, cloud_mask_v2.png

Key fixes over prior approaches:
  • Per-crop processing (inside Gemini bbox) prevents sheet-level contour welding
  • Morphological close kernel 13px (not 3px) bridges pipe-crossing gaps (5–50px)
  • approxPolyDP instead of convexHull preserves the scalloped boundary shape
  • Scallopedness threshold 1.10 (not 1.30) recovers validation-rejected clouds
  • Adaptive binarize (CLAHE) preserves faint arcs that global Otsu drops

Usage:
  python cloud_detector_v2.py input_drawing.jpg
  python cloud_detector_v2.py input_drawing.jpg --out output/ --debug
  python cloud_detector_v2.py input_drawing.jpg --no-gemini  # deterministic only
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Detection parameters ──────────────────────────────────────────────────────
GEMINI_MODEL          = "gemini-3.1-pro-preview"
GEMINI_TEMPERATURE    = 0.0           # deterministic decoding (best-effort)
GEMINI_SEED           = 42            # fixed seed for reproducible sampling
GEMINI_CACHE_NAME     = "gemini_cloud_cache.json"  # content-hash response cache
GEMINI_MAX_SCALE      = 3072          # max longest side when sending to Gemini
SNAP_PAD              = 60            # px to pad around Gemini bbox before crop
MORPH_CLOSE_K         = 13            # kernel size — bridges junction gaps 5–50px
MORPH_CLOSE_ITER      = 4             # iterations of morphological close
VALIDATE_MIN_SCALLOP  = 1.10          # peri/hull_peri (lowered from 1.30)
VALIDATE_MIN_SOLIDITY = 0.15          # area/hull_area below this = a pipe/line network, not a cloud
VALIDATE_MIN_VERTICES = 6
VALIDATE_ASPECT_MAX   = 8.0
VALIDATE_AREA_MIN     = 800           # px²
VALIDATE_AREA_MAX_FRAC= 0.30          # fraction of total image area
NMS_IOU_THRESHOLD     = 0.35
NEST_CONTAIN_FRAC     = 0.80
ADAPTIVE_BLOCK        = 51            # must be odd
ADAPTIVE_C            = 10
CLAHE_CLIP            = 3.0
CLAHE_TILE            = 8
STAGE1_MIN_SCALLOP    = 1.60          # stage1 OpenCV acceptance threshold (kept high for precision)

# ── Border-rejection parameters ───────────────────────────────────────────────
# Longest straight segment / image diagonal threshold.
# Test sheet (9934×7017): border welds measured 2862–9567 px; all real clouds < 730 px.
# Threshold at 0.10 × diagonal (~1216 px) gives clean separation with margin.
STRAIGHT_RUN_MAX_FRAC = 0.10          # segments longer than this fraction → border (if smooth)
FRAME_RUN_FRAC        = 0.45          # run ≥ this fraction of diagonal → always a frame edge
BORDER_SMOOTH_MAX     = 2.2           # below this scallopedness a long run is a frame/pipe, not a cloud
STRAIGHT_RUN_EPSILON  = 5.0           # approxPolyDP epsilon for segment extraction
BORDER_EDGE_MARGIN_PX = 2            # px from image edge counts as "touching"
BORDER_EDGE_SIDES_MIN = 2             # touching ≥ N sides → frame structure

# ── Exclusion zones (fractions of image W, H) ─────────────────────────────────
EXCL_ZONES = {
    "title_block":  dict(x0=0.50, y0=0.82, x1=0.99, y1=1.00),
    "notes_block":  dict(x0=0.00, y0=0.78, x1=0.50, y1=1.00),
    "legend":       dict(x0=0.50, y0=0.72, x1=0.99, y1=0.82),
    "border_top":   dict(x0=0.00, y0=0.00, x1=1.00, y1=0.01),
    "border_bottom":dict(x0=0.00, y0=0.98, x1=1.00, y1=1.00),
    "border_left":  dict(x0=0.00, y0=0.00, x1=0.01, y1=1.00),
    "border_right": dict(x0=0.99, y0=0.00, x1=1.00, y1=1.00),
}


# ═══════════════════════════════════════════════════════════════════════════════
# Geometry helpers
# ═══════════════════════════════════════════════════════════════════════════════

def scallopedness(contour: np.ndarray) -> float:
    """peri / hull_peri — values > 1 indicate bumpiness."""
    peri = cv2.arcLength(contour, True)
    hull = cv2.convexHull(contour)
    hull_peri = cv2.arcLength(hull, True)
    if hull_peri < 1e-6:
        return 0.0
    return peri / hull_peri


def poly_area_px(contour: np.ndarray) -> float:
    return abs(cv2.contourArea(contour))


def solidity(contour: np.ndarray) -> float:
    """
    contourArea / convexHullArea — how much of the shape's hull is filled.

    A revision cloud is a closed boundary enclosing a substantial region, so its
    solidity is high (≳0.35). A pipe / signal-line network traced as a contour
    zigzags through a large hull while enclosing almost nothing, so its solidity
    is tiny (≲0.10) even though its scallopedness (peri/hull_peri) looks cloud-like.
    This is the discriminator that separates a real cloud from a 'line inside a cloud'.
    """
    area = abs(cv2.contourArea(contour))
    hull_area = abs(cv2.contourArea(cv2.convexHull(contour)))
    if hull_area < 1e-6:
        return 0.0
    return area / hull_area


def poly_bbox(contour: np.ndarray) -> Tuple[int, int, int, int]:
    x, y, w, h = cv2.boundingRect(contour)
    return x, y, x + w, y + h


def poly_aspect(contour: np.ndarray) -> float:
    x, y, w, h = cv2.boundingRect(contour)
    if min(w, h) < 1:
        return 999.0
    return max(w, h) / min(w, h)


def poly_centroid(contour: np.ndarray) -> Tuple[float, float]:
    M = cv2.moments(contour)
    if M["m00"] < 1e-6:
        xs = contour[:, 0, 0]
        ys = contour[:, 0, 1]
        return float(np.mean(xs)), float(np.mean(ys))
    return M["m10"] / M["m00"], M["m01"] / M["m00"]


def polygon_iou(a: np.ndarray, b: np.ndarray, img_shape: Tuple) -> float:
    """Rasterize and compute IoU."""
    H, W = img_shape[:2]
    m_a = np.zeros((H, W), dtype=np.uint8)
    m_b = np.zeros((H, W), dtype=np.uint8)
    cv2.fillPoly(m_a, [a], 1)
    cv2.fillPoly(m_b, [b], 1)
    inter = np.count_nonzero(m_a & m_b)
    union = np.count_nonzero(m_a | m_b)
    return inter / union if union > 0 else 0.0


def containment_fraction(inner: np.ndarray, outer: np.ndarray, img_shape: Tuple) -> float:
    """Fraction of inner polygon's area that falls inside outer polygon."""
    H, W = img_shape[:2]
    m_i = np.zeros((H, W), dtype=np.uint8)
    m_o = np.zeros((H, W), dtype=np.uint8)
    cv2.fillPoly(m_i, [inner], 1)
    cv2.fillPoly(m_o, [outer], 1)
    area_i = np.count_nonzero(m_i)
    if area_i == 0:
        return 0.0
    return np.count_nonzero(m_i & m_o) / area_i


# ═══════════════════════════════════════════════════════════════════════════════
# Border-line rejection
# ═══════════════════════════════════════════════════════════════════════════════

def max_straight_run(contour: np.ndarray,
                     epsilon: float = STRAIGHT_RUN_EPSILON) -> float:
    """
    Length (px) of the longest single straight segment in the contour.

    approxPolyDP collapses the contour into polyline segments; the longest
    segment is the key discriminator between border lines and cloud arcs.
    Drawing borders produce segments of 2000–9500 px; cloud arcs stay < 730 px.
    """
    pts = cv2.approxPolyDP(contour, epsilon, True).reshape(-1, 2).astype(float)
    if len(pts) < 2:
        return 0.0
    n = len(pts)
    best = 0.0
    for i in range(n):
        seg = float(np.linalg.norm(pts[(i + 1) % n] - pts[i]))
        if seg > best:
            best = seg
    return best


# def is_border_structure(contour: np.ndarray, img_shape: Tuple) -> bool:
#     """
#     Returns True if this contour is a drawing border / sheet frame /
#     title-block boundary — NOT a revision cloud.

#     Primary discriminator  — longest straight run > STRAIGHT_RUN_MAX_FRAC × diagonal.
#     Secondary discriminator — contour points touch ≥ BORDER_EDGE_SIDES_MIN image edges.

#     Do NOT use area/extent here: those filters delete large genuine clouds.
#     (Validated on 9934×7017 sheet; see memory: cloud-pipeline-discriminator.)
#     """
#     H, W = img_shape[:2]
#     diag = float(np.sqrt(W * W + H * H))

#     if max_straight_run(contour) > STRAIGHT_RUN_MAX_FRAC * diag:
#         return True

#     pts = contour.reshape(-1, 2)
#     m = BORDER_EDGE_MARGIN_PX
#     sides_touched = (
#         int(np.any(pts[:, 0] <= m)) +
#         int(np.any(pts[:, 0] >= W - m)) +
#         int(np.any(pts[:, 1] <= m)) +
#         int(np.any(pts[:, 1] >= H - m))
#     )
#     if sides_touched >= BORDER_EDGE_SIDES_MIN:
#         return True

#     return False

def is_border_structure(contour, img_shape):
    """
    Reject drawing frames / sheet borders / long pipes — but NOT large revision
    clouds that happen to have a long straight run.

    A genuine revision cloud is heavily scalloped (peri/hull_peri well above 1),
    even when it is huge and runs to the sheet edge (e.g. the big right→middle
    cloud: run≈3500px but scallopedness≈5.9). The plain "long run → border" rule
    discarded it. Two-part discriminator instead:
      • run ≥ FRAME_RUN_FRAC × diagonal  → a true frame edge spans ~half the
        sheet; nothing scalloped does that. Always a frame.
      • run > STRAIGHT_RUN_MAX_FRAC × diagonal AND shape is SMOOTH
        (scallopedness < BORDER_SMOOTH_MAX) → a long straight, low-curvature run
        is a frame/pipe, not a cloud.
    A long run on a highly-scalloped contour is kept.
    """
    H, W = img_shape[:2]
    diag = np.hypot(W, H)

    run_px = max_straight_run(contour)
    s = scallopedness(contour)

    if run_px > FRAME_RUN_FRAC * diag:
        return True
    if run_px > STRAIGHT_RUN_MAX_FRAC * diag and s < BORDER_SMOOTH_MAX:
        return True
    return False

def reject_borders(
    polys: List[dict],
    img_shape: Tuple,
    vis_path: Optional[str] = None,
    img_bgr: Optional[np.ndarray] = None,
) -> Tuple[List[dict], List[dict]]:
    """
    Partition polys into (kept, removed_borders).

    When vis_path + img_bgr are provided, writes a before/after JPEG:
      Red   = rejected border structures (labelled with max straight-run px)
      Green = kept cloud candidates
    """
    kept, removed = [], []
    for p in polys:
        c = p["contour"]
        if is_border_structure(c, img_shape):
            x, y, w, h = cv2.boundingRect(c)
            run_px = max_straight_run(c)
            log.info(
                "BORDER REJECTED  src=%-14s  bbox=[%d,%d,%d,%d]  "
                "area=%.0f  max_run=%.0f px",
                p.get("source", "?"), x, y, x + w, y + h,
                poly_area_px(c), run_px,
            )
            removed.append(p)
        else:
            kept.append(p)

    log.info("Border filter: %d kept  /  %d borders removed", len(kept), len(removed))

    if vis_path and img_bgr is not None:
        vis = img_bgr.copy()
        for p in removed:
            c = p["contour"]
            cv2.polylines(vis, [c], True, (0, 0, 255), 4)
            cx, cy = map(int, poly_centroid(c))
            run_px = max_straight_run(c)
            cv2.putText(
                vis, f"BORDER {run_px:.0f}px",
                (max(0, cx - 80), max(20, cy)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2,
            )
        for p in kept:
            cv2.polylines(vis, [p["contour"]], True, (0, 220, 0), 3)
        cv2.imwrite(vis_path, vis, [cv2.IMWRITE_JPEG_QUALITY, 88])
        log.info("Border filter visualization saved: %s", vis_path)

    return kept, removed


# ═══════════════════════════════════════════════════════════════════════════════
# Frame-border removal (the drawing's outer rectangle only)
# ═══════════════════════════════════════════════════════════════════════════════

FRAME_EDGE_BAND   = 400    # px: only lines within this distance of an image edge count as frame
FRAME_MIN_RUN_FRAC = 0.45  # a near-edge straight line spanning ≥ this fraction of W/H is the frame


def remove_frame_border(img_bgr: np.ndarray) -> np.ndarray:
    """
    Erase ONLY the drawing's outer frame rectangle — long straight horizontal/
    vertical lines that lie within FRAME_EDGE_BAND px of an image edge.

    This is deliberately narrow and NON-destructive to clouds: revision clouds
    are scalloped interior shapes, never long straight strokes hugging the sheet
    edge. Removing the frame stops the stage-1 close from welding interior
    content onto the border (the welds that trace the left/top border and
    swallow the genuine left clouds). Interior pipes are NOT touched — only the
    near-edge frame, unlike the earlier global line-removal that broke clouds.
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    bn = adaptive_binarize(gray)
    h, w = bn.shape

    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (int(w * FRAME_MIN_RUN_FRAC), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(h * FRAME_MIN_RUN_FRAC)))
    long_lines = cv2.bitwise_or(
        cv2.morphologyEx(bn, cv2.MORPH_OPEN, hk),
        cv2.morphologyEx(bn, cv2.MORPH_OPEN, vk),
    )

    edge = np.zeros_like(long_lines)
    b = FRAME_EDGE_BAND
    edge[:b, :] = 255; edge[-b:, :] = 255
    edge[:, :b] = 255; edge[:, -b:] = 255
    frame = cv2.bitwise_and(long_lines, edge)
    frame = cv2.dilate(frame, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))

    out = img_bgr.copy()
    out[frame > 0] = (255, 255, 255)   # paint white = erase the frame ink
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# Binarization
# ═══════════════════════════════════════════════════════════════════════════════

def adaptive_binarize(gray: np.ndarray) -> np.ndarray:
    """
    CLAHE + adaptive threshold.
    Preserves faint arcs that global Otsu drops (Failure Mode A fix).
    """
    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP, tileGridSize=(CLAHE_TILE, CLAHE_TILE))
    enhanced = clahe.apply(gray)
    binary = cv2.adaptiveThreshold(
        enhanced, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
        ADAPTIVE_BLOCK, ADAPTIVE_C
    )
    # small bridge to reconnect 1–2px breaks from JPEG artifacts
    bridge = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, bridge)
    return binary


# ═══════════════════════════════════════════════════════════════════════════════
# Validation gate
# ═══════════════════════════════════════════════════════════════════════════════

def validate_cloud(contour: np.ndarray, img_shape: Tuple,
                   min_scallop: float = VALIDATE_MIN_SCALLOP,
                   skip_zones: bool = False) -> bool:
    """
    All conditions must pass.

    skip_zones=True bypasses the legend/notes/title-block exclusion rectangles.
    Use it for Gemini-localized boxes: Gemini was explicitly told to return only
    revision clouds and to ignore title blocks/borders, so a genuine clouded note
    sitting in the bottom/legend region must NOT be vetoed just for its location.
    The deterministic detectors keep the zone veto (skip_zones=False) because they
    would otherwise grab title-block rectangles.
    """
    if len(contour) < VALIDATE_MIN_VERTICES:
        return False
    s = scallopedness(contour)
    if s < min_scallop:
        return False
    # Reject pipe/signal-line networks: they zigzag (high scallopedness) but
    # enclose almost no area, so their solidity is far below any real cloud.
    if solidity(contour) < VALIDATE_MIN_SOLIDITY:
        return False
    if poly_aspect(contour) > VALIDATE_ASPECT_MAX:
        return False
    H, W = img_shape[:2]
    area = poly_area_px(contour)
    if area < VALIDATE_AREA_MIN:
        return False
    if area > VALIDATE_AREA_MAX_FRAC * H * W:
        return False
    if not skip_zones:
        cx, cy = poly_centroid(contour)
        for zone in EXCL_ZONES.values():
            if (zone["x0"] * W <= cx <= zone["x1"] * W and
                    zone["y0"] * H <= cy <= zone["y1"] * H):
                return False
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini localization
# ═══════════════════════════════════════════════════════════════════════════════

_GEMINI_PROMPT = """You are analyzing a P&ID (Piping and Instrumentation Diagram) engineering drawing.

Your task: locate ALL revision clouds — the scalloped/bumpy closed boundaries that mark the scope of engineering changes.

Revision clouds have distinctive features:
- Bumpy/scalloped outline (series of convex arcs like a cloud shape)
- Form closed or nearly-closed boundaries
- Can be large or small, any aspect ratio
- Often overlap with pipes, text, and symbols

Return a JSON array (no markdown, no explanation) with one object per cloud:
[{"x0": left, "y0": top, "x1": right, "y1": bottom, "confidence": 0.0-1.0}, ...]

Coordinates are in pixels of the image you received.
Include ALL clouds you see, even faint or partial ones.
Do NOT include title blocks, borders, or legend areas."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _gemini_call(img_bgr: np.ndarray, api_key: str, retries: int = 2) -> List[dict]:
    """
    Send image to Gemini, return list of bbox dicts. Tries new SDK, then legacy.

    Transient failures (network blips, 5xx, occasional empty responses) are
    retried before giving up — otherwise a single hiccup silently drops the
    whole pipeline back to the noisy OpenCV-only fallback.
    """
    ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        return []
    img_bytes = buf.tobytes()

    err_new = err_legacy = None

    for attempt in range(retries + 1):
        # try google-genai (new SDK)
        try:
            import google.genai as genai
            from google.genai import types as gtypes
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    gtypes.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
                    _GEMINI_PROMPT,
                ],
                config=gtypes.GenerateContentConfig(
                    temperature=GEMINI_TEMPERATURE,
                    seed=GEMINI_SEED,
                ),
            )
            parsed = json.loads(_strip_fences(resp.text))
            if parsed:
                return parsed
            err_new = "empty response"
        except Exception as e:
            err_new = e
            log.debug("New Gemini SDK attempt %d failed: %s", attempt + 1, e)

        # fallback: google-generativeai (legacy)
        try:
            import google.generativeai as genai_legacy
            import PIL.Image
            import io
            genai_legacy.configure(api_key=api_key)
            model = genai_legacy.GenerativeModel(GEMINI_MODEL)
            pil_img = PIL.Image.open(io.BytesIO(img_bytes))
            resp = model.generate_content(
                [pil_img, _GEMINI_PROMPT],
                generation_config={"temperature": GEMINI_TEMPERATURE},
            )
            parsed = json.loads(_strip_fences(resp.text))
            if parsed:
                return parsed
            err_legacy = "empty response"
        except Exception as e:
            err_legacy = e
            log.debug("Legacy Gemini SDK attempt %d failed: %s", attempt + 1, e)

        log.warning("Gemini attempt %d/%d returned nothing (new=%s legacy=%s) — retrying",
                    attempt + 1, retries + 1, err_new, err_legacy)

    log.error("Gemini failed after %d attempts: new=%s legacy=%s", retries + 1, err_new, err_legacy)
    return []


def _cache_key(img_bytes: bytes) -> str:
    """Stable key over the exact bytes sent + model + prompt + decode params.

    Identical image + identical request → identical key → cache hit, so a
    re-run reuses the previous Gemini answer instead of sampling a new
    (different) one. The model/prompt/seed are folded in so changing any of
    them correctly invalidates the cache.
    """
    h = hashlib.sha256()
    h.update(img_bytes)
    h.update(GEMINI_MODEL.encode())
    h.update(_GEMINI_PROMPT.encode())
    h.update(f"t={GEMINI_TEMPERATURE};seed={GEMINI_SEED}".encode())
    return h.hexdigest()


def _load_cache(cache_path: Optional[str]) -> dict:
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                return json.load(f)
        except Exception as e:
            log.warning("Could not read Gemini cache %s: %s", cache_path, e)
    return {}


def _save_cache(cache_path: Optional[str], cache: dict) -> None:
    if not cache_path:
        return
    try:
        with open(cache_path, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        log.warning("Could not write Gemini cache %s: %s", cache_path, e)


def locate_with_gemini(img_bgr: np.ndarray, api_key: str,
                       cache_path: Optional[str] = None,
                       use_cache: bool = True) -> List[dict]:
    """
    Scale image to max GEMINI_MAX_SCALE, call Gemini, map coords back to orig.
    Returns list of {x0, y0, x1, y1, confidence} in original pixel coords.

    A content-hash cache makes re-runs on an unchanged image deterministic:
    the raw Gemini response is keyed by the exact bytes sent. On a hit, the API
    is not called at all, so the output is bit-for-bit identical to the prior
    run. This is the hard guarantee — temperature=0/seed reduce, but do not
    eliminate, Gemini's run-to-run variation.
    """
    H, W = img_bgr.shape[:2]
    scale = min(1.0, GEMINI_MAX_SCALE / max(H, W))
    if scale < 1.0:
        send_img = cv2.resize(img_bgr, (int(W * scale), int(H * scale)))
    else:
        send_img = img_bgr

    # Encode once so the cache key matches the exact bytes that would be sent.
    ok, buf = cv2.imencode(".jpg", send_img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    key = _cache_key(buf.tobytes()) if ok else None

    cache = _load_cache(cache_path) if use_cache else {}
    if use_cache and key and key in cache:
        raw = cache[key]
        log.info("Gemini cache HIT (%s…) — %d cached bboxes, no API call",
                 key[:12], len(raw))
    else:
        raw = _gemini_call(send_img, api_key)
        if use_cache and key and raw:
            cache[key] = raw
            _save_cache(cache_path, cache)
            log.info("Gemini cache MISS — stored %d bboxes under %s…",
                     len(raw), key[:12])
    if not raw:
        return []

    results = []
    for item in raw:
        try:
            if "box_2d" in item:
                # Gemini 3.x native format: [ymin, xmin, ymax, xmax] normalized 0–1000.
                # Map straight to ORIGINAL pixels (normalized coords already encode
                # the full frame, so the send-scale cancels out).
                ymin, xmin, ymax, xmax = (float(v) for v in item["box_2d"])
                x0 = max(0, int(xmin / 1000.0 * W))
                y0 = max(0, int(ymin / 1000.0 * H))
                x1 = min(W, int(xmax / 1000.0 * W))
                y1 = min(H, int(ymax / 1000.0 * H))
                conf = float(item.get("confidence", 0.8))
            else:
                # legacy pixel format {x0,y0,x1,y1} in the SENT image's pixels
                x0 = max(0, int(item["x0"] / scale))
                y0 = max(0, int(item["y0"] / scale))
                x1 = min(W, int(item["x1"] / scale))
                y1 = min(H, int(item["y1"] / scale))
                conf = float(item.get("confidence", 0.8))
            if x1 > x0 and y1 > y0:
                results.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "confidence": conf})
        except (KeyError, ValueError, TypeError):
            continue
    log.info("Gemini returned %d candidate bboxes", len(results))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Fallback localization (no API key)
# ═══════════════════════════════════════════════════════════════════════════════

def locate_fallback(binary: np.ndarray) -> List[dict]:
    """
    Deterministic fallback when Gemini is unavailable.
    Find all contours with scallopedness > STAGE1_MIN_SCALLOP.
    Uses connected components directly from adaptive binary.
    """
    # larger close to form candidate regions
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, k, iterations=3)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    bboxes = []
    H, W = binary.shape[:2]
    for c in contours:
        area = cv2.contourArea(c)
        if area < VALIDATE_AREA_MIN or area > VALIDATE_AREA_MAX_FRAC * H * W:
            continue
        s = scallopedness(c)
        if s < 1.05:
            continue
        x, y, w, h = cv2.boundingRect(c)
        bboxes.append({"x0": x, "y0": y, "x1": x + w, "y1": y + h, "confidence": float(s - 1.0)})
    log.info("Fallback locator found %d candidates", len(bboxes))
    return bboxes


# ═══════════════════════════════════════════════════════════════════════════════
# Per-crop boundary recovery (the core fix)
# ═══════════════════════════════════════════════════════════════════════════════

def recover_from_crop(img_bgr: np.ndarray, bbox: dict, img_shape: Tuple,
                      debug_dir: Optional[Path] = None,
                      bbox_idx: int = 0,
                      skip_zones: bool = False) -> List[np.ndarray]:
    """
    Given a bounding box in original image coords, recover the actual cloud polygon.

    Key architecture:
    1. Crop with padding
    2. Adaptive binarize
    3. Scallop-scale morphological close (13px, 4 iter) — bridges junction gaps
    4. RETR_EXTERNAL → contours in crop-local coords
    5. approxPolyDP (NOT convexHull) — preserves scalloped shape
    6. Map back to full image coords
    7. Validate
    """
    H, W = img_shape[:2]
    x0 = max(0, bbox["x0"] - SNAP_PAD)
    y0 = max(0, bbox["y0"] - SNAP_PAD)
    x1 = min(W, bbox["x1"] + SNAP_PAD)
    y1 = min(H, bbox["y1"] + SNAP_PAD)

    crop = img_bgr[y0:y1, x0:x1]
    if crop.size == 0:
        return []

    gray_crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    binary_crop = adaptive_binarize(gray_crop)

    # Scallop-scale close: bridges 5–50px gaps at pipe/text crossings
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (MORPH_CLOSE_K, MORPH_CLOSE_K))
    closed = cv2.morphologyEx(binary_crop, cv2.MORPH_CLOSE, k_close, iterations=MORPH_CLOSE_ITER)

    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(debug_dir / f"crop_{bbox_idx:03d}_binary.png"), binary_crop)
        cv2.imwrite(str(debug_dir / f"crop_{bbox_idx:03d}_closed.png"), closed)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []

    # sort by area desc, try up to 3 largest
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    results = []

    for c in contours[:3]:
        area = cv2.contourArea(c)
        if area < VALIDATE_AREA_MIN:
            break  # sorted, so rest are smaller

        # approxPolyDP — NOT convexHull — this preserves the scalloped boundary shape
        eps = 2.0
        approx = cv2.approxPolyDP(c, eps, True)

        # map to full-image coords
        approx_global = approx.copy()
        approx_global[:, 0, 0] += x0
        approx_global[:, 0, 1] += y0

        if validate_cloud(approx_global, img_shape, skip_zones=skip_zones):
            results.append(approx_global)
            break  # best candidate accepted

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Stage-1 OpenCV detector (independent coverage source)
# ═══════════════════════════════════════════════════════════════════════════════

def stage1_detect(img_bgr: np.ndarray) -> List[np.ndarray]:
    """
    Run the OpenCV scalloped contour detector from stage1_cloud.py logic.
    This catches clouds Gemini localization misses. Returns list of contours
    in original image coords that pass the validation gate.
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, k)

    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_TC89_L1)

    H, W = img_bgr.shape[:2]
    results = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < 40_000 or area > VALIDATE_AREA_MAX_FRAC * H * W:
            continue
        s = scallopedness(c)
        if s < STAGE1_MIN_SCALLOP:
            continue
        approx = cv2.approxPolyDP(c, 2.0, True)
        if validate_cloud(approx, img_bgr.shape, min_scallop=1.10):
            results.append(approx)

    # Also try adaptive binary for faint clouds
    binary_adp = adaptive_binarize(gray)
    k_m = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    binary_adp = cv2.morphologyEx(binary_adp, cv2.MORPH_CLOSE, k_m)
    contours2, _ = cv2.findContours(binary_adp, cv2.RETR_LIST, cv2.CHAIN_APPROX_TC89_L1)
    for c in contours2:
        area = cv2.contourArea(c)
        if area < 40_000 or area > VALIDATE_AREA_MAX_FRAC * H * W:
            continue
        s = scallopedness(c)
        if s < STAGE1_MIN_SCALLOP:
            continue
        approx = cv2.approxPolyDP(c, 2.0, True)
        if validate_cloud(approx, img_bgr.shape, min_scallop=1.10):
            results.append(approx)

    log.info("Stage-1 OpenCV found %d raw candidates", len(results))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# NMS merge
# ═══════════════════════════════════════════════════════════════════════════════

def nms_polygons(polys: List[dict], img_shape: Tuple) -> List[dict]:
    """
    Greedy IoU-based NMS. polys is list of {contour, source, confidence}.
    Sorts by confidence desc, keeps non-overlapping.
    """
    polys_sorted = sorted(polys, key=lambda p: p.get("confidence", 0.5), reverse=True)
    kept = []
    for cand in polys_sorted:
        overlap = False
        for keep in kept:
            iou = polygon_iou(cand["contour"], keep["contour"], img_shape)
            if iou > NMS_IOU_THRESHOLD:
                overlap = True
                break
        if not overlap:
            kept.append(cand)
    return kept


# ═══════════════════════════════════════════════════════════════════════════════
# Nesting tag
# ═══════════════════════════════════════════════════════════════════════════════

def tag_nesting(polys: List[dict], img_shape: Tuple) -> List[dict]:
    """
    Mark each polygon as outer or inner.
    inner = containment fraction ≥ NEST_CONTAIN_FRAC within another polygon.
    """
    n = len(polys)
    is_inner = [False] * n
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            frac = containment_fraction(polys[i]["contour"], polys[j]["contour"], img_shape)
            if frac >= NEST_CONTAIN_FRAC:
                is_inner[i] = True
                break
    for i, p in enumerate(polys):
        p["tag"] = "inner" if is_inner[i] else "outer"
    return polys


# ═══════════════════════════════════════════════════════════════════════════════
# Output helpers
# ═══════════════════════════════════════════════════════════════════════════════

def draw_overlay(img_bgr: np.ndarray, polys: List[dict]) -> np.ndarray:
    overlay = img_bgr.copy()
    for p in polys:
        c = p["contour"]
        color = (0, 200, 0) if p.get("tag") == "outer" else (0, 220, 220)
        cv2.polylines(overlay, [c], True, color, 3)
        cx, cy = map(int, poly_centroid(c))
        label = f"{p.get('id', '?')} {p.get('source', '')[:4]}"
        cv2.putText(overlay, label, (cx - 20, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    return overlay


def build_mask(polys: List[dict], img_shape: Tuple) -> np.ndarray:
    H, W = img_shape[:2]
    mask = np.zeros((H, W), dtype=np.uint8)
    for p in polys:
        if p.get("tag") == "outer":
            cv2.fillPoly(mask, [p["contour"]], 255)
    return mask


def polys_to_json(polys: List[dict]) -> list:
    out = []
    for p in polys:
        c = p["contour"]
        pts = c.reshape(-1, 2).tolist()
        x, y, w, h = cv2.boundingRect(c)
        out.append({
            "id": p.get("id"),
            "tag": p.get("tag"),
            "source": p.get("source"),
            "confidence": round(p.get("confidence", 0.8), 3),
            "bbox": [x, y, x + w, y + h],
            "area": round(poly_area_px(c), 1),
            "scallopedness": round(scallopedness(c), 3),
            "polygon": pts,
        })
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def detect_all(img_path: str, api_key: Optional[str] = None,
               out_dir: str = "output_v2", debug: bool = False,
               use_cache: bool = True) -> dict:
    """
    Full pipeline. Returns dict with outer_count, inner_count, total_count, output files.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    debug_dir = out_path / "debug_crops" if debug else None
    cache_path = str(out_path / GEMINI_CACHE_NAME)

    img_bgr = cv2.imread(img_path)
    if img_bgr is None:
        raise FileNotFoundError(f"Cannot read image: {img_path}")
    H, W = img_bgr.shape[:2]
    log.info("Image: %dx%d  output: %s", W, H, out_dir)

    # [P0] Pre-process. img_clean has the outer drawing frame erased so the
    # deterministic detectors can't weld interior content onto the border (those
    # welds traced the left/top frame and swallowed the genuine left clouds).
    # Gemini localization and the overlay still use the original img_bgr.
    img_clean = remove_frame_border(img_bgr)
    gray = cv2.cvtColor(img_clean, cv2.COLOR_BGR2GRAY)
    binary_global = adaptive_binarize(gray)

    all_polys: List[dict] = []

    # [P1] Localize
    bboxes = []
    if api_key:
        try:
            bboxes = locate_with_gemini(img_bgr, api_key,
                                        cache_path=cache_path, use_cache=use_cache)
        except Exception as e:
            log.warning("Gemini localization error: %s — using fallback", e)

    if not bboxes:
        log.info("Using deterministic fallback locator")
        bboxes = locate_fallback(binary_global)

    # [P2] Per-crop boundary recovery
    log.info("Recovering boundaries from %d bboxes...", len(bboxes))
    # Gemini-localized boxes are trusted detections — skip the legend/notes/title
    # zone veto so genuine clouds near the bottom/right borders are not discarded.
    skip_zones = bool(api_key)
    for i, bbox in enumerate(bboxes):
        recovered = recover_from_crop(img_clean, bbox, img_bgr.shape, debug_dir, i,
                                      skip_zones=skip_zones)
        for poly in recovered:
            all_polys.append({
                "contour": poly,
                "source": "gemini_snap" if api_key else "fallback_snap",
                "confidence": bbox.get("confidence", 0.8),
            })

    # [P3] Stage-1 OpenCV detector (additional coverage)
    log.info("Running stage-1 OpenCV detector...")
    s1_polys = stage1_detect(img_clean)
    for poly in s1_polys:
        all_polys.append({
            "contour": poly,
            "source": "stage1_opencv",
            "confidence": scallopedness(poly) - 1.0,
        })

    log.info("Total before border filter: %d polygons", len(all_polys))

    # [PB] Border rejection — drawing frame / sheet boundary / title-block lines
    # Must run before NMS: a single border structure can absorb (via IoU) real clouds
    # sitting near the drawing frame, causing them to be dropped by NMS instead.
    border_vis_path = str(out_path / "border_filter.jpg")
    all_polys, border_removed = reject_borders(
        all_polys, img_bgr.shape,
        vis_path=border_vis_path,
        img_bgr=img_bgr,
    )
    log.info("After border filter: %d polygons  (%d borders removed)",
             len(all_polys), len(border_removed))

    # [P4] Merge & dedup
    if all_polys:
        kept = nms_polygons(all_polys, img_bgr.shape)
    else:
        kept = []
    log.info("After NMS: %d polygons", len(kept))

    # Assign IDs
    for i, p in enumerate(kept):
        p["id"] = i + 1

    # [P5] Nesting tag
    kept = tag_nesting(kept, img_bgr.shape)

    outer = [p for p in kept if p["tag"] == "outer"]
    inner = [p for p in kept if p["tag"] == "inner"]
    log.info("Result: %d outer + %d inner = %d total clouds", len(outer), len(inner), len(kept))

    # Write outputs
    overlay = draw_overlay(img_bgr, kept)
    overlay_path = str(out_path / "overlay_v2.jpg")
    cv2.imwrite(overlay_path, overlay, [cv2.IMWRITE_JPEG_QUALITY, 92])

    mask = build_mask(kept, img_bgr.shape)
    mask_path = str(out_path / "cloud_mask_v2.png")
    cv2.imwrite(mask_path, mask)

    json_data = polys_to_json(kept)
    json_path = str(out_path / "outer_clouds_v2.json")
    with open(json_path, "w") as f:
        json.dump({"clouds": json_data, "stats": {
            "total": len(kept), "outer": len(outer), "inner": len(inner),
            "image_size": [W, H],
        }}, f, indent=2)

    log.info("Saved: %s  %s  %s", overlay_path, mask_path, json_path)

    return {
        "total_count": len(kept),
        "outer_count": len(outer),
        "inner_count": len(inner),
        "overlay": overlay_path,
        "mask": mask_path,
        "json": json_path,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Combined 95%+ cloud detector")
    parser.add_argument("image", help="Path to P&ID drawing (JPG/PNG/TIFF)")
    parser.add_argument("--out", default="output_v2", help="Output directory")
    parser.add_argument("--debug", action="store_true", help="Save per-crop debug images")
    parser.add_argument("--no-gemini", action="store_true", help="Skip Gemini, use deterministic only")
    parser.add_argument("--api-key", help="Gemini API key (overrides env)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Bypass the Gemini response cache (force a fresh API call)")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="Delete any cached Gemini response before running")
    args = parser.parse_args()

    if args.refresh_cache:
        cp = Path(args.out) / GEMINI_CACHE_NAME
        if cp.exists():
            cp.unlink()
            log.info("Removed Gemini cache: %s", cp)

    api_key = None
    if not args.no_gemini:
        api_key = (args.api_key
                   or os.environ.get("GEMINI_API_KEY")
                   or os.environ.get("GOOGLE_API_KEY"))
        if not api_key:
            log.warning("No GEMINI_API_KEY found — using deterministic fallback")

    result = detect_all(args.image, api_key=api_key, out_dir=args.out, debug=args.debug,
                        use_cache=not args.no_cache)
    print(f"\n=== Detection complete ===")
    print(f"  Outer clouds: {result['outer_count']}")
    print(f"  Inner clouds: {result['inner_count']}")
    print(f"  Total:        {result['total_count']}")
    print(f"  Overlay:      {result['overlay']}")
    print(f"  Mask:         {result['mask']}")
    print(f"  JSON:         {result['json']}")


if __name__ == "__main__":
    main()
