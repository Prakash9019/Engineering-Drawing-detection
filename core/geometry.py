"""
Geometric Reasoning Utilities
==============================
Polygon operations, IoU, point-in-polygon, IoU-based deduplication.
"""
import math
from typing import List, Tuple

import cv2
import numpy as np


def iou(box_a: List[int], box_b: List[int]) -> float:
    """Compute IoU between two boxes [x0,y0,x1,y1]."""
    ax0, ay0, ax1, ay1 = box_a
    bx0, by0, bx1, by1 = box_b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0


def box_center(box: List[int]) -> Tuple[float, float]:
    """Return (cx, cy) center of a box."""
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def box_area(box: List[int]) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def distance(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    return math.sqrt((p1[0]-p2[0])**2 + (p1[1]-p2[1])**2)


def point_in_mask(point: Tuple[float, float], mask: np.ndarray) -> bool:
    """Check if a point falls within a non-zero region of the binary mask."""
    if mask is None:
        return True  # no mask = scope is full image
    H, W = mask.shape[:2]
    x, y = int(point[0]), int(point[1])
    if x < 0 or y < 0 or x >= W or y >= H:
        return False
    return mask[y, x] > 0


def box_overlaps_mask(box: List[int], mask: np.ndarray, min_frac: float = 0.10) -> bool:
    """
    Check if a box overlaps the mask region by at least min_frac of the box area.

    Used to decide whether a tile is worth processing (any cloud coverage).
    """
    if mask is None:
        return True
    H, W = mask.shape[:2]
    x0, y0 = max(0, box[0]), max(0, box[1])
    x1, y1 = min(W, box[2]), min(H, box[3])
    if x0 >= x1 or y0 >= y1:
        return False
    roi = mask[y0:y1, x0:x1]
    if roi.size == 0:
        return False
    coverage = (roi > 0).mean()
    return coverage >= min_frac


def dedup_by_iou_and_label(
    detections: List[dict],
    iou_threshold: float = 0.4,
    center_threshold_px: int = 80,
) -> List[dict]:
    """
    Deduplicate detections using IoU AND label+center proximity.

    Same-label tags whose centers are within `center_threshold_px` are treated
    as duplicates (overlapping tiles often produce these).
    """
    if not detections:
        return []

    # Sort by area ascending — prefer smaller/tighter boxes
    sorted_dets = sorted(detections, key=lambda d: box_area(d.get('box', [0,0,0,0])))
    keep = []

    for det in sorted_dets:
        is_dup = False
        d_box = det.get('box', [0,0,0,0])
        d_label = det.get('label', '')
        d_cx, d_cy = box_center(d_box)

        for kept in keep:
            k_box = kept.get('box', [0,0,0,0])
            k_label = kept.get('label', '')
            k_cx, k_cy = box_center(k_box)

            # IoU overlap
            if iou(d_box, k_box) > iou_threshold:
                is_dup = True
                break

            # Same label + close centers
            if (d_label == k_label and
                    abs(d_cx - k_cx) < center_threshold_px and
                    abs(d_cy - k_cy) < center_threshold_px):
                is_dup = True
                break

        if not is_dup:
            keep.append(det)

    return keep


def contour_to_polygon(contour, epsilon_frac: float = 0.002) -> np.ndarray:
    """
    Simplify a contour to a polygon using Douglas-Peucker.

    Returns Nx2 array of (x,y) vertices.
    """
    peri = cv2.arcLength(contour, True)
    epsilon = epsilon_frac * peri
    approx = cv2.approxPolyDP(contour, epsilon, True)
    return approx.reshape(-1, 2)


def make_polygon_mask(
    image_shape: Tuple[int, int],
    polygons: List[np.ndarray],
    dilate_px: int = 0,
) -> np.ndarray:
    """
    Fill a binary mask from a list of polygons.

    Args:
        image_shape: (H, W) of the target mask
        polygons: list of Nx2 (x,y) arrays
        dilate_px: optional dilation in pixels (for tolerance padding)

    Returns:
        uint8 binary mask, 255 inside polygons, 0 outside
    """
    H, W = image_shape[:2]
    mask = np.zeros((H, W), dtype=np.uint8)
    for poly in polygons:
        if poly is not None and len(poly) >= 3:
            cv2.fillPoly(mask, [poly.astype(np.int32)], 255)
    if dilate_px > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate_px*2+1, dilate_px*2+1))
        mask = cv2.dilate(mask, kernel)
    return mask
