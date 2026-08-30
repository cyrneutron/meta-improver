"""Hash-bound, fail-closed contracts for Phase 4A attribution."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SECRET = re.compile(r"(?:\bbearer\s+[A-Za-z0-9._~+/=-]+|(?<![A-Za-z0-9])(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]+|(?<![A-Za-z0-9])(?:password|passwd|token|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+)", re.I)
_SHELL = re.compile(r"[;&|`$<>\r\n\x00]")

def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()

def _safe_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or _SECRET.search(value) or _SHELL.search(value):
        raise ValueError(f"{name} contains unsafe or secret-shaped text")
    return value

def _safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or value.startswith(("/", "~")) or "\\" in value or "\x00" in value or re.match(r"^[A-Za-z]:", value):
        raise ValueError("affected_paths must contain safe relative paths")
    if any(part in ("", ".", "..") for part in value.split("/")) or _SECRET.search(value):
        raise ValueError("affected_paths must contain safe relative paths")
    return value

class AttributionError(ValueError):
    """Raised when an attribution contract cannot be safely rehydrated."""

class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)

class BaselineObservation(_Contract):
    attempt_id: str = Field(min_length=1, max_length=200)
    signal_signature: str = Field(min_length=1, max_length=500)
    base_commit: str = Field(pattern=_COMMIT)
    passed: bool
    command: str = Field(min_length=1, max_length=20_000)
    summary: str = Field(min_length=1, max_length=20_000)
    observed_at: datetime
    observation_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("attempt_id", "signal_signature")
    @classmethod
    def identifiers(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value) or _SECRET.search(value):
            raise ValueError("identifier is unsafe")
        return value

    @field_validator("command", "summary")
    @classmethod
    def text(cls, value: str, info: Any) -> str:
        return _safe_text(value, info.field_name)

    @field_validator("observed_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def derive_hash(self) -> "BaselineObservation":
        expected = _hash(self.model_dump(mode="json", exclude={"observation_hash"}))
        if self.observation_hash is not None and self.observation_hash != expected:
            raise ValueError("observation_hash does not match canonical contents")
        object.__setattr__(self, "observation_hash", expected)
        return self

class AttributionHypothesis(_Contract):
    baseline_hash: str = Field(pattern=_HASH)
    category: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0, le=1)
    root_cause: str = Field(min_length=1, max_length=20_000)
    affected_paths: list[str] = Field(default_factory=list, max_length=200)
    model_version: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=200)
    hypothesis_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("category", "model_version", "prompt_version")
    @classmethod
    def version_identifiers(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value) or _SECRET.search(value):
            raise ValueError("identifier is unsafe")
        return value

    @field_validator("root_cause")
    @classmethod
    def cause_text(cls, value: str) -> str:
        return _safe_text(value, "root_cause")

    @field_validator("affected_paths")
    @classmethod
    def paths(cls, value: list[str]) -> list[str]:
        paths = [_safe_path(item) for item in value]
        if len(paths) != len(set(paths)):
            raise ValueError("affected_paths must be unique")
        return sorted(paths)

    @model_validator(mode="after")
    def derive_hash(self) -> "AttributionHypothesis":
        expected = _hash(self.model_dump(mode="json", exclude={"hypothesis_hash"}))
        if self.hypothesis_hash is not None and self.hypothesis_hash != expected:
            raise ValueError("hypothesis_hash does not match canonical contents")
        object.__setattr__(self, "hypothesis_hash", expected)
        return self


class CandidateChangeEvidence(_Contract):
    hypothesis_hash: str = Field(pattern=_HASH)
    baseline_hash: str = Field(pattern=_HASH)
    patch_hash: str = Field(pattern=_HASH)
    targeted_tests: list[str] = Field(default_factory=list, max_length=200)
    regression_tests: list[str] = Field(default_factory=list, max_length=200)
    residual_risk: str = Field(min_length=1, max_length=20_000)
    evidence_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("targeted_tests", "regression_tests")
    @classmethod
    def test_commands(cls, values: list[str], info: Any) -> list[str]:
        return [_safe_text(value, info.field_name) for value in values]

    @field_validator("residual_risk")
    @classmethod
    def risk_text(cls, value: str) -> str:
        return _safe_text(value, "residual_risk")

    @model_validator(mode="after")
    def derive_hash(self) -> "CandidateChangeEvidence":
        expected = _hash(self.model_dump(mode="json", exclude={"evidence_hash"}))
        if self.evidence_hash is not None and self.evidence_hash != expected:
            raise ValueError("evidence_hash does not match canonical contents")
        object.__setattr__(self, "evidence_hash", expected)
        return self

def _rehydrate(value: Any, model: type[_Contract], hash_field: str) -> Any:
    if not isinstance(value, model) or not getattr(value, hash_field, None):
        raise AttributionError("unhashed evidence is rejected")
    canonical = _canonical(value.model_dump(mode="json"))
    try:
        hydrated = model.model_validate(json.loads(canonical))
    except Exception as exc:
        raise AttributionError("evidence failed integrity rehydration") from exc
    if getattr(hydrated, hash_field) != getattr(value, hash_field) or _canonical(hydrated.model_dump(mode="json")) != canonical:
        raise AttributionError("evidence hash does not match canonical contents")
    return hydrated

def rehydrate_baseline(observation: BaselineObservation) -> BaselineObservation:
    return _rehydrate(observation, BaselineObservation, "observation_hash")

def rehydrate_hypothesis(hypothesis: AttributionHypothesis, baseline_hash: str | BaselineObservation | None = None) -> AttributionHypothesis:
    hydrated = _rehydrate(hypothesis, AttributionHypothesis, "hypothesis_hash")
    expected = baseline_hash.observation_hash if isinstance(baseline_hash, BaselineObservation) else baseline_hash
    if expected is None:
        raise AttributionError("hypothesis baseline binding is required")
    if hydrated.baseline_hash != expected:
        raise AttributionError("hypothesis baseline hash does not match")
    return hydrated


def rehydrate_candidate(
    evidence: CandidateChangeEvidence,
    baseline_hash: str | BaselineObservation | None = None,
    hypothesis_hash: str | AttributionHypothesis | None = None,
    patch_hash: str | None = None,
) -> CandidateChangeEvidence:
    hydrated = _rehydrate(evidence, CandidateChangeEvidence, "evidence_hash")
    expected_baseline = baseline_hash.observation_hash if isinstance(baseline_hash, BaselineObservation) else baseline_hash
    expected_hypothesis = hypothesis_hash.hypothesis_hash if isinstance(hypothesis_hash, AttributionHypothesis) else hypothesis_hash
    if expected_baseline is None or expected_hypothesis is None or patch_hash is None:
        raise AttributionError("candidate baseline, hypothesis, and patch bindings are required")
    if hydrated.baseline_hash != expected_baseline:
        raise AttributionError("candidate baseline hash does not match")
    if hydrated.hypothesis_hash != expected_hypothesis:
        raise AttributionError("candidate hypothesis hash does not match")
    if hydrated.patch_hash != patch_hash:
        raise AttributionError("candidate patch hash does not match")
    return hydrated

rehydrate_baseline_observation = rehydrate_baseline
rehydrate_attribution_hypothesis = rehydrate_hypothesis
rehydrate_candidate_change_evidence = rehydrate_candidate

__all__ = ["AttributionError", "BaselineObservation", "AttributionHypothesis", "CandidateChangeEvidence", "rehydrate_baseline", "rehydrate_hypothesis", "rehydrate_candidate", "rehydrate_baseline_observation", "rehydrate_attribution_hypothesis", "rehydrate_candidate_change_evidence"]
