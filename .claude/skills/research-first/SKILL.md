---
name: research-first
description: Systematic research workflow to inspect existing code, reproduce failures, map dependencies, and compare trade-offs before writing code.
---

# Research-First Skill

## Intent
Enforce thorough codebase investigation and empirical validation before any code modifications occur. Never write code on untested assumptions.

## Workflow

### 1. Codebase Mapping & Dependency Analysis
- Locate relevant scripts in `stages/`, `core/`, or `step2c_cloud_editor/`.
- Trace data contracts: check which keys in `output/drawing_context.json` or intermediate artifacts are read/written.
- Identify dependencies and external APIs (Gemini, Tesseract, OpenCV).

### 2. Empirical State Inspection & Reproduction
- Check existing output artifacts in `output/` before modifying code.
- Reproduce bugs or unexpected outputs using exact reproduction commands:
  ```bash
  python -m pytest tests/test_core.py -v
  ```
- If investigating pipeline output, inspect the specific stage JSON or debug visual overlay.

### 3. Alternative Evaluation
- Compare at least two architectural options before choosing an approach:
  * Option A: Low-touch / surgical adjustment within existing stage logic.
  * Option B: Additive modular post-processor or utility function.
- Evaluate pros, cons, backward compatibility, and runtime impact.

### 4. Integration with Superpowers
- For broad design exploration, integrate with the installed Superpowers `brainstorming` skill.
- Stop and confirm before proceeding to implementation planning.
