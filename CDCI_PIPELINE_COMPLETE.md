# CDCI Tag Extraction Pipeline — Complete Reference
## All Commands, File Map, Data Flow & Implementation Plan

---

## QUICK START — Full Pipeline (Copy-Paste)

```bash
# ── Set your variables ────────────────────────────────────────────────────────
export GEMINI_KEY="YOUR_GEMINI_API_KEY"
export DRAWING="input_drawing.jpg"          
export SOW_EXCEL="ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx"
export REG_EXCEL="ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx"
export OUT="output"

# ── PHASE 1: Context (run once per drawing, ~2 min) ───────────────────────────
python step1_format_detect.py   $DRAWING --out $OUT --api-key $GEMINI_KEY
python step2_title_block.py     --context $OUT/drawing_context.json --api-key $GEMINI_KEY --debug
python step3_notes_agent_v2.py  --context $OUT/drawing_context.json --api-key $GEMINI_KEY --debug
python step4_sow_agent.py build --excel $SOW_EXCEL --out $OUT --api-key $GEMINI_KEY
python step6_table_agent.py     --context $OUT/drawing_context.json --api-key $GEMINI_KEY --debug

# ── PHASE 2: Extraction (core Gemini work, ~2-5 min) ─────────────────────────
python step5a_candidate_extraction.py --context $OUT/drawing_context.json \
    --api-key $GEMINI_KEY --workers 8 --debug

# ── PHASE 3: Post-processing (no API, <30 sec total) ─────────────────────────
python step5b_geometric_association.py --candidates $OUT/step5a_candidates.json \
    --image $DRAWING --out $OUT --debug
python step5c_validation_engine.py     --associations $OUT/step5b_associations.json \
    --register $REG_EXCEL --notes $OUT/notes_context.json --out $OUT
python step5d_duplicate_resolution.py  --validated $OUT/step5c_validated.json --out $OUT

# ── PHASE 4: Output (no API, <10 sec) ────────────────────────────────────────
python step7_cedm_normalizer.py  --final $OUT/step5_final_output.json \
    --context $OUT/drawing_context.json --out $OUT
python step8_confidence_router.py --cedm $OUT/step7_cedm_output.json \
    --context $OUT/drawing_context.json --out $OUT

# ── PHASE 5: Human Review Images ─────────────────────────────────────────────
python step5_visualizer.py --candidates $OUT/step5a_candidates.json \
    --deduped $OUT/step5d_deduped.json --image $DRAWING --out $OUT --tile-size 2000
```

---

## COMPLETE FILE MAP

```
cdci_extractor_final/
│
├── INPUTS
│   ├── input_drawing.jpg / .pdf          ← P&ID drawing
│   ├── ANNEXURE-2_CDC-SYMBOLS_*.xlsx     ← SOW symbol scope (USE / DO NOT USE)
│   └── ANNEXURE-4_4224-*-001-C.xlsx      ← Asset register (tag validation)
│
├── PIPELINE SCRIPTS (12 files)
│   ├── step1_format_detect.py            ← Layer 1:  Format detect + rasterize
│   ├── step2_title_block.py              ← Layer 2:  Title block + revision routing
│   ├── step3_notes_agent_v2.py           ← Layer 5:  Notes + rules extraction
│   ├── step4_sow_agent.py                ← Layer 6:  SOW symbol scope memory
│   ├── step5a_candidate_extraction.py    ← Layer 7+8: SAHI + Gemini + Tesseract
│   ├── step5b_geometric_association.py   ← Layer 10: Pipe/leader line geometry
│   ├── step5c_validation_engine.py       ← Layer 12: ISA + business rule checks
│   ├── step5d_duplicate_resolution.py    ← Layer 13: SMM deduplication
│   ├── step5_visualizer.py               ← QA: Bbox overlay + duplicate highlights
│   ├── step6_table_agent.py              ← Layer 9:  Table extraction
│   ├── step7_cedm_normalizer.py          ← Layer 14: CEDM canonical normalisation
│   └── step8_confidence_router.py        ← Layer 15+16: Confidence + Excel export
│
└── OUTPUT/
    ├── drawing_context.json              ← Master context (all steps update this)
    ├── title_block_context.json          ← Step 2 output
    ├── notes_context.json                ← Step 3 output
    ├── rules_prompt_block.txt            ← Injected into all Gemini prompts
    ├── sow_symbol_memory.json            ← 132 symbols (100 ALLOW + 32 BLOCK)
    ├── sow_scope_summary.txt             ← Human-readable scope list
    ├── tables_context.json               ← Step 6 output
    ├── master_tags.json                  ← Flat tag list from tables
    ├── step5a_candidates.json            ← 320 raw candidates
    ├── step5b_associations.json          ← Spatial relationships added
    ├── step5c_validated.json             ← ISA + registry validation added
    ├── step5d_deduped.json               ← SMM dedup (PRIMARY / DISCARDED)
    ├── step5_final_output.json           ← PRIMARY candidates only → feeds step7
    ├── step7_cedm_output.json            ← All 15 Annexure-4 fields populated
    ├── final_tags.xlsx                   ← ★ FINAL DELIVERABLE (3 sheets)
    ├── human_review_queue.json           ← P1-P4 flagged items for reviewer
    ├── audit_log.json                    ← Auto-rejected records
    ├── step8_routing_summary.json        ← Pipeline statistics
    ├── viz_all_candidates.jpg            ← All detections coloured by type
    ├── viz_duplicates_highlighted.jpg    ← RED=duplicate, GREEN=kept
    ├── viz_final_clean.jpg               ← Final clean output for review
    └── viz_tiles/                        ← Zoomable 2000×2000px tile grid
        └── final_R01C01.jpg ...
```

---

## DATA FLOW DIAGRAM

```
DRAWING.JPG / .PDF
      │
      ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 1 — CONTEXT SETUP  (7 Gemini calls, ~2 min)                   │
│                                                                      │
│  step1 ──→ drawing_context.json  (format, raster path, image size)  │
│     │                                                                │
│  step2 ──→ title_block_context.json                                  │
│     │       revision_mode: CLOUD_SCOPE_MODE ←────────────┐          │
│     │       extraction_scope: CLOUD_ONLY                  │          │
│     │                                                      │          │
│  step3 ──→ notes_context.json                             │          │
│     │       rules_prompt_block.txt ──────────────────────┼──────┐   │
│     │                                                      │      │   │
│  step4 ──→ sow_symbol_memory.json                         │      │   │
│     │       (100 ALLOW + 32 BLOCK symbols)                │      │   │
│     │                                                      │      │   │
│  step6 ──→ tables_context.json                            │      │   │
│             master_tags.json ─────────────────────────────┼──────┼──┐│
│                                                            │      │  ││
└────────────────────────────────────────────────────────────┼──────┼──┼┘
                                                             │      │  │
      ▼                                                       │      │  │
┌─────────────────────────────────────────────────────────────────────┐│
│ PHASE 2 — EXTRACTION  (117 Gemini Pro calls, parallel, ~2-5 min)    ││
│                                                                      ││
│  step5a  SAHI → 117 patches → [8 parallel workers]                  ││
│          ├── pre-filter (OpenCV) skip blanks                         ││
│          ├── Tesseract OCR (deterministic text)                      ││
│          ├── Gemini 2.5 Pro (semantic symbol understanding) ←────────┘│
│          ├── revision cloud filter ←─────────────────────────────────┘
│          ├── SOW filter (ALLOW/BLOCK/UNSPECIFIED)                     │
│          └──→ step5a_candidates.json  (320 candidates)               │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 3 — POST-PROCESSING  (no API, <30 sec total)                   │
│                                                                      │
│  step5b  OpenCV line detection → spatial association                 │
│          leader lines, pipe connections, containment                 │
│          ──→ step5b_associations.json                                │
│                                                                      │
│  step5c  Programmatic validation                                     │
│          ISA-5.1 regex + business rules + register lookup ←──────────┘
│          ──→ step5c_validated.json                                   │
│                                                                      │
│  step5d  SMM deduplication (Union-Find + IoU + tag similarity)       │
│          ──→ step5d_deduped.json                                     │
│          ──→ step5_final_output.json  (PRIMARY only)                 │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 4 — OUTPUT GENERATION  (no API, <10 sec)                       │
│                                                                      │
│  step7  CEDM normalisation                                           │
│         FIT.1001 → FIT-1001 │ discipline │ canonical_id             │
│         ──→ step7_cedm_output.json  (15 Annexure-4 fields)          │
│                                                                      │
│  step8  Confidence aggregation & routing                             │
│         C = 0.25×det + 0.30×ocr + 0.15×geo + 0.20×val + 0.10×reg  │
│         ≥0.85 → AUTO_ACCEPT │ 0.60-0.85 → REVIEW │ <0.60 → REJECT  │
│         ──→ final_tags.xlsx  (★ FINAL DELIVERABLE)                  │
│         ──→ human_review_queue.json  (P1-P4 items)                  │
│         ──→ audit_log.json                                           │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 5 — HUMAN REVIEW IMAGES  (no API)                              │
│                                                                      │
│  step5_visualizer                                                    │
│         GREEN boxes = valid detections (by category)                 │
│         RED boxes   = DISCARDED duplicates (arrows to PRIMARY)       │
│         Tile grid   = full-res zoomable crops for reviewer           │
│         ──→ viz_all_candidates.jpg                                   │
│         ──→ viz_duplicates_highlighted.jpg                           │
│         ──→ viz_final_clean.jpg                                      │
│         ──→ viz_tiles/                                               │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## STEP-BY-STEP COMMAND REFERENCE

### STEP 1 — Format Detection
```bash
# JPG/PNG input:
python step1_format_detect.py input_drawing.jpg --out output/ --api-key $GEMINI_KEY

# PDF input:
python step1_format_detect.py input_drawing.pdf --out output/ --api-key $GEMINI_KEY --dpi 300

# Force raster path (even if PDF has text layer):
python step1_format_detect.py input_drawing.pdf --out output/ --force-raster
```
**Writes:** `output/drawing_context.json`

---

### STEP 2 — Title Block & Revision Intelligence
```bash
# Standard run after step1:
python step2_title_block.py --context output/drawing_context.json --api-key $GEMINI_KEY --debug

# Direct on image:
python step2_title_block.py input_drawing.jpg --out output/ --api-key $GEMINI_KEY --debug
```
**Writes:** `output/title_block_context.json`, updates `drawing_context.json`
**Detects:** `revision_mode: CLOUD_SCOPE_MODE` — tells step5a to filter by clouds

---

### STEP 3 — Notes Extraction (USE v2)
```bash
python step3_notes_agent.py --context output/drawing_context.json --api-key $GEMINI_KEY --debug
```
**Writes:** `output/notes_context.json`, `output/rules_prompt_block.txt`
**Produces:** 58 unique notes, abbreviations, drawing-specific rules

---

### STEP 4 — SOW Symbol Scope
```bash
# With Gemini vision enrichment (recommended, ~132 calls):
python step4_sow_agent.py build \
    --excel ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx \
    --out output/ --api-key $GEMINI_KEY

# Text-only fast mode (no API, slightly less accurate):
python step4_sow_agent.py build \
    --excel ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx \
    --out output/ --skip-vision

# Test classify a symbol:
python step4_sow_agent.py classify --memory output/sow_symbol_memory.json \
    --symbol "FLOW TRANSMITTER"

# Filter step6 tags against SOW:
python step4_sow_agent.py filter \
    --memory output/sow_symbol_memory.json --tags output/master_tags.json --out output/
```
**Writes:** `output/sow_symbol_memory.json`, `output/sow_scope_summary.txt`

---

### STEP 6 — Table Extraction (run BEFORE step5c)
```bash
python step6_table_agent.py --context output/drawing_context.json --api-key $GEMINI_KEY --debug
```
**Writes:** `output/tables_context.json`, `output/master_tags.json`
**Note:** Run before step5c — master_tags.json feeds the registry validation

---

### STEP 5A — Candidate Extraction (SAHI + Gemini + Tesseract)
```bash
# Paid API key (8 workers, ~45s for 117 patches):
python step5a_candidate_extraction.py \
    --context output/drawing_context.json \
    --api-key $GEMINI_KEY --workers 8 --debug

# Free tier key (1 worker, avoids 429):
python step5a_candidate_extraction.py \
    --context output/drawing_context.json \
    --api-key $GEMINI_KEY --workers 1

# Test single patch:
python step5a_candidate_extraction.py input_drawing.jpg \
    --out output/ --api-key $GEMINI_KEY --patch 19
```
**Writes:** `output/step5a_candidates.json` (320 candidates)

---

### STEP 5B — Geometric Association (no API)
```bash
python step5b_geometric_association.py \
    --candidates output/step5a_candidates.json \
    --image input_drawing.jpg --out output/ --debug
```
**Writes:** `output/step5b_associations.json`

---

### STEP 5C — Validation Engine (no API)
```bash
# With asset register Excel:
python step5c_validation_engine.py \
    --associations output/step5b_associations.json \
    --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx \
    --notes output/notes_context.json --out output/

# With master_tags.json from step6:
python step5c_validation_engine.py \
    --associations output/step5b_associations.json \
    --register output/master_tags.json \
    --notes output/notes_context.json --out output/
```
**Writes:** `output/step5c_validated.json`

---

### STEP 5D — Duplicate Resolution (no API)
```bash
python step5d_duplicate_resolution.py \
    --validated output/step5c_validated.json --out output/
```
**Writes:** `output/step5d_deduped.json`, `output/step5_final_output.json`

---

### STEP 7 — CEDM Normalisation (no API)
```bash
python step7_cedm_normalizer.py \
    --final output/step5_final_output.json \
    --context output/drawing_context.json \
    --out output/ --project CDCI
```
**Writes:** `output/step7_cedm_output.json`

---

### STEP 8 — Confidence Routing + Excel Export (no API)
```bash
python step8_confidence_router.py \
    --cedm output/step7_cedm_output.json \
    --context output/drawing_context.json --out output/
```
**Writes:** `output/final_tags.xlsx`, `output/human_review_queue.json`, `output/audit_log.json`

---

### STEP 5 VISUALIZER — Human Review Images (no API)
```bash
# Full visualization (all three images + tiles):
python step5_visualizer.py \
    --candidates output/step5a_candidates.json \
    --deduped output/step5d_deduped.json \
    --image input_drawing.jpg --out output/ --tile-size 2000

# After step5a only (before dedup):
python step5_visualizer.py \
    --candidates output/step5a_candidates.json \
    --image input_drawing.jpg --out output/
```
**Writes:** `output/viz_*.jpg`, `output/viz_tiles/*.jpg`

---

## API CALL BUDGET PER DRAWING

| Step | Model | Calls | Cost Driver |
|------|-------|-------|-------------|
| step1 | Flash-Lite | 1 | thumbnail classification |
| step2 | Flash | 2 | title block + revision table |
| step3 | Pro + Flash | 7 | 7 tile regions |
| step4 | Flash | ~132 | per symbol image (skip-vision = 0) |
| step5a | Pro | 82-117 | per SAHI patch (parallel) |
| step6 | Flash + Pro | 2-4 | table detection + extraction |
| **Total** | | **~226-263** | |

**Estimated cost (paid tier):**
- Pro calls (~120): ~$0.15
- Flash calls (~140): ~$0.04
- **Total per drawing: ~$0.20**

---

## TROUBLESHOOTING

| Error | Cause | Fix |
|-------|-------|-----|
| `No module named 'anthropic'` | Wrong import | Uses `google.genai` not anthropic |
| `AttributeError: 'NoneType'...get` | Gemini returns null bbox | Fixed in step5a v2 |
| `429 Resource Exhausted` | Rate limit | Use `--workers 1` |
| `0 tag candidates` (Tesseract) | Small text / low contrast | CLAHE auto-applied |
| `step5b_associations.json not found` | 5B before 5A | Run in order: 5A→5B→5C→5D |
| `final_tags.xlsx` missing sheets | openpyxl version | `pip install openpyxl>=3.1` |

---

## PIPELINE STATUS MATRIX

| Blueprint Layer | Script | Status | API? | Time |
|----------------|--------|--------|------|------|
| Layer 1 — Format Detect | step1_format_detect.py | ✅ Done | Flash-Lite opt. | <5s |
| Layer 2 — Title Block | step2_title_block.py | ✅ Done | Flash ×2 | <15s |
| Layer 5 — Notes | step3_notes_agent_v2.py | ✅ Done | Pro+Flash ×7 | ~45s |
| Layer 6 — SOW Filter | step4_sow_agent.py | ✅ Done | Flash ×132 | ~3min |
| Layer 7+8 — Detection | step5a_candidate_extraction.py | ✅ Done | Pro ×117 | ~45s |
| Layer 9 — Tables | step6_table_agent.py | ✅ Done | Flash ×2-4 | <30s |
| Layer 10+11 — Geometry | step5b_geometric_association.py | ✅ Done | None | <5s |
| Layer 12 — Validation | step5c_validation_engine.py | ✅ Done | None | <3s |
| Layer 13 — Dedup | step5d_duplicate_resolution.py | ✅ Done | None | <3s |
| Layer 14 — CEDM | step7_cedm_normalizer.py | ✅ Done | None | <3s |
| Layer 15+16 — Output | step8_confidence_router.py | ✅ Done | None | <5s |
| QA Visualizer | step5_visualizer.py | ✅ Done | None | <15s |

**12/12 steps complete.**

