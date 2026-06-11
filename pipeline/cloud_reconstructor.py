"""
Phase 3: Cloud Reconstruction from Fragment Clusters
=====================================================
Consumes Phase 1 fragments, Phase 2 clusters/edges, and produces validated
ReconstructedCloud instances.

WHY THIS IS NEEDED
------------------
Phase 2 (affinity grouper) uses a tangent-product formula that correctly
REJECTS continuations across scallop cusps (sign-reversing arcs). This
means one revision cloud boundary yields many Phase-2 clusters rather than
one. Phase 3 bridges those clusters using proximity alone (no f_tang filter)
and validates the merged boundary as a closed ring.

ALGORITHM (v2 — two-tier proximity)
------------------------------------
Phase 2 proximity at R=150px (no affinity filter) caused runaway transitive
closure: pipe/text clusters chained through cloud clusters producing
super-clusters covering most of the drawing. Ring closure then fails because
the traversal visits hundreds of unrelated fragments.

Fix: two-tier proximity.

1. Cloud-candidate cluster classification
   Clusters with scallop_fraction > 0 OR mean_scallop_periodicity > 0 are
   "cloud-candidate." These participate in the primary proximity graph.

2. Primary super-cluster formation (cloud-candidate clusters only)
   KDTree on terminal endpoints of cloud-candidate clusters. Search radius
   CLOUD_R3_MAX=250px (wider than R3_MAX=150px to bridge inter-cloud gaps
   where scallop detection was missed). No affinity scoring.

3. Expansion pass
   For each primary super-cluster, add nearby non-scallop clusters whose
   terminal endpoints are within EXPAND_R3_MAX=150px of any core terminal,
   as long as they have fewer than PIPE_FRAG_THRESH=3 fragments. This adds
   cloud boundary arcs that passed Phase 1 without scallop classification.

4. Ordered fragment traversal + ring closure
   Walk all included fragments in nearest-neighbor endpoint order.
   Gaps are bridged with linear interpolation.
   Ring closes if the last exit endpoint is within EXPAND_R3_MAX of start.

5. Cloud polygon validation
   Re-applies stage1_cloud._validate_cloud_shape (scallopedness >= 1.30,
   area bounds, aspect ratio, exclusion zones).
   Additional gate: gap_fraction < MAX_GAP_FRACTION=0.55.
"""
import json
import logging
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import cv2
import numpy as np
from scipy.spatial import cKDTree

from pipeline.fragment_extractor import Fragment
from pipeline.affinity_grouper import Cluster, Edge, GROUPING_AFFINITY

log = logging.getLogger(__name__)

# ── Phase 3 parameters ────────────────────────────────────────────────────────
CLOUD_R3_MAX      = 250.0  # px: proximity radius for cloud-suspect cluster graph
EXPAND_R3_MAX     = 150.0  # px: expansion radius for adding bridge fragments
MAX_GAP_FRACTION  = 0.65   # reject if >65% of perimeter is interpolated bridges
PIPE_FRAG_THRESH  = 3      # non-scallop clusters with >=N fragments → excluded as pipes
MIN_SCALLOP_FRAC  = 0.08   # super-cluster must have >=8% scallop fragments
MIN_TOTAL_ARCLEN  = 250.0  # px: minimum arc length for a valid cloud boundary
MAX_FRAGS         = 120    # super-clusters with more fragments are over-merged
MAX_BBOX_DIM      = 3000   # px: super-clusters with bbox wider/taller are over-merged
CLOSURE_R         = 400.0  # px: ring closure test radius (wider than EXPAND_R3_MAX)
MAX_JUMP          = 500.0  # px: traversal stops if nearest unvisited frag > this far
DEBUG_MAX_DIM     = 4000   # px: longest side of debug images


# ═══════════════════════════════════════════════════════════════════
# Output data structure
# ═══════════════════════════════════════════════════════════════════
@dataclass
class ReconstructedCloud:
    id: int
    cluster_ids: List[int]
    fragment_ids: List[int]
    polygon: np.ndarray              # Nx2 (x,y), ordered, closed
    area: float
    scallop_fraction: float
    total_arc_length: float
    gap_count: int
    gap_fraction: float
    bbox: Tuple[int, int, int, int]  # x0,y0,x1,y1
    is_valid: bool
    reject_reason: str
    used_tier: str = "all"   # "core" or "all" — which fragment set was used

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'cluster_ids': self.cluster_ids,
            'n_fragments': len(self.fragment_ids),
            'fragment_ids': self.fragment_ids,
            'n_polygon_points': int(len(self.polygon)),
            'area': round(float(self.area), 1),
            'scallop_fraction': round(float(self.scallop_fraction), 3),
            'total_arc_length': round(float(self.total_arc_length), 1),
            'gap_count': self.gap_count,
            'gap_fraction': round(float(self.gap_fraction), 3),
            'bbox': list(self.bbox),
            'is_valid': self.is_valid,
            'reject_reason': self.reject_reason,
            'used_tier': self.used_tier,
        }


# ═══════════════════════════════════════════════════════════════════
# Union-Find (path-halving, union by rank)
# ═══════════════════════════════════════════════════════════════════
class _UnionFind:
    def __init__(self, n: int):
        self._parent = list(range(n))
        self._rank   = [0] * n

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, x: int, y: int) -> bool:
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
        groups: dict = {}
        for i in range(n):
            r = self.find(i)
            groups.setdefault(r, []).append(i)
        return list(groups.values())


# ═══════════════════════════════════════════════════════════════════
# Step 1: Terminal endpoint extraction
# ═══════════════════════════════════════════════════════════════════
def _find_terminal_endpoints(
    fragments: List[Fragment],
    clusters: List[Cluster],
    edges: List[Edge],
) -> Dict[int, List[Tuple[int, int, np.ndarray]]]:
    """
    For each cluster, find fragment endpoints NOT bridged by an intra-cluster
    Phase-2 edge with affinity >= GROUPING_AFFINITY.

    Returns: cluster_id → list of (fragment_id, ep_idx, xy)
    """
    frag_to_cluster: Dict[int, int] = {}
    for c in clusters:
        for fi in c.fragment_ids:
            frag_to_cluster[fi] = c.id

    bridged: Set[Tuple[int, int]] = set()
    for e in edges:
        if e.affinity < GROUPING_AFFINITY:
            continue
        ca = frag_to_cluster.get(e.frag_a)
        cb = frag_to_cluster.get(e.frag_b)
        if ca is not None and cb is not None and ca == cb:
            bridged.add((e.frag_a, e.ep_a))
            bridged.add((e.frag_b, e.ep_b))

    terminals: Dict[int, List[Tuple[int, int, np.ndarray]]] = {
        c.id: [] for c in clusters
    }
    for c in clusters:
        for fi in c.fragment_ids:
            f = fragments[fi]
            if f.is_loop:
                continue
            for ep_idx in range(2):
                if (fi, ep_idx) not in bridged:
                    terminals[c.id].append((fi, ep_idx, f.endpoints[ep_idx].copy()))

    return terminals


# ═══════════════════════════════════════════════════════════════════
# Step 2: Primary super-cluster formation (cloud-candidate only)
# ═══════════════════════════════════════════════════════════════════
def _build_primary_super_clusters(
    cloud_cands: List[Cluster],
    terminals: Dict[int, List[Tuple[int, int, np.ndarray]]],
    r_max: float,
) -> List[List[int]]:
    """
    Build super-clusters from cloud-candidate clusters using terminal endpoint
    proximity. Returns list of lists of cluster_ids.
    """
    if not cloud_cands:
        return []

    all_pts:  List[np.ndarray] = []
    all_cids: List[int]        = []

    for c in cloud_cands:
        for (fi, ep_idx, xy) in terminals.get(c.id, []):
            all_pts.append(xy.astype(np.float32))
            all_cids.append(c.id)

    if not all_pts:
        return [[c.id] for c in cloud_cands]

    pts_arr = np.array(all_pts, dtype=np.float32)
    tree = cKDTree(pts_arr)

    cid_to_local = {c.id: li for li, c in enumerate(cloud_cands)}
    n = len(cloud_cands)
    uf = _UnionFind(n)

    for (i, j) in tree.query_pairs(r_max):
        ci, cj = all_cids[i], all_cids[j]
        if ci != cj:
            uf.union(cid_to_local[ci], cid_to_local[cj])

    comps = uf.components(n)
    return [[cloud_cands[li].id for li in comp] for comp in comps]


# ═══════════════════════════════════════════════════════════════════
# Step 3: Expansion pass (add bridge fragments)
# ═══════════════════════════════════════════════════════════════════
def _expand_super_cluster(
    core_cids: List[int],
    all_clusters: List[Cluster],
    cloud_cand_ids: Set[int],
    terminals: Dict[int, List[Tuple[int, int, np.ndarray]]],
    expand_r: float,
    pipe_frag_thresh: int,
) -> List[int]:
    """
    Add non-scallop, non-pipe clusters whose terminals are within expand_r
    of any terminal of the core super-cluster.

    Excluded: cloud-candidate clusters (already in primary graph)
              clusters with >= pipe_frag_thresh fragments and 0 scallop (pipes)
    """
    # Gather core terminal coordinates
    core_pts = []
    for cid in core_cids:
        for (fi, ep_idx, xy) in terminals.get(cid, []):
            core_pts.append(xy.astype(np.float32))

    if not core_pts:
        return list(core_cids)

    core_tree = cKDTree(np.array(core_pts, dtype=np.float32))
    core_set = set(core_cids)
    expansion: List[int] = []

    for c in all_clusters:
        if c.id in cloud_cand_ids or c.id in core_set:
            continue
        if len(c.fragment_ids) >= pipe_frag_thresh:
            continue  # likely pipe or text structure
        for (fi, ep_idx, xy) in terminals.get(c.id, []):
            d, _ = core_tree.query(xy.astype(np.float32))
            if d <= expand_r:
                expansion.append(c.id)
                break

    return list(core_cids) + expansion


# ═══════════════════════════════════════════════════════════════════
# Step 4: Ordered fragment traversal + ring closure
# ═══════════════════════════════════════════════════════════════════
def _single_traversal(
    pool: List[dict],
    frag_map: Dict[int, Fragment],
    active_ids: List[int],
    start_pi: int,
    max_jump: float,
) -> Tuple[List[list], float, float, int]:
    """
    Greedy nearest-neighbor walk from one starting endpoint.

    Stops early if the nearest unvisited fragment is > max_jump away.
    Returns (polygon_pts, gap_arc, total_arc, gap_count).
    """
    # Reset pool used flags for this run
    for p in pool:
        p['used'] = False

    visited_frags: Set[int] = set()
    polygon_pts: List[list] = []
    gap_arc = 0.0
    total_arc = 0.0
    gap_count = 0

    pool[start_pi]['used'] = True
    cur_fi = pool[start_pi]['fi']
    cur_ep = pool[start_pi]['ep']
    visited_frags.add(cur_fi)

    f0 = frag_map[cur_fi]
    pts0 = (f0.points.astype(np.float64) if cur_ep == 0
            else f0.points[::-1].astype(np.float64))
    polygon_pts.extend(pts0.tolist())
    total_arc += f0.arc_length

    exit_ep = 1 - cur_ep
    cur_xy = f0.endpoints[exit_ep].astype(np.float64)
    for p in pool:
        if p['fi'] == cur_fi and p['ep'] == exit_ep:
            p['used'] = True
            break

    while len(visited_frags) < len(active_ids):
        best_dist = float('inf')
        best_pi = -1

        for pi, p in enumerate(pool):
            if p['used'] or p['fi'] in visited_frags:
                continue
            d = float(np.hypot(p['xy'][0] - cur_xy[0], p['xy'][1] - cur_xy[1]))
            if d < best_dist:
                best_dist = d
                best_pi = pi

        if best_pi == -1 or best_dist > max_jump:
            break  # no reachable fragment or gap too large → stop

        next_p = pool[best_pi]
        gap_end = next_p['xy'].copy()
        gap_dist = float(np.hypot(gap_end[0] - cur_xy[0], gap_end[1] - cur_xy[1]))
        if gap_dist > 2.0:
            n_i = max(2, int(gap_dist / 5))
            for t in np.linspace(0.0, 1.0, n_i)[1:]:
                polygon_pts.append((cur_xy + t * (gap_end - cur_xy)).tolist())
            gap_arc  += gap_dist
            gap_count += 1
            total_arc += gap_dist

        next_fi = next_p['fi']
        next_ep = next_p['ep']
        visited_frags.add(next_fi)
        next_p['used'] = True

        nf = frag_map[next_fi]
        nf_pts = (nf.points.astype(np.float64) if next_ep == 0
                  else nf.points[::-1].astype(np.float64))
        polygon_pts.extend(nf_pts.tolist())
        total_arc += nf.arc_length

        exit_ep2 = 1 - next_ep
        cur_xy = nf.endpoints[exit_ep2].astype(np.float64)
        for p in pool:
            if p['fi'] == next_fi and p['ep'] == exit_ep2:
                p['used'] = True
                break

    return polygon_pts, gap_arc, total_arc, gap_count


def _angle_sorted_traversal(
    frag_map: Dict[int, Fragment],
    active_ids: List[int],
    closure_r: float,
) -> Tuple[List[list], float, float, int, float]:
    """
    Sort fragments by their angular position around the centroid of all
    endpoints, then walk them in order. This gives near-correct ring order
    for convex clouds without the O(N²) instability of greedy nearest-neighbor.

    Returns: (polygon_pts, gap_arc, total_arc, gap_count, closure_gap)
    closure_gap == inf if the ring doesn't close within closure_r.
    """
    if not active_ids:
        return [], 0.0, 0.0, 0, float('inf')

    # Compute centroid of all fragment endpoints
    all_eps: List[np.ndarray] = []
    for fi in active_ids:
        f = frag_map[fi]
        all_eps.append(f.endpoints[0].astype(np.float64))
        all_eps.append(f.endpoints[1].astype(np.float64))
    centroid = np.mean(np.array(all_eps), axis=0)

    # Sort fragments by angle of their midpoint from centroid
    frag_angles: List[Tuple[float, int]] = []
    for fi in active_ids:
        f = frag_map[fi]
        mid = (f.endpoints[0].astype(np.float64) + f.endpoints[1].astype(np.float64)) * 0.5
        angle = float(np.arctan2(mid[1] - centroid[1], mid[0] - centroid[0]))
        frag_angles.append((angle, fi))
    frag_angles.sort()
    sorted_fids = [fi for _, fi in frag_angles]

    polygon_pts: List[list] = []
    gap_arc = 0.0
    total_arc = 0.0
    gap_count = 0
    cur_xy: Optional[np.ndarray] = None

    for idx, fi in enumerate(sorted_fids):
        f = frag_map[fi]
        ep0 = f.endpoints[0].astype(np.float64)
        ep1 = f.endpoints[1].astype(np.float64)

        # Choose entry endpoint: whichever is closer to cur_xy
        if cur_xy is None:
            # First fragment: choose entry based on which endpoint is farther
            # from the NEXT fragment (so exit faces the ring)
            if len(sorted_fids) > 1:
                next_f = frag_map[sorted_fids[1]]
                nep = (next_f.endpoints[0].astype(np.float64) +
                       next_f.endpoints[1].astype(np.float64)) * 0.5
                d0 = float(np.hypot(ep0[0]-nep[0], ep0[1]-nep[1]))
                d1 = float(np.hypot(ep1[0]-nep[0], ep1[1]-nep[1]))
                # entry = farther from next (exit = closer to next)
                if d0 > d1:
                    entry_ep, exit_ep = 0, 1
                else:
                    entry_ep, exit_ep = 1, 0
            else:
                entry_ep, exit_ep = 0, 1
        else:
            # Choose entry = endpoint closer to cur_xy
            d0 = float(np.hypot(ep0[0]-cur_xy[0], ep0[1]-cur_xy[1]))
            d1 = float(np.hypot(ep1[0]-cur_xy[0], ep1[1]-cur_xy[1]))
            if d0 <= d1:
                entry_ep, exit_ep = 0, 1
            else:
                entry_ep, exit_ep = 1, 0

        entry_xy = (ep0 if entry_ep == 0 else ep1)
        exit_xy  = (ep1 if entry_ep == 0 else ep0)

        # Bridge gap from cur_xy to entry_xy
        if cur_xy is not None:
            gap_dist = float(np.hypot(entry_xy[0]-cur_xy[0], entry_xy[1]-cur_xy[1]))
            if gap_dist > 2.0:
                n_i = max(2, int(gap_dist / 5))
                for t in np.linspace(0.0, 1.0, n_i)[1:]:
                    polygon_pts.append((cur_xy + t * (entry_xy - cur_xy)).tolist())
                gap_arc  += gap_dist
                gap_count += 1
                total_arc += gap_dist

        # Walk the fragment
        pts = (f.points.astype(np.float64) if entry_ep == 0
               else f.points[::-1].astype(np.float64))
        polygon_pts.extend(pts.tolist())
        total_arc += f.arc_length
        cur_xy = exit_xy.copy()

    if len(polygon_pts) < 3 or cur_xy is None:
        return polygon_pts, gap_arc, total_arc, gap_count, float('inf')

    # Ring closure test
    first_pt = np.array(polygon_pts[0], dtype=np.float64)
    closure_gap = float(np.hypot(cur_xy[0]-first_pt[0], cur_xy[1]-first_pt[1]))

    if closure_gap <= closure_r and closure_gap > 2.0:
        n_i = max(2, int(closure_gap / 5))
        for t in np.linspace(0.0, 1.0, n_i)[1:-1]:
            polygon_pts.append((cur_xy + t * (first_pt - cur_xy)).tolist())
        gap_arc  += closure_gap
        gap_count += 1
        total_arc += closure_gap

    return polygon_pts, gap_arc, total_arc, gap_count, closure_gap


def _traversal_polygon(
    fragments: List[Fragment],
    fragment_ids: List[int],
    closure_r: float,
    max_jump: float,
) -> Tuple[np.ndarray, int, float]:
    """
    Best-of: angle-sorted ring traversal (convex-cloud-optimal) plus
    multi-start greedy (handles non-convex cases). Takes the result with
    the smallest ring-closure gap.

    Returns: (polygon Nx2, gap_count, gap_fraction)
    gap_fraction == 1.0 signals open-chain (ring closure failed).
    """
    frag_map = {f.id: f for f in fragments}
    active_ids = [fi for fi in fragment_ids
                  if fi in frag_map and not frag_map[fi].is_loop]

    if not active_ids:
        return np.zeros((0, 2), dtype=np.float32), 0, 1.0

    # Candidate results: list of (gap_fraction, closure_gap, poly_pts, gap_arc, total_arc, gap_count)
    # gap_fraction == inf if ring didn't close (closure_gap > closure_r)
    candidates = []

    # ── Strategy 1: angle-sorted (closes ring from both CW and CCW orderings) ─
    for reverse in (False, True):
        ang_pts, ang_gap_arc, ang_total_arc, ang_gap_count, ang_cg = \
            _angle_sorted_traversal(
                frag_map,
                active_ids if not reverse else list(reversed(active_ids)),
                closure_r)
        if len(ang_pts) >= 3:
            if ang_cg <= closure_r and ang_total_arc > 0:
                gf = ang_gap_arc / ang_total_arc
            else:
                gf = float('inf')
            candidates.append((gf, ang_cg, ang_pts, ang_gap_arc, ang_total_arc, ang_gap_count))

    # ── Strategy 2: multi-start greedy ────────────────────────────────────────
    pool: List[dict] = []
    for fi in active_ids:
        f = frag_map[fi]
        pool.append({'fi': fi, 'ep': 0, 'xy': f.endpoints[0].astype(np.float64), 'used': False})
        pool.append({'fi': fi, 'ep': 1, 'xy': f.endpoints[1].astype(np.float64), 'used': False})

    if pool:
        pts_arr = np.array([p['xy'] for p in pool], dtype=np.float64)
        tree = cKDTree(pts_arr)
        k = min(3, len(pts_arr))
        dists, _ = tree.query(pts_arr, k=k)
        nn_dist = (dists[:, 1] if (len(dists.shape) > 1 and dists.shape[1] > 1)
                   else dists[:, 0])

        MAX_STARTS = min(20, len(pool))
        ranked = sorted(range(len(pool)), key=lambda i: -float(nn_dist[i]))

        best_greedy_cg = float('inf')
        for start_pi in ranked[:MAX_STARTS]:
            poly_pts, g_gap_arc, g_total_arc, g_gap_count = _single_traversal(
                pool, frag_map, active_ids, start_pi, max_jump)
            if len(poly_pts) < 3:
                continue

            first = np.array(poly_pts[0],  dtype=np.float64)
            last  = np.array(poly_pts[-1], dtype=np.float64)
            cg = float(np.hypot(last[0]-first[0], last[1]-first[1]))

            if cg < best_greedy_cg:
                best_greedy_cg = cg
                # Add closure bridge to this candidate
                pts_with_bridge = list(poly_pts)
                ba = g_gap_arc; ta = g_total_arc; bc = g_gap_count
                if cg <= closure_r and cg > 2.0:
                    n_i = max(2, int(cg / 5))
                    for t in np.linspace(0.0, 1.0, n_i)[1:-1]:
                        pts_with_bridge.append((last + t * (first - last)).tolist())
                    ba += cg; ta += cg; bc += 1
                gf = ba / ta if ta > 0 else float('inf')
                if cg > closure_r:
                    gf = float('inf')
                candidates.append((gf, cg, pts_with_bridge, ba, ta, bc))

            if cg <= closure_r:
                break

    if not candidates:
        return np.zeros((0, 2), dtype=np.float32), 0, 1.0

    # Choose best: among those that close (gf < inf), pick lowest gf.
    # If none close, pick minimum closure_gap for diagnostics.
    closed = [(gf, cg, pts, ga, ta, gc) for (gf, cg, pts, ga, ta, gc) in candidates
              if gf < float('inf') and len(pts) >= 3]

    if not closed:
        # Return the candidate with minimum closure gap (open chain)
        best = min(candidates, key=lambda x: x[1])
        poly = np.array(best[2], dtype=np.float32) if len(best[2]) >= 3 else np.zeros((0, 2), dtype=np.float32)
        return poly, best[5], 1.0

    best = min(closed, key=lambda x: x[0])  # minimum gap_fraction
    poly = np.array(best[2], dtype=np.float32)
    return poly, best[5], best[0]


# ═══════════════════════════════════════════════════════════════════
# Step 5: Cloud polygon validation
# ═══════════════════════════════════════════════════════════════════
def _validate_polygon(
    poly: np.ndarray,
    image_shape: Tuple[int, int],
    gap_fraction: float,
) -> Tuple[bool, str]:
    """Validate using stage1_cloud logic + gap fraction gate."""
    if len(poly) < 6:
        return False, f"too few points ({len(poly)})"
    if gap_fraction >= MAX_GAP_FRACTION:
        return False, f"gap_fraction {gap_fraction:.2f}>={MAX_GAP_FRACTION:.2f}"

    try:
        from pipeline.stage1_cloud import _validate_cloud_shape
        return _validate_cloud_shape(poly.astype(np.int32), image_shape)
    except Exception:
        cnt = poly.reshape(-1, 1, 2).astype(np.int32)
        peri = cv2.arcLength(cnt, True)
        hull = cv2.convexHull(cnt)
        hull_peri = cv2.arcLength(hull, True)
        if hull_peri < 50:
            return False, "hull too small"
        scallop = peri / hull_peri if hull_peri > 0 else 0.0
        if scallop < 1.30:
            return False, f"scallopedness {scallop:.2f}<1.30"
        area = cv2.contourArea(cnt)
        H, W = image_shape
        if area < 500:
            return False, "area too small"
        if area > W * H * 0.30:
            return False, "area >30%"
        return True, f"OK (scallop={scallop:.2f})"


# ═══════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════
def reconstruct_clouds(
    fragments: List[Fragment],
    clusters: List[Cluster],
    edges: List[Edge],
    image_shape: Tuple[int, int],
    cloud_r3_max: float  = CLOUD_R3_MAX,
    expand_r3_max: float = EXPAND_R3_MAX,
    closure_r: float     = CLOSURE_R,
    max_jump: float      = MAX_JUMP,
    debug_dir: Optional[Path] = None,
) -> List['ReconstructedCloud']:
    """
    Phase 3: Reconstruct and validate revision clouds.

    Args:
        fragments:    Phase 1 output.
        clusters:     Phase 2 output.
        edges:        Phase 2 graph edges.
        image_shape:  (H, W).
        cloud_r3_max: Proximity radius for cloud-suspect cluster graph.
        expand_r3_max: Expansion radius for adding bridge fragments.
        debug_dir:    If given, writes debug images and summary JSON.

    Returns:
        List[ReconstructedCloud] sorted by area descending.
    """
    H, W = image_shape

    if not fragments or not clusters:
        log.warning("  [rec] No fragments or clusters — returning empty.")
        return []

    log.info(f"  [rec] Reconstructing from {len(clusters)} clusters, "
             f"{len(fragments)} fragments...")

    frag_map = {f.id: f for f in fragments}
    cid_map  = {c.id: c for c in clusters}

    # ── Step 1: terminal endpoints ─────────────────────────────────
    terminals = _find_terminal_endpoints(fragments, clusters, edges)
    n_term = sum(len(v) for v in terminals.values())
    log.info(f"  [rec] {n_term} terminal endpoints")

    # ── Step 2: classify cloud-candidate clusters ──────────────────
    cloud_cands = [c for c in clusters
                   if c.scallop_fraction > 0 or c.mean_scallop_periodicity > 0]
    cloud_cand_ids = {c.id for c in cloud_cands}
    log.info(f"  [rec] {len(cloud_cands)} cloud-candidate clusters "
             f"(out of {len(clusters)} total)")

    # ── Step 3: primary super-clusters (cloud-candidates, wider R) ─
    primary_scs = _build_primary_super_clusters(cloud_cands, terminals, cloud_r3_max)
    log.info(f"  [rec] {len(primary_scs)} primary super-clusters "
             f"(cloud_r3_max={cloud_r3_max}px)")

    # ── Step 4: expand each super-cluster with bridge fragments ────
    # Store (core_cids, all_cids) tuples for two-tier traversal
    expanded_scs = []
    for sc_cids in primary_scs:
        exp = _expand_super_cluster(
            sc_cids, clusters, cloud_cand_ids, terminals,
            expand_r3_max, PIPE_FRAG_THRESH)
        expanded_scs.append((list(sc_cids), exp))

    # ── Step 5: traversal + validation per super-cluster ──────────
    clouds: List[ReconstructedCloud] = []
    cloud_id = 0

    for core_cids, sc_cids in expanded_scs:
        sc_clusters = [cid_map[cid] for cid in sc_cids if cid in cid_map]
        if not sc_clusters:
            continue

        all_fids: List[int] = []
        for c in sc_clusters:
            all_fids.extend(c.fragment_ids)
        all_fids = list(dict.fromkeys(all_fids))

        # Separate core fragment IDs (cloud-candidate cluster members only)
        core_set = set(core_cids)
        core_fids: List[int] = []
        for c in sc_clusters:
            if c.id in core_set:
                core_fids.extend(c.fragment_ids)
        core_fids = list(dict.fromkeys(core_fids))

        total_arc = sum(frag_map[fi].arc_length
                        for fi in all_fids if fi in frag_map)
        scal_frac = (float(sum(
            1.0 if frag_map[fi].scallop_periodicity > 0.25 else 0.0
            for fi in all_fids if fi in frag_map
        )) / len(all_fids)) if all_fids else 0.0

        # Pre-filter: too short
        if total_arc < MIN_TOTAL_ARCLEN:
            clouds.append(ReconstructedCloud(
                id=cloud_id, cluster_ids=list(sc_cids), fragment_ids=all_fids,
                polygon=np.zeros((0, 2), dtype=np.float32),
                area=0.0, scallop_fraction=scal_frac,
                total_arc_length=total_arc, gap_count=0, gap_fraction=1.0,
                bbox=(0, 0, 0, 0), is_valid=False,
                reject_reason=f"arc_length {total_arc:.0f}<{MIN_TOTAL_ARCLEN:.0f}",
            ))
            cloud_id += 1
            continue

        # Pre-filter: too many fragments → over-merged super-cluster
        if len(all_fids) > MAX_FRAGS:
            clouds.append(ReconstructedCloud(
                id=cloud_id, cluster_ids=list(sc_cids), fragment_ids=all_fids,
                polygon=np.zeros((0, 2), dtype=np.float32),
                area=0.0, scallop_fraction=scal_frac,
                total_arc_length=total_arc, gap_count=0, gap_fraction=1.0,
                bbox=(0, 0, 0, 0), is_valid=False,
                reject_reason=f"over-merged: {len(all_fids)} frags > {MAX_FRAGS}",
            ))
            cloud_id += 1
            continue

        # Pre-filter: bbox too large → over-merged
        all_pts = np.vstack([frag_map[fi].points for fi in all_fids if fi in frag_map])
        bx0 = int(all_pts[:, 0].min()); bx1 = int(all_pts[:, 0].max())
        by0 = int(all_pts[:, 1].min()); by1 = int(all_pts[:, 1].max())
        if max(bx1 - bx0, by1 - by0) > MAX_BBOX_DIM:
            clouds.append(ReconstructedCloud(
                id=cloud_id, cluster_ids=list(sc_cids), fragment_ids=all_fids,
                polygon=np.zeros((0, 2), dtype=np.float32),
                area=0.0, scallop_fraction=scal_frac,
                total_arc_length=total_arc, gap_count=0, gap_fraction=1.0,
                bbox=(bx0, by0, bx1, by1), is_valid=False,
                reject_reason=f"over-merged: bbox {max(bx1-bx0, by1-by0)}px > {MAX_BBOX_DIM}px",
            ))
            cloud_id += 1
            continue

        # Pre-filter: no cloud signal
        if scal_frac < MIN_SCALLOP_FRAC:
            clouds.append(ReconstructedCloud(
                id=cloud_id, cluster_ids=list(sc_cids), fragment_ids=all_fids,
                polygon=np.zeros((0, 2), dtype=np.float32),
                area=0.0, scallop_fraction=scal_frac,
                total_arc_length=total_arc, gap_count=0, gap_fraction=1.0,
                bbox=(0, 0, 0, 0), is_valid=False,
                reject_reason=f"scallop_frac {scal_frac:.2f}<{MIN_SCALLOP_FRAC:.2f}",
            ))
            cloud_id += 1
            continue

        # Two-tier traversal: core-only first, fall back to all fragments
        # Tier 1: cloud-candidate fragments only (no expansion)
        if len(core_fids) >= 2:
            poly_core, gc_core, gf_core = _traversal_polygon(
                fragments, core_fids, closure_r=closure_r, max_jump=max_jump)
        else:
            poly_core, gc_core, gf_core = np.zeros((0, 2), dtype=np.float32), 0, 1.0

        # Tier 2: core + expansion fragments
        poly_all, gc_all, gf_all = _traversal_polygon(
            fragments, all_fids, closure_r=closure_r, max_jump=max_jump)

        # Pick best: prefer core-only when it closes (avoids interior pipe contamination);
        # use all-fragments only when core can't close or all gives strictly better gf.
        if gf_core < 1.0 and (gf_all >= 1.0 or gf_core <= gf_all):
            poly, gap_count, gap_fraction, used_tier = poly_core, gc_core, gf_core, "core"
        else:
            poly, gap_count, gap_fraction, used_tier = poly_all, gc_all, gf_all, "all"

        if len(poly) < 3:
            clouds.append(ReconstructedCloud(
                id=cloud_id, cluster_ids=list(sc_cids), fragment_ids=all_fids,
                polygon=poly, area=0.0, scallop_fraction=scal_frac,
                total_arc_length=total_arc, gap_count=gap_count,
                gap_fraction=gap_fraction, bbox=(0, 0, 0, 0),
                is_valid=False, reject_reason="traversal produced <3 points",
                used_tier=used_tier,
            ))
            cloud_id += 1
            continue

        x0 = int(poly[:, 0].min()); y0 = int(poly[:, 1].min())
        x1 = int(poly[:, 0].max()); y1 = int(poly[:, 1].max())
        bbox = (x0, y0, x1, y1)
        cnt  = poly.reshape(-1, 1, 2).astype(np.int32)
        area = float(cv2.contourArea(cnt))

        # Ring closure sentinel
        if gap_fraction >= 1.0:
            is_valid, reason = False, "open chain (ring closure failed)"
        else:
            is_valid, reason = _validate_polygon(poly, (H, W), gap_fraction)

        clouds.append(ReconstructedCloud(
            id=cloud_id, cluster_ids=list(sc_cids), fragment_ids=all_fids,
            polygon=poly, area=area, scallop_fraction=scal_frac,
            total_arc_length=total_arc, gap_count=gap_count,
            gap_fraction=gap_fraction, bbox=bbox,
            is_valid=is_valid, reject_reason=reason,
            used_tier=used_tier,
        ))
        cloud_id += 1

    # Sort by area descending; re-assign IDs
    clouds.sort(key=lambda c: -c.area)
    for i, c in enumerate(clouds):
        c.id = i

    n_valid = sum(1 for c in clouds if c.is_valid)
    mean_gf = (float(sum(c.gap_fraction for c in clouds if c.is_valid)) / n_valid
               if n_valid else 0.0)
    log.info(f"  [rec] {len(clouds)} reconstructed ({n_valid} valid, "
             f"mean gap_frac={mean_gf:.3f})")

    if debug_dir is not None:
        _save_debug(fragments, clusters, clouds,
                    [all_cids for _, all_cids in expanded_scs],
                    image_shape, debug_dir)

    return clouds


# ═══════════════════════════════════════════════════════════════════
# Debug visualisations
# ═══════════════════════════════════════════════════════════════════
def _distinct_color(idx: int) -> Tuple[int, int, int]:
    hue = int((idx * 137.508) % 180)
    h = np.array([[[hue, 200, 220]]], dtype=np.uint8)
    bgr = cv2.cvtColor(h, cv2.COLOR_HSV2BGR)[0, 0]
    return (int(bgr[0]), int(bgr[1]), int(bgr[2]))


def _debug_scale(H: int, W: int, max_dim: int = DEBUG_MAX_DIM) -> float:
    return min(1.0, max_dim / max(H, W))


def _save_debug(
    fragments: List[Fragment],
    clusters: List[Cluster],
    clouds: List[ReconstructedCloud],
    super_clusters: List[List[int]],
    image_shape: Tuple[int, int],
    debug_dir: Path,
) -> None:
    H, W = image_shape
    s = _debug_scale(H, W)
    dH, dW = int(H * s), int(W * s)
    debug_dir.mkdir(parents=True, exist_ok=True)

    frag_map = {f.id: f for f in fragments}
    cid_map  = {c.id: c for c in clusters}

    # Map cluster_id → super-cluster index
    cid_to_sc: Dict[int, int] = {}
    for sc_idx, sc_cids in enumerate(super_clusters):
        for cid in sc_cids:
            cid_to_sc[cid] = sc_idx

    # ── 1. super_clusters.png ──────────────────────────────────────
    sc_img = np.zeros((dH, dW, 3), dtype=np.uint8)
    for c in clusters:
        sc_idx = cid_to_sc.get(c.id, c.id)
        color  = _distinct_color(sc_idx)
        for fi in c.fragment_ids:
            f = frag_map.get(fi)
            if f is None or len(f.points) < 2:
                continue
            cv2.polylines(sc_img, [(f.points * s).astype(np.int32)],
                          f.is_loop, color, 1)
    for sc_idx, sc_cids in enumerate(super_clusters):
        if len(sc_cids) < 2:
            continue
        all_pts = []
        for cid in sc_cids:
            c = cid_map.get(cid)
            if c:
                for fi in c.fragment_ids:
                    f = frag_map.get(fi)
                    if f is not None:
                        all_pts.append(f.points)
        if all_pts:
            stk = np.vstack(all_pts)
            cx = int(stk[:, 0].mean() * s)
            cy = int(stk[:, 1].mean() * s)
            cv2.putText(sc_img, f"SC{sc_idx}({len(sc_cids)}c)",
                        (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.35,
                        _distinct_color(sc_idx), 1, cv2.LINE_AA)
    cv2.imwrite(str(debug_dir / "super_clusters.png"), sc_img,
                [cv2.IMWRITE_PNG_COMPRESSION, 3])

    # ── 2. traversal_debug.png ─────────────────────────────────────
    trav_img = np.zeros((dH, dW, 3), dtype=np.uint8)
    for cloud in clouds:
        if len(cloud.polygon) < 3:
            continue
        color = _distinct_color(cloud.id)
        cv2.polylines(trav_img,
                      [(cloud.polygon * s).astype(np.int32).reshape(-1, 1, 2)],
                      True, color, 1)
    for cloud in clouds:
        for fi in cloud.fragment_ids:
            f = frag_map.get(fi)
            if f is None or len(f.points) < 2:
                continue
            cv2.polylines(trav_img, [(f.points * s).astype(np.int32)],
                          f.is_loop, (200, 200, 200), 1)
    for cloud in clouds:
        if cloud.gap_count > 0 and len(cloud.polygon) >= 3:
            cv2.polylines(trav_img,
                          [(cloud.polygon * s).astype(np.int32).reshape(-1, 1, 2)],
                          True, (0, 0, 255), 1)
    cv2.imwrite(str(debug_dir / "traversal_debug.png"), trav_img,
                [cv2.IMWRITE_PNG_COMPRESSION, 3])

    # ── 3. polygons_raw.png ────────────────────────────────────────
    raw_img = np.zeros((dH, dW, 3), dtype=np.uint8)
    for cloud in clouds:
        if len(cloud.polygon) < 3:
            continue
        color = _distinct_color(cloud.id)
        cv2.polylines(raw_img,
                      [(cloud.polygon * s).astype(np.int32).reshape(-1, 1, 2)],
                      True, color, 2)
        cx = int(cloud.polygon[:, 0].mean() * s)
        cy = int(cloud.polygon[:, 1].mean() * s)
        cv2.putText(raw_img, f"R{cloud.id}(gf={cloud.gap_fraction:.2f})",
                    (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.30, color, 1, cv2.LINE_AA)
    cv2.imwrite(str(debug_dir / "polygons_raw.png"), raw_img,
                [cv2.IMWRITE_PNG_COMPRESSION, 3])

    # ── 4. polygons_validated.png ──────────────────────────────────
    val_img = np.zeros((dH, dW, 3), dtype=np.uint8)
    for cloud in clouds:
        if not cloud.is_valid or len(cloud.polygon) < 3:
            continue
        color = _distinct_color(cloud.id)
        poly  = (cloud.polygon * s).astype(np.int32).reshape(-1, 1, 2)
        cv2.fillPoly(val_img, [poly], tuple(c // 4 for c in color))
        cv2.polylines(val_img, [poly], True, color, 2)
        cx = int(cloud.polygon[:, 0].mean() * s)
        cy = int(cloud.polygon[:, 1].mean() * s)
        cv2.putText(val_img,
                    f"V{cloud.id} area={cloud.area:.0f} gf={cloud.gap_fraction:.2f}",
                    (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.32, color, 1, cv2.LINE_AA)
    cv2.imwrite(str(debug_dir / "polygons_validated.png"), val_img,
                [cv2.IMWRITE_PNG_COMPRESSION, 3])

    # ── 5. reconstruction_summary.json ────────────────────────────
    valid_clouds = [c for c in clouds if c.is_valid]
    mean_gf = (sum(c.gap_fraction for c in valid_clouds) / len(valid_clouds)
               if valid_clouds else 0.0)
    reject_reasons: Dict[str, int] = {}
    for c in clouds:
        if not c.is_valid:
            key = ' '.join(c.reject_reason.split()[:3])
            reject_reasons[key] = reject_reasons.get(key, 0) + 1

    summary = {
        'parameters': {
            'CLOUD_R3_MAX':     CLOUD_R3_MAX,
            'EXPAND_R3_MAX':    EXPAND_R3_MAX,
            'MAX_GAP_FRACTION': MAX_GAP_FRACTION,
            'MIN_SCALLOP_FRAC': MIN_SCALLOP_FRAC,
            'MIN_TOTAL_ARCLEN': MIN_TOTAL_ARCLEN,
            'PIPE_FRAG_THRESH': PIPE_FRAG_THRESH,
            'GROUPING_AFFINITY': GROUPING_AFFINITY,
        },
        'totals': {
            'n_input_clusters':   len(clusters),
            'n_cloud_candidates': sum(1 for c in clusters
                                      if c.scallop_fraction > 0
                                      or c.mean_scallop_periodicity > 0),
            'n_primary_sc':       len(super_clusters),
            'n_reconstructed':    len(clouds),
            'n_valid':            len(valid_clouds),
            'n_invalid':          len(clouds) - len(valid_clouds),
            'mean_gap_fraction_valid': round(mean_gf, 4),
            'n_used_core_tier':   sum(1 for c in clouds if c.used_tier == "core"),
            'n_used_all_tier':    sum(1 for c in clouds if c.used_tier == "all"),
            'reject_reasons':     reject_reasons,
        },
        'clouds': [c.to_dict() for c in clouds],
    }
    with open(debug_dir / "reconstruction_summary.json", 'w') as f:
        json.dump(summary, f, indent=2)

    log.info(f"  [rec] debug → {debug_dir}/ "
             f"(super_clusters, traversal_debug, polygons_raw, "
             f"polygons_validated, reconstruction_summary.json)")


# ═══════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════
def main() -> None:
    import argparse, time

    ap = argparse.ArgumentParser(description="Phase 3: cloud reconstruction")
    ap.add_argument("input",        help="P&ID image (Otsu-binarized internally)")
    ap.add_argument("--out",        default="debug_reconstruction", help="debug output dir")
    ap.add_argument("--cloud-r3",   type=float, default=CLOUD_R3_MAX,
                    help=f"cloud-suspect proximity radius (default {CLOUD_R3_MAX}px)")
    ap.add_argument("--expand-r3",  type=float, default=EXPAND_R3_MAX,
                    help=f"expansion / ring-closure radius (default {EXPAND_R3_MAX}px)")
    ap.add_argument("--cstar-bbox", nargs=4, type=int,
                    metavar=('X0','Y0','X1','Y1'),
                    default=[3973, 1585, 5046, 2294],
                    help="C* test region bbox for reporting")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    from pipeline.fragment_extractor import extract_fragments, _binarize
    from pipeline.affinity_grouper import build_graph, group_fragments

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
    log.info("Phase 2: building affinity graph + grouping...")
    g_edges = build_graph(frags)
    clusters = group_fragments(frags, g_edges, image_shape=(H, W))
    log.info(f"  {len(clusters)} clusters in {time.time()-t1:.1f}s")

    t3 = time.time()
    log.info("Phase 3: reconstructing clouds...")
    clouds = reconstruct_clouds(
        frags, clusters, g_edges,
        image_shape=(H, W),
        cloud_r3_max=args.cloud_r3,
        expand_r3_max=args.expand_r3,
        debug_dir=Path(args.out),
    )
    log.info(f"  done in {time.time()-t3:.1f}s")

    # ── C* region analysis ────────────────────────────────────────
    cx0, cy0, cx1, cy1 = args.cstar_bbox
    cstar_clouds = [c for c in clouds
                    if c.bbox[2] >= cx0 and c.bbox[0] <= cx1
                    and c.bbox[3] >= cy0 and c.bbox[1] <= cy1]

    valid_clouds = [c for c in clouds if c.is_valid]
    mean_gf = (sum(c.gap_fraction for c in valid_clouds) / len(valid_clouds)
               if valid_clouds else 0.0)

    print("\n" + "═" * 68)
    print(" PHASE 3: CLOUD RECONSTRUCTION")
    print("═" * 68)
    print(f"  input fragments        : {len(frags)}")
    print(f"  input clusters         : {len(clusters)}")
    print(f"  reconstructed clouds   : {len(clouds)}")
    print(f"  validated clouds       : {len(valid_clouds)}")
    print(f"  mean gap_fraction      : {mean_gf:.3f}")
    print(f"  elapsed                : {time.time()-t0:.1f}s")
    print("─" * 68)
    print(f"  C* region {args.cstar_bbox}: "
          f"{len(cstar_clouds)} cloud(s) overlap")
    for c in cstar_clouds:
        print(f"    Cloud {c.id}: valid={c.is_valid}  area={c.area:.0f}  "
              f"gf={c.gap_fraction:.2f}  reason={c.reject_reason}")
    print("─" * 68)
    print("  Top 20 validated clouds by area:")
    for c in valid_clouds[:20]:
        print(f"    V{c.id:3d}: area={c.area:8.0f}  gf={c.gap_fraction:.2f}  "
              f"scal%={c.scallop_fraction*100:.0f}%  "
              f"frags={len(c.fragment_ids):3d}  bbox={c.bbox}")
    print("─" * 68)
    print(f"  debug → {args.out}/")
    print("═" * 68)


if __name__ == "__main__":
    main()
