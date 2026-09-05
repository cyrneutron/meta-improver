from datetime import datetime, timezone

import pytest

from src.ha_evidence import HAEvidenceArtifact, HAEvidenceError, HATargetEvidence, rehydrate_ha_target_evidence


def _bundle() -> HATargetEvidence:
    baseline = "sha256:" + "a" * 64
    return HATargetEvidence(
        task_id="task-target-1",
        execution_id="execution-target-1",
        base_commit="a" * 40,
        squad_run_id="squad_run_1",
        artifacts=[HAEvidenceArtifact(kind="baseline", path="artifacts/baseline.json", digest=baseline)],
        baseline_hash=baseline,
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_evidence_is_hash_bound_and_replayable():
    bundle = _bundle()
    assert bundle.evidence_hash and rehydrate_ha_target_evidence(bundle) == bundle


def test_optional_artifact_hash_must_match_reference():
    values = _bundle().model_dump(by_alias=True, exclude={"artifacts", "evidence_hash"})
    values["diff_hash"] = "sha256:" + "c" * 64
    with pytest.raises(ValueError, match="diff artifact"):
        HATargetEvidence(
            **values,
            artifacts=[
                HAEvidenceArtifact(kind="baseline", path="artifacts/baseline.json", digest="sha256:" + "a" * 64),
                HAEvidenceArtifact(kind="diff", path="artifacts/diff.json", digest="sha256:" + "b" * 64),
            ],
        )


def test_artifact_paths_cannot_escape_canonical_root():
    with pytest.raises(ValueError):
        HAEvidenceArtifact(kind="baseline", path="../baseline.json", digest="sha256:" + "a" * 64)
