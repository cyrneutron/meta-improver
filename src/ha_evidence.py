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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    observed_at: datetime
    evidence_hash: str | None = Field(default=None, pattern=_HASH.pattern)

    @field_validator("task_id", "execution_id", "squad_run_id")
    @classmethod
    def identities_are_safe(cls, value: str, info: Any) -> str:
        return _safe(value, info.field_name, _IDENTITY, 200)

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


__all__ = ["HAEvidenceArtifact", "HAEvidenceError", "HATargetEvidence", "rehydrate_ha_target_evidence"]
