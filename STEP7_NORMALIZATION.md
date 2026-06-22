# Step 7 — CEDM Normalization Engine
## Complete analysis of `stages/step7_cedm_normalizer.py`

> **Type:** Programmatic only — no Gemini, no API calls  
> **Input:** `output/step5_final_output.json` (PRIMARY candidates from step5d)  
> **Output:** `output/step7_cedm_output.json` → feeds step8 → `final_tags.xlsx`  
> **Purpose:** Transform raw extracted tags into the **15-column Annexure-4 register format**

---

## Where step7 sits in the pipeline

```
step5a  raw tags + bboxes + vision/ocr confidence
   ↓
step5b  geometry (pipes, equipment links)
   ↓
step5c  ISA validation + Annexure-4 registry lookup → validation_details
   ↓
step5d  deduplication → step5_final_output.json (PRIMARY only)
   ↓
step7   CEDM normalisation ← THIS STEP
   ↓
step8   confidence scoring + Excel routing
```

Step7 does **not** detect new tags. It **reshapes** what step5d already accepted into a client-ready register row.

---

## The five normalisation operations

Each candidate passes through five transforms in order:

| # | Operation | Function | What it produces |
|---|-----------|----------|------------------|
| 1 | **Tag text normaliser** | `normalise_tag()` | `TAG NUMBER` (canonical form) |
| 2 | **Description standardiser** | `standardise_description()` | `TAG DESCRIPTION` + `EQUIPMENT DESCRIPTION` |
| 3 | **Discipline classifier** | `classify_discipline()` | `DISCIPLINE` |
| 4 | **Canonical ID generator** | `make_canonical_id()` | `_canonical_id` (internal key) |
| 5 | **DOC STATUS mapper** | `map_doc_status()` | `DOC STATUS` |

Plus **metadata assembly**: drawing fields from title block, REMARKS from validation/SOW state, confidence signals for step8.

---

## Operation 1 — Tag Text Normaliser

**Function:** `normalise_tag(raw_tag) → (canonical_tag, transforms[])`

Takes the raw `tag_text` from step5a (after dedup in step5d) and produces a **canonical tag string** matching Annexure-4 style.

### Transform pipeline (applied in order)

| Step | Rule | Example |
|------|------|---------|
| 1 | Strip whitespace | `" FIT-1001 "` → `FIT-1001` |
| 2 | OCR character fixes | en-dash `–`, em-dash `—`, minus `−`, NBSP → ASCII hyphen/space |
| 3 | Inch notation | `10"` → `10IN`, `6''` → `6IN` (only after a digit) |
| 4 | Uppercase | `fit-1001` → `FIT-1001` |
| 5 | Separator normalisation | spaces, dots, slashes, underscores → single hyphen |
| 6 | Collapse hyphens | `V--BV--2246` → `V-BV-2246` |
| 7 | Remove illegal chars | keep only `A-Z`, `0-9`, `-`, `"` |
| 8 | Strip leading/trailing hyphens | `-FIT-1001-` → `FIT-1001` |

### Examples

| Raw (from pipeline) | Canonical `TAG NUMBER` | Transforms applied |
|---------------------|------------------------|-------------------|
| `V—NRV-748` | `V-NRV-748` | ocr_char_fix (unicode dash) |
| `10"-ETH-V061-61440X` | `10IN-ETH-V061-61440X` | inch_normalised + uppercased |
| `fit.1001` | `FIT-1001` | separators_normalised |
| `V-BV-2246` | `V-BV-2246` | no_change |
| `""` or garbage | `UNKNOWN` | no_valid_tag |

### Audit trail

Every transform is recorded in `_tag_transforms` on the output record. If OCR/separator fixes were applied, `REMARKS` gets `TAG_NORMALISED_FROM: <raw_tag>`.

---

## Operation 2 — Description Standardiser

**Function:** `standardise_description(symbol_name, canonical_tag, registry_entry, functional_context)`

Produces two Annexure-4 columns:

| Column | Annexure-4 example |
|--------|-------------------|
| `TAG DESCRIPTION` | `NRV,KO DRUM INLET LINE,V-V-201` |
| `EQUIPMENT DESCRIPTION` | `VALVE,CHECK,12IN` |

### Priority chain (first match wins)

```
1. Annexure-4 registry entry (from step5c validation_details)
      ↓ if not in register
2. functional_context (from step5a Gemini patch extraction)
      ↓ if empty
3. Engineering ontology (_ONTOLOGY regex table — 50+ symbol patterns)
      ↓ if no pattern match
4. Fallback: TAG DESCRIPTION = canonical_tag, EQUIPMENT = symbol_name
```

### Registry path (highest priority)

When step5c finds the tag in `ANNEXURE-4_*.xlsx`, `validation_details` contains:

```json
{"rule": "REGISTRY", "in_registry": true, "registry_entry": {
  "description": "...",
  "equipment_description": "...",
  "discipline": "...",
  "size_rating": "..."
}}
```

Step7 extracts this via `_extract_registry_entry()` and uses the **exact client descriptions** — no guessing.

### Ontology path (fallback)

`_ONTOLOGY` is a list of `(regex_pattern, equipment_description, tag_prefix)` tuples:

| symbol_name from step5a | EQUIPMENT DESCRIPTION | TAG DESCRIPTION |
|-------------------------|----------------------|-----------------|
| `"Ball Valve"` | `VALVE,BALL` | `BV,<tag_number_suffix>` |
| `"Flow Indicating Transmitter"` | `TRANSMITTER,FLOW,INDICATING` | `FIT,<suffix>` |
| `"Non-Return Valve"` | `VALVE,CHECK` | `NRV,<suffix>` |
| `"Thermowell"` | `THERMOWELL` | `TW,<suffix>` |

Pattern matching is case-insensitive regex on `symbol_name` from step5a.

### functional_context path (middle priority)

Step5a asks Gemini: *"what does this tag connect to or control?"*  
If present and tag is **not** in registry, that text becomes `TAG DESCRIPTION`.

---

## Operation 3 — Discipline Classifier

**Function:** `classify_discipline(canonical_tag, symbol_category, symbol_name)`

Sets `DISCIPLINE` column: `INSTRUMENTATION`, `MECHANICAL`, `PIPING`, or `ELECTRICAL`.

### Logic

1. Parse tag prefix from canonical tag (e.g. `V-BV-2246` → check `BV`, `V`, full prefix)
2. Look up in `_DISCIPLINE_MAP` — instrument codes (FIT, PT, TT, ZSC…), valve codes (BV, GV, NRV…), equipment (K, V, P…), piping service codes (ETH, GAS…)
3. **Registry override:** if step5c registry entry has `discipline`, use that
4. **Category fallback:** `instrument` → INSTRUMENTATION, `valve`/`equipment` → MECHANICAL, `piping` → PIPING
5. **Pipe size pattern:** tags starting with `12IN-` or `10"-` → PIPING
6. Default: MECHANICAL

### Examples

| TAG NUMBER | DISCIPLINE | Why |
|------------|------------|-----|
| `FIT-207` | INSTRUMENTATION | FIT in instrument map |
| `V-BV-2246` | MECHANICAL | BV in valve map |
| `10IN-ETH-V061-61440X` | PIPING | pipe size prefix pattern |
| `K-V-201` | MECHANICAL | K = compressor prefix |
| `KM-V-201` | ELECTRICAL | KM = motor |

---

## Operation 4 — Canonical ID Generator

**Function:** `make_canonical_id(project_id, canonical_tag, drawing_number)`

```python
SHA-256(f"{project_id}|{canonical_tag}|{drawing_number}")[:12].upper()
```

Example: `CDCI|V-BV-2246|4224-MGDV-6-50-2004` → `A3F8C21B9D04`

- Stable across re-runs (same inputs → same ID)
- Used as `_canonical_id` internal primary key
- **Not** written to the Excel deliverable (Annexure-4 has no such column)

---

## Operation 5 — DOC STATUS Mapper

**Function:** `map_doc_status(revision_code, issue_status)`

Maps title-block revision code to Annexure-4 `DOC STATUS` string.

| Priority | Source | Example |
|----------|--------|---------|
| 1 | `current_issue_status` from title_block_context.json | Exact string from revision table |
| 2 | `DOC_STATUS_MAP` lookup | Rev `C` → `RE-ISSUED FOR CONSTRUCTION-SCOPE CLOUD` |
| 3 | Fallback | `REVISION C` or `ACTIVE` |

Test drawing Rev C → `RE-ISSUED FOR CONSTRUCTION-SCOPE CLOUD` (or title-block issue status if present).

---

## Drawing metadata assembly

**Function:** `load_drawing_meta(drawing_context.json, title_block_context.json)`

Populates these Annexure-4 columns from the title block (same value on every row):

| Annexure-4 column | Source field |
|-------------------|--------------|
| `DOCUMENT NUMBER` | `drawing_number` e.g. `4224-MGDV-6-50-2004` |
| `SHEET NO` | `sheet_number` e.g. `001` |
| `REV` | `revision_code` e.g. `C` |
| `DRAWING REFERENCE` | `{drawing_number}-{sheet_number}-{revision_code}` |
| `DOCUMENT TITLE` | `drawing_title` |
| `DOC STATUS` | mapped from revision |
| `DATE` | `issue_date` from title block |

---

## REMARKS field — audit trail

`REMARKS` is built from pipeline state flags (pipe-separated):

| Condition | REMARKS fragment |
|-----------|------------------|
| `validation_status == FAIL` | `VALIDATION_FAIL: <reason>` |
| `validation_status == WARN` | `WARN: <reason>` |
| `sow_status == UNSPECIFIED` | `NO_SCOPE_DEFINITION_FOUND` |
| `sow_status == OUT_OF_SCOPE` | `OUT_OF_SCOPE` |
| Not in Annexure-4 register | `NOT_IN_REGISTER` |
| Tag text was transformed | `TAG_NORMALISED_FROM: <raw_tag>` |
| Extracted inside revision cloud | `WITHIN_REVISION_CLOUD` |

Example REMARKS:
```
WARN: Tag not found in asset registry: TE-211 | NOT_IN_REGISTER | WITHIN_REVISION_CLOUD
```

---

## Confidence signals passed to step8

Step7 computes intermediate scores stored as `_c_*` fields (not in Excel):

| Field | Source | Meaning |
|-------|--------|---------|
| `_c_det` | `vision_confidence` from step5a | Gemini detection confidence |
| `_c_ocr` | `ocr_confidence` from step5a | Tesseract OCR confidence |
| `_c_geo` | `association_confidence` from step5b | Geometry link quality |
| `_c_val` | min of step5c validation checks | PASS=1.0, WARN=0.5, FAIL=0.0 |
| `_c_reg` | 1.0 if in register, else 0.5 | Registry match bonus |

Step8 combines these into `C_final` for AUTO_ACCEPT / HUMAN_REVIEW routing.

---

## Output record structure

### 15 Annexure-4 columns (written to Excel by step8)

| # | Column | Set by |
|---|--------|--------|
| 1 | `S.NO` | Sequential SLNO at output time |
| 2 | `DISCIPLINE` | classify_discipline + registry override |
| 3 | `TAG NUMBER` | normalise_tag() |
| 4 | `TAG DESCRIPTION` | standardise_description() |
| 5 | `EQUIPMENT DESCRIPTION` | standardise_description() |
| 6 | `SIZE&RATING` | registry `size_rating` (empty if not in register) |
| 7 | `DOCUMENT NUMBER` | drawing_context |
| 8 | `SHEET NO` | drawing_context |
| 9 | `REV` | drawing_context |
| 10 | `DRAWING REFERENCE` | composed from dwg/sheet/rev |
| 11 | `DOCUMENT TITLE` | drawing_context |
| 12 | `DOC STATUS` | map_doc_status() |
| 13 | `DATE` | title_block issue_date |
| 14 | `DUPLICATE STATUS` | `NO` for PRIMARY (always in step7 input) |
| 15 | `REMARKS` | assembled audit flags |

### Internal metadata fields (step8 only, prefixed `_`)

`_candidate_id`, `_canonical_id`, `_raw_tag`, `_canonical_tag`, `_tag_transforms`, `_symbol_name`, `_symbol_category`, `_symbol_bbox`, `_validation_status`, `_sow_status`, `_in_registry`, `_c_det`, `_c_ocr`, `_c_geo`, `_c_val`, `_c_reg`, `_patch_id`, `_scope_type`

---

## Input filtering

Step7 only processes **PRIMARY** candidates:

```python
candidates = [c for c in candidates if c.get("duplicate_status", "PRIMARY") == "PRIMARY"]
```

DISCARDED duplicates from step5d are never normalised.

Input file resolution:
1. `--final output/step5_final_output.json` (preferred)
2. Falls back to `step5d_deduped.json` if final not found

---

## Post-normalisation checks

After all records are built, step7 runs:

### Canonical duplicate detection
Warns if two different pipeline candidates normalise to the **same canonical tag**:
```
WARNING Post-CEDM canonical duplicates: 5 groups
WARNING   V-BV-2242 → records [45, 54]
```
These are not merged — both rows pass through to step8. Human review may be needed.

### Transform statistics
Logged and saved in `step7_cedm_output.json`:
```json
"transform_counts": {
  "ocr_char_fix": 3,
  "inch_normalised": 4,
  "separators_normalised": 4,
  "illegal_chars_removed": 2
}
```

### Discipline breakdown
```json
"discipline_counts": {
  "INSTRUMENTATION": 117,
  "MECHANICAL": 91,
  "PIPING": 21
}
```

---

## End-to-end example

**Input candidate** (from `step5_final_output.json`):

```json
{
  "tag_text": "V—NRV-748",
  "symbol_name": "Non-Return Valve",
  "symbol_category": "valve",
  "validation_status": "PASS",
  "sow_status": "IN_SCOPE",
  "validation_details": [{
    "rule": "REGISTRY", "in_registry": true,
    "registry_entry": {
      "description": "NRV,KO DRUM INLET LINE,V-V-201",
      "equipment_description": "VALVE,CHECK,12IN",
      "discipline": "MECHANICAL",
      "size_rating": "12IN"
    }
  }]
}
```

**Output CEDM record** (abbreviated):

```json
{
  "TAG NUMBER":           "V-NRV-748",
  "TAG DESCRIPTION":      "NRV,KO DRUM INLET LINE,V-V-201",
  "EQUIPMENT DESCRIPTION":"VALVE,CHECK,12IN",
  "DISCIPLINE":           "MECHANICAL",
  "SIZE&RATING":          "12IN",
  "DOCUMENT NUMBER":      "4224-MGDV-6-50-2004",
  "SHEET NO":             "001",
  "REV":                  "C",
  "DOC STATUS":           "RE-ISSUED FOR CONSTRUCTION-SCOPE CLOUD",
  "REMARKS":              "TAG_NORMALISED_FROM: V—NRV-748",
  "_raw_tag":             "V—NRV-748",
  "_canonical_tag":       "V-NRV-748",
  "_tag_transforms":      ["ocr_char_fix: 'V—NRV-748' → 'V-NRV-748'"],
  "_in_registry":         true,
  "_c_reg":               1.0
}
```

---

## Command

```bash
python3 stages/step7_cedm_normalizer.py \
  --final output/step5_final_output.json \
  --context output/drawing_context.json \
  --out output/ \
  --project CDCI
```

**Console output:**
```
=== Step 7 Complete — CEDM Normalisation ===
  Records normalised : 198
    INSTRUMENTATION       117
    MECHANICAL             91
    PIPING                 21
  Tags normalised    : 8/198

  Output: output/step7_cedm_output.json
```

---

## What step7 does NOT do

| Not in scope | Handled by |
|--------------|------------|
| Detect new tags | step5a |
| Validate ISA format | step5c |
| Remove duplicates | step5d |
| Score confidence / route to Excel | step8 |
| Compare vs Annexure-4 | eval_coverage.py, compare_final_vs_annexure4.py |

---

## Files

| File | Role |
|------|------|
| `stages/step7_cedm_normalizer.py` | Normalisation engine |
| `output/step5_final_output.json` | Input (PRIMARY candidates) |
| `output/drawing_context.json` | Drawing metadata |
| `output/title_block_context.json` | Title block override (date, issue status) |
| `output/step7_cedm_output.json` | Output → step8 |
