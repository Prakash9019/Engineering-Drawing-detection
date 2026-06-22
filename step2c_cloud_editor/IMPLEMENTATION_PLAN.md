STEP 2C — INTERACTIVE REVISION CLOUD CORRECTION EDITOR
COMPLETE PRODUCTION IMPLEMENTATION SPECIFICATION v2.0
══════════════════════════════════════════════════════════
#
This is the SINGLE AUTHORITATIVE document for implementing step2c.
Hand this entire file to Cursor / Claude Code in a terminal session.
Every file, function, algorithm, edge case, and test is specified.
#
Audit status: Cross-checked against original spec — 0 gaps remaining.
#
══════════════════════════════════════════════════════════


┌─────────────────────────────────────────────────────────┐
│  TABLE OF CONTENTS                                      │
├─────────────────────────────────────────────────────────┤
│  §1  PURPOSE & TERMINAL USAGE                           │
│  §2  DATA CONTRACTS (coordinates, files, schemas)       │
│  §3  PROJECT STRUCTURE                                  │
│  §4  PYTHON BACKEND (complete function specs)           │
│  §5  FRONTEND EDITOR (complete tool specs)              │
│  §6  SCALLOP CURVE GENERATOR (algorithm)                │
│  §7  MERGE TOOL (algorithm)                             │
│  §8  EXTEND CLOUD TOOL (algorithm)                      │
│  §9  VERTEX EDITOR (algorithm)                          │
│  §10 SEGMENT OPERATIONS (algorithm)                     │
│  §11 VALIDATION ENGINE                                  │
│  §12 EXPORT & OUTPUT GENERATION                         │
│  §13 DOWNSTREAM INTEGRATION (5A, 5B, 5C, tags)         │
│  §14 EDGE CASES & ERROR HANDLING                        │
│  §15 IMPLEMENTATION PHASES (step-by-step for terminal)  │
│  §16 TESTING CHECKLIST                                  │
└─────────────────────────────────────────────────────────┘


══════════════════════════════════════════════════════════
§1  PURPOSE & TERMINAL USAGE
══════════════════════════════════════════════════════════

What this system does:
  Step2b auto-detects revision clouds (~90-95% accuracy).
  Step2c (THIS) lets a human review, correct, and approve them.
  The approved clouds become THE ground truth for all downstream steps.
#
Why browser-based:
  - Canvas handles 8000×5650 images with smooth zoom/pan
  - No tkinter/Qt dependency
  - Polygon editing is dramatically easier in JS Canvas
  - Python handles the heavy computation (OpenCV mask, coordinate transforms)
#
Terminal commands:
#
  Full workflow:
  python step2c_cloud_editor.py \
      --image input_drawing.jpg \
      --clouds output/outer_clouds_v2.json \
      --out output/
#
  With overlay toggle:
  python step2c_cloud_editor.py \
      --image input_drawing.jpg \
      --clouds output/outer_clouds_v2.json \
      --overlay output/overlay_v2.jpg \
      --out output/
#
  Custom port, no auto-open:
  python step2c_cloud_editor.py \
      --image input_drawing.jpg \
      --clouds output/outer_clouds_v2.json \
      --out output/ \
      --port 9000 --no-browser
#
What happens:
  1. Python loads image + cloud JSON
  2. Transforms coordinates (JSON space → display space)
  3. Starts HTTP server on localhost:8765
  4. Opens browser to the editor
  5. User reviews, adds, deletes, merges, edits clouds
  6. User clicks Done
  7. Editor POSTs approved data to Python
  8. Python generates 3 output files + validates
  9. Server shuts down, script exits
#
Output files:
  output/approved_clouds.json      → ground truth for all downstream steps
  output/cloud_mask_approved.png   → binary mask at image resolution
  output/overlay_approved.jpg      → visual verification


══════════════════════════════════════════════════════════
§2  DATA CONTRACTS
══════════════════════════════════════════════════════════

── 2.1  COORDINATE SYSTEMS ─────────────────────────────
#
THIS IS THE ROOT CAUSE OF THE V1 FAILURE. Get this wrong and
every cloud appears in the wrong position.
#
Two coordinate spaces exist:
#
  JSON SPACE:   9934 × 7017 px
    - Used by: outer_clouds_v2.json polygon/bbox values
    - Used by: step5a consumption (load_cloud_regions_from_step2b)
    - This is the resolution the detector ran at
#
  IMAGE SPACE:  8000 × 5650 px
    - Used by: input_drawing.jpg, overlay_v2.jpg, cloud_mask_v2.png
    - Used by: mask generation (cv2.fillPoly)
    - This is the resolution of all actual image files
#
Conversion factors:
  SCALE_J2I = 8000 / 9934 = 0.80534   (JSON → Image)
  SCALE_I2J = 9934 / 8000 = 1.24175   (Image → JSON)
#
The editor adds a third space:
#
  DISPLAY SPACE: 2400 × ~1694 px
    - The downscaled image sent to the browser for rendering
    - DISPLAY_SCALE = 2400 / 8000 = 0.3
#
Conversion chain:
  JSON ──(×0.80534)──→ Image ──(×0.3)──→ Display
  Display ──(÷0.3)──→ Image ──(÷0.80534)──→ JSON
#
RULES:
  - Editor canvas works in DISPLAY SPACE for all rendering & interaction
  - On export: display → image (for mask), display → JSON (for JSON file)
  - Unchanged clouds: use ORIGINAL polygon_json (no precision loss)
  - Modified/added clouds: recompute JSON coords from display coords
  - The scale factors are COMPUTED at runtime from actual file dimensions,
    NEVER hardcoded (different drawings may have different ratios)

── 2.2  INPUT FILES ────────────────────────────────────
#
File                    │ Resolution  │ Format      │ Source
────────────────────────┼─────────────┼─────────────┼──────────
input_drawing.jpg       │ 8000×5650   │ RGB JPEG    │ P&ID drawing
outer_clouds_v2.json    │ N/A (data)  │ JSON        │ Step2b output
overlay_v2.jpg          │ 8000×5650   │ RGB JPEG    │ Step2b (optional)
cloud_mask_v2.png       │ 8000×5650   │ RGB/Gray PNG│ Step2b (optional)

── 2.3  INPUT JSON SCHEMA (outer_clouds_v2.json) ──────
#
{
  "clouds": [
    {
      "id": 1,
      "tag": "outer",              ← only "outer" clouds loaded into editor
      "source": "stage1_opencv",    ← or "gemini_snap"
      "confidence": 3.656,
      "bbox": [2741, 280, 9793, 5792],   ← JSON SPACE coords [x0,y0,x1,y1]
      "area": 19236637.5,
      "scallopedness": 4.656,
      "polygon": [[x,y], [x,y], ...]     ← JSON SPACE coords, 28–4789 points
    },
    ...
  ],
  "stats": {
    "total": 43,
    "outer": 23,
    "inner": 20,
    "image_size": [9934, 7017]     ← THIS defines JSON SPACE dimensions
  }
}

── 2.4  OUTPUT JSON SCHEMA (approved_clouds.json) ─────
#
MUST be backward-compatible with step5a's load_cloud_regions_from_step2b().
That function reads: clouds[].bbox, clouds[].polygon, clouds[].id, clouds[].tag
#
{
  "clouds": [
    {
      "id": 1,
      "tag": "outer",
      "source": "stage1_opencv",
      "confidence": 1.0,
      "bbox": [x0, y0, x1, y1],        ← JSON SPACE
      "area": 123456.7,                 ← JSON SPACE area (px²)
      "scallopedness": 2.34,
      "polygon": [[x,y], ...],          ← JSON SPACE, integer coordinates
      "status": "approved",             ← approved | added | modified | merged
      "mask_bbox": [x0,y0,x1,y1],      ← IMAGE SPACE bbox for per-cloud mask
      "mask_rle": "base64..."           ← run-length-encoded per-cloud mask (optional)
    }
  ],
  "stats": {
    "total": 22,
    "outer": 22,
    "inner": 0,
    "image_size": [9934, 7017]          ← preserved from input for step5a compat
  },
  "edit_metadata": {
    "workflow": "human_in_the_loop",
    "editor_version": "2c-v1.0",
    "original_auto_detected": 23,
    "approved_unchanged": 19,
    "deleted": 3,
    "added": 2,
    "modified": 1,
    "merged": 0,
    "image_file_size": [8000, 5650]
  }
}

── 2.5  OUTPUT MASK (cloud_mask_approved.png) ──────────
#
- Resolution: SAME as input image (8000×5650)
- Format: single-channel grayscale PNG (NOT RGB)
- Values: 0 = outside clouds, 255 = inside approved cloud region
- Generated by: cv2.fillPoly for each approved cloud's image-space polygon
- This is the COMBINED mask (all clouds merged into one image)

── 2.6  OUTPUT OVERLAY (overlay_approved.jpg) ──────────
#
- Resolution: SAME as input image (8000×5650)
- Format: RGB JPEG, quality 92
- Content: input drawing + green outlines for approved clouds +
           cloud ID labels at centroids + semi-transparent green fill


══════════════════════════════════════════════════════════
§3  PROJECT STRUCTURE
══════════════════════════════════════════════════════════

step2c_cloud_editor.py       ← SINGLE Python file: CLI + server + output gen
                                 (~500 lines)
                                 The editor HTML is EMBEDDED as a string constant
                                 inside this file (no separate HTML file needed)
                                 This makes deployment trivial: one file, copy & run.
#
Alternative: if the HTML grows beyond ~1500 lines, split into:
  step2c_cloud_editor.py     ← Python backend (~400 lines)
  editor/index.html          ← Frontend (~1200 lines)
#
Zero external dependencies beyond what's already installed:
  Python: cv2, numpy (already in pipeline env)
  Frontend: nothing (pure HTML/CSS/JS, no React/npm/CDN)


══════════════════════════════════════════════════════════
§4  PYTHON BACKEND — COMPLETE FUNCTION SPECIFICATIONS
══════════════════════════════════════════════════════════

── 4.1  CLI ────────────────────────────────────────────
#
def parse_args() -> argparse.Namespace:
    """
    Arguments:
      --image     (required) Path to P&ID drawing JPG/PNG
      --clouds    (required) Path to outer_clouds_v2.json
      --out       (default: "output") Output directory
      --port      (default: 8765) HTTP server port
      --no-browser  Don't auto-open browser
      --overlay   (optional) Path to overlay_v2.jpg for toggle view
    """

── 4.2  MAIN ENTRY POINT ──────────────────────────────
#
def main():
    """
    1. Parse args
    2. Validate input files exist
    3. Load image with cv2.imread → get IMG_W, IMG_H
    4. Load JSON → get JSON_W, JSON_H from stats.image_size
    5. Compute SCALE_J2I = IMG_W / JSON_W
    6. Call prepare_editor_data()
    7. Optionally load overlay image
    8. Create EditorServer instance
    9. Auto-open browser (unless --no-browser)
    10. Print startup banner
    11. server.serve_forever() — blocks until Done or Ctrl+C
    """

── 4.3  DATA PREPARATION ──────────────────────────────
#
def prepare_editor_data(img_bgr, cloud_data, scale_j2i) -> dict:
    """
    Transforms all data into what the browser editor needs.
#
    Steps:
      1. Compute display_scale = 2400 / IMG_W
      2. Resize image to 2400px wide, encode as base64 JPEG (quality 75)
      3. For each outer cloud in cloud_data['clouds']:
         a. Transform polygon from JSON space → image space (× scale_j2i)
         b. Simplify polygon for display (keep max 120 points, using
            uniform subsampling — NOT RDP, which distorts curves)
         c. Transform simplified polygon to display space (× display_scale)
         d. Compute display bbox from display polygon
         e. Store: polygon (display), polygonImg (image), polygonJson (original)
      4. Return payload dict
#
    Returns:
      {
        'image_b64': str,          base64 JPEG
        'display_w': int,          2400
        'display_h': int,          ~1694
        'image_w': int,            8000
        'image_h': int,            5650
        'json_w': int,             9934
        'json_h': int,             7017
        'scale_j2i': float,        0.80534
        'display_scale': float,    0.3
        'clouds': [                list of cloud dicts
          {
            'id': int,
            'tag': 'outer',
            'source': str,
            'confidence': float,
            'scallopedness': float,
            'areaJson': float,
            'bbox': [x0,y0,x1,y1],        display space
            'bboxImg': [x0,y0,x1,y1],     image space
            'polygon': [[x,y], ...],       display space (simplified)
            'polygonImg': [[x,y], ...],    image space (simplified)
            'polygonJson': [[x,y], ...],   JSON space (ORIGINAL FULL)
            'pointCount': int,             original full point count
          }
        ]
      }
    """

── 4.4  HTTP SERVER ────────────────────────────────────
#
class EditorServer:
    """
    Endpoints:
      GET  /              → serves editor HTML
      GET  /api/data      → returns JSON payload from prepare_editor_data()
      GET  /api/overlay   → returns overlay base64 (or null)
      POST /api/save      → receives approved clouds, generates outputs, shuts down
#
    The /api/save handler:
      1. Receives JSON payload from browser
      2. Calls validate_clouds() — raises on invalid data
      3. Calls generate_all_outputs()
      4. Returns success response
      5. Schedules server shutdown (in background thread)
#
    Error handling:
      - Port in use: try port, port+1, ..., port+10
      - Save failure: return error JSON, don't shut down, let user retry
      - Ctrl+C: clean exit, no files saved
    """

── 4.5  VALIDATION ENGINE ─────────────────────────────
#
def validate_clouds(clouds: list) -> list[str]:
    """
    Validate approved cloud list before generating outputs.
    Returns list of warning strings (empty = all good).
    Raises ValueError for critical errors.
#
    Checks:
      1. Each cloud has 'polygon' with >= 3 points
      2. Each polygon point is [x, y] with numeric values
      3. No polygon has zero area (all points collinear)
      4. Bbox is consistent with polygon bounds (within 5px tolerance)
      5. All coordinates are within image bounds (with 50px margin)
      6. No duplicate cloud IDs
      7. At least 1 cloud in the approved list (warn if 0)
    """

── 4.6  OUTPUT GENERATION ──────────────────────────────
#
def generate_all_outputs(payload, img_bgr, original_cloud_data,
                         scale_j2i, display_scale, out_dir) -> dict:
    """
    Generate all 3 output files from approved cloud list.
#
    Steps:
      1. For each cloud in payload['clouds']:
         a. Determine best-quality polygon coordinates:
            - If status='approved' (unchanged): use polygonJson (original)
            - If status='modified'|'merged': recompute from display coords
            - If status='added': recompute from display coords
         b. Compute polygon in image space (for mask)
         c. Compute polygon in JSON space (for JSON file)
         d. Compute bbox, area, scallopedness from JSON polygon
#
      2. WRITE approved_clouds.json:
         - clouds[] with all fields from §2.4
         - stats{} with counts and image_size = [9934, 7017]
         - edit_metadata{} with change summary
#
      3. WRITE cloud_mask_approved.png:
         - np.zeros((IMG_H, IMG_W), dtype=np.uint8)
         - For each cloud: cv2.fillPoly(mask, [pts_img], 255)
         - cv2.imwrite as single-channel PNG
#
      4. WRITE overlay_approved.jpg:
         - Copy input image
         - For each cloud:
           a. Semi-transparent green fill (addWeighted, alpha=0.10)
           b. Green outline (polylines, thickness=3)
           c. Cloud ID label at centroid
         - cv2.imwrite as JPEG quality 92
#
    Returns:
      { 'json': path, 'mask': path, 'overlay': path, 'cloud_count': int }
    """

── 4.7  PER-CLOUD MASK (for JSON field) ───────────────
#
def compute_per_cloud_mask(poly_img, bbox_img, img_shape) -> dict:
    """
    Generate a per-cloud mask cropped to the cloud's bounding box.
#
    Used for the 'mask_bbox' and 'mask_rle' fields in the output JSON.
    Step5a can use this for precise point-in-polygon testing without
    needing the full combined mask.
#
    Steps:
      1. Crop region: bbox_img with 10px padding
      2. Create local mask at crop size
      3. cv2.fillPoly with polygon translated to crop origin
      4. Run-length encode the mask (list of [start, length] pairs)
      5. Base64 encode the RLE data
#
    Returns:
      {
        'mask_bbox': [x0, y0, x1, y1],  image space, with padding
        'mask_rle': 'base64...',          RLE-encoded mask data
      }
    """


══════════════════════════════════════════════════════════
§5  FRONTEND EDITOR — COMPLETE SPECIFICATION
══════════════════════════════════════════════════════════

── 5.1  LAYOUT ─────────────────────────────────────────
#
┌──────────────────────────────────────────────────────────────────┐
│  HEADER:  ⊕ Cloud Editor  │  Clouds: 23  Added: 0  Del: 0     │
│                            │  [Reset] [Undo] [Redo] [✓ Done]   │
├────┬─────────────────────────────────────────────────────┬──────┤
│ T  │                                                     │  S   │
│ O  │                                                     │  I   │
│ O  │                                                     │  D   │
│ L  │              CANVAS AREA                            │  E   │
│ B  │              (drawing + cloud overlays)             │  B   │
│ A  │                                                     │  A   │
│ R  │                                                     │  R   │
│    │                                                     │      │
│ 48 │                                                     │ 280  │
│ px │                                                     │  px  │
├────┴─────────────────────────────────────────────────────┴──────┤
│  STATUS: Tool: Select — Click a cloud.  │  x: 4521  y: 2890   │
└──────────────────────────────────────────────────────────────────┘

── 5.2  TOOLBAR (left side, 48px wide) ─────────────────
#
Button │ Key │ Tool          │ Cursor
───────┼─────┼───────────────┼──────────
✋     │  1  │ Pan           │ grab / grabbing
☞      │  2  │ Select        │ pointer / crosshair
▭+     │  3  │ Add Rectangle │ crosshair
✎+     │  4  │ Add Lasso     │ crosshair
⬠+     │  5  │ Add Polygon   │ crosshair
↔      │  6  │ Edit Vertices │ pointer / move
⊕      │  7  │ Merge Clouds  │ crosshair
✂      │  8  │ Extend Cloud  │ crosshair
🗑     │  9  │ Delete        │ crosshair
───────┼─────┼───────────────┼──────────
🔍+    │  +  │ Zoom In       │
🔍-    │  -  │ Zoom Out      │
⊡      │  F  │ Fit to View   │
👁     │  O  │ Toggle Overlay│

── 5.3  GLOBAL STATE ───────────────────────────────────
#
const STATE = {
    // Data
    clouds: [],              // Array of CloudObject (mutable)
    imageB64: '',            // Base64 drawing image for canvas
    overlayB64: null,        // Base64 overlay (optional)
    displayW: 2400,          // Display image width
    displayH: 1694,          // Display image height
    imageW: 8000,            // Full image width
    imageH: 5650,            // Full image height
    jsonW: 9934,             // JSON coord width
    jsonH: 7017,             // JSON coord height
    scaleJ2I: 0.80534,       // JSON→Image
    displayScale: 0.3,       // Display→Image (display_w / image_w)
#
    // View state
    zoom: 1.0,
    panX: 0, panY: 0,
    showOverlay: false,
#
    // Tool state
    currentTool: 'select',
    selectedIds: [],          // Array for multi-select (merge needs 2+)
    hoveredId: null,
    isDragging: false,
    spaceHeld: false,
#
    // Draw-in-progress state
    drawPoints: [],           // For lasso / polygon drawing
    drawRect: null,           // For rectangle drawing {x1,y1,x2,y2}
#
    // Edit state
    editVertexIdx: null,      // Index of vertex being dragged
    editCPStep: 1,            // Control point subsampling step
#
    // Extend state
    extendTargetId: null,     // Cloud being extended
    extendPoints: [],         // New region being drawn for extension
#
    // History
    undoStack: [],
    redoStack: [],
#
    // Counters
    nextId: 100,
    addedCount: 0,
    deletedCount: 0,
    modifiedCount: 0,
    mergedCount: 0,
};

── 5.4  CLOUD OBJECT (in editor state) ────────────────
#
{
    id: 1,
    source: 'stage1_opencv',      // original detection source
    confidence: 0.85,
    scallopedness: 2.34,
    areaJson: 346465.5,
    pointCount: 298,              // original polygon point count
#
    status: 'approved',           // approved | added | modified | merged | deleted
#
    polygon: [[x,y], ...],       // DISPLAY SPACE — used for rendering
    bbox: [x0,y0,x1,y1],        // DISPLAY SPACE — used for quick hit test
#
    polygonImg: [[x,y], ...],    // IMAGE SPACE — used for mask export
    polygonJson: [[x,y], ...],   // JSON SPACE — ORIGINAL from detection
                                 //   null for user-added clouds
#
    isUserAdded: false,
    isModified: false,
    isMerged: false,
    visible: true,
}

── 5.5  COORDINATE TRANSFORMS (JavaScript) ────────────
#
// Display ↔ Screen (canvas pixels)
function worldToScreen(wx, wy):
    return [wx * zoom + panX, wy * zoom + panY]
#
function screenToWorld(sx, sy):
    return [(sx - panX) / zoom, (sy - panY) / zoom]
#
// Display → Image (for export/mask)
function displayToImage(dx, dy):
    s = 1.0 / STATE.displayScale   // e.g. 1/0.3 = 3.333
    return [Math.round(dx * s), Math.round(dy * s)]
#
// Display → JSON (for export)
function displayToJson(dx, dy):
    s = 1.0 / (STATE.displayScale * STATE.scaleJ2I)
    return [Math.round(dx * s), Math.round(dy * s)]

── 5.6  CANVAS RENDERER ───────────────────────────────
#
function render():
    1. Clear canvas (dark background)
    2. ctx.save() → apply zoom/pan transform
    3. Draw background image (or overlay if toggled)
    4. For each visible non-deleted non-selected cloud:
       - drawCloudPolygon() with Catmull-Rom smooth curves
       - Color: blue (auto-detected), green (user-added), orange (modified)
    5. Draw selected cloud(s) on top:
       - Color: red highlight
       - If edit tool active: draw control points
    6. ctx.restore()
    7. Draw tool overlays in screen space:
       - Rectangle preview (add rect tool)
       - Lasso path (add lasso tool)
       - Polygon vertices (add polygon tool)
       - Extend region preview (extend tool)
#
POLYGON RENDERING — Catmull-Rom Spline:
#
function drawCloudPolygon(ctx, polygon, strokeColor, fillColor, lineWidth):
    """
    Render polygon as SMOOTH CURVES through all points.
    NOT straight lines between points.
#
    Uses Catmull-Rom → Cubic Bezier conversion:
      For each consecutive triplet p0, p1, p2, p3:
        cp1 = p1 + (p2 - p0) / 6
        cp2 = p2 - (p3 - p1) / 6
        ctx.bezierCurveTo(cp1, cp2, p2)
#
    This produces smooth curves that pass through every polygon vertex,
    matching the scalloped appearance of real revision clouds.
    """
#
CLOUD LABELS:
  - Render "C{id}" at polygon centroid
  - Font size: 12px / zoom (constant screen size)
  - Color matches cloud stroke color
  - Only show when zoom level > 0.3 (hide when zoomed out far)
#
COLOR SCHEME:
  Status          │ Stroke    │ Fill (alpha)
  ────────────────┼───────────┼──────────────
  Auto-detected   │ #4fc3f7   │ rgba(79,195,247, 0.06)
  User-added      │ #2ecc71   │ rgba(46,204,113, 0.12)
  Modified        │ #f39c12   │ rgba(243,156,18, 0.10)
  Merged          │ #9b59b6   │ rgba(155,89,182, 0.10)
  Selected        │ #e94560   │ rgba(233,69,96, 0.15)
  Hovered         │ +glow     │ same + brighter fill


══════════════════════════════════════════════════════════
§6  SCALLOP CURVE GENERATOR
══════════════════════════════════════════════════════════

This is the core algorithm that makes added clouds look like REAL
revision clouds instead of rectangles or jagged freehand shapes.

── 6.1  RECTANGLE → SCALLOPED CLOUD ───────────────────
#
function rectToScallopedCloud(x1, y1, x2, y2):
    """
    Convert a rectangle to a revision-cloud-shaped boundary.
#
    Algorithm:
      1. Create 4 corners of the rectangle (clockwise winding)
      2. For each edge (top, right, bottom, left):
         a. Compute edge length
         b. Calculate how many scallop arcs fit: numArcs = edgeLen / (arcRadius * 2)
         c. For each arc position along the edge:
            - Compute arc center (on the edge)
            - Compute outward normal direction
            - Generate 8 points on a semicircle bulging outward
         d. Collect all arc points in order
      3. Return the scalloped polygon
#
    Parameters:
      arcRadius = 8 display pixels (~27 JSON pixels, ~22 image pixels)
                  This matches typical revision cloud arc size on P&ID drawings
      pointsPerArc = 8 (smooth enough for visual quality)
#
    Visual result:
      Input:  ┌──────────────┐
              │              │
              │              │
              └──────────────┘
#
      Output: ╔~~~~~~~~~~~~~~╗     ← arcs bump outward
              ║              ║
              ║              ║
              ╚~~~~~~~~~~~~~~╝
    """

── 6.2  FREEHAND LASSO → SMOOTHED CLOUD ───────────────
#
function lassoToCloudBoundary(rawPoints):
    """
    Convert freehand lasso path to a clean cloud boundary.
#
    Algorithm:
      1. Remove duplicate/nearby points (min distance: 2px)
      2. Ramer-Douglas-Peucker simplification (epsilon: 3px)
      3. Apply scallop arcs along the simplified boundary
         (same generateScallopedBoundary as rectangle uses)
      4. Return scalloped polygon
#
    If user toggled "smooth only" (no scallops):
      Skip step 3, just return simplified smooth polygon
    """

── 6.3  POLYGON → CLOUDIFIED ──────────────────────────
#
function cloudifyPolygon(polygon):
    """
    Add scallop arcs to any arbitrary polygon.
    Called when user finishes click-to-place polygon and hits "Cloudify".
    Same algorithm as §6.1 step 2, applied to arbitrary edges.
    """

── 6.4  CORE SCALLOP ALGORITHM ────────────────────────
#
function generateScallopedBoundary(polygon, arcRadius, pointsPerArc):
    """
    For each edge of the polygon:
      1. Compute edge vector: dx = p2.x - p1.x, dy = p2.y - p1.y
      2. Compute edge length: L = sqrt(dx² + dy²)
      3. Compute outward normal: nx = -dy/L, ny = dx/L
         (assumes clockwise winding; for CCW, negate)
      4. Number of arcs: N = max(1, round(L / (arcRadius * 2)))
      5. For each arc a = 0..N-1:
         - Arc spans from t = a/N to t = (a+1)/N along the edge
         - For each point j = 0..pointsPerArc-1:
           - angle = π × j / (pointsPerArc - 1)
           - t = a/N + (1/N) × j/(pointsPerArc-1)
           - baseX = p1.x + dx × t
           - baseY = p1.y + dy × t
           - bump = sin(angle) × arcRadius
           - resultX = baseX + nx × bump
           - resultY = baseY + ny × bump
           - Add [resultX, resultY] to output
    """


══════════════════════════════════════════════════════════
§7  MERGE TOOL
══════════════════════════════════════════════════════════

This was MISSING from the v1 plan. The original spec requires:
"Merge cloud fragments" — combining 2+ clouds into one.

── 7.1  MERGE WORKFLOW ─────────────────────────────────
#
1. User switches to Merge tool (key 7)
2. User clicks first cloud → it gets selected (red highlight)
3. User clicks second cloud → both are now selected
   - Visual: both highlighted, dashed line connecting centroids
4. User clicks "Merge" button in sidebar (or presses Enter)
5. System merges the two clouds into one:
   a. Compute convex hull of both polygons combined
      OR union polygon (if they overlap)
   b. Optionally cloudify the merged boundary
6. Original two clouds marked as deleted
7. New merged cloud created with source='manual_merge'

── 7.2  MERGE ALGORITHM ───────────────────────────────
#
function mergeClouds(cloudA, cloudB):
    """
    Merge two clouds into a single cloud.
#
    Strategy (depends on overlap):
#
    Case A — Clouds OVERLAP (any polygon points of A inside B or vice versa):
      1. Rasterize both polygons onto a temporary canvas
      2. OR the two raster masks
      3. Find contour of the combined mask (cv2.findContours equivalent in JS)
      4. The outer contour becomes the merged polygon
#
    Case B — Clouds are SEPARATE (no overlap):
      1. Compute convex hull of all points from both polygons
      2. This creates a single enclosing boundary
      3. Optionally: shrink-wrap (concave hull) for tighter fit
#
    Simpler JS-only approach (no rasterization):
      1. Concatenate all points from both polygons
      2. Compute convex hull using Graham scan / Andrew's monotone chain
      3. Return hull as the merged polygon
#
    Returns: new polygon (display space)
    """

── 7.3  MULTI-SELECT FOR MERGE ────────────────────────
#
When merge tool is active:
  - STATE.selectedIds is an ARRAY (not single ID)
  - Clicking a cloud toggles it in/out of the selection
  - Sidebar shows "Selected for merge: C5, C9"
  - "Merge Selected" button enabled when 2+ clouds selected
  - Can merge 3+ clouds at once (all combined into one)


══════════════════════════════════════════════════════════
§8  EXTEND CLOUD TOOL
══════════════════════════════════════════════════════════

This was MISSING from the v1 plan. The original spec requires:
"Add disconnected cloud sections" and "Extend cloud regions"

── 8.1  EXTEND WORKFLOW ────────────────────────────────
#
1. User selects a cloud (with Select tool or click)
2. User switches to Extend tool (key 8)
3. Status bar shows: "Draw a region to extend C{id}"
4. User draws a region (lasso or rectangle) NEAR the selected cloud
5. On mouse-up:
   a. The drawn region is converted to a polygon (with scallop arcs)
   b. This new polygon is MERGED into the existing cloud
      (using the merge algorithm from §7.2)
   c. The existing cloud's polygon is replaced with the merged result
   d. Cloud marked as status='modified'
#
This is different from "Add" (which creates a NEW cloud):
  Add:    creates cloud C100 (new, independent)
  Extend: modifies cloud C5 (adds area to existing cloud)

── 8.2  EXTEND ALGORITHM ──────────────────────────────
#
function extendCloud(existingCloud, newRegionPolygon):
    """
    1. Combine points from existingCloud.polygon + newRegionPolygon
    2. Compute convex hull of combined points
       (OR: union via rasterize-OR-contour if overlap exists)
    3. Replace existingCloud.polygon with the merged polygon
    4. Recompute existingCloud.bbox
    5. Set existingCloud.status = 'modified'
    6. Set existingCloud.isModified = true
    7. Set existingCloud.polygonJson = null  (force recompute on export)
    """


══════════════════════════════════════════════════════════
§9  VERTEX EDITOR
══════════════════════════════════════════════════════════

── 9.1  CONTROL POINT DISPLAY ──────────────────────────
#
When Edit tool (key 6) is active AND a cloud is selected:
#
Cloud polygon points → control points displayed as circles
#
For clouds with many points:
  pointCount <= 80:  show ALL points as control points
  pointCount > 80:   show every Nth point where N = ceil(pointCount / 60)
                     (keeps max ~60 visible control points)
#
Control point appearance:
  Normal:  ○  white fill, red border, radius = 4px screen
  Hovered: ◉  yellow fill, red border, radius = 6px screen
  Dragged: ●  red fill, white border, radius = 6px screen
#
Hit radius: 10px screen (larger than visual for easy clicking)

── 9.2  VERTEX OPERATIONS ──────────────────────────────
#
MOVE VERTEX:
  - Click + drag a control point
  - Polygon updates in real-time as vertex moves
  - On mouse-up: commit the change, push undo
  - Cloud marked as modified
#
INSERT VERTEX:
  - Click on a polygon EDGE (between two control points)
  - New vertex inserted at the click position
  - Becomes immediately draggable
  - Edge detection: for each consecutive pair of control points,
    compute perpendicular distance from cursor to edge segment;
    if distance < 10px screen, that edge is "hit"
#
DELETE VERTEX:
  - Shift+click OR right-click on a control point
  - Vertex removed from polygon
  - Minimum 3 vertices enforced (cannot delete below 3)
  - Cloud marked as modified
#
After ANY vertex edit:
  - Recompute bbox from updated polygon
  - Set cloud.isModified = true
  - Set cloud.status = 'modified'
  - Set cloud.polygonJson = null (force recompute on export)
  - Push undo snapshot


══════════════════════════════════════════════════════════
§10  SEGMENT OPERATIONS
══════════════════════════════════════════════════════════

This was PARTIALLY MISSING from the v1 plan. The original spec
says "Edit segments" and "Remove individual segments".

── 10.1  WHAT IS A SEGMENT ─────────────────────────────
#
A "segment" = the polygon edge between two consecutive control points.
On a scalloped cloud, each segment roughly corresponds to one arc.

── 10.2  SEGMENT SELECTION ─────────────────────────────
#
In Edit tool mode, when hovering BETWEEN control points:
  - Highlight the nearest segment (the edge between two CPs)
  - Visual: segment turns yellow/thick

── 10.3  SEGMENT OPERATIONS ───────────────────────────
#
DELETE SEGMENT:
  - Shift+click on a highlighted segment
  - Removes all polygon points BETWEEN the two control points of that segment
  - Effectively "cuts" the cloud boundary at that segment
  - The two endpoints remain, so the polygon stays closed
    (straight line replaces the deleted arc segment)
#
RESHAPE SEGMENT:
  - Click + drag on a segment midpoint
  - Adjusts the curve of that segment (pushes it in or out)
  - Implementation: insert a new vertex at the drag position,
    positioned along the segment's normal direction
#
This is functionally sufficient. The user can:
  - Remove an incorrectly detected arc: delete that segment
  - Reshape a misaligned boundary: drag the segment


══════════════════════════════════════════════════════════
§11  VALIDATION ENGINE (frontend, before submit)
══════════════════════════════════════════════════════════

── 11.1  PRE-SAVE VALIDATION ───────────────────────────
#
function validateBeforeSave():
    """
    Run before sending data to /api/save.
    Shows warnings in the Done modal if issues found.
#
    Checks:
      1. At least 1 cloud exists (warn if 0)
      2. Each cloud has >= 3 polygon points (error if not)
      3. No cloud is absurdly small (< 20px display = ~67px image, warn)
      4. No cloud is absurdly large (> 90% of image area, warn)
      5. No two approved clouds are exact duplicates (warn)
#
    Returns: { errors: [...], warnings: [...] }
    If errors.length > 0: block save, show errors
    If warnings.length > 0: show warnings, allow save with confirmation
    """

── 11.2  SERVER-SIDE VALIDATION ────────────────────────
#
Python validate_clouds() runs as a second check after the browser.
See §4.5 for specification.


══════════════════════════════════════════════════════════
§12  EXPORT & DONE FLOW
══════════════════════════════════════════════════════════

── 12.1  DONE BUTTON FLOW ──────────────────────────────
#
1. User clicks [✓ Done] button
2. Frontend runs validateBeforeSave()
3. If errors → show error list, block
4. If warnings → show warning list, require confirm
5. Show Done modal with:
   - Summary: X approved, Y added, Z deleted, W modified, V merged
   - Output file list
   - JSON preview (scrollable textarea)
   - Buttons: [Continue Editing] [Copy JSON] [✓ Save & Exit]
#
6. User clicks [Save & Exit]
7. Frontend builds payload:
   {
     clouds: [
       {
         id, source, status, confidence,
         polygon: [[x,y]...],          // DISPLAY space
         polygonImg: [[x,y]...] | null, // IMAGE space (if available)
         polygonJson: [[x,y]...] | null, // JSON space (original only)
         isUserAdded, isModified, isMerged,
       }
     ],
     deleted_ids: [3, 7, 14],
     edit_summary: {
       original_count: 23,
       deleted: 3, added: 2, modified: 1, merged: 0,
     }
   }
#
8. POST to /api/save
9. Server validates, generates outputs, returns result
10. On success: show "Saved! You can close this tab. Files at output/"
    Server shuts down, Python script exits.
11. On error: show error message, allow retry

── 12.2  COORDINATE PRECISION ON EXPORT ────────────────
#
CRITICAL RULE: For unchanged clouds, ALWAYS use the original polygonJson.
Do NOT round-trip through display→image→JSON, which loses precision.
#
Decision tree per cloud:
#
  cloud.status == 'approved' AND cloud.polygonJson exists?
    YES → use cloud.polygonJson directly (zero precision loss)
    NO  → recompute:
          display coords → ÷displayScale → image coords
          image coords → ÷scaleJ2I → JSON coords
          round to integers


══════════════════════════════════════════════════════════
§13  DOWNSTREAM INTEGRATION
══════════════════════════════════════════════════════════

── 13.1  STEP 5A (CANDIDATE EXTRACTION) ───────────────
#
File: step5a_candidate_extraction.py
Change: 8 lines in the cloud-loading section
#
CURRENT CODE:
  clouds_path = os.path.join(out_dir, "outer_clouds_v2.json")
  mask_path = os.path.join(out_dir, "cloud_mask_v2.png")
#
NEW CODE:
  approved_json = os.path.join(out_dir, "approved_clouds.json")
  approved_mask = os.path.join(out_dir, "cloud_mask_approved.png")
  if os.path.exists(approved_json):
      log.info("Using APPROVED clouds (human-verified) from step2c")
      clouds_path = approved_json
      mask_path = approved_mask if os.path.exists(approved_mask) else mask_path
  else:
      log.info("No approved clouds found — using auto-detected from step2b")
      clouds_path = os.path.join(out_dir, "outer_clouds_v2.json")
      mask_path = os.path.join(out_dir, "cloud_mask_v2.png")
#
WHY THIS WORKS: The output JSON schema (§2.4) is backward-compatible.
load_cloud_regions_from_step2b() reads: clouds[].bbox, clouds[].polygon
These fields exist in both schemas.

── 13.2  STEP 5B (ASSOCIATION) ─────────────────────────
#
Step5b uses cloud regions from step5a's output (already filtered).
NO CHANGE NEEDED. Step5a passes through the cloud boundaries,
and 5b inherits whatever 5a loaded.

── 13.3  STEP 5C (VALIDATION) ─────────────────────────
#
Same as 5b — inherits from 5a's cloud data.
NO CHANGE NEEDED.

── 13.4  TAG EXTRACTION ───────────────────────────────
#
If tag extraction loads clouds independently (not via step5a):
  Apply the same 8-line patch as §13.1.
  Prefer approved_clouds.json, fall back to outer_clouds_v2.json.

── 13.5  INTEGRATION PRINCIPLE ─────────────────────────
#
The ONLY change needed is at the POINT OF LOADING cloud data.
Every downstream step that reads outer_clouds_v2.json should:
  1. Check if approved_clouds.json exists
  2. If yes, use it instead
  3. If no, use outer_clouds_v2.json (backward compatible)
#
This is a 4-line if/else. No schema changes. No API changes.


══════════════════════════════════════════════════════════
§14  EDGE CASES & ERROR HANDLING
══════════════════════════════════════════════════════════

── 14.1  DATA EDGE CASES ──────────────────────────────
#
Case                                  │ Handling
──────────────────────────────────────┼──────────────────────────────
Cloud C1 has 4789 polygon points      │ Simplify to 120 for display.
                                      │ Show 60 control points in edit.
                                      │ Keep full polygon for export.
──────────────────────────────────────┼──────────────────────────────
Cloud C1 covers 5679×4438 px (huge)   │ Allow it. Step2b validated it.
                                      │ Show area warning in sidebar.
──────────────────────────────────────┼──────────────────────────────
Overlapping clouds (C10 inside C9)    │ Hit test returns SMALLEST first
                                      │ (by area). User can toggle.
──────────────────────────────────────┼──────────────────────────────
User draws outside image bounds       │ Clamp coordinates to [0, displayW/H]
──────────────────────────────────────┼──────────────────────────────
User draws < 3 points (polygon)       │ Reject. Status: "Need 3+ points."
──────────────────────────────────────┼──────────────────────────────
Rectangle < 20px either dimension     │ Treat as click (select), not add.
──────────────────────────────────────┼──────────────────────────────
All clouds deleted                    │ Allow. Warn in Done modal.
──────────────────────────────────────┼──────────────────────────────
Browser tab closed without saving     │ beforeunload confirmation dialog.
──────────────────────────────────────┼──────────────────────────────
JSON has different image_size         │ Scale factors computed at runtime
than actual image dimensions          │ from actual values. Never hardcoded.
──────────────────────────────────────┼──────────────────────────────
Merging 2 non-overlapping clouds      │ Convex hull (connects them).
                                      │ Show preview before confirming.
──────────────────────────────────────┼──────────────────────────────
Extend draws far from target cloud    │ Allow. Merge algorithm handles it.
                                      │ Result may be large convex hull.

── 14.2  PERFORMANCE EDGE CASES ───────────────────────
#
Case                                  │ Handling
──────────────────────────────────────┼──────────────────────────────
43 clouds rendered simultaneously     │ No issue. Canvas is fast.
──────────────────────────────────────┼──────────────────────────────
4789-point cloud as Catmull-Rom       │ Pre-simplified to 120 points.
                                      │ 120 bezierCurveTo calls = fine.
──────────────────────────────────────┼──────────────────────────────
Rapid vertex dragging (edit tool)     │ requestAnimationFrame throttle.
──────────────────────────────────────┼──────────────────────────────
Large base64 image (~500KB)           │ Single load. Canvas caches it.
──────────────────────────────────────┼──────────────────────────────
POST body large (many edited clouds)  │ Max content length: 50MB.

── 14.3  SERVER EDGE CASES ────────────────────────────
#
Case                                  │ Handling
──────────────────────────────────────┼──────────────────────────────
Port 8765 in use                      │ Try 8766..8775 automatically.
──────────────────────────────────────┼──────────────────────────────
Save fails (disk full)                │ Return error JSON, don't shutdown.
──────────────────────────────────────┼──────────────────────────────
Browser refresh mid-edit              │ Reloads original data. Edits lost.
──────────────────────────────────────┼──────────────────────────────
Ctrl+C during editing                 │ Clean exit, no files written.
──────────────────────────────────────┼──────────────────────────────
Multiple browser tabs open            │ Only one can save. Server accepts
                                      │ first POST, ignores subsequent.

── 14.4  COORDINATE PRECISION ─────────────────────────
#
Case                                  │ Handling
──────────────────────────────────────┼──────────────────────────────
Unchanged cloud exported              │ Use ORIGINAL polygonJson.
                                      │ ZERO precision loss.
──────────────────────────────────────┼──────────────────────────────
Modified cloud exported               │ Recompute via display→JSON.
                                      │ Max error: ±1px at JSON resolution.
                                      │ Acceptable for human-edited clouds.
──────────────────────────────────────┼──────────────────────────────
User-added cloud exported             │ Recompute via display→JSON.
                                      │ Same ±1px tolerance.
──────────────────────────────────────┼──────────────────────────────
Float→int rounding in coordinates     │ Math.round() everywhere. Consistent.


══════════════════════════════════════════════════════════
§15  IMPLEMENTATION PHASES
══════════════════════════════════════════════════════════
#
These phases are designed for sequential implementation in a
terminal coding session (Cursor / Claude Code). Each phase
produces a testable result.

── PHASE 1: Python Backend Skeleton ────────────────────
#
Time: ~25 minutes
Files: step2c_cloud_editor.py
#
Implement:
  □ parse_args() — CLI with all flags
  □ main() — load image, load JSON, compute scales
  □ prepare_editor_data() — transform coords, encode image
  □ EditorServer class — serve placeholder HTML, /api/data, /api/save
  □ generate_all_outputs() — JSON + mask + overlay file generation
  □ validate_clouds() — pre-save validation
  □ compute_per_cloud_mask() — per-cloud RLE mask
  □ Port fallback (try port..port+10)
#
Test:
  python step2c_cloud_editor.py --image input_drawing.jpg \
      --clouds output/outer_clouds_v2.json --out test/ --no-browser
  curl http://localhost:8765/api/data | python -m json.tool | head -30
  Verify: clouds have display-space coords, image is base64
  Verify: scale factors are correct (0.80534, 0.3)

── PHASE 2: Editor HTML — Layout + Renderer ───────────
#
Time: ~35 minutes
Files: editor HTML (embedded in Python or separate file)
#
Implement:
  □ Full HTML layout: header, toolbar, canvas, sidebar, status bar
  □ CSS: dark theme, all components styled
  □ fetch('/api/data') on page load
  □ Background image rendering on canvas
  □ drawCloudPolygon() with Catmull-Rom smooth curves
  □ Cloud color coding (blue/green/orange/red)
  □ Cloud labels at centroids
  □ Zoom: mouse wheel centered on cursor
  □ Pan: Space+drag
  □ Fit-to-view on initial load
  □ Zoom controls (+/- buttons, zoom % display)
  □ Overlay toggle (if overlay was provided)
#
Test:
  python step2c_cloud_editor.py --image input_drawing.jpg \
      --clouds output/outer_clouds_v2.json --out test/
  Browser opens
  ✓ All 23 outer clouds visible in correct positions
  ✓ Clouds are rendered as smooth curves (not jagged)
  ✓ Cloud C5 aligns with the actual cloud arcs on the drawing
  ✓ Zoom and pan work smoothly
  ✓ Colors: blue outlines with light blue fill

── PHASE 3: Select + Delete Tools ─────────────────────
#
Time: ~20 minutes
#
Implement:
  □ Point-in-polygon hit testing
  □ Overlapping cloud priority (smallest area first)
  □ Select tool: click to select, click empty to deselect
  □ Selected cloud: red highlight, info in sidebar
  □ Delete tool: click cloud to delete (soft-delete)
  □ Delete key removes selected cloud
  □ Sidebar cloud list: click to select, [×] to delete
  □ Sidebar info panel for selected cloud
  □ Counter updates (deleted count)
  □ Hover highlight (glow effect)
  □ Double-click to zoom-to-cloud
#
Test:
  ✓ Click cloud → turns red, sidebar shows info
  ✓ Click empty → deselects
  ✓ Delete key → cloud disappears, count updates
  ✓ Double-click → zooms in to cloud
  ✓ Overlapping clouds: clicking returns the smaller one

── PHASE 4: Add Rectangle with Scalloped Curves ───────
#
Time: ~30 minutes
#
Implement:
  □ Rectangle draw interaction (mousedown → drag → mouseup)
  □ Dashed green preview while drawing
  □ generateScallopedBoundary() algorithm
  □ rectToScallopedCloud() function
  □ New cloud creation with proper metadata
  □ Auto-select new cloud
  □ Counter update (added count)
#
Test:
  ✓ Draw rectangle → creates cloud with ARC EDGES (not straight)
  ✓ Arcs bump outward from the rectangle boundary
  ✓ Cloud appears green in sidebar (user-added)
  ✓ Cloud is selectable, deletable
  ✓ Minimum size check works (tiny rect = no cloud)

── PHASE 5: Add Lasso + Add Polygon Tools ─────────────
#
Time: ~25 minutes
#
Implement:
  □ Lasso: freehand draw → collect points → simplify → scallop
  □ RDP simplification algorithm
  □ Polygon: click-to-place → Enter/double-click to close
  □ "Cloudify" option for polygon (add scallops)
  □ Visual feedback during drawing (dashed lines, vertex dots)
  □ Escape to cancel in-progress drawing
#
Test:
  ✓ Lasso around a region → creates smoothed cloud
  ✓ Polygon click-click-click-Enter → creates cloud
  ✓ Cloudify converts straight edges to scalloped arcs

── PHASE 6: Vertex Editor ─────────────────────────────
#
Time: ~30 minutes
#
Implement:
  □ Control point rendering (adaptive density)
  □ Control point hit testing
  □ Drag vertex → live polygon update
  □ Insert vertex on edge (click between CPs)
  □ Delete vertex (Shift+click / right-click)
  □ Segment highlight on hover
  □ Segment delete (Shift+click on segment)
  □ Cloud marked as modified after any edit
  □ polygonJson set to null (force recompute)
#
Test:
  ✓ Select cloud → switch to Edit → see white control points
  ✓ Drag point → polygon shape changes live
  ✓ Click edge → new point appears
  ✓ Shift+click point → point removed (min 3 enforced)
  ✓ Cloud status changes to "modified" (orange)

── PHASE 7: Merge + Extend Tools ──────────────────────
#
Time: ~30 minutes
#
Implement:
  □ Merge tool: multi-select (click to toggle selection)
  □ Merge button / Enter to execute merge
  □ Convex hull algorithm (Graham scan)
  □ Merged cloud creation, originals marked deleted
  □ Extend tool: draw region while cloud selected
  □ Extend merges drawn region into selected cloud
  □ extendCloud() function
  □ Visual: merge preview (dashed outline), extend preview
#
Test:
  ✓ Select 2 clouds → Merge → new combined cloud appears
  ✓ Original 2 clouds disappear
  ✓ Select cloud → Extend → draw nearby → cloud grows
  ✓ Merged cloud has source='manual_merge'

── PHASE 8: Undo/Redo + Keyboard Shortcuts ────────────
#
Time: ~20 minutes
#
Implement:
  □ pushUndo() before each mutating action
  □ undo() — restore previous state (Ctrl+Z)
  □ redo() — re-apply undone action (Ctrl+Shift+Z)
  □ Full keyboard shortcut set (§5.9 from v1 plan)
  □ beforeunload dialog
  □ Sidebar "Deleted" section with restore buttons
  □ Next/previous cloud navigation ([ ] keys)
#
Test:
  ✓ Delete → Ctrl+Z → cloud restored
  ✓ Edit vertex → Ctrl+Z → vertex back to original
  ✓ Closing tab shows "unsaved changes" warning

── PHASE 9: Export + Done Modal + Save Flow ────────────
#
Time: ~25 minutes
#
Implement:
  □ validateBeforeSave() — frontend checks
  □ Done modal with summary, JSON preview, buttons
  □ Coordinate precision: unchanged clouds use original polygonJson
  □ Modified/added clouds: display→JSON recompute
  □ POST payload assembly
  □ fetch('/api/save', { method: 'POST', body: payload })
  □ Success: "Saved! Close this tab." message
  □ Error: show error, allow retry
  □ Copy JSON button
  □ Server shutdown after successful save
#
Test:
  ✓ Click Done → modal with correct summary
  ✓ Save → files appear in output/
  ✓ approved_clouds.json: correct schema, JSON-space coords
  ✓ cloud_mask_approved.png: 8000×5650, grayscale, correct regions
  ✓ overlay_approved.jpg: 8000×5650, green outlines
  ✓ Server exits after save

── PHASE 10: Step5a Integration + Full Pipeline Test ───
#
Time: ~15 minutes
#
Implement:
  □ Add 8-line cloud-loading patch to step5a
  □ Add same patch to any other script that loads cloud data directly
#
Test:
  Full pipeline:
  python step2b_cloud_detection.py input_drawing.jpg --out output/ --no-gemini
  python step2c_cloud_editor.py --image input_drawing.jpg --clouds output/outer_clouds_v2.json --out output/
    → Add 1 cloud, delete 2, edit 1, merge 2, click Done
  python -c "
  import json
  with open('output/approved_clouds.json') as f:
      d = json.load(f)
  print(f'Clouds: {d[\"stats\"][\"total\"]}')
  print(f'Coords: {d[\"stats\"][\"image_size\"]}')
  m = d['edit_metadata']
  print(f'Added: {m[\"added\"]}, Deleted: {m[\"deleted\"]}, Modified: {m[\"modified\"]}, Merged: {m[\"merged\"]}')
  for c in d['clouds'][:3]:
      print(f'  C{c[\"id\"]}: src={c[\"source\"]} status={c[\"status\"]} pts={len(c[\"polygon\"])} bbox={c[\"bbox\"]}')
  "
  ✓ step5a loads approved_clouds.json when present
  ✓ step5a falls back to outer_clouds_v2.json when approved missing


══════════════════════════════════════════════════════════
§16  TESTING CHECKLIST
══════════════════════════════════════════════════════════

── COORDINATE ACCURACY ────────────────────────────────
[ ] Cloud C5 (small) aligns with visible cloud arcs on drawing
[ ] Cloud C1 (mega) boundary follows visible outlines
[ ] Cloud C37 (tiny, 137×272 img px) is visible and clickable
[ ] Added cloud exports with correct JSON-space coordinates
[ ] Round-trip: load → save unchanged → diff → coords identical ±1px
[ ] Scale computed from file dims, not hardcoded

── ALL TOOLS ──────────────────────────────────────────
[ ] Pan: smooth drag, no jitter
[ ] Zoom: cursor-centered, smooth, 10%–1000% range
[ ] Select: click inside = select, outside = deselect
[ ] Select: double-click = zoom to cloud
[ ] Delete: click deletes, Ctrl+Z restores
[ ] Add Rect: creates SCALLOPED cloud (arcs, not box)
[ ] Add Lasso: freehand → smoothed cloud boundary
[ ] Add Polygon: click vertices → close → cloud
[ ] Edit: control points visible, draggable
[ ] Edit: insert vertex on edge click
[ ] Edit: delete vertex on Shift+click
[ ] Edit: segment highlight and delete
[ ] Merge: select 2+ clouds → merge → new combined cloud
[ ] Extend: draw region near cloud → cloud grows
[ ] Undo/Redo: all operations reversible

── OUTPUT FILES ───────────────────────────────────────
[ ] approved_clouds.json: valid JSON
[ ] approved_clouds.json: backward-compatible with step5a
[ ] approved_clouds.json: all coords in JSON space (9934×7017)
[ ] approved_clouds.json: has edit_metadata
[ ] approved_clouds.json: unchanged clouds keep original polygon
[ ] cloud_mask_approved.png: 8000×5650, GRAYSCALE (not RGB)
[ ] cloud_mask_approved.png: 0/255 values only
[ ] cloud_mask_approved.png: white regions match approved clouds
[ ] overlay_approved.jpg: 8000×5650, green outlines + labels

── EDGE CASES ─────────────────────────────────────────
[ ] No changes → save works, output matches input data
[ ] All clouds deleted → save works, empty list, empty mask
[ ] Browser refresh → reloads original data
[ ] Very small cloud (50×50) → renders and exports correctly
[ ] Very large cloud (C1) → editable, control points work
[ ] Overlapping clouds → correct hit testing (smallest first)
[ ] Port conflict → auto-finds next available port

── DOWNSTREAM ─────────────────────────────────────────
[ ] step5a loads approved_clouds.json when present
[ ] step5a loads outer_clouds_v2.json when approved missing
[ ] step5a mask matches approved mask file

═══ END OF SPECIFICATION ═══
