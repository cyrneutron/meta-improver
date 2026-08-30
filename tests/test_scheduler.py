from datetime import datetime, timedelta, timezone

import pytest

from src.scheduler import (
    DispatchAction,
    DispatchStatus,
    InMemoryDispatchCoordinator,
    SchedulerError,
    DispatchRequest,
    rehydrate_dispatch_receipt,
    rehydrate_dispatch_request,
)
from src.proposal import ApprovalScope, ApprovalToken


HASH = "sha256:" + "a" * 64
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def request(*, key: str = "idem-1", event: str = "event-1", action: DispatchAction = DispatchAction.PROPOSAL, attempt: int = 1) -> DispatchRequest:
    return DispatchRequest(
        idempotency_key=key,
        event_id=event,
        proposal_plan_hash=HASH,
        action=action,
        attempt=attempt,
    )


def test_request_and_receipt_are_deterministic_and_hash_bound() -> None:
    first = request()
    second = request()
    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    assert rehydrate_dispatch_request(first).model_dump() == first.model_dump()
    coordinator = InMemoryDispatchCoordinator()
    receipt = coordinator.dispatch(first, now=NOW)
    assert receipt.status is DispatchStatus.ACCEPTED
    assert rehydrate_dispatch_receipt(receipt).model_dump() == receipt.model_dump()


def test_replay_by_idempotency_or_event_is_deterministically_deduplicated() -> None:
    coordinator = InMemoryDispatchCoordinator()
    first = coordinator.dispatch(request(), now=NOW)
    replay = coordinator.dispatch(request(), now=NOW + timedelta(hours=1))
    assert replay.status is DispatchStatus.DEDUPLICATED
    assert replay.receipt_hash == coordinator.dispatch(request(), now=NOW + timedelta(hours=2)).receipt_hash

    other_key_same_event = request(key="idem-2")
    event_replay = coordinator.dispatch(other_key_same_event, now=NOW)
    assert event_replay.status is DispatchStatus.DEDUPLICATED


def test_conflict_and_explicit_lock_fail_closed() -> None:
    coordinator = InMemoryDispatchCoordinator()
    coordinator.lock(request())
    locked = coordinator.dispatch(request(), now=NOW)
    assert locked.status is DispatchStatus.LOCKED
    conflicting = coordinator.dispatch(request(key="idem-2", event="event-2", attempt=2), now=NOW)
    assert conflicting.status is DispatchStatus.ACCEPTED

    conflict = coordinator.dispatch(request(key="idem-1", event="event-3", attempt=2), now=NOW)
    assert conflict.status is DispatchStatus.REJECTED


def test_bounded_exponential_backoff_and_circuit_breaker() -> None:
    coordinator = InMemoryDispatchCoordinator(failure_threshold=3, base_backoff_seconds=2, max_backoff_seconds=3)
    req = request()
    coordinator.dispatch(req, now=NOW)
    first = coordinator.fail(req, reason="temporary", now=NOW)
    assert first.status is DispatchStatus.BACKOFF
    assert first.state.next_retry_at == NOW + timedelta(seconds=2)
    blocked = coordinator.dispatch(req, now=NOW + timedelta(seconds=1))
    assert blocked.status is DispatchStatus.BACKOFF
    coordinator.dispatch(req, now=NOW + timedelta(seconds=2))
    second = coordinator.fail(req, reason="temporary", now=NOW + timedelta(seconds=2))
    assert second.status is DispatchStatus.BACKOFF
    assert second.state.next_retry_at == NOW + timedelta(seconds=5)
    coordinator.dispatch(req, now=NOW + timedelta(seconds=5))
    opened = coordinator.fail(req, reason="persistent", now=NOW + timedelta(seconds=5))
    assert opened.status is DispatchStatus.CIRCUIT_OPEN
    assert opened.state.next_retry_at is None
    assert coordinator.dispatch(req, now=NOW + timedelta(days=1)).status is DispatchStatus.CIRCUIT_OPEN


@pytest.mark.parametrize("action", [DispatchAction.PUSH, DispatchAction.CREATE_PR])
def test_push_and_create_pr_require_matching_authorization(action: DispatchAction) -> None:
    req = request(action=action)
    assert InMemoryDispatchCoordinator().dispatch(req, now=NOW).status is DispatchStatus.REJECTED
    assert InMemoryDispatchCoordinator(approval=True).dispatch(req, now=NOW).status is DispatchStatus.REJECTED
    assert InMemoryDispatchCoordinator(approval="proposal").dispatch(req, now=NOW).status is DispatchStatus.REJECTED
    assert InMemoryDispatchCoordinator(approval=action.value).dispatch(req, now=NOW).status is DispatchStatus.REJECTED
    token = ApprovalToken(
        scope=ApprovalScope(action.value),
        plan_hash=HASH,
        actor="reviewer/alice",
        review_digest=HASH,
        content_digest=HASH,
        approved_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    assert InMemoryDispatchCoordinator(approval=token).dispatch(req, now=NOW).status is DispatchStatus.ACCEPTED


@pytest.mark.parametrize("approval", [True, "push", "create_pr"])
def test_publish_rejects_approval_without_complete_consent_token(approval: object) -> None:
    action = DispatchAction.PUSH if approval != "create_pr" else DispatchAction.CREATE_PR
    receipt = InMemoryDispatchCoordinator(approval=approval).dispatch(
        request(action=action), now=NOW
    )
    assert receipt.status is DispatchStatus.REJECTED
    assert "complete approval token" in receipt.reason


def test_merge_main_is_always_rejected_and_proposal_is_queueable() -> None:
    assert InMemoryDispatchCoordinator(approval=True).dispatch(request(action=DispatchAction.MERGE_MAIN), now=NOW).status is DispatchStatus.REJECTED
    assert InMemoryDispatchCoordinator().dispatch(request(action=DispatchAction.PROPOSAL), now=NOW).status is DispatchStatus.ACCEPTED


def test_mutation_and_unknown_fields_fail_closed() -> None:
    req = request()
    object.__setattr__(req, "event_id", "mutated")
    with pytest.raises(SchedulerError):
        rehydrate_dispatch_request(req)
    with pytest.raises(Exception):
        DispatchRequest.model_validate({**request().model_dump(), "unexpected": "value"})
    with pytest.raises(Exception):
        DispatchRequest(idempotency_key="a;rm", event_id="e", proposal_plan_hash=HASH, action="proposal", attempt=1)
