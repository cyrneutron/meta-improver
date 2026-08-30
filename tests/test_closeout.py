from datetime import datetime, timedelta, timezone

import pytest

from src.attribution import BaselineObservation
from src.closeout import (
    CloseoutError,
    CloseoutPacket,
    HAConsentReference,
    HAReviewReference,
    rehydrate_closeout_packet,
)
from src.ingestion import HAExecution, HATaskContext, SignalEvent, SignalSource


HASH = "sha256:" + "a" * 64
NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)


def _packet(**overrides: object) -> CloseoutPacket:
    values: dict[str, object] = {
        "signal": "ci/run/123",
        "baseline": HASH,
        "holdout": HASH,
        "candidate": HASH,
        "pipeline_receipt": HASH,
        "acceptance_receipt": HASH,
        "task_id": "task_closeout",
        "execution_id": "exec_closeout",
        "provider": "provider/build-1",
        "base_commit": "b" * 40,
        "patch": HASH,
        "tests": ["pytest -q tests/test_closeout.py", "pytest -q"],
        "risks": ["provider remains unchanged"],
        "rollback": "reset to base commit b" * 1,
        "review": HASH,
        "consent": HASH,
        "publication": HASH,
        "created_at": NOW,
    }
    values.update(overrides)
    return CloseoutPacket(**values)


def test_packet_derives_stable_digests_and_rehydrates() -> None:
    packet = _packet()

    assert packet.packet_hash is not None
    assert packet.tests_digest is not None
    assert packet.risks_digest is not None
    restored = rehydrate_closeout_packet(packet)
    assert restored.model_dump() == packet.model_dump()
    assert restored.created_at.tzinfo is not None
    assert restored.created_at.utcoffset() == timedelta(0)


def test_existing_hash_bound_artifacts_are_accepted() -> None:
    baseline = BaselineObservation(
        attempt_id="attempt-1",
        signal_signature=HASH,
        base_commit="b" * 40,
        passed=False,
        command="pytest -q",
        summary="failure reproduced",
        observed_at=NOW,
    )
    packet = _packet(baseline=baseline)
    assert packet.baseline == baseline.observation_hash


def test_signal_and_ha_contracts_are_compacted_to_stable_identities() -> None:
    signal = SignalEvent(
        source=SignalSource.CI,
        external_id="run-1",
        content="failure",
        observed_at=NOW,
    )
    task = HATaskContext(
        task_id="task_closeout",
        title="Closeout",
        status="active",
        package_path="tasks/task_closeout",
    )
    execution = HAExecution(execution_id="exec_closeout", state="active")
    packet = _packet(signal=signal, task_id=task, execution_id=execution)
    assert packet.signal == signal.signature
    assert packet.task_id == task.task_id
    assert packet.execution_id == execution.execution_id


def test_complete_receipt_objects_must_agree_on_bindings() -> None:
    acceptance = {
        "receipt_hash": HASH,
        "acceptance_plan": {
            "baseline": {"observation_hash": HASH},
            "candidate": {"evidence_hash": HASH, "patch_hash": HASH},
        },
    }
    pipeline = {"receipt_hash": HASH, "acceptance_receipt_hash": HASH}
    assert _packet(acceptance_receipt=acceptance, pipeline_receipt=pipeline).packet_hash
    with pytest.raises(ValueError, match="pipeline receipt"):
        _packet(acceptance_receipt=acceptance, pipeline_receipt={"receipt_hash": HASH, "acceptance_receipt_hash": "sha256:" + "b" * 64})


def test_aliases_cover_digest_and_ha_names() -> None:
    values = _packet().model_dump(mode="json")
    aliases = {
        "baseline": "baseline_hash",
        "holdout": "holdout_digest",
        "candidate": "candidate_hash",
        "pipeline_receipt": "pipeline_receipt_hash",
        "acceptance_receipt": "acceptance_receipt_digest",
        "task_id": "ha_task_id",
        "execution_id": "ha_execution_id",
        "provider": "provider_id",
        "patch": "patch_hash",
        "review": "review_digest",
        "consent": "consent_digest",
        "publication": "publication_digest",
    }
    for field, alias in aliases.items():
        values[alias] = values.pop(field)
    packet = CloseoutPacket(**values)
    assert packet.closeout_hash == packet.packet_hash
    assert packet.baseline_hash == HASH
    assert packet.patch_hash == HASH


def test_packet_rejects_mutation_and_bad_digests() -> None:
    packet = _packet()
    object.__setattr__(packet, "risks", ["changed"])
    with pytest.raises(CloseoutError, match="integrity|hash"):
        rehydrate_closeout_packet(packet)

    with pytest.raises(ValueError, match="packet_hash"):
        _packet(packet_hash=HASH)
    with pytest.raises(ValueError, match="tests_digest"):
        _packet(tests_digest=HASH)


@pytest.mark.parametrize(
    "field,value",
    [
        ("signal", "x" * 501),
        ("tests", ["pytest"] * 201),
        ("risks", ["risk"] * 201),
        ("created_at", datetime(2026, 8, 30, 12)),
        ("consent", "not-a-digest"),
    ],
)
def test_packet_bounds_and_utc_are_fail_closed(field: str, value: object) -> None:
    with pytest.raises(Exception):
        _packet(**{field: value})


def test_unknown_fields_are_forbidden() -> None:
    with pytest.raises(ValueError):
        _packet(unexpected="value")


def _ha_references(**overrides: object) -> tuple[HAReviewReference, HAConsentReference]:
    review_values: dict[str, object] = {
        "task_id": "task_closeout",
        "execution_id": "exec_closeout",
        "review_id": "review_1",
        "review_digest": HASH,
        "content_digest": HASH,
        "packet_digest": HASH,
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(hours=1),
    }
    consent_values: dict[str, object] = {
        "task_id": "task_closeout",
        "execution_id": "exec_closeout",
        "review_id": "review_1",
        "consent_id": "consent_1",
        "review_digest": HASH,
        "content_digest": HASH,
        "packet_digest": HASH,
        "consent_digest": HASH,
        "approver_identity": "reviewer/alice",
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(hours=1),
    }
    review_values.update(overrides)
    consent_values.update(overrides)
    return HAReviewReference(**review_values), HAConsentReference(**consent_values)


def test_structured_ha_review_and_consent_references_bind_packet() -> None:
    review, consent = _ha_references()
    packet = _packet(
        review=review,
        consent=consent,
        review_reference=review,
        consent_reference=consent,
    )
    assert packet.review == HASH
    assert packet.consent == HASH
    assert packet.review_reference is not None
    assert packet.consent_reference is not None
    assert rehydrate_closeout_packet(packet).model_dump() == packet.model_dump()


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "task_other"),
        ("execution_id", "exec_other"),
        ("review_id", "review_other"),
        ("review_digest", "sha256:" + "b" * 64),
        ("content_digest", "sha256:" + "b" * 64),
        ("packet_digest", "sha256:" + "b" * 64),
    ],
)
def test_structured_ha_reference_wrong_bindings_fail_closed(field: str, value: object) -> None:
    review, consent = _ha_references(**{field: value})
    if field == "review_id":
        review_values = review.model_dump()
        review_values["review_id"] = value
        review = HAReviewReference(**review_values)
        _review, consent = _ha_references()
    with pytest.raises(ValueError, match="HA reference|binding|bound"):
        _packet(review=review, consent=consent, review_reference=review, consent_reference=consent)


def test_structured_ha_reference_expiry_is_checked_at_closeout_time() -> None:
    review, consent = _ha_references(expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="expired|valid"):
        _packet(review=review, consent=consent, review_reference=review, consent_reference=consent)


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "../task"),
        ("execution_id", "exec//closeout"),
        ("review_id", "review;rm"),
        ("approver_identity", "Authorization: Bearer abc"),
    ],
)
def test_structured_ha_reference_unsafe_identity_fails_closed(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _ha_references(**{field: value})


def test_structured_ha_reference_hash_tampering_is_rejected() -> None:
    review, _consent = _ha_references()
    values = review.model_dump()
    values["review_digest"] = "sha256:" + "b" * 64
    values["reference_hash"] = review.reference_hash
    with pytest.raises(ValueError, match="reference_hash|canonical"):
        HAReviewReference(**values)
