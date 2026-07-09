# CDCI P&ID Tag Extraction System
## Team Presentation — Complete Technical Reference

---

## What We Built — The Simple Version

We have built an automated system that reads engineering drawings — the technical diagrams called P&IDs (Piping and Instrumentation Diagrams) — and extracts every equipment tag from them. A tag is a label like `FIT-1001` or `V-BV-2246` that identifies a physical instrument or valve in a plant. Doing this manually takes a team days per drawing. Our system does it in under 10 minutes per drawing, automatically.

The system is a pipeline of 12 Python scripts. Each script does one specific job and passes its results to the next. The drawing goes in one end, a structured Excel register of all tags comes out the other end. Every script saves its work to a shared file called `drawing_context.json` so every later step knows everything the earlier steps already found.

The system uses three types of technology working together: **Google Gemini AI** to understand what it sees in the image, **Tesseract OCR** to read text precisely, and **OpenCV** to process the image geometry. The key idea is that Gemini understands meaning (what is a ball valve, where is the title block) while Tesseract handles accuracy (the exact characters in a tag number). Neither one alone is enough.

---

## What This Kind of System Is Called

When you send an image or document to an AI like Gemini along with instructions asking it to find specific things, extract data, or make decisions — that is called a **multimodal AI call**. Multimodal means the AI is processing more than one type of input at the same time: the image and the text prompt together.

The prompt is the instruction we write that tells Gemini exactly what to do. For example, we send it the bottom-right corner of the drawing and say: "You are reading the title block of a P&ID drawing. Extract the drawing number, revision, sheet number, and title. Return it as JSON." Gemini reads the image, understands the layout, and returns a structured JSON object with all those fields filled in.

This style of system — where multiple AI calls are chained together, each one doing a specific job and passing structured data to the next — is called a **multi-agent pipeline**. Each script in our system is effectively an agent: it has a specific responsibility, specific inputs, and specific outputs. None of them tries to do everything.

---

## The 12 Scripts — What Each One Does

---

### Step 1 — `step1_format_detect.py`
**What it does:** The very first thing the pipeline does is look at the drawing file and understand what kind of file it is. Is it a PDF that still has text embedded inside it (called a vector PDF)? Or is it a scanned image where everything is just pixels? This matters because vector PDFs can have their text extracted directly — no AI needed. Raster scans need image processing.

For our drawings, which are scanned JPG images, it applies a technique called **CLAHE** (Contrast Limited Adaptive Histogram Equalisation) which makes the thin lines and faint text in engineering drawings much sharper and cleaner before any AI looks at it. It also makes an optional call to Gemini 2.5 Flash-Lite (the cheapest, fastest Gemini model) to confirm the document type.

**AI used:** Gemini 2.5 Flash-Lite (optional, 1 call)
**What you tell Gemini:** "Classify this document: is it a vector PDF, a raster scan, or a hybrid? Does it have revision clouds? What type of drawing is it?"
**What Gemini returns:** A JSON object with fields like `document_type`, `has_revision_clouds`, `image_quality`, `confidence`
**Output file created:** `drawing_context.json` — the master shared file all other steps update

**Run command:**
```bash
python stages/step1_format_detect.py input_drawing.jpg --out output/ --api-key $GEMINI_KEY
```

---

### Step 2 — `step2_title_block.py`
**What it does:** Every engineering drawing has a title block — the bordered table in the bottom-right corner with the drawing number, sheet number, revision, project name, and revision history. This step finds and reads it.

It does this in two Gemini calls. The first call reads all the basic fields: drawing number, title, revision code, discipline, date, who drew it and who approved it. The second call reads the revision history table (the rows that say things like "Rev C — Re-issued for Construction — 29-08-24") and checks for a specific sentence that means this is a revision drawing: "USE THIS DRAWING FOR INFORMATION WITHIN THE CLOUDED AREAS ONLY."

After reading those fields it makes a programmatic decision (no AI, just logic): is this a brand-new drawing (revision 0 or A) meaning we extract everything? Or is it a revision drawing (B, C, 1, 2 etc.) meaning we should only extract the things inside the revision clouds? This decision is called the **Revision Mode** and it controls everything downstream.

**AI used:** Gemini 2.5 Flash (2 calls)
**What you tell Gemini (Call 1):** "You are reading the title block of a P&ID. Extract drawing number, sheet, revision, title, project name, contract number, scale, dates, drawn by, checked by, approved by. Return as JSON."
**What you tell Gemini (Call 2):** "Read the revision history table. Extract each row. Also check if the drawing says 'CLOUDED AREAS ONLY'. Return revision history as JSON array plus cloud_scope_only flag."
**What Gemini returns:** Structured JSON with all title block fields and revision history
**Output files:** `title_block_context.json`, updates `drawing_context.json`

**Actual result on our drawing:**
- Drawing: `4224-MGDV-6-50-2004`, Sheet 001, Revision C
- 5 revisions found (Rev 0 through Rev C)
- Revision notice detected → Revision Mode = **CLOUD_SCOPE_MODE**
- Decision: extract only inside revision cloud boundaries

**Run command:**
```bash
python stages/step2_title_block.py --context output/drawing_context.json --api-key $GEMINI_KEY --debug
```

---

### Step 2B — `step2b_cloud_detection.py`
**What it does:** If Step 2 determined this is a revision drawing (which it did for our test drawing), this step finds the revision clouds. Revision clouds are the scalloped, bumpy closed boundaries drawn around areas that changed in this revision. They look like clouds. Only the equipment inside these clouds is new or modified — everything outside is old and does not need to be extracted.

This step is a clean wrapper around a separate file called `cloud_detector_v2.py` that was already written and proven to work. The wrapper reads the decision from Step 2 (`revision_cloud_required = true`), calls the cloud detector, and then writes the cloud polygon coordinates into `drawing_context.json` so that Step 5A knows exactly where to look. If the drawing is not a revision drawing, this step skips itself entirely in under a second.

The cloud detector itself uses two methods: it sends the drawing to Gemini to roughly locate where clouds are, then uses OpenCV morphological operations to trace the exact bumpy boundary of each cloud. It found **9 outer clouds** and **18 inner clouds** on our test drawing.

**AI used:** None in this wrapper — `cloud_detector_v2.py` uses Gemini 2.5 Pro internally for localization
**Output files:** `overlay_v2.jpg` (drawing with cloud boundaries drawn), `cloud_mask_v2.png` (binary mask), `outer_clouds_v2.json` (polygon coordinates), updates `drawing_context.json`

**Run command:**
```bash
python stages/step2b_cloud_detection.py --context output/drawing_context.json --api-key $GEMINI_KEY
```

---

### Step 3 — `step3_notes_agent_v2.py`
**What it does:** Engineering drawings always have a notes section — a numbered list of special rules specific to that project. Things like: "All valves with an F prefix are field-routed lines." or "Refer to drawing 4224-MGDTY-6-50-2001-003 for interlock details." These notes change how we should interpret everything else on the drawing.

This step finds and reads all those notes and turns them into a set of machine-readable rules. It splits the drawing into 7 regions (left margin upper, left margin lower, bottom-left strip, bottom-right strip, abbreviations block, reference drawings block, title block area) and processes each one separately. For each region it first runs Tesseract OCR to get the raw text, then sends both the image and the OCR text to Gemini and asks it to understand the semantic meaning of each note.

The result is a file called `rules_prompt_block.txt` which is injected as context into every downstream Gemini call. This is how drawing-specific knowledge flows through the whole pipeline.

**AI used:** Gemini 2.5 Pro (priority regions, 4 calls) + Gemini 2.5 Flash (supplemental regions, 3 calls) + Tesseract OCR (all 7 regions)
**What you tell Gemini:** "You are extracting engineering notes from a P&ID. This is the [region name] region. The OCR extracted this text: [raw text]. Extract every note. For each note give the raw text, the type (abbreviation / equipment rule / scope rule / drafting rule), and a machine-readable rule statement. Return as JSON."
**What Gemini returns:** JSON with all notes, abbreviations dictionary, global constraints, tag detection rules
**Actual result:** 62 unique notes extracted, 8 abbreviations learned, 11 cloud regions detected within note areas

**Run command:**
```bash
python stages/step3_notes_agent.py --context output/drawing_context.json --api-key $GEMINI_KEY --debug
```

---

### Step 4 — `step4_sow_agent.py`
**What it does:** SOW stands for Scope of Work. The client provides an Excel file with two sheets: one listing every symbol type that should be extracted (like Ball Valve, Flow Transmitter, Pressure Gauge) and one listing every symbol type that must never be extracted (like Computer Function Signal Tags, Soft Tags, Flange symbols). This step reads both sheets, understands every symbol, and builds a memory object that all downstream steps use to filter their results.

There are 100 symbols in the USE list and 32 in the DO NOT USE list. For each one the system reads the symbol name and, if a Gemini API key is provided, also sends the symbol image from the Excel file to Gemini and asks it to describe exactly what that symbol looks like visually. This means the system can match symbols by appearance, not just by name.

The output is a file called `sow_symbol_memory.json`. Every downstream agent checks detected symbols against this memory before deciding whether to extract or discard them.

**AI used:** Gemini 2.5 Flash (optional, ~132 calls — one per symbol image; skip with `--skip-vision`)
**What you tell Gemini:** "You are an ISA 5.1 expert. Describe this P&ID symbol image. What is the primary shape? What are the internal markings? What are the connections? How would you identify this on a drawing?"
**What Gemini returns:** JSON with `primary_shape`, `internal_markings`, `distinctive_features`, `isa_match_hint`
**Actual result:** 132 symbols parsed — Valve (16), Transmitter (11), Switch (8), Gauge (6), Pump (5) in ALLOW; Signal/Logic (19), Piping (5) in BLOCK

**Run command:**
```bash
# Fast (no vision, text-only):
python stages/step4_sow_agent.py build --excel ANNEXURE-2.xlsx --out output/ --skip-vision

# Full (with symbol image understanding):
python stages/step4_sow_agent.py build --excel ANNEXURE-2.xlsx --out output/ --api-key $GEMINI_KEY
```

---

### Step 6 — `step6_table_agent.py`
**What it does:** Many drawings contain embedded tables — Tag Lists, Equipment Schedules, Line Lists. A Tag List is a grid where each row is one process slot and each column is one instrument type, with the actual tag numbers as the cell values. This step scans the drawing for any tables, detects their grid structure using OpenCV line detection, runs Tesseract OCR on the cells, and then asks Gemini to parse the whole table into structured rows and columns.

It scans in priority order: top-left first (where tag lists most commonly sit), then the full top strip, then the right margin, then wider regions. It stops when it finds high-confidence tables so it does not waste API calls on empty regions.

**AI used:** Gemini 2.5 Flash (detection, 2-4 calls) + Gemini 2.5 Flash/Pro (extraction, 1-2 calls) + Tesseract + OpenCV
**What you tell Gemini (detection):** "Find all tabular structures in this image region. For each table give its title, type, bounding box, and estimated row and column count."
**What you tell Gemini (extraction):** "Extract the complete table. Here is the pre-extracted OCR text as a guide. Extract every row and column. Return as JSON with headers array and rows array."
**Output files:** `tables_context.json`, `master_tags.json` (flat list of all tags found in tables)

---

### Step 5A — `step5a_candidate_extraction.py`
**What it does:** This is the main extraction step — the core of the whole system. It takes the drawing and systematically finds every engineering symbol on it.

The drawing is too large to send to Gemini as one image. So we use a technique called **SAHI** (Slicing Aided Hyper Inference). The drawing is cut into 117 overlapping patches of 1024×1024 pixels each, with 25% overlap so symbols at the edges of patches are not missed. All 117 patches are processed in parallel using 8 worker threads simultaneously, making this 8 times faster than sequential processing.

For each patch, two things happen in sequence. First, Tesseract OCR scans the patch and finds every text string that looks like an engineering tag (matching patterns like `FIT-1001` or `V-BV-2246`). Second, Gemini 2.5 Pro looks at the same patch and identifies every engineering symbol — instrument bubbles, valves, pumps, compressors — and associates each one with its nearest tag text.

Then two filters are applied. The **revision cloud filter** checks whether each detected symbol falls inside a cloud boundary (since this is a revision drawing, anything outside is discarded). The **SOW filter** checks each symbol type against the memory from Step 4 and discards any that are in the DO NOT USE list.

**AI used:** Gemini 2.5 Pro (117 calls, 8 in parallel, temperature=0.0 for deterministic output) + Tesseract OCR (117 patches, local)
**What you tell Gemini:** "You are an expert P&ID extraction agent. Scan this image patch top-left to bottom-right. Detect every instrument bubble, valve, pump, compressor, heat exchanger, vessel, and analyzer. For each detected symbol: identify its type, find the nearest associated tag text, record its bounding box. Only extract what is visually present. Never hallucinate. Return as JSON."
**What Gemini returns:** JSON with array of candidates, each having symbol_name, symbol_category, tag_text, symbol_bbox, tag_bbox, vision_confidence
**Actual result:** 264 candidates extracted — 165 instruments, 51 valves, 34 piping elements, 8 equipment items

**Run command:**
```bash
python stages/step5a_candidate_extraction.py --context output/drawing_context.json \
    --api-key $GEMINI_KEY --workers 8 --debug
```

---

### Step 5B — `step5b_geometric_association.py`
**What it does:** Step 5A gives us a list of detected symbols and their tag texts, but it does not tell us which pipe each instrument is connected to or which equipment each instrument belongs to. Step 5B figures that out using pure geometry — no AI needed.

It scans the drawing image for all horizontal and vertical lines (pipes) using OpenCV morphological operations and finds 9,882 lines on our test drawing. For each candidate from Step 5A it then calculates: Is there a leader line connecting the tag text to the symbol? Which pipe is closest to this symbol? Is this instrument contained inside a larger equipment boundary?

Based on these geometric calculations it assigns each candidate a spatial relationship: ATTACHED_TO (the tag directly connects to the symbol via a short line), CONNECTED_TO (the symbol sits on a pipe), CONTAINED_WITHIN (an instrument is inside an equipment), or ADJACENT_TO.

**AI used:** None — pure OpenCV geometry
**Actual result:** 237 ATTACHED_TO, 15 CONTAINED_WITHIN, 8 ADJACENT_TO, 3 CONNECTED_TO, 1 ISOLATED

**Run command:**
```bash
python stages/step5b_geometric_association.py --candidates output/step5a_candidates.json \
    --image input_drawing.jpg --out output/ --debug
```

---

### Step 5C — `step5c_validation_engine.py`
**What it does:** Every candidate from the previous steps now goes through a series of programmatic checks — no AI, just rules. There are three levels of checking. First, does the tag text match a valid ISA 5.1 format? We check against 30 regex patterns covering every instrument type: FIT, PT, TT, LT, BV, GV, SDV, PSV, and more. Second, do the business rules pass? For example, a transmitter tag should end in T or IT, a relief valve should start with RV or PSV. Third, does this tag exist in the client's asset register? We look it up in the Annexure-4 Excel file.

**AI used:** None — pure programmatic
**Actual result:** PASS=30 | WARN=119 | FAIL=115. The high FAIL count is because the asset register only has 46 tags but we extracted 264. Many valid tags from the drawing are simply not in the provided register yet — that is expected for a new project drawing.

**Run command:**
```bash
python stages/step5c_validation_engine.py --associations output/step5b_associations.json \
    --register ANNEXURE-4.xlsx --notes output/notes_context.json --out output/
```

---

### Step 5D — `step5d_duplicate_resolution.py`
**What it does:** Because SAHI uses 25% overlap between patches, the same symbol near a patch boundary gets detected twice — once in each patch. Step 5D finds and removes these duplicates. It uses an algorithm called **Spatial Mask Merging (SMM)** with three signals: bounding box overlap (IoU), distance between symbol centres, and tag text similarity. If two candidates have overlapping boxes and the same (or very similar) tag text, they are the same detection. One is kept as PRIMARY and the other is marked DISCARDED.

**AI used:** None — pure geometric algorithm
**Actual result:** 264 candidates → 233 PRIMARY + 31 DISCARDED. 11.7% duplicate rate, which matches the expected overlap from a 25% SAHI grid.

---

### Step 5 Visualizer — `step5_visualizer.py`
**What it does:** Produces three annotated images of the drawing for human review. The first shows every detected candidate with a thin coloured border and a small tag label — green for instruments, orange for valves, purple for equipment, cyan for piping. The second image highlights duplicates — green boxes for candidates that were kept (PRIMARY) and red boxes for those that were discarded (DISCARDED), with arrows showing which duplicate was merged into which primary. The third image shows only the final clean set of candidates after deduplication. The images are also saved as a zoomable tile grid so reviewers can zoom into any region.

The key design principle is that the bounding boxes are thin (2 pixels) and the labels are small so the original drawing is still clearly visible underneath.

**AI used:** None — pure OpenCV drawing

**Run command:**
```bash
python stages/step5_visualizer.py --candidates output/step5a_candidates.json \
    --deduped output/step5d_deduped.json --image input_drawing.jpg --out output/
```

---

### Step 7 — `step7_cedm_normalizer.py`
**What it does:** CEDM stands for Common Engineering Data Model — it is the standard format the client system expects tags to be in. Raw tags from OCR are often messy: lowercase letters, dots instead of hyphens, extra spaces, en-dashes instead of hyphens. This step cleans all of that up. `fit.1001` becomes `FIT-1001`. `P - 101` becomes `P-101`. `V-BV--2246` becomes `V-BV-2246`.

It also fills in the other 14 fields that the Annexure-4 output template requires: Discipline (INSTRUMENTATION / MECHANICAL / PIPING), Equipment Description (mapped from the symbol type using an engineering ontology), Document Number, Sheet, Revision, Document Title, DOC Status, and more. It generates a stable Canonical ID for each tag using a hash of the project ID and tag number, so the same tag always gets the same ID across runs.

**AI used:** None — pure programmatic normalisation using regex and lookup tables
**Actual result:** 233 records: 149 INSTRUMENTATION, 57 MECHANICAL, 27 PIPING. 51 tags required normalisation (43 separator fixes, 4 OCR character fixes).

**Run command:**
```bash
python stages/step7_cedm_normalizer.py --final output/step5_final_output.json \
    --context output/drawing_context.json --out output/ --project CDCI
```

---

### Step 8 — `step8_confidence_router.py`
**What it does:** The final step takes every normalised tag and calculates a single confidence score using a weighted formula defined in the blueprint:

```
C_final = 0.25 × C_detect + 0.30 × C_ocr + 0.15 × C_geometry + 0.20 × C_validation + 0.10 × C_register
```

Each component comes from a different step: detection confidence from Gemini in Step 5A, OCR confidence from Tesseract, geometry confidence from Step 5B, validation from Step 5C, and registry confidence from the asset register lookup.

Based on the final score, every tag is routed to one of three outcomes:
- **AUTO_ACCEPT** (score ≥ 0.85): Written directly to the final Excel output, no human review needed
- **HUMAN_REVIEW** (score 0.60–0.85): Added to a review queue with a priority label (P1 Critical through P4 Low) and the specific reason it needs review
- **AUTO_REJECT** (score < 0.60): Saved to an audit log but excluded from the output

The final deliverable is `final_tags.xlsx` — an Excel file matching the Annexure-4 format exactly — with three sheets: AUTO_ACCEPT (blue), HUMAN_REVIEW (orange with priority columns), and SUMMARY statistics.

**AI used:** None — pure mathematical formula
**Current result note:** The current run shows 0 AUTO_ACCEPT because the asset register only has 46 tags while we extracted 233. When the register is not in scope (C_register=0.5 for all), the weighted score drops below the 0.85 threshold. The weights need tuning for sparse-registry projects. This is the next refinement.

**Run command:**
```bash
python stages/step8_confidence_router.py --cedm output/step7_cedm_output.json \
    --context output/drawing_context.json --out output/
```

---

## The Technology Stack

| Technology | What It Is | Where We Use It |
|---|---|---|
| **Gemini 2.5 Pro** | Google's most capable multimodal AI. Understands complex engineering layouts, symbol arrangements, and drawing conventions. | Step 5A (extraction), Step 3 (notes) |
| **Gemini 2.5 Flash** | Faster, cheaper Gemini model for structured extraction tasks where layout is simpler. | Steps 1, 2, 3, 4, 6 |
| **Gemini 2.5 Flash-Lite** | Cheapest, fastest Gemini model for simple classification tasks. | Step 1 (document type check) |
| **Tesseract OCR** | Google's open-source OCR engine. Runs locally, no API cost. Reads text from images with high character-level accuracy. | Steps 2, 3, 5A, 6 |
| **OpenCV** | Computer vision library for image processing. Used for CLAHE enhancement, morphological operations, line detection, and drawing annotations. | All steps |
| **SAHI** | Slicing Aided Hyper Inference — a technique that splits large images into overlapping patches so small objects are not missed. | Step 5A |
| **openpyxl** | Python library for reading and writing Excel files. Used to read the symbol scope Excel and write the final output register. | Steps 4, 5C, 8 |

---

## How Gemini Prompting Works — What to Say in the Meeting

When we send an image to Gemini along with a text instruction, the instruction is called a **prompt**. The prompt tells Gemini exactly what role to play, what to look for, what format to return the answer in, and what not to do.

A good prompt has four parts. The first part sets the role: "You are an expert P&ID extraction agent." The second part describes the task: "Detect every instrument bubble and valve in this image patch." The third part specifies constraints: "Only extract what is visually present. Never hallucinate. Do not assign a tag to a symbol unless visual evidence exists." The fourth part defines the output format: "Return ONLY a JSON object with this structure: {candidates: [...]}".

The reason we always ask for JSON is that it can be parsed by code reliably. If Gemini returned a paragraph of text, we would have to guess how to extract the information. JSON means the data flows directly into the next step without any interpretation.

The temperature setting controls how creative or deterministic Gemini is. We set temperature=0.0 for all extraction tasks. This means Gemini always gives the most likely answer rather than exploring different possibilities. Creativity is useful for writing. For precise data extraction from technical drawings, we want the same input to always give the same output.

---

## The Complete Run Order

```bash
# Set environment
export GEMINI_KEY="your-key-here"
export DRAWING="input_drawing.jpg"

# Phase 1: Understand the drawing (one-time per drawing)
python stages/step1_format_detect.py   $DRAWING --out output/ --api-key $GEMINI_KEY
python stages/step2_title_block.py     --context output/drawing_context.json --api-key $GEMINI_KEY
python stages/step2b_cloud_detection.py --context output/drawing_context.json --api-key $GEMINI_KEY
python stages/step3_notes_agent.py  --context output/drawing_context.json --api-key $GEMINI_KEY
python stages/step4_sow_agent.py build --excel ANNEXURE-2.xlsx --out output/ --skip-vision
python stages/step6_table_agent.py     --context output/drawing_context.json --api-key $GEMINI_KEY

# Phase 2: Extract tags (~117 Gemini calls, ~2-5 minutes)
python stages/step5a_candidate_extraction.py --context output/drawing_context.json \
    --api-key $GEMINI_KEY --workers 8

# Phase 3: Process results (no API, under 30 seconds total)
python stages/step5b_geometric_association.py --candidates output/step5a_candidates.json \
    --image $DRAWING --out output/
python stages/step5c_validation_engine.py --associations output/step5b_associations.json \
    --register ANNEXURE-4.xlsx --notes output/notes_context.json --out output/
python stages/step5d_duplicate_resolution.py --validated output/step5c_validated.json --out output/

# Phase 4: Generate output (no API, under 10 seconds)
python stages/step7_cedm_normalizer.py --final output/step5_final_output.json \
    --context output/drawing_context.json --out output/
python stages/step8_confidence_router.py --cedm output/step7_cedm_output.json \
    --context output/drawing_context.json --out output/

# Phase 5: Human review images (no API)
python stages/step5_visualizer.py --candidates output/step5a_candidates.json \
    --deduped output/step5d_deduped.json --image $DRAWING --out output/
```

---

## What Comes Out — The Output Files

| File | What It Is |
|---|---|
| `drawing_context.json` | The master file. Every step reads and updates this. Contains everything known about the drawing. |
| `title_block_context.json` | Drawing number, revision, title, all revision history rows. |
| `notes_context.json` | All 62 notes extracted, abbreviations, drawing-specific rules. |
| `rules_prompt_block.txt` | The rules formatted as text, injected into every Gemini call. |
| `sow_symbol_memory.json` | 132 symbols with ALLOW/BLOCK status and visual descriptions. |
| `master_tags.json` | Flat list of all tags found in embedded tables. |
| `step5a_candidates.json` | 264 raw detections with bboxes, tag text, confidence scores. |
| `step5_final_output.json` | 233 candidates after duplicate removal. |
| `step7_cedm_output.json` | 233 records with all 15 Annexure-4 fields populated. |
| `final_tags.xlsx` | **★ The deliverable.** Three sheets: AUTO_ACCEPT, HUMAN_REVIEW, SUMMARY. |
| `human_review_queue.json` | Flagged items with reasons and priority levels for the review team. |
| `viz_final_clean.jpg` | Annotated drawing image showing all extracted tags with colour-coded boxes. |
| `viz_duplicates_highlighted.jpg` | Shows which detections were kept (green) and which were duplicates (red). |

---

## Current Results — Our Test Drawing

| Metric | Result |
|---|---|
| Drawing | 4224-MGDV-6-50-2004, Sheet 001, Revision C |
| Document type detected | Raster scan (scanned JPG) |
| Revision mode detected | CLOUD_SCOPE_MODE — extract within clouds only |
| Notes extracted | 62 unique notes, 8 abbreviations |
| SOW symbols loaded | 100 ALLOW + 32 BLOCK |
| Candidates detected | 264 across 117 image patches |
| After deduplication | 233 final candidates (11.7% duplicate rate) |
| Disciplines | 149 Instrumentation + 57 Mechanical + 27 Piping |
| Confidence average | 0.554 (needs tuning — see Next Steps) |

---
