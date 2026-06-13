# CDCI P&ID Pipeline — Runbook

> Practical "how to run it and what each command does" guide.
> Reflects all tuning done in June 2026 (recall fixes, downstream fixes, SOW + duplicate
> stage images). Read this top-to-bottom to run the whole thing from scratch.

---

## 0. What this pipeline produces

From one scanned P&ID drawing it produces:

- `output/step5a_candidates.json` — every tag detected on the drawing (with bounding boxes)
- `output/final_tags.xlsx` — the client deliverable in Annexure-4 format (AUTO_ACCEPT / HUMAN_REVIEW / SUMMARY sheets)
- `output/stages/` — per-stage annotated images + JSON for the review UI (detect vs filter views)

**Result on the test drawing (`input_drawing.jpg`):**
`44/46` Annexure-4 tags extracted (the other 2 — `V-ZSC-203`, `V-ZSO-203` — are **not drawn** on the sheet),
`226` total tags detected, `AUTO_ACCEPT 160 (76.6%)`, `AUTO_REJECT 0`.

---

## 1. Prerequisites (run once)

```bash
cd /Users/suryprakash/Downloads/cdci_extractor_final
source .venv/bin/activate                 # Python venv with cv2, pytesseract, openpyxl, google-genai
brew install tesseract                     # if not already installed
export $(grep GEMINI_KEY .env | xargs)     # loads GEMINI_KEY from .env into the shell
echo $GEMINI_KEY                           # sanity check — should print a key
```

- **Gemini model used for extraction:** `gemini-3.1-pro-preview` (set in `stages/step5a_candidate_extraction.py`, line ~72).
- **Free-tier key (5 req/min):** add `--workers 1` to the step5a command, otherwise you hit HTTP 429.

---

## 2. Full pipeline — copy-paste

```bash
cd /Users/suryprakash/Downloads/cdci_extractor_final
source .venv/bin/activate
export $(grep GEMINI_KEY .env | xargs)
export DRAWING="input_drawing.jpg"

# ── Phase 1: context (run once per drawing) ──────────────────────────────────
python3 stages/step1_format_detect.py  $DRAWING --out output/ --api-key $GEMINI_KEY
python3 stages/step2_title_block.py    --context output/drawing_context.json --api-key $GEMINI_KEY
python3 stages/step2b_cloud_detection.py $DRAWING --out output/ --api-key $GEMINI_KEY
python3 stages/step3_notes_agent.py    --context output/drawing_context.json --api-key $GEMINI_KEY
python3 stages/step4_sow_agent.py build --excel ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx --out output/ --skip-vision
python3 stages/step6_table_agent.py    --context output/drawing_context.json --api-key $GEMINI_KEY

# ── Phase 2: extraction (the slow step — ~4 min, 315 Gemini calls) ───────────
python3 stages/step5a_candidate_extraction.py --context output/drawing_context.json --api-key $GEMINI_KEY --workers 8

# ── Phase 3: post-processing (no API, < 30s) ─────────────────────────────────
python3 stages/step5b_geometric_association.py --candidates output/step5a_candidates.json --image $DRAWING --out output/
python3 stages/step5c_validation_engine.py     --associations output/step5b_associations.json --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx --notes output/notes_context.json --out output/
python3 stages/step5d_duplicate_resolution.py  --validated output/step5c_validated.json --out output/

# ── Phase 4: output (no API, < 10s) ──────────────────────────────────────────
python3 stages/step7_cedm_normalizer.py  --final output/step5_final_output.json --context output/drawing_context.json --out output/ --project CDCI
python3 stages/step8_confidence_router.py --cedm output/step7_cedm_output.json --context output/drawing_context.json --out output/

# ── Reporting / visuals (no API) ─────────────────────────────────────────────
python3 stages/eval_coverage.py    --candidates output/step5a_candidates.json --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx --image $DRAWING --out output/
python3 stages/stage_visualizer.py --candidates output/step5a_candidates.json --deduped output/step5d_deduped.json --sow output/sow_symbol_memory.json --image $DRAWING --out output/stages/
```

> **Resume after a failure:** every step writes one JSON. Re-run only from the failed step onward.
> Phase 1 outputs (context/notes/sow/tables) rarely change, so you usually only re-run Phase 2→4.

---

## 3. What each command does

| # | Command | Reads | Writes | One-liner |
|---|---------|-------|--------|-----------|
| 1 | `step1_format_detect.py` | drawing | `drawing_context.json`, enhanced images | Detect raster/PDF, CLAHE enhance, classify doc |
| 2 | `step2_title_block.py` | context | `title_block_context.json` | Read title block (dwg no, sheet, rev) |
| 2B| `step2b_cloud_detection.py` | drawing | cloud regions in context | Find revision-cloud boundaries |
| 3 | `step3_notes_agent.py` | context | `notes_context.json`, `rules_prompt_block.txt` | Extract notes → drawing-specific rules |
| 4 | `step4_sow_agent.py build` | ANNEXURE-2 xlsx | `sow_symbol_memory.json` | Build 100-USE / 32-DO-NOT-USE scope memory |
| 6 | `step6_table_agent.py` | context | `master_tags.json`, `tables_context.json` | Extract tag-list tables |
| **5A** | `step5a_candidate_extraction.py` | context, sow, rules | `step5a_candidates.json` | **Detect every tag** (SAHI patches + Gemini + Tesseract) |
| 5B| `step5b_geometric_association.py` | 5a json, drawing | `step5b_associations.json` | Link tags to pipes/equipment (geometry only) |
| 5C| `step5c_validation_engine.py` | 5b json, register, notes | `step5c_validated.json` | ISA-5.1 format + registry lookup |
| 5D| `step5d_duplicate_resolution.py` | 5c json | `step5d_deduped.json`, `step5_final_output.json` | Flag SAHI duplicates (recall-safe) |
| 7 | `step7_cedm_normalizer.py` | 5d final, context | `step7_cedm_output.json` | Normalise tags, fill 15 Annexure-4 fields |
| 8 | `step8_confidence_router.py` | step7 json, context | `final_tags.xlsx`, review queue, audit log | Score + route → Excel deliverable |
| — | `eval_coverage.py` | 5a json, ANNEXURE-4 | annotated images, coverage report | **Measure recall vs ground truth** |
| — | `stage_visualizer.py` | 5a json, 5d json, sow | `output/stages/*` | **Per-stage detect/filter images + JSON** |

---

## 4. Reporting & verification tools (new)

### `eval_coverage.py` — did we get the ground-truth tags?
```bash
python3 stages/eval_coverage.py \
    --candidates output/step5a_candidates.json \
    --register   ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx \
    --image      input_drawing.jpg \
    --out        output/
```
Prints `FOUND x/46`, lists which tags are missing, and writes:
- `output/step5a_eval_annotated_fullres.jpg` — every tag boxed (🟩 = in register, 🟧 = extra)
- `output/step5a_eval_annotated.jpg` — scaled overview
- `output/eval_coverage_report.json`

Tag matching folds `10"` == `10IN` and ignores unicode dashes/quotes, so piping line numbers match correctly.

### `stage_visualizer.py` — detect-vs-filter images for the UI
```bash
python3 stages/stage_visualizer.py \
    --candidates output/step5a_candidates.json \
    --deduped    output/step5d_deduped.json \
    --sow        output/sow_symbol_memory.json \
    --image      input_drawing.jpg \
    --out        output/stages/
```
Produces, in `output/stages/` (each = full-res `.jpg` + `_overview.jpg` + the JSON the next step consumes):

| Image | Meaning |
|-------|---------|
| `5a_detection` | all detected tags (green) |
| `sow_detected` | 🟩 in-scope · 🟥 do-not-use · ⬜ unspecified |
| `sow_filtered` | after the SOW filter button — do-not-use tags removed |
| `dup_detected` | duplicates **kept & flagged** 🟪, linked to their 🟩 primary (nothing deleted) |
| `dup_filtered` | after the duplicate filter button — primaries only |

`output/stages/manifest.json` lists every image, overview, count and JSON path — drive the UI off this.

> **Principle:** detection never deletes. "Filtered" images are just the view after the user clicks a filter button.

---

## 5. What changed during tuning (and why)

All changes raise recall and stop good tags from being silently dropped.

### step5a — `step5a_candidate_extraction.py` (recall 26 → 44 / 46)
- **Full-extraction mode:** stopped injecting the "only extract inside revision clouds" note (it made Gemini skip valid center/edge symbols).
- **Prompt:** now *includes* piping line numbers (`10IN-ETH-V061-61440X`, …) which were previously in the IGNORE list; added "extract EVERY valve in a bank / both switches in a pair"; preserve the `V-` prefix.
- **Tiling:** SAHI `768 px / 40% overlap` (was 1024 / 25%), and small patches are **upscaled to 1024** before Gemini (with a coordinate rescale so boxes stay correct) → tiny valves/switches become legible.
- **Dedup:** intra-step dedup is recall-safe — merges only exact-same text, never sequential neighbours (`V-BV-2245` vs `2246`).

### step5c — `step5c_validation_engine.py`
- `LINE` regex accepts `IN` / `"` / unicode quotes & dashes.
- Registry matching folds the inch marker (`10"`==`10IN`) and the `V-` area prefix → 39/46 register tags reconcile.

### step5d — `step5d_duplicate_resolution.py`
- **Recall-safe merging:** merges only the *same* tag (exact or OCR-confusable like `0`↔`O`). The old `IoU≥0.2 merges ANY tags` rule was deleting different-but-nearby tags (e.g. the `FZSC-208`/`FZSO-208` switch pair).
- Primary selection prefers the **register-matching spelling** (keeps `V012`, not `VO12`).
- Now carries `validation_details` into the final output so step7 can recover registry descriptions.

### step7 — `step7_cedm_normalizer.py`
- Converts `10"` → `10IN` so piping canonical tags match the Annexure-4 style.

### step8 — `step8_confidence_router.py` (AUTO_ACCEPT 0% → 76.6%)
- **Root cause fixed:** the formula weighted Tesseract OCR at 0.30, but Tesseract is silent here (Gemini reads the text), which dragged scores down and AUTO_REJECTed ~87% of good tags. Now when OCR is silent the text-confidence **falls back to the model's read confidence**.
- Rebalanced weights (`W_DET 0.30`, `W_REG 0.05`); `THRESHOLD_ACCEPT 0.85→0.80`, `THRESHOLD_REVIEW 0.60→0.55`.
- Review-queue priorities are properly triaged (P1 = validation fails, P2 = weak text, P3 = novel/scope) instead of everything being P1.

### step4 — SOW memory
- `step4_sow_agent.py build` must run so `output/sow_symbol_memory.json` exists; the SOW images and any scope filtering depend on it.

---

## 6. Quick checks

```bash
# Recall vs ground truth
python3 stages/eval_coverage.py --candidates output/step5a_candidates.json \
    --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx --image input_drawing.jpg --out output/

# Routing distribution (accept / review / reject)
python3 -c "import json; d=json.load(open('output/step8_routing_summary.json')); print(d['totals']); print(d['rates'])"

# Duplicate counts (PRIMARY vs flagged duplicate)
python3 -c "import json; d=json.load(open('output/step5d_deduped.json')); print('PRIMARY',d['primary_count'],'DUPLICATE',d['discarded_count'])"
```

---

## 7. Known limitations

- **`V-ZSC-203` / `V-ZSO-203`** are in the Annexure-4 register but are **not drawn as labelled bubbles** on this sheet (confirmed by a dedicated Gemini pass). They cannot be extracted from text that isn't there.
- **SOW:** ~105 tags are `UNSPECIFIED` because extracted symbol-name strings don't always phrase-match ANNEXURE-2. Tightening the SOW name-matcher would reduce this.
- The 46-tag register is sparse vs 226 detected tags, so most novel tags route to HUMAN_REVIEW (by design) rather than AUTO_ACCEPT.
```
