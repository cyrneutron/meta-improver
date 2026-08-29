"""Bounded and serializable contracts for improvement attempts."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_TEXT = 20_000
_SECRET_KEY = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|authorization)", re.I)
_SECRET_VALUE = re.compile(r"(?:bearer\s+|ghp_|github_pat_|sk-[A-Za-z0-9_-]+)", re.I)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def redact(value: Any) -> Any:
    """Redact secret-shaped mapping keys and common token-shaped strings."""
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _SECRET_KEY.search(str(key)) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        return _SECRET_VALUE.sub("[REDACTED]", value)
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="after")
    def redact_external_strings(self) -> ContractModel:
        """Apply redaction at the contract boundary before persistence."""
        for name in type(self).model_fields:
            value = getattr(self, name)
            redacted = redact(value)
            if redacted != value:
                setattr(self, name, redacted)
        return self


class AttemptStatus(StrEnum):
    PROPOSED = "proposed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class TestStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class InputSnapshot(ContractModel):
    source: Literal["ci", "issue", "local_log", "manual"]
    content: str = Field(min_length=1, max_length=MAX_TEXT)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("captured_at")
    @classmethod
    def captured_at_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("metadata", mode="after")
    @classmethod
    def metadata_is_redacted(cls, value: dict[str, Any]) -> dict[str, Any]:
        return redact(value)

    @model_validator(mode="after")
    def content_hash_matches(self) -> InputSnapshot:
        expected = hashlib.sha256(self.content.encode()).hexdigest()
        if self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match the redacted content")
        return self


class PatchFile(ContractModel):
    path: str = Field(min_length=1, max_length=500)
    patch: str = Field(min_length=1, max_length=MAX_TEXT)

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        if value.startswith(("/", "\\")) or ".." in value.split("/") or "\\" in value:
            raise ValueError("patch paths must be relative and cannot traverse directories")
        return value


class TestEvidence(ContractModel):
    # Prevent pytest from treating this domain model as a test class.
    __test__ = False

    command: str = Field(min_length=1, max_length=500)
    status: TestStatus
    output: str = Field(default="", max_length=MAX_TEXT)
    duration_seconds: float | None = Field(default=None, ge=0, le=86_400)
    failure_reason: str | None = Field(default=None, max_length=2_000)
    ran_at: datetime = Field(default_factory=utc_now)

    @field_validator("ran_at")
    @classmethod
    def ran_at_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def require_failure_reason(self) -> TestEvidence:
        if self.status is TestStatus.FAILED and not self.failure_reason:
            raise ValueError("failed tests require a failure_reason")
        return self


class ResidualRisk(ContractModel):
    description: str = Field(min_length=1, max_length=2_000)
    severity: Literal["low", "medium", "high", "critical"]
    mitigation: str | None = Field(default=None, max_length=2_000)


class MutationProposal(ContractModel):
    proposal_id: str = Field(min_length=1, max_length=100)
    base_commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    files: list[PatchFile] = Field(min_length=1, max_length=50)
    patch_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    model_version: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=100)
    tests: list[TestEvidence] = Field(default_factory=list, max_length=200)
    residual_risks: list[ResidualRisk] = Field(default_factory=list, max_length=100)
    rationale: str = Field(min_length=1, max_length=MAX_TEXT)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def created_at_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def derive_patch_hash(self) -> MutationProposal:
        payload = "\n".join(f"{item.path}\n{item.patch}" for item in self.files).encode()
        expected = "sha256:" + hashlib.sha256(payload).hexdigest()
        if self.patch_hash is not None and self.patch_hash != expected:
            raise ValueError("patch_hash does not match patch contents")
        self.patch_hash = expected
        return self


class Attempt(ContractModel):
    attempt_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=500)
    signal: str = Field(min_length=1, max_length=MAX_TEXT)
    base_commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    strategy_version: str = Field(min_length=1, max_length=100)
    input_snapshot: InputSnapshot
    proposal: MutationProposal | None = None
    status: AttemptStatus = AttemptStatus.PROPOSED
    model_version: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=100)
    test_evidence: list[TestEvidence] = Field(default_factory=list, max_length=200)
    failure_reason: str | None = Field(default=None, max_length=2_000)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def validate_state(self) -> Attempt:
        if self.status is AttemptStatus.FAILED and not self.failure_reason:
            raise ValueError("failed attempts require a failure_reason")
        return self
