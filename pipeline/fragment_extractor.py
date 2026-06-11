"""
Phase 1: Stroke-Derived Arc Fragment Extraction
================================================
Replaces contour-derived primitives (cv2.findContours region components) with
stroke-derived arc fragments.

WHY
---
The diagnostic proved that connected-component topology != cloud-instance
topology. `findContours` either SHATTERS a cloud into many region pieces (B/D
failures) or WELDS it — through the border frame, pipes, leaders, and text —
into sheet-spanning blobs that the area ceiling discards (C* failures). Both are
the same disease: the region component is not the cloud.

This module attacks the disease at its source. It does NOT look at regions at
all. It reduces the line work to 1-px centerlines, finds the junction pixels
where pipes/text/borders cross the cloud outline, and CUTS there. The cut
severs the welds (de-fusing C*) and yields clean open arcs (normalizing B/D)
that downstream affinity grouping (Phase 2) and graph reconstruction (Phase 3)
can operate on.

PIPELINE
--------
    binary
    → skeletonization            (skimage, Zhang-Suen/Lee)
    → graph extraction           (per-pixel 8-neighbour degree)
    → junction detection         (degree >= 3)
    → split at junctions         (remove dilated junction pixels)
    → fragment generation        (connected components → ordered polylines)

SCOPE (per Phase 1 instructions)
--------------------------------
  - NO affinity graph
  - NO clustering
  - NO reconstruction
  Only high-quality cloud-boundary fragments + debug visualisations.

DEPENDENCIES
------------
  scikit-image  (skeletonize)        — added to requirements.txt
  scipy         (ndimage.label)      — added to requirements.txt
  opencv, numpy (already present)
"""
import os, sys, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from skimage.morphology import skeletonize
from scipy import ndimage

log = logging.getLogger(__name__)

# ── Extraction parameters (structural, not detection thresholds) ──
MIN_FRAGMENT_LEN  = 40      # px arc-length floor — drops skeleton speckle/spurs
JUNCTION_DILATE   = 1       # px: widen junction cut so diagonal crossings split cleanly
TANGENT_SPAN      = 6       # px along the chain used to estimate endpoint tangents
CURVATURE_STEP    = 7       # px step for discrete curvature (matches stage1 convention)


# ═══════════════════════════════════════════════════════════════════
# Fragment data structure
# ═══════════════════════════════════════════════════════════════════
@dataclass
class Fragment:
    """A single stroke-derived arc fragment (open polyline, ordered)."""
    id: int
    points: np.ndarray                       # Nx2 (x,y), ordered along the arc
    endpoints: Tuple[np.ndarray, np.ndarray] # (start_xy, end_xy)
    endpoint_tangents: Tuple[np.ndarray, np.ndarray]  # unit vectors pointing INWARD
    arc_length: float
    mean_curvature: float                    # mean |turning angle| over interior
    scallop_periodicity: float               # 0-1, reuse of stage1 periodicity model
    is_loop: bool = False                     # closed chain (no free endpoints)

    def to_dict(self) -> dict:
        (s, e) = self.endpoints
        (ts, te) = self.endpoint_tangents
        return {
            'id': self.id,
            'n_points': int(len(self.points)),
            'points': self.points.astype(int).tolist(),
            'endpoints': [s.tolist(), e.tolist()],
            'endpoint_tangents': [ts.tolist(), te.tolist()],
            'arc_length': round(float(self.arc_length), 2),
            'mean_curvature': round(float(self.mean_curvature), 4),
            'scallop_periodicity': round(float(self.scallop_periodicity), 3),
            'is_loop': bool(self.is_loop),
        }


# ═══════════════════════════════════════════════════════════════════
# Skeleton graph: degree map, endpoints, junctions
# ═══════════════════════════════════════════════════════════════════
_NB_KERNEL = np.array([[1, 1, 1],
                       [1, 0, 1],
                       [1, 1, 1]], dtype=np.uint8)


def _skeletonize(binary: np.ndarray) -> np.ndarray:
    """Reduce strokes to 1-px centerlines. Returns bool array."""
    bw = (binary > 0)
    skel = skeletonize(bw)
    return skel.astype(np.uint8)


def _degree_map(skel: np.ndarray) -> np.ndarray:
    """8-neighbour count for every skeleton pixel (0 off-skeleton)."""
    deg = cv2.filter2D(skel, -1, _NB_KERNEL, borderType=cv2.BORDER_CONSTANT)
    return deg * skel


def _split_at_junctions(skel: np.ndarray, deg: np.ndarray
                        ) -> Tuple[np.ndarray, np.ndarray]:
    """
    Remove junction pixels (degree >= 3) so welded structures separate into
    independent arc chains. Returns (cut_skeleton, junction_mask).
    """
    junctions = ((skel == 1) & (deg >= 3)).astype(np.uint8)
    if JUNCTION_DILATE > 0 and junctions.any():
        k = cv2.getStructuringElement(
            cv2.MORPH_RECT, (2 * JUNCTION_DILATE + 1, 2 * JUNCTION_DILATE + 1))
        junc_dil = cv2.dilate(junctions, k)
    else:
        junc_dil = junctions
    cut = skel & (~junc_dil.astype(bool))
    return cut.astype(np.uint8), junctions


# ═══════════════════════════════════════════════════════════════════
# Chain ordering: connected component pixels → ordered polyline
# ═══════════════════════════════════════════════════════════════════
def _order_chain(coords: List[Tuple[int, int]]) -> Tuple[List[Tuple[int, int]], bool]:
    """
    Order a set of 8-connected (y,x) pixels into a single path.

    Returns (ordered_list, is_loop). Walks from a free endpoint (degree 1) if
    one exists; otherwise treats the chain as a closed loop and starts anywhere.
    Branch leftovers (rare after junction removal) follow one branch — short
    spurs are dropped later by the length filter.
    """
    pixset = set(coords)
    nbrs = {}
    for (y, x) in coords:
        ns = [(y + dy, x + dx)
              for dy in (-1, 0, 1) for dx in (-1, 0, 1)
              if (dy or dx) and (y + dy, x + dx) in pixset]
        nbrs[(y, x)] = ns

    ends = [p for p, n in nbrs.items() if len(n) == 1]
    is_loop = len(ends) == 0
    start = ends[0] if ends else coords[0]

    path = [start]
    visited = {start}
    cur = start
    while True:
        nxt = None
        for n in nbrs[cur]:
            if n not in visited:
                nxt = n
                break
        if nxt is None:
            break
        path.append(nxt)
        visited.add(nxt)
        cur = nxt
    return path, is_loop


# ═══════════════════════════════════════════════════════════════════
# Per-fragment geometry
# ═══════════════════════════════════════════════════════════════════
def _arc_length(pts: np.ndarray) -> float:
    if len(pts) < 2:
        return 0.0
    d = np.diff(pts.astype(np.float64), axis=0)
    return float(np.hypot(d[:, 0], d[:, 1]).sum())


def _endpoint_tangents(pts: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Unit tangents at start/end, each pointing INWARD along the chain."""
    n = len(pts)
    k = min(TANGENT_SPAN, n - 1)
    if k < 1:
        z = np.zeros(2)
        return z, z
    p = pts.astype(np.float64)
    t_start = p[k] - p[0]
    t_end = p[n - 1 - k] - p[n - 1]
    def _unit(v):
        m = np.hypot(v[0], v[1])
        return v / m if m > 1e-6 else np.zeros(2)
    return _unit(t_start), _unit(t_end)


def _signed_curvature(pts: np.ndarray, step: int = CURVATURE_STEP) -> np.ndarray:
    """
    Signed turning angle along an OPEN polyline (no wraparound).

    Mirrors stage1's _compute_curvature but for open chains: only interior
    indices [step, n-step) are valid. Revision-cloud scallops produce rapid
    sign oscillation; pipes/straight runs stay near zero.
    """
    p = pts.astype(np.float64)
    n = len(p)
    if n < step * 2 + 1:
        return np.array([])
    out = np.zeros(n)
    for i in range(step, n - step):
        v1 = p[i] - p[i - step]
        v2 = p[i + step] - p[i]
        cross = v1[0] * v2[1] - v1[1] * v2[0]
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        m1 = np.hypot(v1[0], v1[1])
        m2 = np.hypot(v2[0], v2[1])
        if m1 > 0.5 and m2 > 0.5:
            out[i] = np.arctan2(cross, dot)
    return out[step:n - step]


def _scallop_periodicity(pts: np.ndarray, arclen: float) -> float:
    """
    Periodicity score on an open arc, reusing stage1's model:
      crossings-per-100px of curvature sign changes, gated by amplitude.
    Cloud arcs: periodic oscillation. Pipes/text: too few or too noisy.
    """
    curv = _signed_curvature(pts)
    if len(curv) < 8 or arclen < 60:
        return 0.0
    signs = np.sign(curv)
    signs[signs == 0] = 1
    crossings = int(np.sum(np.abs(np.diff(signs)) > 0))
    density = crossings / (arclen / 100.0)
    if 1.5 <= density <= 14.0:
        nz = curv[curv != 0]
        std = float(np.std(nz)) if nz.size else 0.0
        if std > 0.05:
            return float(min(1.0, density / 6.0) * min(1.0, std * 5.0))
    return 0.0


def _mean_curvature(pts: np.ndarray) -> float:
    curv = _signed_curvature(pts)
    return float(np.mean(np.abs(curv))) if len(curv) else 0.0


# ═══════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════
def extract_fragments(binary: np.ndarray,
                      debug_dir: Optional[Path] = None) -> List[Fragment]:
    """
    Extract stroke-derived arc fragments from a binary image.

    Args:
        binary:    uint8 image, non-zero = foreground (line work).
        debug_dir: if given, writes skeleton/junctions/fragments visualisations.

    Returns:
        List[Fragment] — open arcs, junction-split, length-filtered.
    """
    if binary is None or binary.size == 0:
        raise ValueError("empty binary image")
    H, W = binary.shape[:2]
    log.info(f"  [frag] skeletonizing {W}x{H}...")

    skel = _skeletonize(binary)
    deg = _degree_map(skel)
    cut, junctions = _split_at_junctions(skel, deg)

    # Connected components of the cut skeleton = candidate arc chains.
    labels, n_lab = ndimage.label(cut, structure=np.ones((3, 3), dtype=int))
    log.info(f"  [frag] {n_lab} raw chains after junction split")

    # Group pixel coords by label in one pass.
    ys, xs = np.nonzero(cut)
    lab = labels[ys, xs]
    order = np.argsort(lab, kind='stable')
    ys, xs, lab = ys[order], xs[order], lab[order]
    boundaries = np.searchsorted(lab, np.arange(1, n_lab + 1), side='left')
    boundaries = np.append(boundaries, len(lab))

    fragments: List[Fragment] = []
    fid = 0
    for li in range(n_lab):
        a, b = boundaries[li], boundaries[li + 1]
        if b - a < 2:
            continue
        coords = list(zip(ys[a:b].tolist(), xs[a:b].tolist()))
        path, is_loop = _order_chain(coords)
        if len(path) < 2:
            continue
        pts = np.array([(x, y) for (y, x) in path], dtype=np.int32)  # (x,y)
        arclen = _arc_length(pts)
        if arclen < MIN_FRAGMENT_LEN:
            continue
        ts, te = _endpoint_tangents(pts)
        frag = Fragment(
            id=fid,
            points=pts,
            endpoints=(pts[0].copy(), pts[-1].copy()),
            endpoint_tangents=(ts, te),
            arc_length=arclen,
            mean_curvature=_mean_curvature(pts),
            scallop_periodicity=_scallop_periodicity(pts, arclen),
            is_loop=is_loop,
        )
        fragments.append(frag)
        fid += 1

    log.info(f"  [frag] {len(fragments)} fragments (>= {MIN_FRAGMENT_LEN}px)")

    if debug_dir is not None:
        _save_debug(debug_dir, (H, W), skel, junctions, fragments)

    return fragments


# ═══════════════════════════════════════════════════════════════════
# Debug visualisations
# ═══════════════════════════════════════════════════════════════════
def _save_debug(debug_dir: Path, shape: Tuple[int, int],
                skel: np.ndarray, junctions: np.ndarray,
                fragments: List[Fragment]) -> None:
    H, W = shape
    debug_dir.mkdir(parents=True, exist_ok=True)

    # 1. skeleton.png — white centerlines on black
    cv2.imwrite(str(debug_dir / "skeleton.png"), (skel * 255).astype(np.uint8))

    # 2. junctions.png — skeleton dim grey, junctions as red dots
    jvis = cv2.cvtColor((skel * 80).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    jy, jx = np.nonzero(junctions)
    for y, x in zip(jy.tolist(), jx.tolist()):
        cv2.circle(jvis, (x, y), 3, (0, 0, 255), -1)
    cv2.imwrite(str(debug_dir / "junctions.png"), jvis)

    # 3. fragments.png — all arcs white on black (the clean arc set)
    fimg = np.zeros((H, W), dtype=np.uint8)
    for f in fragments:
        cv2.polylines(fimg, [f.points], isClosed=f.is_loop, color=255, thickness=2)
    cv2.imwrite(str(debug_dir / "fragments.png"), fimg)

    # 4. fragments_colored.png — each fragment its own colour (separation proof)
    cimg = np.zeros((H, W, 3), dtype=np.uint8)
    rng = np.random.default_rng(12345)
    for f in fragments:
        color = tuple(int(c) for c in rng.integers(60, 256, size=3))
        cv2.polylines(cimg, [f.points], isClosed=f.is_loop, color=color, thickness=2)
        # mark endpoints to make arc breaks visible
        if not f.is_loop:
            for ep in f.endpoints:
                cv2.circle(cimg, (int(ep[0]), int(ep[1])), 3, (255, 255, 255), -1)
    cv2.imwrite(str(debug_dir / "fragments_colored.png"), cimg)

    log.info(f"  [frag] debug → {debug_dir}/ "
             f"(skeleton, junctions, fragments, fragments_colored)")


# ═══════════════════════════════════════════════════════════════════
# CLI — binarize an image the same way Stage 1 does, then extract
# ═══════════════════════════════════════════════════════════════════
def _binarize(image: np.ndarray) -> np.ndarray:
    """Global Otsu (inverted) — identical to stage1_cloud Stage 1."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw


def main():
    import argparse, json
    ap = argparse.ArgumentParser(description="Phase 1: stroke-derived arc fragments")
    ap.add_argument("input", help="P&ID image (will be binarized) OR a binary png")
    ap.add_argument("--out", default="debug_fragments", help="debug output dir")
    ap.add_argument("--binary", action="store_true",
                    help="treat input as an already-binarized image (skip Otsu)")
    ap.add_argument("--json", help="optional path to dump fragments as JSON")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    img = cv2.imread(args.input, cv2.IMREAD_COLOR if not args.binary else cv2.IMREAD_GRAYSCALE)
    if img is None:
        print(f"Cannot read: {args.input}", file=sys.stderr); sys.exit(1)
    binary = img if args.binary else _binarize(img)

    frags = extract_fragments(binary, debug_dir=Path(args.out))

    if args.json:
        with open(args.json, 'w') as f:
            json.dump([fr.to_dict() for fr in frags], f, indent=2)
        print(f"  fragments JSON → {args.json}")

    # Summary stats
    lens = np.array([f.arc_length for f in frags]) if frags else np.array([0])
    pers = np.array([f.scallop_periodicity for f in frags]) if frags else np.array([0])
    print("\n" + "═" * 60)
    print(" PHASE 1: ARC FRAGMENT EXTRACTION")
    print("═" * 60)
    print(f"  fragments            : {len(frags)}")
    print(f"  arc length  (px)     : min={lens.min():.0f} med={np.median(lens):.0f} max={lens.max():.0f}")
    print(f"  loops (closed arcs)  : {sum(1 for f in frags if f.is_loop)}")
    print(f"  with scallop signal  : {int((pers > 0.25).sum())} (periodicity>0.25)")
    print(f"  debug images         : {args.out}/")
    print("═" * 60)
    print("  Inspect fragments_colored.png: cloud boundaries should appear as")
    print("  many distinctly-coloured arcs, NOT one welded blob.")
    print()


if __name__ == "__main__":
    main()
