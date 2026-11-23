---
name: pipeline-debugging
description: Structured troubleshooting methodology for P&ID extraction stages, vision overlays, graph connectivity, and confidence scores.
---

# Pipeline Debugging Skill

## Intent
Systematically isolate, reproduce, and resolve anomalies across the multi-stage P&ID extraction pipeline using empirical artifacts and deterministic checks.

## Diagnostic Protocol

### 1. Identify the Anomaly Boundary
Map the failure to a specific pipeline stage:
- **Cloud Mask Issues**: Step 2b / 2c. Check `output/outer_clouds_v2.json`, `overlay_v2.jpg`. Verify if a cloud exceeds the 85% area threshold.
- **Candidate Detection Misses**: Step 5a. Check SAHI patch overlap (40%) and Gemini prompt logs. Compare against `output/step5a_eval_annotated.jpg`.
- **Association / Pipe Disconnects**: Step 5b. Inspect `output/step5b_pipe_debug.jpg`. Check morphological kernel size and line detection threshold.
- **Hierarchy / Stale Join**: Step 5b2 / Step 7. Check if candidate IDs match. If <50% match, Step 5b2 was run on out-of-sync candidate sets.
- **Deliverable Discrepancies**: Step 8 / Step 9. Check confidence score weights and routing thresholds in `settings.py`.

### 2. Isolate Using Offline Diagnostics
- Use visual debug flags instead of re-running the entire pipeline:
  ```bash
  # Check hierarchy graph visually
  python stages/visualize_hierarchy.py --hierarchy output/step5b2_hierarchy_full.json --image input_drawing.jpg --out output/hierarchy_verification.jpg
  ```
- Run isolated unit checks on geometry and decoding:
  ```bash
  python -m pytest tests/test_core.py -v
  ```

### 3. Leverage Superpowers
- Activate the Superpowers `systematic-debugging` skill to trace root causes from symptoms to mechanics without random trial-and-error edits.
