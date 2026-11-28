"""Structured, dependency-light workflow telemetry.

Every event is JSON so it can be shipped to OpenTelemetry/Langfuse later.
Current legacy stage scripts do not expose token usage consistently; do not
invent token/cost values. This module records wall time, exit status, retries,
artifact counts, and stage-level metadata.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_LOG = logging.getLogger("cdci.workflow")
if not _LOG.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    _LOG.addHandler(handler)
    _LOG.setLevel(logging.INFO)


def emit_event(event: str, **fields: Any) -> dict[str, Any]:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "cdci-orchestrator",
        "event": event,
        **fields,
    }
    _LOG.info(json.dumps(record, default=str, separators=(",", ":")))
    return record


def timed(name: str, fn: Callable[[], Any], **fields: Any) -> tuple[Any, float]:
    started = time.perf_counter()
    emit_event("stage.started", stage=name, **fields)
    try:
        result = fn()
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 3)
        emit_event("stage.failed", stage=name, duration_seconds=elapsed,
                   error_type=type(exc).__name__, error=str(exc), **fields)
        raise
    elapsed = round(time.perf_counter() - started, 3)
    emit_event("stage.completed", stage=name, duration_seconds=elapsed, **fields)
    return result, elapsed


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, default=str) + os.linesep)
