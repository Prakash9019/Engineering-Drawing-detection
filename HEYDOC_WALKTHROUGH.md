# Engineering Drawing Detection — Project Walkthrough
### Prepared for HeyDoc AI call · 20 Nov 2026

---

## One-line summary

An end-to-end AI pipeline that reads scanned P&ID engineering drawings and produces a structured tag register (Excel deliverable) in under 10 minutes — a task that otherwise takes a human team multiple days per drawing.

---

## The Problem

P&ID (Piping & Instrumentation Diagram) drawings are the master reference documents for industrial plants. Every instrument, valve, or sensor on the drawing has a **tag** — a label like `V-FV-208` or `FIT-1001`. The client needs those tags extracted, normalised, and filed into a standard Excel format called Annexure-4.

Doing this manually: slow, error-prone, and doesn't scale. The challenge is that these are scanned images (not searchable PDFs), the tags are tiny text at arbitrary positions, and only the tags *inside revision clouds* (marked areas of a changed drawing) are relevant for a given revision.

---

## What I Built

A **5-phase Python pipeline** that orchestrates Gemini AI, Tesseract OCR, and OpenCV to solve every sub-problem in sequence:

```
Phase 1  Context       step1 → step2 → step2b → [step2c] → step3 → step4 → step6
                                                 ↑ optional human cloud review (browser)
Phase 2  Extraction    step5a  (SAHI + Gemini + Tesseract, 8 parallel workers)
Phase 3  Validate      step5b → step5b2 → step5c → step5d
                                ↑ connectivity graph · hierarchy · flow direction · control loops
Phase 4  Deliverable   step7 → step8
Phase 5  Reporting     eval_coverage · compare_final_vs_annexure4 · stage_visualizer
```

Every step reads/writes a shared `drawing_context.json` spine so each stage inherits everything the previous stages learned.

---

## Phase-by-phase breakdown

### Phase 1 — Context (understand the drawing before touching it)

**Step 1 — Format detection**
Classifies the file (raster scan vs vector PDF), applies CLAHE contrast enhancement to sharpen faint lines and thin text, and writes pixel dimensions to `drawing_context.json`.

**Step 2 — Title block reader**
Two Gemini calls extract the drawing number, revision, project, and revision history table. A pure-logic decision follows: is this a revision drawing? If so, `extraction_scope = CLOUD_ONLY`. That single field gates everything downstream.

**Step 2B — Revision cloud detection**
Finds the scalloped "cloud" shapes that mark changed regions. Two-stage: Gemini locates approximate bounding boxes across the full drawing; OpenCV morphological operations then trace the exact polygon boundary within each located region. A border/line-rejection pass filters out pipes running full-width — without it, long pipes would be misidentified as clouds.

Output: `outer_clouds_v2.json` (43 clouds on the test drawing), `cloud_mask_v2.png`, `overlay_v2.jpg`.

**Step 2C — Human cloud editor (optional)**
A browser-based editor (Flask + JS canvas) for reviewing/correcting step 2B's auto-detected clouds before extraction. Add, delete, merge, extend, or edit vertices. On "Done", writes `approved_clouds.json` as a drop-in replacement. Step 5A auto-prefers approved clouds when present.

**Steps 3, 4, 6 — Notes / SOW / tables**
Gemini reads the drawing's general notes, the Scope of Work Excel (which symbols are in-scope), and embedded data tables. This context feeds the validation and normalisation stages.

---

### Phase 2 — Extraction (the core AI step)

**Step 5A — Candidate tag extraction**

This is where most of the engineering is. The drawing (~A0 format) is too large to send to Gemini as one image — small tags would be invisible. Solution: **SAHI** (Slicing Aided Hyper Inference).

- Slice the drawing into 768-px tiles with 40% overlap
- Upscale each tile to 1024 px before sending to Gemini (better OCR at small sizes)
- Run up to 8 tiles in parallel (paid API; use `--workers 1` on free tier)
- For each tile: Gemini returns candidate tags with bounding boxes and a `functional_context` description
- Tesseract re-reads each candidate region and arbitrates the final text (accuracy ground truth)
- Intra-step dedup removes exact duplicates from overlapping tiles

The pipeline runs in two modes deliberately:
- **`--force-full-drawing`**: all ~315 tiles, ~239 candidates, 42/46 Annexure-4 recall
- **Cloud-scoped (default)**: only tiles intersecting cloud regions, ~25 candidates — this is the revision deliverable

These are kept as **two separate chains** because a cloud-scoped instrument can have a parent equipment outside the cloud. Hierarchy enrichment must always come from the full-drawing scan.

---

### Phase 3 — Validate & enrich

**Step 5B — Geometric association**
Hough line detection + LSD (Line Segment Detector) + skeleton analysis to find which pipe segments connect which tags. Outputs `step5b_associations.json`.

**Step 5B2 — Hierarchy, flow direction, control loops (no API by default)**
The most complex post-processing step. Builds a connectivity graph from step 5B associations, then:
- **Process hierarchy**: BFS from equipment nodes to find parent–child relationships (`PARENT_EQUIP: V-V-03`)
- **Track B — flow direction**: arrowhead detection, check-valve orientation, topology dead-leg analysis. Optional Gemini fallback for ambiguous category-D cases.
- **Track C — control loops**: dashed signal-line edges → `control_loops[]` groups; over-merged clusters quarantined as `signal_clusters`

Output: `step5b2_hierarchy_full.json` (primary, always off full-drawing associations).

**Steps 5C, 5D — Validation and dedup**
5C cross-checks candidates against the SOW symbol register and notes context. 5D resolves true duplicates from overlapping SAHI tiles using a 300-px proximity fragment-merge rule. Carries `validation_details` forward to step 7.

---

### Phase 4 — Deliverable

**Step 7 — CEDM normaliser**
Programmatic (no API). Applies ISA naming standards to produce all 15 Annexure-4 fields: tag number, tag type, description, service, line number, P&ID ref, etc. Auto-loads `step5b2_hierarchy_full.json` to inject `PARENT_EQUIP`, `FLOW: upstream|downstream`, `CONTROL_LOOP: <id>`, and `ISOLATED_DETECTION` into the REMARKS field. Issues a `STALE hierarchy` warning if fewer than 50% of candidate IDs match (guards against re-running extraction without re-running 5B2).

**Step 8 — Confidence router**
Scores each candidate and routes to `AUTO_ACCEPT` / `HUMAN_REVIEW` / `AUTO_REJECT`:

```
C_final = 0.30×C_detect + 0.30×C_text + 0.15×C_geometry + 0.20×C_validation + 0.05×C_registry

≥ 0.80 → AUTO_ACCEPT
0.55–0.80 → HUMAN_REVIEW  (P1–P4 priority)
< 0.55 → AUTO_REJECT
```

Isolated tags (no pipe/equipment/signal edge in the connectivity graph) get `c_geometry × 0.5` penalty → HUMAN_REVIEW.

---

### Phase 5 — Reporting

- `eval_coverage.py` — recall vs Annexure-4 ground truth, produces annotated drawing with every detected tag boxed
- `compare_final_vs_annexure4.py` — 5-sheet Excel comparison (match/missing/extra/SUMMARY)
- `stage_visualizer.py` — per-stage bounding-box overlay for debugging

---

## Results on test drawing (Rev C)

| Metric | Value |
|--------|-------|
| Drawing | `4224-MGDV-6-50-2004` Sheet 001 Rev C |
| Tags in Annexure-4 | 46 |
| Detected (step5a full) | 42/46 (91%) |
| In final_tags.xlsx | 41/46 unique |
| AUTO_ACCEPT | 142/198 rows (72%) |
| AUTO_REJECT | 0 |
| Avg confidence | 0.813 |
| Run time | ~5–10 min |
| Tags truly not on drawing | 2 (`V-ZSC-203`, `V-ZSO-203`) |

---

## Tech stack

| Layer | Tool | Why |
|-------|------|-----|
| Vision AI | Gemini 3.1 Pro Preview | Understands layout/symbols across the full image |
| Fast text extraction | Gemini 2.5 Flash | Title block, notes, tables — cheaper/faster |
| OCR ground truth | Tesseract | Deterministic character-level accuracy for tag text |
| Image processing | OpenCV | CLAHE, Hough lines, LSD, morphological ops for clouds |
| Tiling | SAHI | Sliced inference to maintain resolution on large drawings |
| Data | Pandas + openpyxl | Annexure-4 Excel output |
| Optional UI | Flask + JS canvas | Step 2C cloud editor |

---

## Key design decisions worth explaining

**1. Two-stage cloud detection (Gemini bbox → OpenCV polygon)**
Gemini alone can't give pixel-precise polygon coordinates. OpenCV alone can't decide what counts as a revision cloud vs a pipe segment. The combination does both.

**2. Two extraction chains, not one**
Full-drawing extraction feeds hierarchy. Cloud-scoped extraction feeds the deliverable. Collapsing them into one would mean either losing hierarchy context or inflating the deliverable with out-of-scope tags. Validated simplification gate: 50–100 drawing validation set, ≥99% recall, 0 critical misses — not yet reached, so both chains are kept.

**3. SAHI at 768px/40% overlap**
Getting the patch size right was empirical. Too small → tags cut across patch boundaries. Too large → Gemini loses resolution on small characters. 40% overlap ensures every point on the drawing appears in at least 2 patches, so a tag cut at one patch boundary is fully visible in the overlapping patch.

**4. Stale hierarchy guard**
`candidate_id` changes on every step5a re-run. If you re-run extraction but forget to re-run step5b2, step7 would silently produce zero hierarchy enrichment. The guard: if fewer than 50% of candidates in `step5b2_hierarchy_full.json` match the current step5a output, step7 logs `STALE hierarchy` and continues without enrichment rather than writing corrupt data.

**5. `drawing_context.json` as the pipeline spine**
Every step reads and updates a single shared JSON. This means any step can be re-run in isolation as long as its input files are present — no orchestration framework needed. The runbook is just shell commands; resuming after failure means deleting the step's output file and re-running from there.

---

## Claude Code / AI agent workflow

This pipeline was largely built with Claude Code (this repo has a full `.claude/` config). Key practices:

- **Context management**: `CLAUDE.md` captures the full architecture, decision rationale, and "do not" rules so Claude never re-learns the system mid-session
- **STEP7_NORMALIZATION.md**: deep-dive doc for the most complex normalisation logic so Claude can reason about edge cases without re-reading all the code
- **Two-track decision documented explicitly**: the hierarchy vs deliverable split is spelled out in CLAUDE.md with the exact validation gate conditions — Claude can propose changes without accidentally collapsing the two chains
- **Hooks and settings**: `.claude/settings.json` configured for the project's permission model and tool preferences
- **Memory system**: project context, design decisions, and feedback persist across sessions so each new conversation starts with full situational awareness

---

## What I'd improve next

1. **Validate the simplification gate**: run the FULL→FILTER comparison on 50+ drawings; if it passes, collapse to a single extraction chain
2. **Step 5A confidence calibration**: the 72% AUTO_ACCEPT rate leaves 28% in HUMAN_REVIEW — most are low-confidence text reads that a fine-tuned model on P&ID patches could lift
3. **Multi-drawing batch mode**: the pipeline is currently single-drawing; a job queue with shared Gemini caching would reduce per-drawing cost significantly
4. **step2c integration**: cloud editor currently runs as a separate Flask server; embedding it as a step in the pipeline with a proper approval callback would remove the manual hand-off

---

*Run order, exact commands, and all flags: `PIPELINE_RUNBOOK.md`*  
*Normalisation deep-dive: `STEP7_NORMALIZATION.md`*  
*Full architecture: `CLAUDE.md`*
