import pytest
from pydantic import ValidationError

from src.models import (
    ReportArtifact,
    ReportChain,
    ReportChainError,
    ReportEvidence,
    ReportFinding,
    append_report,
    rehydrate_report_artifact,
    rehydrate_report_chain,
)


def report(
    report_id: str,
    role: str,
    round_number: int,
    prior: tuple[str, ...] = (),
    **overrides: object,
) -> ReportArtifact:
    values: dict[str, object] = {
        "report_id": report_id,
        "attempt_id": f"attempt-{report_id}",
        "reviewer_id": role,
        "reviewer_role": role,
        "round": round_number,
        "prior_report_refs": prior,
        "source_identity": "ha-target@commit-a",
        "target_harness_identity": "ha-target/revision-1",
        "model_version": f"model-{role}",
        "prompt_version": "prompt-v1",
        "summary": f"Summary for {report_id}",
        "findings": (
            ReportFinding(
                claim_id=f"claim-{report_id}",
                assessment="Evidence supports this assessment.",
                severity="medium",
                evidence_refs=(f"evidence-{report_id}",),
            ),
        ),
        "evidence": (
            ReportEvidence(ref=f"evidence-{report_id}", kind="test", summary="Focused test evidence"),
        ),
    }
    values.update(overrides)
    return ReportArtifact(**values)


def test_append_only_chain_accepts_a_b_a_then_leader_and_preserves_previous_value() -> None:
    first = report("a-r1", "reviewer_a", 1)
    chain = ReportChain(reports=(first,))
    second = report("b-r1", "reviewer_b", 1, ("report/a-r1",))
    third = report("a-r2", "reviewer_a", 2, ("report/b-r1",))
    synthesis = report("leader-r1", "leader_synthesis", 3, ("report/a-r2",))

    chain_after_b = append_report(chain, second)
    chain_after_a = append_report(chain_after_b, third)
    final = append_report(chain_after_a, synthesis)

    assert [item.report_id for item in final.reports] == ["a-r1", "b-r1", "a-r2", "leader-r1"]
    assert chain.chain_hash != final.chain_hash
    assert len(chain.reports) == 1
    assert rehydrate_report_chain(final) == final


def test_report_and_chain_are_frozen_and_hash_bound() -> None:
    value = report("a-r1", "reviewer_a", 1)
    with pytest.raises((ValidationError, TypeError)):
        value.summary = "mutated"
    with pytest.raises((ValidationError, TypeError)):
        value.findings += (value.findings[0],)

    payload = value.model_dump(mode="json", by_alias=True)
    payload["summary"] = "tampered"
    with pytest.raises((ReportChainError, ValidationError)):
        rehydrate_report_artifact(ReportArtifact.model_validate(payload))


def test_forward_duplicate_and_post_synthesis_reports_are_rejected() -> None:
    first = report("a-r1", "reviewer_a", 1)
    with pytest.raises((ReportChainError, ValidationError), match="forward"):
        ReportChain(reports=(first, report("b-r1", "reviewer_b", 1, ("report/later",))))
    with pytest.raises((ReportChainError, ValidationError), match="duplicate"):
        ReportChain(reports=(first, report("a-r1", "reviewer_a", 2, ("report/a-r1",))))
    synthesis = report("leader-r1", "leader_synthesis", 2, ("report/a-r1",))
    with pytest.raises((ReportChainError, ValidationError), match="final"):
        ReportChain(reports=(first, synthesis, report("b-r1", "reviewer_b", 3, ("report/leader-r1",))))


def test_report_input_is_redacted_before_hashing() -> None:
    value = report("a-r1", "reviewer_a", 1, summary="Token sk-example-secret")
    assert value.summary == "Token [REDACTED]"
    assert "sk-example-secret" not in value.model_dump_json()
    assert rehydrate_report_artifact(value) == value
