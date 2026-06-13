# CDCI P&ID Tag Extraction Pipeline
## Project Memory for Claude Code

This file gives Claude Code full context about this project.
Read this before doing anything. Every answer is here.

> **▶ CURRENT SOURCE OF TRUTH FOR RUNNING THE PIPELINE: [`PIPELINE_RUNBOOK.md`](PIPELINE_RUNBOOK.md)**
> It has the verified, up-to-date commands and the June-2026 tuning changes
> (recall fixes in 5a, recall-safe dedup in 5d, the step8 confidence fix, the SOW +
> duplicate stage images, and the `eval_coverage.py` / `stage_visualizer.py` tools).
> The run-order and results sections further down in this file predate that tuning.

---

## What This Project Does

Automated system that reads engineering P&ID drawings (Piping and Instrumentation Diagrams)
and extracts every equipment tag (like FIT-1001, V-BV-2246, PT-201) into a structured
Excel register matching the client's Annexure-4 format.

- Input: scanned JPG/PDF engineering drawing
- Output: final_tags.xlsx with AUTO_ACCEPT, HUMAN_REVIEW, SUMMARY sheets
- Time: under 10 minutes per drawing
- Tech: Gemini AI + Tesseract OCR + OpenCV

---

## Project Folder Structure

```
cdci_extractor_final/
├── CLAUDE.md                              ← this file
├── input_drawing.jpg                      ← test P&ID drawing (9934×7017px)
├── ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx   ← SOW scope (100 ALLOW + 32 BLOCK)
├── ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx       ← asset register (46 tags)
├── stages/
│   ├── step2b_cloud_detection.py         ← cloud detection wrapper (DO NOT MODIFY internals)
│   ├── step1_format_detect.py
│   ├── step2_title_block.py
│   ├── step3_notes_agent.py              ← v2 implementation (do not use old root copy)
│   ├── step4_sow_agent.py
│   ├── step5a_live_annotator.py          ← NEW: detect + draw bbox in one step
│   ├── step5a_candidate_extraction.py    ← OLD: kept for reference
│   ├── step5b_geometric_association.py
│   ├── step5c_validation_engine.py
│   ├── step5d_duplicate_resolution.py
│   ├── step5_visualizer.py
│   ├── step6_table_agent.py
│   ├── step7_cedm_normalizer.py
│   └── step8_confidence_router.py
│
└── output/
    ├── drawing_context.json              ← MASTER: all steps read/update this
    ├── title_block_context.json
    ├── notes_context.json
    ├── rules_prompt_block.txt            ← injected into ALL Gemini prompts
    ├── sow_symbol_memory.json
    ├── sow_scope_summary.txt
    ├── tables_context.json
    ├── master_tags.json
    ├── step5a_live_candidates.json       ← from live annotator
    ├── step5a_candidates.json            ← from old step5a
    ├── step5b_associations.json
    ├── step5c_validated.json
    ├── step5d_deduped.json
    ├── step5_final_output.json
    ├── step7_cedm_output.json
    ├── final_tags.xlsx                   ← FINAL DELIVERABLE
    ├── human_review_queue.json
    ├── audit_log.json
    ├── step5a_live_annotated.jpg         ← overview with all bboxes
    ├── step5a_live_annotated_fullres.jpg ← full resolution
    └── step5a_live_tiles/               ← zoomable 2000×2000 grid
```

---

## The 12 Steps — One Line Each

| Step | File | What it does |
|------|------|--------------|
| 1 | step1_format_detect.py | Detect raster vs PDF, apply CLAHE enhancement |
| 2 | step2_title_block.py | Read title block, set revision mode (CLOUD_SCOPE_MODE) |
| 2B | step2b_cloud_detection.py | Find cloud boundaries → write cloud_regions to context |
| 3 | stages/step3_notes_agent.py | Extract 62 notes → generate rules_prompt_block.txt |
| 4 | step4_sow_agent.py | Build 132-symbol scope memory from Excel |
| 6 | step6_table_agent.py | Extract tag list tables → master_tags.json |
| 5A | step5a_live_annotator.py | SAHI 117 patches, detect + draw bbox simultaneously |
| 5B | step5b_geometric_association.py | OpenCV geometry: pipes, leader lines, containment |
| 5C | step5c_validation_engine.py | ISA-5.1 validation + asset register lookup |
| 5D | step5d_duplicate_resolution.py | SMM dedup: 264 → 233 candidates |
| 7 | step7_cedm_normalizer.py | Normalise tags, fill 15 Annexure-4 fields |
| 8 | step8_confidence_router.py | Score + route → final_tags.xlsx |

---

## Complete Run Order (Copy-Paste)

```bash
export GEMINI_KEY="your-api-key-here"
export DRAWING="input_drawing.jpg"

# Phase 1 — context (run once per drawing)
python stages/step1_format_detect.py $DRAWING --out output/ --api-key $GEMINI_KEY
python stages/step2_title_block.py --context output/drawing_context.json --api-key $GEMINI_KEY
python stages/step2b_cloud_detection.py $DRAWING --out output/ --api-key $GEMINI_KEY
python stages/step3_notes_agent.py --context output/drawing_context.json --api-key $GEMINI_KEY
python stages/step4_sow_agent.py build --excel ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx --out output/ --skip-vision
python stages/step6_table_agent.py --context output/drawing_context.json --api-key $GEMINI_KEY

# Phase 2 — extraction (~2-5 min, 117 Gemini Pro calls)
python stages/step5a_live_annotator.py --context output/drawing_context.json --api-key $GEMINI_KEY --workers 8

# Phase 3 — post-processing (no API, <30 sec)
python stages/step5b_geometric_association.py --candidates output/step5a_live_candidates.json --image $DRAWING --out output/
python stages/step5c_validation_engine.py --associations output/step5b_associations.json --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx --notes output/notes_context.json --out output/
python stages/step5d_duplicate_resolution.py --validated output/step5c_validated.json --out output/

# Phase 4 — output (no API, <10 sec)
python stages/step7_cedm_normalizer.py --final output/step5_final_output.json --context output/drawing_context.json --out output/ --project CDCI
python stages/step8_confidence_router.py --cedm output/step7_cedm_output.json --context output/drawing_context.json --out output/
```

**Free tier API key** (5 RPM limit): use `--workers 1` in step5a to avoid 429 errors.

**Resume after failure**: each step checks if its output file already exists.
Delete the specific output file to re-run only that step.

---

## Models Used

| Model | Steps | Purpose |
|-------|-------|---------|
| gemini-2.5-pro | 5A, 3 (priority) | Symbol detection, complex layouts, temp=0.0 |
| gemini-2.5-flash | 2, 3, 4, 6 | Structured extraction, title block, tables |
| gemini-2.5-flash-lite | 1 (optional) | Document type classification |
| Tesseract OCR | 2, 3, 5A, 6 | Deterministic text — ground truth for tag chars |
| OpenCV | all | CLAHE, line detection, morphology, drawing |

---

## Actual Results on Test Drawing

```
Drawing:           4224-MGDV-6-50-2004  Sheet 001  Rev C
Revision mode:     CLOUD_SCOPE_MODE (explicit notice on drawing)
Clouds:            9 outer + 18 inner = 27 total
Notes extracted:   62 unique, 8 abbreviations
SOW loaded:        100 ALLOW + 32 BLOCK symbols
Step 5A output:    264 candidates (165 instruments, 51 valves, 34 piping, 8 equip)
After dedup:       233 PRIMARY candidates (11.7% dup rate)
CEDM output:       149 INSTRUMENTATION + 57 MECHANICAL + 27 PIPING
Confidence avg:    0.554 (needs tuning — see Known Issues)
```

---

## Known Issues and Fixes Already Applied

### Issue 1 — step2b was not in the run order (FIXED)
**Problem:** step5a read `clouds=0` from drawing_context.json because step2b
was never called. Drawing was processed as FULL_DRAWING instead of CLOUD_ONLY.
**Fix:** step2b_cloud_detection.py must run between step2 and step3.
The cloud_regions list is written to drawing_context.json for step5a to read.

### Issue 2 — Bbox drawn on symbol bubble, not tag text (FIXED)
**Problem:** Visualizer drew boxes at symbol_bbox (instrument circle location),
not tag_bbox (where the text "FIT-207" actually sits on the drawing).
**Fix:** step5a_live_annotator.py and step5_visualizer.py now use tag_bbox first,
fall back to symbol_bbox only if tag_bbox is empty.

### Issue 3 — Same tag detected 2-4 times (FIXED)
**Problem:** V-GV-911 appeared 3 times, T-274C appeared 3 times from adjacent SAHI patches.
Old IoU threshold (0.30) missed them because bbox offset from different patches.
**Fix:** New Rule 0 in step5d — exact same tag text within 400px → ALWAYS merge.
Also added intra-step dedup inside step5a before writing JSON.

### Issue 4 — False positives: drawing refs, node IDs extracted as tags (FIXED)
**Problem:** 54 false positives in 264 candidates: 4224-MGDV-6-50-2002-001 (×6),
I-004 (×9), RCI (×4), LC (×3), GDV-6-50-... patterns.
**Fix:** _is_false_positive() function with two regex patterns in step5a.
After fix: 264 → 139 clean candidates (47% reduction, zero legitimate tags lost).

### Issue 5 — step8 AUTO_ACCEPT = 0% (NEEDS FIX)
**Problem:** Avg C_final = 0.554, all 233 candidates below 0.85 threshold.
Root cause: C_register = 0.5 for 218/233 tags not in the 46-tag register.
Register is incomplete — it only has 46 tags but drawing has 233.
**To fix:** Lower THRESHOLD_ACCEPT to 0.70 for sparse-register projects, or
reduce W_REG weight from 0.10 to 0.05 in step8_confidence_router.py.

### Issue 6 — step6 found revision table not TAG LIST (EXPECTED)
**Problem:** step6 detected 'Revision Table' (0 tags) not the Tag List.
**Reason:** The TAG LIST table lives on the companion sheet 002 drawing
(4224-MGDV-6-50-2004-002), not on this P&ID sheet 001.
**Action:** Run step6 on the correct source drawing for the tag list.

---

## Key Architecture Decisions

### Why SAHI?
The drawing is 9934×7017px. Gemini's effective resolution cap means small
instrument bubbles (~60-100px) get missed on the full image.
SAHI splits into 1024×1024 patches with 25% overlap → no missed symbols.
117 patches × 8 parallel workers = ~45 sec on paid tier.

### Why Tesseract + Gemini together?
Gemini understands WHAT the symbol is (semantic understanding).
Tesseract reads the EXACT text characters (deterministic accuracy).
Gemini sometimes reads "FIT-1001" as "FIT-100I" (1→I confusion).
Tesseract is used as ground truth for tag text when confidence ≥ 80%.

### Why temperature=0.0 in step5a?
Extraction must be deterministic. Same patch must always give same output.
Temperature=0.0 forces the most probable answer, no creative variation.

### Why tag_bbox not symbol_bbox for drawing?
On a P&ID the tag text (e.g. "FIT-207") is physically separated from the
instrument circle, connected by a leader line. symbol_bbox = the circle.
tag_bbox = where the text "FIT-207" is written. Box must go ON the text.

### The drawing_context.json is the spine
Every step reads this file at start, updates it at end.
It contains: image path, title block data, revision mode, cloud regions,
notes paths, SOW paths, table paths. Never delete it mid-run.

---

## How Prompting Works (For Team Explanation)

A **prompt** is the instruction we send to Gemini alongside the image.
Every prompt has 4 parts:

1. **Role**: "You are an expert P&ID extraction agent (ISA 5.1)"
2. **Task**: "Detect every instrument bubble and valve in this patch"
3. **Constraints**: "Only extract what is visually present. Never hallucinate."
4. **Output format**: "Return ONLY a JSON object with this structure: {candidates: [...]}"

We always request JSON so the output flows directly into the next step.
`temperature=0.0` means deterministic — same input always gives same output.

The **rules_prompt_block.txt** from step3 is injected into every step5a call.
This means drawing-specific rules (like "F prefix = field-routed") are
automatically applied during extraction without any code changes.

---

## Safety Filters (7 Layers)

1. **Revision cloud scope** — discard symbols outside cloud boundaries
2. **SOW block filter** — discard symbols in DO NOT USE list (32 types)
3. **False positive regex** — reject drawing refs, node IDs, spec codes
4. **Intra-step dedup** — same tag within 400px → keep one (in step5a)
5. **SMM dedup** — cross-patch duplicates via IoU + distance (step5d)
6. **ISA-5.1 validation** — 30 regex patterns, business rules (step5c)
7. **Confidence routing** — C_final formula, reject if < 0.60 (step8)

---

## Confidence Formula (Step 8)

```
C_final = 0.25 × C_detect   (Gemini vision_confidence)
        + 0.30 × C_ocr       (Tesseract ocr_confidence)
        + 0.15 × C_geometry  (association_confidence from step5b)
        + 0.20 × C_validation (ISA check scores from step5c)
        + 0.10 × C_registry  (1.0 if in register, 0.5 if not)

≥ 0.85 → AUTO_ACCEPT
0.60–0.85 → HUMAN_REVIEW (P1 critical → P4 low)
< 0.60 → AUTO_REJECT
```

---

## Immediate Next Actions

```
1. Run step2b between step2 and step3 — cloud regions must be in context
2. Fix step8 confidence thresholds — lower THRESHOLD_ACCEPT to 0.70
3. Run full pipeline end-to-end on input_drawing.jpg
4. Compare final_tags.xlsx against ANNEXURE-4 (ground truth)
5. Measure precision + recall — target >95% recall, >90% precision
```

---

## Useful Single Commands for Claude Code

```bash
# Check what's in output folder
ls -lh output/*.json | awk '{print $5, $9}'

# Count candidates at each stage
python -c "import json; d=json.load(open('output/step5a_live_candidates.json')); print(len(d['candidates']))"
python -c "import json; d=json.load(open('output/step5_final_output.json')); print(len(d['candidates']))"

# Check duplicate rate
python -c "
import json
d = json.load(open('output/step5d_deduped.json'))
p = sum(1 for r in d['all_records'] if r['duplicate_status']=='PRIMARY')
x = sum(1 for r in d['all_records'] if r['duplicate_status']=='DISCARDED')
print(f'PRIMARY={p} DISCARDED={x} dup_rate={x/(p+x)*100:.1f}%')
"

# Quick confidence distribution
python -c "
import json
d = json.load(open('output/step8_routing_summary.json'))
print(json.dumps(d['totals'], indent=2))
print(json.dumps(d['rates'], indent=2))
"

# Test single SAHI patch (patch 19 is dense, good for testing)
python stages/step5a_live_annotator.py --context output/drawing_context.json \
    --api-key \$GEMINI_KEY --patch 19 --debug

# Re-run only from step5d onwards (skip expensive Gemini steps)
python stages/step5d_duplicate_resolution.py --validated output/step5c_validated.json --out output/
python stages/step7_cedm_normalizer.py --final output/step5_final_output.json --context output/drawing_context.json --out output/
python stages/step8_confidence_router.py --cedm output/step7_cedm_output.json --out output/
```

---

## What NOT to Do

- Do NOT modify stages/step2b_cloud_detection.py internals — cloud detector is embedded there
- Do NOT use any old root-level step3_notes_agent copy — use stages/step3_notes_agent.py (v2)
- Do NOT use step5a_candidate_extraction.py for new runs — use step5a_live_annotator.py
- Do NOT delete drawing_context.json mid-pipeline — it is the shared state
- Do NOT run step5a without step2b having run first (clouds=0 problem)
- Do NOT skip step6 before step5c — master_tags.json feeds validation
- Do NOT use --workers > 1 on free-tier Gemini keys (429 rate limit)