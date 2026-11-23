---
name: testing-and-verification
description: Comprehensive testing protocol covering fast offline unit tests, acceptance criteria, Annexure-4 recall benchmarks, and deliverable integrity.
---

# Testing and Verification Skill

## Intent
Guarantee behavioral correctness and zero regressions across all pipeline components, using tiered verification that avoids costly end-to-end API runs for routine edits.

## Verification Tiers

### Tier 1: Fast Offline Unit Tests (<2s)
- Run automatically via hooks or manually:
  ```bash
  python -m pytest tests/test_core.py -v
  ```
- Validates ISA-5.1 decoding, confidence scoring formula, geometric IoU/polygon functions, and JSON parsing.

### Tier 2: Static Quality & Secret Verification (<1s)
- Verify Python syntax across all modified files:
  ```bash
  python .claude/hooks/code_quality.py
  ```
- Verify zero committed secrets:
  ```bash
  python .claude/hooks/secret_scanner.py
  ```

### Tier 3: Benchmark & Acceptance Verification (Offline / Deliverables)
- Check recall against ground truth:
  ```bash
  python stages/eval_coverage.py --candidates output/step5a_candidates.json --register ANNEXURE-4_4224-MGDV-6-50-2004-001-C.xlsx --image input_drawing.jpg --out output/
  ```
- Compare client deliverable:
  ```bash
  python stages/compare_final_vs_annexure4.py
  ```
- Acceptance Target on Rev C test sheet:
  * Unique candidates detected at step5a: $\ge 42/46$.
  * Unique tags in `final_tags.xlsx` SUMMARY sheet: $\ge 41/46$.
  * AUTO_ACCEPT rows: $\ge 46$ (includes valid multi-instance detections).
  * AUTO_REJECT rows: 0.

### Tier 4: Superpowers Integration
- Adhere to Superpowers `test-driven-development` (reproduce failure first with a test before fixing).
- Adhere to Superpowers `verification-before-completion` (always run verification command before marking a task complete).
