# CDCI P&ID Tag Extraction System
## Complete Technical Reference

---

## What We Built — The Simple Version

We have built an automated system that reads engineering drawings — the technical diagrams called P&IDs (Piping and Instrumentation Diagrams) — and extracts every equipment tag from them. A tag is a label like `FIT-1001` or `V-BV-2246` that identifies a physical instrument or valve in a plant. Doing this manually takes a team days per drawing. Our system does it in under 10 minutes per drawing, automatically.

The system is a pipeline of Python scripts. Each script does one specific job and passes its results to the next. The drawing goes in one end; a structured Excel register of all tags comes out the other end. Every script saves its work to a shared file called `drawing_context.json` so every later step knows everything the earlier steps already found.

The system uses three types of technology working together: **Google Gemini AI** to understand what it sees in the image, **Tesseract OCR** to read text precisely, and **OpenCV** to process the image geometry. The key idea is that Gemini understands meaning (what is a ball valve, where is the title block) while Tesseract handles accuracy (the exact characters in a tag number). Neither one alone is enough.

---

## The Pipeline Phases

The pipeline is divided into five phases. The first three phases are the most important to understand in sequence because each phase builds on what the previous one learned.

```
Phase 1  Context      step1 → step2 → step2b → [step2c] → step3 → step4 → step6
                                                ↑ optional human cloud review
Phase 2  Extraction   step5a  (SAHI + Gemini + Tesseract)
Phase 3  Validate     step5b → step5b2 → step5c → step5d
                               ↑ connectivity / hierarchy / flow / control-loops
Phase 4  Deliverable  step7 → step8
Phase 5  Reporting    eval_coverage · compare_final_vs_annexure4 · stage_visualizer
```

There is also an important design decision that runs throughout Phases 2 and 3: the pipeline deliberately keeps two separate extraction chains. One chain runs on the full drawing and feeds the hierarchy analysis (step5b2 through step9). The other runs cloud-scoped and produces the revision deliverable (final_tags.xlsx). They use the same scripts but different input files and flags. This is covered in detail in the Step 5B2 section below.

---

## The Scripts — What Each One Does

---

### Step 1 — `stages/step1_format_detect.py`
**What it does:** The very first thing the pipeline does is look at the drawing file and understand what kind of file it is. Is it a PDF that still has text embedded inside it (called a vector PDF)? Or is it a scanned image where everything is just pixels? This matters because vector PDFs can have their text extracted directly — no AI needed. Raster scans need image processing.

For our drawings, which are scanned images, it applies a technique called **CLAHE** (Contrast Limited Adaptive Histogram Equalisation) which makes the thin lines and faint text in engineering drawings much sharper and cleaner before any AI looks at it. The enhanced image is saved as a binary PNG that all downstream OCR steps use instead of the raw original. It also records the drawing's pixel dimensions into `drawing_context.json` so every later step knows the coordinate space it is working in.

**AI used:** Optional Gemini call (1 call) for document type classification
**What you tell Gemini:** "Classify this document: is it a vector PDF, a raster scan, or a hybrid? Does it have revision clouds? What type of drawing is it?"
**What Gemini returns:** A JSON object with `document_type`, `has_revision_clouds`, `image_quality`, `confidence`
**Output files created:** `drawing_context.json` (master shared file all other steps update), `input_drawing_enhanced_binary.png`

**Run command:**
```bash
python3 stages/step1_format_detect.py $DRAWING --out output/ --api-key $GEMINI_KEY
```

---

### Step 2 — `stages/step2_title_block.py`
**What it does:** Every engineering drawing has a title block — the bordered table in the bottom-right corner with the drawing number, sheet number, revision, project name, and revision history. This step finds and reads it.

It does this in two Gemini calls. The first call reads all the basic fields: drawing number, title, revision code, discipline, date, who drew it and who approved it. The second call reads the revision history table (the rows that say things like "Rev C — Re-issued for Construction — 29-08-24") and checks for a specific sentence that means this is a revision drawing: "USE THIS DRAWING FOR INFORMATION WITHIN THE CLOUDED AREAS ONLY."

After reading those fields it makes a programmatic decision (no AI, just logic): is this a brand-new drawing (revision 0 or A) meaning we extract everything? Or is it a revision drawing (B, C, 1, 2 etc.) meaning we should only extract the things inside the revision clouds? This decision is called the **Revision Mode** and it controls everything downstream. It gets written into `drawing_context.json` as `extraction_scope` — either `FULL_DRAWING` or `CLOUD_ONLY` — and every downstream step checks this field to know what to do.

**AI used:** Gemini 2.5 Flash (2 calls)
**What you tell Gemini (Call 1):** "You are reading the title block of a P&ID. Extract drawing number, sheet, revision, title, project name, contract number, scale, dates, drawn by, checked by, approved by. Return as JSON."
**What you tell Gemini (Call 2):** "Read the revision history table. Extract each row. Also check if the drawing says 'CLOUDED AREAS ONLY'. Return revision history as JSON array plus cloud_scope_only flag."
**What Gemini returns:** Structured JSON with all title block fields and revision history
**Output files:** `title_block_context.json`, updates `drawing_context.json`

**Actual result on our test drawing:**
- Drawing: `4224-MGDV-6-50-2004`, Sheet 001, Revision C
- 5 revisions found (Rev 0 through Rev C)
- Revision notice detected → Revision Mode = **CLOUD_SCOPE_MODE**
- Decision: extract only inside revision cloud boundaries

**Run command:**
```bash
python3 stages/step2_title_block.py --context output/drawing_context.json --api-key $GEMINI_KEY
```

---

### Step 2B — `stages/step2b_cloud_detection.py`
**What it does:** If Step 2 determined this is a revision drawing (which it did for our test drawing), this step finds the revision clouds. Revision clouds are the scalloped, bumpy closed boundaries drawn around areas that changed in this revision. They look like clouds. Only the equipment inside these clouds is new or modified — everything outside is old and does not need to be extracted.

The cloud detector uses a two-stage method. First, it sends the drawing to Gemini to roughly locate where clouds are — Gemini returns bounding boxes around each cloud region. Then, for each located region, it crops that area and uses OpenCV morphological operations to trace the exact bumpy boundary of each cloud at pixel precision. This combination is important: Gemini is good at understanding "that bumpy shape is a cloud" across the whole drawing, but OpenCV is more precise at tracing the exact polygon boundary once you know where to look.

The step also includes a border and line-artifact rejection pass. Long straight shapes that touch the drawing frame (the outer border) or run nearly the full width of the sheet are rejected as pipeline lines, not clouds. Without this filter, major pipes running across the drawing would be mistakenly included as cloud regions. The `border_filter.jpg` output shows exactly what was rejected so you can verify the filter is working correctly.

The resulting cloud geometry is written to `drawing_context.json` and to `outer_clouds_v2.json`. Step 5A reads these polygon coordinates to gate its extraction.

**AI used:** Gemini 3.1 Pro Preview (cloud localization calls)
**Output files:** `outer_clouds_v2.json` (polygon coordinates), `overlay_v2.jpg` (drawing with cloud boundaries drawn), `cloud_mask_v2.png` (binary mask), `border_filter.jpg` (rejected border candidates)

**Actual result:** ~23 outer + ~20 inner clouds detected on test drawing

**Run command:**
```bash
python3 stages/step2b_cloud_detection.py $DRAWING --out output/ --api-key $GEMINI_KEY
```

---

### Step 2C — `step2c_cloud_editor/step2c_cloud_editor.py` *(optional)*
**What it does:** This is an optional human-in-the-loop step that sits between step2b and step3. It opens a browser-based editor where a person can review the cloud boundaries that step2b detected, correct any that are wrong, add missing clouds, remove false detections, and then approve the final geometry before extraction begins.

Why does this exist? Cloud detection is hard. The drawings are complex, the cloud boundaries can be faint, and a misdrawn cloud region means either missed tags (if the cloud is drawn too small) or scope creep (if the cloud boundary is drawn too wide). Step2b's automated detection is good but not perfect. For high-stakes revision deliverables where the cloud scope is being disputed or is unusually complex, having a human verify the geometry before running the expensive step5a extraction saves significant rework.

The step is optional — if you skip it, step5a automatically falls back to step2b's output. Step5a logs which source it chose at startup so you always know which cloud geometry the extraction used.

**AI used:** None — browser editor, no API calls
**What you do:** Open the browser, add/delete/merge/extend cloud polygons, click Done → the script writes the approved files and exits
**Output files:** `approved_clouds.json`, `cloud_mask_approved.png`, `overlay_approved.jpg`
**Step5a precedence:** Prefers `approved_clouds.json` when present; otherwise uses `outer_clouds_v2.json`

**Run command:**
```bash
python3 step2c_cloud_editor/step2c_cloud_editor.py \
  --image $DRAWING \
  --clouds output/outer_clouds_v2.json \
  --overlay output/overlay_v2.jpg \
  --out output/
# Browser opens → edit → click Done → script exits
# Add --no-browser or --port N for headless/port control
```

---

### Step 3 — `stages/step3_notes_agent.py`
**What it does:** Engineering drawings always have a notes section — a numbered list of special rules specific to that project. Things like: "All valves with an F prefix are field-routed lines" or "Refer to drawing 4224-MGDTY-6-50-2001-003 for interlock details." These notes change how we should interpret everything else on the drawing.

This step finds and reads all those notes and turns them into a set of machine-readable rules. It splits the drawing into regions (left margin upper, left margin lower, bottom strips, abbreviations block, reference drawings block, title block area) and processes each one separately. For each region it first runs Tesseract OCR to get the raw text, then sends both the image and the OCR text to Gemini and asks it to understand the semantic meaning of each note.

The most important output is a file called `rules_prompt_block.txt`. This is injected as a context block into every downstream Gemini call — specifically into every one of step5a's ~315 extraction calls. This is how drawing-specific knowledge propagates through the whole pipeline. When Gemini looks at a patch and sees an instrument, it already knows the drawing-specific rules about how to interpret it.

**AI used:** Gemini 2.5 Flash (all regions)
**What you tell Gemini:** "You are extracting engineering notes from a P&ID. This is the [region name] region. The OCR extracted this text: [raw text]. Extract every note. For each note give the raw text, the type (abbreviation / equipment rule / scope rule / drafting rule), and a machine-readable rule statement. Return as JSON."
**What Gemini returns:** JSON with all notes, abbreviations dictionary, global constraints, tag detection rules
**Output files:** `notes_context.json`, `rules_prompt_block.txt`

**Actual result:** 62 unique notes extracted, 8 abbreviations learned

**Run command:**
```bash
python3 stages/step3_notes_agent.py --context output/drawing_context.json --api-key $GEMINI_KEY
```

---

### Step 4 — `stages/step4_sow_agent.py`
**What it does:** SOW stands for Scope of Work. The client provides an Excel file with two sheets: one listing every symbol type that should be extracted (like Ball Valve, Flow Transmitter, Pressure Gauge) and one listing every symbol type that must never be extracted (like Computer Function Signal Tags, Soft Tags, Flange symbols). This step reads both sheets and builds a memory object that all downstream steps use to filter their results.

There are 100 symbols in the ALLOW list and 32 in the BLOCK list. For each symbol the system reads the name and description from the Excel. Optionally, if a Gemini API key is provided, it also sends the symbol image from the Excel to Gemini and asks it to describe exactly what that symbol looks like visually — its primary shape, internal markings, connection pattern. This gives the extraction step the ability to match symbols by appearance rather than just by name.

The output is a file called `sow_symbol_memory.json`. Step 5A checks every detected symbol against this memory before deciding whether to include it in the output.

**AI used:** Gemini 2.5 Flash (optional, ~132 calls — one per symbol image; skip with `--skip-vision`)
**What you tell Gemini:** "You are an ISA 5.1 expert. Describe this P&ID symbol image. What is the primary shape? What are the internal markings? What are the connections? How would you identify this on a drawing?"
**What Gemini returns:** JSON with `primary_shape`, `internal_markings`, `distinctive_features`, `isa_match_hint`
**Actual result:** 132 symbols parsed — 100 ALLOW (Valve ×16, Transmitter ×11, Switch ×8, Gauge ×6, Pump ×5, others) + 32 BLOCK (Signal/Logic ×19, Piping ×5, others)
**Output files:** `sow_symbol_memory.json`, `sow_scope_summary.txt`

**Run command:**
```bash
# Fast (no vision, text-only):
python3 stages/step4_sow_agent.py build --excel $SOW --out output/ --skip-vision

# Full (with symbol image understanding):
python3 stages/step4_sow_agent.py build --excel $SOW --out output/ --api-key $GEMINI_KEY
```

---

### Step 6 — `stages/step6_table_agent.py`
**What it does:** Many drawings contain embedded tables — Tag Lists, Equipment Schedules, Line Lists. A Tag List is a grid where each row is one process slot and each column is one instrument type, with the actual tag numbers as the cell values. This step scans the drawing for any tables, detects their grid structure using OpenCV line detection, runs Tesseract OCR on the cells, and then asks Gemini to parse the whole table into structured rows and columns.

It scans in priority order: top-left first (where tag lists most commonly sit), then the full top strip, then the right margin. It stops when it finds high-confidence tables to avoid wasting API calls on empty regions.

On our test drawing (sheet 001), the Tag List table is on the companion sheet 002, not on this sheet. So step6 returns 0 meaningful tags on sheet 001 — this is expected, not a bug. The `master_tags.json` output is used by step5c to do a presence check: if a tag was in the table list, it gets higher validation confidence.

**AI used:** Gemini 2.5 Flash (2–4 detection calls + 1–2 extraction calls) + Tesseract + OpenCV
**What you tell Gemini (detection):** "Find all tabular structures in this image region. For each table give its title, type, bounding box, and estimated row and column count."
**What you tell Gemini (extraction):** "Extract the complete table. Here is the pre-extracted OCR text as a guide. Return as JSON with headers array and rows array."
**Output files:** `tables_context.json`, `master_tags.json`

**Run command:**
```bash
python3 stages/step6_table_agent.py --context output/drawing_context.json --api-key $GEMINI_KEY
```

---

### Step 5A — `stages/step5a_candidate_extraction.py`
**What it does:** This is the main extraction step — the core of the whole system. It takes the drawing and systematically finds every engineering symbol on it.

The drawing is too large to send to Gemini as one image — instrument bubbles are only 60–100 pixels across on a 9,934×7,017 pixel sheet, and they would be invisible at the scale needed to fit the full drawing in a single API call. So we use a technique called **SAHI** (Slicing Aided Hyper Inference). The drawing is cut into overlapping patches of 768×768 pixels each with 40% overlap, upscaled to 1024 px for the Gemini call, so symbols at the edges of patches are not missed. The full drawing requires ~315 patches; a cloud-scoped run covers ~53 patches. All patches are processed in parallel using 8 worker threads, making this 8 times faster than sequential processing.

For each patch, two things happen in sequence. First, Tesseract OCR scans the patch and finds every text string that looks like an engineering tag. Second, Gemini looks at the same patch and identifies every engineering symbol — instrument bubbles, valves, pumps, compressors — and associates each one with its nearest tag text. The OCR result is passed to Gemini as a ground-truth hint so it corrects any OCR errors in its final tag reading.

Then filtering is applied. The **revision cloud filter** checks whether each detected symbol falls inside a cloud boundary. On a revision drawing, anything outside the cloud boundaries is discarded. The `--force-full-drawing` flag bypasses this filter — use it when you need full Annexure-4 recall or when building the hierarchy. The **SOW filter** then checks each symbol type against the memory from Step 4 and discards any that are in the BLOCK list.

There is an important design point here. Gemini is **not** asked to determine whether a detected symbol is inside a cloud or not. Cloud gating is done entirely in code using the pixel mask from step2b. This is deliberate: Gemini makes poor judgements about cloud boundaries on small image patches because it cannot see the full cloud shape. The code-based mask is faster, cheaper, and more accurate.

**AI used:** Gemini 3.1 Pro Preview (~315 calls full / ~53 calls cloud scope, 8 in parallel, temperature=0.0 for deterministic output) + Tesseract OCR (all patches, local)
**What you tell Gemini:** "You are an expert P&ID extraction agent. Scan this image patch top-left to bottom-right. Detect every instrument bubble, valve, pump, compressor, heat exchanger, vessel, and analyzer. For each detected symbol: identify its type, find the nearest associated tag text, record its bounding box, describe its functional context. Only extract what is visually present. Never hallucinate. Return as JSON."
**What Gemini returns:** JSON array of candidates, each with `candidate_id`, `tag_text`, `symbol_name`, `symbol_category`, `symbol_bbox`, `tag_bbox`, `scope_type`, `ocr_confidence`, `vision_confidence`, `functional_context`, `sow_status`
**Output files:** `step5a_candidates.json`, `step5a_patches/` (with `--debug`)

**Extraction modes:**

| Mode | Command flag | Patches | Annexure-4 recall | Use when |
|------|-------------|---------|-------------------|----------|
| **Full sheet** | `--force-full-drawing` | ~315 | **42/46 (91%)** | Register comparison, hierarchy input |
| **Cloud scope** | *(default on CLOUD_ONLY drawings)* | ~53 | ~2–25/46 | Revision deliverable only |

**Run command:**
```bash
# Full sheet (recommended for register comparison):
python3 stages/step5a_candidate_extraction.py \
    --context output/drawing_context.json --api-key $GEMINI_KEY \
    --workers 8 --force-full-drawing

# Cloud scope only:
python3 stages/step5a_candidate_extraction.py \
    --context output/drawing_context.json --api-key $GEMINI_KEY --workers 8
```

---

### Step 5B — `stages/step5b_geometric_association.py`
**What it does:** Step 5A gives us a list of detected symbols and their tag texts, but it does not tell us which pipe each instrument is connected to or which equipment each instrument belongs to. Step 5B figures that out using pure geometry — no AI needed.

It scans the drawing image for all horizontal and vertical lines (pipes) using OpenCV morphological operations. For each candidate from Step 5A it then calculates: Is there a leader line connecting the tag text to the symbol? Which pipe is closest to this symbol? Is this instrument contained inside a larger equipment boundary?

Based on these geometric calculations it assigns each candidate a spatial relationship: `ATTACHED_TO` (the tag directly connects to the symbol via a short line), `CONNECTED_TO` (the symbol sits on a pipe), `CONTAINED_WITHIN` (an instrument is inside an equipment boundary), or `ADJACENT_TO`.

The geometric association data flows downstream in two directions. For the revision deliverable chain, it feeds step5c validation. For the hierarchy chain, the full-drawing version feeds step5b2 to build the connectivity graph.

**AI used:** None — pure OpenCV geometry
**Output files:** `step5b_associations.json`

**Run command:**
```bash
python3 stages/step5b_geometric_association.py \
    --candidates output/step5a_candidates.json \
    --image $DRAWING --out output/
```

---

### Step 5B2 — `stages/step5b2_hierarchy.py`
**What it does:** This step builds the connectivity graph, process hierarchy, flow direction, and control loop map for the drawing. It is a side branch that runs after step5b and produces enrichment data that step7 uses to fill the `PARENT_EQUIP`, `FLOW`, and `CONTROL_LOOP` fields in the deliverable.

Understanding why this step must exist requires understanding a limitation of P&ID extraction: tags are extracted as a flat list, but on the actual drawing they are connected — instruments belong to equipment, valves sit on process lines, transmitters feed controllers. Without this structure, every instrument in the output looks equally isolated even though on the drawing they are clearly part of a named equipment train. Step5b2 recovers this structure from the geometry.

It runs in three tracks. **Track A** builds the equipment hierarchy — it finds which instruments are directly attached to which equipment items, creating parent-child relationships. Out of 170 instruments on our test drawing, 25 have a definite equipment parent. **Track B** determines flow direction — it traces pipe connections, reads arrowheads and check-valve symbols, and propagates flow direction from sources to sinks. Of 389 pipe segments, 45 have determined flow direction. **Track C** identifies control loops — it follows dashed signal lines (the lines drawn between a sensor and its controller) to group related instruments into loops.

The key design constraint for this step is that it **must always run on full-drawing extraction**, never on cloud-scoped output. The reason: an instrument inside a revision cloud can have its parent equipment located outside the cloud. If step5b2 only sees cloud-scoped candidates, it cannot find that parent. The full-drawing context is required for the hierarchy to be complete. The revision deliverable can still be cloud-scoped — but the hierarchy enrichment of that deliverable must come from a full-drawing context.

Step7 joins the hierarchy data to the revision-deliverable candidates by `candidate_id`. This means step5b2 must be re-run whenever step5a or step5b re-run — the candidate IDs change with every fresh extraction. A stale hierarchy produces zero matches and silently loses all enrichment. Step7 logs a `STALE hierarchy` warning when it detects this condition (fewer than 50% of candidates match).

**AI used:** None by default. Optional `--gemini-flow-fallback` makes ~6 Gemini calls (cached in `gemini_flow_cache.json`) for category-D ambiguous flow cases.
**Output files:** `step5b2_hierarchy_full.json` (primary), `step5b2_hierarchy.json` (backward-compatible alias)

**Run command:**
```bash
python3 stages/step5b2_hierarchy.py \
    --associations output/step5b_associations_full.json \
    --image $DRAWING --out output/
# optional: --gemini-flow-fallback --api-key $GEMINI_KEY
```

---

### Step 5C — `stages/step5c_validation_engine.py`
**What it does:** Every candidate from the previous steps now goes through a series of programmatic checks — no AI, just rules. There are three levels of checking.

First, does the tag text match a valid ISA 5.1 format? We check against 30 regex patterns covering every instrument type: FIT, PT, TT, LT, BV, GV, SDV, PSV, and more. The patterns allow 3–6 digit loop numbers so they correctly handle both short tags like `PT-208` and longer ones like `FIT-1001`.

Second, do the business rules pass? Three rules are enforced: a control valve must carry a function letter prefix (F, T, P, L, A, Z, X), a transmitter tag must end in T or IT, and a relief valve must start with RV or PSV.

Third, does this tag exist in the client's asset register? We look it up in the Annexure-4 Excel file with a normalised key comparison that strips unicode dashes, inch marks, and spaces so formatting differences do not cause false mismatches.

The FAIL count from this step is often high on new project drawings — that is expected. The 46-tag Annexure-4 register is a sample, not a complete list. Many valid tags on the drawing simply do not appear in the register yet. The validation step does not reject them; it flags them for HUMAN_REVIEW so a person can verify.

**AI used:** None — pure programmatic
**Output files:** `step5c_validated.json`

**Run command:**
```bash
python3 stages/step5c_validation_engine.py \
    --associations output/step5b_associations.json \
    --register $REGISTER --notes output/notes_context.json --out output/
```

---

### Step 5D — `stages/step5d_duplicate_resolution.py`
**What it does:** Because SAHI uses 40% overlap between patches, the same symbol near a patch boundary gets detected in both patches. Step 5D finds and removes these duplicates. It uses an algorithm called **Spatial Mask Merging (SMM)** with three signals: bounding box overlap (IoU), distance between symbol centres, and tag text similarity. If two candidates have overlapping boxes and the same (or very similar) tag text, they are the same detection. One is kept as PRIMARY and the other is marked DISCARDED.

The deduplication is designed to be recall-safe. It merges only clearly overlapping boxes — it never merges sequential tag numbers from a valve bank (like V-BV-2244 and V-BV-2245) even if they are physically close, because those are two different physical valves. The algorithm requires both high IoU and high text similarity before merging.

It also handles a specific OCR fragmentation pattern: sometimes a tag like `V-FE-224` is read as two fragments — `V-FE` and `FE-224` — by OCR. The fragment merge rule reunites these if both pieces are within 300 pixels of each other.

The `step5_final_output.json` file written by this step contains only the PRIMARY candidates and is what step7 and step8 process.

**AI used:** None — pure geometric algorithm
**Output files:** `step5d_deduped.json`, `step5_final_output.json`

**Run command:**
```bash
python3 stages/step5d_duplicate_resolution.py \
    --validated output/step5c_validated.json --out output/
```

---

### Step 7 — `stages/step7_cedm_normalizer.py`
**What it does:** CEDM stands for Common Engineering Data Model — it is the standard format the client system expects tags to be in. Raw tags from OCR are often messy: lowercase letters, dots instead of hyphens, extra spaces, en-dashes instead of hyphens. This step cleans all of that up. `fit.1001` becomes `FIT-1001`. `P - 101` becomes `P-101`. `V-BV--2246` becomes `V-BV-2246`. Inch notations like `10"` become `10IN`.

It also fills in the other 14 fields that the Annexure-4 output template requires. **Discipline** is inferred from the tag prefix: 40+ prefix codes are mapped to INSTRUMENTATION, MECHANICAL, PIPING, or ELECTRICAL. **Equipment Description** is built from an engineering ontology that maps symbol types to descriptions like "VALVE,BALL,1IN". **Document Number**, **Sheet**, **Revision**, **Document Title**, and **DOC Status** all come from the title block data read by step2. A stable Canonical ID is generated for each tag using a hash of the project ID and tag number, so the same tag always gets the same ID across runs.

For the tag description field, step7 uses a priority hierarchy: the client's Annexure-4 registry entry is used first if the tag is already known; otherwise the `functional_context` field that Gemini wrote during step5a extraction is used; otherwise the ontology generates a generic description. This means well-known tags get their established client descriptions while novel tags get the best available contextual description Gemini could infer from the drawing.

Step7 also auto-loads the hierarchy data from step5b2 (`step5b2_hierarchy_full.json`). For each candidate it looks up connectivity enrichment by `candidate_id` and appends `PARENT_EQUIP: <tag>`, `FLOW: upstream|downstream`, and `CONTROL_LOOP: <id>` to the REMARKS field. Isolated detections — instruments with no pipe, equipment, or signal edge connecting them — are flagged with `ISOLATED_DETECTION`.

**AI used:** None — pure programmatic normalisation using regex and lookup tables
**Output files:** `step7_cedm_output.json`

**Run command:**
```bash
python3 stages/step7_cedm_normalizer.py \
    --final output/step5_final_output.json \
    --context output/drawing_context.json --out output/ --project CDCI
```

---

### Step 8 — `stages/step8_confidence_router.py`
**What it does:** The final step takes every normalised tag and calculates a single confidence score using a weighted formula:

```
C_final = 0.30 × C_detect      (Gemini vision_confidence from step5a)
        + 0.30 × C_text         (Tesseract OCR confidence; falls back to Gemini if silent)
        + 0.15 × C_geometry     (association_confidence from step5b)
        + 0.20 × C_validation   (ISA check scores from step5c)
        + 0.05 × C_registry     (1.0 if in register, 0.5 if not)
```

Based on the final score, every tag is routed to one of three outcomes:
- **AUTO_ACCEPT** (score ≥ 0.80): Written directly to the final Excel output, no human review needed
- **HUMAN_REVIEW** (score 0.55–0.80): Added to a review queue with a priority label (P1 Critical through P4 Low) and the specific reason it needs review
- **AUTO_REJECT** (score < 0.55): Saved to an audit log but excluded from the output

Isolated detections (flagged by step5b2 and carried through step7) get an additional penalty: their geometry component is multiplied by 0.5, which typically pushes them into P3 HUMAN_REVIEW.

The final deliverable is `final_tags.xlsx` — an Excel file matching the Annexure-4 format exactly — with three sheets: AUTO_ACCEPT (high-confidence, ready for client), HUMAN_REVIEW (medium-confidence with priority and reason columns), and SUMMARY (run statistics).

**AI used:** None — pure mathematical formula
**Output files:** `final_tags.xlsx`, `human_review_queue.json`, `audit_log.json`, `step8_routing_summary.json`

**Run command:**
```bash
python3 stages/step8_confidence_router.py \
    --cedm output/step7_cedm_output.json \
    --context output/drawing_context.json --out output/
```

---

### Step 9 — `stages/step9_hierarchy_deliverables.py`
**What it does:** This step converts the raw JSON hierarchy output from step5b2 into presentation-ready artefacts for the engineering team to review and hand off to the client. It does not make any additional AI calls — it is a pure formatter and report generator.

The step produces four outputs. The **hierarchy Excel** (`final_hierarchy.xlsx`) has six sheets: Equipment Hierarchy (the tree structure), Parent-Child Relationships (flat table of every parent-child link), Functional Location (synthesised dotted-path location codes), Cross-Drawing References (cross-sheet instruments, if present), Orphan Nodes (instruments with no resolved parent), and Hierarchy Statistics.

The **interactive viewer** (`hierarchy_viewer.html`) is a self-contained browser app with no external dependencies. It shows the hierarchy as an expandable tree with search by tag number, equipment name, or functional location. A second tab — the Relationship Explorer — shows the full relationship set for any selected tag: its parent, grandparent, children, the instruments it controls, the instruments that monitor it, and the lines it is connected to.

The **force-directed graph** (`hierarchy_graph.html`) is another self-contained browser page showing every node and edge as a colour-coded network: blue for equipment, green for instruments, orange for control devices, purple for virtual connector nodes, red for orphans. You can drag, zoom, and click any node to see its connections.

The **validation report** (`hierarchy_validation_report.xlsx`) lists structural problems in the hierarchy: nodes with multiple parents, cyclic references, missing parent/system/functional-location, broken cross-drawing references, duplicate nodes, and disconnected subgraphs — each with a severity rating and a suggested fix.

One important data-provenance note: the drawing itself does not contain Plant/Area/Unit codes or Functional Location paths. These are derived from the drawing number (`4224-MGDV-6-50-2004` → Plant `MGDV`, Area `6-50`, Unit `001`) and the hierarchy structure. This derivation is clearly labelled in the output so reviewers understand it is synthesised, not directly read from the drawing.

**AI used:** None
**Output files:** `final_hierarchy.xlsx`, `hierarchy_viewer.html`, `hierarchy_graph.html`, `hierarchy_validation_report.xlsx`

**Run command:**
```bash
python3 stages/step9_hierarchy_deliverables.py \
    --hierarchy output/step5b2_hierarchy_full.json \
    --context output/drawing_context.json --out output/
```

---

### Phase 5 — Reporting Tools
**What they do:** Three QA tools that run after the main pipeline and answer the key validation questions.

`eval_coverage.py` answers: did we find the tags that are supposed to be on this drawing? It compares step5a output against the Annexure-4 ground truth register and reports FOUND/MISSING counts with an annotated drawing image where found tags are boxed in green and missed tags are marked.

`compare_final_vs_annexure4.py` answers: what is in our final Excel vs what is in the register? It produces a four-sheet comparison Excel: AUTO_ACCEPT tags that match the register, AUTO_ACCEPT tags that do not, HUMAN_REVIEW tags that match the register, and HUMAN_REVIEW tags that do not. This is the right tool for measuring precision and recall on the delivered output.

`stage_visualizer.py` answers: at which stage did each tag get filtered out? It produces QA images for each pipeline stage — all detected tags, tags after SOW filtering, tags after deduplication — so you can visually trace why a specific tag ended up in the output or was removed.

**Run commands:**
```bash
python3 stages/eval_coverage.py \
    --candidates output/step5a_candidates.json \
    --register $REGISTER --image $DRAWING --out output/

python3 stages/compare_final_vs_annexure4.py \
    --final output/final_tags.xlsx --register $REGISTER \
    --out output/final_tags_vs_annexure4.xlsx

python3 stages/stage_visualizer.py \
    --candidates output/step5a_candidates.json \
    --deduped output/step5d_deduped.json \
    --sow output/sow_symbol_memory.json \
    --image $DRAWING --out output/stages/
```

---

## The Technology Stack

| Technology | What It Is | Where We Use It |
|---|---|---|
| **Gemini 3.1 Pro Preview** | Google's most capable multimodal AI. Understands complex engineering layouts, symbol arrangements, and drawing conventions at high accuracy. | Step 5A (tag extraction), Step 2B (cloud localization) |
| **Gemini 2.5 Flash** | Faster, cheaper Gemini model for structured extraction tasks where layout is simpler and throughput matters more than depth. | Steps 1, 2, 3, 4, 6 |
| **Tesseract OCR** | Google's open-source OCR engine. Runs locally, no API cost. Reads text from images with high character-level accuracy and provides per-character confidence scores. | Steps 2, 3, 5A, 6 |
| **OpenCV** | Computer vision library for image processing. Used for CLAHE enhancement, morphological operations, pipe-line detection, cloud boundary tracing, and drawing bounding boxes. | All steps |
| **SAHI** | Slicing Aided Hyper Inference — splits large images into overlapping patches so small objects (60–100 px instrument bubbles on a 10,000 px drawing) are not missed by the AI. | Step 5A |
| **openpyxl** | Python library for reading and writing Excel files. Reads the symbol scope Excel and writes the final output register in Annexure-4 format. | Steps 4, 5C, 8, 9 |

---

## How Gemini Prompting Works

When we send an image to Gemini along with a text instruction, the instruction is called a **prompt**. The prompt tells Gemini exactly what role to play, what to look for, what format to return the answer in, and what not to do.

A good prompt has four parts. The first part sets the role: "You are an expert P&ID extraction agent." The second part describes the task: "Detect every instrument bubble and valve in this image patch." The third part specifies constraints: "Only extract what is visually present. Never hallucinate. Do not assign a tag to a symbol unless visual evidence exists." The fourth part defines the output format: "Return ONLY a JSON object with this structure: {candidates: [...]}".

The reason we always ask for JSON is that it can be parsed by code reliably. If Gemini returned a paragraph of text, we would have to guess how to extract the information. JSON means the data flows directly into the next step without any interpretation.

The temperature setting controls how creative or deterministic Gemini is. We set temperature=0.0 for all extraction tasks. This means Gemini always gives the most likely answer rather than exploring different possibilities. Creativity is useful for writing. For precise data extraction from technical drawings, we want the same input to always give the same output.

The `rules_prompt_block.txt` from step3 is injected as a context section into every step5a Gemini call. This is how 62 drawing-specific notes travel through the whole extraction phase without any code changes — Gemini reads them as part of its instructions.

---

## The Complete Run Order

```bash
# Prerequisites (run once per machine)
cd /Users/suryprakash/Downloads/cdci_extractor_final
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
brew install tesseract

# Set environment
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
export DRAWING="input_drawing.jpg"
export REGISTER="ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx"
export SOW="ANNEXURE-2_CDC-SYMBOLS_USED_AND_NOT_USED.xlsx"

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Context (run once per drawing, ~2–4 min, uses Gemini API)
# ══════════════════════════════════════════════════════════════════════════════

python3 stages/step1_format_detect.py  $DRAWING --out output/ --api-key $GEMINI_KEY
python3 stages/step2_title_block.py    --context output/drawing_context.json --api-key $GEMINI_KEY
python3 stages/step2b_cloud_detection.py $DRAWING --out output/ --api-key $GEMINI_KEY

# OPTIONAL — step2c: human review of revision clouds (browser, no API).
# Skip it and step5a falls back to step2b output automatically.
python3 step2c_cloud_editor/step2c_cloud_editor.py \
  --image $DRAWING --clouds output/outer_clouds_v2.json \
  --overlay output/overlay_v2.jpg --out output/

python3 stages/step3_notes_agent.py    --context output/drawing_context.json --api-key $GEMINI_KEY
python3 stages/step4_sow_agent.py build --excel $SOW --out output/ --skip-vision
python3 stages/step6_table_agent.py    --context output/drawing_context.json --api-key $GEMINI_KEY

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Full drawing extraction for hierarchy (Gemini API, ~4 min, 8 workers)
# ══════════════════════════════════════════════════════════════════════════════

python3 stages/step5a_candidate_extraction.py \
  --context output/drawing_context.json --api-key $GEMINI_KEY \
  --workers 8 --force-full-drawing
cp output/step5a_candidates.json output/step5a_candidates_full.json

python3 stages/step5b_geometric_association.py \
  --candidates output/step5a_candidates_full.json --image $DRAWING --out output/
cp output/step5b_associations.json output/step5b_associations_full.json

python3 stages/step5b2_hierarchy.py \
  --associations output/step5b_associations_full.json --image $DRAWING --out output/

python3 stages/step9_hierarchy_deliverables.py \
  --hierarchy output/step5b2_hierarchy_full.json \
  --context output/drawing_context.json --out output/

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — Post-processing for revision deliverable (no API, < 30 s)
# ══════════════════════════════════════════════════════════════════════════════

# For a revision drawing: re-run step5a cloud-scoped for the deliverable chain.
# For a full extraction: the full candidates above can go directly to step5c/5d.
python3 stages/step5a_candidate_extraction.py \
  --context output/drawing_context.json --api-key $GEMINI_KEY --workers 8

python3 stages/step5b_geometric_association.py \
  --candidates output/step5a_candidates.json --image $DRAWING --out output/
python3 stages/step5c_validation_engine.py \
  --associations output/step5b_associations.json \
  --register $REGISTER --notes output/notes_context.json --out output/
python3 stages/step5d_duplicate_resolution.py \
  --validated output/step5c_validated.json --out output/

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — Deliverable (no API, < 10 s)
# ══════════════════════════════════════════════════════════════════════════════

python3 stages/step7_cedm_normalizer.py \
  --final output/step5_final_output.json \
  --context output/drawing_context.json --out output/ --project CDCI
python3 stages/step8_confidence_router.py \
  --cedm output/step7_cedm_output.json \
  --context output/drawing_context.json --out output/

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 5 — Reporting / QA (optional, no API)
# ══════════════════════════════════════════════════════════════════════════════

python3 stages/eval_coverage.py \
  --candidates output/step5a_candidates.json \
  --register $REGISTER --image $DRAWING --out output/
python3 stages/compare_final_vs_annexure4.py \
  --final output/final_tags.xlsx --register $REGISTER \
  --out output/final_tags_vs_annexure4.xlsx
python3 stages/stage_visualizer.py \
  --candidates output/step5a_candidates.json \
  --deduped output/step5d_deduped.json \
  --sow output/sow_symbol_memory.json \
  --image $DRAWING --out output/stages/
```

**Free-tier API key (5 RPM limit):** add `--workers 1` to step5a.
**Resume after failure:** each step writes its own output file. Delete that file and re-run from that step forward. Never delete `output/drawing_context.json` mid-pipeline.

---

## What Comes Out — The Output Files

| File | Step | What It Is |
|---|---|---|
| `drawing_context.json` | 1–6 | The master spine. Every step reads and updates this. Contains image path, title block, revision mode, cloud paths, notes paths, SOW path. |
| `title_block_context.json` | 2 | Drawing number, revision, title, all revision history rows. |
| `outer_clouds_v2.json` | 2B | Cloud polygons + bboxes in JSON space coordinates. |
| `overlay_v2.jpg` | 2B | Drawing with detected cloud boundaries drawn on it (green=outer, cyan=inner). |
| `cloud_mask_v2.png` | 2B | Binary filled cloud mask at full drawing resolution. |
| `approved_clouds.json` | 2C *(optional)* | Human-verified cloud geometry — step5a prefers this over step2b output when present. |
| `notes_context.json` | 3 | All extracted notes, abbreviations, drawing-specific rules. |
| `rules_prompt_block.txt` | 3 | The rules formatted as text, injected into every step5a Gemini call. |
| `sow_symbol_memory.json` | 4 | 132 symbols with ALLOW/BLOCK status and visual descriptions. |
| `master_tags.json` | 6 | Flat list of all tags found in embedded drawing tables. |
| `step5a_candidates.json` | 5A | Raw detected tags + bboxes + confidence scores (revision-deliverable chain). |
| `step5a_candidates_full.json` | 5A | Same, from full-drawing extraction (hierarchy chain input). |
| `step5b_associations_full.json` | 5B | Geometry associations off the full-drawing candidates (hierarchy chain input). |
| `step5b2_hierarchy_full.json` | 5B2 | **Primary hierarchy** (full drawing): connectivity graph, process hierarchy, Track B flow direction, Track C control loops. Feeds step7 and step9. |
| `step5b2_hierarchy.json` | 5B2 | Backward-compatible alias of `_full`. |
| `final_hierarchy.xlsx` | 9 | Engineer hierarchy register — 6 sheets. |
| `hierarchy_viewer.html` | 9 | Interactive expandable tree with search + Relationship Explorer tab. |
| `hierarchy_graph.html` | 9 | Self-contained colour-coded force-directed graph (no CDN required). |
| `hierarchy_validation_report.xlsx` | 9 | Hierarchy structural issues with severity + suggested fixes. |
| `step5_final_output.json` | 5D | PRIMARY candidates only (after deduplication). |
| `step7_cedm_output.json` | 7 | Normalised records with all 15 Annexure-4 fields populated + hierarchy enrichment. |
| `final_tags.xlsx` | 8 | **★ The client deliverable.** Three sheets: AUTO_ACCEPT, HUMAN_REVIEW, SUMMARY. |
| `human_review_queue.json` | 8 | Flagged items with reasons and P1–P4 priority levels for the review team. |
| `eval_coverage_report.json` | QA | FOUND/MISSING vs Annexure-4 ground truth. |
| `final_tags_vs_annexure4.xlsx` | QA | Four-sheet comparison: pipeline output vs Annexure-4 register. |

---

## Current Results — Our Test Drawing

| Metric | Result |
|---|---|
| Drawing | 4224-MGDV-6-50-2004, Sheet 001, Revision C |
| Document type detected | Raster scan (scanned image) |
| Revision mode detected | CLOUD_SCOPE_MODE — extract within clouds only |
| Clouds detected (step2b) | ~23 outer + ~20 inner |
| Notes extracted | 62 unique, 8 abbreviations |
| SOW symbols loaded | 100 ALLOW + 32 BLOCK |
| Step5a candidates (full drawing) | ~239 after intra-step dedup |
| Annexure-4 recall at step5a | **42/46 (91%)** — 2 tags not on this sheet |
| After deduplication (step5d) | ~198 PRIMARY candidates |
| AUTO_ACCEPT | ~142/198 (72%) |
| AUTO_REJECT | 0 |
| Average confidence | 0.813 |
| Tags not on sheet 001 | `V-ZSC-203`, `V-ZSO-203` (in register, not drawn) |

**Which metric tool to use:**

| Question | Tool | Metric |
|---|---|---|
| Did step5a detect the tag on the drawing? | `eval_coverage.py` | Unique 42/46 |
| What is in the final Excel vs the register? | `compare_final_vs_annexure4.py` | SUMMARY sheet — 41/46 unique |
| Row count in AUTO_ACCEPT matching Annexure-4 | compare script | 46 rows (includes duplicates) |

---

## Known Limitations

- `V-ZSC-203` and `V-ZSO-203` are in Annexure-4 but **not drawn** on sheet 001 — the pipeline cannot extract what is not there.
- The Tag List table is on companion sheet 002, not on sheet 001. Step6 returns 0 tags on sheet 001 — this is expected.
- The 46-tag register covers only a subset of the ~200+ tags on the drawing. Tags not in the register route to HUMAN_REVIEW by design — this is correct behaviour for a sparse register.
- Cloud-scoped extraction (`CLOUD_ONLY`) misses tags outside revision clouds even if they are in Annexure-4. Use `--force-full-drawing` for register comparison.
- Cloud detection quality depends on drawing scan quality. Always inspect `output/overlay_v2.jpg` before trusting cloud-scoped extraction.
- The hierarchy cross-drawing references field is empty on single-sheet extractions — this is expected. It populates when the companion sheets are also processed.

---

## What NOT to Do

- Do NOT use `stages/step5a_live_annotator.py` — it is reference code only, not for pipeline runs
- Do NOT delete `output/drawing_context.json` mid-pipeline — it is the shared state every step depends on
- Do NOT run step5a before step1 (step5a needs `raster_path` in `drawing_context.json`)
- Do NOT skip step6 before step5c (`master_tags.json` feeds the presence check in validation)
- Do NOT use `--workers > 1` on free-tier Gemini API keys (HTTP 429 rate limit errors)
- Do NOT compare Annexure-4 recall in cloud-scoped mode — use `--force-full-drawing` for that measurement
- Do NOT skip re-running step5b2 after a fresh step5a run — stale candidate IDs cause silent loss of all hierarchy enrichment in the final output
