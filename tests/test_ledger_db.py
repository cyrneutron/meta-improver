from concurrent.futures import ThreadPoolExecutor
import hashlib

import pytest

from src.models import Attempt
from src.storage import Ledger, LedgerConflictError


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
