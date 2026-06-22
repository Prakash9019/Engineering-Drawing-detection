# Step 2C — Interactive Revision Cloud Correction Editor

Human-in-the-loop revision cloud correction for the CDCI P&ID pipeline.
Sits between **step2b** (auto-detection) and **step5a** (symbol extraction).

```
step2b (auto-detect)  →  step2c (human review)  →  step5a (extraction)
   outer_clouds_v2.json     approved_clouds.json      reads approved
```

---

## What it does

1. Loads the drawing + step2b's detected clouds (`outer_clouds_v2.json`)
2. Opens a browser-based editor showing every detected cloud over the drawing
3. You **add, delete, merge, extend, and edit** clouds with full curve support
4. On **Done**, generates the approved cloud geometry that all downstream steps consume

---

## Quick start

```bash
# 1. Install dependencies (already present in the pipeline env)
pip install -r requirements.txt

# 2. Run the editor
python step2c_cloud_editor/step2c_cloud_editor.py \
    --image input_drawing.jpg \
    --clouds output/outer_clouds_v2.json \
    --out output/

# A browser opens automatically. Edit, then click "Done → Save & Exit".
```

### With overlay toggle (compare against step2b's render)

```bash
python step2c_cloud_editor/step2c_cloud_editor.py \
    --image input_drawing.jpg \
    --clouds output/outer_clouds_v2.json \
    --overlay output/overlay_v2.jpg \
    --out output/
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--image` | (required) | P&ID drawing JPG/PNG |
| `--clouds` | (required) | `outer_clouds_v2.json` from step2b |
| `--out` | `output` | Output directory |
| `--port` | `8765` | HTTP port (auto-falls back if busy) |
| `--no-browser` | off | Don't auto-open browser |
| `--overlay` | none | `overlay_v2.jpg` for toggle view |

---

## The editor

### Toolbar

| Key | Tool | What it does |
|-----|------|--------------|
| `1` | Pan | Drag to move, scroll to zoom |
| `2` | Select | Click a cloud; Del removes it; double-click zooms |
| `3` | Add Rectangle | Drag a box → becomes a **scalloped cloud** (not a box) |
| `4` | Add Lasso | Draw freehand → smoothed cloud boundary |
| `5` | Add Polygon | Click points, Enter to close |
| `6` | Edit | **Grab the boundary anywhere and pull** — a stretch of the edge follows as one smooth curve. `[`/`]` size the pull, `Alt`-drag moves a single point, `Shift`/right-click deletes a point |
| `7` | Merge | Click 2+ clouds, Enter → merged into one |
| `8` | Extend | Select a cloud, drag a box to grow it |
| `R` | Remove Region | **The inverse of Add Polygon.** Select a cloud, click points around the unwanted area, Enter / double-click → that polygon is **subtracted** (Cloud − Polygon). Draw across the cloud edge for tails/trims |
| `E` | Eraser | **Paint over unwanted cloud area** → it's subtracted from the region. `[`/`]` size the brush. Tiny fragments auto-removed; a split becomes separate clouds |
| `X` | Cut | **Drag a line across a false tail** → the cloud is sliced and the larger side kept, the tail discarded |
| `9` | Delete | Click a cloud to remove |

### Other shortcuts

`Space`+drag = pan anytime · `Ctrl+Z`/`Ctrl+Shift+Z` = undo/redo ·
`F` = fit to view · `O` = toggle overlay · `Esc` = deselect/cancel ·
`+`/`-` = zoom · `[`/`]` = shrink/grow the Edit pull radius

### Cloud repair workflow (region editing, not vertex editing)

Think in terms of **regions**, not points. Vertex editing is the exception.

| Problem | Tool | How |
|---------|------|-----|
| **Missing area** | Extend (`8`) / Add Rect·Lasso·Poly (`3`/`4`/`5`) | Draw the area to add, or drag a box to grow a cloud |
| **Missing area (nudge)** | Edit (`6`) | Grab the boundary and **pull it outward** — a smooth stretch follows the cursor |
| **Extra blob / overshoot** | Remove Region (`R`) / Eraser (`E`) | **Draw a polygon** around the area to subtract (like Add Polygon, inverted), or **paint over** it |
| **Long false-positive tail** | Remove Region (`R`) / Cut (`X`) | **Draw a polygon** around the tail and subtract it, or **drag a line** across its neck |
| **Minor correction** | Edit (`6`) | `Alt`-drag a single point, or `Shift`/right-click to delete one |

**Eraser & Cut** treat the cloud as a filled region: the editor rasterizes the
boundary, subtracts your stroke/slice, then re-traces and simplifies the result.
This removes tiny fragments, drops orphan slivers, and resolves self-intersections
automatically — no manual vertex cleanup. An eraser stroke that splits a cloud
keeps both significant halves as separate clouds; Cut keeps only the larger side.

> Very large clouds at low zoom may exceed the raster budget — zoom in and the
> operation runs at full fidelity.

### Color coding

- **Blue** — auto-detected (unchanged)
- **Green** — user-added
- **Orange** — modified
- **Purple** — merged
- **Red** — selected

---

## Outputs

Written to `--out`:

| File | Format | Purpose |
|------|--------|---------|
| `approved_clouds.json` | JSON (9934×7017 coords) | Ground truth for step5a/5b/5c |
| `cloud_mask_approved.png` | Grayscale PNG (8000×5650) | Binary mask for inside-cloud test |
| `overlay_approved.jpg` | RGB JPEG (8000×5650) | Visual verification |

The JSON schema is **backward-compatible** with `outer_clouds_v2.json`, so
step5a reads it with no schema changes.

---

## Coordinate systems (important)

Three coordinate spaces are handled automatically:

| Space | Example | Used for |
|-------|---------|----------|
| JSON | 9934×7017 | `outer_clouds_v2.json`, output JSON, step5a |
| Image | 8000×5650 | image files, mask generation |
| Display | 2400×~1694 | browser rendering only |

Scale factors are **computed at runtime** from `stats.image_size` in the JSON
vs the actual image dimensions — never hardcoded. This fixes the v1 bug where
clouds were positioned ~56% off.

**Precision:** Unchanged clouds keep their original full-resolution polygon
(zero precision loss). Modified/added clouds are recomputed display→JSON with
±1px accuracy.

---

## Connecting to step5a

See `step5a_integration_patch.py`. It's a small helper that makes step5a
prefer `approved_clouds.json` when present, falling back to
`outer_clouds_v2.json` otherwise. Steps 5B/5C inherit automatically.

---

## File structure

```
step2c_cloud_editor/
├── step2c_cloud_editor/step2c_cloud_editor.py        # Backend: CLI, HTTP server, output generation
├── editor/
│   ├── index.html                # Self-contained editor (HTML+CSS+JS inlined)
│   ├── index_split.html          # HTML+CSS only (dev — references editor.js)
│   └── editor.js                 # Editor engine (dev source)
├── step5a_integration_patch.py   # How to wire step5a to use approved clouds
├── requirements.txt
├── run_example.sh                # Example invocation
└── README.md
```

**Note:** The server serves `editor/index.html` (the inlined single file).
`index_split.html` + `editor.js` are the editable dev sources — after editing
`editor.js`, rebuild `index.html`:

```bash
python build_editor.py    # inlines editor.js into index.html
```

---

## Full pipeline example

```bash
# 1. Auto-detect
python step2b_cloud_detection.py input_drawing.jpg --out output/

# 2. Human review & correction
python step2c_cloud_editor/step2c_cloud_editor.py \
    --image input_drawing.jpg \
    --clouds output/outer_clouds_v2.json \
    --overlay output/overlay_v2.jpg \
    --out output/

# 3. Extraction (now uses approved clouds)
python step5a_candidate_extraction.py input_drawing.jpg \
    --context output/drawing_context.json --api-key $GEMINI_API_KEY
```
