"""Central task-to-model routing defaults for the existing script agents.

Each stage reads its task-specific environment variable at import time. Setting
CDCI_MODEL_* before a run allows model policy to change without editing prompts.
"""
from __future__ import annotations

from typing import MutableMapping

MODEL_DEFAULTS = {
    "CDCI_MODEL_CLASSIFY": "gemini-2.5-flash-lite",
    "CDCI_MODEL_TITLE": "gemini-2.5-flash",
    "CDCI_MODEL_CLOUD": "gemini-3.1-pro-preview",
    "CDCI_MODEL_NOTES": "gemini-2.5-flash",
    "CDCI_MODEL_SOW": "gemini-2.5-flash",
    "CDCI_MODEL_EXTRACTION": "gemini-3.1-pro-preview",
    "CDCI_MODEL_TABLE": "gemini-2.5-flash",
    "CDCI_MODEL_TABLE_FALLBACK": "gemini-2.5-pro",
}

STAGE_MODEL_KEY = {
    "1_format_detection": "CDCI_MODEL_CLASSIFY",
    "2_title_revision": "CDCI_MODEL_TITLE",
    "3_revision_clouds": "CDCI_MODEL_CLOUD",
    "4_notes_context": "CDCI_MODEL_NOTES",
    "7_full_sheet_candidates": "CDCI_MODEL_EXTRACTION",
    "7b_cloud_scope_candidates": "CDCI_MODEL_EXTRACTION",
    "6_table_extraction": "CDCI_MODEL_TABLE",
}


def apply_model_routing(env: MutableMapping[str, str], stage: str) -> dict[str, str]:
    """Populate safe defaults, preserve operator overrides, and return route metadata."""
    for key, value in MODEL_DEFAULTS.items():
        env.setdefault(key, value)
    key = STAGE_MODEL_KEY.get(stage)
    selected = env.get(key, "") if key else ""
    return {"model_key": key or "deterministic", "model": selected or "none"}
