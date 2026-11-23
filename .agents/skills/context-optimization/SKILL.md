---
name: context-optimization
description: Techniques to minimize token consumption, use progressive context loading, delegate to subagents, and keep sessions lean.
---

# Context Optimization Skill

## Intent
Maximize context window efficiency, reduce latency, and prevent session bloat when working in large engineering repositories.

## Best Practices

### 1. Progressive Context Loading
- Never read whole multi-thousand line files into memory unprompted.
- Use targeted symbol searches (`grep_search`) first to identify relevant functions or definitions.
- Use line-range reads (`start_line`, `end_line`) to inspect only the required blocks.

### 2. Output and Data Volume Control
- Filter command outputs using quiet flags (`git --no-pager`, `pytest -q`).
- Do not dump large JSON files (e.g. `drawing_context.json`, `step5a_candidates.json` which can exceed 500 KB) into the prompt context.
- Inspect JSON schemas with python one-liners:
  ```bash
  python -c "import json; d=json.load(open('output/drawing_context.json')); print(list(d.keys()))"
  ```

### 3. Strategic Subagent Delegation
- Delegate repetitive multi-file tasks or noisy investigative searches to specialized subagents.
- Let subagents perform trial-and-error operations and return a concise summary to the orchestrator.
- Keep the main orchestrator conversation focused on decisions and user interaction.

### 4. Session Handoff & Memory Consolidation
- When context reaches high capacity (>60% token budget), summarize current state into `PROJECT_MEMORY.md` or a milestone note.
- Start a fresh session with only `CLAUDE.md`, `PROJECT_MEMORY.md`, and active goals.
