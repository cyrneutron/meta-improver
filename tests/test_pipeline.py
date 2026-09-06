from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib

import pytest

from src.acceptance import (
    BaselineGateEvidence,
    QualityGateEvidence,
    ValidationGateEvidence,
    accept_candidate,
    plan_candidate_acceptance,
)
from src.attribution import AttributionHypothesis, BaselineObservation, CandidateChangeEvidence
from src.ingestion import IngestionService, IssueSignal, normalize_issue
from src.ha_diagnosis import HaDiagnosis, HaDiagnosisStatus, record_squad_diagnosis_attempt
from src.models import AttemptStage, AttemptStatus, InputSnapshot
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
from src.storage import Ledger, LedgerConflictError
from src.target_publication import (
    RequiredCheck,
    TargetPublicationReceipt,
    TargetPublicationRequest,
    TargetPublicationStatus,
)
from src.acceptance import CandidateAcceptanceAdmission


def _fixtures(
    *,
    passed: bool = False,
    attempt_id: str = "attempt-1",
    signal_signature: str = "signal-v1",
    model_version: str = "model-v1",
    prompt_version: str = "prompt-v1",
):
    baseline = BaselineObservation(
        attempt_id=attempt_id,
        signal_signature=signal_signature,
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
        model_version=model_version,
        prompt_version=prompt_version,
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


def _snapshot(content: str = "captured failure signal") -> InputSnapshot:
    return InputSnapshot(
        source="manual",
        content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


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


def test_pipeline_persists_one_attempt_lifecycle_and_replays_idempotently(tmp_path):
    ledger = Ledger(tmp_path / "history.db")
    fixtures = _fixtures()
    first = run_pipeline(
        *fixtures,
        ledger=ledger,
        input_snapshot=_snapshot(),
        strategy_version="strategy-v1",
    )
    replay = replay_pipeline(
        *_fixtures(),
        ledger=ledger,
        input_snapshot=_snapshot(),
        strategy_version="strategy-v1",
    )

    attempt = ledger.get_attempt("attempt-1")
    assert attempt is not None
    assert attempt.status is AttemptStatus.SUCCEEDED
    assert attempt.stage is AttemptStage.COMPLETED
    assert attempt.base_commit == "a" * 40
    assert attempt.model_version == "model-v1"
    assert attempt.prompt_version == "prompt-v1"
    assert attempt.baseline_hash == fixtures[0].observation_hash
    assert attempt.diagnosis_hash == fixtures[1].hypothesis_hash
    assert attempt.patch_hash == fixtures[2].patch_hash
    assert attempt.acceptance_receipt_hash == fixtures[4].receipt_hash
    assert attempt.pipeline_receipt_hash == first.receipt_hash
    assert [evidence.command for evidence in attempt.test_evidence] == [
        "pytest tests/test_target.py",
        "pytest -q",
    ]
    assert first == replay
    assert ledger.count() == 1
    assert [event.stage for event in ledger.iter_attempt_events("attempt-1")] == list(
        AttemptStage
    )


def test_pipeline_continues_the_attempt_created_by_ingestion(tmp_path):
    ledger = Ledger(tmp_path / "history.db")
    event = normalize_issue(
        IssueSignal(
            issue_id="issue-cross-entry",
            title="Failure",
            body="Details",
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    ingested = IngestionService(
        ledger,
        base_commit="a" * 40,
        strategy_version="strategy-v1",
        model_version="model-v1",
        prompt_version="prompt-v1",
    ).ingest(event)
    fixtures = _fixtures(
        attempt_id=ingested.attempt_id,
        signal_signature=ingested.signal,
    )

    receipt = run_pipeline(
        *fixtures,
        ledger=ledger,
        input_snapshot=ingested.input_snapshot,
        strategy_version="strategy-v1",
    )

    completed = ledger.get_attempt(ingested.attempt_id)
    assert completed is not None
    assert completed.idempotency_key == ingested.idempotency_key
    assert completed.status is AttemptStatus.SUCCEEDED
    assert completed.stage is AttemptStage.COMPLETED
    assert completed.pipeline_receipt_hash == receipt.receipt_hash
    assert ledger.count() == 1
    assert [event.stage for event in ledger.iter_attempt_events(ingested.attempt_id)] == list(
        AttemptStage
    )


def test_pipeline_reuses_captured_diagnosis_attempt_and_preserves_source(tmp_path):
    ledger = Ledger(tmp_path / "history.db")
    diagnosis = HaDiagnosis(
        diagnosis_id="diag-1",
        task_id="task-1",
        squad_id="squad-1",
        run_id="run-1",
        status=HaDiagnosisStatus.CONVERGED,
        summary="Found the failure boundary.",
        provider_version="provider-v1",
        provider_build_id="build-1",
        poll_attempts=1,
        receipt_digest="sha256:" + "c" * 64,
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    attempt = record_squad_diagnosis_attempt(
        ledger,
        diagnosis,
        base_commit="a" * 40,
        strategy_version="strategy-v1",
        model_version="model-v1",
        prompt_version="prompt-v1",
    )
    fixtures = _fixtures(
        attempt_id=attempt.attempt_id,
        signal_signature=attempt.signal,
    )

    run_pipeline(
        *fixtures,
        ledger=ledger,
        input_snapshot=attempt.input_snapshot,
        strategy_version="strategy-v1",
    )

    completed = ledger.get_attempt(attempt.attempt_id)
    assert completed is not None
    assert completed.status is AttemptStatus.SUCCEEDED
    assert completed.stage is AttemptStage.COMPLETED
    assert completed.source_diagnosis_id == diagnosis.diagnosis_id
    assert completed.source_diagnosis_hash == diagnosis.record_hash
    assert ledger.count() == 1
    assert [event.stage for event in ledger.iter_attempt_events(attempt.attempt_id)] == list(
        AttemptStage
    )


def test_pipeline_validation_failure_is_recorded_and_replayed(tmp_path):
    ledger = Ledger(tmp_path / "history.db")
    _, hypothesis, candidate, plan, receipt = _fixtures()
    invalid_baseline = BaselineObservation(
        attempt_id="attempt-1",
        signal_signature="signal-v1",
        base_commit="a" * 40,
        passed=True,
        command="pytest tests/test_target.py",
        summary="baseline no longer reproduces the failure",
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    arguments = (invalid_baseline, hypothesis, candidate, plan, receipt)

    for _ in range(2):
        with pytest.raises(PipelineError, match="failed baseline"):
            run_pipeline(
                *arguments,
                ledger=ledger,
                input_snapshot=_snapshot(),
                strategy_version="strategy-v1",
            )

    attempt = ledger.get_attempt("attempt-1")
    assert attempt is not None
    assert attempt.status is AttemptStatus.REJECTED
    assert attempt.stage is AttemptStage.BASELINE_EVALUATED
    assert attempt.failure_reason == "pipeline requires a failed baseline"
    assert [event.status for event in ledger.iter_attempt_events("attempt-1")] == [
        AttemptStatus.PROPOSED,
        AttemptStatus.RUNNING,
        AttemptStatus.REJECTED,
    ]


def test_pipeline_same_key_with_different_input_fails_closed(tmp_path):
    ledger = Ledger(tmp_path / "history.db")
    run_pipeline(
        *_fixtures(),
        ledger=ledger,
        input_snapshot=_snapshot(),
        strategy_version="strategy-v1",
    )

    with pytest.raises(LedgerConflictError, match="different pipeline attempt"):
        run_pipeline(
            *_fixtures(),
            ledger=ledger,
            input_snapshot=_snapshot("different captured signal"),
            strategy_version="strategy-v1",
        )

    assert ledger.count() == 1


def test_pipeline_same_attempt_id_with_different_identity_fails_closed(tmp_path):
    ledger = Ledger(tmp_path / "history.db")
    run_pipeline(
        *_fixtures(),
        ledger=ledger,
        input_snapshot=_snapshot(),
        strategy_version="strategy-v1",
    )

    with pytest.raises(LedgerConflictError, match="attempt id belongs"):
        run_pipeline(
            *_fixtures(signal_signature="different-signal"),
            ledger=ledger,
            input_snapshot=_snapshot(),
            strategy_version="strategy-v1",
        )

    assert ledger.count() == 1


def test_concurrent_same_pipeline_attempt_has_one_event_per_stage(tmp_path):
    ledger = Ledger(tmp_path / "history.db")

    def run_once(_index):
        return run_pipeline(
            *_fixtures(),
            ledger=ledger,
            input_snapshot=_snapshot(),
            strategy_version="strategy-v1",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(run_once, range(4)))

    assert all(receipt == receipts[0] for receipt in receipts)
    assert ledger.count() == 1
    assert [event.stage for event in ledger.iter_attempt_events("attempt-1")] == list(
        AttemptStage
    )


def test_pipeline_binds_successful_publication_receipt(tmp_path):
    baseline, hypothesis, candidate, acceptance_plan, acceptance_receipt = _fixtures()
    admission = CandidateAcceptanceAdmission(
        acceptance_receipt=acceptance_receipt,
        repository="cyrneutron/harness-anything",
        target_task_id="task-publication",
        accepted_execution_id="exe-publication",
        accepted_commit="a" * 40,
        target_root=tmp_path,
        expected_remote="https://github.com/cyrneutron/harness-anything.git",
    )
    publication_request = TargetPublicationRequest(
        admission=admission,
        title="publish accepted target",
        body="A bounded publication body.",
        poll_interval_seconds=0,
    )
    publication = TargetPublicationReceipt(
        status=TargetPublicationStatus.SUCCEEDED,
        admission=admission,
        request_hash=publication_request.request_hash,
        repository=publication_request.repository,
        target_task_id=publication_request.target_task_id,
        accepted_execution_id=publication_request.accepted_execution_id,
        accepted_commit=publication_request.accepted_commit,
        acceptance_plan_hash=acceptance_plan.plan_hash,
        target_root=tmp_path,
        expected_remote=publication_request.expected_origin,
        head_branch=publication_request.head_branch,
        pr_number=7,
        pr_url="https://github.com/cyrneutron/harness-anything/pull/7",
        checks=[RequiredCheck(name="test", state="SUCCESS")],
        poll_attempts=1,
        observed_at="2026-01-01T00:00:00Z",
    )
    result = run_pipeline(
        baseline,
        hypothesis,
        candidate,
        acceptance_plan,
        acceptance_receipt,
        publication,
    )
    assert result.pipeline_plan.publication_receipt_hash == publication.receipt_hash
