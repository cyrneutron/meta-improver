from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.attribution import (
    AttributionError,
    AttributionHypothesis,
    BaselineObservation,
    CandidateChangeEvidence,
    rehydrate_baseline,
    rehydrate_candidate,
    rehydrate_hypothesis,
)


def baseline(**overrides: object) -> BaselineObservation:
    values: dict[str, object] = {
        "attempt_id": "attempt-1",
        "signal_signature": "signal-v1",
        "base_commit": "a" * 40,
        "passed": True,
        "command": "pytest tests/test_runner.py",
        "summary": "Baseline passed all checks.",
        "observed_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return BaselineObservation(**values)


def hypothesis(observation: BaselineObservation, **overrides: object) -> AttributionHypothesis:
    values: dict[str, object] = {
        "baseline_hash": observation.observation_hash,
        "category": "dependency",
        "confidence": 0.8,
        "root_cause": "The dependency contract changed.",
        "affected_paths": ["src/runner.py"],
        "model_version": "model-v1",
        "prompt_version": "prompt-v1",
    }
    values.update(overrides)
    return AttributionHypothesis(**values)


def test_hashes_are_deterministic_and_rehydrate() -> None:
    first = baseline()
    second = baseline()
    assert first.observation_hash == second.observation_hash
    assert rehydrate_baseline(first).model_dump() == first.model_dump()
    h = hypothesis(first)
    assert h.hypothesis_hash == hypothesis(first).hypothesis_hash
    assert rehydrate_hypothesis(h, first.observation_hash).model_dump() == h.model_dump()


def test_mutation_is_rejected_during_rehydration() -> None:
    observation = baseline()
    object.__setattr__(observation, "summary", "mutated")
    with pytest.raises(AttributionError):
        rehydrate_baseline(observation)


def test_rejects_unsafe_paths_and_secrets() -> None:
    with pytest.raises(ValidationError):
        baseline(command="pytest; curl https://example.invalid")
    with pytest.raises(ValidationError):
        baseline(summary="token: sk-test-secret")
    with pytest.raises(ValidationError):
        hypothesis(baseline(), affected_paths=["../escape.py"])
    with pytest.raises(ValidationError):
        hypothesis(baseline(), affected_paths=["/etc/passwd"])


def test_rejects_baseline_hash_mismatch() -> None:
    first = baseline()
    second = baseline(summary="A different baseline.")
    h = hypothesis(first)
    with pytest.raises(AttributionError):
        rehydrate_hypothesis(h, second.observation_hash)
    with pytest.raises(AttributionError):
        rehydrate_hypothesis(h)
    with pytest.raises(AttributionError):
        rehydrate_hypothesis(h, None)


def test_candidate_evidence_is_deterministic_and_bound() -> None:
    observation = baseline()
    h = hypothesis(observation)
    values = {
        "hypothesis_hash": h.hypothesis_hash,
        "baseline_hash": observation.observation_hash,
        "patch_hash": "sha256:" + "b" * 64,
        "targeted_tests": ["pytest tests/test_runner.py"],
        "regression_tests": ["pytest -q"],
        "residual_risk": "No known residual risk.",
    }
    first = CandidateChangeEvidence(**values)
    second = CandidateChangeEvidence(**values)
    assert first.evidence_hash == second.evidence_hash
    assert rehydrate_candidate(first, observation, h, values["patch_hash"]).model_dump() == first.model_dump()


def test_candidate_rejects_binding_mismatch_and_secrets() -> None:
    observation = baseline()
    h = hypothesis(observation)
    evidence = CandidateChangeEvidence(
        hypothesis_hash=h.hypothesis_hash,
        baseline_hash=observation.observation_hash,
        patch_hash="sha256:" + "b" * 64,
        residual_risk="Review required.",
    )
    with pytest.raises(AttributionError):
        rehydrate_candidate(evidence, baseline_hash="sha256:" + "c" * 64)
    with pytest.raises(AttributionError):
        rehydrate_candidate(evidence, hypothesis_hash="sha256:" + "d" * 64)
    with pytest.raises(AttributionError):
        rehydrate_candidate(evidence, patch_hash="sha256:" + "e" * 64)
    with pytest.raises(AttributionError):
        rehydrate_candidate(evidence, observation, h, None)
    with pytest.raises(AttributionError):
        rehydrate_candidate(evidence, observation, None, values_patch_hash := "sha256:" + "b" * 64)
    with pytest.raises(AttributionError):
        rehydrate_candidate(evidence, None, h, "sha256:" + "b" * 64)
    with pytest.raises(ValidationError):
        CandidateChangeEvidence(
            hypothesis_hash=h.hypothesis_hash,
            baseline_hash=observation.observation_hash,
            patch_hash="sha256:" + "b" * 64,
            residual_risk="token: sk-test-secret",
        )
