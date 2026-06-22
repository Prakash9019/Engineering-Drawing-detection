#!/usr/bin/env python3
"""
step2c_cloud_editor.py — Interactive Revision Cloud Correction Editor
=====================================================================
CDCI P&ID Pipeline — Step 2C

Human-in-the-loop revision cloud correction. Sits between step2b
(auto-detection) and step5a (symbol extraction).

Workflow:
  1. Loads input drawing + step2b cloud detections (outer_clouds_v2.json)
  2. Launches a browser-based interactive editor (localhost HTTP server)
  3. User reviews, adds, deletes, merges, extends, and edits clouds
  4. On "Done", generates approved cloud geometry consumed by step5a

Outputs:
  approved_clouds.json      — ground truth for all downstream steps (JSON space)
  cloud_mask_approved.png   — binary mask at image resolution (grayscale)
  overlay_approved.jpg      — visual verification

Coordinate spaces (see COORDINATE_NOTES below):
  JSON space    = polygon coords in outer_clouds_v2.json (e.g. 9934x7017)
  Image space   = actual image file resolution (e.g. 8000x5650)
  Display space = downscaled image sent to browser (2400px wide)

Usage:
  python step2c_cloud_editor.py --image input_drawing.jpg \
      --clouds output/outer_clouds_v2.json --out output/

  python step2c_cloud_editor.py --image input_drawing.jpg \
      --clouds output/outer_clouds_v2.json --overlay output/overlay_v2.jpg \
      --out output/ --port 9000 --no-browser
"""

import argparse
import base64
import json
import logging
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("step2c")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Configuration ──────────────────────────────────────────────────────────
DISPLAY_WIDTH = 2400          # browser display image width (downscaled)
DISPLAY_JPEG_QUALITY = 78     # quality for the base64 image sent to browser
DISPLAY_MAX_POLY_POINTS = 120 # max polygon points kept for display rendering
OVERLAY_JPEG_QUALITY = 92     # quality for the output overlay
MASK_PER_CLOUD_PAD = 10       # px padding around per-cloud mask crops
MAX_POST_BYTES = 50 * 1024 * 1024  # 50 MB POST limit


# ═══════════════════════════════════════════════════════════════════════════
# Geometry helpers
# ═══════════════════════════════════════════════════════════════════════════

def polygon_area(pts):
    """Shoelace area of a polygon (list of [x, y])."""
    if len(pts) < 3:
        return 0.0
    arr = np.asarray(pts, dtype=np.float64)
    x = arr[:, 0]
    y = arr[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)


def polygon_bbox(pts):
    """Bounding box [x0, y0, x1, y1] of a polygon."""
    arr = np.asarray(pts)
    return [int(arr[:, 0].min()), int(arr[:, 1].min()),
            int(arr[:, 0].max()), int(arr[:, 1].max())]


def polygon_scallopedness(pts):
    """peri / hull_peri — bumpiness measure."""
    if len(pts) < 3:
        return 0.0
    cnt = np.asarray(pts, dtype=np.int32).reshape(-1, 1, 2)
    peri = cv2.arcLength(cnt, True)
    hull = cv2.convexHull(cnt)
    hull_peri = cv2.arcLength(hull, True)
    return float(peri / hull_peri) if hull_peri > 1e-6 else 0.0


def simplify_uniform(pts, max_points):
    """
    Uniform subsampling that PRESERVES curve shape (not RDP, which flattens arcs).
    Keeps every Nth point so scalloped boundaries stay scalloped.
    """
    if len(pts) <= max_points:
        return list(pts)
    step = max(1, len(pts) // max_points)
    out = pts[::step]
    # Ensure the polygon stays closed-ish by keeping the last point
    if out[-1] != pts[-1]:
        out = out + [pts[-1]]
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Data preparation
# ═══════════════════════════════════════════════════════════════════════════

def encode_image_b64(img_bgr, max_width=DISPLAY_WIDTH, quality=DISPLAY_JPEG_QUALITY):
    """Resize (if needed) and base64-encode an OpenCV image as JPEG."""
    h, w = img_bgr.shape[:2]
    if w > max_width:
        scale = max_width / w
        img_bgr = cv2.resize(img_bgr, (max_width, int(round(h * scale))),
                             interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("Failed to JPEG-encode image")
    return base64.b64encode(buf).decode("ascii")


def prepare_editor_data(img_bgr, cloud_data, scale_j2i):
    """
    Build the full payload the browser editor needs.
    Returns dict (see schema in the implementation plan §4.3).
    """
    img_h, img_w = img_bgr.shape[:2]
    display_scale = DISPLAY_WIDTH / img_w
    display_h = int(round(img_h * display_scale))

    image_b64 = encode_image_b64(img_bgr)

    clouds = []
    for c in cloud_data.get("clouds", []):
        if c.get("tag") != "outer":
            continue  # editor handles outer clouds only

        poly_json = c["polygon"]                          # JSON space (original, full)
        # JSON -> image space (full resolution, for export precision)
        poly_img_full = [[round(p[0] * scale_j2i), round(p[1] * scale_j2i)]
                         for p in poly_json]
        # Simplify for display
        poly_img = simplify_uniform(poly_img_full, DISPLAY_MAX_POLY_POINTS)
        # image -> display space
        poly_display = [[round(p[0] * display_scale, 1),
                         round(p[1] * display_scale, 1)] for p in poly_img]

        bbox_img = [round(v * scale_j2i) for v in c["bbox"]]
        bbox_display = [round(v * display_scale, 1) for v in bbox_img]

        clouds.append({
            "id": c["id"],
            "tag": c.get("tag", "outer"),
            "source": c.get("source", "unknown"),
            "confidence": round(c.get("confidence", 0.8), 3),
            "scallopedness": round(c.get("scallopedness", 0.0), 3),
            "areaJson": round(c.get("area", 0.0), 1),
            "pointCount": len(poly_json),
            "bbox": bbox_display,
            "bboxImg": bbox_img,
            "polygon": poly_display,
            "polygonImg": poly_img,
            "polygonJson": poly_json,
        })

    log.info("Prepared %d outer clouds for editor", len(clouds))

    return {
        "image_b64": image_b64,
        "display_w": DISPLAY_WIDTH,
        "display_h": display_h,
        "image_w": img_w,
        "image_h": img_h,
        "json_w": cloud_data["stats"]["image_size"][0],
        "json_h": cloud_data["stats"]["image_size"][1],
        "scale_j2i": scale_j2i,
        "display_scale": display_scale,
        "clouds": clouds,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════════════

def validate_clouds(clouds, img_w, img_h):
    """
    Validate the approved cloud list before generating outputs.
    Returns list of warning strings. Raises ValueError on critical errors.
    """
    warnings = []
    seen_ids = set()
    margin = 50  # px tolerance outside image bounds

    if not clouds:
        warnings.append("No clouds in approved set — outputs will be empty.")

    for c in clouds:
        cid = c.get("id")
        poly = c.get("_poly_img")  # image-space polygon (set during generation)
        if poly is None:
            poly = c.get("polygon_img") or c.get("polygonImg")

        if cid in seen_ids:
            raise ValueError(f"Duplicate cloud id: {cid}")
        seen_ids.add(cid)

        if not poly or len(poly) < 3:
            raise ValueError(f"Cloud {cid} has fewer than 3 polygon points")

        for p in poly:
            if len(p) != 2:
                raise ValueError(f"Cloud {cid} has malformed point: {p}")

        if polygon_area(poly) < 1.0:
            raise ValueError(f"Cloud {cid} has zero area (collinear points)")

        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        if (min(xs) < -margin or min(ys) < -margin or
                max(xs) > img_w + margin or max(ys) > img_h + margin):
            warnings.append(f"Cloud {cid} extends beyond image bounds.")

    return warnings


# ═══════════════════════════════════════════════════════════════════════════
# Per-cloud mask (RLE) for JSON
# ═══════════════════════════════════════════════════════════════════════════

def rle_encode(mask_1d):
    """Simple run-length encode of a flat 0/1 array → list of run lengths."""
    runs = []
    prev = 0
    count = 0
    for v in mask_1d:
        if v == prev:
            count += 1
        else:
            runs.append(count)
            prev = v
            count = 1
    runs.append(count)
    return runs


def compute_per_cloud_mask(poly_img, img_shape):
    """
    Generate a per-cloud mask cropped to the cloud's bbox (with padding).
    Returns { mask_bbox: [...], mask_rle_b64: str } in IMAGE space.
    """
    img_h, img_w = img_shape[:2]
    arr = np.asarray(poly_img, dtype=np.int32)
    x0 = max(0, arr[:, 0].min() - MASK_PER_CLOUD_PAD)
    y0 = max(0, arr[:, 1].min() - MASK_PER_CLOUD_PAD)
    x1 = min(img_w, arr[:, 0].max() + MASK_PER_CLOUD_PAD)
    y1 = min(img_h, arr[:, 1].max() + MASK_PER_CLOUD_PAD)

    crop_w = max(1, x1 - x0)
    crop_h = max(1, y1 - y0)
    local = np.zeros((crop_h, crop_w), dtype=np.uint8)
    local_pts = arr.copy()
    local_pts[:, 0] -= x0
    local_pts[:, 1] -= y0
    cv2.fillPoly(local, [local_pts.reshape(-1, 1, 2)], 1)

    runs = rle_encode(local.flatten().tolist())
    rle_bytes = json.dumps(runs).encode("utf-8")
    rle_b64 = base64.b64encode(rle_bytes).decode("ascii")

    return {
        "mask_bbox": [int(x0), int(y0), int(x1), int(y1)],
        "mask_shape": [int(crop_h), int(crop_w)],
        "mask_rle_b64": rle_b64,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Output generation
# ═══════════════════════════════════════════════════════════════════════════

def generate_all_outputs(payload, img_bgr, original_cloud_data,
                         scale_j2i, display_scale, out_dir, include_per_cloud_mask=True):
    """
    Generate approved_clouds.json, cloud_mask_approved.png, overlay_approved.jpg.
    Returns dict of output paths + counts.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    img_h, img_w = img_bgr.shape[:2]
    json_w, json_h = original_cloud_data["stats"]["image_size"]
    scale_i2j = 1.0 / scale_j2i
    scale_d2i = 1.0 / display_scale

    approved = payload["clouds"]
    edit_summary = payload.get("edit_summary", {})

    production = []
    for c in approved:
        status = c.get("status", "approved")

        # --- Resolve polygon in JSON space ---
        if status == "approved" and c.get("polygon_json"):
            # Unchanged: use original — zero precision loss
            poly_json = [[int(p[0]), int(p[1])] for p in c["polygon_json"]]
            poly_img = [[round(p[0] * scale_j2i), round(p[1] * scale_j2i)]
                        for p in poly_json]
        elif c.get("polygon_img"):
            poly_img = [[int(p[0]), int(p[1])] for p in c["polygon_img"]]
            poly_json = [[round(p[0] * scale_i2j), round(p[1] * scale_i2j)]
                         for p in poly_img]
        else:
            # Only display-space available (user-drawn)
            poly_display = c["polygon_display"]
            poly_img = [[round(p[0] * scale_d2i), round(p[1] * scale_d2i)]
                        for p in poly_display]
            poly_json = [[round(p[0] * scale_d2i * scale_i2j),
                          round(p[1] * scale_d2i * scale_i2j)]
                         for p in poly_display]

        entry = {
            "id": c["id"],
            "tag": "outer",
            "source": c.get("source", "manual_add"),
            "confidence": round(c.get("confidence", 1.0), 3),
            "bbox": polygon_bbox(poly_json),
            "area": round(polygon_area(poly_json), 1),
            "scallopedness": round(polygon_scallopedness(poly_json), 3),
            "polygon": poly_json,
            "status": status,
            "_poly_img": poly_img,  # internal, stripped from JSON
        }

        if include_per_cloud_mask:
            entry.update(compute_per_cloud_mask(poly_img, img_bgr.shape))

        production.append(entry)

    # --- Validate ---
    warnings = validate_clouds(production, img_w, img_h)
    for w in warnings:
        log.warning("VALIDATION: %s", w)

    # --- OUTPUT 1: approved_clouds.json ---
    json_clouds = [{k: v for k, v in c.items() if not k.startswith("_")}
                   for c in production]
    original_count = edit_summary.get("original_count", 0)
    json_output = {
        "clouds": json_clouds,
        "stats": {
            "total": len(production),
            "outer": len(production),
            "inner": 0,
            "image_size": [json_w, json_h],
        },
        "edit_metadata": {
            "workflow": "human_in_the_loop",
            "editor_version": "2c-v1.0",
            "original_auto_detected": original_count,
            "approved_unchanged": max(0, original_count
                                      - edit_summary.get("deleted", 0)
                                      - edit_summary.get("modified", 0)
                                      - edit_summary.get("merged", 0)),
            "deleted": edit_summary.get("deleted", 0),
            "added": edit_summary.get("added", 0),
            "modified": edit_summary.get("modified", 0),
            "merged": edit_summary.get("merged", 0),
            "image_file_size": [img_w, img_h],
        },
    }
    json_path = str(out_path / "approved_clouds.json")
    with open(json_path, "w") as f:
        json.dump(json_output, f, indent=2)
    log.info("Wrote %s (%d clouds)", json_path, len(production))

    # --- OUTPUT 2: cloud_mask_approved.png (grayscale) ---
    mask = np.zeros((img_h, img_w), dtype=np.uint8)
    for c in production:
        pts = np.asarray(c["_poly_img"], dtype=np.int32).reshape(-1, 1, 2)
        cv2.fillPoly(mask, [pts], 255)
    mask_path = str(out_path / "cloud_mask_approved.png")
    cv2.imwrite(mask_path, mask)
    log.info("Wrote %s", mask_path)

    # --- OUTPUT 3: overlay_approved.jpg ---
    overlay = img_bgr.copy()
    for c in production:
        pts = np.asarray(c["_poly_img"], dtype=np.int32).reshape(-1, 1, 2)
        fill = overlay.copy()
        cv2.fillPoly(fill, [pts], (0, 200, 0))
        cv2.addWeighted(fill, 0.10, overlay, 0.90, 0, overlay)
        color = (0, 200, 0) if c["status"] in ("approved", "modified") else (0, 255, 128)
        cv2.polylines(overlay, [pts], True, color, 3)
        cx = int(np.mean(pts[:, 0, 0]))
        cy = int(np.mean(pts[:, 0, 1]))
        cv2.putText(overlay, f"C{c['id']}", (cx - 20, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
    overlay_path = str(out_path / "overlay_approved.jpg")
    cv2.imwrite(overlay_path, overlay, [cv2.IMWRITE_JPEG_QUALITY, OVERLAY_JPEG_QUALITY])
    log.info("Wrote %s", overlay_path)

    return {
        "json": json_path,
        "mask": mask_path,
        "overlay": overlay_path,
        "cloud_count": len(production),
        "warnings": warnings,
    }


# ═══════════════════════════════════════════════════════════════════════════
# HTTP server
# ═══════════════════════════════════════════════════════════════════════════

def load_editor_html():
    """Load the editor HTML from the editor/ directory next to this script."""
    here = Path(__file__).resolve().parent
    html_path = here / "editor" / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"Editor HTML not found at {html_path}. "
        "Ensure editor/index.html exists next to step2c_cloud_editor.py."
    )


class EditorServer:
    def __init__(self, editor_data, overlay_b64, img_bgr, cloud_data,
                 scale_j2i, out_dir, port):
        self.editor_data = editor_data
        self.overlay_b64 = overlay_b64
        self.img_bgr = img_bgr
        self.cloud_data = cloud_data
        self.scale_j2i = scale_j2i
        self.out_dir = out_dir
        self.port = port
        self.httpd = None
        self.saved = False

    def serve_forever(self):
        handler = self._make_handler()
        # Port fallback
        last_err = None
        for p in range(self.port, self.port + 11):
            try:
                self.httpd = HTTPServer(("localhost", p), handler)
                self.port = p
                break
            except OSError as e:
                last_err = e
                continue
        if self.httpd is None:
            raise RuntimeError(f"No free port in range {self.port}-{self.port+10}: {last_err}")
        self.httpd.serve_forever()

    def shutdown(self):
        if self.httpd:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def _make_handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    try:
                        html = load_editor_html()
                        self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
                    except FileNotFoundError as e:
                        self._send(500, "text/plain", str(e).encode())
                elif self.path == "/api/data":
                    self._json(srv.editor_data)
                elif self.path == "/api/overlay":
                    self._json({"overlay_b64": srv.overlay_b64})
                else:
                    self.send_error(404)

            def do_POST(self):
                if self.path != "/api/save":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_POST_BYTES:
                    self._json({"status": "error", "message": "Payload too large"}, 413)
                    return
                body = self.rfile.read(length)
                try:
                    payload = json.loads(body)
                    result = generate_all_outputs(
                        payload, srv.img_bgr, srv.cloud_data,
                        srv.scale_j2i, srv.editor_data["display_scale"], srv.out_dir)
                    srv.saved = True
                    self._json({"status": "ok", "result": {
                        k: v for k, v in result.items()}})
                    print(f"\n{'='*60}")
                    print(f"  Approved clouds saved to: {srv.out_dir}/")
                    print(f"    approved_clouds.json   ({result['cloud_count']} clouds)")
                    print(f"    cloud_mask_approved.png")
                    print(f"    overlay_approved.jpg")
                    print(f"{'='*60}")
                    srv.shutdown()
                except Exception as e:  # noqa
                    log.exception("Save failed")
                    self._json({"status": "error", "message": str(e)}, 500)

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
                self._send(code, "application/json", body)

            def log_message(self, *a):
                pass

        return Handler


# ═══════════════════════════════════════════════════════════════════════════
# CLI / main
# ═══════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="Interactive revision cloud editor (step2c)")
    p.add_argument("--image", required=True, help="P&ID drawing (JPG/PNG)")
    p.add_argument("--clouds", required=True, help="outer_clouds_v2.json from step2b")
    p.add_argument("--out", default="output", help="Output directory")
    p.add_argument("--port", type=int, default=8765, help="HTTP port (default 8765)")
    p.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    p.add_argument("--overlay", help="Optional overlay_v2.jpg for toggle view")
    return p.parse_args()


def main():
    args = parse_args()

    if not os.path.exists(args.image):
        log.error("Image not found: %s", args.image)
        sys.exit(1)
    if not os.path.exists(args.clouds):
        log.error("Clouds JSON not found: %s", args.clouds)
        sys.exit(1)
    os.makedirs(args.out, exist_ok=True)

    img_bgr = cv2.imread(args.image)
    if img_bgr is None:
        log.error("Cannot read image: %s", args.image)
        sys.exit(1)
    img_h, img_w = img_bgr.shape[:2]

    with open(args.clouds) as f:
        cloud_data = json.load(f)
    json_w, json_h = cloud_data["stats"]["image_size"]
    scale_j2i = img_w / json_w

    log.info("Image %dx%d | JSON space %dx%d | scale J2I=%.5f",
             img_w, img_h, json_w, json_h, scale_j2i)

    editor_data = prepare_editor_data(img_bgr, cloud_data, scale_j2i)

    overlay_b64 = None
    if args.overlay and os.path.exists(args.overlay):
        ov = cv2.imread(args.overlay)
        if ov is not None:
            overlay_b64 = encode_image_b64(ov)
            log.info("Loaded overlay for toggle view")

    server = EditorServer(editor_data, overlay_b64, img_bgr, cloud_data,
                          scale_j2i, args.out, args.port)

    # Resolve port before opening browser
    # (serve_forever picks a free port; do a quick bind test here)
    url = f"http://localhost:{args.port}"
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    print(f"\n{'='*60}")
    print(f"  Cloud Editor — {len(editor_data['clouds'])} clouds loaded")
    print(f"  Open: {url}")
    print(f"  (auto-opening browser; Ctrl+C to cancel without saving)")
    print(f"{'='*60}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCancelled — no files saved.")
        sys.exit(0)

    if not server.saved:
        print("Editor closed without saving.")


if __name__ == "__main__":
    main()
