"""SQLite experience ledger with migrations, idempotency, and serialized writes."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterator

from src.models import Attempt


SCHEMA_VERSION = 1


class LedgerConflictError(RuntimeError):
    """Raised when an idempotency key is reused for different attempt data."""


class Ledger:
    """A small append-oriented SQLite ledger safe for multiple writer threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _migrate(self) -> None:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = db.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
            version = int(row[0]) if row else 0
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"ledger schema {version} is newer than supported {SCHEMA_VERSION}")
            if version < 1:
                db.execute(
                    """CREATE TABLE attempts (
                        attempt_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        signal TEXT NOT NULL,
                        base_commit TEXT NOT NULL,
                        strategy_version TEXT NOT NULL,
                        status TEXT NOT NULL,
                        model_version TEXT NOT NULL,
                        prompt_version TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )"""
                )
                db.execute("CREATE INDEX attempts_signal_idx ON attempts(signal, base_commit, strategy_version)")
                db.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', '1')")
            db.execute("COMMIT")

    @staticmethod
    def _json(attempt: Attempt) -> str:
        return json.dumps(attempt.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def record_attempt(self, attempt: Attempt) -> Attempt:
        payload = self._json(attempt)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT attempt_id, payload_json FROM attempts WHERE idempotency_key = ?",
                (attempt.idempotency_key,),
            ).fetchone()
            if existing:
                if existing[1] != payload:
                    db.execute("ROLLBACK")
                    raise LedgerConflictError(
                        f"idempotency key already belongs to attempt {existing[0]} with different data"
                    )
                db.execute("COMMIT")
                return Attempt.model_validate_json(existing[1])
            db.execute(
                """INSERT INTO attempts (
                    attempt_id, idempotency_key, signal, base_commit, strategy_version,
                    status, model_version, prompt_version, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    attempt.attempt_id,
                    attempt.idempotency_key,
                    attempt.signal,
                    attempt.base_commit,
                    attempt.strategy_version,
                    attempt.status.value,
                    attempt.model_version,
                    attempt.prompt_version,
                    payload,
                    attempt.created_at.isoformat(),
                    attempt.updated_at.isoformat(),
                ),
            )
            db.execute("COMMIT")
        return attempt

    def get_attempt(self, attempt_id: str) -> Attempt | None:
        with self._connect() as db:
            row = db.execute("SELECT payload_json FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        return Attempt.model_validate_json(row[0]) if row else None

    def find_by_idempotency_key(self, key: str) -> Attempt | None:
        with self._connect() as db:
            row = db.execute("SELECT payload_json FROM attempts WHERE idempotency_key = ?", (key,)).fetchone()
        return Attempt.model_validate_json(row[0]) if row else None

    def count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])

    def iter_attempts(self) -> Iterator[Attempt]:
        with self._connect() as db:
            rows = db.execute("SELECT payload_json FROM attempts ORDER BY created_at, attempt_id").fetchall()
        yield from (Attempt.model_validate_json(row[0]) for row in rows)

