# Context Management & Operational Lifecycle Guide
## AI Engineering Guidelines (`CONTEXT.md`)

---

## 1. Progressive Context Loading

When navigating this repository, minimize context window saturation by following a strict progressive discovery pattern:

1. **Hierarchy First**: Start with directory listings (`ls`, `glob`) to locate the relevant stage or module.
2. **Search Before Reading**: Use targeted regex searches (`grep_search`) to locate specific classes, function definitions, or CLI flags.
3. **Surgical Line-Range Reads**: Use targeted reads (`start_line`, `end_line`) to inspect 30–60 lines around the target symbol. Avoid reading entire multi-hundred line scripts into context.
4. **Data Isolation**: Never dump large generated JSON files (e.g. `drawing_context.json`, `step5a_candidates.json`, which often exceed 500 KB) into the conversation context. Use focused Python one-liners to query keys, counts, or small sample records.

```bash
# Example: Inspecting specific JSON keys without reading the full file into LLM context
python -c "import json; d = json.load(open('output/drawing_context.json')); print('Keys:', list(d.keys()))"
```

---

## 2. Token Optimization Guidelines

- **Quiet Command Flags**: Always pass quiet flags to CLI tools (`pytest -q`, `git --no-pager`, `pip list --format=freeze`).
- **Compact File Diffs**: Keep edits focused on immediate lines using replacement tools rather than rewriting entire files.
- **Selective Output Tailoring**: When reviewing logs, capture only the last 20–40 lines or pipe output through filters (`head`, `tail`, `grep`).
- **Do Not Duplicate Documentation**: Do not create fragmented markdown files for minor features. Maintain all architecture records strictly in `PROJECT_MEMORY.md`.

---

## 3. When and How to Delegate to Subagents

Subagents isolate deep exploration and repetitive batch tasks from the primary orchestrator's context window.

### Delegation Decision Matrix

| Task Type | Recommended Agent | Execution Strategy |
|---|---|---|
| Pipeline architecture, data contracts, model decisions | `architect` | Single-turn analysis returning architectural recommendations |
| Code writing, bug fixes, refactoring | `implementer` | Surgical implementation guided by approved plans |
| Security audits, secret detection, code review | `reviewer` | Static analysis of diffs before acceptance |
| OCR mismatch, cloud errors, graph disconnects | `pipeline-debugger` | Inspects intermediate debug images and JSON graphs |
| Regression testing, recall evaluation, acceptance checks | `verifier` | Runs pytest suites and Annexure-4 comparisons |

### Strategic Orchestration Rules
- Delegate repetitive tasks involving >3 files to subagents.
- Run subagents sequentially when modifying shared project state.
- Subagents must return compressed, high-signal summaries (e.g. root cause, changed files, test output) back to the orchestrator.

---

## 4. Session Handoff & Summarization

When a working session approaches token capacity or reaches a major milestone:

1. **State Snapshot**: Ensure all modified files pass `python -m pytest tests/test_core.py -v`.
2. **Commit Check**: Check `git status --short` to identify modified files. (Do NOT commit unless explicitly requested by the user).
3. **Memory Update**: If any architectural contract or stage behavior was changed, record it in `PROJECT_MEMORY.md`.
4. **Handoff Note**: Provide a concise handoff block:
   * **Goal Accomplished**: Summary of changes made.
   * **Current State**: Passing tests, active branch, modified files.
   * **Immediate Next Step**: The exact task the next session should execute.

---

## 5. Memory Update Rules

- **Durable Facts Only**: Only persist facts that remain true across multiple developer sessions:
  * Schema contract changes in `output/drawing_context.json`.
  * Addition or removal of active pipeline stages.
  * Verified benchmark numbers on ground truth drawings.
  * Key architectural constraints (e.g. two-track extraction).
- **Prohibited in Memory**:
  * Ephemeral debugging statements or single-run logs.
  * Personal machine paths or sensitive API keys.
  * Duplicate copies of code or source files.
- **Synchronization**: Keep `CLAUDE.md`, `AGENTS.md`, and `PROJECT_MEMORY.md` consistent whenever pipeline contracts change.

---

## 6. Model Selection & Escalation Strategy

| Task Category | Selected Model | Rationale |
|---|---|---|
| SAHI Drawing Tag Detection (Step 5a) | `gemini-3.1-pro-preview` | Superior reasoning on dense engineering symbols and complex vision |
| Cloud Localization (Step 2b) | `gemini-3.1-pro-preview` | Accurate bounding box and polygon contour identification |
| Title Block OCR (Step 2) | `gemini-2.5-flash` | Low latency, cost-effective for structured text fields |
| Notes & SOW Extraction (Step 3, 4) | `gemini-2.5-flash` | Fast OCR synthesis and symbol categorization |
| Table Schedule Extraction (Step 6) | `gemini-2.5-flash` | High accuracy on tabular key-value extraction |
| Code Architecture & Reasoning | Claude Sonnet 4.6 / OpenAI Codex | Deep code understanding, minimal diffs, rigorous adherence to conventions |

### Escalation Protocol
- If `gemini-2.5-flash` fails to parse a degraded or low-contrast title block/table after 2 attempts, escalate the stage call to `gemini-3.1-pro-preview`.
- If free-tier rate limits (15 RPM) are hit (HTTP 429), fall back to exponential backoff with jitter (`core/gemini_client.py`).

---

## 7. The 8-Step Engineering Lifecycle

Every code change must progress through these 8 discrete stages:

```
[1. Research] ──> [2. Plan] ──> [3. Review Plan] ──> [4. Implement]
                                                           │
[8. Update Memory] <── [7. Verify] <── [6. Review Code] <── [5. Test]
```

1. **Research**: Map the problem space. Inspect relevant modules, trace `output/drawing_context.json`, and reproduce failures with deterministic scripts.
2. **Plan**: Formulate an implementation plan with bounded scope, sequenced steps, and rollback criteria.
3. **Review Plan**: Validate backward compatibility and token/runtime efficiency before making edits.
4. **Implement**: Apply surgical, typed changes conforming to project conventions.
5. **Test**: Run fast offline unit tests (`python -m pytest tests/test_core.py -v`). Never skip testing.
6. **Review Code**: Run quality and secret hooks (`code_quality.py`, `secret_scanner.py`).
7. **Verify**: Verify against acceptance criteria and ground-truth recall benchmarks (`eval_coverage.py`).
8. **Update Memory**: Record permanent architectural adjustments in `PROJECT_MEMORY.md`.
