from datetime import datetime, timezone
import hashlib

from src.acceptance import (
    BaselineGateEvidence,
    QualityGateEvidence,
    ValidationGateEvidence,
    accept_candidate,
    plan_candidate_acceptance,
)
from src.attribution import AttributionHypothesis, BaselineObservation, CandidateChangeEvidence
from src.models import InputSnapshot
from src.pipeline import replay_pipeline, run_pipeline
from src.proposal import ProposalPayload, plan_proposal
from src.storage import Ledger


def test_fixture_improvement_reaches_proposal_and_replays(tmp_path):
    observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    baseline = BaselineObservation(
        attempt_id="attempt-fixture-e2e",
        signal_signature="signal-fixture-e2e",
        base_commit="a" * 40,
        passed=False,
        command="pytest tests/test_target.py",
        summary="fixture baseline failure",
        observed_at=observed_at,
    )
    hypothesis = AttributionHypothesis(
        baseline_hash=baseline.observation_hash,
        category="dependency",
        confidence=0.8,
        root_cause="fixture dependency contract changed",
        affected_paths=["src/target.py"],
        model_version="fixture-model",
        prompt_version="fixture-prompt",
    )
    candidate = CandidateChangeEvidence(
        hypothesis_hash=hypothesis.hypothesis_hash,
        baseline_hash=baseline.observation_hash,
        patch_hash="sha256:" + "b" * 64,
        targeted_tests=["pytest tests/test_target.py"],
        regression_tests=["pytest -q"],
        residual_risk="none known",
    )
    acceptance_plan = plan_candidate_acceptance(
        baseline,
        candidate,
        BaselineGateEvidence(
            baseline_hash=baseline.observation_hash,
            reproduction_command="pytest tests/test_target.py",
            reproduction_evidence="exit 1",
        ),
        ValidationGateEvidence(
            candidate_hash=candidate.evidence_hash,
            hypothesis_hash=hypothesis.hypothesis_hash,
            patch_hash=candidate.patch_hash,
            targeted_test_count=1,
            regression_test_count=1,
            targeted_evidence="passed",
            regression_evidence="passed",
        ),
        QualityGateEvidence(complexity_delta=0.0, cost_units=1, quality_evidence="passed"),
        hypothesis=hypothesis,
    )
    acceptance_receipt = accept_candidate(acceptance_plan)
    snapshot_content = "fixture failure"
    snapshot = InputSnapshot(
        source="manual",
        content=snapshot_content,
        content_sha256=hashlib.sha256(snapshot_content.encode()).hexdigest(),
        captured_at=observed_at,
    )

    ledger = Ledger(tmp_path / "ledger.sqlite")
    first = run_pipeline(
        baseline,
        hypothesis,
        candidate,
        acceptance_plan,
        acceptance_receipt,
        ledger=ledger,
        input_snapshot=snapshot,
        strategy_version="fixture-e2e-v1",
    )
    replay = replay_pipeline(
        baseline,
        hypothesis,
        candidate,
        acceptance_plan,
        acceptance_receipt,
        ledger=ledger,
        input_snapshot=snapshot,
        strategy_version="fixture-e2e-v1",
    )
    proposal = plan_proposal(
        ProposalPayload(
            repo="cyrneutron/harness-anything",
            head="mi/fixture-e2e",
            base="main",
            base_commit="a" * 40,
            patch_hash=candidate.patch_hash,
            acceptance_receipt_hash=acceptance_receipt.receipt_hash,
            changed_paths=["src/target.py"],
            title="fix: fixture e2e proposal",
            body="Evidence-backed fixture proposal",
        )
    )

    attempts = list(ledger.iter_attempts())
    events = list(ledger.iter_attempt_events(baseline.attempt_id))
    assert first == replay
    assert attempts[0].status.value == "succeeded"
    assert attempts[0].stage.value == "completed"
    assert len(events) == 6
    assert proposal.mode.value == "proposal_only"
