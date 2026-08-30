import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.scheduler import (
    DispatchRequest,
    DispatchStatus,
    SQLiteDispatchCoordinator,
    SchedulerPersistenceError,
)


HASH = "sha256:" + "a" * 64
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def request(*, key: str = "idem-1", event: str = "event-1") -> DispatchRequest:
    return DispatchRequest(
        idempotency_key=key,
        event_id=event,
        proposal_plan_hash=HASH,
        action="proposal",
        attempt=1,
    )


def test_restart_rehydrates_idempotency_and_lock(tmp_path) -> None:
    path = tmp_path / "dispatch.sqlite"
    req = request()
    first = SQLiteDispatchCoordinator(path).dispatch(req, now=NOW)
    assert first.status is DispatchStatus.ACCEPTED

    restarted = SQLiteDispatchCoordinator(path)
    replay = restarted.dispatch(req, now=NOW + timedelta(hours=1))
    assert replay.status is DispatchStatus.DEDUPLICATED
    assert replay.receipt_hash == restarted.dispatch(req, now=NOW).receipt_hash

    locked_req = request(key="locked", event="locked-event")
    restarted.acquire_lock(locked_req)
    assert SQLiteDispatchCoordinator(path).dispatch(locked_req, now=NOW).status is DispatchStatus.LOCKED


def test_restart_rehydrates_backoff_and_circuit_state(tmp_path) -> None:
    path = tmp_path / "dispatch.sqlite"
    req = request()
    coordinator = SQLiteDispatchCoordinator(path, failure_threshold=2, base_backoff_seconds=2, max_backoff_seconds=3)
    coordinator.dispatch(req, now=NOW)
    failed = coordinator.fail(req, now=NOW, reason="temporary")
    assert failed.status is DispatchStatus.BACKOFF

    restarted = SQLiteDispatchCoordinator(path)
    assert restarted.dispatch(req, now=NOW + timedelta(seconds=1)).status is DispatchStatus.BACKOFF
    restarted.dispatch(req, now=NOW + timedelta(seconds=2))
    opened = restarted.fail(req, now=NOW + timedelta(seconds=2), reason="persistent")
    assert opened.status is DispatchStatus.CIRCUIT_OPEN
    assert SQLiteDispatchCoordinator(path).dispatch(req, now=NOW + timedelta(days=1)).status is DispatchStatus.CIRCUIT_OPEN


def test_corrupt_journal_fails_closed(tmp_path) -> None:
    path = tmp_path / "dispatch.sqlite"
    coordinator = SQLiteDispatchCoordinator(path)
    coordinator.dispatch(request(), now=NOW)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE dispatch_journal SET snapshot_json = ? WHERE sequence = 1", (json.dumps({"schema": "wrong"}),))
    with pytest.raises(SchedulerPersistenceError):
        SQLiteDispatchCoordinator(path)


def test_journal_does_not_persist_secrets(tmp_path) -> None:
    path = tmp_path / "dispatch.sqlite"
    SQLiteDispatchCoordinator(path).dispatch(request(), now=NOW)
    with sqlite3.connect(path) as db:
        payload = db.execute("SELECT snapshot_json FROM dispatch_journal").fetchone()[0]
    assert "approval" not in payload
    assert "sk-test-secret" not in payload
    assert "token" not in payload.casefold()
