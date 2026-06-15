# CDCI — Revision Cloud Detection & P&ID Tag Extraction

**Project:** CDCI Engineering Document Digitization
**Phase:** Revision-cloud boundary detection (precursor to tag / hierarchy / BOM extraction)
**Reference drawing:** `4224-MGDV-6-50-2004-001-C` — Ethane Gas Compressor (K-V-201), Mesaieed Gas Distribution Stations
**Status:** Phase 1 (cloud detection) **not yet solved** — see the technical section. Investigation has converged on the correct architecture; the current blocker is upstream signal loss, not reconstruction.

This document has two halves:

1. **Part A — Investigation Narrative** (for management / board): what was attempted, why each approach was eliminated, what was learned, and where the project stands.
2. **Part B — Technical Reality, Runbook & Forward Plan** (for engineering): the blunt status, every script and command tried, the parameters to tune, and the recommended fix.

---
---

# PART A — Engineering Investigation Narrative

## Objective

Accurately detect the **outermost revision-cloud boundary** in P&ID drawings.

This is a critical prerequisite because **all downstream extraction depends on identifying the correct revision scope.** If the cloud boundary is wrong, then tag extraction, hierarchy extraction, and BOM (MBOM) generation are all wrong. Errors here propagate to every later stage.

**Business requirement:**

- Detect the outermost revision cloud.
- Ignore inner (nested) clouds.
- Ignore drawing content (pipes, symbols, text, title block, borders).
- Generate a valid revision scope (polygon / mask).
- Extract only equipment and tags **within** the approved scope.

A revision cloud is a **closed shape whose border is a chain of small convex bumps (scallops)** — not a straight-edged polygon, not a smooth ellipse. On real drawings these clouds are distorted, stretched, broken, partially hidden behind pipes, and have uneven scallop spacing. The defining, invariant property is the **periodic alternation of curvature along the boundary** — the only structure on a P&ID with that signature.

---

## Approach 1 — Vision LLM Detection (Gemini / Claude)

**Hypothesis.** A large vision-language model can visually identify revision clouds directly from the drawing.

**Implementation.** Full P&ID drawings were sent to Gemini and Claude, requesting outer-cloud detection, bounding boxes, and inside/outside element lists.

**Results.** The models *recognized* clouds in some drawings, but:

- Results were inconsistent run-to-run.
- Bounding boxes were imprecise.
- Nested clouds were not handled reliably.
- Large drawings exceeded reliable visual-reasoning resolution.
- Cloud boundaries were frequently confused with nearby pipelines and symbols.

**Root cause.** Revision-cloud detection is fundamentally a **geometric boundary-extraction** problem, not a **semantic image-understanding** problem. Vision LLMs reason about drawings at a high level; they are not designed for pixel-precise boundary reconstruction.

**Conclusion.** Rejected as the *primary* solution. **Retained as a locator / verification layer** — the models are good at *finding roughly where* clouds are, even when they cannot trace them precisely. This distinction becomes the basis of the recommended architecture (Part B).

---

## Approach 2 — Direct Image Segmentation

**Hypothesis.** Treat the cloud as a segmentation object and isolate it with a segmentation model / mask.

**Results.** Segmentation produced **large blobs**, not clean cloud boundaries. On dense drawings, pipelines and text merged into the cloud region, producing large false-positive areas. (Observed concretely: the mask output had one ~8.2-million-pixel blob covering ~26% of the sheet, with thousands of speckle fragments around it.)

**Root cause.** Revision clouds are **not filled objects** — they are thin scalloped *boundaries*. Segmentation prefers solid regions and therefore floods across unrelated adjacent content.

**Conclusion.** Rejected. Segmentation does not preserve cloud geometry.

---

## Approach 3 — Fragment Extraction Pipeline

**Hypothesis.** Don't detect the whole cloud at once; detect small arc fragments and reconstruct the cloud from them.

**Implementation.** Three phases: (1) edge / arc detection → fragment extraction; (2) fragment grouping; (3) cloud reconstruction.

**Results.** **First approach to capture real cloud structure.** But many valid fragments were rejected, fragments were disconnected, and reconstruction quality was inconsistent.

**Failure modes identified.**

1. **Signal loss** — weak arcs disappeared during preprocessing.
2. **Fragmentation failure** — one cloud became hundreds of disconnected arc pieces.
3. **Match failure** — valid neighbouring fragments were not connected.
4. **Valid-arc rejection** — genuine cloud arcs were discarded.
5. **Pipeline contamination** — some pipe segments resembled cloud arcs and were admitted.

**Conclusion.** Promising but incomplete.

---

## Approach 4 — Arc-Fragment Graph Architecture

**Hypothesis.** The cloud is not an image; it is a **graph**. Each arc fragment is a node; geometric compatibility between arcs forms edges.

**Implementation.** Edge compatibility scored on distance, curvature, orientation, and tangent direction.

**Results.** Major improvement — cloud structure emerged as a connected graph rather than isolated fragments, and many fragmentation issues were reduced.

**Remaining issues.** Missing links, incorrect links, ambiguity in complex regions, nested-cloud confusion.

**Status.** Most promising architecture to date.

---

## Key Discovery

> **Revision clouds are not objects. They are graphs of connected arc fragments.**

This reframing changed the architecture from `Cloud = Image Object` to `Cloud = Geometric Graph`, and is the foundation of the current CDCI design.

---

## What Was Completed

- Cloud problem decomposition
- Failure-mode analysis
- Arc-fragment extraction
- Fragment classification
- Root-cause identification
- Graph-based cloud representation
- Revision-scope architecture design

## What Is In Progress

1. Robust graph assembly
2. Fragment-matching optimization
3. Outer-cloud selection
4. Nested-cloud resolution
5. Polygon generation
6. Revision-scope generation
7. Integration with tag extraction

## Management-Level Conclusion

Revision-cloud detection cannot be reliably solved by Vision LLMs or by traditional segmentation alone. The correct solution is a **geometric reasoning pipeline**: arc extraction → fragment analysis → graph construction → cloud reconstruction → scope-polygon generation, with a vision model used only to *locate* candidate regions. This architecture is the foundation for CDCI revision-aware tag, hierarchy, and MBOM extraction.

---
---

# PART B — Technical Reality, Runbook & Forward Plan

This section is deliberately blunt. Read it before quoting Part A's status.

## B.1 — Honest status: Phase 1 is NOT solved

The board narrative implies:

```
Phase 1 (cloud detection)  →  works
        ↓
Reconstruction             →  failing
```

**The evidence says otherwise.** The real situation is:

```
Original cloud
   ↓ threshold        ← 40–60% of cloud arcs destroyed HERE
   ↓ skeleton
   ↓ fragment extraction
   ↓
Many cloud arcs were NEVER extracted
   ↓
Affinity grouping / graph traversal / convex hull / concave hull / SAM / alpha shape
   ↓
ALL fail — they cannot recover geometry that never existed
```

The recurring observation **"the cloud area itself is not appearing in the mask"** is the decisive clue. If the boundary is missing *before* reconstruction, no downstream algorithm — graph assembly, convex/concave hull, SAM, alpha shape — can invent it back. **The bottleneck is preprocessing signal loss and skeleton welding, not reconstruction.** Effort spent improving the graph/reconstruction stages cannot pay off until Phase 1 actually captures the arcs.

### Two concrete, measured root causes

1. **Global Otsu binarization destroys weak arcs.** A single threshold across a ~45–70 MP sheet drops faint or thin cloud arcs below threshold. The boundary is broken *before* any contour or fragment is traced — irrecoverable.
   - Measured: global Otsu inverse foreground ≈ 18% of the sheet, but the breakage falls disproportionately on thin scalloped arcs.

2. **The skeleton is fully welded.** On `skeleton.png` (8000×5650, later runs on the 9934×7017 source), `findContours(RETR_EXTERNAL)` returns **one** external contour for the whole sheet because clouds are connected to pipes and text at thousands of junctions. Result: a purely geometric locator finds **0** isolated clouds.
   - Measured: current mask = **4135 connected components**, dominated by a single **8.2 M-pixel** blob (~26% of sheet); next largest only 297 K. That is simultaneous over-merge (one giant weld) and shatter (thousands of speckles).

3. **OR'd acceptance admits non-clouds.** Accepting a contour when `scallopedness > 1.70` **OR** `CLS ≥ 0.55`, where `CLS` rewards concavity (`1 − solidity`), lets jagged text, hatched triangles, and symbol clusters pass without any *periodic* scallop signal. Pipes/text/borders survive.

4. **Dilation before masking welds neighbours.** Dilating polygons before rasterizing fuses adjacent clouds and bridges a cloud to a touching pipe across the thin inter-cloud gaps.

### What "periodicity" buys us

A cloud boundary, walked end-to-end, shows a **sustained periodic alternation of curvature cusps**. Pipes are flat; text is random; triangles/rectangles/diamonds are straight; smooth shapes don't oscillate. Making **ring periodicity a mandatory AND gate** (not one OR'd factor among five) rejects almost every false positive intrinsically — and was verified to fire at `periodicity = 1.0` exactly in the cloud band (x ≈ 3200–4500) of the real skeleton, while staying silent on pipe/text regions.

---

## B.2 — Scripts in the pipeline

Existing pipeline (the graph/reconstruction line of work):

| File | Role |
|---|---|
| `stage1_cloud.py` | Multi-stage OpenCV + Gemini detector with a scalloped-edge validation gate. Source of the current (over-merged) mask. |
| `fragment_extractor.py` | Skeletonizes line work, cuts at junctions into arc fragments. Holds the `_signed_curvature` / periodicity helpers. |
| `affinity_grouper.py` | Groups fragments by tangent / distance affinity. (Math is fine; it is no longer the acceptance gate.) |
| `cloud_reconstructor.py` | Bridges fragment clusters into closed rings via endpoint proximity (no affinity filter → transitive welding). |
| `diagnose_clouds.py` | Failure-mode diagnostic; accepts a ground-truth file `[{"box":[x0,y0,x1,y1]}]` via `--truth` and buckets misses A/B/C/D. |

New detection-first scripts (built during this investigation):

| File | Role |
|---|---|
| `detect_outer_clouds.py` | Self-contained, **deterministic** detector. Local adaptive binarize → skeleton fragments → **periodicity-gated ring tracer** → nesting filter. Outputs binary mask + per-cloud tiles + debug overlay + JSON. |
| `mark_outer_clouds.py` | **Locate-then-snap** detector. Gemini (or offline fallback) **locates** rough cloud boxes → deterministic **periodicity-gated border snap** inside each box → outer/inner tagging → **overlay image** + JSON. This is the recommended runtime path. |

Key inputs:

| File | Meaning |
|---|---|
| `input_drawing_clouds.jpg` / `drawing.jpg` | Source P&ID (colour/greyscale) — used for overlay + snapping. |
| `skeleton.png` | 1-px skeleton of all line work (anti-aliased, values 0–4; `>0` binarizes). Welded, hence the locate step is needed. |
| `02_scope_mask.png` | Current (wrong) mask: over-merged blob + speckle. |
| `03_scope_tinted.jpg` | Current tinted overlay. |

---

## B.3 — Runbook: every approach and how to run it

### Environment

> **Use Python ≥ 3.10.** The runs were done on `pyenv` Python 3.8.18, which now triggers end-of-life `FutureWarning`s from `google-*` and will eventually break.

```bash
# Recommended: fresh env on 3.10+
python -m venv .venv && source .venv/bin/activate   # or conda create -n cdci python=3.11
pip install --upgrade pip
pip install opencv-python-headless numpy scipy scikit-image
# Gemini locator — NOTE: use the CURRENT SDK, not the legacy one:
pip install google-genai          # NEW SDK: `from google import genai`
# (legacy `google-generativeai` is auto-detected as a fallback if present)
```

### Approach 4 line — deterministic detector (no API key)

```bash
python detect_outer_clouds.py input_drawing_clouds.jpg --out results --debug
```

Outputs in `results/`: `mask_outer_clouds.png`, `cloud_01.png …` (outer tiles),
`inner_cloud_01.png …` (nested, tagged), `debug_overlay.jpg` (red = outer, blue = inner), `clouds.json`.

### Recommended line — locate-then-snap with Gemini

```bash
export GEMINI_API_KEY=your_key            # or pass --api-key
python mark_outer_clouds.py drawing.jpg \
       --skeleton skeleton.png \
       --gemini
```

Output: `outer_cloud_overlay/overlay_outer_clouds.jpg` (outer borders red, inner blue) + `outer_clouds.json`.

Offline (no key) — runs but **unreliable on this welded drawing** (see B.1; it over-detects pipes/equipment/text):

```bash
python mark_outer_clouds.py drawing.jpg --skeleton skeleton.png
```

Verbose / different model:

```bash
python mark_outer_clouds.py drawing.jpg --skeleton skeleton.png --gemini -v
python mark_outer_clouds.py drawing.jpg --gemini --model gemini-2.5-pro
```

### Diagnostic with ground truth (fastest way to converge)

Create `truth.json` with a dozen clouds you can see, then:

```bash
# format: [{"box":[x0,y0,x1,y1]}, ...]
python diagnose_clouds.py --image drawing.jpg --truth truth.json
```

It reports which clouds are missed and which failure bucket (A signal-loss / B fragmentation / C match / D contamination) each falls in — so tuning is targeted, not guesswork.

### Known errors already hit (and fixes)

- `module 'google.generativeai' has no attribute 'GenerativeModel'`
  → SDK mismatch. The environment has the **new** `google-genai`; the legacy API call fails. Fixed by calling `genai.Client()` / `types.Part.from_bytes` first, legacy second. `pip install google-genai`.
- `0 boxes` from the offline fallback on `skeleton.png`
  → welded skeleton → one external contour. Expected. Use `--gemini`; the offline sliding-window fallback is a last resort and over-detects here.

---

## B.4 — Parameters to tune

### `detect_outer_clouds.py`

| Param | Default | Effect |
|---|---|---|
| `ADAPTIVE_BLOCK` | 51 | Local-threshold window (odd px). Larger = smoother binarization. |
| `ADAPTIVE_C` | 10 | Higher = less ink kept. |
| `BRIDGE_KERNEL` | 3 | Closes cusp-scale gaps only — keep small so it doesn't bridge pipes. |
| `JUNCTION_DILATE` | 2 | Raise to sever thick pipe/cloud crossings at high resolution. |
| `PERIODICITY_MIN` | 0.45 | Per-fragment seed threshold. ↑ precision, ↓ recall. |
| `RING_PERIODICITY_MIN` | 0.45 | **Mandatory AND gate** on whole ring. The main precision knob. |
| `MIN_SCALLOP_CUSPS` | 6 | Min curvature sign-changes for a valid ring. |
| `TRACE_RADIUS` | 120 | Max endpoint gap to continue a ring across a break. ↑ to join split clouds. |
| `RING_CLOSE_DIST` | 140 | Ring closes within this of the start. |
| `CLOUD_DILATE` | **0** | Keep at 0 — dilation welds neighbours. |
| `NEST_CONTAIN_FRAC` | 0.80 | Inner ring ≥ this fraction inside another → tagged inner. |

### `mark_outer_clouds.py`

| Param | Default | Effect |
|---|---|---|
| `PERIODICITY_MIN` | 0.40 | Ring acceptance at the snap step. ↓ if real clouds get dropped. |
| `MIN_CUSPS` | 6 | Min curvature sign-changes. |
| `SNAP_PAD` | 40 | Padding around a located box before snapping. ↓ if a box swallows two clouds. |
| `NEST_CONTAIN_FRAC` | 0.80 | Inner-cloud tagging threshold. |
| Gemini downscale cap | `3072` (in `locate_gemini`) | **↑ for the 9934×7017 source** — small scallops vanish at 3072 px and Gemini then misses faint clouds. |
| `FB_WIN_FRAC` / `FB_STEP_FRAC` | 0.13 / 0.45 | Offline sliding-window size / stride (fallback only). |

**High-precision bias (current default):** expect to **miss a few** faint/broken clouds before accepting any junk. To recover misses, lower `RING_PERIODICITY_MIN` / `PERIODICITY_MIN` (e.g. 0.45 → 0.35). To cut false positives, raise them. If two clouds fuse, raise `JUNCTION_DILATE`; if one cloud splits, raise `TRACE_RADIUS` + `RING_CLOSE_DIST`.

---

## B.5 — Recommended forward plan

The graph architecture (Approach 4) is correct and should remain the target. **But it is starved of input.** Fix Phase 1 first; the graph stage will then have arcs to work with.

### Step 1 — Fix Phase-1 signal loss (highest leverage; do this first)

1. **Replace global Otsu with local adaptive binarization** (CLAHE pre-step + adaptive threshold), tuned to keep thin/faint arcs. This is the single change that recovers the missing 40–60% of arcs. *Acceptance test:* re-run fragment extraction and confirm arc coverage over a hand-marked cloud rises from ~40–60% to >90%.
2. **Multi-scale binarization:** binarize at 2–3 scales and union, so both thin and bold arcs survive.
3. **Cusp-scale morphological bridge only** (3×3 close, 1 iter) — never a pipe-scale kernel.

### Step 2 — De-weld the skeleton at junctions

1. Raise `JUNCTION_DILATE` and add a **curvature-discontinuity cut**: sever a chain where curvature jumps from oscillating (cloud) to flat (pipe). This prevents the single-giant-contour problem and stops pipe/text from chaining through cloud clusters.
2. *Acceptance test:* `findContours(RETR_EXTERNAL)` on the de-welded skeleton yields many local contours, not one sheet-spanning blob.

### Step 3 — Make periodicity the only hard gate

1. Drop the `OR CLS≥0.55` path. Require ring **periodicity AND min-cusps** as mandatory; demote scallopedness/solidity to tie-breakers.
2. Demote the hardcoded title-block/notes/border exclusion rectangles to a cheap *backstop*, not the primary filter.

### Step 4 — Locate-then-snap as the runtime detector

1. **Gemini 2.5 Pro locates** rough cloud boxes (it is reliable at *finding*, unreliable at *tracing*). Send the image at high resolution (raise the 3072 cap).
2. **Deterministic periodicity-gated snap** inside each box recovers the exact outer ring. Running per-box (not whole-sheet) is what avoids the merge problem — few competing pipes/text inside one cloud's neighbourhood.
3. This is exactly `mark_outer_clouds.py`. Treat the graph reconstructor as the *inside-the-box* snapper rather than a whole-sheet assembler.

### Step 5 — Outer selection + nested resolution

1. Rasterize rings with **zero dilation**; run containment filtering.
2. Keep the **outermost** ring per nested group (tag inner clouds; don't fill them into the outer mask).

### Step 6 — Scope polygon → tag extraction integration

1. Emit the outer-cloud polygon(s) as the **revision scope**.
2. Point-in-polygon test every extracted tag / symbol / equipment centroid against the scope.
3. Keep only items **inside** an approved outer cloud → feeds revision-aware **tag extraction → hierarchy extraction → MBOM**.

### Step 7 — Verification layer (closing the loop with Approach 1)

1. Re-use the Vision LLM **only** to *verify* the final overlay ("is each marked region a revision cloud? any obvious cloud missed?"). This is where the rejected Approach 1 earns its place — verification, not detection.

### Suggested priority

```
P0  Step 1 (binarization signal loss)   ← nothing else matters until this is fixed
P0  Step 2 (de-weld skeleton)
P1  Step 3 (periodicity-only gate)
P1  Step 4 (locate-then-snap runtime)
P2  Step 5 (outer/nested)
P2  Step 6 (scope → tag extraction)
P3  Step 7 (LLM verification)
```

---

## B.6 — One-paragraph summary for the next engineer

Cloud detection is the gate for all CDCI tag/hierarchy/BOM work, and it is **not** solved. Despite a board narrative that says "Phase 1 works, reconstruction fails," the measured reality is that **40–60% of cloud arcs are destroyed at the binarization/skeleton stage**, so every downstream step (affinity grouping, graph traversal, convex/concave hull, SAM, alpha shape) is trying to recover geometry that was never captured. The correct architecture is settled — clouds are **geometric graphs of periodic arc fragments**, detected via **locate (Gemini) → periodicity-gated snap (deterministic) → outer/nested filtering → scope polygon** — and two working scripts (`detect_outer_clouds.py`, `mark_outer_clouds.py`) implement it. **Do not tune the graph stage next. Fix Phase-1 binarization and skeleton de-welding first** (Steps 1–2); the periodicity gate (verified to fire at 1.0 in the real cloud band) and the locate-then-snap runtime will then produce a reliable outer-cloud scope.
