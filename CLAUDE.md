# CDCI P&ID Tag Extraction Pipeline
## Project Memory for Claude Code

> **ALL commands and architecture details are in [`PIPELINE_RUNBOOK.md`](PIPELINE_RUNBOOK.md).**
> Read that file first before doing anything. This file is Claude Code context only.

---

## What This Project Does

Automated system that reads engineering P&ID drawings (Piping and Instrumentation Diagrams)
and extracts every equipment tag (like FIT-1001, V-BV-2246, PT-201) into a structured
Excel register matching the client's Annexure-4 format.

- Input: scanned JPG/PDF engineering drawing
- Output: `output/final_tags.xlsx` with AUTO_ACCEPT, HUMAN_REVIEW, SUMMARY sheets
- Time: under 10 minutes per drawing
- Tech: Gemini AI + Tesseract OCR + OpenCV

---

## Active Files

- **`stages/`** — all pipeline scripts (current, use these)
- **`core/`** — shared utilities imported by stages (gemini_client, isa_decode, etc.)
- **`settings.py`** — model names and thresholds (imported by core/; do not delete)
- **`output/drawing_context.json`** — shared state spine; every step reads/writes this
- **Input data:** `input_drawing.jpg`, `ANNEXURE-2_*.xlsx`, `ANNEXURE-4_*.xlsx`

---

## Key Architecture Points

- **`drawing_context.json` is the spine** — never delete mid-pipeline
- **step5a auto-detects cloud regions** — if `output/outer_clouds_v2.json` exists (written by step2b), cloud-filter mode activates and only tags inside cloud boundaries are kept. If the file is absent, full-drawing extraction runs.
- **Active extraction script:** `stages/step5a_candidate_extraction.py` (not `step5a_live_annotator.py`)
- **Active notes agent:** `stages/step3_notes_agent.py` (not any root-level copy)
- **`core/gemini_client.py` and `core/confidence.py` import `settings.py`** — keep settings.py

---

## Models Used

| Model | Steps | Purpose |
|-------|-------|---------|
| gemini-3.1-pro-preview | 5A, 2B | Symbol detection, cloud localization |
| gemini-2.5-flash | 2, 3, 4, 6 | Structured extraction, title block, tables |
| Tesseract OCR | 2, 3, 5A, 6 | Deterministic text — ground truth for tag chars |
| OpenCV | all | CLAHE, line detection, morphology, drawing |

---

## Actual Results on Test Drawing

```
Drawing:           4224-MGDV-6-50-2004  Sheet 001  Rev C
Revision mode:     CLOUD_SCOPE_MODE (but step5a runs FULL_EXTRACTION for recall)
Clouds detected:   per step2b output (outer_clouds_v2.json)
Notes extracted:   62 unique, 8 abbreviations
SOW loaded:        100 ALLOW + 32 BLOCK symbols
Step 5A output:    226 candidates
Register recall:   44/46 Annexure-4 tags (2 not drawn on sheet)
AUTO_ACCEPT:       160 (76.6%)
AUTO_REJECT:       0
```

---

## What NOT to Do

- Do NOT use `step5a_live_annotator.py` for pipeline runs — use `step5a_candidate_extraction.py`
- Do NOT use any root-level step3 copy — use `stages/step3_notes_agent.py`
- Do NOT delete `output/drawing_context.json` mid-pipeline
- Do NOT run step5a before step2 (needs `raster_path` in context)
- Do NOT skip step6 before step5c (`master_tags.json` feeds ISA validation)
- Do NOT use `--workers > 1` on free-tier Gemini keys (429 rate limit)
- Do NOT modify `stages/step2b_cloud_detection.py` internals
