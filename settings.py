"""
CDCI Tag Extraction Engine — Configuration
============================================
All tunable parameters live here. Modify these values without touching code.
"""
from pathlib import Path

# ── Gemini API ──────────────────────────────────────────────────────
# Text/Vision model for OCR and structured detection
GEMINI_MODEL          = "gemini-2.5-pro"
GEMINI_DELAY_SEC      = 3.0          # seconds between API calls
GEMINI_MAX_RETRIES    = 2
GEMINI_THINKING_TOKENS= 1024         # cap thinking to prevent empty responses

# ── Image Tiling ────────────────────────────────────────────────────
TILE_SIZE             = 2500         # px per tile side
TILE_OVERLAP          = 350          # px overlap between adjacent tiles

# ── Phase 1: GREEN Cloud Detection (HSV thresholds) ─────────────────
# Tuned for common highlighter/marker green colors.
# Adjust if your green markings use a different shade.
GREEN_HSV_LOW         = (35,  40,  40)    # H, S, V lower bound
GREEN_HSV_HIGH        = (85, 255, 255)    # H, S, V upper bound

CLOUD_MIN_AREA_PX     = 5000         # minimum polygon area to be a cloud
CLOUD_MORPH_KERNEL    = 7            # kernel size for closing gaps in marker strokes
CLOUD_DILATE_PX       = 15           # padding around cloud polygon (extends scope slightly)
CLOUD_POLY_EPSILON    = 0.002        # Douglas-Peucker simplification factor

# ── Phase 2-3: Detection Exclusion Zones (relative to image WxH) ────
# These zones are ALWAYS excluded even within cloud scope:
#   notes block (bottom-left), title block (bottom-right), reference text (right)
EXCL_NOTES_Y_FRAC     = 0.60         # notes start at 60% down
EXCL_NOTES_X_FRAC     = 0.52         # notes are in left 52%
EXCL_TITLE_Y_FRAC     = 0.80         # title block at bottom 20%
EXCL_REF_X_FRAC       = 0.83         # reference text column at right 17%

# ── Phase 8: Confidence Scoring Weights (per architecture L15) ──────
# C_final = w_det*c_det + w_ocr*c_ocr + w_geo*c_geo + w_val*c_val + w_reg*c_reg
CONF_W_DET            = 0.25
CONF_W_OCR            = 0.30
CONF_W_GEO            = 0.15
CONF_W_VAL            = 0.20
CONF_W_REG            = 0.10
CONF_AUTO_ACCEPT      = 0.85
CONF_REVIEW_THRESHOLD = 0.60

# ── Phase 8: Reference text rejection (regex patterns) ──────────────
# Tags matching these patterns are cross-references from OTHER drawings,
# not actual tags on this P&ID. They get rejected during validation.
REFERENCE_TEXT_PATTERNS = [
    r'^(V-)?PIC-\d{3,4}$',         r'^(V-)?SC-\d{3}$',
    r'^(V-)?FC-\d{3}$',            r'^(V-)?FT-246[A-Z]?$',
    r'^(V-)?RCI-',                 r'^(V-)?LC-201[A-Z]$',
    r'^(V-)?HS-14\d{2}[A-Z]?$',    r'^(V-)?RST-\d{3}$',
    r'^(V-)?XS-2[3-7]\d$',         r'^(V-)?XA-2\d{2}$',
    r'^(V-)?PIC-1\d{3}[A-Z]?$',    r'^(V-)?RC-201$',
    r'^(V-)?PC-004[A-Z]?$',        r'^(V-)?Y4-109$',
    r'^(V-)?Y2-207$',
]

# ── Excel Output ────────────────────────────────────────────────────
EXCEL_HEADERS = [
    "S.NO", "DISCIPLINE", "TAG NUMBER", "TAG DESCRIPTION",
    "EQUIPMENT DESCRIPTION", "SIZE&RATING", "DOCUMENT NUMBER",
    "SHEET NO", "REV", "DRAWING REFERENCE", "DOCUMENT TITLE",
    "DOC STATUS", "DATE", "DUPLICATE STATUS", "REMARKS"
]
EXCEL_COL_WIDTHS = [6, 18, 22, 52, 42, 18, 24, 8, 5, 30, 42, 42, 12, 16, 20]

# ── Debug & Output Directories ──────────────────────────────────────
DEBUG_DIR             = Path("debug")          # green mask, cloud overlay, etc.
SAVE_DEBUG_IMAGES     = True
