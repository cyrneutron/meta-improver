"""Pure proposal and explicit-approval contracts.

This module stops at the proposal boundary.  It creates a deterministic PR
payload and validates an explicitly supplied approval token, but it never
calls a repository, process, network, credential, or filesystem API.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic import BaseModel


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,499}$")
_SECRET = re.compile(
    r"(?:\bbearer\s+[A-Za-z0-9._~+/=-]+|(?<![A-Za-z0-9])"
    r"(?:ghp_|github_pat_|sk-|xox[baprs]-)[A-Za-z0-9_-]+|(?<![A-Za-z0-9])"
    r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|authorization)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+)",
    re.IGNORECASE,
)
_SHELL = re.compile(r"[;&|`$<>\r\n\x00]")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_text(value: str, field_name: str, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or _SECRET.search(value)
        or _SHELL.search(value)
    ):
        raise ValueError(f"{field_name} contains unsafe or secret-shaped text")
    return value


def _safe_identity(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or not _IDENTITY.fullmatch(value)
        or ".." in value
        or "//" in value
        or value.endswith(("/", ".", ":"))
        or _SECRET.search(value)
        or _SHELL.search(value)
    ):
        raise ValueError(f"{field_name} must be a safe non-empty identity")
    return value


def _safe_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not _PATH.fullmatch(value)
        or value.startswith(("/", "~"))
        or "\\" in value
        or "\x00" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or _SECRET.search(value)
    ):
        raise ValueError("changed_paths must contain safe relative paths")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


class ProposalError(ValueError):
    """Raised whenever a proposal or authorization cannot be proven safe."""


class _ProposalContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )


class ProposalMode(StrEnum):
    PROPOSAL_ONLY = "proposal_only"


class ProposalAction(StrEnum):
    PROPOSAL = "proposal"
    PUSH = "push"
    CREATE_PR = "create_pr"
    MERGE_MAIN = "merge_main"


class ApprovalScope(StrEnum):
    PROPOSAL = "proposal"
    PUSH = "push"
    CREATE_PR = "create_pr"
    MERGE_MAIN = "merge_main"


class ProposalPayload(_ProposalContract):
    """A bounded, hashable payload suitable for human review or later PR use."""

    schema_: Literal["proposal-payload/v1"] = Field(
        default="proposal-payload/v1", alias="schema", serialization_alias="schema"
    )
    repo: str = Field(min_length=1, max_length=200)
    head: str = Field(min_length=1, max_length=200)
    base: str = Field(min_length=1, max_length=200)
    base_commit: str = Field(pattern=_COMMIT)
    patch_hash: str = Field(pattern=_HASH)
    acceptance_receipt_hash: str = Field(pattern=_HASH)
    changed_paths: list[str] = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20_000)
    payload_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("repo", "head", "base")
    @classmethod
    def safe_identities(cls, value: str, info: Any) -> str:
        return _safe_identity(value, info.field_name)

    @field_validator("changed_paths")
    @classmethod
    def safe_changed_paths(cls, values: list[str]) -> list[str]:
        paths = [_safe_path(value) for value in values]
        if len(paths) != len(set(paths)):
            raise ValueError("changed_paths must be unique")
        return sorted(paths)

    @field_validator("title")
    @classmethod
    def safe_title(cls, value: str) -> str:
        return _safe_text(value, "title", 200)

    @field_validator("body")
    @classmethod
    def safe_body(cls, value: str) -> str:
        return _safe_text(value, "body", 20_000)

    @model_validator(mode="after")
    def derive_hash(self) -> ProposalPayload:
        expected = _digest(
            self.model_dump(mode="json", by_alias=True, exclude={"payload_hash"})
        )
        if self.payload_hash is not None and self.payload_hash != expected:
            raise ValueError("payload_hash does not match canonical contents")
        object.__setattr__(self, "payload_hash", expected)
        return self


class ProposalPlan(_ProposalContract):
    """A proposal-only plan; it carries no executable transport operation."""

    schema_: Literal["proposal-plan/v1"] = Field(
        default="proposal-plan/v1", alias="schema", serialization_alias="schema"
    )
    payload: ProposalPayload
    mode: Literal[ProposalMode.PROPOSAL_ONLY] = ProposalMode.PROPOSAL_ONLY
    plan_hash: str | None = Field(default=None, pattern=_HASH)

    @model_validator(mode="after")
    def derive_hash(self) -> ProposalPlan:
        expected = _digest(self.model_dump(mode="json", by_alias=True, exclude={"plan_hash"}))
        if self.plan_hash is not None and self.plan_hash != expected:
            raise ValueError("plan_hash does not match canonical contents")
        object.__setattr__(self, "plan_hash", expected)
        return self


class ApprovalToken(_ProposalContract):
    """An explicit, bounded authorization tied to exactly one proposal plan."""

    schema_: Literal["proposal-approval-token/v1"] = Field(
        default="proposal-approval-token/v1", alias="schema", serialization_alias="schema"
    )
    scope: ApprovalScope
    plan_hash: str = Field(pattern=_HASH)
    approved_at: datetime
    expires_at: datetime
    token_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("approved_at", "expires_at")
    @classmethod
    def timestamps_utc(cls, value: datetime, info: Any) -> datetime:
        return _utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_window_and_hash(self) -> ApprovalToken:
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must be after approval time")
        expected = _digest(
            self.model_dump(mode="json", by_alias=True, exclude={"token_hash"})
        )
        if self.token_hash is not None and self.token_hash != expected:
            raise ValueError("token_hash does not match canonical contents")
        object.__setattr__(self, "token_hash", expected)
        return self


def rehydrate_proposal_payload(payload: ProposalPayload) -> ProposalPayload:
    if not isinstance(payload, ProposalPayload) or payload.payload_hash is None:
        raise ProposalError("cannot use an unhashed proposal payload")
    existing = payload.payload_hash
    canonical = _canonical(payload.model_dump(mode="json", by_alias=True))
    try:
        hydrated = ProposalPayload.model_validate(json.loads(canonical))
    except Exception as exc:
        raise ProposalError("proposal payload failed integrity rehydration") from exc
    if hydrated.payload_hash != existing or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise ProposalError("proposal payload hash does not match canonical contents")
    return hydrated


def rehydrate_proposal_plan(plan: ProposalPlan) -> ProposalPlan:
    if not isinstance(plan, ProposalPlan) or plan.plan_hash is None:
        raise ProposalError("cannot use an unhashed proposal plan")
    existing = plan.plan_hash
    canonical = _canonical(plan.model_dump(mode="json", by_alias=True))
    try:
        hydrated = ProposalPlan.model_validate(json.loads(canonical))
    except Exception as exc:
        raise ProposalError("proposal plan failed integrity rehydration") from exc
    if hydrated.plan_hash != existing or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise ProposalError("proposal plan hash does not match canonical contents")
    return hydrated


def rehydrate_approval_token(token: ApprovalToken) -> ApprovalToken:
    if not isinstance(token, ApprovalToken) or token.token_hash is None:
        raise ProposalError("cannot use an unhashed approval token")
    existing = token.token_hash
    canonical = _canonical(token.model_dump(mode="json", by_alias=True))
    try:
        hydrated = ApprovalToken.model_validate(json.loads(canonical))
    except Exception as exc:
        raise ProposalError("approval token failed integrity rehydration") from exc
    if hydrated.token_hash != existing or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise ProposalError("approval token hash does not match canonical contents")
    return hydrated


def plan_proposal(payload: ProposalPayload) -> ProposalPlan:
    """Create a deterministic proposal-only plan from a validated payload."""

    try:
        return ProposalPlan(payload=rehydrate_proposal_payload(payload))
    except ProposalError:
        raise
    except Exception as exc:
        raise ProposalError("proposal payload was rejected") from exc


def approve_proposal(
    plan: ProposalPlan,
    scope: ApprovalScope | str = ApprovalScope.PROPOSAL,
    *,
    approved_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> ApprovalToken:
    """Create an explicit authorization token; this performs no external action."""

    hydrated = rehydrate_proposal_plan(plan)
    try:
        selected = ApprovalScope(scope)
    except Exception as exc:
        raise ProposalError("unknown approval scope") from exc
    if selected is ApprovalScope.MERGE_MAIN:
        raise ProposalError("merge_main is permanently forbidden")
    start = _utc(approved_at, "approved_at") if approved_at is not None else datetime.now(timezone.utc)
    end = _utc(expires_at, "expires_at") if expires_at is not None else start + timedelta(hours=1)
    try:
        return ApprovalToken(
            scope=selected,
            plan_hash=hydrated.plan_hash or "",
            approved_at=start,
            expires_at=end,
        )
    except Exception as exc:
        raise ProposalError("approval token was rejected") from exc


def authorize_action(
    plan: ProposalPlan,
    action: ProposalAction | ApprovalScope | str,
    token: ApprovalToken | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return true only for a permitted action; never execute that action."""

    hydrated_plan = rehydrate_proposal_plan(plan)
    try:
        selected = ProposalAction(action)
    except Exception as exc:
        raise ProposalError("unknown action is rejected") from exc
    if selected is ProposalAction.MERGE_MAIN:
        raise ProposalError("merge_main is permanently forbidden")
    if selected is ProposalAction.PROPOSAL:
        return True
    if token is None:
        raise ProposalError(f"{selected.value} requires explicit approval")
    hydrated_token = rehydrate_approval_token(token)
    if hydrated_token.scope.value != selected.value:
        raise ProposalError("approval scope does not match action")
    if hydrated_token.plan_hash != hydrated_plan.plan_hash:
        raise ProposalError("approval token is bound to a different proposal plan")
    current = _utc(now, "now") if now is not None else datetime.now(timezone.utc)
    if not (hydrated_token.approved_at <= current < hydrated_token.expires_at):
        raise ProposalError("approval token is expired or not yet valid")
    return True


# Naming parallels the rest of the repository's planner/rehydration APIs.
plan_pr_proposal = plan_proposal
rehydrate_plan = rehydrate_proposal_plan
rehydrate_payload = rehydrate_proposal_payload


__all__ = [
    "ApprovalScope",
    "ApprovalToken",
    "ProposalAction",
    "ProposalError",
    "ProposalMode",
    "ProposalPayload",
    "ProposalPlan",
    "approve_proposal",
    "authorize_action",
    "plan_proposal",
    "plan_pr_proposal",
    "rehydrate_approval_token",
    "rehydrate_payload",
    "rehydrate_plan",
    "rehydrate_proposal_payload",
    "rehydrate_proposal_plan",
]
