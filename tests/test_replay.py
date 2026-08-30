import hashlib
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.ingestion import (
    CIRunSignal,
    IssueSignal,
    LocalLogSignal,
    replay_fixture_signals,
)
from src.storage import Ledger


OBSERVED = datetime(2026, 8, 30, tzinfo=timezone.utc)
BASE_COMMIT = "a" * 40


def _fixtures() -> list[object]:
    return [
        CIRunSignal(
            run_id="run-1",
            workflow="ci",
            status="completed",
            conclusion="failure",
            commit_sha=BASE_COMMIT,
            output="pytest failed",
            observed_at=OBSERVED,
        ),
        IssueSignal(
            issue_id="issue-1",
            title="Failure",
            body="Something broke",
            observed_at=OBSERVED,
        ),
        LocalLogSignal(
            log_id="log-1",
            path="logs/app.log",
            level="error",
            content="trace",
            observed_at=OBSERVED,
        ),
    ]


def test_replay_normalizes_ci_issue_and_local_log_fixtures(tmp_path) -> None:
    attempts = replay_fixture_signals(
        _fixtures(),
        Ledger(tmp_path / "history.db"),
        base_commit=BASE_COMMIT,
    )

    assert len(attempts) == 3
    assert [attempt.input_snapshot.source for attempt in attempts] == ["ci", "issue", "local_log"]
    assert [attempt.status.value for attempt in attempts] == ["failed", "proposed", "proposed"]
    assert attempts[0].failure_reason == "CI conclusion was not success"


def test_replay_accepts_source_tagged_mappings(tmp_path) -> None:
    values = [
        {
            "source": "issue",
            "issue_id": "issue-map",
            "title": "Mapped issue",
            "body": "Details",
            "observed_at": OBSERVED,
        },
        {
            "kind": "local_log",
            "log_id": "log-map",
            "path": "logs/map.log",
            "level": "warning",
            "content": "warning",
            "observed_at": OBSERVED,
        },
    ]
    attempts = replay_fixture_signals(values, Ledger(tmp_path / "history.db"), base_commit=BASE_COMMIT)

    assert [attempt.input_snapshot.source for attempt in attempts] == ["issue", "local_log"]


def test_replay_is_idempotent_on_repeated_fixture_values(tmp_path) -> None:
    ledger = Ledger(tmp_path / "history.db")
    first = replay_fixture_signals(_fixtures(), ledger, base_commit=BASE_COMMIT)
    replay = replay_fixture_signals(_fixtures(), ledger, base_commit=BASE_COMMIT)

    assert replay == first
    assert ledger.count() == 3
    assert [attempt.attempt_id for attempt in replay] == [attempt.attempt_id for attempt in first]


def test_replay_redacts_fixture_secrets_before_persistence(tmp_path) -> None:
    value = IssueSignal(
        issue_id="issue-secret",
        title="token=topsecret",
        body="Authorization: Bearer abc123",
        observed_at=OBSERVED,
    )
    attempt = replay_fixture_signals([value], Ledger(tmp_path / "history.db"), base_commit=BASE_COMMIT)[0]

    assert "topsecret" not in attempt.input_snapshot.content
    assert "abc123" not in attempt.input_snapshot.content
    assert "[REDACTED]" in attempt.input_snapshot.content


def test_replay_preserves_empty_fixture_content_without_placeholders(tmp_path) -> None:
    values = [
        CIRunSignal(
            run_id="run-empty",
            workflow="ci",
            status="completed",
            conclusion="timed_out",
            commit_sha=BASE_COMMIT,
            output="",
            observed_at=OBSERVED,
        ),
        IssueSignal(issue_id="issue-empty", title="No body", body="", observed_at=OBSERVED),
        LocalLogSignal(log_id="log-empty", path="logs/empty.log", level="error", content="", observed_at=OBSERVED),
    ]
    ledger = Ledger(tmp_path / "history.db")

    attempts = replay_fixture_signals(values, ledger, base_commit=BASE_COMMIT)

    assert [attempt.input_snapshot.content for attempt in attempts] == ["", "No body", ""]
    assert attempts[0].status.value == "failed"
    assert attempts[0].input_snapshot.content_sha256 == hashlib.sha256(b"").hexdigest()
    assert ledger.count() == 3


@pytest.mark.parametrize(
    "value",
    [
        {"source": "unknown", "value": "x"},
        {"source": "issue", "issue_id": "bad", "title": "missing body"},
        {"source": "local_log", "log_id": "bad", "path": "../escape", "level": "error", "content": "x"},
    ],
)
def test_replay_rejects_unknown_or_invalid_fixture_values(tmp_path, value) -> None:
    with pytest.raises((TypeError, ValueError, ValidationError)):
        replay_fixture_signals([value], Ledger(tmp_path / "history.db"), base_commit=BASE_COMMIT)


def test_replay_rejects_entire_batch_before_persisting_partial_results(tmp_path) -> None:
    ledger = Ledger(tmp_path / "history.db")
    values = [
        _fixtures()[0],
        {"source": "issue", "issue_id": "bad", "title": "missing body"},
    ]

    with pytest.raises((TypeError, ValueError, ValidationError)):
        replay_fixture_signals(values, ledger, base_commit=BASE_COMMIT)

    assert ledger.count() == 0
