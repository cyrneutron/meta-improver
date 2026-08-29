from datetime import datetime, timezone
import hashlib

import pytest
from pydantic import ValidationError

from src.models import Attempt, AttemptStatus, InputSnapshot, MutationProposal, PatchFile, TestEvidence


def snapshot() -> InputSnapshot:
    content = "failure signal"
    return InputSnapshot(source="manual", content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest())


def attempt(**overrides: object) -> Attempt:
    data: dict[str, object] = {
        "attempt_id": "attempt-1",
        "idempotency_key": "signal/base/strategy",
        "signal": "failure signal",
        "base_commit": "a" * 40,
        "strategy_version": "strategy-v1",
        "input_snapshot": snapshot(),
        "model_version": "model-v1",
        "prompt_version": "prompt-v1",
    }
    data.update(overrides)
    return Attempt(**data)


def test_datetimes_are_utc_aware_and_patch_hash_is_derived() -> None:
    proposal = MutationProposal(
        proposal_id="proposal-1",
        base_commit="a" * 40,
        files=[PatchFile(path="src/fix.py", patch="@@ -1 +1 @@")],
        model_version="model-v1",
        prompt_version="prompt-v1",
        rationale="Fix the observed failure.",
    )
    assert proposal.created_at.tzinfo == timezone.utc
    assert proposal.patch_hash and proposal.patch_hash.startswith("sha256:")


def test_rejects_naive_datetimes_and_unsafe_paths() -> None:
    with pytest.raises(ValidationError):
        InputSnapshot(source="manual", content="x", content_sha256="0" * 64, captured_at=datetime.now())
    with pytest.raises(ValidationError):
        PatchFile(path="../escape.py", patch="x")


def test_redacts_secret_metadata_and_bounds_text() -> None:
    value = InputSnapshot(
        source="manual",
        content="token sk-secret",
        content_sha256=hashlib.sha256("token [REDACTED]".encode()).hexdigest(),
        metadata={"api_token": "sk-secret", "safe": "value"},
    )
    assert value.metadata == {"api_token": "[REDACTED]", "safe": "value"}
    assert value.content == "token [REDACTED]"
    with pytest.raises(ValidationError):
        InputSnapshot(source="manual", content="x" * 20_001, content_sha256="0" * 64)
    with pytest.raises(ValidationError):
        InputSnapshot(source="manual", content="x", content_sha256="0" * 64)


def test_failed_attempt_requires_reason() -> None:
    with pytest.raises(ValidationError):
        attempt(status=AttemptStatus.FAILED)
    assert attempt(status=AttemptStatus.FAILED, failure_reason="tests failed").failure_reason == "tests failed"


def test_failed_evidence_requires_reason() -> None:
    with pytest.raises(ValidationError):
        TestEvidence(command="pytest", status="failed")
