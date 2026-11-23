---
name: project-memory
description: Workflow for maintaining and updating PROJECT_MEMORY.md to preserve architectural decisions, stage state, and known constraints.
---

# Project Memory Skill

## Intent
Maintain a single, authoritative, high-density project memory document (`PROJECT_MEMORY.md`) to retain architectural decisions across AI sessions without documentation bloat.

## Principles of Durable Memory

### 1. What Belongs in PROJECT_MEMORY.md
- **Core Pipeline Contracts**: Changes to stage inputs, outputs, or schemas (e.g. `step5b2_hierarchy_full.json`).
- **Architectural Invariants**: The two-track extraction decision (Full for hierarchy vs Cloud for deliverable).
- **Benchmark Baselines**: Verified metrics on ground-truth sheets (e.g. 42/46 unique tags detected).
- **Hard Constraints & Gates**: Why direct cloud extraction cannot be deprecated without 50-100 drawing validation.
- **Active vs Deprecated Scripts**: Explicit list of active scripts to prevent running obsolete tooling.

### 2. What Does NOT Belong
- Temporary debugging steps or ephemeral trial outputs.
- Raw stack traces or temporary variable values.
- Duplicated file contents that already exist in code.

### 3. Update Procedure
1. When a task modifies an architectural contract or reaches a validated milestone, locate the relevant section in `PROJECT_MEMORY.md`.
2. Apply surgical updates to keep the document concise, structured, and factual.
3. Keep `CLAUDE.md` and `AGENTS.md` synchronized with any high-level changes.
