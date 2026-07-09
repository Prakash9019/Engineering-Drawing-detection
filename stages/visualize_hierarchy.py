#!/usr/bin/env python3
"""
visualize_hierarchy.py — Hierarchy verification overlay
========================================================

Renders a single annotated JPEG that lets an engineer eyeball whether the
step5b2 hierarchy is correct: which pipes fed the graph, which symbols were
detected, and — colour-coded by evidence — how each instrument/valve was
bound to its equipment parent.

INPUT
    --image       input_drawing.jpg                  (background)
    --hierarchy   output/step5b2_hierarchy_full.json (segments + graph + hierarchy)
    --associations output/step5b_associations_full.json (optional; pipe segments
                  are read from the hierarchy JSON's line_segments[] by default)
    --out         output/hierarchy_verification.jpg

LAYERS (bottom → top)
    1  original drawing
    2  detected pipe segments that feed a real pipeline (cyan)
    3  all detected symbols (thin gray bbox + tag)
    4  equipment nodes (thick red bbox + tag)
    5  parent→child edges coloured by equipment_parent_evidence
         gemini_vision      → orange
         pipeline_traversal → blue
         mounted / proximity→ green
    6  instruments/valves with NO equipment parent (magenta, not isolated)

COORDINATE SYSTEM
    The hierarchy JSON coordinates live in the drawing's native detection space
    (== input_drawing.jpg's pixel space, 9934x7017 on the test sheet). The output
    canvas is rescaled to OUTPUT_WIDTH px; every bbox/segment coordinate is scaled
    by  scale = OUTPUT_WIDTH / image_width  before drawing, so labels stay legible.

Only depends on cv2 + numpy.
"""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

OUTPUT_WIDTH = 5000   # px — final canvas width
JPEG_QUALITY = 92

# ── colours (BGR for OpenCV) ────────────────────────────────────────────────
def _bgr(r, g, b):
    return (b, g, r)

CYAN    = _bgr(0, 255, 255)
GRAY    = _bgr(150, 150, 150)
RED     = _bgr(255, 0, 0)
ORANGE  = _bgr(255, 165, 0)     # gemini_vision
BLUE    = _bgr(0, 100, 255)     # pipeline_traversal
GREEN   = _bgr(0, 200, 0)       # mounted_on / proximity
MAGENTA = _bgr(255, 0, 255)     # no parent
WHITE   = _bgr(255, 255, 255)
BLACK   = _bgr(0, 0, 0)

FONT = cv2.FONT_HERSHEY_SIMPLEX


def evidence_color(ev: str):
    """Map equipment_parent_evidence string → (color, label) by mechanism."""
    e = (ev or "").lower()
    if "gemini_vision" in e:
        return ORANGE, "gemini"
    if "pipeline_traversal" in e:
        return BLUE, "pipeline"
    if "mounted" in e or "mounted_on" in e or "from equipment" in e:
        return GREEN, "mounted"
    return None, "other"


def bbox_pts(bb, s):
    """Return scaled (x1,y1),(x2,y2) integer tuples from a bbox dict."""
    x1 = int(round(bb.get("x1", 0) * s)); y1 = int(round(bb.get("y1", 0) * s))
    x2 = int(round(bb.get("x2", 0) * s)); y2 = int(round(bb.get("y2", 0) * s))
    return (x1, y1), (x2, y2)


def bbox_center(bb, s):
    cx = (bb.get("x1", 0) + bb.get("x2", 0)) / 2.0 * s
    cy = (bb.get("y1", 0) + bb.get("y2", 0)) / 2.0 * s
    return (int(round(cx)), int(round(cy)))


def label(img, text, org, color, scale=0.3, thick=1, bg=None):
    """putText with an optional filled background box for readability."""
    if bg is not None:
        (tw, th), base = cv2.getTextSize(text, FONT, scale, thick)
        x, y = org
        cv2.rectangle(img, (x - 1, y - th - base - 1), (x + tw + 1, y + base - 1), bg, -1)
    cv2.putText(img, text, org, FONT, scale, color, thick, cv2.LINE_AA)


def draw_legend(img):
    """White box, top-left, explaining every colour."""
    rows = [
        (GREEN,   "GREEN box/arrow  = MOUNTED_ON (physical proximity)"),
        (BLUE,    "BLUE box/arrow   = Pipeline traversal (graph path)"),
        (ORANGE,  "ORANGE box/arrow = Gemini vision (AI inference)"),
        (MAGENTA, "MAGENTA box      = No equipment parent found"),
        (CYAN,    "CYAN line        = Detected pipe segment"),
    ]
    pad = 14
    line_h = 30
    sample_w = 46
    box_w = 640
    box_h = pad * 2 + line_h * len(rows)
    x0, y0 = 20, 20
    # white background with black border
    cv2.rectangle(img, (x0, y0), (x0 + box_w, y0 + box_h), WHITE, -1)
    cv2.rectangle(img, (x0, y0), (x0 + box_w, y0 + box_h), BLACK, 2)
    cv2.putText(img, "LEGEND", (x0 + pad, y0 + pad + 8), FONT, 0.55, BLACK, 2, cv2.LINE_AA)
    for i, (col, txt) in enumerate(rows):
        cy = y0 + pad + line_h * (i + 1) + 4
        cv2.line(img, (x0 + pad, cy - 6), (x0 + pad + sample_w, cy - 6), col, 5, cv2.LINE_AA)
        cv2.putText(img, txt, (x0 + pad + sample_w + 14, cy), FONT, 0.5, BLACK, 1, cv2.LINE_AA)


def main():
    ap = argparse.ArgumentParser(description="Hierarchy verification overlay")
    ap.add_argument("--hierarchy", default="output/step5b2_hierarchy_full.json")
    ap.add_argument("--image", default="input_drawing.jpg")
    ap.add_argument("--associations", default="output/step5b_associations_full.json",
                    help="optional; pipe segments are read from --hierarchy by default")
    ap.add_argument("--out", default="output/hierarchy_verification.jpg")
    args = ap.parse_args()

    H = json.load(open(args.hierarchy))
    img = cv2.imread(args.image)
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {args.image}")
    img_h, img_w = img.shape[:2]

    # scale JSON (native detection space == image pixel space) → output canvas
    s = OUTPUT_WIDTH / float(img_w)
    out_h = int(round(img_h * s))
    canvas = cv2.resize(img, (OUTPUT_WIDTH, out_h), interpolation=cv2.INTER_AREA)
    print(f"Image {img_w}x{img_h} → canvas {OUTPUT_WIDTH}x{out_h} (scale {s:.4f})")

    nodes = {n["node_id"]: n for n in H.get("graph", {}).get("nodes", [])}
    hier = H.get("hierarchy", [])
    segs = H.get("line_segments", [])

    # ── Layer 2: pipe segments feeding a real pipeline ──────────────────────
    n_pipe = 0
    for seg in segs:
        if seg.get("type") not in ("horizontal_pipe", "vertical_pipe"):
            continue
        if (seg.get("length") or 0) <= 150:
            continue
        if not seg.get("pipeline_id"):
            continue
        p0 = (int(round(seg["x0"] * s)), int(round(seg["y0"] * s)))
        p1 = (int(round(seg["x1"] * s)), int(round(seg["y1"] * s)))
        cv2.line(canvas, p0, p1, CYAN, 3, cv2.LINE_AA)
        n_pipe += 1

    # ── Layer 3: all detected symbols (thin gray) ───────────────────────────
    for n in nodes.values():
        if n.get("kind") not in ("instrument", "valve", "equipment"):
            continue
        bb = n.get("bbox") or {}
        (x1, y1), (x2, y2) = bbox_pts(bb, s)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), GRAY, 1, cv2.LINE_AA)
        tag = n.get("tag_text") or ""
        if tag:
            label(canvas, tag, (x1, max(y1 - 3, 10)), GRAY, scale=0.3, thick=1)

    # ── Layer 4: equipment nodes (thick red) ────────────────────────────────
    for n in nodes.values():
        if n.get("kind") != "equipment":
            continue
        bb = n.get("bbox") or {}
        (x1, y1), (x2, y2) = bbox_pts(bb, s)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), RED, 4, cv2.LINE_AA)
        tag = n.get("tag_text") or ""
        if tag:
            label(canvas, tag, (x1, max(y1 - 10, 24)), RED, scale=0.9, thick=2, bg=WHITE)

    # ── Layer 5: parent→child edges coloured by evidence ────────────────────
    n_edges = 0
    by_ev = {"gemini": 0, "pipeline": 0, "mounted": 0, "other": 0}
    for h in hier:
        if not h.get("equipment_parent"):
            continue
        pid = h.get("equipment_parent_id")
        cid = h.get("node_id")
        pnode = nodes.get(pid)
        cnode = nodes.get(cid)
        if not pnode or not cnode:
            continue
        col, evname = evidence_color(h.get("equipment_parent_evidence"))
        by_ev[evname] = by_ev.get(evname, 0) + 1
        if col is None:           # unknown evidence — skip drawing (still counted)
            continue
        pbb = pnode.get("bbox") or {}
        cbb = cnode.get("bbox") or {}
        (px1, py1), (px2, py2) = bbox_pts(pbb, s)
        (cx1, cy1), (cx2, cy2) = bbox_pts(cbb, s)
        # parent bbox (4px), child bbox (3px) in evidence colour
        cv2.rectangle(canvas, (px1, py1), (px2, py2), col, 4, cv2.LINE_AA)
        cv2.rectangle(canvas, (cx1, cy1), (cx2, cy2), col, 3, cv2.LINE_AA)
        # arrow child → parent
        cv2.arrowedLine(canvas, bbox_center(cbb, s), bbox_center(pbb, s),
                        col, 2, cv2.LINE_AA, tipLength=0.02)
        # label near child
        conf = h.get("equipment_parent_confidence")
        conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
        txt = f"{h.get('tag_text','')} -> {pnode.get('tag_text','')} [{conf_s}]"
        label(canvas, txt, (cx1, max(cy1 - 3, 10)), col, scale=0.3, thick=1, bg=WHITE)
        n_edges += 1

    # ── Layer 6: instruments/valves with NO equipment parent ────────────────
    n_noparent = 0
    for h in hier:
        if h.get("kind") not in ("instrument", "valve"):
            continue
        if h.get("equipment_parent"):
            continue
        if h.get("is_isolated"):
            continue
        node = nodes.get(h.get("node_id"))
        if not node:
            continue
        bb = node.get("bbox") or {}
        (x1, y1), (x2, y2) = bbox_pts(bb, s)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), MAGENTA, 2, cv2.LINE_AA)
        label(canvas, "NO PARENT", (x1, max(y1 - 3, 10)), MAGENTA, scale=0.3, thick=1, bg=WHITE)
        n_noparent += 1

    # ── Legend (drawn last, top-left) ───────────────────────────────────────
    draw_legend(canvas)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), canvas, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])

    # ── Console report ──────────────────────────────────────────────────────
    print("\n=== Hierarchy Verification Overlay ===")
    print(f"  Parent-child edges drawn : {n_edges}")
    print(f"    by evidence            : gemini={by_ev['gemini']}  "
          f"pipeline={by_ev['pipeline']}  mounted={by_ev['mounted']}"
          + (f"  other={by_ev['other']}" if by_ev.get('other') else ""))
    print(f"  NO PARENT instruments    : {n_noparent}  (magenta boxes)")
    print(f"  Pipe segments drawn      : {n_pipe}  (cyan lines)")
    print(f"  Output                   : {out}")


if __name__ == "__main__":
    main()
