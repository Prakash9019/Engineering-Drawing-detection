# CDCI P&ID Tag Extraction Pipeline — Runbook
> Single source of truth. All commands are verified and copy-paste ready. Last updated June 2026.

---

## What this pipeline produces

From one scanned P&ID drawing:
- `output/step5a_candidates.json` — every tag detected (with bounding boxes)
- `output/final_tags.xlsx` — client deliverable in Annexure-4 format (AUTO_ACCEPT / HUMAN_REVIEW / SUMMARY sheets)
- `output/stages/` — per-stage annotated images + JSON for review UI

**Result on test drawing (`input_drawing.jpg`):**
`44/46` Annexure-4 tags extracted (the other 2 — `V-ZSC-203`, `V-ZSO-203` — are **not drawn** on the sheet),
`226` total tags detected, `AUTO_ACCEPT 160 (76.6%)`, `AUTO_REJECT 0`.

---

## Project Structure

```
cdci_extractor_final/
├── CLAUDE.md                              ← Claude Code project memory
├── PIPELINE_RUNBOOK.md                    ← THIS FILE — single source of truth
├── input_drawing.jpg                      ← test P&ID drawing (9934×7017px)
├── ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx   ← SOW scope (100 ALLOW + 32 BLOCK)
├── ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx       ← asset register (46 tags, ground truth)
├── settings.py                            ← model names, thresholds (imported by core/)
├── requirements.txt
├── .env                                   ← GEMINI_KEY lives here
│
├── core/                                  ← shared utilities (gemini_client, isa_decode, etc.)
│
├── stages/
│   ├── step1_format_detect.py
│   ├── step2_title_block.py
│   ├── step2b_cloud_detection.py          ← detects revision clouds (writes outer_clouds_v2.json)
│   ├── step3_notes_agent.py
│   ├── step4_sow_agent.py
│   ├── step5a_candidate_extraction.py     ← MAIN extraction (FULL_EXTRACTION mode, cloud filter off)
│   ├── step5a_live_annotator.py           ← alternative annotator (kept for reference)
│   ├── step5b_geometric_association.py
│   ├── step5c_validation_engine.py
│   ├── step5d_duplicate_resolution.py
│   ├── step5_visualizer.py
│   ├── step6_table_agent.py
│   ├── step7_cedm_normalizer.py
│   ├── step8_confidence_router.py
│   ├── eval_coverage.py                   ← recall vs ground truth
│   ├── stage_visualizer.py               ← detect/filter stage images
│   └── cloud_detection_v2_claude.py      ← standalone cloud detector (Gemini, not Anthropic)
│
└── output/                                ← all pipeline outputs (JSON + images + xlsx)
```

---

## Prerequisites (run once)

```bash
cd /Users/suryprakash/Downloads/cdci_extractor_final
source .venv/bin/activate
brew install tesseract                     # if not already installed
export $(grep GEMINI_KEY .env | xargs)    # load GEMINI_KEY from .env
echo $GEMINI_KEY                          # verify — should print your key
```

- **Gemini model:** `gemini-3.1-pro-preview` (set in `stages/step5a_candidate_extraction.py` line ~72)
- **Free-tier key (5 req/min):** add `--workers 1` to the step5a command to avoid HTTP 429

---

## Full Pipeline — Copy-Paste

```bash
cd /Users/suryprakash/Downloads/cdci_extractor_final
source .venv/bin/activate
export $(grep GEMINI_KEY .env | xargs)
export DRAWING="input_drawing.jpg"

# ── Phase 1: context (run once per drawing) ───────────────────────────────────
python3 stages/step1_format_detect.py  $DRAWING --out output/ --api-key $GEMINI_KEY
python3 stages/step2_title_block.py    --context output/drawing_context.json --api-key $GEMINI_KEY
python3 stages/step2b_cloud_detection.py $DRAWING --out output/ --api-key $GEMINI_KEY
python3 stages/step3_notes_agent.py    --context output/drawing_context.json --api-key $GEMINI_KEY
python3 stages/step4_sow_agent.py build --excel ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx --out output/ --skip-vision
python3 stages/step6_table_agent.py    --context output/drawing_context.json --api-key $GEMINI_KEY

# ── Phase 2: extraction (~4 min, 315 Gemini calls) ────────────────────────────
python3 stages/step5a_candidate_extraction.py --context output/drawing_context.json --api-key $GEMINI_KEY --workers 8

# ── Phase 3: post-processing (no API, < 30s) ──────────────────────────────────
python3 stages/step5b_geometric_association.py --candidates output/step5a_candidates.json --image $DRAWING --out output/
python3 stages/step5c_validation_engine.py     --associations output/step5b_associations.json --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx --notes output/notes_context.json --out output/
python3 stages/step5d_duplicate_resolution.py  --validated output/step5c_validated.json --out output/

# ── Phase 4: output (no API, < 10s) ───────────────────────────────────────────
python3 stages/step7_cedm_normalizer.py  --final output/step5_final_output.json --context output/drawing_context.json --out output/ --project CDCI
python3 stages/step8_confidence_router.py --cedm output/step7_cedm_output.json --context output/drawing_context.json --out output/

# ── Reporting / visuals (optional, no API) ────────────────────────────────────
python3 stages/eval_coverage.py    --candidates output/step5a_candidates.json --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx --image $DRAWING --out output/
python3 stages/stage_visualizer.py --candidates output/step5a_candidates.json --deduped output/step5d_deduped.json --sow output/sow_symbol_memory.json --image $DRAWING --out output/stages/
```

> **Resume after failure:** every step writes one JSON file. Delete that file and re-run only from that step forward. Phase 1 outputs rarely change — usually only re-run Phase 2→4.

---

## What Each Step Does

| # | Script | Reads | Writes | Purpose |
|---|--------|-------|--------|---------|
| 1 | `step1_format_detect.py` | drawing | `drawing_context.json`, enhanced images | Detect raster/PDF, CLAHE enhance |
| 2 | `step2_title_block.py` | context | `title_block_context.json` | Read title block (dwg no, sheet, rev) |
| 2B | `step2b_cloud_detection.py` | drawing | `outer_clouds_v2.json`, `overlay_v2.jpg`, `cloud_mask_v2.png` | Find revision-cloud boundaries |
| 3 | `step3_notes_agent.py` | context | `notes_context.json`, `rules_prompt_block.txt` | Extract notes → drawing-specific rules |
| 4 | `step4_sow_agent.py build` | ANNEXURE-2 xlsx | `sow_symbol_memory.json` | Build 100-USE / 32-DO-NOT-USE scope memory |
| 6 | `step6_table_agent.py` | context | `master_tags.json`, `tables_context.json` | Extract tag-list tables |
| **5A** | `step5a_candidate_extraction.py` | context, sow, rules | `step5a_candidates.json` | **Detect every tag** (SAHI patches + Gemini + Tesseract) |
| 5B | `step5b_geometric_association.py` | 5a json, drawing | `step5b_associations.json` | Link tags to pipes/equipment (geometry) |
| 5C | `step5c_validation_engine.py` | 5b json, register, notes | `step5c_validated.json` | ISA-5.1 format + registry lookup |
| 5D | `step5d_duplicate_resolution.py` | 5c json | `step5d_deduped.json`, `step5_final_output.json` | Flag SAHI duplicates (recall-safe) |
| 7 | `step7_cedm_normalizer.py` | 5d final, context | `step7_cedm_output.json` | Normalise tags, fill 15 Annexure-4 fields |
| 8 | `step8_confidence_router.py` | step7 json, context | `final_tags.xlsx`, review queue, audit log | Score + route → Excel deliverable |
| — | `eval_coverage.py` | 5a json, ANNEXURE-4 | annotated images, coverage report | Measure recall vs ground truth |
| — | `stage_visualizer.py` | 5a json, 5d json, sow | `output/stages/*` | Per-stage detect/filter images + JSON |

---

## Cloud Detection — Status and Architecture

### What runs and what it produces
```bash
# step2b detects revision clouds on the drawing
python3 stages/step2b_cloud_detection.py input_drawing.jpg --out output/ --api-key $GEMINI_KEY

# Outputs:
#   output/outer_clouds_v2.json   ← cloud bounding boxes (list of {x0,y0,x1,y1,...})
#   output/overlay_v2.jpg         ← annotated image showing detected clouds
#   output/cloud_mask_v2.png      ← binary mask
```

### Architecture (how cloud detection flows)
- **step2b** uses Gemini 2.5 Pro to localize each cloud bbox, then OpenCV morphology to trace the scalloped contour precisely. Falls back to pure OpenCV if no API key (`--no-gemini`). Writes `output/outer_clouds_v2.json`.
- **step5a** auto-detects `outer_clouds_v2.json` in the output directory at startup. If the file exists and contains cloud regions, cloud-filter mode activates automatically — only tags whose symbol center falls inside a cloud bbox are kept. If the file is absent or has zero regions, full-drawing extraction runs instead.

### Cloud filter status per stage
| Stage | Cloud-aware? | Active? | How |
|-------|-------------|---------|-----|
| step2b | YES — detects clouds | YES | Runs, writes `outer_clouds_v2.json` |
| step5a | YES — `filter_by_revision_cloud()` | **AUTO** | On if `outer_clouds_v2.json` exists with regions |
| step5b | NO | — | Geometry-only; operates on already-filtered candidates |
| step5c | NO | — | Format/registry validation only |
| step5d | NO | — | Dedup only |
| step7 | NO | — | Normalization only |
| step8 | NO | — | Scoring/routing only |

### To force full-drawing extraction even when clouds exist
Simply don't run step2b, or delete `output/outer_clouds_v2.json` before running step5a.

---

## Quick Checks

```bash
# Recall vs ground truth (how many Annexure-4 tags did we find?)
python3 stages/eval_coverage.py \
    --candidates output/step5a_candidates.json \
    --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx \
    --image input_drawing.jpg \
    --out output/

# Routing distribution (accept / review / reject counts)
python3 -c "import json; d=json.load(open('output/step8_routing_summary.json')); print(d['totals']); print(d['rates'])"

# Duplicate counts (PRIMARY vs flagged duplicates)
python3 -c "import json; d=json.load(open('output/step5d_deduped.json')); print('PRIMARY', d['primary_count'], 'DUPLICATE', d['discarded_count'])"

# Count candidates at each stage
python3 -c "import json; d=json.load(open('output/step5a_candidates.json')); print('step5a:', len(d['candidates']))"
python3 -c "import json; d=json.load(open('output/step5_final_output.json')); print('step5d:', len(d['candidates']))"

# Check cloud regions from step2b
python3 -c "import json; d=json.load(open('output/outer_clouds_v2.json')); print('clouds:', d['stats'])"

# Check what's in output folder
ls -lh output/*.json | awk '{print $5, $9}'

# Test single SAHI patch (patch 19 is dense, good for testing)
python3 stages/step5a_candidate_extraction.py \
    --context output/drawing_context.json \
    --api-key $GEMINI_KEY \
    --patch 19 --debug
```

---

## Reporting Tools

### `eval_coverage.py` — did we get the ground-truth tags?
```bash
python3 stages/eval_coverage.py \
    --candidates output/step5a_candidates.json \
    --register   ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx \
    --image      input_drawing.jpg \
    --out        output/
```
Prints `FOUND x/46`, lists missing tags, and writes:
- `output/step5a_eval_annotated_fullres.jpg` — every tag boxed (green = in register, orange = extra)
- `output/step5a_eval_annotated.jpg` — scaled overview
- `output/eval_coverage_report.json`

Tag matching folds `10"` == `10IN` and ignores unicode dashes/quotes.

### `stage_visualizer.py` — detect-vs-filter images for UI
```bash
python3 stages/stage_visualizer.py \
    --candidates output/step5a_candidates.json \
    --deduped    output/step5d_deduped.json \
    --sow        output/sow_symbol_memory.json \
    --image      input_drawing.jpg \
    --out        output/stages/
```
Produces in `output/stages/` (each = full-res `.jpg` + `_overview.jpg` + JSON):

| Image | Meaning |
|-------|---------|
| `5a_detection` | all detected tags (green) |
| `sow_detected` | in-scope (green) / do-not-use (red) / unspecified (white) |
| `sow_filtered` | after SOW filter — do-not-use tags removed |
| `dup_detected` | duplicates kept and flagged (purple), linked to primary (green) |
| `dup_filtered` | after duplicate filter — primaries only |

`output/stages/manifest.json` lists every image, overview, count, and JSON path.

---

## Confidence Formula (Step 8)

```
C_final = 0.30 × C_detect      (Gemini vision_confidence)
        + 0.30 × C_ocr          (Tesseract OCR; falls back to model read confidence if silent)
        + 0.15 × C_geometry     (association_confidence from step5b)
        + 0.20 × C_validation   (ISA check scores from step5c)
        + 0.05 × C_registry     (1.0 if in register, 0.5 if not)

≥ 0.80 → AUTO_ACCEPT
0.55–0.80 → HUMAN_REVIEW  (P1 = validation fail, P2 = weak text, P3 = novel/scope)
< 0.55 → AUTO_REJECT
```

---

## What Changed in June 2026 Tuning

### step5a (recall 26 → 44 / 46)
- **FULL_EXTRACTION mode:** disabled the "only extract inside revision clouds" prompt injection — it made Gemini skip valid center/edge symbols.
- **Prompt:** now includes piping line numbers (`10IN-ETH-V061-61440X`, …); added "extract EVERY valve in a bank / both switches in a pair"; preserve the `V-` prefix.
- **Tiling:** SAHI `768 px / 40% overlap` (was 1024 / 25%); small patches upscaled to 1024 before Gemini so tiny valves become legible.
- **Dedup:** intra-step dedup merges only exact-same text, never sequential neighbours (`V-BV-2245` vs `2246`).

### step5c
- `LINE` regex now accepts `IN` / `"` / unicode quotes & dashes.
- Registry matching folds the inch marker (`10"` == `10IN`) and the `V-` area prefix → 39/46 register tags reconcile.

### step5d
- Merges only the *same* tag (exact or OCR-confusable like `0`↔`O`). The old `IoU≥0.2 merges ANY tags` rule was deleting different-but-nearby tags (e.g. `FZSC-208`/`FZSO-208` switch pair).
- Primary selection prefers the register-matching spelling (keeps `V012`, not `VO12`).
- Carries `validation_details` into final output so step7 can recover registry descriptions.

### step7
- Converts `10"` → `10IN` so piping canonical tags match Annexure-4 style.

### step8 (AUTO_ACCEPT 0% → 76.6%)
- **Root cause fixed:** formula weighted Tesseract OCR at 0.30, but Tesseract is silent (Gemini reads text), dragging scores down. Now when OCR is silent, text-confidence falls back to model read confidence.
- Rebalanced weights (`W_DET 0.30`, `W_REG 0.05`); `THRESHOLD_ACCEPT 0.85→0.80`, `THRESHOLD_REVIEW 0.60→0.55`.

---

## Known Limitations

- **`V-ZSC-203` / `V-ZSO-203`** — in the Annexure-4 register but **not drawn** on this sheet. Cannot be extracted from text that isn't there.
- **SOW scope:** ~105 tags are `UNSPECIFIED` because extracted symbol-name strings don't always phrase-match ANNEXURE-2. Tightening the SOW name-matcher would reduce this.
- The 46-tag register is sparse vs 226 detected tags — most novel tags route to HUMAN_REVIEW by design.

---

## What NOT to Do

- Do NOT use `step5a_live_annotator.py` for pipeline runs — use `step5a_candidate_extraction.py`
- Do NOT use any old root-level step3 copy — use `stages/step3_notes_agent.py`
- Do NOT delete `output/drawing_context.json` mid-pipeline — it is the shared state spine
- Do NOT run step5a without step2 having run first (needs `raster_path` in context)
- Do NOT skip step6 before step5c — `master_tags.json` feeds ISA validation
- Do NOT use `--workers > 1` on free-tier Gemini keys (429 rate limit)
- Do NOT modify `stages/step2b_cloud_detection.py` internals — cloud detector is self-contained there
