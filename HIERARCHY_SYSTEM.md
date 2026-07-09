# CDCI Hierarchy Extraction System — Complete Documentation
# ============================================================
# Step 5B / Step 5B3 / Step 5B2 / Step 9 | P&ID Hierarchy & Association Extraction
# Last updated: June 2026
# ============================================================

## What this system does

Takes a P&ID engineering drawing, extracts every symbol (instruments,
valves, equipment, pipelines), and builds a navigable hierarchy showing:
- Which equipment each instrument belongs to
- How pipelines connect equipment
- Which instruments belong to the same control loop
- Flow direction through the process

The output is consumed by MBOM generation.

**Status summary for stakeholders:** The system is fully functional and
validated end-to-end on one real client P&ID drawing (4224-MGDV-6-50-2004,
Ethane Gas Compressor K-V-201). It currently achieves 75% equipment-parent
coverage with a fully auditable evidence trail for every relationship. The
known accuracy ceiling and the plan to raise it are documented in
**Future Plan** below. The system has not yet been validated on a second
real client drawing — this is the single highest-priority next step before
further accuracy investment is justified.

---

## Architecture — files and their roles

```
input_drawing.jpg
      │
      ▼
step5a_candidate_extraction.py    ← detects all symbols (YOLO + Gemini)
      │
      ▼  step5a_candidates_full.json (full drawing, always used for hierarchy)
      │
      ▼
step5b_geometric_association.py   ← detects pipe segments (CV), scale-aware
      │
      ▼  step5b_pipe_segments.json + step5b_associations_full.json
      │
      ▼
step5b3_pipe_connectivity.py      ← Gemini Pipeline Connectivity Agent (NEW)
      │
      ├── Task A: gap bridging (verify broken pipe segments)
      ├── Task C: tee vs crossing (verify ambiguous junctions)
      ├── Task D: pipeline tracing (trace a pipe through valves/symbols)
      ▼
      step5b3_verified_graph.json   ← repaired, Gemini-verified pipe graph
      │
      ▼
step5b2_hierarchy.py              ← builds the full hierarchy graph (THE MAIN FILE)
      │
      ├── [--verified-graph]       ← loads step5b3 output instead of raw CV
      ├── [--gemini-attach]        ← Gemini instrument-to-equipment assignment
      ├── [--gemini-attach-workers 8]  ← parallel workers (default 8)
      ▼
      step5b2_hierarchy_full.json
      │
      ▼
step9_hierarchy_deliverables.py   ← exports to Excel, HTML viewer, graph
      │
      ├── output/final_hierarchy.xlsx
      ├── output/hierarchy_viewer.html
      ├── output/hierarchy_graph.html
      └── output/hierarchy_validation_report.xlsx

      ▼
visualize_hierarchy.py            ← standalone visual verification overlay
      │
      └── output/hierarchy_verification.jpg  (run anytime, any stage)
```

---

## How to run

```bash
# Step 1: Pipe detection (CV, scale-aware, persists segments for step5b3)
python stages/step5b_geometric_association.py \
    --image input_drawing.jpg \
    --out output/

# Step 2: Gemini Pipeline Connectivity Agent — repairs the CV pipe graph
# (gap bridging, tee/crossing verification, pipeline tracing)
python stages/step5b3_pipe_connectivity.py \
    --segments output/step5b_pipe_segments.json \
    --image input_drawing.jpg \
    --out output/ \
    --api-key $GEMINI_KEY \
    --workers 8 \
    --limit-gaps -1 \
    --limit-traces -1

# Step 3: Hierarchy on the verified graph + Gemini instrument attachment
python stages/step5b2_hierarchy.py \
    --associations output/step5b_associations_full.json \
    --image input_drawing.jpg \
    --out output/ \
    --verified-graph output/step5b3_verified_graph.json \
    --gemini-attach \
    --gemini-attach-workers 8 \
    --api-key $GEMINI_KEY

# Step 4: Generate deliverables
python stages/step9_hierarchy_deliverables.py \
    --hierarchy output/step5b2_hierarchy_full.json \
    --context output/drawing_context.json \
    --out output/

# Step 5: Visual verification (run anytime, regenerates from current JSON)
python stages/visualize_hierarchy.py \
    --hierarchy output/step5b2_hierarchy_full.json \
    --image input_drawing.jpg \
    --out output/hierarchy_verification.jpg
```

**Total run time:** ~8 minutes end to end (8 Gemini workers, cache-backed —
re-runs after the first are much faster since unchanged regions hit cache).
**Total Gemini cost (one full drawing):** ~$1.50–2.00.

---

## What was built — phase by phase

### Phase 1 — Graph foundation (CV only)

Converts 9,882 raw line detections into a clean pipe network.

| What | How | Result |
|------|-----|--------|
| Pipe segment cleaning | Morphological detection with zone filters (border margin 200px, title block y>80%, ref panel x>88%, min length 250px) | 220 real pipe segments from original 9,882 |
| Pipeline construction | Union-find with endpoint snapping (SNAP_TOL_PX), split at junctions, gap bridging (GAP_BRIDGE_PX=100) | 111 pipeline entities |
| Junction detection | Endpoint-on-interior snapping, degree ≥ 3 | 145 junctions |
| Graph construction | Nodes: symbols + pipelines + junctions. Edges: CONNECTED_TO, ADJACENT_TO, CONTAINED_WITHIN, MONITORS, MOUNTED_ON | 663 edges |

**Key constants (step5b_geometric_association.py):**
```python
PIPE_BORDER_MARGIN_PX = 200
PIPE_MIN_LENGTH_PX    = 250   # raised from 150 to remove text separators
PIPE_MAX_FRACTION     = 0.75
PIPE_TITLE_BLOCK_FRAC = 0.80
PIPE_REF_PANEL_X_FRAC = 0.88
TABLE_GRID_Y_BAND_PX  = 15
TABLE_GRID_MIN_PARALLEL = 4
```

**Key constants (step5b2_hierarchy.py):**
```python
SNAP_TOL_PX            = 25
GAP_BRIDGE_PX          = 100   # bridges instrument-symbol gaps in pipe runs
SYMBOL_PIPE_RADIUS     = 60
EQUIP_PIPE_RADIUS      = 90
MOUNTED_ON_RADIUS      = 200
MIN_PIPE_LEN           = 60
DUP_MAX_DIST_PX        = 1500  # same tag >1500px apart = kept separate
MIN_SYMBOL_HEIGHT_PX   = 30    # candidates smaller than this = filtered
MIN_SYMBOL_WIDTH_PX    = 30
EQUIPMENT_MAX_ASPECT   = 3.5   # flat bbox = text label, not real symbol
```

---

### Entity Resolution (runs before graph construction)

Fixes the problem of the same tag being detected multiple times.

**Three-stage filter:**

1. **Symbol size filter** — candidates smaller than 30×30px image space
   are removed. These are text mentions in notes, not real symbols.

2. **Aspect ratio filter** (equipment only) — equipment candidates with
   bbox aspect ratio > 3.5 (width/height) are removed as text labels.
   - Example removed: K-V-201 at (3000,186), 323×72px, aspect=4.49
     This was the equipment title block, not the compressor symbol.
   - Example kept: K-V-201 at (5886,906), 678×542px, aspect=1.25

3. **Label-only recovery** — if an equipment tag was filtered by aspect
   ratio AND no real symbol exists for it, it is kept as `is_label_only=True`
   with confidence capped at 0.4. This handles equipment like KM-V-201
   (motor) which is labeled as text beneath the MOTOR box but has no
   separate symbol box on this drawing type.

4. **Deduplication** — candidates with the same tag_text and centroid
   within DUP_MAX_DIST_PX=1500px are merged into one canonical node.
   Candidates further apart are kept separate (e.g. K-V-201 appears
   in both the title block AND the main drawing — these are different
   things, handled by the aspect ratio filter above).

**After entity resolution on this drawing (final run):**
- 299 raw candidates → 282 after size/aspect filter → 226 after dedup
- 34 duplicate merges + 24 missing-prefix merges (e.g. ESDV-209 →
  V-ESDV-209) = 58 total merges performed
- 17 candidates filtered as non-symbols (title block text, OCR noise,
  tiny bboxes), 5 kept as label-only equipment (see point 3 above)

---

### Track A — Equipment binding

**Three binding mechanisms, in confidence order:**

1. **MOUNTED_ON edges (conf 0.674+)**
   Instruments within MOUNTED_ON_RADIUS=200px of equipment bbox get a
   direct edge. These are physically mounted instruments.

2. **Pipeline traversal (conf 0.5)**
   Instruments whose parent_chain passes through a pipeline that connects
   to equipment get equipment_parent via traversal. Lower confidence
   because the path goes through virtual nodes.

3. **Gemini attachment (conf 0.65–0.85)**
   For instruments with no equipment parent after CV methods, Gemini
   is shown a crop of the drawing region + the full equipment roster
   and asked to identify the parent equipment. See Gemini layer below.

**Result: 142/189 (75%) instruments have equipment_parent set.**

---

### Step 5B3 — Gemini Pipeline Connectivity Agent

A dedicated repair stage between CV pipe detection and hierarchy
construction. It does NOT replace the pipe detector — it verifies and
repairs the graph the detector produces, using small, targeted Gemini
calls rather than asking Gemini to interpret the whole drawing.

**Three deterministic tasks, each scoped to a local crop:**

| Task | Question asked | What it fixes |
|------|----------------|----------------|
| A — Gap bridging | "Are these two pipe segments the same physical pipeline, interrupted by a symbol or text?" | Reconnects pipes broken by valves, OCR text, revision clouds |
| C — Tee vs crossing | "Do these pipes physically connect here, or cross without joining?" | Removes false junction edges where pipes visually overlap but don't connect |
| D — Pipeline tracing | "Starting from this pipeline, does it continue? Does it pass through valves? Where does it terminate?" | Merges multi-segment pipelines, identifies equipment termination points |

**Every decision is auditable** — stored with the crop image, Gemini's
exact reasoning, and a confidence score, so any relationship can be traced
back to "why did the system think this."

**Result on the validated drawing (full run, all candidates processed):**
- 107 raw CV pipelines → 81 verified pipelines (26 merged/corrected)
- 15 gap bridges accepted (132 correctly rejected as unrelated)
- 4 false crossing junctions removed, 1 genuine tee confirmed
- 37 pipeline traces completed, 11 additional merges
- Validation HIGH issues dropped 87 → 55 as a direct result

**Cost:** ~$0.92 for a full run (187 Gemini calls) on this drawing size.
**Honest finding:** step5b3 makes the pipe graph more *correct*, not more
*complete*. It cannot create a pipe connection that was never drawn or
never detected by CV in the first place — see Gap 5 and Future Plan below.

---

### Track B — Flow direction

**Four evidence sources, applied in order:**

1. Arrowhead detection (RETR_LIST contour, solidity>0.85, 200-2000px²)
2. Check-valve glyph detection (triangle + seat-bar within 5-15px)
3. Equipment conventions (compressor suction=in, discharge=out)
4. BFS propagation through junction graph

**Result: 61/111 pipelines directed (55%)**

**Ceiling:** This drawing uses very few explicit flow arrows. 50 pipelines
remain UNKNOWN — this is a property of the drawing, not a code gap.
The Gemini pipe verification layer (Phase 2, not yet built) would improve
this but is deferred.

---

### Track C — Control loops (signal lines)

Detects dashed signal lines between instrument bubbles using path probing.

**Method:** For each pair of instrument candidates within
SIGNAL_MAX_PAIR_DIST=850px, probe the orthogonal path between them for
dashed ink patterns. Accept as signal edge only if all path segments
follow a line.

**Result:**
- 101 signal edges added to graph (category="signal", rel="SIGNAL_TO")
- 7 valid control loops (size ≤ 15 members) — CL-03=FIC-207 anti-surge,
  CL-00=TIC-213 temperature, CL-01=FY-208 cluster, etc.
- 2 over-merged clusters quarantined as unresolved_signal_cluster
- is_isolated fixed to mean "zero edges of ANY kind" (process + signal)

---

### Gemini Instrument Attachment Layer

For instruments that CV methods (MOUNTED_ON proximity, pipeline
traversal) could not connect to equipment.

**How it works:**
1. Group unresolved instruments into spatial clusters (GEMINI_ATTACH_CLUSTER_PX=800)
2. For each cluster: crop the drawing region (GEMINI_ATTACH_CROP_PAD_PX=500px)
3. Annotate crop: red circles on target instruments, blue boxes on equipment
4. Inject global equipment roster as text (8 equipment tags + positions)
5. Gemini reasons about connections even when equipment is off-crop
6. Add GEMINI_ATTACHED edge for high/medium confidence answers

**Cache:** tag-based key (sorted instrument tags per cluster) — stable
across runs with the same candidate set, even when the pipe graph
upstream changes. Cache file: gemini_attach_cache.json

**Performance:** 66 clusters, 8 parallel workers → ~3 minutes
(mostly cache hits on repeat runs; first run on a new candidate set
takes the full time)

**Result (final run, on the step5b3-verified graph):**
equipment_parent 31/189 (CV only, post step5b3) → 142/189 (with Gemini
attach), +90 new GEMINI_ATTACHED edges from 135 attachments returned.

---

## Current metrics — final validated baseline

| Metric | Value | Notes |
|--------|-------|-------|
| Total hierarchy nodes | 226 | After full entity resolution |
| Equipment parent coverage | 142/189 (75%) | CV + step5b3 + Gemini attach combined |
| Pipeline entities | 81 | Down from 107 raw CV — Gemini-verified |
| Orphan nodes | 50 | Nodes with no parent connection at all |
| NO PARENT instruments | 47 | Have edges but no equipment_parent |
| Isolated candidates | 3 | Zero connections of any kind |
| Hierarchy depth | 6 | |
| Control loops | 4 valid, 3 unresolved clusters | |
| Directed pipelines | 26/81 (32%) | arrowhead + check-valve + convention + propagation |
| Validation HIGH issues | 55 | Down from 87 before step5b3 |
| GEMINI_ATTACHED edges | 90 | 66 clusters, mostly cache hits |
| step5b3 gap bridges | 15 of 147 | Conservative — high precision |
| step5b3 crossings removed | 4 | False junction connections eliminated |
| step5b3 pipeline traces | 37 of 81 | 11 resulted in merges |
| Entity merges | 34 dedup + 24 prefix + 17 filtered | |
| Run time (full pipeline) | ~8 minutes | 8 workers, step5b3 + step5b2 combined |
| Total Gemini cost (one drawing) | ~$1.50–2.00 | step5b3 (~$0.92) + attach (~$0.40–0.60) |

### Equipment subtrees (final, after step5b3 + Gemini attach)

| Equipment | Type | Notes |
|-----------|------|-------|
| K-V-201 | Real symbol | Main ethane compressor — children count varies slightly run-to-run within Gemini's medium-confidence band; all children pass the ISA tag validity gate |
| V-V-201 | Label only | Suction K.O. drum — largest subtree, absorbs most purge/alarm instruments via Gemini reasoning |
| E-V-201 | Label only | Discharge gas cooler |
| KM-V-201 | Label only | Motor — labeled as text beneath the MOTOR box, no separate symbol box on this drawing |
| S-V-204 | Real symbol | Suction strainer — clean subtree, no label_only flag, no duplicate issues |
| KG-V-201 | Real symbol | Gearbox |
| 01/E-V-201A | Label only | No children resolved yet |
| V-V-03 | Label only | No children resolved yet |

**Note on "label only" equipment:** 5 of 8 equipment nodes have no
detectable symbol box on this drawing — only a text label. This is a
property of how this specific drawing was authored (motor and some
vessels are labeled by text reference rather than a drawn symbol box),
not a detection failure. These nodes are kept in the hierarchy with
confidence capped at 0.4 to make this distinction visible downstream.

---

## Deliverables (step9 output)

### final_hierarchy.xlsx

| Sheet | Content |
|-------|---------|
| Asset Hierarchy | All instrument/valve/equipment nodes with plant/area/system/parent columns — renamed from "Equipment Hierarchy" since it covers all tagged assets, not equipment only |
| Piping Lines | Pipe specification line numbers (e.g. 2IN-ETH-V057-61440X), separated out so they don't pollute the asset view |
| Parent Child Relationships | 427 direct relationships (part_of, controls, monitors) |
| Functional Location | Derived MGDV.6-50.{tag} codes |
| Cross Drawing References | Empty — single drawing |
| Orphan Nodes | 50 nodes with no parent connection, deduplicated by tag_text (keeps highest-confidence instance) |
| Hierarchy Statistics | Key metrics summary |

### hierarchy_viewer.html
Interactive tree with search. Expand/collapse, click for details.
Works offline (no CDN dependencies). Search by tag to see full
parent chain, siblings, and connected relationships.

### hierarchy_graph.html
Force-directed network graph. Blue=equipment, green=instrument,
orange=control device, purple=virtual, red=orphan. Drag, zoom, click.

### hierarchy_verification.jpg (visualize_hierarchy.py)
Standalone annotated overlay on the original drawing — not a step9
output, generated separately, but the most direct way to visually
audit the hierarchy. Color-codes every parent-child edge by evidence
type (green=physical proximity, blue=pipeline traversal, orange=Gemini
reasoning) and flags instruments with no equipment parent in magenta.
Can be regenerated at any time from the current hierarchy JSON with
one command — see "How to run", Step 5.

### hierarchy_validation_report.xlsx
162 issues: 55 HIGH, 58 MEDIUM, 49 LOW (down from 87/50/40 before the
step5b3 pipeline repair layer was added). Most remaining HIGH issues
are multiple-parent conflicts from pipeline_traversal evidence, which
is expected given the CV pipe detection ceiling documented in Gap 5.

---

## Known gaps — documented, not silent failures

### Gap 1: Pipe spec labels as children (HIGH priority to fix)
Tags like 12IN-ETH-V012-61440X-PP, SB 6IN, 7-61440X, "213", "212"
appear as children of K-V-201 via pipeline_traversal evidence.
These are pipe specification labels detected by step5a OCR, not real
instrument children.
**Fix:** Tag validity gate in hierarchy traversal — only assign
equipment_parent via pipeline_traversal if tag matches ISA pattern
(letter prefix + hyphen + alphanumeric).
**Status:** FIXED — tag validity gate active (`is_valid_instrument_tag()`
gates the pipeline_traversal branches in `_resolve_equipment_parent`).

### Gap 2: TAG_NUMBER = None in step7 (HIGH priority)
step7_cedm_normalizer.py sets TAG_NUMBER to None for some records.
This is a field mapping issue — step7 is reading the wrong field name.
**Fix:** Find the field name mismatch, correct the mapping.
**Status:** CONFIRMED NOT A BUG — already correct. step7 reads `tag_text`
(the actual candidate field) → `TAG NUMBER`; 0 None/empty across 246 rows
in final_tags.xlsx. The lone "UNKNOWN" is a genuinely unreadable OCR tag.

### Gap 3: Piping in Asset Hierarchy sheet (MEDIUM)
Pipe line numbers (2IN-ETH-V057-61440X etc.) appear in the Asset
Hierarchy sheet because step9 doesn't filter by kind=='equipment'.
**Fix:** Add is_equipment_row() filter in step9. Add separate
"Piping Lines" sheet.
**Status:** FIXED — P1a/P1b (`is_piping_node()` excludes piping from the
Asset Hierarchy sheet; 29 line numbers moved to the new "Piping Lines" sheet).

### Gap 4: V-ESDV-209 duplicate in orphans (LOW)
Appears twice — once as instrument, once as valve (different
symbol_category so dedup kept both).
**Fix:** step9 orphan dedup by tag_text, keep highest confidence.
**Status:** FIXED — P1c (orphan sheet dedups by tag_text, keeps the
highest-confidence instance).

### Gap 5: CV pipe detection ceiling (ACCEPTED — addressed in Future Plan)
Morphological detection plus Hough transform captures roughly 30% of pipe
runs on the validated drawing. The other ~70% are missed because pipes
cross symbols, bend, merge with text, or are drawn faintly. The Gemini
attachment layer and step5b3 pipeline-repair layer substantially
compensate for this (75% equipment-parent coverage achieved despite the
detection gap), but cannot fully close it — Gemini can repair and verify
connections between detected segments, it cannot detect a pipe that the
CV stage never found in the first place.
**Status:** ACCEPTED as the current ceiling. See **Future Plan → Phase B**
for the proposed LSD/skeletonization upgrade to raise raw detection
recall, which is the only way to close this specific gap further.

**Root cause refined (2026-06-30 — re-scopes future detector work):** The
Phase B LSD/skeleton experiment (now TESTED, NOT ADOPTED) established that
the remaining hierarchy gaps are **not** caused by weak solid-pipe detection
in general. The instruments that remain NO-PARENT are connected by **dashed
instrument signal lines**, and at the production min-length threshold the
individual dashes fall far below the floor — so neither the production Hough
detector nor the LSD/skeleton candidates pick them up. Adding more
solid-line detection power (LSD, skeleton, connected components) does not
touch these connections. This narrows the future detector problem from the
broad "detect more pipe runs" to the much more specific and more solvable
**"detect dashed signal lines"** (e.g. dash-gap-aware linking, a dedicated
dashed-line tracer, or signal-line-specific Gemini probing). Future detector
effort should target dashed-line detection, not general line recall.

### Gap 6: Mechanical shaft K-V-201 → GEAR → KM-V-201 (DEFERRED)
Shaft lines connecting the compressor train are not detectable as
process pipe connections. Nodes flagged with unresolved_connection=true
and connection_hint="mechanical_shaft".
**Status:** DEFERRED

### Gap 7: FIC-207 multi-bend transmitter (DEFERRED)
The flow controller's transmitter inputs route via multi-bend dashed
paths that 2-segment path probing cannot follow.
**Status:** DEFERRED

### Gap 8: Plant/Area/System codes synthetic (PENDING EXTERNAL DATA)
MGDV / 6-50 / UNASSIGNED derived from drawing number.
Authoritative codes require SAP or CMMS master data — not available.
This is NOT a code issue. It is pending external reference data.
**Status:** WAITING ON CLIENT DATA

---

## Next actions (prioritised)

| Priority | Action | Effort | Impact | Status |
|----------|--------|--------|--------|--------|
| P1 | Fix step9: filter piping from Asset Hierarchy, add Piping Lines sheet, dedup orphans | 30 min | Clean deliverable | **COMPLETE** |
| P2 | Fix TAG_NUMBER=None in step7 field mapping | 15 min | Correct Excel output | **COMPLETE** (not a bug — already correct) |
| P3 | Tag validity gate in hierarchy traversal (removes pipe spec children) | 45 min | Cleaner K-V-201 subtree | **COMPLETE** |
| P3b | Gemini Pipeline Connectivity Agent (step5b3) — gap bridging, tee/crossing, pipeline tracing | ~1 day | Pipelines 107→81, validation HIGH 87→55 | **COMPLETE** |
| P4 | Validate on a second drawing | 2-3 hrs | Production confidence | **NOT YET DONE** — `input2.png` was tested but is a different drawing type with no equipment symbols, so it does not count as a valid second-drawing test. A real second client P&ID is needed. |
| P5 | Future Plan Phase A — get a second real client P&ID drawing | Depends on client | Unblocks everything below | **BLOCKED — awaiting client data** |
| P6 | Future Plan Phase B — LSD/skeletonization comparison | 1-2 days | Higher pipe detection recall | **TESTED, NOT ADOPTED** — neither recovers dashed signal lines (the actual gap); see Gap 5 + Phase B |
| P7 | Future Plan Phase C — Gemini ensemble verification | TBD after Phase B | Higher step5b3 decision precision | Proposed, not started |
| P8 | Begin MBOM generation from hierarchy | New work | Next deliverable | Proposed, not started |

---

## Two-track extraction architecture

The hierarchy always uses FULL DRAWING extraction, not cloud-filtered.

```
step5a FULL_DRAWING
      │
      ├── step5a_candidates_full.json   ← HIERARCHY uses this
      │
      └── cloud_filter.py
              │
              └── step5a_candidates_cloud.json  ← TAG DELIVERABLE uses this

step5b (on full) → step5b_associations_full.json   ← HIERARCHY
step5b (on cloud) → step5b_associations_cloud.json ← TAG DELIVERABLE

step5b2 → step5b2_hierarchy_full.json              ← always full
step7/8 → consume cloud candidates + full hierarchy enrichment
```

Reason: parent equipment may be outside the revision cloud while
its child instrument is inside. Hierarchy needs complete context.

---

## Future Plan

This section is written for stakeholder review. Nothing below has been
implemented or run — these are proposed next phases with honest scoping
on effort, cost, and expected impact. No accuracy numbers below are
guarantees; they are targets to be validated once the work is done.

### Why a future plan is needed now

The system has reached a measurement ceiling on the one drawing it has
been validated against: 75% equipment-parent coverage, with the remaining
gap traced to a specific, well-understood root cause — see Gap 5 above.
Closing that gap further requires either (a) a stronger pipe detector or
(b) more Gemini reasoning per drawing region, or both. Before committing
further engineering time to either, the single most valuable next step is
**validating the current system on a second real client P&ID drawing**,
because the only drawing tested so far may not represent the diversity of
drawing styles, line quality, and equipment conventions in the actual
client dataset.

### Phase A (next, blocking) — Second drawing validation

Run the full pipeline end-to-end on a real client P&ID different from the
one already validated. This requires the client to share at least one
additional drawing with known/expected tags so results can be checked
against ground truth. Without this, further accuracy investment is
guesswork — we would be tuning against a sample of one.
**Status: blocked on data access from the client.**

### Phase B — Stronger pipe detection (classical CV upgrade) — TESTED, NOT ADOPTED

> **Status: TESTED, NOT ADOPTED (2026-06-30).** The LSD and skeletonization
> comparison described below was run as an A/B measurement experiment against
> the production Hough detector on the validated drawing. Both candidate
> detectors were added to `step5b_geometric_association.py` as standalone
> functions (`detect_lines_lsd`, `detect_lines_skeleton`) for the experiment
> only — **neither is wired into the pipeline.** Findings:
>
> - **LSD — modest genuine recovery, not worth integrating.** After the same
>   min-length filter, LSD produced 450 segments vs the Hough baseline of 224,
>   with 347 flagged "new". However the gain is heavily inflated by
>   **edge-doubling** — LSD detects each side of a thick pipe line as a
>   separate parallel segment, so one physical pipe becomes two segments — and
>   it suffered a **near-total loss of diagonal pipes (45 → 2)**, which Hough's
>   Canny+Hough diagonal pass already captures. Net real recovery after
>   discounting these artifacts is small.
> - **Skeleton+Hough — best raw centerline completeness, currently unusable.**
>   It traced solid-pipe centerlines more completely (973 segments, 893 "new"),
>   but the experimental function applies **only** the min-length filter — it
>   lacks the zone/border/title-block/reference-panel/table-grid rejection that
>   the production Hough detector already has. As a result the overwhelming
>   majority of its "new" segments are **notes-block text strokes and
>   reference-panel/title-block table-grid lines, not real pipes.** It would
>   need all of those filters ported before its count means anything.
> - **Neither detector reaches the actual target.** The remaining NO-PARENT
>   instruments are connected by **dashed instrument signal lines**, not solid
>   pipes. At the production min-length threshold the dashes are individually
>   far below the floor and are dropped by *both* candidate detectors. The
>   thing Phase B was meant to recover is precisely the thing neither method
>   recovers — see the re-scoping note in **Gap 5**.
>
> Conclusion: detecting *more solid lines* is not the lever. Do not
> re-investigate LSD/skeleton as a general "detect more pipes" upgrade without
> first reading the Gap 5 re-scoping below.

The current pipe detector (step5b_geometric_association.py) uses
morphological filtering plus probabilistic Hough transform
(`cv2.HoughLinesP`). This is a standard, well-understood approach but it
is not the most capable option available in OpenCV for this class of
problem. The proposed upgrade adds three additional techniques, run
alongside the existing detector and compared before any replacement:

| Technique | What it does | Why it may help |
|-----------|---------------|------------------|
| Line Segment Detector (LSD) | `cv2.createLineSegmentDetector()` — sub-pixel line detection, less parameter-sensitive than Hough | May recover broken/faint pipe runs that Hough's threshold-then-detect approach misses |
| Skeletonization | Reduces each pipe to a 1-pixel-wide centerline before line detection | Produces cleaner, more reliable junction and endpoint detection than detecting on the raw thick pipe outline |
| Connected Components | Groups pipe pixels into regions before line-fitting | More stable than contour-based detection for pipes that bend or branch |

**This phase is scoped as a measurement experiment first**, not a
pipeline replacement: detect with all methods, compare segment counts and
overlap against the existing baseline, and only integrate if the new
methods demonstrably recover real pipe runs the current system misses.
**Estimated effort:** 1–2 days for the comparison, additional time to
integrate if it proves out. **Target outcome:** higher pipe-segment recall
feeding into step5b3 and the hierarchy, pending validation — no number is
committed until the comparison is run.

### Phase C — Gemini ensemble verification (multi-pass consensus)

For the highest-ambiguity decisions in step5b3 (gap bridging, tee vs
crossing, pipeline tracing), the system currently asks Gemini once per
decision. The proposed extension runs each ambiguous decision through
Gemini multiple times (or with multiple prompt framings) and takes a
majority/consensus answer rather than a single response. This is a
standard self-consistency technique for raising reliability on judgment
calls where a single model pass can be uncertain.
**Target outcome:** improved precision on the step5b3 repair decisions —
aiming for 95%+ accuracy on pipe-connectivity verification specifically
(not overall equipment-parent coverage, which depends on additional
factors). **This is a target range to validate, not a guaranteed result.**
**Estimated cost impact:** roughly 2–3× the current step5b3 Gemini cost
(~$0.92 → ~$2–3 per drawing) since each decision requires multiple calls.

### Recommended sequencing

```
Phase A (second drawing validation)
        │
        ▼  confirms whether Phase B/C are worth pursuing,
           and on which failure modes to focus
        │
Phase B (LSD + skeletonization comparison)
        │
        ▼  only integrate if comparison shows real recall gain
        │
Phase C (Gemini ensemble — applied on top of the improved
         pipe graph from Phase B, not as a substitute for it)
```

Phase A is the most important step in this list and the only one that is
not gated on engineering time — it is gated on getting a second real
client drawing. Phases B and C are real, scoped engineering proposals that
will be executed and reported on with the same level of honesty as
everything documented above — including reporting if a phase does not
move the numbers, as happened with parts of the step5b3 rollout (see
"Honest finding" under Step 5B3 above).

---

## Architectural direction (longer-term, not scheduled)

### Track D — Cross-document relationships
Hierarchy spanning multiple drawings: P&ID → Equipment Layout →
GA Drawing → Vendor Package → SPIR → Register.
**Status:** Designed, not started. Requires drawings from other document
types, which depends on the same data-access question as Phase A above.

### Knowledge Graph as canonical model
Current hierarchy is a derived structure. Future: hierarchy, BOM,
Material Master, and GraphRAG all as views of one canonical graph.
**Status:** Architectural direction documented, not implemented.