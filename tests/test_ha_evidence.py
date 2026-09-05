from datetime import datetime, timezone

import pytest

from src.ha_evidence import HAEvidenceArtifact, HAEvidenceError, HATargetEvidence, evidence_to_attribution, evidence_to_proposal, read_ha_target_evidence, rehydrate_ha_target_evidence


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


def test_reader_accepts_only_canonical_harness_manifest(tmp_path):
    manifest = tmp_path / "harness" / "tasks" / "task-1" / "artifacts" / "evidence.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(_bundle().model_dump_json(by_alias=True), encoding="utf-8")
    assert read_ha_target_evidence(tmp_path, "harness/tasks/task-1/artifacts/evidence.json") == _bundle()


def test_reader_rejects_projection_and_escape(tmp_path):
    with pytest.raises(HAEvidenceError):
        read_ha_target_evidence(tmp_path, ".harness/evidence.json")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(HAEvidenceError):
        read_ha_target_evidence(tmp_path, "harness/../outside.json")


def test_verified_evidence_maps_to_attribution_contracts():
    baseline, hypothesis, candidate = evidence_to_attribution(_bundle())
    assert baseline.base_commit == "a" * 40
    assert hypothesis.baseline_hash == baseline.observation_hash
    assert candidate.hypothesis_hash == hypothesis.hypothesis_hash


def test_verified_evidence_composes_proposal_only_payload():
    baseline = "sha256:" + "a" * 64
    diff = "sha256:" + "b" * 64
    acceptance = "sha256:" + "c" * 64
    evidence = HATargetEvidence(
        task_id="task-target-1",
        execution_id="execution-target-1",
        base_commit="a" * 40,
        candidate_commit="b" * 40,
        squad_run_id="squad_run_1",
        artifacts=[
            HAEvidenceArtifact(kind="baseline", path="artifacts/baseline.json", digest=baseline),
            HAEvidenceArtifact(kind="diff", path="artifacts/diff.json", digest=diff),
            HAEvidenceArtifact(kind="acceptance", path="artifacts/acceptance.json", digest=acceptance),
        ],
        baseline_hash=baseline,
        diff_hash=diff,
        acceptance_hash=acceptance,
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    proposal = evidence_to_proposal(
        evidence,
        repository="cyrneutron/harness-anything",
        head="mi/task-target-1",
        title="fix: target proposal",
        body="Evidence-backed target proposal",
        acceptance_receipt_hash=acceptance,
    )
    assert proposal.base_commit == evidence.base_commit
    assert proposal.patch_hash == diff
    assert proposal.changed_paths == ["artifacts/diff.json"]
