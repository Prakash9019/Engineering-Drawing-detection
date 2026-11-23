# Engineering Drawing Detection & P&ID Extraction Pipeline
## System Architectural Memory (`PROJECT_MEMORY.md`)

---

## 1. Project Purpose & Architectural Vision

The CDCI P&ID Tag Extraction system is an automated computer vision and language reasoning pipeline designed to extract engineering tags (instrumentation, equipment, valves, piping, lines) from complex, high-resolution scanned Piping & Instrumentation Diagrams (P&IDs).

### Primary Deliverables
1. **Normalized Annexure-4 Tag Register** (`output/final_tags.xlsx`):
   - Multi-sheet client deliverable containing:
     * `AUTO_ACCEPT`: High-confidence detections ($C_{\text{final}} \ge 0.80$).
     * `HUMAN_REVIEW`: Borderline or ambiguous detections ($0.55 \le C_{\text{final}} < 0.80$) prioritized P1–P4.
     * `SUMMARY`: Aggregated statistics, unique tag counts, and validation checks.
2. **Hierarchy Deliverables** (`output/final_hierarchy.xlsx`, `output/hierarchy_viewer.html`, `output/hierarchy_graph.html`):
   - Plant, Area, Unit, System decomposition.
   - Component parentage (`PARENT_EQUIP`), process lines (`via_line`), flow orientation (upstream/downstream), and control loops.

---

## 2. Pipeline Stages & Architectural Responsibilities

The pipeline follows a strictly ordered, decoupled architecture mediated by `output/drawing_context.json`:

```
Phase 1: Context Preparation
  [Step 1: Format Detect] ──> [Step 2: Title Block] ──> [Step 2B: Cloud Detection]
                                                                  │
                                                        [Step 2C: Cloud Editor (Optional HITL)]
                                                                  │
  [Step 6: Table Agent]   <── [Step 4: SOW Agent]   <── [Step 3: Notes Agent]
        │
Phase 2: Tag Extraction
        └──> [Step 5A: Candidate Extraction (SAHI Tiling + Gemini Vision + Tesseract)]
                   │
Phase 3: Association, Hierarchy & Validation
                   ├──> [Step 5B: Geometric Association (Symbols to Tags)]
                   │          │
                   │    [Step 5B2: Hierarchy & Flow (Graph, BFS, Control Loops)]
                   │          │
                   ├──> [Step 5C: Validation Engine (ISA-5.1 Rules)]
                   │          │
                   └──> [Step 5D: Duplicate Resolution (Spatial Dedup & Fragment Merge)]
                              │
Phase 4: Deliverables & Routing
                              └──> [Step 7: CEDM Normalizer (15 Annexure-4 Fields)]
                                         │
                                   [Step 8: Confidence Router (Multi-factor Formula)]
                                         │
                                   [Step 9: Hierarchy Deliverables (Excel, Tree, Force Graph)]
Phase 5: Benchmarking & QA
  [eval_coverage.py]  |  [compare_final_vs_annexure4.py]  |  [visualize_hierarchy.py]
```

### Detailed Stage Breakdown
- **Step 1 (`stages/step1_format_detect.py`)**: Resolves image raster path, validates dimension/DPI, establishes canonical coordinates.
- **Step 2 (`stages/step2_title_block.py`)**: Uses `gemini-2.5-flash` to parse title block metadata (drawing number, revision, sheet, plant/area).
- **Step 2B (`stages/step2b_cloud_detection.py`)**: Detects revision clouds using border/line rejection filtering and Gemini contour bounding boxes; generates `outer_clouds_v2.json`, `cloud_mask_v2.png`, and `overlay_v2.jpg`.
- **Step 2C (`step2c_cloud_editor/step2c_cloud_editor.py`)**: Optional browser-based human review tool to adjust or approve detected clouds (`approved_clouds.json`). Step 5a seamlessly falls back to Step 2b when Step 2c is omitted.
- **Step 3 (`stages/step3_notes_agent.py`)**: Extracts drawing notes, discipline instructions, and general abbreviations via `gemini-2.5-flash` + Tesseract.
- **Step 4 (`stages/step4_sow_agent.py`)**: Builds scope-of-work symbol memory from `ANNEXURE-2_*.xlsx` (`--skip-vision` supported).
- **Step 6 (`stages/step6_table_agent.py`)**: Extracts equipment schedule tables into `tables_context.json`.
- **Step 5A (`stages/step5a_candidate_extraction.py`)**: Core tag detector. Employs Sliced Aided Hyper Inference (SAHI) with 768px patches, 40% overlap, upscaled to 1024px for `gemini-3.1-pro-preview`. Employs intra-step deduplication.
- **Step 5B (`stages/step5b_geometric_association.py`)**: Associates detected tags with nearby symbols, piping runs, and instrument bubbles using morphological skeletonization and line detection.
- **Step 5B2 (`stages/step5b2_hierarchy.py`)**: Connects components into a directed process graph. Performs BFS process line rooting, flow propagation (arrowheads, check valves), and Track C signal loop discovery.
- **Step 5C (`stages/step5c_validation_engine.py`)**: Evaluates tag syntax against ISA-5.1 standards and checks presence in master notes/tables.
- **Step 5D (`stages/step5d_duplicate_resolution.py`)**: Resolves overlapping patch detections (IoU > 0.5) and merges fragmented text blocks within 300px.
- **Step 7 (`stages/step7_cedm_normalizer.py`)**: Normalizes candidate attributes into 15 Annexure-4 fields and enriches with parent equipment and flow provenance from `step5b2_hierarchy_full.json`.
- **Step 8 (`stages/step8_confidence_router.py`)**: Computes weighted final confidence $C_{\text{final}}$ and routes tags into `AUTO_ACCEPT` or `HUMAN_REVIEW` sheets.
- **Step 9 (`stages/step9_hierarchy_deliverables.py`)**: Generates standalone Excel hierarchy, interactive HTML tree, and validation issue registers.

---

## 3. Important Technologies & Source Files

### Technologies
- **Python 3.10+**: Core programming language.
- **OpenCV (`opencv-python` / `opencv-python-headless`)**: CLAHE image enhancement, morphological transforms, contour detection, line segment detection (LSD).
- **Tesseract OCR (`pytesseract`)**: Deterministic character recognition and bounding box verification.
- **Google GenAI SDK (`google-genai`)**: Multimodal LLM client for Gemini 3.1 Pro Preview (vision) and Gemini 2.5 Flash (metadata/tables).
- **NumPy & SciPy**: Spatial distance matrices, array slicing, graph analysis.
- **OpenPyXL**: Excel workbook generation with styling and multi-sheet output.
- **Pytest**: Fast unit test execution framework.

### Key Source Files & Manifest
- `core/confidence.py`: Multi-factor confidence formula and routing logic.
- `core/geometry.py`: Spatial algorithms (IoU, distance, polygon-mask intersection, deduping).
- `core/isa_decode.py`: ISA-5.1 tag syntax parser, discipline classifier, CEDM tag normalizer.
- `core/json_parser.py`: Resilient JSON parser handling markdown fences and LLM artifacts.
- `core/gemini_client.py`: API client wrapper with backoff and retry mechanisms.
- `settings.py`: Global configuration parameters, weights, and model definitions.
- `tests/test_core.py`: Fast unit tests for core utilities.

---

## 4. Implemented vs. Planned Features

### Implemented Features
- [x] End-to-end P&ID extraction pipeline (Step 1 through Step 9).
- [x] SAHI patch tiling (768px / 40% overlap) with Gemini 3.1 Pro Preview vision.
- [x] Two-track extraction architecture (full drawing for hierarchy, cloud for deliverables).
- [x] Optional human-in-the-loop cloud editor (Step 2c) with transparent fallback to Step 2b.
- [x] Robust JSON parsing recovering from malformed LLM markdown fences.
- [x] Directional flow analysis and Track C control loop extraction.
- [x] 15-field Annexure-4 Excel deliverable generation with confidence routing.
- [x] Multi-tier test suite and automated hook enforcement (secret scanner, syntax verification).

### Planned / In-Progress Features
- [ ] Direct cloud extraction unification gate: evaluate full-sheet + post-filter across 50–100 drawings to eliminate redundant extraction runs.
- [ ] Multi-sheet cross-drawing reference resolution (currently single-sheet heuristic).
- [ ] Continuous learning symbol memory: auto-updating symbol catalog from user-reviewed feedback.

---

## 5. Architectural Decisions, Invariants & Known Limitations

### Critical Invariants
1. **`drawing_context.json` is Sacred**: State is shared across stages through this file. Do not delete mid-run.
2. **Two-Track Extraction Separation**:
   - `step5a --force-full-drawing` writes full candidate sets.
   - `step5b2` must process full candidates (`step5b_associations_full.json`) to accurately discover parent equipment located outside revision clouds.
   - Do NOT collapse the two tracks into a single cloud run without formal approval via `stages/validate_full_vs_cloud.py`.
3. **No Hardcoded Machine Paths**: Use portable paths and environment variables (`$env:GEMINI_KEY` or `export GEMINI_KEY`).
4. **Mega-Cloud Area Guard**: Step 5a skips degenerate clouds that cover $\ge 85\%$ of the total sheet area, measured strictly in JSON coordinate space against `stats.image_size`.

### Known Limitations
- High-resolution processing (8000x5650) requires ~4GB RAM during SAHI tile generation.
- Free-tier Gemini API keys are rate-limited to 15 RPM; avoid setting `--workers > 1` on free tier keys to prevent HTTP 429 throttling.
- Scanned drawings with extreme skew (>5 degrees) require deskewing before running format detection.

---

## 6. Testing & Execution Commands

### Fast Offline Testing (<2s)
```bash
python -m pytest tests/test_core.py -v
```

### Static Hooks & Quality Checks
```bash
python .claude/hooks/code_quality.py
python .claude/hooks/secret_scanner.py
python .claude/hooks/completion_check.py
```

### Ground-Truth Recall Benchmark (Test Drawing Rev C)
```bash
python stages/eval_coverage.py \
  --candidates output/step5a_candidates.json \
  --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx \
  --image input_drawing.jpg \
  --out output/
```

### Compare Deliverable vs Client Ground Truth
```bash
python stages/compare_final_vs_annexure4.py
```
