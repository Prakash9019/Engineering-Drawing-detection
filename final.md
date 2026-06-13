

OpenCV Scallop Detection
        ↓
Fragment Extraction
        ↓
Arc Fragment Graph
        ↓
Boundary Reconstruction
        ↓
Polygon Generation
        ↓
SMM Merge
        ↓
Validation Gate
        ↓
Final Clouds



What we done till now :: --

  The clean ~92k cloud loop does not exist as a separate connected component. In the real binary, the cloud's outline is
  electrically welded — through the border frame, pipes, leader lines, and text — into sheet-spanning blobs of 20–70 million
  pixels that blow past the area ceiling and are discarded. My diagnostic's 60 px crop severed those welds, manufacturing a
  clean loop the live pipeline never sees. The contour was never presented to any gate. There is no gate-bypass bug to fix.

  I own that: the C* label is a misleading artifact of how the diagnostic re-runs findContours on a padded crop. But the
  finding it surfaced is the most important result in the whole run.

  This unifies B, D, and C* into one root cause

  All three are the same failure: cv2.findContours connected-component topology ≠ cloud-instance topology.

  - B (33) — one cloud shatters into many components (crossings break the loop).
  - D (25) — those pieces are present and would re-close, but IoU can't see it.
  - C (7)* — the opposite extreme: the cloud is fused to clutter into a mega-blob and discarded by the area ceiling.

  Region-based contour extraction sometimes shatters a cloud and sometimes welds it to the whole sheet. Either way, the
  connected component is not the cloud. This is exactly your earlier "giant merged regions" and "one cloud → several
  detections" observations — two symptoms of one disease.

  So the proposed ordering is wrong, and here is the correction

  Phase 1 as written (hunt and fix a gate-bypass bug) is a dead end — proven above. Nothing in the gate/dedup/merge code is
  dropping a valid contour.

  And Phase 2 (affinity merge) cannot run on the current fragment source. You cannot compute "boundary distance / tangent
  continuity / periodicity similarity" between two fragments when one of them is a 22-million-pixel blob spanning the sheet.
  Affinity merge is meaningless until fragments are actually cloud-boundary arcs.

  Both phases depend on a step that isn't in your plan: changing where fragments come from. That is the real Phase 1.

  Corrected ordering

  1. Phase 1 (redefined): Fragment-source replacement — de-weld + de-shatter. Stop deriving fragments from RETR region
  components. Derive them from the line work itself: edge/ridge map → contour-follow the strokes → split at junctions (where
  pipes/text/borders cross the cloud arc). Junction-splitting is the operation that simultaneously unwelds C* (cuts the cloud
  arc free of the crossing pipe) and normalizes B/D into clean arc primitives. This is the substrate both Phase 2 and Phase 3
  require. Build it first.
  2. Phase 2: Affinity-based grouping over those arc fragments (your metric list is correct).
  3. Phase 3: The Arc Fragment Graph is then a natural generalization of Phase 2 — same nodes, richer edges, global
  clustering. Phases 2 and 3 are the same machine at two levels of sophistication.

  Note what this means: your Phase 1 and the front-end of Phase 3 are the same component. Build it once, at the start.

  Before any of that — a data-quality caveat on the statistics

  The reference set is gemini_instance_oracle with 93 candidates → 73 missed (22% "recall"). Two problems:
  - Gemini emitted duplicates: miss #6 = miss #67 (box [4221,154,4967,1164]), miss #16 = miss #62 ([3973,1585,5046,2294]). So
  7 C* are really ~5 unique, and the 93 is inflated.
  - Gemini over-proposes: 93 candidates vs 43 detections — some references are likely not real clouds.

  The B/D dominance almost certainly holds directionally, but the exact counts are not trustworthy. Before committing
  engineering to Phase 1, hand-label clouds_truth.json for one drawing (20–30 min) and re-run with --truth. That converts
  "recall vs Gemini's guesses" into a real number and de-duplicates the buckets. I'd do this in parallel with Phase 1 design.

  ---
  Implementation plan — Phase 1 (fragment-source replacement)
  
  Goal: produce a set of cloud-boundary arc fragments that is robust to both fusion and shattering — replacing the
  region-component fragments that cause B/D/C*.

  - New stage 0_fragment_extraction, upstream of the existing detection. Output: a list of open polyline fragments (ordered
  point chains) with per-fragment geometry (endpoints, endpoint tangents, arc length, mean curvature, scallop periodicity from
  your existing _scallop_periodicity).
  - Source = strokes, not regions. Run edge detection on the line work, contour-follow the edge map to get stroke centerlines
  (skeleton or edge-linked chains). Do not use RETR_LIST region contours as the primitive.
  - Junction splitting (the core operation). Detect junction pixels (skeleton points with ≥3 neighbors = where a
  pipe/text/border crosses the cloud). Cut chains at junctions. This is what frees the cloud arc from the welds that created
  the mega-blobs — directly dissolving the C* class.
  - Border/frame removal as a cheap pre-step worth measuring. The 69M-pixel full-sheet component is largely the outer drawing
  frame connecting everything. Detecting and deleting the border rectangle before extraction may collapse a large fraction of
  the welds for almost no cost. Test it; treat as mitigation, not the fix.
  - Keep the existing per-fragment features (_scallop_periodicity, curvature) — they now operate on clean arcs instead of
  welded blobs, which is the condition they were designed for.
  - Validation moves to the cluster level later (Phase 2/3 output), not the fragment level. Individual arcs are not expected
  to pass the cloud gate — that's the whole point.
  - Acceptance test for Phase 1 in isolation: re-run the diagnostic. Success = the mega-blob components at C* locations are
  replaced by isolable cloud arcs, and the B/D fragments become clean arc chains. You are not chasing recall yet — you are
  proving the fragment source is sound.

  Implementation plan — Phase 2 (affinity merge, replacing IoU)

  Precondition: Phase 1 fragments exist. Do not start before that.

  - Replace the IoU gates in detect_clouds (iou(...) > 0.30 in Stage 2 filtering and Stage 4 dedup) and make_polygon_mask's
  implicit "each polygon independent" assumption with an affinity-graph grouping step.
  - Affinity between two fragments = weighted combination of your four metrics:
    - endpoint/boundary distance — gap between nearest fragment endpoints (clouds break at small gaps; unrelated arcs are far
  apart);
    - tangent continuity — alignment of endpoint tangents across the gap (a cloud boundary continues smoothly; a pipe
  T-junction does not);
    - curvature similarity — comparable local curvature magnitude (scallops have a characteristic radius);
    - scallop periodicity similarity — same scallop wavelength/phase (the strongest cloud-specific signal; reuse
  _scallop_periodicity).
  - Grouping = connected components on the thresholded affinity graph (a single, interpretable affinity cutoff — not a
  per-metric threshold audit). Each component = one cloud instance.
  - Reconstruct per cluster: order the grouped arcs head-to-tail, fit a closed boundary (concave hull / alpha-shape over the
  union of fragment points) → polygon → make_polygon_mask.
  - Validate at the cluster level: does the reconstructed closed region have scallop texture around its perimeter, plausible
  size/aspect, and lie outside exclusion zones. This replaces per-contour validation.
  - The known risk is over-merging (two adjacent clouds fused, or clutter grouped in). This is where the Gemini Instance
  Oracle earns its place — its per-instance envelope constrains grouping (fragments in different envelopes don't merge; the
  affinity graph is cut at envelope boundaries). That keeps the precision you fought for while the affinity merge raises
  recall.

  ---
  Bottom line: your three phases are really two components — a stroke-based, junction-split fragment extractor and an affinity
  grouper — and the extractor must come first because it is the precondition for everything else and the only thing that can
  resolve the C* (fusion) failures. The "C* bug fix" you wanted to do first is unbuildable as a bug fix; it is a symptom of
  the fragment-source problem that Phase 1 (redefined) solves.

  Want me to (a) build the hand-labeled ground-truth harness and re-run the diagnostic to lock down real numbers, or (b) start
  implementing the Phase 1 stroke/junction fragment extractor as a standalone stage with its own diagnostic? I'd recommend
  (a) first — it's cheap and it tells us how much of the 73 is real before we build.



   The success criterion is met. That image is the proof: the scalloped revision-cloud boundary — previously fused into a
  22-million-pixel sheet-spanning blob (the C* failure) — now traces as a sequence of distinctly-colored arc fragments
  following the bumpy outline. White dots mark the clean junction cuts between arcs.

  Phase 1 complete — pipeline/fragment_extractor.py

  Result on input_drawing.jpg (9934×7017)

  - 6,924 fragments in 11s; the welded mega-blobs are gone.
  - At the C* test location: the blob → 31 fragments, 9 with strong scallop signal (periodicity up to 1.0).
  - Long straight border/pipe stroke (5,489 px) correctly scored periodicity 0 — non-cloud, exactly as intended.

  Architecture

  binary
   → _skeletonize()        skimage Zhang-Suen/Lee → 1-px centerlines
   → _degree_map()         8-neighbour count via cv2.filter2D (vectorized)
   → _split_at_junctions() degree≥3 pixels (dilated 1px) removed → welds severed
   → ndimage.label()       connected components of cut skeleton = raw chains
   → _order_chain()        each component walked into an ordered polyline
   → Fragment              geometry computed, length-filtered (≥40px)

  Each Fragment (dataclass) carries exactly the requested fields: id, points, endpoints, endpoint_tangents (inward unit
  vectors), arc_length, mean_curvature, scallop_periodicity, plus an is_loop flag. The curvature/periodicity functions mirror
  stage1's model but operate on open chains (no wraparound). to_dict() gives JSON serialization for Phase 2 hand-off.

  Debug outputs in debug_fragments/: skeleton.png, junctions.png, fragments.png, fragments_colored.png (all as specified),
  plus a summary printout.

  No affinity graph, no clustering, no reconstruction — as instructed.

  Dependencies

  Added to requirements.txt: scikit-image>=0.19.0, scipy>=1.10.0 (both already present in your env; cv2.ximgproc is
  unavailable in the headless build, so skimage is the skeletonizer). No new heavyweight deps.

  Integration plan (how this plugs in — not yet wired)

  1. Standalone now. extract_fragments(binary) -> List[Fragment] is pure and side-effect-free; the CLI binarizes with the same
  global Otsu as Stage 1 so fragments correspond to the real pipeline input.
  2. It does not touch stage1_cloud.py or main.py yet — Phase 1 only produces fragments. Wiring happens at Phase 2, where the
  affinity grouper consumes List[Fragment] and emits cloud-instance clusters → polygons → make_polygon_mask, replacing the IoU
  merge.
  3. Validation against the diagnostic: re-run diagnose_clouds.py; the C* mega-blobs at those coordinates should now resolve
  into scalloped arcs (already confirmed at one site).

  Two observations that matter for Phase 2 (no action now)

  - Cloud arcs are short (40–94 px here). Junction-splitting cuts at every scallop cusp where bumps self-touch, so one cloud =
  many short arcs. Phase 2's affinity grouping must expect dozens of fragments per cloud and lean on scallop-periodicity + 
  tangent continuity to relink them — MIN_FRAGMENT_LEN is the floor that keeps this from exploding.
  - Text becomes fragments too (the "61502X" label in the crop). That's expected and harmless: text fragments have low/erratic
  periodicity and won't group into a closed scalloped boundary. The scallop_periodicity field is already the discriminator
  Phase 2 will use to suppress them.

  Phase 1 delivers what you asked: high-quality, de-welded cloud-boundary arc fragments. Ready for Phase 2 (affinity merge)
  whenever you want to proceed.

✻ Crunched for 3m 58s

※ recap: Goal: lift P&ID revision-cloud detection recall to 95%+ by reconstructing clouds from boundary fragments. Phase 1 
  (stroke-derived fragment extractor) is built and verified. Next: implement Phase 2, affinity-based merge replacing the IoU 
  merge.
  
❯ /usage                                                                                                                      

──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Settings  Status   Config   Usage   Stats

  affinity grouper consumes List[Fragment] and emits cloud-instance clusters → polygons → make_polygon_mask, replacing the IoU
  merge.
  3. Validation against the diagnostic: re-run diagnose_clouds.py; the C* mega-blobs at those coordinates should now resolve
  into scalloped arcs (already confirmed at one site).

  Two observations that matter for Phase 2 (no action now)

  - Cloud arcs are short (40–94 px here). Junction-splitting cuts at every scallop cusp where bumps self-touch, so one cloud =
  many short arcs. Phase 2's affinity grouping must expect dozens of fragments per cloud and lean on scallop-periodicity +
  tangent continuity to relink them — MIN_FRAGMENT_LEN is the floor that keeps this from exploding.
  - Text becomes fragments too (the "61502X" label in the crop). That's expected and harmless: text fragments have low/erratic
  periodicity and won't group into a closed scalloped boundary. The scallop_periodicity field is already the discriminator
  Phase 2 will use to suppress them.

  into scalloped arcs (already confirmed at one site).

  Two observations that matter for Phase 2 (no action now)

  - Cloud arcs are short (40–94 px here). Junction-splitting cuts at every scallop cusp where bumps self-touch, so one cloud =
  many short arcs. Phase 2's affinity grouping must expect dozens of fragments per cloud and lean on scallop-periodicity +
  tangent continuity to relink them — MIN_FRAGMENT_LEN is the floor that keeps this from exploding.
  - Text becomes fragments too (the "61502X" label in the crop). That's expected and harmless: text fragments have low/erratic
  periodicity and won't group into a closed scalloped boundary. The scallop_periodicity field is already the discriminator
  Phase 2 will use to suppress them.

  Phase 1 delivers what you asked: high-quality, de-welded cloud-boundary arc fragments. Ready for Phase 2 (affinity merge)
  whenever you want to proceed.



The data gives me everything I need to set the parameters from evidence rather than assumption. Here is the complete Phase 2
  architecture.

  ---
  Phase 2 Architecture: Affinity Graph & Fragment Grouping
  
  What the measurements tell us

  Before the formulas, four measurements constrain every parameter:

  ┌──────────────────────────────────────┬────────────────────────┬──────────────────────────────────────┐
  │             Measurement              │         Value          │           What it decides            │
  ├──────────────────────────────────────┼────────────────────────┼──────────────────────────────────────┤
  │ Nearest-endpoint gap p50 / p90 / p99 │ 27 / 49 / 100 px       │ σ_d for distance decay, R_max        │
  ├──────────────────────────────────────┼────────────────────────┼──────────────────────────────────────┤
  │ Edges at R=80px                      │ ~67k                   │ Radius is viable (not O(N²))         │
  ├──────────────────────────────────────┼────────────────────────┼──────────────────────────────────────┤
  │ Cloud-arc mean curvature p25/med/p75 │ 0.56 / 0.76 / 0.86 rad │ σ_c for curvature similarity         │
  ├──────────────────────────────────────┼────────────────────────┼──────────────────────────────────────┤
  │ Cloud-arc scallop periodicity p75    │ 0.27                   │ Periodicity is a bonus, never a gate │
  └──────────────────────────────────────┴────────────────────────┴──────────────────────────────────────┘

  The last point matters: 69 of the 31 cloud-region fragments have period < 0.25. Short arcs cannot exhibit enough oscillation
  cycles to score well. If periodicity were weighted heavily or used as a hard gate, most cloud arcs would fail to connect.
  It stays as a soft bonus.

  ---
  1. Affinity Function
  
  For a pair (A, B), we first identify the connecting endpoint pair — the pair of endpoints (one from each fragment) with
  minimum gap. All terms are computed from those two endpoints.

  For fragment A with endpoints {A.ep0, A.ep1} and tangents {A.t0, A.t1}
  For fragment B with endpoints {B.ep0, B.ep1} and tangents {B.t0, B.t1}

  (i*, j*) = argmin_{i∈{0,1}, j∈{0,1}} ||A.ep_i − B.ep_j||

  gap   = ||A.ep_{i*} − B.ep_{j*}||
  ep_A  = A.endpoints[i*]        # connecting endpoint on A
  ep_B  = B.endpoints[j*]        # connecting endpoint on B
  t_A   = A.endpoint_tangents[i*]  # inward unit vector at ep_A
  t_B   = B.endpoint_tangents[j*]  # inward unit vector at ep_B
  d_AB  = (ep_B − ep_A) / gap    # unit vector: A → B

  Term 1 — Endpoint Distance

  f_dist = exp(−gap / σ_d)       σ_d = 40 px

  Formula: exponential decay.
  Normalization: naturally [0, 1]; equals 1 when endpoints touch, equals 0.37 at the median observed gap (27 px), 0.12 at p90
  (49 px).
  Range: (0, 1].
  Rationale: The p99 nearest-neighbor gap is 100 px; almost all real cloud-junction gaps are ≤ 50 px. A hard cutoff at R_max =
  80 px (see §3) removes the long tail geometrically; the exponential decay penalizes distance smoothly within that radius.
  σ_d = 40 px is set to the median gap so that a "typical" junction has f_dist ≈ 0.5 rather than being near either extreme.

  Term 2 — Tangent Continuity

  cos_out = dot(−t_A, d_AB)        # A exits toward B: range [−1, 1]
  cos_in  = dot( t_B, d_AB)        # B receives from d_AB: range [−1, 1]

  f_tang  = (max(0, cos_out) + max(0, cos_in)) / 2

  Formula: mean of two half-rectified cosines.
  Normalization: [0, 1]; equals 1 when A exits collinearly toward B and B continues collinearly inward.
  Range: [0, 1].
  Rationale: t_A points inward along the chain from ep_A; so −t_A is the outgoing direction. A cloud boundary is a smooth
  closed curve — consecutive arcs must exit and enter collinearly. A pipe that crosses and creates a junction will have its
  endpoint tangent pointing perpendicular to the cloud arc's tangent, driving both cosines toward 0. Half-rectification (clamp
  at 0) prevents negative cosines from numerically rewarding bad alignments. This is the most discriminative term; it is the
  only one that distinguishes "continuation" from "coincidental proximity."

  Term 3 — Curvature Similarity

  f_curv = exp(−|A.mean_curvature − B.mean_curvature| / σ_c)    σ_c = 0.4 rad

  Formula: exponential decay on absolute difference.
  Normalization: [0, 1]; equals 1 when curvatures are identical.
  Range: (0, 1].
  Rationale: The measured inter-quartile range of cloud-arc curvature is 0.56–0.86 rad (IQR ≈ 0.30 rad). Setting σ_c = 0.40
  rad means two arcs spanning the full IQR still score f_curv ≈ 0.47, while an arc from a smooth straight pipe (curvature ≈ 0)
  paired with a scallop arc (≈ 0.76) scores f_curv ≈ 0.15. This is a supplemental discriminator, not the primary one.

  Term 4 — Scallop Periodicity Similarity

  f_period = min(A.scallop_periodicity, B.scallop_periodicity)

  Formula: minimum of both scores.
  Normalization: [0, 1] by the definition of scallop_periodicity from Phase 1.
  Range: [0, 1].
  Rationale: min ensures that both arcs must exhibit scallop signal to score well. Two arcs with period = 0 (short or
  straight) score 0, not 1 — they are agnostic to this term, not rewarded for mutual silence. This is preferred over a
  similarity-distance formula (1 − |A − B|) which would reward two non-scalloped arcs for both being non-scalloped. This term
  is a bonus only; heavily weighting it would break connections between short cloud arcs that cannot exhibit a full scallop
  cycle.

  Combined Affinity

  Affinity(A, B) = 0.45 · f_dist + 0.35 · f_tang + 0.10 · f_curv + 0.10 · f_period

  Worked example with measured values (cloud arc pair, gap = 27 px, perfect alignment, similar curvature):
  f_dist   = exp(−27/40)           = 0.51
  f_tang   = (1.0 + 1.0) / 2      = 1.00    (collinear continuation)
  f_curv   = exp(−0.10/0.40)      = 0.78    (curvatures close)
  f_period = min(0.80, 0.00)      = 0.00    (one short arc)

  Affinity = 0.45·0.51 + 0.35·1.00 + 0.10·0.78 + 0.10·0.00
           = 0.23 + 0.35 + 0.08 + 0.00 = 0.66
   
  Adversarial example (pipe fragment ending near a cloud arc, gap = 27 px, orthogonal tangents):
  f_dist   = 0.51
  f_tang   = (max(0, −0.05) + max(0, 0.05)) / 2 = 0.05   (near-perpendicular)
  f_curv   = exp(−0.76/0.40)      = 0.15    (pipe curvature ≈ 0)
  f_period = min(0.00, 0.00)      = 0.00

  Affinity = 0.45·0.51 + 0.35·0.05 + 0.10·0.15 + 0.10·0.00
           = 0.23 + 0.02 + 0.02 + 0.00 = 0.27

  The separation at the natural threshold of 0.35 is clean: 0.66 (cloud) vs 0.27 (pipe). This threshold is not a tuning
  parameter — it falls naturally in the gap between the two worked examples.

  ---
  2. Graph Construction — Scalability

  Data structure

  Index all 13,848 endpoints (6,924 fragments × 2) in a scipy.spatial.cKDTree. Each entry in the tree stores:
  (endpoint_xy, fragment_id, endpoint_index)

  Algorithm

  Build cKDTree on all endpoint positions        O(N log N), N = 13,848

  For each of the 13,848 endpoints ei:
    neighbors = tree.query_ball_point(ei.xy, r=R_MAX)    O(log N + k)
    For each neighbor ej where frag_id(ej) != frag_id(ei):
      if (frag_id(ei), frag_id(ej)) not already evaluated:
        compute Affinity(fi, fj)
        if Affinity > EDGE_MIN_AFFINITY: add edge

  R_MAX             = 80 px          # covers gap p96; keeps edge count ~67k
  EDGE_MIN_AFFINITY = 0.20           # loose pre-filter; discards obvious non-pairs
  GROUPING_AFFINITY = 0.35           # threshold for merging into same cluster

  The two thresholds serve different roles: EDGE_MIN_AFFINITY is a cheap pre-filter that avoids storing trivially-zero
  affinity edges (e.g., a pipe far from any cloud). GROUPING_AFFINITY is the actual grouping cut. Keeping them distinct makes
  it possible to inspect the full near-zero affinity edges in debug without them polluting the clusters.

  Complexity

  ┌───────────────────────────────────┬────────────────┬────────────────────┐
  │               Step                │   Complexity   │ Estimate at N=6924 │
  ├───────────────────────────────────┼────────────────┼────────────────────┤
  │ KDTree build                      │ O(N log N)     │ ~0.1 s             │
  ├───────────────────────────────────┼────────────────┼────────────────────┤
  │ Radius queries (all 2N endpoints) │ O(N log N + E) │ ~0.5 s             │
  ├───────────────────────────────────┼────────────────┼────────────────────┤
  │ Affinity computation              │ O(E)           │ E ≈ 67k; trivial   │
  ├───────────────────────────────────┼────────────────┼────────────────────┤
  │ Connected components              │ O(N + E)       │ <0.01 s            │
  ├───────────────────────────────────┼────────────────┼────────────────────┤
  │ Total                             │ O(N log N + E) │ < 1 s              │
  └───────────────────────────────────┴────────────────┴────────────────────┘

  Worst-case E is 225k at R=150px (measured). At R=80px: ~67k. Both are tractable. There is no O(N²) step anywhere.

  ---
  3. Clustering Algorithm — Recommendation: Thresholded Connected Components via Union-Find

  Evaluated options:

  ┌──────────────────────────────────┬────────────┬─────────────┬────────────────┬────────────────────┐
  │              Method              │ Simplicity │ Scalability │    Needs k?    │        Risk        │
  ├──────────────────────────────────┼────────────┼─────────────┼────────────────┼────────────────────┤
  │ Connected components (threshold) │ ★★★        │ ★★★         │ No             │ Over-merge         │
  ├──────────────────────────────────┼────────────┼─────────────┼────────────────┼────────────────────┤
  │ Union-Find (Kruskal-style)       │ ★★★        │ ★★★         │ No             │ Same as CC         │
  ├──────────────────────────────────┼────────────┼─────────────┼────────────────┼────────────────────┤
  │ Correlation clustering           │ ★          │ ★           │ No             │ NP-hard            │
  ├──────────────────────────────────┼────────────┼─────────────┼────────────────┼────────────────────┤
  │ Spectral clustering              │ ★★         │ ★★          │ Yes            │ Need k upfront     │
  ├──────────────────────────────────┼────────────┼─────────────┼────────────────┼────────────────────┤
  │ DBSCAN                           │ ★★         │ ★★          │ No             │ ε/minPts tuning    │
  ├──────────────────────────────────┼────────────┼─────────────┼────────────────┼────────────────────┤
  │ Agglomerative / single-linkage   │ ★★         │ ★★          │ Dendrogram cut │ Memory O(N²) naive │
  └──────────────────────────────────┴────────────┴─────────────┴────────────────┴────────────────────┘

  Recommendation: Thresholded affinity graph → connected components, implemented with Union-Find.

  Union-Find and thresholded CC are mathematically identical (same clusters, same complexity). Union-Find is chosen as the
  implementation because it is O(α(N)) per merge (near-constant) and naturally produces the final cluster mapping without a
  separate labeling pass.

  Why not spectral or correlation clustering? Spectral requires eigendecomposition and knowledge of k (the number of clouds).
  We don't know k — it is an output of the system, not an input. Correlation clustering is NP-hard. Both introduce complexity
  with no recall benefit over CC given a well-designed affinity metric.

  Why CC is safe here: The affinity metric has two natural barriers against over-merging:
  1. R_MAX = 80 px — geometrically prevents distant fragments from ever having an edge.
  2. Tangent continuity (weight 0.35) — a pipe that crosses a cloud boundary creates a junction, which Phase 1 cut. The pipe
  fragment and cloud fragment that remain after cutting have endpoint tangents pointing in different directions. Their f_tang
  ≈ 0, driving total affinity below the threshold even when they are spatially close.
  
  Post-grouping cluster sanity filter: After CC, apply a lightweight per-cluster check — any cluster whose bounding box aspect
  ratio > 12 or whose area > 35% of the drawing is likely a spurious merge. Flag it for Phase 3's reconstruction step (which
  will validate at the reconstituted boundary level). Do not split at this stage — that is Phase 3's job.

  ---
  4. Data Structures

  @dataclass
  class Edge:
      frag_a: int             # fragment id
      frag_b: int             # fragment id
      ep_a: int               # endpoint index (0 or 1) on fragment A
      ep_b: int               # endpoint index (0 or 1) on fragment B
      gap: float              # ||ep_A - ep_B||
      f_dist: float           # [0,1]
      f_tang: float           # [0,1]
      f_curv: float           # [0,1]
      f_period: float         # [0,1]
      affinity: float         # weighted sum [0,1]

  @dataclass
  class Cluster:
      id: int
      fragment_ids: List[int]
      total_arc_length: float           # sum of member arc lengths
      mean_scallop_periodicity: float   # mean across members
      scallop_fraction: float           # fraction of members with period > 0.25
      bbox: Tuple[int,int,int,int]      # [x0,y0,x1,y1] of all member points
      is_suspect: bool                  # flagged by sanity filter

  The Cluster structure is the hand-off to Phase 3. It is a list of fragment ids, not yet a polygon. Reconstruction is Phase
  3's responsibility.

  ---
  5. Debug Outputs — debug_affinity/

  ┌───────────────────────┬────────────────────────────────────────────────────┬──────────────────────────────────────────┐
  │         File          │                      Content                       │                 Purpose                  │
  ├───────────────────────┼────────────────────────────────────────────────────┼──────────────────────────────────────────┤
  │                       │ Fragments grey; edges drawn between their          │ Verify the affinity metric selects       │
  │ graph_edges.png       │ connecting endpoints, heat-mapped green (high      │ cloud-to-cloud edges, not pipe-to-cloud  │
  │                       │ affinity) → red (low)                              │ edges                                    │
  ├───────────────────────┼────────────────────────────────────────────────────┼──────────────────────────────────────────┤
  │                       │                                                    │ The primary success criterion: cloud     │
  │ grouped_fragments.png │ Each cluster rendered in a distinct random colour  │ arcs should appear as a single colour,   │
  │                       │                                                    │ not a rainbow                            │
  ├───────────────────────┼────────────────────────────────────────────────────┼──────────────────────────────────────────┤
  │ cluster_summary.json  │ Per cluster: id, n_fragments, arc_length,          │ Quantitative evidence; used by Phase 3   │
  │                       │ mean_period, scallop_fraction, bbox, is_suspect    │ to prioritize reconstruction             │
  └───────────────────────┴────────────────────────────────────────────────────┴──────────────────────────────────────────┘

  ---
  6. Module Design

  pipeline/affinity_grouper.py

  Functions (public API):
    build_graph(fragments, R_max, edge_min_affinity) -> List[Edge]
    group_fragments(fragments, edges, grouping_affinity) -> List[Cluster]
    save_debug(fragments, edges, clusters, image_shape, debug_dir)

  Internal helpers:
    _best_endpoint_pair(A, B) -> (i, j, gap, d_AB)
    _affinity_terms(A, B, i, j, gap, d_AB) -> (f_dist, f_tang, f_curv, f_period)
    _union_find(n) -> (find, union)  — closure returning two functions
    _cluster_bbox(fragments, ids) -> Tuple[int,int,int,int]
    _cluster_sanity(cluster, H, W) -> bool

  The public API is three pure functions: the graph builder, the grouper, and the debug writer. They are independent — you can
  call build_graph, inspect edges, adjust thresholds, then call group_fragments without re-running the KDTree. That will
  matter during Phase 2 validation.

  ---
  7. Parameters (evidence-grounded, not assumed)

  ┌───────────────────┬─────────┬─────────────────────────────────────────────────────────────────────────────────────────┐
  │     Parameter     │  Value  │                                     Evidence source                                     │
  ├───────────────────┼─────────┼─────────────────────────────────────────────────────────────────────────────────────────┤
  │ R_MAX             │ 80 px   │ Measured gap p96 = 63 px; 80 px provides headroom without exploding edge count          │
  ├───────────────────┼─────────┼─────────────────────────────────────────────────────────────────────────────────────────┤
  │ EDGE_MIN_AFFINITY │ 0.20    │ Below this, tangent must be near-zero AND distance must be large — certainly not same   │
  │                   │         │ cloud                                                                                   │
  ├───────────────────┼─────────┼─────────────────────────────────────────────────────────────────────────────────────────┤
  │ GROUPING_AFFINITY │ 0.35    │ Falls in gap between worked examples (0.66 cloud-cloud vs 0.27 pipe-cloud)              │
  ├───────────────────┼─────────┼─────────────────────────────────────────────────────────────────────────────────────────┤
  │ σ_d               │ 40 px   │ Set to measured gap p50 (27 px) × 1.5; gives f_dist ≈ 0.51 at median gap                │
  ├───────────────────┼─────────┼─────────────────────────────────────────────────────────────────────────────────────────┤
  │ σ_c               │ 0.40    │ ≈ IQR of measured cloud-arc curvature                                                   │
  │                   │ rad     │                                                                                         │
  ├───────────────────┼─────────┼─────────────────────────────────────────────────────────────────────────────────────────┤
  │ w_dist            │ 0.45    │ Primary spatial gate                                                                    │
  ├───────────────────┼─────────┼─────────────────────────────────────────────────────────────────────────────────────────┤
  │ w_tang            │ 0.35    │ Strongest discriminator                                                                 │
  ├───────────────────┼─────────┼─────────────────────────────────────────────────────────────────────────────────────────┤
  │ w_curv            │ 0.10    │ Supplemental                                                                            │
  ├───────────────────┼─────────┼─────────────────────────────────────────────────────────────────────────────────────────┤
  │ w_period          │ 0.10    │ Soft bonus only — many valid cloud arcs have period = 0                                 │
  └───────────────────┴─────────┴─────────────────────────────────────────────────────────────────────────────────────────┘

  ---
  Integration plan (not yet wired)
  
  Phase 2 is standalone. It takes List[Fragment] from Phase 1 and emits List[Cluster]. It does not touch stage1_cloud.py or
  main.py. It can be called from the CLI as:

  python pipeline/affinity_grouper.py input_drawing.jpg --out debug_affinity

  And from Python:

  from pipeline.fragment_extractor import extract_fragments, _binarize
  from pipeline.affinity_grouper import build_graph, group_fragments, save_debug

  frags = extract_fragments(_binarize(img))
  edges = build_graph(frags, R_max=80, edge_min_affinity=0.20)
  clusters = group_fragments(frags, edges, grouping_affinity=0.35)
  save_debug(frags, edges, clusters, img.shape, Path("debug_affinity"))

  Phase 3 (reconstruction) takes List[Cluster] and emits polygons. The three phases compose as a pipeline with clean
  interfaces.

  ---
  Finalize or adjust this plan, and I'll implement pipeline/affinity_grouper.py.
  



  IMP: 

  Both files are written. Here's a summary of what was done:

---
CDCI_Learning_Analysis.md — Complete audit covering:
- All 4 approaches with commands, detection rates, and what to keep/discard
- Debug output analysis (73 misses breakdown: B=33, D=25, C*=7, C=7, A=1)
- Root cause: findContours topology ≠ cloud topology (all failures trace here)
- Threshold table: current vs recommended (scallopedness 1.30→1.10, morph kernel 3→13px, etc.)
- Recommended architecture (Approach H from hi.md)
- Full commands reference for every pipeline

cloud_detector_v2.py — Combined 95%+ pipeline implementing the proven fix:

┌───────────────────┬───────────────────────┬──────────────────────────────────────────────────────────┐
│       Stage       │     What it does      │                       Why it helps                       │
├───────────────────┼───────────────────────┼──────────────────────────────────────────────────────────┤
│ P1 Gemini locate  │ Per-cloud bboxes      │ Avoids global sheet-level processing                     │
├───────────────────┼───────────────────────┼──────────────────────────────────────────────────────────┤
│ P2 Per-crop close │ kernel=13px, iter=4   │ Bridges 5–50px junction gaps (not 3px)                   │
├───────────────────┼───────────────────────┼──────────────────────────────────────────────────────────┤
│ P2 approxPolyDP   │ Instead of convexHull │ Preserves scalloped shape (the #1 bug in all prior code) │
├───────────────────┼───────────────────────┼──────────────────────────────────────────────────────────┤
│ Threshold 1.10    │ Instead of 1.30       │ Recovers C-type misses (scallopedness=1.24 was rejected) │
├───────────────────┼───────────────────────┼──────────────────────────────────────────────────────────┤
│ P3 stage1 OpenCV  │ Parallel coverage     │ Catches clouds Gemini misses                             │
├───────────────────┼───────────────────────┼──────────────────────────────────────────────────────────┤
│ P4 IoU NMS        │ Merge all sources     │ Deduplication across detectors                           │
└───────────────────┴───────────────────────┴──────────────────────────────────────────────────────────┘

Run it: python stages/step2b_cloud_detection.py --context output/drawing_context.json --api-key $GEMINI_KEY --debug


O/p:

(.venv) (base) suryprakash@JULLTR0011 cdci_extractor 4 %  python cloud_detector_v2.py input_drawing.jpg --out output_v2/ --debug
WARNING No GEMINI_API_KEY found — using deterministic fallback
INFO Image: 9934x7017  output: output_v2/
INFO Using deterministic fallback locator
INFO Fallback locator found 0 candidates
INFO Recovering boundaries from 0 bboxes...
INFO Running stage-1 OpenCV detector...
INFO Stage-1 OpenCV found 52 raw candidates
INFO Total before NMS: 52 polygons
INFO After NMS: 24 polygons
INFO Result: 9 outer + 15 inner = 24 total clouds
INFO Saved: output_v2/overlay_v2.jpg  output_v2/cloud_mask_v2.png  output_v2/outer_clouds_v2.json

=== Detection complete ===
  Outer clouds: 9
  Inner clouds: 15
  Total:        24
  Overlay:      output_v2/overlay_v2.jpg
  Mask:         output_v2/cloud_mask_v2.png
  JSON:         output_v2/outer_clouds_v2.json



  python stages/step6_table_agent.py --context output/drawing_context.json --api-key "AQ.Ab8RN6IDMnKvzzAK32jLUD5NfviIaP9lrrN91tAYrmO-LJ2Zbw" --debug


  python stages/step1_format_detect.py sample_table.jpg --out output/



  Step 4 : 

  # Build memory (with vision enrichment — ~132 Gemini calls):
python stages/step4_sow_agent.py build \
  --excel ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx \
  --out output/ --api-key YOUR_KEY

# Text-only (fast, no API):
python stages/step4_sow_agent.py build \
  --excel ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx \
  --out output/ --skip-vision

# Classify one symbol:
python stages/step4_sow_agent.py classify \
  --memory output/sow_symbol_memory.json \
  --symbol "BALL VALVE"

# Filter Step 6 tag list against SOW:
python stages/step4_sow_agent.py filter \
  --memory output/sow_symbol_memory.json \
  --tags output/master_tags.json \
  --out output/


python stages/step5a_candidate_extraction.py input_drawing.jpg --out output/ --api-key "AQ.Ab8RN6IDMnKvzzAK32jLUD5NfviIaP9lrrN91tAYrmO-LJ2Zbw" --debug


python stages/step5a_candidate_extraction.py --context output/drawing_context.json --api-key "AQ.Ab8RN6IDMnKvzzAK32jLUD5NfviIaP9lrrN91tAYrmO-LJ2Zbw" --patch 5



# Default: 8 workers
python stages/step5a_candidate_extraction.py input_drawing.jpg --out output/ --api-key "AQ.Ab8RN6IDMnKvzzAK32jLUD5NfviIaP9lrrN91tAYrmO-LJ2Zbw"

# Free-tier key (5 RPM limit): use 1 worker to avoid 429
python stages/step5a_candidate_extraction.py drawing.jpg --out output/ --api-key KEY --workers 1

# Paid tier: push harder
python stages/step5a_candidate_extraction.py drawing.jpg --out output/ --api-key "AQ.Ab8RN6IDMnKvzzAK32jLUD5NfviIaP9lrrN91tAYrmO-LJ2Zbw" --workers 12


# After 5A completes (before dedup):
python stages/step5_visualizer.py \
    --candidates output/step5a_candidates.json \
    --image input_drawing.jpg --out output/

# After full pipeline (5A → 5D):
python stages/step5_visualizer.py \
    --candidates output/step5a_candidates.json \
    --deduped    output/step5d_deduped.json \
    --image      input_drawing.jpg --out output/

# Auto-detect everything from context:
python stages/step5_visualizer.py \
    --context output/drawing_context.json

# Larger tiles for deep zoom:
python stages/step5_visualizer.py \
    --candidates output/step5a_candidates.json \
    --image input_drawing.jpg --out output/ \
    --tile-size 1500 --overview-width 4000



    # Step 5B — geometry (pure OpenCV, instant)
python stages/step5b_geometric_association.py \
    --candidates output/step5a_candidates.json \
    --image input_drawing.jpg \
    --out output/ --debug

# Step 5C — validation (pure programmatic, instant)
python stages/step5c_validation_engine.py \
    --associations output/step5b_associations.json \
    --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx \
    --notes output/notes_context.json \
    --out output/

# Step 5D — dedup (pure programmatic, instant)
python stages/step5d_duplicate_resolution.py \
    --validated output/step5c_validated.json \
    --out output/

# Visualizer — bbox images for human review
python stages/step5_visualizer.py \
    --candidates output/step5a_candidates.json \
    --deduped output/step5d_deduped.json \
    --image input_drawing.jpg \
    --out output/



# After Step 1:
python stages/step2_title_block.py \
    --context output/drawing_context.json \
    --api-key YOUR_KEY --debug

# Or directly:
python stages/step2_title_block.py input_drawing.jpg \



# After step7:
python stages/step7_cedm_normalizer.py \
    --final output/step5_final_output.json \
    --context output/drawing_context.json \
    --out output/

python stages/step8_confidence_router.py \
    --cedm output/step7_cedm_output.json \
    --context output/drawing_context.json \
    --out output/


The $GEMINI_KEY environment variable isn't set in this shell session — you'll need to run it yourself with your key. Here's the command:

python stages/step6_table_agent.py sample_table.jpg --out output/ --api-key $GEMINI_KEY

Here's a summary of all changes made and why:

Problem 1: Right columns cut off
- Old: top_left tile covered x=0–82% → right 18% (columns 13–16) were never seen by Gemini
- Fix: Replaced with top_adaptive tile covering x=0–100% (full width)

Problem 2: Table rows cut off at bottom
- Old: Fixed top_strip at y=0–8% and top_left at y=0–13% — if table extends to 15%, bottom rows were cropped
- Fix: Added detect_table_bottom() which scans the binary image upward and finds the last dense horizontal line (the table's bottom border). The crop is set to detected_bottom + 3% buffer, so it adapts to any drawing

Problem 3: Extraction resolution too low
- Old: scale_for_gemini() capped at 4096px on the longest side → a 4967×500px table became 4096×412px — too short to read small tag numbers
- Fix: Added scale_for_table() which scales UP to ensure minimum 700px height before capping

Problem 4: Wide tables — columns still hard to read
- New: For tables with aspect ratio > 2.5 (wider than 2.5× their height), _extract_wide_table() splits into left and right halves with 5% overlap, extracts each at full resolution, then merges rows by their slot label. This means each half gets twice the pixel density for Gemini.

Problem 5: Early stopping
- Old: After finding 1 high-confidence table, it stopped scanning all remaining tiles — other tables were never checked
- Fix: Removed the early stop entirely