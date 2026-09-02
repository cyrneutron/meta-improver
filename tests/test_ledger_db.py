from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3

import pytest

from src.models import Attempt, AttemptStage, AttemptStatus
from src.ha_diagnosis import HaDiagnosis, HaDiagnosisFinding, HaDiagnosisStatus
from src.storage import Ledger, LedgerConflictError
from src.storage.ledger_db import SCHEMA_VERSION


def make_attempt(attempt_id: str = "attempt-1", key: str = "key-1") -> Attempt:
    content = "failure"
    return Attempt(
        attempt_id=attempt_id,
        idempotency_key=key,
        signal="test failure",
        base_commit="b" * 40,
        strategy_version="strategy-v1",
        input_snapshot={"source": "manual", "content": content, "content_sha256": hashlib.sha256(content.encode()).hexdigest()},
        model_version="model-v1",
        prompt_version="prompt-v1",
    )


def test_migration_is_repeatable_and_record_is_idempotent(tmp_path) -> None:
    path = tmp_path / "history.db"
    ledger = Ledger(path)
    Ledger(path)
    attempt = make_attempt()
    assert ledger.record_attempt(attempt) == attempt
    assert ledger.record_attempt(attempt) == attempt
    assert ledger.count() == 1
    assert ledger.get_attempt(attempt.attempt_id) == attempt


def test_conflicting_idempotency_key_is_rejected(tmp_path) -> None:
    ledger = Ledger(tmp_path / "history.db")
    ledger.record_attempt(make_attempt())
    with pytest.raises(LedgerConflictError):
        ledger.record_attempt(make_attempt(attempt_id="attempt-2", key="key-1"))


def test_concurrent_same_key_has_one_row(tmp_path) -> None:
    ledger_path = tmp_path / "history.db"
    attempts = [make_attempt(attempt_id=f"attempt-{index}") for index in range(8)]

    def write(attempt: Attempt) -> str:
        try:
            return ledger.record_attempt(attempt).attempt_id
        except LedgerConflictError:
            return "conflict"

    ledger = Ledger(ledger_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, attempts))
    assert sum(result != "conflict" for result in results) == 1
    assert ledger.count() == 1


def test_diagnosis_table_migrates_from_v1_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "history.db"
    ledger = Ledger(path)
    diagnosis = HaDiagnosis(
        diagnosis_id="diag-1",
        task_id="task-1",
        squad_id="squad-1",
        run_id="run-1",
        status=HaDiagnosisStatus.CONVERGED,
        findings=[HaDiagnosisFinding(path="README.md", observation="finding")],
        provider_version="0.1.0",
        provider_build_id="build",
        poll_attempts=1,
        receipt_digest="sha256:" + "a" * 64,
        observed_at="2026-01-01T00:00:00Z",
    )
    assert ledger.record_diagnosis(diagnosis) == diagnosis
    assert ledger.get_diagnosis("diag-1") == diagnosis


def test_schema_v2_attempt_migrates_with_initial_event(tmp_path) -> None:
    path = tmp_path / "history.db"
    attempt = make_attempt()
    legacy = attempt.model_dump(mode="json")
    for field in (
        "stage",
        "baseline_hash",
        "diagnosis_hash",
        "patch_hash",
        "acceptance_receipt_hash",
        "pipeline_receipt_hash",
    ):
        legacy.pop(field)
    payload = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO schema_meta(key, value) VALUES ('version', '2')")
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
        db.execute(
            """CREATE TABLE diagnoses (
                diagnosis_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                record_json TEXT NOT NULL,
                observed_at TEXT NOT NULL
            )"""
        )
        db.execute("CREATE INDEX diagnoses_task_idx ON diagnoses(task_id, observed_at)")
        db.execute(
            """INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

    ledger = Ledger(path)

    assert ledger.get_attempt(attempt.attempt_id) == attempt
    assert ledger.record_attempt(attempt) == attempt
    assert [event.stage for event in ledger.iter_attempt_events(attempt.attempt_id)] == [
        AttemptStage.CAPTURED
    ]
    with sqlite3.connect(path) as db:
        version = db.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        revision = db.execute("SELECT revision FROM attempts").fetchone()
    assert version == (str(SCHEMA_VERSION),)
    assert revision == (1,)


def test_attempt_transitions_are_append_only_idempotent_and_fail_closed(tmp_path) -> None:
    ledger = Ledger(tmp_path / "history.db")
    initial = ledger.record_attempt(make_attempt())
    baseline = Attempt.model_validate(
        {
            **initial.model_dump(),
            "status": AttemptStatus.RUNNING,
            "stage": AttemptStage.BASELINE_EVALUATED,
            "baseline_hash": "sha256:" + "a" * 64,
        }
    )
    assert ledger.transition_attempt(baseline) == baseline
    assert ledger.transition_attempt(baseline) == baseline

    conflicting = Attempt.model_validate(
        {**baseline.model_dump(), "baseline_hash": "sha256:" + "b" * 64}
    )
    with pytest.raises(LedgerConflictError, match="different evidence"):
        ledger.transition_attempt(conflicting)

    regressed = Attempt.model_validate(
        {
            **baseline.model_dump(),
            "status": AttemptStatus.PROPOSED,
            "stage": AttemptStage.CAPTURED,
        }
    )
    with pytest.raises(LedgerConflictError, match="status transition"):
        ledger.transition_attempt(regressed)

    assert [event.stage for event in ledger.iter_attempt_events(initial.attempt_id)] == [
        AttemptStage.CAPTURED,
        AttemptStage.BASELINE_EVALUATED,
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "status": AttemptStatus.FAILED,
            "failure_reason": "failed before recording",
        },
        {
            "status": AttemptStatus.SUCCEEDED,
            "stage": AttemptStage.COMPLETED,
        },
        {
            "stage": AttemptStage.BASELINE_EVALUATED,
            "baseline_hash": "sha256:" + "a" * 64,
        },
        {"baseline_hash": "sha256:" + "a" * 64},
        {"failure_reason": "not valid on an initial attempt"},
    ],
)
def test_new_attempt_rejects_terminal_stage_and_later_evidence(tmp_path, overrides) -> None:
    ledger = Ledger(tmp_path / "history.db")
    invalid = Attempt.model_validate({**make_attempt().model_dump(), **overrides})

    with pytest.raises(LedgerConflictError, match="new attempt"):
        ledger.record_attempt(invalid)

    assert ledger.count() == 0


def test_transition_requires_cumulative_stage_evidence_and_clean_success(tmp_path) -> None:
    ledger = Ledger(tmp_path / "history.db")
    current = ledger.record_attempt(make_attempt())
    updates = (
        (AttemptStage.BASELINE_EVALUATED, {"baseline_hash": "sha256:" + "a" * 64}),
        (AttemptStage.DIAGNOSED, {"diagnosis_hash": "sha256:" + "b" * 64}),
        (AttemptStage.PATCH_VALIDATED, {"patch_hash": "sha256:" + "c" * 64}),
        (
            AttemptStage.ACCEPTANCE_EVALUATED,
            {"acceptance_receipt_hash": "sha256:" + "d" * 64},
        ),
    )
    for stage, evidence in updates:
        required_field = next(iter(evidence))
        missing_evidence = Attempt.model_validate(
            {
                **current.model_dump(),
                "status": AttemptStatus.RUNNING,
                "stage": stage,
            }
        )
        with pytest.raises(LedgerConflictError, match=f"requires {required_field}"):
            ledger.transition_attempt(missing_evidence)
        current = ledger.transition_attempt(
            Attempt.model_validate(
                {
                    **current.model_dump(),
                    **evidence,
                    "status": AttemptStatus.RUNNING,
                    "stage": stage,
                }
            )
        )

    missing_receipt = Attempt.model_validate(
        {
            **current.model_dump(),
            "status": AttemptStatus.SUCCEEDED,
            "stage": AttemptStage.COMPLETED,
        }
    )
    with pytest.raises(LedgerConflictError, match="requires pipeline_receipt_hash"):
        ledger.transition_attempt(missing_receipt)

    success_with_failure = Attempt.model_validate(
        {
            **missing_receipt.model_dump(),
            "pipeline_receipt_hash": "sha256:" + "e" * 64,
            "failure_reason": "success cannot retain failure",
        }
    )
    with pytest.raises(LedgerConflictError, match="cannot contain a failure reason"):
        ledger.transition_attempt(success_with_failure)
