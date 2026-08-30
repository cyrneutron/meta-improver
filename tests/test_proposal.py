from datetime import datetime, timedelta, timezone

import pytest

from src.proposal import (
    ApprovalScope,
    ProposalAction,
    ProposalError,
    ProposalPayload,
    approve_proposal,
    authorize_action,
    plan_proposal,
    rehydrate_approval_token,
    rehydrate_proposal_payload,
    rehydrate_proposal_plan,
)


HASH = "sha256:" + "b" * 64
RECEIPT = "sha256:" + "c" * 64
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _payload() -> ProposalPayload:
    return ProposalPayload(
        repo="cyrneutron/meta-improver",
        head="mi/attempt-1",
        base="main",
        base_commit="a" * 40,
        patch_hash=HASH,
        acceptance_receipt_hash=RECEIPT,
        changed_paths=["tests/test_proposal.py", "src/proposal.py"],
        title="Add proposal protocol",
        body="This proposal is backed by acceptance evidence.",
    )


def test_payload_and_plan_are_deterministic_and_hash_bound() -> None:
    first = _payload()
    second = _payload()
    assert first.payload_hash is not None
    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    assert first.changed_paths == ["src/proposal.py", "tests/test_proposal.py"]

    plan = plan_proposal(first)
    assert plan.mode.value == "proposal_only"
    assert plan.plan_hash is not None
    assert rehydrate_proposal_payload(first).model_dump() == first.model_dump()
    assert rehydrate_proposal_plan(plan).model_dump() == plan.model_dump()


def test_nested_mutation_is_rejected_by_rehydration() -> None:
    plan = plan_proposal(_payload())
    object.__setattr__(plan.payload, "title", "mutated title")
    with pytest.raises(ProposalError, match="plan"):
        rehydrate_proposal_plan(plan)


@pytest.mark.parametrize(
    "field,value",
    [
        ("repo", "../repo"),
        ("head", "refs/heads/x;echo bad"),
        ("base", ""),
        ("changed_paths", ["../secret.txt"]),
        ("title", "fix $HOME"),
        ("body", "Authorization: Bearer abc123"),
    ],
)
def test_unsafe_payload_fields_fail_closed(field: str, value: object) -> None:
    values = _payload().model_dump()
    values[field] = value
    with pytest.raises(Exception):
        ProposalPayload.model_validate(values)


def test_approval_requires_exact_scope_plan_and_valid_time_window() -> None:
    plan = plan_proposal(_payload())
    token = approve_proposal(
        plan,
        ApprovalScope.PUSH,
        approved_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    assert token.token_hash is not None
    assert rehydrate_approval_token(token).model_dump() == token.model_dump()
    assert authorize_action(plan, ProposalAction.PUSH, token, now=NOW + timedelta(minutes=1))

    with pytest.raises(ProposalError, match="scope"):
        authorize_action(plan, ProposalAction.CREATE_PR, token, now=NOW)
    with pytest.raises(ProposalError, match="expired"):
        authorize_action(plan, ProposalAction.PUSH, token, now=NOW + timedelta(hours=1))

    other_payload = _payload().model_dump(exclude={"payload_hash"})
    other_payload["head"] = "mi/attempt-2"
    other = plan_proposal(ProposalPayload.model_validate(other_payload))
    with pytest.raises(ProposalError, match="different"):
        authorize_action(other, ProposalAction.PUSH, token, now=NOW)


def test_default_is_proposal_only_and_no_push_or_merge_occurs() -> None:
    plan = plan_proposal(_payload())
    assert authorize_action(plan, ProposalAction.PROPOSAL, now=NOW)
    with pytest.raises(ProposalError, match="requires"):
        authorize_action(plan, ProposalAction.PUSH)
    with pytest.raises(ProposalError, match="requires"):
        authorize_action(plan, ProposalAction.CREATE_PR)
    with pytest.raises(ProposalError, match="permanently"):
        authorize_action(plan, ProposalAction.MERGE_MAIN)
    with pytest.raises(ProposalError, match="permanently"):
        approve_proposal(plan, ApprovalScope.MERGE_MAIN, approved_at=NOW, expires_at=NOW + timedelta(hours=1))


def test_unknown_action_and_extra_fields_are_rejected() -> None:
    plan = plan_proposal(_payload())
    with pytest.raises(ProposalError, match="unknown"):
        authorize_action(plan, "delete_repo")
    values = _payload().model_dump()
    values["unexpected"] = "value"
    with pytest.raises(Exception):
        ProposalPayload.model_validate(values)
