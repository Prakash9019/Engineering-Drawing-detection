"""
Confidence Scoring (Architecture Layer 15)
============================================
Implements the multi-factor confidence formula and routing thresholds.

C_final = w_det*C_det + w_ocr*C_ocr + w_geo*C_geo + w_val*C_val + w_reg*C_reg

Routing:
  C_final >= 0.85           → AUTO_ACCEPT
  0.60 <= C_final < 0.85    → REVIEW_REQUIRED
  C_final < 0.60            → AUTO_REJECT
"""
from settings import (
    CONF_W_DET, CONF_W_OCR, CONF_W_GEO, CONF_W_VAL, CONF_W_REG,
    CONF_AUTO_ACCEPT, CONF_REVIEW_THRESHOLD,
)


# Map qualitative confidence labels (from Gemini) to numeric scores
CONF_LABEL_MAP = {
    'HIGH': 0.95,
    'MEDIUM': 0.75,
    'LOW': 0.50,
}


def label_to_score(label: str) -> float:
    """Convert HIGH/MEDIUM/LOW label to numeric confidence."""
    return CONF_LABEL_MAP.get(str(label).upper().strip(), 0.75)


def calc_final(
    c_det: float = 0.85,
    c_ocr: float = 0.85,
    c_geo: float = 0.85,
    c_val: float = 1.0,
    c_reg: float = 0.5,
) -> float:
    """
    Compute weighted final confidence per architecture formula.

    Args:
        c_det: Detection model confidence (Gemini detection result)
        c_ocr: OCR/text reading confidence
        c_geo: Geometric reasoning confidence (symbol-tag association)
        c_val: Validation pass score (1.0 if all rules pass)
        c_reg: Register lookup confidence (0.5 default — no register available)

    Returns:
        Final confidence in [0, 1]
    """
    score = (CONF_W_DET * c_det +
             CONF_W_OCR * c_ocr +
             CONF_W_GEO * c_geo +
             CONF_W_VAL * c_val +
             CONF_W_REG * c_reg)
    return round(max(0.0, min(1.0, score)), 3)


def route(c_final: float) -> str:
    """Determine routing decision based on final confidence."""
    if c_final >= CONF_AUTO_ACCEPT:
        return 'AUTO_ACCEPT'
    if c_final >= CONF_REVIEW_THRESHOLD:
        return 'REVIEW_REQUIRED'
    return 'AUTO_REJECT'
