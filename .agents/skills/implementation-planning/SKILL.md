---
name: implementation-planning
description: Structured implementation planning workflow breaking changes into small, verifiable, testable steps with explicit rollback gates.
---

# Implementation Planning Skill

## Intent
Transform validated research into an actionable, phased implementation roadmap. Guarantee that each modification has a defined acceptance test and verification check.

## Planning Protocol

### 1. Scope Definition
- **Explicit Goal**: Define the exact behavioral or structural objective.
- **In-Scope Files**: Explicitly list files to be created or modified.
- **Out-of-Scope Safeguards**: Explicitly list files/behaviors that must remain untouched.

### 2. Phased Task Breakdown
Decompose work into incremental phases:
1. **Foundation & Data Contracts**: Set up schemas, imports, and function signatures.
2. **Core Implementation**: Surgical modifications to algorithm or stage logic.
3. **Automated Unit Testing**: Fast offline tests verifying the modification.
4. **Integration Validation**: Verifying downstream compatibility (e.g. `drawing_context.json`).

### 3. Verification Gates & Rollback Criteria
- For each phase, specify:
  * Verification Command (e.g. `python -m pytest tests/test_core.py -v`).
  * Acceptance Criteria (exact expected output, score threshold, or schema match).
  * Rollback Procedure (how to revert cleanly if verification fails).

### 4. Integration with Superpowers
- Leverage the installed Superpowers `writing-plans` and `executing-plans` skills to track tasks.
- Keep plan documentation concise; update `PROJECT_MEMORY.md` if fundamental architecture changes.
