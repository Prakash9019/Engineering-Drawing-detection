# CDCI Tag Extraction Engine

Production-ready P&ID tag register generator with **manual green-cloud scope detection**.

## Why this design

The previous AI-based cloud detection was unreliable because asking an LLM
to find scalloped revision-cloud shapes is fundamentally hard. This version
takes a much simpler, deterministic approach:

1. **You mark revision-scope boundaries in GREEN** on the input drawing
2. **OpenCV detects the green boundaries** via HSV color thresholding — fast and reliable
3. **The cloud polygons drive the scope mask** that filters every downstream operation
4. **Only tags inside the cloud scope** appear in the final register

This is the right engineering tradeoff: minimal manual annotation upfront → fully automated downstream.

## Project Structure

```
cdci_extractor/
├── main.py                      # Entry point — orchestrates all stages
├── config.py                    # All tunable parameters (HSV thresholds, weights, etc.)
├── requirements.txt
├── README.md
│
├── core/                        # Reusable utilities (no pipeline logic)
│   ├── gemini_client.py         # Gemini API wrapper with retry/thinking control
│   ├── json_parser.py           # Robust JSON extraction from LLM responses
│   ├── isa_decode.py            # ISA-5.1 tag decoder + discipline classifier
│   ├── geometry.py              # Polygon ops, IoU, point-in-mask, dedup
│   └── confidence.py            # Multi-factor confidence scoring (Arch L15)
│
└── pipeline/                    # One file per pipeline stage
    ├── stage1_cloud.py          # GREEN cloud detection (priority milestone)
    ├── stage2_title.py          # Title block OCR
    ├── stage3_notes.py          # Notes intelligence (prefix/scope rules)
    ├── stage4_detect.py         # Symbol+tag detection (cloud-scoped)
    ├── stage5_associate.py      # Tag enrichment (descriptions, discipline)
    ├── stage6_validate.py       # 7-stage validation engine
    └── stage7_excel.py          # 15-field Excel output
```

## Installation

```bash
pip install -r requirements.txt
export GOOGLE_API_KEY="your-key"   # get free key at aistudio.google.com/apikey
```

## Usage

### Single command — full pipeline

```bash
python main.py drawing.jpg output.xlsx
```

### With explicit model

```bash
python main.py drawing.jpg output.xlsx --model gemini-2.5-pro
```

### Verbose logging

```bash
python main.py drawing.jpg output.xlsx --verbose
```

### Test cloud detection alone (no API needed)

```bash
python -m pipeline.stage1_cloud drawing.jpg
```

## Workflow

1. **Open your P&ID in any image editor** (Photoshop, GIMP, even MS Paint)
2. **Mark the revision-scope boundaries in green** using any green color (HSV 35-85)
   - Doesn't need to be precise — irregular shapes are fine
   - Nested clouds are supported
   - Multiple separate clouds are supported
3. **Run the extractor** — it detects the green regions, builds the scope mask,
   and runs all downstream stages constrained to that scope
4. **Review the output**
   - `output.xlsx` — main register (Tag Register, Notes, Metadata, Summary, Cloud Scope sheets)
   - `output.json` — raw detection coordinates
   - `output.jpg` — annotated drawing (red clouds, blue tag boxes)
   - `debug/` — green mask, morphology output, scope mask, tinted scope view

## Pipeline Stages

| Stage | Layer | Purpose | Method |
|-------|-------|---------|--------|
| 1 | L4 (Revision Intel) | Detect green clouds → scope mask | OpenCV HSV |
| 2 | L2 (Title Block) | Extract drawing metadata | Gemini OCR |
| 3 | L5 (Notes Engine) | Parse notes → prefix/scope rules | Gemini OCR + reasoning |
| 4 | L7 (Detection) | Symbol+tag detection (scope-filtered) | Gemini tiled vision |
| 5 | L8+L11 (Assembly) | Apply prefix, decode ISA, descriptions | Gemini + local ISA |
| 6 | L12 (Validation) | 7-stage rule validation | Regex + business rules |
| 7 | L16 (Output) | 15-field Excel + summary sheets | openpyxl |

## Tuning

All thresholds live in `config.py` — adjust without touching code:

| Parameter | Default | Purpose |
|---|---|---|
| `GREEN_HSV_LOW` / `HIGH` | `(35,40,40)` / `(85,255,255)` | Green color range |
| `CLOUD_MIN_AREA_PX` | `5000` | Minimum cloud size in px² |
| `CLOUD_MORPH_KERNEL` | `7` | Morph close kernel to bridge marker gaps |
| `CLOUD_DILATE_PX` | `15` | Padding around cloud polygons |
| `TILE_SIZE` | `2500` | Tile size for tiled Gemini detection |
| `TILE_OVERLAP` | `350` | Overlap between adjacent tiles |
| `CONF_AUTO_ACCEPT` | `0.85` | Confidence ≥ this → auto-accept |
| `CONF_REVIEW_THRESHOLD` | `0.60` | Confidence ≥ this → review required |

## Confidence Routing (Architecture L15)

```
C_final = 0.25×C_det + 0.30×C_ocr + 0.15×C_geo + 0.20×C_val + 0.10×C_reg

C_final ≥ 0.85   →  AUTO_ACCEPT
C_final ≥ 0.60   →  REVIEW_REQUIRED  (flagged in REMARKS column)
C_final <  0.60  →  AUTO_REJECT       (excluded from register)
```

## Output Excel Structure

**Sheet 1 — Tag Register** (15 columns matching ANNEXURE-4 format):
S.NO, DISCIPLINE, TAG NUMBER, TAG DESCRIPTION, EQUIPMENT DESCRIPTION,
SIZE&RATING, DOCUMENT NUMBER, SHEET NO, REV, DRAWING REFERENCE,
DOCUMENT TITLE, DOC STATUS, DATE, DUPLICATE STATUS, REMARKS

**Sheet 2 — Notes**: All engineering notes classified by type
**Sheet 3 — Metadata**: Complete title block data
**Sheet 4 — Summary**: Counts by discipline, confidence routing distribution
**Sheet 5 — Cloud Scope**: Cloud polygon coordinates and bounding boxes

## Debug Outputs

The `debug/` folder helps verify cloud detection worked:

| File | Purpose |
|------|---------|
| `01_green_mask.png` | Raw HSV thresholding result |
| `02_morph_closed.png` | After morphological closing |
| `03_scope_mask.png` | Final binary scope mask |
| `04_cloud_overlay.jpg` | Original drawing with red cloud outlines |
| `05_scope_tinted.jpg` | Out-of-scope areas darkened (visual verification) |

If clouds aren't detected as expected, inspect these in order to find what's wrong.
