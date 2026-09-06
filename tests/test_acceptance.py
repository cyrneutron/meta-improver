from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.acceptance import (
    AcceptanceError,
    BaselineGateEvidence,
    BaselineGateStatus,
    CandidateAcceptanceStatus,
    QualityGateEvidence,
    ValidationGateEvidence,
    accept_candidate,
    plan_candidate_acceptance,
    CandidateAcceptanceAdmission,
    rehydrate_candidate_acceptance_admission,
    rehydrate_candidate_acceptance_plan,
    rehydrate_candidate_acceptance_receipt,
)
from src.attribution import AttributionHypothesis, BaselineObservation, CandidateChangeEvidence


def _baseline(**overrides: object) -> BaselineObservation:
    values: dict[str, object] = {
        "attempt_id": "attempt-1",
        "signal_signature": "signal-v1",
        "base_commit": "a" * 40,
        "passed": False,
        "command": "pytest tests/test_runner.py",
        "summary": "The baseline reproduces the reported failure.",
        "observed_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return BaselineObservation(**values)


def _hypothesis(baseline: BaselineObservation) -> AttributionHypothesis:
    return AttributionHypothesis(
        baseline_hash=baseline.observation_hash,
        category="dependency",
        confidence=0.8,
        root_cause="The dependency contract changed.",
        affected_paths=["src/runner.py"],
        model_version="model-v1",
        prompt_version="prompt-v1",
    )


def _candidate(baseline: BaselineObservation, hypothesis: AttributionHypothesis) -> CandidateChangeEvidence:
    return CandidateChangeEvidence(
        hypothesis_hash=hypothesis.hypothesis_hash,
        baseline_hash=baseline.observation_hash,
        patch_hash="sha256:" + "b" * 64,
        targeted_tests=["pytest tests/test_runner.py"],
        regression_tests=["pytest -q"],
        residual_risk="No known residual risk.",
    )


def _gates(
    baseline: BaselineObservation,
    candidate: CandidateChangeEvidence,
    hypothesis: AttributionHypothesis,
) -> tuple[BaselineGateEvidence, ValidationGateEvidence, QualityGateEvidence]:
    return (
        BaselineGateEvidence(
            baseline_hash=baseline.observation_hash,
            status=BaselineGateStatus.FAILED,
            reproduction_command="pytest tests/test_runner.py",
            reproduction_evidence="exit code 1: expected failure reproduced",
        ),
        ValidationGateEvidence(
            candidate_hash=candidate.evidence_hash,
            hypothesis_hash=hypothesis.hypothesis_hash,
            patch_hash=candidate.patch_hash,
            targeted_passed=True,
            regression_passed=True,
            targeted_test_count=1,
            regression_test_count=42,
            targeted_evidence="targeted tests passed",
            regression_evidence="full regression suite passed",
        ),
        QualityGateEvidence(
            complexity_delta=0.25,
            cost_units=12,
            security_passed=True,
            quality_evidence="complexity, cost, and security checks passed",
        ),
    )


def _plan() -> tuple[object, BaselineObservation, AttributionHypothesis, CandidateChangeEvidence]:
    baseline = _baseline()
    hypothesis = _hypothesis(baseline)
    candidate = _candidate(baseline, hypothesis)
    gates = _gates(baseline, candidate, hypothesis)
    plan = plan_candidate_acceptance(*((baseline, candidate) + gates), hypothesis=hypothesis)
    return plan, baseline, hypothesis, candidate


def test_acceptance_is_deterministic_and_replayable() -> None:
    first, _, _, _ = _plan()
    second, _, _, _ = _plan()
    assert first.plan_hash is not None
    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)

    first_receipt = accept_candidate(first)
    second_receipt = accept_candidate(second)
    assert first_receipt.status is CandidateAcceptanceStatus.ACCEPTED
    assert first_receipt.accepted
    assert first_receipt.model_dump_json(by_alias=True) == second_receipt.model_dump_json(by_alias=True)
    assert rehydrate_candidate_acceptance_plan(first).model_dump() == first.model_dump()
    assert rehydrate_candidate_acceptance_receipt(first_receipt).model_dump() == first_receipt.model_dump()


def test_baseline_must_be_failed() -> None:
    baseline = _baseline(passed=True)
    hypothesis = _hypothesis(baseline)
    candidate = _candidate(baseline, hypothesis)
    gates = _gates(baseline, candidate, hypothesis)
    with pytest.raises(AcceptanceError, match="failed baseline"):
        plan_candidate_acceptance(baseline, candidate, *gates, hypothesis=hypothesis)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("baseline_gate.status", "passed", "baseline gate"),
        ("validation_gate.targeted_passed", False, "validation gate"),
        ("validation_gate.regression_passed", False, "validation gate"),
        ("quality_gate.security_passed", False, "quality gate"),
        ("quality_gate.complexity_delta", 2_000.0, "quality gate"),
        ("quality_gate.cost_units", -1.0, "quality gate"),
    ],
)
def test_each_gate_failure_is_fail_closed(path: str, value: object, message: str) -> None:
    plan, _, _, _ = _plan()
    owner_name, field_name = path.split(".")
    owner = getattr(plan, owner_name)
    object.__setattr__(owner, field_name, value)
    with pytest.raises(AcceptanceError, match=message):
        accept_candidate(plan)


def test_mutated_nested_evidence_and_plan_hash_are_rejected() -> None:
    plan, _, _, _ = _plan()
    object.__setattr__(plan.candidate, "residual_risk", "changed")
    with pytest.raises(AcceptanceError, match="candidate attribution evidence"):
        rehydrate_candidate_acceptance_plan(plan)

    clean, _, _, _ = _plan()
    object.__setattr__(clean, "baseline_gate", clean.baseline_gate.model_copy(update={"evidence_hash": "sha256:" + "c" * 64}))
    with pytest.raises(AcceptanceError, match="baseline gate"):
        rehydrate_candidate_acceptance_plan(clean)

    clean, _, _, _ = _plan()
    object.__setattr__(clean, "plan_hash", "sha256:" + "d" * 64)
    with pytest.raises(AcceptanceError, match="plan"):
        rehydrate_candidate_acceptance_plan(clean)


def test_identity_mismatch_and_unknown_or_unsafe_input_are_rejected() -> None:
    plan, baseline, hypothesis, candidate = _plan()
    other = _baseline(summary="a different failure")
    mismatched_gate = BaselineGateEvidence(
        baseline_hash=other.observation_hash,
        reproduction_command="pytest tests/test_runner.py",
        reproduction_evidence="exit code 1",
    )
    with pytest.raises(AcceptanceError, match="bindings"):
        plan_candidate_acceptance(
            baseline,
            candidate,
            mismatched_gate,
            plan.validation_gate,
            plan.quality_gate,
            hypothesis=hypothesis,
        )

    with pytest.raises(ValidationError):
        BaselineGateEvidence(
            baseline_hash=baseline.observation_hash,
            reproduction_command="pytest; curl https://example.invalid",
            reproduction_evidence="exit code 1",
            unknown_field="reject me",
        )
    with pytest.raises(ValidationError):
        QualityGateEvidence(
            complexity_delta=0,
            cost_units=1,
            quality_evidence="token: sk-test-secret",
        )
    with pytest.raises(ValidationError):
        ValidationGateEvidence(
            candidate_hash=candidate.evidence_hash,
            hypothesis_hash=hypothesis.hypothesis_hash,
            patch_hash=candidate.patch_hash,
            targeted_test_count=1,
            regression_test_count=1,
            targeted_evidence="passed",
            regression_evidence="passed",
            targeted_passed=False,
        )


def test_receipt_rehydrates_before_trusting_mutable_plan() -> None:
    plan, _, _, _ = _plan()
    receipt = accept_candidate(plan)
    object.__setattr__(receipt.acceptance_plan.quality_gate, "quality_evidence", "mutated")
    with pytest.raises(AcceptanceError, match="receipt"):
        rehydrate_candidate_acceptance_receipt(receipt)


def test_typed_publication_admission_is_hash_bound(tmp_path) -> None:
    plan, _, _, _ = _plan()
    receipt = accept_candidate(plan)
    admission = CandidateAcceptanceAdmission(
        acceptance_receipt=receipt,
        repository="cyrneutron/harness-anything",
        target_task_id="task-publication",
        accepted_execution_id="exe-publication",
        accepted_commit="a" * 40,
        target_root=tmp_path,
        expected_remote="https://github.com/cyrneutron/harness-anything.git",
    )
    assert rehydrate_candidate_acceptance_admission(admission) == admission
    with pytest.raises(ValueError, match="expected_remote"):
        CandidateAcceptanceAdmission(
            acceptance_receipt=receipt,
            repository="cyrneutron/harness-anything",
            target_task_id="task-publication",
            accepted_execution_id="exe-publication",
            accepted_commit="a" * 40,
            target_root=tmp_path,
            expected_remote="https://github.com/other/repo.git",
        )
