# CDCI P&ID Tag Extraction Pipeline — Runbook

> **Single source of truth.** All commands below are copy-paste ready.  
> **Last updated:** June 2026 (adds step5b2 hierarchy/flow/control-loops in Phase 3 + step7 enrichment & stale-hierarchy guard; step2b border filtering, step5a cloud-mask gating, compare tool).

---

## What you get at the end

| Deliverable | Path | Purpose |
|-------------|------|---------|
| **Client Excel** | `output/final_tags.xlsx` | Annexure-4 format — `AUTO_ACCEPT`, `HUMAN_REVIEW`, `SUMMARY` sheets |
| **Review queue** | `output/human_review_queue.json` | Tags routed for human review (P1–P4 priority) |
| **Audit log** | `output/audit_log.json` | Auto-rejected records (usually empty) |
| **Coverage report** | `output/eval_coverage_report.json` | Recall vs Annexure-4 ground truth |
| **Comparison Excel** | `output/final_tags_vs_annexure4.xlsx` | Pipeline output vs Annexure-4 (4 comparison sheets) |
| **Annotated drawing** | `output/step5a_eval_annotated_fullres.jpg` | Every detected tag boxed (green = in register) |

### Benchmark on test drawing (`input_drawing.jpg`, Rev C)

Two extraction modes matter — pick the one that matches your goal:

| Mode | Command flag | Annexure-4 recall | Candidates | Use when |
|------|--------------|-------------------|------------|----------|
| **Full sheet** | `--force-full-drawing` | **42/46 (91%)** | ~239 | Compare against full Annexure-4 register |
| **Cloud scope** | *(default for `CLOUD_ONLY` drawings)* | **~2–25/46** | ~25 | Drawing says "clouded areas only" |

The 2 tags never on this sheet: `V-ZSC-203`, `V-ZSO-203` (in register, not drawn).

Latest full-sheet run stats: `AUTO_ACCEPT 142/198 (71.7%)`, `AUTO_REJECT 0`, avg confidence `0.813`.

---

## Prerequisites (run once per machine)

```bash
cd /Users/suryprakash/Downloads/cdci_extractor_final
python3 -m venv .venv          # skip if already exists
source .venv/bin/activate
pip install -r requirements.txt
brew install tesseract         # macOS OCR dependency
export $(grep -v '^#' .env | xargs)
echo $GEMINI_KEY               # must print your key

export DRAWING="input_drawing.jpg"
export REGISTER="ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx"

```

| Setting | Value |
|---------|-------|
| Gemini model (step5a, step2b) | `gemini-3.1-pro-preview` |
| SAHI patch size / overlap | `768 px` / `40%` |
| Free-tier API key (5 RPM) | add `--workers 1` to step5a |

---

## Full pipeline — all commands in one place

```bash
cd /Users/suryprakash/Downloads/cdci_extractor_final
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
export DRAWING="input_drawing.jpg"
export REGISTER="ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx"
export SOW="ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx"

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Context (run once per drawing, ~2–4 min, Gemini API)
# ══════════════════════════════════════════════════════════════════════════════

python3 stages/step1_format_detect.py  $DRAWING --out output/ --api-key $GEMINI_KEY
python3 stages/step2_title_block.py    --context output/drawing_context.json --api-key $GEMINI_KEY
python3 stages/step2b_cloud_detection.py $DRAWING --out output/ --api-key $GEMINI_KEY

# OPTIONAL — step2c: human review & correction of revision clouds (browser, no API).
# Skip it and step5a falls back to step2b output automatically (pipeline still runs).
python3 step2c_cloud_editor/step2c_cloud_editor.py \
  --image $DRAWING --clouds output/outer_clouds_v2.json \
  --overlay output/overlay_v2.jpg --out output/
# edit clouds in browser → click Done → writes approved_clouds.json + cloud_mask_approved.png + overlay_approved.jpg, then exits

python3 stages/step3_notes_agent.py    --context output/drawing_context.json --api-key $GEMINI_KEY
python3 stages/step4_sow_agent.py build --excel $SOW --out output/ --skip-vision
python3 stages/step6_table_agent.py    --context output/drawing_context.json --api-key $GEMINI_KEY

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Tag extraction (~1–5 min depending on mode, Gemini API)
# ══════════════════════════════════════════════════════════════════════════════

# Option A — FULL SHEET (best Annexure-4 recall, recommended for register comparison)
python3 stages/step5a_candidate_extraction.py \
  --context output/drawing_context.json --api-key $GEMINI_KEY --workers 8 \
  --force-full-drawing

# Option B — CLOUD SCOPE ONLY (default when drawing_context has extraction_scope=CLOUD_ONLY)
python3 stages/step5a_candidate_extraction.py \
  --context output/drawing_context.json --api-key $GEMINI_KEY --workers 8

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — Post-processing (no API, < 30 s)
# ══════════════════════════════════════════════════════════════════════════════

python3 stages/step5b_geometric_association.py \
  --candidates output/step5a_candidates.json --image $DRAWING --out output/

# step5b2 — connectivity graph + hierarchy + flow direction (Track B) + control
# loops (Track C). Side-branch off step5b; REQUIRED before step7 — step7 reads
# step5b2_hierarchy.json for PARENT_EQUIP / FLOW / CONTROL_LOOP / isolation
# enrichment. MUST be re-run whenever step5a/step5b change, or step7 silently
# uses a STALE hierarchy (candidate_ids won't match → zero enrichment).
# Deterministic by default; add --gemini-flow-fallback for category-D flow (~6
# Gemini calls, cached in gemini_flow_cache.json).
python3 stages/step5b2_hierarchy.py \
  --associations output/step5b_associations.json --image $DRAWING --out output/
  # optional: --gemini-flow-fallback --api-key $GEMINI_KEY

python3 stages/step5c_validation_engine.py \
  --associations output/step5b_associations.json \
  --register $REGISTER --notes output/notes_context.json --out output/

python3 stages/step5d_duplicate_resolution.py \
  --validated output/step5c_validated.json --out output/

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — Deliverable (no API, < 10 s)
# ══════════════════════════════════════════════════════════════════════════════

python3 stages/step7_cedm_normalizer.py \
  --final output/step5_final_output.json \
  --context output/drawing_context.json --out output/ --project CDCI

python3 stages/step8_confidence_router.py \
  --cedm output/step7_cedm_output.json \
  --context output/drawing_context.json --out output/

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 5 — Reporting / QA (optional, no API)
# ══════════════════════════════════════════════════════════════════════════════

python3 stages/eval_coverage.py \
  --candidates output/step5a_candidates.json \
  --register $REGISTER --image $DRAWING --out output/

python3 stages/compare_final_vs_annexure4.py \
  --final output/final_tags.xlsx --register $REGISTER \
  --out output/final_tags_vs_annexure4.xlsx

python3 stages/stage_visualizer.py \
  --candidates output/step5a_candidates.json \
  --deduped output/step5d_deduped.json \
  --sow output/sow_symbol_memory.json \
  --image $DRAWING --out output/stages/
```

**Resume after failure:** each step writes its own output file. Delete that file and re-run from that step forward. Never delete `output/drawing_context.json` mid-pipeline.

---

## Output folder structure

After a full run, `output/` looks like this:

```
output/
│
├── drawing_context.json              ← MASTER SPINE — every step reads/updates this
├── title_block_context.json          ← full title-block extraction detail
│
├── input_drawing_enhanced_binary.png ← step1 CLAHE + binarize (for OCR)
│
├── outer_clouds_v2.json              ← step2b cloud polygons + bboxes
├── overlay_v2.jpg                    ← step2b visual QA (green=outer, cyan=inner)
├── cloud_mask_v2.png                 ← step2b filled cloud mask
├── border_filter.jpg                 ← step2b debug: rejected border candidates (red)
│
├── approved_clouds.json              ← step2c (OPTIONAL) human-approved clouds — step5a prefers this
├── cloud_mask_approved.png           ← step2c human-approved filled mask
├── overlay_approved.jpg              ← step2c visual verification
│
├── notes_context.json                ← step3 structured notes
├── rules_prompt_block.txt            ← step3 rules injected into ALL Gemini prompts
├── sow_symbol_memory.json            ← step4 SOW scope (100 ALLOW + 32 BLOCK)
├── sow_scope_summary.txt             ← step4 human-readable SOW summary
├── master_tags.json                  ← step6 tag-list table rows (may be empty on sheet 001)
├── tables_context.json               ← step6 table metadata
│
├── step5a_candidates.json            ← ★ raw detected tags + bboxes (main extraction output)
├── step5a_eval_annotated_fullres.jpg ← eval_coverage: full-res tag boxes
├── step5a_eval_annotated.jpg         ← eval_coverage: scaled overview
├── step5b_associations.json          ← geometry: pipe/equipment links per tag
├── step5b2_hierarchy.json            ← graph + hierarchy + flow + control_loops[] (feeds step7)
├── gemini_flow_cache.json            ← step5b2 --gemini-flow-fallback cache (optional)
├── step5c_validated.json             ← ISA-5.1 + registry validation per tag
├── step5d_deduped.json               ← all records incl. DISCARDED duplicates
├── step5_final_output.json           ← PRIMARY candidates only (feeds step7/8)
├── step7_cedm_output.json            ← normalised Annexure-4 field records
├── step8_routing_summary.json        ← accept/review/reject counts + confidence stats
│
├── final_tags.xlsx                   ← ★ CLIENT DELIVERABLE
├── human_review_queue.json           ← tags needing human review
├── audit_log.json                    ← auto-rejected tags
├── eval_coverage_report.json         ← found/missing vs Annexure-4
├── final_tags_vs_annexure4.xlsx      ← 4-sheet pipeline vs register comparison
│
├── stages/                           ← stage_visualizer QA images
│   ├── manifest.json
│   ├── 5a_detection.jpg              ← all detected tags
│   ├── sow_detected.jpg / sow_filtered.jpg
│   └── dup_detected.jpg / dup_filtered.jpg
│
└── debug_crops/                      ← step2b --debug per-crop binarize images
```

---

## Step-by-step reference

### Phase 1 — Context

| Step | Command | API? | Time | Key outputs | What the command does |
|------|---------|------|------|-------------|----------------------|
| **1** | `step1_format_detect.py $DRAWING` | optional | ~10s | `drawing_context.json`, `*_enhanced_binary.png` | Detect raster/PDF, set `raster_path`, `width_px`, `height_px`, document type |
| **2** | `step2_title_block.py --context …` | yes | ~30s | updates `drawing_context.json`, `title_block_context.json` | Read dwg no, sheet, rev, title; set `revision_mode`, `extraction_scope`, `project_mode` |
| **2B** | `step2b_cloud_detection.py $DRAWING` | yes | ~20–60s | `outer_clouds_v2.json`, `overlay_v2.jpg`, `cloud_mask_v2.png`, `border_filter.jpg` | Gemini localizes cloud bboxes → OpenCV traces scalloped polygons; rejects drawing borders |
| **2C** *(optional)* | `step2c_cloud_editor/step2c_cloud_editor.py --image $DRAWING --clouds output/outer_clouds_v2.json` | no (browser) | manual | `approved_clouds.json`, `cloud_mask_approved.png`, `overlay_approved.jpg` | Human reviews step2b clouds in a browser (add/delete/merge/extend/edit), approves geometry → writes approved files, then exits |
| **3** | `step3_notes_agent.py --context …` | yes | ~1–2m | `notes_context.json`, `rules_prompt_block.txt` | Extract notes/abbreviations; build drawing-specific prompt rules |
| **4** | `step4_sow_agent.py build --excel $SOW` | no* | ~5s | `sow_symbol_memory.json`, `sow_scope_summary.txt` | Build 100-ALLOW / 32-BLOCK symbol scope memory (*vision optional) |
| **6** | `step6_table_agent.py --context …` | yes | ~30s | `master_tags.json`, `tables_context.json` | Find tag-list tables on drawing (may be 0 tags on sheet 001) |

**Console output to expect (step2b):**
```
Outer clouds: 23
Inner clouds: 20
Total:        43
Overlay:      output/overlay_v2.jpg
JSON:         output/outer_clouds_v2.json
```

#### Step 2C — interactive cloud editor (OPTIONAL)

A browser-based human-in-the-loop editor that sits **between step2b and step3**. Use it when you want a person to verify/fix the revision-cloud geometry before extraction.

```bash
python3 step2c_cloud_editor/step2c_cloud_editor.py \
  --image $DRAWING \
  --clouds output/outer_clouds_v2.json \
  --overlay output/overlay_v2.jpg \
  --out output/
# flags: --port N (default 8765, auto-tries +10 if busy), --no-browser
```

| | |
|--|--|
| **Reads** | `$DRAWING`, `outer_clouds_v2.json` (step2b), optional `overlay_v2.jpg` |
| **Writes** | `approved_clouds.json`, `cloud_mask_approved.png`, `overlay_approved.jpg` |

**What success looks like:**
- Browser opens to the editor showing every step2b *outer* cloud overlaid on the drawing.
- You add/delete/merge/extend/edit clouds, then click **Done → Save & Exit**.
- Terminal prints:
  ```
  ============================================================
    Approved clouds saved to: output/
      approved_clouds.json   (21 clouds)
      cloud_mask_approved.png
      overlay_approved.jpg
  ============================================================
  ```
  and the script exits (server shuts down). The 3 files now exist in `output/`.
- `approved_clouds.json` is in **JSON space** (`stats.image_size = [9934, 7017]`) and backward-compatible with step5a; `cloud_mask_approved.png` is grayscale (0/255) at full image resolution.

**If you skip step2c (no human review):** nothing breaks. step5a's `resolve_cloud_inputs()` prefers `approved_clouds.json` only when it exists; otherwise it uses step2b's `outer_clouds_v2.json` + `cloud_mask_v2.png`. step5a logs which source it chose at startup:
```
Cloud source: approved (human-verified, step2c)      # step2c was run
  OR
Cloud source: auto-detected (step2b, no human review)  # step2c skipped
```
Only step5a loads cloud data; downstream stages (5b/5c/5d, eval) inherit the scope from step5a's filtered output, so no other stage needs the fallback.

> **Re-running:** delete `output/approved_clouds.json` (and `cloud_mask_approved.png`) to make step5a revert to step2b's auto-detected clouds.

**`drawing_context.json` fields added across Phase 1:**

| Field | Set by | Meaning |
|-------|--------|---------|
| `raster_path` | step1 | Image path for all downstream steps |
| `width_px`, `height_px` | step1 | Drawing dimensions |
| `drawing_number`, `sheet_number`, `revision_code` | step2 | Title block identity |
| `revision_mode` | step2 | `CLOUD_SCOPE_MODE` / `REVISION_DRAWING` / `NEW_DRAWING` |
| `extraction_scope` | step2 | `CLOUD_ONLY` / `CLOUD_PRIORITY` / `FULL_DRAWING` |
| `revision_cloud_required` | step2 | `true` → step5a applies cloud filter |
| `project_mode` | step2 | `FULL_EXTRACTION` or `COUNT_ONLY` (LC/GC vs LT/GT prefix) |
| `rules_prompt_block_path` | step3 | Path to injected Gemini rules |
| `sow_memory_path` | step4 | Path to SOW scope memory |
| `master_tags_path` | step6 | Path to table-extracted tags |

---

### Phase 2 — Extraction (step5a)

```bash
python3 stages/step5a_candidate_extraction.py \
  --context output/drawing_context.json \
  --api-key $GEMINI_KEY \
  --workers 8 \
  [--force-full-drawing]   # add this for full-sheet extraction
```

| | |
|--|--|
| **Reads** | `drawing_context.json`, `approved_clouds.json`+`cloud_mask_approved.png` **if present (step2c)**, else `outer_clouds_v2.json`+`cloud_mask_v2.png` (step2b), `sow_symbol_memory.json`, `rules_prompt_block.txt` |
| **Writes** | `step5a_candidates.json`, `step5a_patches/` (with `--debug`) |
| **API calls** | ~53 patches (cloud scope) or ~315 patches (full sheet) |
| **Time** | ~1 min (cloud) / ~4 min (full, 8 workers) |

**How cloud gating works (step2b → step5a):**

```
step2c approved_clouds.json + cloud_mask_approved.png   (preferred if step2c was run)
  └─ else → step2b outer_clouds_v2.json + cloud_mask_v2.png
        ↓
step5a loads cloud regions (skips a single cloud ≥85% of sheet area,
        measured in JSON space via stats.image_size)
        ↓
If extraction_scope=CLOUD_ONLY and revision_cloud_required=true:
  → only SAHI patches overlapping cloud mask are sent to Gemini
  → only candidates whose centre falls inside cloud mask are kept
        ↓
If --force-full-drawing:
  → all patches processed, no cloud post-filter
  → best for Annexure-4 register comparison
```

**Console output to expect:**
```
Mode: CLOUD_FILTER (N step2b regions, polygon gate)   # cloud scope
  OR
Mode: FULL_DRAWING (step2b loaded N regions, filter off)  # --force-full-drawing

Candidates extracted: <count>
  instrument   <n>
  valve        <n>
  piping       <n>
```

**`step5a_candidates.json` schema (per candidate):**
`candidate_id`, `tag_text`, `symbol_name`, `symbol_category`, `symbol_bbox`, `tag_bbox`, `scope_type`, `ocr_confidence`, `vision_confidence`, `functional_context`, `sow_status`

---

### Phase 3 — Post-processing

| Step | Command | Writes | What it does |
|------|---------|--------|--------------|
| **5B** | `step5b_geometric_association.py` | `step5b_associations.json` | OpenCV: link tags to pipes, leader lines, equipment (`ATTACHED_TO`, `CONTAINED_WITHIN`) |
| **5B2** | `step5b2_hierarchy.py` | `step5b2_hierarchy.json` | Connectivity graph + hierarchy + Track B flow direction + Track C control loops. **Side-branch off 5B; feeds step7 enrichment.** Optional `--gemini-flow-fallback`. |
| **5C** | `step5c_validation_engine.py` | `step5c_validated.json` | ISA-5.1 regex validation + Annexure-4 registry lookup; adds `validation_details` |
| **5D** | `step5d_duplicate_resolution.py` | `step5d_deduped.json`, `step5_final_output.json` | Recall-safe dedup: merges same tag from overlapping patches only; `step5_final_output.json` = PRIMARY only |

**Candidate count shrinks through Phase 3** (example full-sheet run):
`step5a: 239` → `step5d PRIMARY: ~198` (duplicates discarded)

> ⚠️ **step5b2 must be re-run on every fresh extraction.** step7 reads `step5b2_hierarchy.json` for connectivity enrichment (`PARENT_EQUIP` / `FLOW` / `CONTROL_LOOP` / isolation). It joins by `candidate_id`, which changes whenever step5a/5b re-run. A stale hierarchy → **0 matches → silent loss of all enrichment** (step7 now logs a `STALE hierarchy` warning when this happens).

---

### Phase 4 — Deliverable

| Step | Command | Writes | What it does |
|------|---------|--------|--------------|
| **7** | `step7_cedm_normalizer.py` | `step7_cedm_output.json` | Normalise tag text; fill all 15 Annexure-4 columns; description priority: registry → `functional_context` → ontology |
| **8** | `step8_confidence_router.py` | `final_tags.xlsx`, `human_review_queue.json`, `audit_log.json`, `step8_routing_summary.json` | Score each tag; route to AUTO_ACCEPT / HUMAN_REVIEW / AUTO_REJECT |

**`final_tags.xlsx` sheets:**

| Sheet | Contents |
|-------|----------|
| `AUTO_ACCEPT` | High-confidence tags (≥ 0.80) — ready for client |
| `HUMAN_REVIEW` | Medium-confidence tags with `C_FINAL`, `REVIEW_PRIORITY`, `REVIEW_REASON` |
| `SUMMARY` | Run statistics |

---

### Phase 5 — Reporting / QA

| Tool | Command | Writes | What you learn |
|------|---------|--------|----------------|
| `eval_coverage.py` | see full block above | `eval_coverage_report.json`, annotated JPGs | `FOUND x/46` vs Annexure-4; lists missing tags |
| `compare_final_vs_annexure4.py` | see full block above | `final_tags_vs_annexure4.xlsx` | 4 sheets: AUTO_ACCEPT in/not-in A4, HUMAN_REVIEW in/not-in A4 |
| `stage_visualizer.py` | see full block above | `output/stages/*.jpg` + `manifest.json` | Visual detect vs SOW-filter vs dedup stages |

**`final_tags_vs_annexure4.xlsx` sheets:**

| Sheet | Meaning |
|-------|---------|
| `AUTO_ACCEPT_In_A4` | Auto-accepted tags that match Annexure-4 |
| `AUTO_ACCEPT_Not_In_A4` | Auto-accepted tags not in register |
| `HUMAN_REVIEW_In_A4` | Review-queue tags that match Annexure-4 |
| `HUMAN_REVIEW_Not_In_A4` | Review-queue tags not in register |

---

## Quick health checks

```bash
cd /Users/suryprakash/Downloads/cdci_extractor_final
source .venv/bin/activate

# Candidate counts at each stage
python3 -c "import json; d=json.load(open('output/step5a_candidates.json')); print('step5a:', len(d['candidates']))"
python3 -c "import json; d=json.load(open('output/step5_final_output.json')); print('step5d PRIMARY:', len(d['candidates']))"

# Annexure-4 recall
python3 -c "
import json
d=json.load(open('output/eval_coverage_report.json'))
print(f'FOUND {len(d[\"found\"])}/{d[\"ground_truth_count\"]}  missing={len(d[\"missing\"])}  extra={d[\"extra_count\"]}')
"

# Routing distribution
python3 -c "import json; d=json.load(open('output/step8_routing_summary.json')); print(d['totals']); print(d['rates'])"

# Cloud detection stats
python3 -c "import json; d=json.load(open('output/outer_clouds_v2.json')); print(d['stats'])"

# Drawing scope from context
python3 -c "import json; c=json.load(open('output/drawing_context.json')); print(c['extraction_scope'], c['revision_cloud_required'], c['project_mode'])"

# Duplicate rate
python3 -c "
import json; d=json.load(open('output/step5d_deduped.json'))
p=sum(1 for r in d['all_records'] if r['duplicate_status']=='PRIMARY')
x=sum(1 for r in d['all_records'] if r['duplicate_status']=='DISCARDED')
print(f'PRIMARY={p} DISCARDED={x} dup_rate={x/(p+x)*100:.1f}%')
"
```

---

## Re-run shortcuts

```bash
# Re-run extraction only (skip Phase 1)
rm -f output/step5a_candidates.json
python3 stages/step5a_candidate_extraction.py \
  --context output/drawing_context.json --api-key $GEMINI_KEY --workers 8 --force-full-drawing
# then Phase 3 + 4 commands above

# Re-run from dedup onwards (cheapest)
python3 stages/step5d_duplicate_resolution.py --validated output/step5c_validated.json --out output/
python3 stages/step7_cedm_normalizer.py --final output/step5_final_output.json --context output/drawing_context.json --out output/ --project CDCI
python3 stages/step8_confidence_router.py --cedm output/step7_cedm_output.json --context output/drawing_context.json --out output/

# Re-run cloud detection only
python3 stages/step2b_cloud_detection.py input_drawing.jpg --out output/ --api-key $GEMINI_KEY
# inspect output/overlay_v2.jpg before running step5a

# Test a single SAHI patch (debug)
python3 stages/step5a_candidate_extraction.py \
  --context output/drawing_context.json --api-key $GEMINI_KEY --patch 19 --debug
```

---

## Confidence routing (step8)

```
C_final = 0.30 × C_detect      (Gemini vision_confidence)
        + 0.30 × C_text          (Tesseract OCR; falls back to model read if silent)
        + 0.15 × C_geometry      (association_confidence from step5b)
        + 0.20 × C_validation      (ISA check scores from step5c)
        + 0.05 × C_registry      (1.0 if in register, 0.5 if not)

≥ 0.80 → AUTO_ACCEPT
0.55–0.80 → HUMAN_REVIEW  (P1=validation fail, P2=weak text, P3=novel/scope)
< 0.55 → AUTO_REJECT
```

---

## Architecture notes (June 2026)

### Active scripts — use these

| Script | Status |
|--------|--------|
| `stages/step5a_candidate_extraction.py` | **ACTIVE** extraction |
| `stages/step3_notes_agent.py` | **ACTIVE** notes agent |
| `stages/step2b_cloud_detection.py` | **ACTIVE** cloud detector |
| `stages/step5a_live_annotator.py` | reference only — do not use for pipeline runs |

### Key tuning applied

**step2b (cloud detection)**
- Gemini bbox localization → per-crop morphological boundary recovery
- Border/frame rejection (`border_filter.jpg` shows what was removed)
- Line-artifact rejection (extent, compactness, edge-touch)
- Stage-1 OpenCV for additional small-cloud coverage
- Outputs outer + inner clouds in JSON; step5a uses `cloud_mask_v2.png` for gating

**step5a (extraction)**
- No Gemini cloud prompt (Gemini guesses wrong on small patches)
- SAHI `768 px / 40% overlap`; patches upscaled to 1024 px before Gemini
- Cloud mask gating when `extraction_scope=CLOUD_ONLY`
- `--force-full-drawing` bypasses cloud filter for full register recall
- Valve-bank + switch-pair prompt; `functional_context` field for descriptions
- Intra-step dedup: exact-same tag only (never merges sequential neighbours)

**step5d (dedup)**
- Merges same tag only (exact or OCR-confusable `0`↔`O`)
- Fragment merge rule for split OCR tags within 300 px
- Carries `validation_details` to step7 for registry descriptions

**step7 (normalisation)**
- Description priority: Annexure-4 registry → `functional_context` → ontology
- `10"` → `10IN` canonical tag normalisation

---

## Known limitations

- `V-ZSC-203` / `V-ZSO-203` are in Annexure-4 but **not drawn** on sheet 001 — cannot be extracted.
- Tag-list table lives on companion sheet 002 — step6 returns 0 tags on sheet 001 (expected).
- 46-tag register is sparse vs ~200+ detected tags — most novel tags route to HUMAN_REVIEW by design.
- Cloud-scoped extraction (`CLOUD_ONLY`) will miss tags outside revision clouds even if they are in Annexure-4.
- Cloud detection quality depends on drawing scan quality; always inspect `output/overlay_v2.jpg` before trusting cloud-scoped extraction.

---

## What NOT to do

- Do NOT use `step5a_live_annotator.py` for pipeline runs
- Do NOT delete `output/drawing_context.json` mid-pipeline
- Do NOT run step5a before step1 (needs `raster_path` in context)
- Do NOT skip step6 before step5c (`master_tags.json` feeds validation)
- Do NOT use `--workers > 1` on free-tier Gemini keys (HTTP 429)
- Do NOT compare Annexure-4 recall using cloud-scoped mode — use `--force-full-drawing`
