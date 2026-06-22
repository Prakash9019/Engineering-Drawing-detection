# CDCI P&ID Tag Extraction Pipeline
## Project Memory for Claude Code

> **Commands, run order, and output structure:** [`PIPELINE_RUNBOOK.md`](PIPELINE_RUNBOOK.md)  
> **Step 7 normalization deep-dive:** [`STEP7_NORMALIZATION.md`](STEP7_NORMALIZATION.md)

---

## What This Project Does

Reads scanned P&ID engineering drawings and produces a structured tag register in **Annexure-4 Excel format**.

| | |
|--|--|
| **Input** | `input_drawing.jpg` (or PDF), `ANNEXURE-2_*.xlsx` (SOW), `ANNEXURE-4_*.xlsx` (ground truth) |
| **Output** | `output/final_tags.xlsx` — AUTO_ACCEPT / HUMAN_REVIEW / SUMMARY sheets |
| **Spine** | `output/drawing_context.json` — every step reads/updates this |
| **Tech** | Gemini + Tesseract OCR + OpenCV |
| **Time** | ~5–10 min per drawing (paid API, 8 workers on step5a) |

---

## Active Scripts (use these only)

| Step | Script | API? |
|------|--------|------|
| 1 | `stages/step1_format_detect.py` | optional |
| 2 | `stages/step2_title_block.py` | yes |
| 2B | `stages/step2b_cloud_detection.py` | yes |
| 2C | `step2c_cloud_editor/step2c_cloud_editor.py` | no (browser, **optional**) |
| 3 | `stages/step3_notes_agent.py` | yes |
| 4 | `stages/step4_sow_agent.py build` | no (--skip-vision) |
| 6 | `stages/step6_table_agent.py` | yes |
| **5A** | `stages/step5a_candidate_extraction.py` | yes |
| 5B–5D | `step5b` / `step5c` / `step5d` | no |
| 5B2 | `stages/step5b2_hierarchy.py` | no (opt. `--gemini-flow-fallback`) |
| 7 | `stages/step7_cedm_normalizer.py` | no |
| 8 | `stages/step8_confidence_router.py` | no |
| QA | `eval_coverage.py`, `compare_final_vs_annexure4.py`, `stage_visualizer.py` | no |

**Do NOT use:** `step5a_live_annotator.py`, root-level step3 copies.

---

## Pipeline Phases

```
Phase 1  context     step1 → step2 → step2b → [step2c] → step3 → step4 → step6
                                              ↑ optional human cloud review
Phase 2  extraction  step5a  (SAHI + Gemini + Tesseract)
Phase 3  validate    step5b → step5b2 → step5c → step5d
                              ↑ connectivity/hierarchy/flow/control-loops; feeds step7
Phase 4  deliverable step7 → step8
Phase 5  reporting   eval_coverage, compare_final_vs_annexure4, stage_visualizer
```

---

## Key Architecture

### `drawing_context.json` is the spine
Never delete mid-pipeline. Holds `raster_path`, title block fields, `extraction_scope`, `revision_cloud_required`, paths to notes/SOW/tables.

### Cloud detection (step2b) → extraction scope (step5a)
- **step2b** writes `outer_clouds_v2.json`, `cloud_mask_v2.png`, `overlay_v2.jpg`
- Border/line rejection, Gemini bbox → per-crop polygon recovery
- **step5a** loads clouds + mask when `revision_cloud_required=true` and `extraction_scope=CLOUD_ONLY`
- **`--force-full-drawing`** on step5a bypasses cloud filter — use for full Annexure-4 recall

### Cloud review (step2c) — OPTIONAL human-in-the-loop
- **What:** browser editor to review/correct step2b's auto-detected clouds (add / delete / merge / extend / edit vertices), then approve the final geometry.
- **Inputs:** the drawing (`input_drawing.jpg`) + `outer_clouds_v2.json` from step2b (and optionally `overlay_v2.jpg` for the toggle view).
- **Outputs:** `approved_clouds.json`, `cloud_mask_approved.png`, `overlay_approved.jpg` — same schema/coord space as step2b, so they are drop-in.
- **Optional:** if you skip step2c, **step5a falls back to step2b's `outer_clouds_v2.json` automatically** — the pipeline does not break. Run it when you want human-verified cloud scope (higher accuracy).
- **step5a precedence:** `resolve_cloud_inputs()` prefers `approved_clouds.json`/`cloud_mask_approved.png` when present, else uses `outer_clouds_v2.json`/`cloud_mask_v2.png`. The chosen source is logged at startup.
- **CLI:**
  ```bash
  python3 step2c_cloud_editor/step2c_cloud_editor.py \
    --image input_drawing.jpg --clouds output/outer_clouds_v2.json \
    --overlay output/overlay_v2.jpg --out output/
  # opens browser; edit → Done; on save it writes the 3 approved files and exits.
  # add --no-browser / --port N for headless or port control
  ```
- Downstream stages (5b/5c/5d, eval) inherit cloud scope from step5a's filtered output — they do **not** load cloud JSON directly, so only step5a needs the fallback.

### Mega-cloud area filter (step5a) — fixed
`load_cloud_regions_from_step2b()` skips only a degenerate cloud ≥85% of the sheet. It now measures the ratio against `stats.image_size` (JSON space) consistently, not the loaded image dimensions. Previously a JSON-space cloud area was divided by image-space dimensions; when image res ≠ detection res this inflated the ratio and silently dropped large legitimate clouds (and every tag inside them).

### Extraction modes (step5a)

| Mode | When | Patches | Recall vs A4 |
|------|------|---------|--------------|
| `CLOUD_FILTER` | `CLOUD_ONLY` + step2b output | ~53 | Low (scope-limited) |
| `FULL_DRAWING` | `--force-full-drawing` | ~315 | **42/46** on test sheet |

### step5a design choices
- SAHI **768 px / 40% overlap**; patches upscaled to 1024 px for Gemini
- **No Gemini cloud prompt** (guesses wrong on small patches); cloud gating is code-only via mask
- `functional_context` from Gemini → used by step7 for descriptions
- Intra-step dedup: exact-same tag only (never merges sequential neighbours)

### step5d dedup
- Merges same physical tag from overlapping SAHI patches only
- Fragment merge rule for split OCR tags within 300 px
- Carries `validation_details` → step7 registry descriptions

### step7 → step8
- **step7:** programmatic CEDM normalisation → `step7_cedm_output.json` (15 Annexure-4 fields)
- **step8:** confidence scoring → `final_tags.xlsx` routing (AUTO_ACCEPT / HUMAN_REVIEW)

### step5b2 connectivity enrichment (run after 5B, before 7 — additive)
- `step5b2_hierarchy.py` (post-processor off step5b, no API unless `--gemini-flow-fallback`) writes `output/step5b2_hierarchy.json`: connectivity graph, process hierarchy, **Track B** flow direction (arrowheads/check-valves/propagation/equipment-convention/topology-dead-leg, + optional Gemini fallback `evidence=gemini_vision`), and **Track C** control loops (dashed signal-line edges → `control_loops[]`, over-merges quarantined as `signal_clusters`). Pass-through `associations`/`enriched_candidates` are byte-identical to its step5b input.
- **step7 auto-loads it** (`load_connectivity_map`, or `--hierarchy <path>`): adds `PARENT_EQUIP: <tag>` / `ISOLATED_DETECTION` / `FLOW: upstream|downstream` / `CONTROL_LOOP: <id>` to `REMARKS` and `_hier_*` provenance. Absent → step7 behaves exactly as before.
- **step8** reads `_hier_is_isolated`: isolated detections (no pipe/equipment/signal edge) get `c_geo × 0.5` → HUMAN_REVIEW (P3 `ISOLATED_DETECTION`). Signal edges (Track C) fix `is_isolated` via Option A (graph degree over all edges); they are excluded from the process rooting BFS (Decision A2).
- ⚠️ **Re-run step5b2 on every fresh extraction.** step7 joins by `candidate_id`, which changes when step5a/5b re-run. A stale `step5b2_hierarchy.json` → 0 matches → silent loss of all enrichment. step7 now logs a `STALE hierarchy` warning when <50% of candidates match.

See [`STEP7_NORMALIZATION.md`](STEP7_NORMALIZATION.md) for full normalization analysis.

---

## Models

| Model | Steps | Purpose |
|-------|-------|---------|
| `gemini-3.1-pro-preview` | 5A, 2B | Tag detection, cloud localization |
| `gemini-2.5-flash` | 2, 3, 4, 6 | Title block, notes, tables |
| Tesseract | 2, 3, 5A, 6 | Deterministic OCR ground truth |
| OpenCV | all | CLAHE, geometry, cloud morphology |

`settings.py` — imported by `core/`; do not delete.

---

## Benchmark — Test Drawing (`input_drawing.jpg`, Rev C)

**Full-sheet extraction** (`--force-full-drawing`):

```
Drawing:              4224-MGDV-6-50-2004  Sheet 001  Rev C
extraction_scope:     CLOUD_ONLY (but step5a run with --force-full-drawing)
step5a candidates:    ~239 (after dedup from ~432 raw)
Annexure-4 recall:    42/46 at step5a (eval_coverage)
                      41/46 unique in final_tags.xlsx (compare SUMMARY sheet)
Not on sheet:         V-ZSC-203, V-ZSO-203
Missing extracted:    V-BV-2355, V-FE-224, V-V-201 (+ 2 not drawn)
AUTO_ACCEPT:          ~142/198 (72%)
AUTO_REJECT:          0
Clouds (step2b):      ~23 outer + ~20 inner → see overlay_v2.jpg
```

**Metrics — which tool to use:**

| Question | Tool | Metric |
|----------|------|--------|
| Did we detect the tag on the drawing? | `eval_coverage.py` | Unique **42/46** |
| What's in the client Excel vs register? | `compare_final_vs_annexure4.py` | SUMMARY sheet **41/46 unique** |
| Row count in AUTO_ACCEPT matching A4 | compare script | **46 rows** (includes duplicates) |

---

## Key Output Files

| File | Step | Purpose |
|------|------|---------|
| `drawing_context.json` | 1–6 | Shared pipeline state |
| `outer_clouds_v2.json` | 2B | Cloud polygons + bboxes |
| `step5a_candidates.json` | 5A | Raw detected tags + bboxes |
| `step5b2_hierarchy.json` | 5B2 | Graph, hierarchy, flow direction, `control_loops[]` — feeds step7 enrichment |
| `step5_final_output.json` | 5D | PRIMARY candidates only |
| `step7_cedm_output.json` | 7 | Normalised Annexure-4 records |
| `final_tags.xlsx` | 8 | **Client deliverable** |
| `final_tags_vs_annexure4.xlsx` | compare | 5 sheets incl. SUMMARY |
| `eval_coverage_report.json` | eval | Found/missing vs A4 |

---

## Confidence Routing (step8)

```
C_final = 0.30×C_detect + 0.30×C_text + 0.15×C_geometry + 0.20×C_validation + 0.05×C_registry

≥ 0.80 → AUTO_ACCEPT
0.55–0.80 → HUMAN_REVIEW (P1–P4)
< 0.55 → AUTO_REJECT
```

When Tesseract is silent, C_text falls back to Gemini read confidence.

---

## What NOT to Do

- Do NOT use `step5a_live_annotator.py` for pipeline runs
- Do NOT delete `drawing_context.json` mid-pipeline
- Do NOT run step5a before step1 (needs `raster_path`)
- Do NOT skip step6 before step5c (`master_tags.json` feeds validation)
- Do NOT use `--workers > 1` on free-tier Gemini keys (429)
- Do NOT compare Annexure-4 recall in cloud-scoped mode — use `--force-full-drawing`
- Do NOT modify `step2b_cloud_detection.py` without understanding border/line rejection impact

---

## Quick Commands

```bash
cd /Users/suryprakash/Downloads/cdci_extractor_final
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
export DRAWING="input_drawing.jpg"

# Full pipeline — see PIPELINE_RUNBOOK.md for complete block

# Re-run extraction only (full sheet)
python3 stages/step5a_candidate_extraction.py \
  --context output/drawing_context.json --api-key $GEMINI_KEY --workers 8 --force-full-drawing

# Recall check
python3 stages/eval_coverage.py \
  --candidates output/step5a_candidates.json \
  --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx \
  --image $DRAWING --out output/

# Excel vs register comparison
python3 stages/compare_final_vs_annexure4.py
```
