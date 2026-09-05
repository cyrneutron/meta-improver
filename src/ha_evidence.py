"""Bounded, hash-bound evidence contract for HA target improvements.

This module deliberately contains no provider, filesystem, network, or process
access.  Adapters may translate canonical HA artifacts into this contract;
pipeline code then consumes the verified bundle without trusting raw text.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.attribution import AttributionHypothesis, BaselineObservation, CandidateChangeEvidence
from src.proposal import ProposalPayload


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,499}$")
_CONTROL = re.compile(r"[;&|`$<>\r\n\x00]")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _safe(value: str, field: str, pattern: re.Pattern[str], limit: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or pattern.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid or unbounded")
    return value


class HAEvidenceError(ValueError):
    """Raised when an HA evidence bundle cannot be trusted."""


class HAEvidenceArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["baseline", "squad_run", "diff", "validation", "container", "acceptance"]
    path: str
    digest: str = Field(pattern=_HASH.pattern)

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        if not isinstance(value, str) or _PATH.fullmatch(value) is None or value.startswith(("/", "~")) or ".." in value:
            raise ValueError("artifact path must be a safe relative path")
        return value


class HATargetEvidence(BaseModel):
    """Canonical identity and evidence references for one target improvement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal["ha-target-evidence/v1"] = Field(
        default="ha-target-evidence/v1", alias="schema", serialization_alias="schema"
    )
    task_id: str
    execution_id: str
    base_commit: str = Field(pattern=_COMMIT.pattern)
    candidate_commit: str | None = Field(default=None, pattern=_COMMIT.pattern)
    squad_run_id: str
    artifacts: list[HAEvidenceArtifact] = Field(min_length=1, max_length=200)
    baseline_hash: str = Field(pattern=_HASH.pattern)
    diff_hash: str | None = Field(default=None, pattern=_HASH.pattern)
    validation_hash: str | None = Field(default=None, pattern=_HASH.pattern)
    container_hash: str | None = Field(default=None, pattern=_HASH.pattern)
    acceptance_hash: str | None = Field(default=None, pattern=_HASH.pattern)
    targeted_tests: list[str] = Field(default_factory=list, max_length=200)
    regression_tests: list[str] = Field(default_factory=list, max_length=200)
    model_version: str = "ha-evidence-model"
    prompt_version: str = "ha-evidence-prompt"
    signal: str = "ha-target-evidence"
    summary: str = "HA target improvement evidence"
    observed_at: datetime
    evidence_hash: str | None = Field(default=None, pattern=_HASH.pattern)

    @field_validator("task_id", "execution_id", "squad_run_id")
    @classmethod
    def identities_are_safe(cls, value: str, info: Any) -> str:
        return _safe(value, info.field_name, _IDENTITY, 200)

    @field_validator("signal", "summary", "model_version", "prompt_version")
    @classmethod
    def bounded_text(cls, value: str, info: Any) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 2_000 or _CONTROL.search(value):
            raise ValueError(f"{info.field_name} is invalid")
        return value

    @field_validator("targeted_tests", "regression_tests")
    @classmethod
    def bounded_tests(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() or len(value) > 500 or _CONTROL.search(value) for value in values):
            raise ValueError("test commands are invalid")
        return values

    @field_validator("observed_at")
    @classmethod
    def observed_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_bindings_and_hash(self) -> HATargetEvidence:
        by_kind = {item.kind: item.digest for item in self.artifacts}
        if by_kind.get("baseline") != self.baseline_hash:
            raise ValueError("baseline artifact is not bound to baseline_hash")
        for kind, field in (("diff", "diff_hash"), ("validation", "validation_hash"), ("container", "container_hash"), ("acceptance", "acceptance_hash")):
            value = getattr(self, field)
            if value is not None and by_kind.get(kind) != value:
                raise ValueError(f"{kind} artifact is not bound to {field}")
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"evidence_hash"}))
        if self.evidence_hash is not None and self.evidence_hash != expected:
            raise ValueError("evidence_hash does not match canonical contents")
        object.__setattr__(self, "evidence_hash", expected)
        return self


def _manifest_path(repo_root: str | Path, relative: str) -> Path:
    root = Path(repo_root).resolve()
    if not isinstance(relative, str) or not relative.startswith("harness/") or relative.startswith(("/", "~")):
        raise HAEvidenceError("evidence manifest must be a relative harness path")
    path = (root / relative).resolve()
    harness = (root / "harness").resolve()
    try:
        path.relative_to(harness)
    except ValueError as exc:
        raise HAEvidenceError("evidence manifest escapes canonical harness") from exc
    if path.is_symlink() or not path.is_file():
        raise HAEvidenceError("evidence manifest is missing or not regular")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HAEvidenceError("evidence manifest cannot be read") from exc
    if len(raw.encode("utf-8")) > 200_000:
        raise HAEvidenceError("evidence manifest exceeds bounded size")
    return path


def read_ha_target_evidence(
    repo_root: str | Path,
    manifest_path: str,
) -> HATargetEvidence:
    """Read one hash-bound evidence manifest from canonical ``harness/`` only."""

    path = _manifest_path(repo_root, manifest_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HAEvidenceError("evidence manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HAEvidenceError("evidence manifest must be a JSON object")
    try:
        return rehydrate_ha_target_evidence(HATargetEvidence.model_validate(payload))
    except Exception as exc:
        if isinstance(exc, HAEvidenceError):
            raise
        raise HAEvidenceError("evidence manifest failed contract validation") from exc


def rehydrate_ha_target_evidence(value: HATargetEvidence) -> HATargetEvidence:
    if value.evidence_hash is None:
        raise HAEvidenceError("cannot use unhashed HA target evidence")
    try:
        hydrated = HATargetEvidence.model_validate(json.loads(_canonical(value.model_dump(mode="json", by_alias=True))))
    except Exception as exc:
        raise HAEvidenceError("HA target evidence failed integrity validation") from exc
    if hydrated.evidence_hash != value.evidence_hash:
        raise HAEvidenceError("HA target evidence hash does not match canonical contents")
    return hydrated


def evidence_to_attribution(
    evidence: HATargetEvidence,
) -> tuple[BaselineObservation, AttributionHypothesis, CandidateChangeEvidence]:
    """Map verified HA identities into MI attribution contracts."""

    value = rehydrate_ha_target_evidence(evidence)
    baseline = BaselineObservation(
        attempt_id=value.execution_id,
        signal_signature=value.signal,
        base_commit=value.base_commit,
        passed=False,
        command=value.targeted_tests[0] if value.targeted_tests else "ha target baseline",
        summary=value.summary,
        observed_at=value.observed_at,
    )
    hypothesis = AttributionHypothesis(
        baseline_hash=baseline.observation_hash,
        category="ha-target",
        confidence=1.0,
        root_cause="HA evidence bundle requires candidate validation",
        affected_paths=[artifact.path for artifact in value.artifacts if artifact.kind == "diff"],
        model_version=value.model_version,
        prompt_version=value.prompt_version,
    )
    candidate = CandidateChangeEvidence(
        hypothesis_hash=hypothesis.hypothesis_hash,
        baseline_hash=baseline.observation_hash,
        patch_hash=value.diff_hash or value.baseline_hash,
        targeted_tests=value.targeted_tests or ["ha target targeted validation"],
        regression_tests=value.regression_tests or ["ha target regression validation"],
        residual_risk="HA evidence bundle supplied by canonical target artifacts",
    )
    return baseline, hypothesis, candidate


def evidence_to_proposal(
    evidence: HATargetEvidence,
    *,
    repository: str,
    head: str,
    title: str,
    body: str,
    acceptance_receipt_hash: str,
) -> ProposalPayload:
    """Compose a proposal payload from verified HA evidence and acceptance."""

    value = rehydrate_ha_target_evidence(evidence)
    if value.candidate_commit is None or value.diff_hash is None or value.acceptance_hash is None:
        raise HAEvidenceError("proposal requires candidate commit, diff, and acceptance evidence")
    paths = sorted({artifact.path for artifact in value.artifacts if artifact.kind == "diff"})
    if not paths:
        raise HAEvidenceError("proposal requires at least one diff artifact path")
    try:
        return ProposalPayload(
            repo=repository,
            head=head,
            base="main",
            base_commit=value.base_commit,
            patch_hash=value.diff_hash,
            acceptance_receipt_hash=acceptance_receipt_hash,
            changed_paths=paths,
            title=title,
            body=body,
        )
    except Exception as exc:
        raise HAEvidenceError("HA evidence could not compose a proposal payload") from exc


def run_ha_evidence_pipeline(
    evidence: HATargetEvidence,
    acceptance_plan: Any,
    acceptance_receipt: Any,
    *,
    ledger: Any,
    input_snapshot: Any,
    strategy_version: str = "ha-evidence-v1",
) -> Any:
    """Persist a verified HA bundle through MI's existing pipeline stages."""

    from src.pipeline import PipelineError, run_pipeline

    value = rehydrate_ha_target_evidence(evidence)
    baseline, hypothesis, candidate = evidence_to_attribution(value)
    try:
        return run_pipeline(
            baseline,
            hypothesis,
            candidate,
            acceptance_plan,
            acceptance_receipt,
            ledger=ledger,
            input_snapshot=input_snapshot,
            strategy_version=strategy_version,
        )
    except PipelineError as exc:
        raise HAEvidenceError(f"HA evidence pipeline rejected at {exc.stage.value}") from exc


__all__ = [
    "HAEvidenceArtifact",
    "HAEvidenceError",
    "HATargetEvidence",
    "read_ha_target_evidence",
    "evidence_to_attribution",
    "evidence_to_proposal",
    "run_ha_evidence_pipeline",
    "rehydrate_ha_target_evidence",
]
