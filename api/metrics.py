"""Prometheus text metrics from local job state and workflow events."""
from __future__ import annotations
from collections import Counter
from pathlib import Path
import json
from typing import Any
def render_metrics(jobs: list[dict[str,Any]]) -> str:
    statuses=Counter(j.get("status","unknown") for j in jobs)
    lines=["# HELP cdci_jobs_total Jobs by current status.","# TYPE cdci_jobs_total gauge"]
    for status,count in sorted(statuses.items()):
        safe=status.replace('"','')
        lines.append(f'cdci_jobs_total{{status="{safe}"}} {count}')
    durations=[]
    for job in jobs:
        path=Path(job["workspace"])/"events.jsonl"
        if not path.exists(): continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                obj=json.loads(line)
                if obj.get("event")=="stage.finished": durations.append(float(obj.get("duration_seconds",0)))
        except (OSError,ValueError,TypeError): pass
    lines += ["# HELP cdci_stage_duration_seconds_count Count of stage durations.",
              "# TYPE cdci_stage_duration_seconds_count gauge",
              f"cdci_stage_duration_seconds_count {len(durations)}",
              "# HELP cdci_stage_duration_seconds_sum Sum of stage durations.",
              "# TYPE cdci_stage_duration_seconds_sum gauge",
              f"cdci_stage_duration_seconds_sum {sum(durations):.3f}"]
    return "\n".join(lines)+"\n"
