"""
Stage 4: Engineering Symbol Detection (Architecture Layer 7)
==============================================================
Tiled Gemini detection of engineering symbols + their associated tags.

Key features:
  - Tiled processing (handles drawings >8000px)
  - CLOUD SCOPE FILTERING: only detections within the green cloud mask are kept
  - Exclusion zones (notes, title block, reference text margins)
  - Reference-text pattern rejection
  - IoU + label-proximity deduplication

Architecture priority:
  1. Symbol detection (find the visual symbol)
  2. Symbol classification (circle/diamond/valve/equipment)
  3. Tag association (read the tag text near the symbol)
"""
import logging
import math
import re
import time
from typing import List, Optional

import cv2
import numpy as np

from settings import (
    TILE_SIZE, TILE_OVERLAP, GEMINI_DELAY_SEC,
    EXCL_NOTES_Y_FRAC, EXCL_NOTES_X_FRAC,
    EXCL_TITLE_Y_FRAC, EXCL_REF_X_FRAC,
)
from core.gemini_client import GeminiClient
from core.json_parser import parse_json
from core.geometry import (
    box_center, dedup_by_iou_and_label, box_overlaps_mask, point_in_mask
)

log = logging.getLogger(__name__)


DETECTION_PROMPT = """Analyze this P&ID engineering drawing tile.
Find every engineering object that has BOTH a visual symbol AND a tag label.

SYMBOLS TO DETECT (with their associated tags):

INSTRUMENTS (priority on symbol + tag pairing):
- Instrument circles/bubbles → PIT-211, TAHH-212, FE-224, TIT-212, FIT-207, etc.
- Double instrument bubbles → TI-211 (local indicator)
- Logic/control diamonds → I-001, I-004, PAHH-211, PALL-214

VALVES (symbol + tag pairing required):
- Ball valves (BV-) → V-BV-2243, V-BV-2241
- Gate valves (GV-) → V-GV-911, V-GV-915
- Check valves (NRV-) → V-NRV-748
- Relief valves (RV-) → V-RV-207, V-RV-208
- Control valves (FV-, XV-, LV-) → V-FV-208, V-XV-203, V-LV-206
- Solenoid valves (FY-, XY-, ZY-)

EQUIPMENT (large outlined symbols with tags):
- Compressors → K-V-201
- Vessels/Drums → V-V-201
- Gearboxes/Motors → KG-V-201, KM-V-201
- Strainers → S-V-204

THERMOWELL & ELEMENTS:
- Thermowell (TW-) → TW-211, TW-212
- Temperature elements (TE-) → TE-211, TE-212
- Flow elements (FE-) → FE-224

PIPING (line numbers with engineering format):
- 2"-ETH-V057-61440X, 12"-ETH-V012-61440X-PP, etc.

DO NOT DETECT (these are false positives):
- Cross-reference text: "FROM V-PIC-1446", "TO V-Y4-109", "FROM V-SC5-003"
- Drawing numbers: "4224-MGDV-...", "MGDV-..."
- Reference text in margins: "SPEED CONTROL SIGNAL", "CONTROLLER OUTPUT", "LOAD SHARING"
- Notes, legends, abbreviations, title block content
- Dimension text, border grid references

Return ONLY a JSON array:
[
  {"label": "PIT-211", "box": [x_min, y_min, x_max, y_max], "symbol_type": "circle"},
  {"label": "V-BV-2243", "box": [x_min, y_min, x_max, y_max], "symbol_type": "ball_valve"}
]

box = pixel coordinates from top-left of this tile.
Bounding box MUST include: symbol + tag text + leader line connecting them.
symbol_type: circle | double_circle | diamond | ball_valve | gate_valve | check_valve | relief_valve | control_valve | solenoid | equipment | thermowell | element | piping

If nothing found, return: []"""


def _tile_in_scope(
    tile_box: List[int],
    mask: Optional[np.ndarray],
) -> bool:
    """Check if a tile overlaps the cloud scope mask at all."""
    if mask is None:
        return True
    return box_overlaps_mask(tile_box, mask, min_frac=0.02)


def _det_in_scope(
    det_box: List[int],
    mask: Optional[np.ndarray],
) -> bool:
    """Check if a detection's CENTROID falls within the cloud scope."""
    if mask is None:
        return True
    cx, cy = box_center(det_box)
    return point_in_mask((cx, cy), mask)


def _det_in_exclusion_zone(
    det_box: List[int],
    image_shape: tuple,
) -> bool:
    """Check if detection is in notes/title-block/ref-text exclusion zone."""
    H, W = image_shape[:2]
    cx, cy = box_center(det_box)
    gcx, gcy = cx / W, cy / H

    if gcy > EXCL_NOTES_Y_FRAC and gcx < EXCL_NOTES_X_FRAC:
        return True  # notes area
    if gcy > EXCL_TITLE_Y_FRAC:
        return True  # title block band
    if gcx > EXCL_REF_X_FRAC:
        return True  # reference text margin
    return False


def _is_garbage_label(label: str) -> bool:
    """Reject obvious non-tag strings."""
    if not label or len(label) < 3:
        return True
    if re.match(r'^(NOTE|FROM|TO)\b', label, re.I):
        return True
    if any(x in label.upper() for x in ['4224', 'MGDV', 'MCDTY', 'MGDY']):
        return True
    if re.match(r'^\d+$', label):
        return True
    return False


def detect_symbols(
    image: np.ndarray,
    gemini: GeminiClient,
    scope_mask: Optional[np.ndarray] = None,
) -> List[dict]:
    """
    Run tiled symbol+tag detection across the drawing.

    Args:
        image: BGR full-resolution drawing
        gemini: GeminiClient instance
        scope_mask: Binary mask from cloud detection (None = full scope)

    Returns:
        List of detection dicts: [{label, box, symbol_type}, ...]
        where box is in FULL-IMAGE pixel coordinates.
    """
    H, W = image.shape[:2]
    step = TILE_SIZE - TILE_OVERLAP
    rows = max(1, math.ceil((H - TILE_OVERLAP) / step))
    cols = max(1, math.ceil((W - TILE_OVERLAP) / step))

    has_scope = scope_mask is not None
    log.info(f"  Tiling {W}x{H} → {rows*cols} tiles ({cols} cols × {rows} rows)")
    log.info(f"  Cloud scope: {'ACTIVE' if has_scope else 'FULL DRAWING'}")

    all_dets = []
    tile_num = 0
    total = rows * cols
    skipped_oos = 0
    skipped_excl = 0

    for r in range(rows):
        for c in range(cols):
            y0 = min(r * step, max(0, H - TILE_SIZE))
            x0 = min(c * step, max(0, W - TILE_SIZE))
            y1 = min(y0 + TILE_SIZE, H)
            x1 = min(x0 + TILE_SIZE, W)
            tile_box = [x0, y0, x1, y1]

            # Skip tiles fully in exclusion zones
            tcx, tcy = (x0 + x1) / 2 / W, (y0 + y1) / 2 / H
            if (tcy > EXCL_NOTES_Y_FRAC and tcx < EXCL_NOTES_X_FRAC) or \
               (tcy > EXCL_TITLE_Y_FRAC and tcx > EXCL_NOTES_X_FRAC):
                log.info(f"  [skip excl-zone] R{r}C{c}")
                skipped_excl += 1
                continue

            # Skip tiles entirely outside cloud scope
            if not _tile_in_scope(tile_box, scope_mask):
                log.info(f"  [skip out-of-scope] R{r}C{c}")
                skipped_oos += 1
                continue

            tile_num += 1
            log.info(f"  [tile {tile_num}/{total}] R{r}C{c}")
            tile = image[y0:y1, x0:x1]
            tw, th = x1 - x0, y1 - y0

            raw = gemini.ask(DETECTION_PROMPT, tile)
            items = parse_json(raw)

            if not isinstance(items, list):
                log.warning(f"    R{r}C{c}: no valid JSON returned")
                time.sleep(GEMINI_DELAY_SEC)
                continue

            tile_kept = 0
            for it in items:
                if not isinstance(it, dict):
                    continue
                label = str(it.get('label', '')).strip()
                box = it.get('box', [])
                sym = str(it.get('symbol_type', '')).strip().lower()

                if not label or len(box) != 4:
                    continue
                try:
                    coords = [int(float(v)) for v in box]
                except (ValueError, TypeError):
                    continue
                bx0, by0, bx1, by1 = coords

                # Handle Gemini's 0-1000 normalized [y_min,x_min,y_max,x_max] format
                if all(0 <= v <= 1000 for v in coords) and (tw > 1000 or th > 1000):
                    yn0, xn0, yn1, xn1 = coords
                    bx0 = int(xn0 * tw / 1000)
                    by0 = int(yn0 * th / 1000)
                    bx1 = int(xn1 * tw / 1000)
                    by1 = int(yn1 * th / 1000)

                # Translate to global image coordinates
                gx0, gy0 = bx0 + x0, by0 + y0
                gx1, gy1 = bx1 + x0, by1 + y0
                global_box = [gx0, gy0, gx1, gy1]

                # Filter: garbage labels
                if _is_garbage_label(label):
                    continue

                # Filter: exclusion zones
                if _det_in_exclusion_zone(global_box, (H, W)):
                    continue

                # CRITICAL: Filter by cloud scope (architecture L6)
                if not _det_in_scope(global_box, scope_mask):
                    continue

                all_dets.append({
                    'label': label,
                    'box': global_box,
                    'symbol_type': sym,
                })
                tile_kept += 1

            log.info(f"    R{r}C{c}: {tile_kept} valid detections")
            time.sleep(GEMINI_DELAY_SEC)

    log.info(f"  Detection raw: {len(all_dets)}")
    log.info(f"  Tiles processed: {tile_num}, skipped exclusion: {skipped_excl}, "
             f"skipped out-of-scope: {skipped_oos}")

    # Dedup across overlapping tiles
    deduped = dedup_by_iou_and_label(all_dets, iou_threshold=0.4, center_threshold_px=80)
    log.info(f"  After dedup: {len(deduped)}")

    return deduped
