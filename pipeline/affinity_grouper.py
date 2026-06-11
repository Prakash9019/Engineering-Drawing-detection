"""
Phase 2: Affinity Graph Construction and Fragment Grouping
==========================================================
Consumes List[Fragment] from Phase 1 and produces List[Cluster].
No polygon reconstruction — grouping only.

DESIGN
------
Each fragment is a graph node. Edges are built via cKDTree radius search
(avoids O(N²) all-pairs). Each edge carries an affinity score:

    Affinity(A, B) = W_DIST·f_dist + W_TANG·f_tang + W_CURV·f_curv + W_PERIOD·f_period

Clusters = connected components on the thresholded affinity graph,
implemented with Union-Find for O(α(N)) per merge.

WEIGHT RATIONALE (from pre-implementation review)
--------------------------------------------------
The plan document specified W_DIST=0.45, W_TANG=0.35. Analysis showed a
concrete flaw: at a 1–2 px junction-artifact gap, f_dist ≈ 0.97, so
W_DIST · f_dist ≈ 0.44 exceeds the 0.35 grouping threshold *before tangent
is consulted*. Tangent continuity is rendered irrelevant at close range —
exactly the wrong behaviour at junction cuts where pipe and cloud-arc
endpoints are 1-2 px apart.

Fix: W_TANG raised to 0.55 (dominant term), W_DIST lowered to 0.25.
Worst-case false merge (gap=1.4px, f_tang=0, f_curv=1): 0.342 < 0.35 ✓
Cloud-cloud at gap=27px (f_tang=1): 0.777 >> 0.35 ✓

TANGENT FORMULA FIX
-------------------
The plan specified f_tang = (max(0,cos_out) + max(0,cos_in)) / 2 (average).
This gives f_tang = 0.35 when one cosine is 0 and the other is 0.71 —
rewarding a half-aligned pipe fragment.

Fix: f_tang = max(0,cos_out) · max(0,cos_in) (product).
Both endpoints must show collinear continuation. If either is non-positive,
f_tang = 0. The product does not change the symmetric property:
Affinity(A,B) == Affinity(B,A) is preserved.

INTERFACE
---------
    from pipeline.affinity_grouper import build_graph, group_fragments, save_debug
    edges    = build_graph(fragments)
    clusters = group_fragments(fragments, edges, image_shape=img.shape)
    save_debug(fragments, edges, clusters, img.shape, Path('debug_affinity'), img)
"""
import json
import logging
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from scipy.spatial import cKDTree

from pipeline.fragment_extractor import Fragment

log = logging.getLogger(__name__)

# ── Parameters (evidence-grounded; deviations from plan noted above) ─────────
R_MAX             = 80.0    # px: KDTree search radius  (gap p96 = 63 px)
SIGMA_D           = 40.0    # px: distance decay         (set to gap p50 × 1.5)
SIGMA_C           = 0.40    # rad: curvature decay       (≈ IQR of cloud arc curvature)
W_DIST            = 0.25    # distance weight            (lowered from plan 0.45)
W_TANG            = 0.55    # tangent weight             (raised from plan 0.35)
W_CURV            = 0.10    # curvature weight
W_PERIOD          = 0.10    # periodicity weight
_WEIGHT_SUM       = W_DIST + W_TANG + W_CURV + W_PERIOD
assert abs(_WEIGHT_SUM - 1.0) < 1e-9, f"weights must sum to 1.0, got {_WEIGHT_SUM}"

EDGE_MIN_AFFINITY = 0.20    # loose pre-filter for stored edges
GROUPING_AFFINITY = 0.35    # threshold for merging into same cluster
MAX_CLUSTER_ASPECT = 12.0   # sanity: flag clusters with extreme aspect ratio
MAX_CLUSTER_AREA_F = 0.35   # sanity: flag clusters covering > 35% of image
DEBUG_MAX_DIM     = 4000    # px: longest side of debug images


# ═══════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════
@dataclass
class Edge:
    """A directed-less affinity edge between two fragments."""
    frag_a:   int    # fragment id
    frag_b:   int    # fragment id
    ep_a:     int    # connecting endpoint index on frag_a (0 or 1)
    ep_b:     int    # connecting endpoint index on frag_b (0 or 1)
    gap:      float  # px distance between the connecting endpoints
    f_dist:   float  # [0,1] distance term
    f_tang:   float  # [0,1] tangent continuity term
    f_curv:   float  # [0,1] curvature similarity term
    f_period: float  # [0,1] periodicity similarity term
    affinity: float  # [0,1] weighted sum

    def to_dict(self) -> dict:
        return {k: round(v, 4) if isinstance(v, float) else v
                for k, v in self.__dict__.items()}


@dataclass
class Cluster:
    """A group of fragments that belong to one cloud instance."""
    id:                       int
    fragment_ids:             List[int]
    total_arc_length:         float
    mean_scallop_periodicity: float
    scallop_fraction:         float   # fraction of members with period > 0.25
    bbox:                     Tuple[int, int, int, int]  # x0,y0,x1,y1
    is_suspect:               bool    # flagged by sanity filter

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'n_fragments': len(self.fragment_ids),
            'fragment_ids': self.fragment_ids,
            'total_arc_length': round(self.total_arc_length, 1),
            'mean_scallop_periodicity': round(self.mean_scallop_periodicity, 3),
            'scallop_fraction': round(self.scallop_fraction, 3),
            'bbox': list(self.bbox),
            'is_suspect': self.is_suspect,
        }


# ═══════════════════════════════════════════════════════════════════
# Union-Find (path-halving, union by rank)
# ═══════════════════════════════════════════════════════════════════
class _UnionFind:
    """
    Disjoint-set with path-halving and union by rank.
    O(α(n)) amortized per operation; iterative (no recursion depth risk).
    """
    def __init__(self, n: int):
        self._parent = list(range(n))
        self._rank   = [0] * n

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]  # path halving
            x = self._parent[x]
        return x

    def union(self, x: int, y: int) -> bool:
        """Merge x and y. Returns True if they were in different components."""
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return False
        if self._rank[rx] < self._rank[ry]:
            rx, ry = ry, rx
        self._parent[ry] = rx
        if self._rank[rx] == self._rank[ry]:
            self._rank[rx] += 1
        return True

    def components(self, n: int) -> List[List[int]]:
        """Return all connected components as lists of node indices."""
        groups: dict = {}
        for i in range(n):
            r = self.find(i)
            if r not in groups:
                groups[r] = []
            groups[r].append(i)
        return list(groups.values())


# ═══════════════════════════════════════════════════════════════════
# Affinity computation
# ═══════════════════════════════════════════════════════════════════
def _best_endpoint_pair(fa: Fragment, fb: Fragment
                        ) -> Tuple[int, int, float, np.ndarray]:
    """
    Find the endpoint pair (i ∈ {0,1}, j ∈ {0,1}) with minimum gap.

    Returns (ep_i, ep_j, gap_px, unit_d_ab) where d_ab points from
    fa.endpoints[ep_i] toward fb.endpoints[ep_j].
    """
    best_gap = float('inf')
    best_i = best_j = 0
    best_d = np.zeros(2, dtype=np.float64)

    for i in range(2):
        for j in range(2):
            diff = fb.endpoints[j].astype(np.float64) - fa.endpoints[i].astype(np.float64)
            gap = float(np.hypot(diff[0], diff[1]))
            if gap < best_gap:
                best_gap = gap
                best_i, best_j = i, j
                if gap > 1e-6:
                    best_d = diff / gap
                else:
                    best_d = np.zeros(2, dtype=np.float64)  # coincident endpoints

    return best_i, best_j, best_gap, best_d


def _compute_affinity_terms(fa: Fragment, fb: Fragment,
                             ep_i: int, ep_j: int,
                             gap: float, d_ab: np.ndarray
                             ) -> Tuple[float, float, float, float]:
    """
    Compute (f_dist, f_tang, f_curv, f_period) for a given endpoint pair.
    All terms guaranteed in [0, 1].

    f_tang uses the PRODUCT formula (not average) so that BOTH endpoint
    directions must align — a pipe endpoint with only one aligned direction
    gives f_tang = 0 rather than 0.35.
    """
    # ── Term 1: endpoint distance ─────────────────────────────────
    f_dist = float(np.exp(-gap / SIGMA_D))

    # ── Term 2: tangent continuity (product formula) ───────────────
    t_a = fa.endpoint_tangents[ep_i]
    t_b = fb.endpoint_tangents[ep_j]
    t_a_norm = float(np.hypot(t_a[0], t_a[1]))
    t_b_norm = float(np.hypot(t_b[0], t_b[1]))

    if gap < 1e-6 or t_a_norm < 1e-6 or t_b_norm < 1e-6:
        # Coincident endpoints or degenerate tangent: neutral score.
        # Do not reward or penalise — let distance + curvature decide.
        f_tang = 0.5
    else:
        # -t_a is the outgoing direction from fa through ep_i.
        # t_b is the inward direction of fb through ep_j.
        # For a smooth continuation: both should align with d_ab.
        cos_out = float(np.dot(-t_a, d_ab))   # [-1, 1]
        cos_in  = float(np.dot( t_b, d_ab))   # [-1, 1]
        # Product: both must be positive; one perpendicular kills the score.
        f_tang = max(0.0, cos_out) * max(0.0, cos_in)

    # ── Term 3: curvature similarity ──────────────────────────────
    f_curv = float(np.exp(-abs(fa.mean_curvature - fb.mean_curvature) / SIGMA_C))

    # ── Term 4: scallop periodicity (min — both must show signal) ─
    f_period = float(min(fa.scallop_periodicity, fb.scallop_periodicity))

    return f_dist, f_tang, f_curv, f_period


def _affinity(f_dist: float, f_tang: float,
              f_curv: float, f_period: float) -> float:
    """Weighted sum of affinity terms. Always in [0, 1]."""
    return W_DIST * f_dist + W_TANG * f_tang + W_CURV * f_curv + W_PERIOD * f_period


# ═══════════════════════════════════════════════════════════════════
# Graph construction
# ═══════════════════════════════════════════════════════════════════
def build_graph(fragments: List[Fragment],
                r_max: float = R_MAX,
                edge_min_affinity: float = EDGE_MIN_AFFINITY) -> List[Edge]:
    """
    Build the affinity graph with O(N log N + E) complexity.

    Strategy
    --------
    Index all 2N endpoints in a cKDTree. For each endpoint, radius-query
    to find candidate partners. Evaluate each fragment-pair exactly once
    (deduplication via a set of (min_id, max_id) pairs). Loop fragments
    have no free endpoints and are excluded.

    Args:
        fragments:         Phase 1 output.
        r_max:             KDTree search radius (px).
        edge_min_affinity: Pre-filter; edges below this are not stored.

    Returns:
        List[Edge], sorted by affinity descending.
    """
    if not fragments:
        return []

    n = len(fragments)

    # Collect non-loop fragment indices; loops have no free endpoints.
    active = [i for i, f in enumerate(fragments) if not f.is_loop]
    if not active:
        log.info("  [aff] all fragments are loops; no edges possible")
        return []

    # Build endpoint array: index = 2*local_idx + ep_idx  →  fragment id + ep
    # Use original fragment IDs for dedup, local indices for array addressing.
    ep_coords = np.empty((2 * len(active), 2), dtype=np.float32)
    for li, fi in enumerate(active):
        ep_coords[2 * li]     = fragments[fi].endpoints[0]
        ep_coords[2 * li + 1] = fragments[fi].endpoints[1]

    tree = cKDTree(ep_coords)

    evaluated: set = set()   # (min_frag_id, max_frag_id) already processed
    edges: List[Edge] = []

    for li_a, fi_a in enumerate(active):
        fa = fragments[fi_a]

        for local_ep_a in range(2):
            ep_tree_idx_a = 2 * li_a + local_ep_a
            neighbors = tree.query_ball_point(ep_coords[ep_tree_idx_a], r_max)

            for ep_tree_idx_b in neighbors:
                li_b = ep_tree_idx_b // 2
                fi_b = active[li_b]

                if fi_b == fi_a:          # same fragment
                    continue

                pair_key = (min(fi_a, fi_b), max(fi_a, fi_b))
                if pair_key in evaluated:
                    continue
                evaluated.add(pair_key)

                fb = fragments[fi_b]
                ep_i, ep_j, gap, d_ab = _best_endpoint_pair(fa, fb)

                # gap is guaranteed ≤ r_max because the KDTree found the
                # triggering pair and best_gap ≤ triggering_gap ≤ r_max.
                # Guard retained for defensive correctness.
                if gap > r_max:
                    continue

                f_dist, f_tang, f_curv, f_period = _compute_affinity_terms(
                    fa, fb, ep_i, ep_j, gap, d_ab)
                aff = _affinity(f_dist, f_tang, f_curv, f_period)

                if aff >= edge_min_affinity:
                    edges.append(Edge(
                        frag_a=fi_a,  frag_b=fi_b,
                        ep_a=ep_i,    ep_b=ep_j,
                        gap=round(gap, 2),
                        f_dist=round(f_dist, 4),   f_tang=round(f_tang, 4),
                        f_curv=round(f_curv, 4),   f_period=round(f_period, 4),
                        affinity=round(aff, 4),
                    ))

    edges.sort(key=lambda e: -e.affinity)
    n_above_group = sum(1 for e in edges if e.affinity >= GROUPING_AFFINITY)
    log.info(f"  [aff] {len(edges)} edges stored "
             f"({n_above_group} above grouping threshold {GROUPING_AFFINITY})")
    return edges


# ═══════════════════════════════════════════════════════════════════
# Grouping
# ═══════════════════════════════════════════════════════════════════
def _cluster_bbox(frags_in: List[Fragment]) -> Tuple[int, int, int, int]:
    all_pts = np.vstack([f.points for f in frags_in])
    return (int(all_pts[:, 0].min()), int(all_pts[:, 1].min()),
            int(all_pts[:, 0].max()), int(all_pts[:, 1].max()))


def group_fragments(fragments: List[Fragment],
                    edges: List[Edge],
                    grouping_affinity: float = GROUPING_AFFINITY,
                    image_shape: Optional[Tuple[int, ...]] = None) -> List[Cluster]:
    """
    Group fragments into cloud instances via Union-Find on the thresholded
    affinity graph.

    Args:
        fragments:         Phase 1 output (same list passed to build_graph).
        edges:             Output of build_graph.
        grouping_affinity: Merge threshold.
        image_shape:       (H, W, ...) used for sanity-check aspect/area tests.

    Returns:
        List[Cluster] sorted by total_arc_length descending (likely clouds first).
    """
    n = len(fragments)
    if n == 0:
        return []

    uf = _UnionFind(n)
    for edge in edges:
        if edge.affinity >= grouping_affinity:
            uf.union(edge.frag_a, edge.frag_b)

    components = uf.components(n)
    clusters: List[Cluster] = []

    for cid, members in enumerate(sorted(components, key=len, reverse=True)):
        frags_in = [fragments[i] for i in members]

        total_len  = float(sum(f.arc_length for f in frags_in))
        periods    = [f.scallop_periodicity for f in frags_in]
        mean_per   = float(np.mean(periods))
        scal_frac  = float(np.mean([1.0 if p > 0.25 else 0.0 for p in periods]))
        bbox       = _cluster_bbox(frags_in)

        is_suspect = False
        if image_shape is not None:
            H, W = image_shape[:2]
            bw = max(bbox[2] - bbox[0], 1)
            bh = max(bbox[3] - bbox[1], 1)
            ar = max(bw, bh) / min(bw, bh)
            af = (bw * bh) / (W * H)
            if ar > MAX_CLUSTER_ASPECT or af > MAX_CLUSTER_AREA_F:
                is_suspect = True

        clusters.append(Cluster(
            id=cid,
            fragment_ids=sorted(members),
            total_arc_length=round(total_len, 1),
            mean_scallop_periodicity=round(mean_per, 3),
            scallop_fraction=round(scal_frac, 3),
            bbox=bbox,
            is_suspect=is_suspect,
        ))

    n_suspect = sum(1 for c in clusters if c.is_suspect)
    log.info(f"  [aff] {len(clusters)} clusters "
             f"(suspect={n_suspect}, "
             f"single-fragment={sum(1 for c in clusters if len(c.fragment_ids)==1)})")
    return clusters


# ═══════════════════════════════════════════════════════════════════
# Debug visualisation
# ═══════════════════════════════════════════════════════════════════
def _debug_scale(H: int, W: int, max_dim: int = DEBUG_MAX_DIM) -> float:
    return min(1.0, max_dim / max(H, W))


def _affinity_color(affinity: float, threshold: float) -> Tuple[int, int, int]:
    """BGR colour: green (high) → yellow → red (below threshold)."""
    if affinity >= threshold + 0.20:
        return (0, 200, 0)         # green: clearly merging
    elif affinity >= threshold:
        return (0, 200, 200)       # yellow: just above threshold
    elif affinity >= threshold - 0.10:
        return (0, 100, 220)       # orange: just below threshold
    else:
        return (80, 80, 80)        # dark grey: weak edge


def _distinct_color(idx: int) -> Tuple[int, int, int]:
    """Perceptually-spread BGR colour using golden-ratio hue rotation."""
    hue = int((idx * 137.508) % 180)           # OpenCV HSV hue range 0-179
    h = np.array([[[hue, 210, 210]]], dtype=np.uint8)
    bgr = cv2.cvtColor(h, cv2.COLOR_HSV2BGR)[0, 0]
    return (int(bgr[0]), int(bgr[1]), int(bgr[2]))


def save_debug(fragments: List[Fragment],
               edges: List[Edge],
               clusters: List[Cluster],
               image_shape: Tuple[int, ...],
               debug_dir: Path,
               source_image: Optional[np.ndarray] = None) -> None:
    """
    Write debug_affinity/ artefacts.

    graph_edges.png        — fragments dim grey; edges heat-mapped by affinity
    grouped_fragments.png  — each cluster its own distinct colour
    cluster_summary.json   — per-cluster stats + top-level summary
    """
    H, W = image_shape[:2]
    s = _debug_scale(H, W)
    dH, dW = int(H * s), int(W * s)
    debug_dir.mkdir(parents=True, exist_ok=True)

    # Background: dim source image or black canvas
    if source_image is not None:
        bg = cv2.resize(source_image, (dW, dH))
        bg = (bg * 0.20).astype(np.uint8)
    else:
        bg = np.zeros((dH, dW, 3), dtype=np.uint8)

    # ── graph_edges.png ────────────────────────────────────────────
    edge_img = bg.copy()

    # Draw all fragments faintly
    for f in fragments:
        if len(f.points) < 2:
            continue
        pts = (f.points * s).astype(np.int32)
        cv2.polylines(edge_img, [pts], f.is_loop, (50, 50, 50), 1)

    # Draw edges (sorted weakest-first so strong edges render on top)
    for e in sorted(edges, key=lambda x: x.affinity):
        fa, fb = fragments[e.frag_a], fragments[e.frag_b]
        p_a = (int(fa.endpoints[e.ep_a][0] * s), int(fa.endpoints[e.ep_a][1] * s))
        p_b = (int(fb.endpoints[e.ep_b][0] * s), int(fb.endpoints[e.ep_b][1] * s))
        color = _affinity_color(e.affinity, GROUPING_AFFINITY)
        cv2.line(edge_img, p_a, p_b, color, 1)

    cv2.imwrite(str(debug_dir / "graph_edges.png"), edge_img,
                [cv2.IMWRITE_PNG_COMPRESSION, 3])

    # ── grouped_fragments.png ──────────────────────────────────────
    grp_img = bg.copy()

    for cluster in clusters:
        color = _distinct_color(cluster.id)
        for fi in cluster.fragment_ids:
            f = fragments[fi]
            if len(f.points) < 2:
                continue
            pts = (f.points * s).astype(np.int32)
            thickness = 2 if len(cluster.fragment_ids) > 1 else 1
            cv2.polylines(grp_img, [pts], f.is_loop, color, thickness)
            # Mark endpoints of non-loop fragments
            if not f.is_loop:
                for ep in f.endpoints:
                    cv2.circle(grp_img, (int(ep[0]*s), int(ep[1]*s)), 2,
                               (255, 255, 255), -1)

    # Label the 20 largest clusters
    for cluster in clusters[:20]:
        x0, y0, x1, y1 = cluster.bbox
        cx, cy = int((x0+x1)/2*s), int((y0+y1)/2*s)
        color = _distinct_color(cluster.id)
        label = f"C{cluster.id}:{len(cluster.fragment_ids)}"
        cv2.putText(grp_img, label, (cx, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1, cv2.LINE_AA)

    cv2.imwrite(str(debug_dir / "grouped_fragments.png"), grp_img,
                [cv2.IMWRITE_PNG_COMPRESSION, 3])

    # ── cluster_summary.json ───────────────────────────────────────
    n_multi = sum(1 for c in clusters if len(c.fragment_ids) > 1)
    summary = {
        'parameters': {
            'R_MAX': R_MAX, 'SIGMA_D': SIGMA_D, 'SIGMA_C': SIGMA_C,
            'W_DIST': W_DIST, 'W_TANG': W_TANG,
            'W_CURV': W_CURV, 'W_PERIOD': W_PERIOD,
            'EDGE_MIN_AFFINITY': EDGE_MIN_AFFINITY,
            'GROUPING_AFFINITY': GROUPING_AFFINITY,
        },
        'totals': {
            'n_fragments': len(fragments),
            'n_fragments_non_loop': sum(1 for f in fragments if not f.is_loop),
            'n_edges_stored': len(edges),
            'n_edges_grouping': sum(1 for e in edges if e.affinity >= GROUPING_AFFINITY),
            'n_clusters': len(clusters),
            'n_clusters_multi_fragment': n_multi,
            'n_clusters_suspect': sum(1 for c in clusters if c.is_suspect),
        },
        'clusters': [c.to_dict() for c in clusters],
    }
    with open(debug_dir / "cluster_summary.json", 'w') as f:
        json.dump(summary, f, indent=2)

    log.info(f"  [aff] debug → {debug_dir}/ "
             f"(graph_edges, grouped_fragments, cluster_summary)")


# ═══════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════
def main() -> None:
    import argparse, time

    ap = argparse.ArgumentParser(
        description="Phase 2: affinity graph + fragment grouping")
    ap.add_argument("input",         help="P&ID image (binarized via Otsu internally)")
    ap.add_argument("--out",         default="debug_affinity", help="debug output dir")
    ap.add_argument("--r-max",       type=float, default=R_MAX,
                    help=f"KDTree search radius (default {R_MAX}px)")
    ap.add_argument("--edge-min",    type=float, default=EDGE_MIN_AFFINITY,
                    help=f"edge storage threshold (default {EDGE_MIN_AFFINITY})")
    ap.add_argument("--group-min",   type=float, default=GROUPING_AFFINITY,
                    help=f"grouping threshold (default {GROUPING_AFFINITY})")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    from pipeline.fragment_extractor import extract_fragments, _binarize

    img = cv2.imread(args.input)
    if img is None:
        print(f"Cannot read: {args.input}", file=sys.stderr); sys.exit(1)
    H, W = img.shape[:2]
    log.info(f"Image: {W}x{H}")

    t0 = time.time()
    log.info("Phase 1: extracting fragments...")
    frags = extract_fragments(_binarize(img))
    log.info(f"  {len(frags)} fragments in {time.time()-t0:.1f}s")

    t1 = time.time()
    log.info("Phase 2a: building affinity graph...")
    edges = build_graph(frags, r_max=args.r_max, edge_min_affinity=args.edge_min)
    log.info(f"  {len(edges)} edges in {time.time()-t1:.1f}s")

    t2 = time.time()
    log.info("Phase 2b: grouping fragments...")
    clusters = group_fragments(frags, edges,
                               grouping_affinity=args.group_min,
                               image_shape=(H, W))
    log.info(f"  {len(clusters)} clusters in {time.time()-t2:.2f}s")

    log.info("Saving debug outputs...")
    save_debug(frags, edges, clusters, (H, W), Path(args.out), img)

    # ── Summary ──────────────────────────────────────────────────
    multi = [c for c in clusters if len(c.fragment_ids) > 1]
    cloud_like = [c for c in multi
                  if c.scallop_fraction > 0.15 and not c.is_suspect]

    print("\n" + "═" * 64)
    print(" PHASE 2: AFFINITY GRAPH + FRAGMENT GROUPING")
    print("═" * 64)
    print(f"  fragments (total)      : {len(frags)}")
    print(f"  edges stored           : {len(edges)}")
    print(f"  edges above threshold  : {sum(1 for e in edges if e.affinity>=args.group_min)}")
    print(f"  clusters (total)       : {len(clusters)}")
    print(f"  clusters (multi-frag)  : {len(multi)}")
    print(f"  clusters (cloud-like)  : {len(cloud_like)}")
    print(f"  clusters (suspect)     : {sum(1 for c in clusters if c.is_suspect)}")
    print(f"  elapsed                : {time.time()-t0:.1f}s")
    print("─" * 64)
    print("  Top 15 clusters by arc length:")
    for c in clusters[:15]:
        flag = " [SUSPECT]" if c.is_suspect else ""
        print(f"    C{c.id:4d}: {len(c.fragment_ids):4d} frags  "
              f"arclen={c.total_arc_length:7.0f}  "
              f"period={c.mean_scallop_periodicity:.2f}  "
              f"scal%={c.scallop_fraction*100:.0f}%  "
              f"bbox={c.bbox}{flag}")
    print("─" * 64)
    print(f"  debug_affinity/ → {args.out}/")
    print("═" * 64)


if __name__ == "__main__":
    main()
