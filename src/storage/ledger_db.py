"""SQLite experience ledger with migrations, idempotency, and serialized writes."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterator

from src.ha_diagnosis import HaDiagnosis, rehydrate_diagnosis
from src.models import Attempt, AttemptStage, AttemptStatus


SCHEMA_VERSION = 3

_STAGE_ORDER = {stage: index for index, stage in enumerate(AttemptStage)}
_REQUIRED_EVIDENCE = {
    AttemptStage.CAPTURED: (),
    AttemptStage.BASELINE_EVALUATED: ("baseline_hash",),
    AttemptStage.DIAGNOSED: ("baseline_hash", "diagnosis_hash"),
    AttemptStage.PATCH_VALIDATED: ("baseline_hash", "diagnosis_hash", "patch_hash"),
    AttemptStage.ACCEPTANCE_EVALUATED: (
        "baseline_hash",
        "diagnosis_hash",
        "patch_hash",
        "acceptance_receipt_hash",
    ),
    AttemptStage.COMPLETED: (
        "baseline_hash",
        "diagnosis_hash",
        "patch_hash",
        "acceptance_receipt_hash",
    ),
}
_STAGE_EVIDENCE = (
    "baseline_hash",
    "diagnosis_hash",
    "patch_hash",
    "acceptance_receipt_hash",
    "pipeline_receipt_hash",
)


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
                version = 1
            if version < 2:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS diagnoses (
                        diagnosis_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        record_json TEXT NOT NULL,
                        observed_at TEXT NOT NULL
                    )"""
                )
                db.execute("CREATE INDEX IF NOT EXISTS diagnoses_task_idx ON diagnoses(task_id, observed_at)")
                db.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', '2')")
                version = 2
            if version < 3:
                db.execute("ALTER TABLE attempts ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
                db.execute(
                    """CREATE TABLE attempt_events (
                        attempt_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        PRIMARY KEY (attempt_id, revision),
                        FOREIGN KEY (attempt_id) REFERENCES attempts(attempt_id)
                    )"""
                )
                db.execute(
                    """INSERT INTO attempt_events (
                        attempt_id, revision, status, stage, payload_json, recorded_at
                    )
                    SELECT attempt_id, 1, status, 'captured', payload_json, updated_at
                    FROM attempts"""
                )
                db.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', '3')")
            db.execute("COMMIT")

    @staticmethod
    def _json(attempt: Attempt) -> str:
        return json.dumps(attempt.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _validate_initial(attempt: Attempt) -> None:
        if attempt.status is not AttemptStatus.PROPOSED or attempt.stage is not AttemptStage.CAPTURED:
            raise LedgerConflictError("new attempt must start proposed at captured stage")
        if (
            attempt.proposal is not None
            or attempt.failure_reason is not None
            or attempt.test_evidence
            or any(getattr(attempt, field) is not None for field in _STAGE_EVIDENCE)
        ):
            raise LedgerConflictError("new attempt cannot contain later-stage evidence")

    @staticmethod
    def _is_initial_replay(existing: Attempt, candidate: Attempt) -> bool:
        try:
            Ledger._validate_initial(candidate)
        except LedgerConflictError:
            return False
        identity = (
            "attempt_id",
            "idempotency_key",
            "signal",
            "base_commit",
            "strategy_version",
            "input_snapshot",
            "model_version",
            "prompt_version",
            "created_at",
        )
        return all(
            getattr(existing, field) == getattr(candidate, field)
            for field in identity
        )

    @staticmethod
    def _validate_stage_evidence(attempt: Attempt) -> None:
        for field in _REQUIRED_EVIDENCE[attempt.stage]:
            if getattr(attempt, field) is None:
                raise LedgerConflictError(
                    f"attempt stage {attempt.stage.value} requires {field}"
                )
        allowed = set(_REQUIRED_EVIDENCE[attempt.stage])
        for field in _STAGE_EVIDENCE:
            value = getattr(attempt, field)
            if field == "pipeline_receipt_hash":
                if attempt.status is AttemptStatus.SUCCEEDED and value is None:
                    raise LedgerConflictError("succeeded attempt requires pipeline_receipt_hash")
                if attempt.status is not AttemptStatus.SUCCEEDED and value is not None:
                    raise LedgerConflictError("only succeeded attempt may contain pipeline_receipt_hash")
            elif field not in allowed and value is not None:
                raise LedgerConflictError(
                    f"attempt stage {attempt.stage.value} cannot contain {field}"
                )
        if attempt.status in {AttemptStatus.FAILED, AttemptStatus.REJECTED}:
            if not attempt.failure_reason:
                raise LedgerConflictError("unsuccessful attempt transition requires a failure reason")
        elif attempt.failure_reason is not None:
            raise LedgerConflictError("successful or running attempt cannot contain a failure reason")

    def record_attempt(self, attempt: Attempt) -> Attempt:
        payload = self._json(attempt)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT attempt_id, payload_json FROM attempts WHERE idempotency_key = ?",
                (attempt.idempotency_key,),
            ).fetchone()
            if existing:
                existing_attempt = Attempt.model_validate_json(existing[1])
                if existing_attempt != attempt and not self._is_initial_replay(
                    existing_attempt, attempt
                ):
                    db.execute("ROLLBACK")
                    raise LedgerConflictError(
                        f"idempotency key already belongs to attempt {existing[0]} with different data"
                    )
                db.execute("COMMIT")
                return existing_attempt
            self._validate_initial(attempt)
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
            db.execute(
                """INSERT INTO attempt_events (
                    attempt_id, revision, status, stage, payload_json, recorded_at
                ) VALUES (?, 1, ?, ?, ?, ?)""",
                (
                    attempt.attempt_id,
                    attempt.status.value,
                    attempt.stage.value,
                    payload,
                    attempt.updated_at.isoformat(),
                ),
            )
            db.execute("COMMIT")
        return attempt

    @staticmethod
    def _validate_transition(current: Attempt, updated: Attempt) -> None:
        immutable = (
            "attempt_id",
            "idempotency_key",
            "signal",
            "base_commit",
            "strategy_version",
            "input_snapshot",
            "model_version",
            "prompt_version",
            "created_at",
        )
        if any(getattr(current, field) != getattr(updated, field) for field in immutable):
            raise LedgerConflictError("attempt transition changed immutable identity")
        if current.status in {AttemptStatus.SUCCEEDED, AttemptStatus.FAILED, AttemptStatus.REJECTED}:
            raise LedgerConflictError("terminal attempt cannot transition")
        allowed_statuses = {
            AttemptStatus.PROPOSED: {
                AttemptStatus.RUNNING,
                AttemptStatus.FAILED,
                AttemptStatus.REJECTED,
            },
            AttemptStatus.RUNNING: {
                AttemptStatus.RUNNING,
                AttemptStatus.SUCCEEDED,
                AttemptStatus.FAILED,
                AttemptStatus.REJECTED,
            },
        }
        if updated.status not in allowed_statuses[current.status]:
            raise LedgerConflictError(
                f"illegal attempt status transition: {current.status.value} -> {updated.status.value}"
            )
        current_stage = _STAGE_ORDER[current.stage]
        updated_stage = _STAGE_ORDER[updated.stage]
        if updated_stage < current_stage or updated_stage > current_stage + 1:
            raise LedgerConflictError(
                f"illegal attempt stage transition: {current.stage.value} -> {updated.stage.value}"
            )
        if updated_stage == current_stage and updated.status is current.status:
            raise LedgerConflictError("attempt stage already has different evidence")
        if updated.status is AttemptStatus.RUNNING and updated_stage != current_stage + 1:
            raise LedgerConflictError("running attempt must advance exactly one stage")
        if updated.status is AttemptStatus.SUCCEEDED and updated.stage is not AttemptStage.COMPLETED:
            raise LedgerConflictError("succeeded attempt must be completed")
        if updated.stage is AttemptStage.COMPLETED and updated.status is not AttemptStatus.SUCCEEDED:
            raise LedgerConflictError("completed attempt must be succeeded")
        Ledger._validate_stage_evidence(updated)

    @staticmethod
    def _is_semantic_replay(current: Attempt, updated: Attempt) -> bool:
        current_payload = current.model_dump(exclude={"updated_at"})
        updated_payload = updated.model_dump(exclude={"updated_at"})
        if current_payload == updated_payload:
            return True
        if updated.status is not AttemptStatus.RUNNING:
            return False
        if _STAGE_ORDER[updated.stage] > _STAGE_ORDER[current.stage]:
            return False
        identity = (
            "attempt_id",
            "idempotency_key",
            "signal",
            "base_commit",
            "strategy_version",
            "input_snapshot",
            "model_version",
            "prompt_version",
            "created_at",
            "proposal",
        )
        if any(getattr(current, field) != getattr(updated, field) for field in identity):
            return False
        evidence_fields = (*_REQUIRED_EVIDENCE[updated.stage], "test_evidence")
        return all(
            getattr(current, field) == getattr(updated, field)
            for field in evidence_fields
        )

    def transition_attempt(self, updated: Attempt) -> Attempt:
        """Atomically append one legal attempt revision and update its latest snapshot."""

        payload = self._json(updated)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload_json, revision FROM attempts WHERE attempt_id = ?",
                (updated.attempt_id,),
            ).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise LedgerConflictError(f"attempt does not exist: {updated.attempt_id}")
            current = Attempt.model_validate_json(row[0])
            if self._is_semantic_replay(current, updated):
                db.execute("COMMIT")
                return current
            self._validate_transition(current, updated)
            revision = int(row[1]) + 1
            db.execute(
                """UPDATE attempts SET
                    status = ?, payload_json = ?, updated_at = ?, revision = ?
                WHERE attempt_id = ?""",
                (
                    updated.status.value,
                    payload,
                    updated.updated_at.isoformat(),
                    revision,
                    updated.attempt_id,
                ),
            )
            db.execute(
                """INSERT INTO attempt_events (
                    attempt_id, revision, status, stage, payload_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    updated.attempt_id,
                    revision,
                    updated.status.value,
                    updated.stage.value,
                    payload,
                    updated.updated_at.isoformat(),
                ),
            )
            db.execute("COMMIT")
        return updated

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

    def iter_attempt_events(self, attempt_id: str) -> Iterator[Attempt]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload_json FROM attempt_events WHERE attempt_id = ? ORDER BY revision",
                (attempt_id,),
            ).fetchall()
        yield from (Attempt.model_validate_json(row[0]) for row in rows)

    def record_diagnosis(self, diagnosis: HaDiagnosis) -> HaDiagnosis:
        """Persist one hash-bound diagnosis idempotently by diagnosis_id."""
        value = rehydrate_diagnosis(diagnosis)
        payload = json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT record_json FROM diagnoses WHERE diagnosis_id = ?", (value.diagnosis_id,)
            ).fetchone()
            if existing:
                if existing[0] != payload:
                    db.execute("ROLLBACK")
                    raise LedgerConflictError(
                        f"diagnosis id already belongs to {value.diagnosis_id} with different data"
                    )
                db.execute("COMMIT")
                return HaDiagnosis.model_validate_json(existing[0])
            db.execute(
                "INSERT INTO diagnoses (diagnosis_id, task_id, run_id, status, record_json, observed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (value.diagnosis_id, value.task_id, value.run_id, value.status.value, payload, value.observed_at.isoformat()),
            )
            db.execute("COMMIT")
        return value

    def get_diagnosis(self, diagnosis_id: str) -> HaDiagnosis | None:
        with self._connect() as db:
            row = db.execute("SELECT record_json FROM diagnoses WHERE diagnosis_id = ?", (diagnosis_id,)).fetchone()
        return HaDiagnosis.model_validate_json(row[0]) if row else None

    def iter_diagnoses(self, task_id: str | None = None) -> Iterator[HaDiagnosis]:
        with self._connect() as db:
            if task_id is None:
                rows = db.execute("SELECT record_json FROM diagnoses ORDER BY observed_at, diagnosis_id").fetchall()
            else:
                rows = db.execute(
                    "SELECT record_json FROM diagnoses WHERE task_id = ? ORDER BY observed_at, diagnosis_id", (task_id,)
                ).fetchall()
        yield from (HaDiagnosis.model_validate_json(row[0]) for row in rows)
