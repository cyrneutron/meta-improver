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


def test_chain_is_frozen_and_chain_hash_is_verified() -> None:
    chain = ReportChain(reports=(report("a-r1", "reviewer_a", 1),))
    with pytest.raises((ValidationError, TypeError)):
        chain.reports += (report("b-r1", "reviewer_b", 1, ("report/a-r1",)),)

    payload = chain.model_dump(mode="json", by_alias=True)
    payload["chain_hash"] = "sha256:" + "0" * 64
    with pytest.raises((ReportChainError, ValidationError)):
        rehydrate_report_chain(ReportChain.model_validate(payload))


def test_chain_rejects_unordered_report_collections() -> None:
    first = report("a-r1", "reviewer_a", 1)
    for reports in ({first}, frozenset({first})):
        with pytest.raises(ValidationError, match="unordered report input"):
            ReportChain(reports=reports)


def test_chain_requires_reviewer_roles_and_rounds_in_order() -> None:
    first = report("a-r1", "reviewer_a", 1)
    with pytest.raises((ReportChainError, ValidationError), match="reviewer_b"):
        ReportChain(reports=(first, report("a-r2", "reviewer_a", 2, ("report/a-r1",))))
    with pytest.raises((ReportChainError, ValidationError), match="round 1"):
        ReportChain(reports=(first, report("b-r2", "reviewer_b", 2, ("report/a-r1",))))

    second = report("b-r1", "reviewer_b", 1, ("report/a-r1",))
    with pytest.raises((ReportChainError, ValidationError), match="reviewer_a"):
        ReportChain(reports=(first, second, report("b-r2", "reviewer_b", 2, ("report/b-r1",))))
    with pytest.raises((ReportChainError, ValidationError), match="round 2"):
        ReportChain(reports=(first, second, report("a-r3", "reviewer_a", 3, ("report/b-r1",))))


def test_append_revalidates_existing_chain_before_extending() -> None:
    chain = ReportChain(reports=(report("a-r1", "reviewer_a", 1),))
    object.__setattr__(chain, "chain_hash", "sha256:" + "0" * 64)
    with pytest.raises(ReportChainError, match="integrity"):
        append_report(chain, report("b-r1", "reviewer_b", 1, ("report/a-r1",)))


def test_nested_report_input_is_redacted_and_control_fields_are_rejected() -> None:
    secret = "Token sk-example-secret"
    value = report(
        "a-r1",
        "reviewer_a",
        1,
        summary=secret,
        source_identity=secret,
        target_harness_identity=secret,
        findings=(
            ReportFinding(
                claim_id="claim-a-r1",
                assessment=secret,
                severity="high",
                evidence_refs=(secret,),
            ),
        ),
        evidence=(ReportEvidence(ref="evidence-a-r1", kind="artifact", summary=secret),),
        challenged_claims=(secret,),
        open_disagreements=(secret,),
    )
    serialized = value.model_dump_json()
    assert "sk-example-secret" not in serialized

    raw_payload = value.model_dump(mode="json", by_alias=True)
    raw_payload["findings"][0]["assessment"] = secret
    raw_payload["evidence"][0]["summary"] = secret
    hydrated = ReportArtifact.model_validate(raw_payload)
    assert "sk-example-secret" not in hydrated.model_dump_json()

    raw_payload["findings"][0]["assessment"] = secret + "\n"
    with pytest.raises(ValidationError):
        ReportArtifact.model_validate(raw_payload)

    raw_key_payload = value.model_dump(mode="json", by_alias=True)
    raw_key_payload["bad\tkey"] = "value"
    with pytest.raises(ValidationError):
        ReportArtifact.model_validate(raw_key_payload)

    raw_nested_key_payload = value.model_dump(mode="python", by_alias=True)
    raw_nested_key_payload["findings"][0][("bad\nkey",)] = "value"
    with pytest.raises(ValidationError):
        ReportArtifact.model_validate(raw_nested_key_payload)

    tampered_finding = ReportFinding(
        claim_id="claim-a-r1",
        assessment="assessment",
        severity="low",
    )
    object.__setattr__(tampered_finding, "assessment", "assessment\tinvalid")
    with pytest.raises(ValidationError):
        report("a-r1", "reviewer_a", 1, findings=(tampered_finding,))

    with pytest.raises(ValidationError):
        ReportEvidence(ref="evidence\ninvalid", kind="test", summary="summary")
    with pytest.raises(ValidationError):
        ReportEvidence(ref="\nevidence", kind="test", summary="summary")
    with pytest.raises(ValidationError):
        ReportFinding(
            claim_id="claim-a-r1",
            assessment="assessment",
            severity="low",
            evidence_refs=("evidence\ninvalid",),
        )
    with pytest.raises(ValidationError):
        ReportFinding(
            claim_id="claim-a-r1",
            assessment="assessment\n",
            severity="low",
        )
    with pytest.raises(ValidationError):
        ReportFinding(
            claim_id="claim-a-r1",
            assessment="assessment",
            severity="low",
            evidence_refs={secret + "\n"},
        )
    with pytest.raises(ValidationError):
        report("a-r1", "reviewer_a", 1, challenged_claims={secret})
    with pytest.raises(ValidationError):
        report("a-r1", "reviewer_a", 1, source_identity="source\nidentity")
    with pytest.raises(ValidationError):
        report("a-r1", "reviewer_a", 1, summary="x" * 8_001)


@pytest.mark.parametrize("bad_control", ["summary\tinvalid", "summary\x1binvalid", "summary\vinvalid"])
def test_all_ascii_control_characters_are_rejected(bad_control: str) -> None:
    with pytest.raises(ValidationError):
        report("a-r1", "reviewer_a", 1, summary=bad_control)


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
