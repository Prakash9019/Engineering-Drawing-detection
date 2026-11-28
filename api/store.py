"""Small SQLite job/event store for the recruiter demo."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class JobStore:
    def __init__(self, database: Path):
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    input_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                )
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, job_id: str, workspace: Path, input_name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = {"current_step": None, "steps": [], "evals": [], "artifacts": []}
        with self._connect() as db:
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, "queued", str(workspace), input_name, now, now, json.dumps(payload)),
            )
        self.event(job_id, "job.queued", input_name=input_name)

    def update(self, job_id: str, status: str | None = None, **fields: Any) -> None:
        with self._connect() as db:
            row = db.execute("SELECT payload, status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            payload = json.loads(row["payload"])
            payload.update(fields)
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                "UPDATE jobs SET status=?, payload=?, updated_at=? WHERE job_id=?",
                (status or row["status"], json.dumps(payload, default=str), now, job_id),
            )

    def event(self, job_id: str, event: str, **fields: Any) -> None:
        record = {"event": event, "timestamp": datetime.now(timezone.utc).isoformat(), **fields}
        with self._connect() as db:
            db.execute(
                "INSERT INTO job_events(job_id, created_at, payload) VALUES (?, ?, ?)",
                (job_id, record["timestamp"], json.dumps(record, default=str)),
            )

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            result["events"] = [
                json.loads(item["payload"])
                for item in db.execute(
                    "SELECT payload FROM job_events WHERE job_id=? ORDER BY id", (job_id,)
                ).fetchall()
            ]
            return result

    def list(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [job for row in rows if (job := self.get(row["job_id"])) is not None]
