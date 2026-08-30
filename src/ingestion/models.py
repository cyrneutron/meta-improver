"""Bounded input contracts and deterministic normalization for Phase 2A."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from src.models.contracts import ContractModel, MAX_TEXT, ensure_utc, redact, utc_now


class SignalSource(StrEnum):
    CI = "ci"
    ISSUE = "issue"
    LOCAL_LOG = "local_log"


class SignalEvent(ContractModel):
    """The common, sanitized representation consumed by the local pipeline."""

    source: SignalSource
    external_id: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=MAX_TEXT)
    metadata: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=utc_now)
    signature: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("observed_at")
    @classmethod
    def observed_at_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def derive_signature(self) -> SignalEvent:
        canonical = {
            "source": self.source.value,
            "external_id": self.external_id,
            "content": self.content,
            "metadata": self.metadata,
            "observed_at": self.observed_at.isoformat(),
        }
        digest = "sha256:" + hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.signature is not None and self.signature != digest:
            raise ValueError("signature does not match normalized signal")
        self.signature = digest
        return self


class CIRunSignal(ContractModel):
    run_id: str = Field(min_length=1, max_length=200)
    workflow: str = Field(min_length=1, max_length=200)
    status: Literal["queued", "in_progress", "completed"]
    conclusion: str = Field(default="", max_length=100)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    output: str = Field(max_length=MAX_TEXT)
    observed_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def observed_at_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class IssueSignal(ContractModel):
    issue_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(max_length=MAX_TEXT)
    state: Literal["open", "closed"] = "open"
    observed_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def observed_at_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class LocalLogSignal(ContractModel):
    log_id: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=500)
    level: Literal["debug", "info", "warning", "error"]
    content: str = Field(max_length=MAX_TEXT)
    observed_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def observed_at_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if value.startswith(("/", "\\")) or "\\" in value or ".." in value.split("/"):
            raise ValueError("log paths must be relative and cannot traverse directories")
        return value


def normalize_ci_run(value: CIRunSignal) -> SignalEvent:
    outcome = "success" if value.conclusion == "success" else "failure"
    return SignalEvent(
        source=SignalSource.CI,
        external_id=value.run_id,
        content=value.output,
        observed_at=value.observed_at,
        metadata={
            **value.metadata,
            "workflow": value.workflow,
            "status": value.status,
            "conclusion": value.conclusion,
            "outcome": outcome,
            "commit_sha": value.commit_sha,
        },
    )


def normalize_issue(value: IssueSignal) -> SignalEvent:
    content = value.title if not value.body else f"{value.title}\n\n{value.body}"
    return SignalEvent(
        source=SignalSource.ISSUE,
        external_id=value.issue_id,
        content=content,
        observed_at=value.observed_at,
        metadata={**value.metadata, "state": value.state, "title": value.title},
    )


def normalize_local_log(value: LocalLogSignal) -> SignalEvent:
    return SignalEvent(
        source=SignalSource.LOCAL_LOG,
        external_id=value.log_id,
        content=value.content,
        observed_at=value.observed_at,
        metadata={**value.metadata, "path": value.path, "level": value.level},
    )
