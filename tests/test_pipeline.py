from datetime import datetime, timezone

import pytest

from src.acceptance import (
    BaselineGateEvidence,
    QualityGateEvidence,
    ValidationGateEvidence,
    accept_candidate,
    plan_candidate_acceptance,
)
from src.attribution import AttributionHypothesis, BaselineObservation, CandidateChangeEvidence
from src.pipeline import (
    PipelineError,
    PipelineFacade,
    PipelineInput,
    PipelineStatus,
    plan_pipeline,
    replay_pipeline,
    rehydrate_pipeline_input,
    rehydrate_pipeline_plan,
    rehydrate_pipeline_receipt,
    run_pipeline,
)


def _fixtures(*, passed: bool = False):
    baseline = BaselineObservation(
        attempt_id="attempt-1",
        signal_signature="signal-v1",
        base_commit="a" * 40,
        passed=passed,
        command="pytest tests/test_target.py",
        summary="baseline reproduces the failure",
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    hypothesis = AttributionHypothesis(
        baseline_hash=baseline.observation_hash,
        category="dependency",
        confidence=0.8,
        root_cause="the dependency contract changed",
        affected_paths=["src/target.py"],
        model_version="model-v1",
        prompt_version="prompt-v1",
    )
    candidate = CandidateChangeEvidence(
        hypothesis_hash=hypothesis.hypothesis_hash,
        baseline_hash=baseline.observation_hash,
        patch_hash="sha256:" + "b" * 64,
        targeted_tests=["pytest tests/test_target.py"],
        regression_tests=["pytest -q"],
        residual_risk="no known residual risk",
    )
    gates = (
        BaselineGateEvidence(
            baseline_hash=baseline.observation_hash,
            reproduction_command="pytest tests/test_target.py",
            reproduction_evidence="exit code 1: failure reproduced",
        ),
        ValidationGateEvidence(
            candidate_hash=candidate.evidence_hash,
            hypothesis_hash=hypothesis.hypothesis_hash,
            patch_hash=candidate.patch_hash,
            targeted_test_count=1,
            regression_test_count=2,
            targeted_evidence="targeted tests passed",
            regression_evidence="regression tests passed",
        ),
        QualityGateEvidence(
            complexity_delta=0.1,
            cost_units=1,
            quality_evidence="quality and security checks passed",
        ),
    )
    acceptance_plan = plan_candidate_acceptance(
        baseline, candidate, *gates, hypothesis=hypothesis
    )
    acceptance_receipt = accept_candidate(acceptance_plan)
    return baseline, hypothesis, candidate, acceptance_plan, acceptance_receipt


def test_success_and_replay_are_hash_bound():
    fixtures = _fixtures()
    first = run_pipeline(*fixtures)
    second = replay_pipeline(*_fixtures())
    assert first.status is PipelineStatus.SUCCEEDED
    assert first.receipt_hash is not None
    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    assert rehydrate_pipeline_receipt(first).model_dump() == first.model_dump()


def test_validation_order_rejects_baseline_before_other_contracts():
    _, hypothesis, candidate, plan, receipt = _fixtures()
    baseline = BaselineObservation(
        attempt_id="attempt-1",
        signal_signature="signal-v1",
        base_commit="a" * 40,
        passed=True,
        command="pytest tests/test_target.py",
        summary="baseline reproduces the failure",
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(PipelineError, match="failed baseline"):
        plan_pipeline(baseline, hypothesis, candidate, plan, receipt)


def test_hypothesis_and_candidate_bindings_are_checked_before_acceptance():
    baseline, hypothesis, candidate, plan, receipt = _fixtures()
    other = AttributionHypothesis(
        baseline_hash=baseline.observation_hash,
        category="different-category",
        confidence=hypothesis.confidence,
        root_cause=hypothesis.root_cause,
        affected_paths=hypothesis.affected_paths,
        model_version=hypothesis.model_version,
        prompt_version=hypothesis.prompt_version,
    )
    with pytest.raises(PipelineError, match="hypothesis/candidate bindings"):
        plan_pipeline(baseline, other, candidate, plan, receipt)


def test_rejected_acceptance_cannot_produce_pipeline_receipt():
    baseline, hypothesis, candidate, plan, receipt = _fixtures()
    rejected_receipt = receipt.model_copy(update={"status": "rejected"})
    with pytest.raises(PipelineError, match="acceptance receipt"):
        run_pipeline(baseline, hypothesis, candidate, plan, rejected_receipt)


def test_mutations_are_rejected_by_input_plan_and_receipt_rehydration():
    fixtures = _fixtures()
    input_value = PipelineInput(
        baseline=fixtures[0],
        hypothesis=fixtures[1],
        candidate=fixtures[2],
        acceptance_plan=fixtures[3],
        acceptance_receipt=fixtures[4],
    )
    assert rehydrate_pipeline_input(input_value).input_hash == input_value.input_hash
    plan = plan_pipeline(input_value)
    object.__setattr__(plan.pipeline_input.candidate, "residual_risk", "changed")
    with pytest.raises(PipelineError, match="plan"):
        rehydrate_pipeline_plan(plan)

    clean = plan_pipeline(*fixtures)
    receipt = run_pipeline(clean)
    object.__setattr__(receipt.pipeline_plan.pipeline_input.baseline, "summary", "changed")
    with pytest.raises(PipelineError, match="receipt"):
        rehydrate_pipeline_receipt(receipt)


def test_facade_and_plan_rehydration_are_deterministic():
    fixtures = _fixtures()
    facade = PipelineFacade()
    first = facade.plan(*fixtures)
    second = facade.plan(*_fixtures())
    assert first.plan_hash is not None
    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    assert rehydrate_pipeline_plan(first).model_dump() == first.model_dump()
    assert facade.replay(first).model_dump_json(by_alias=True) == facade.run(second).model_dump_json(by_alias=True)
