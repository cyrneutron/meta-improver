"""Pure proposal and explicit-approval contracts.

This module stops at the proposal boundary.  It creates a deterministic PR
payload and validates an explicitly supplied approval token, but it never
calls a repository, process, network, credential, or filesystem API.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import AliasChoices, ConfigDict, Field, field_validator, model_validator
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


def _resolve_identity_aliases(
    *,
    actor: str | None = None,
    identity: str | None = None,
    approver: str | None = None,
    approver_identity: str | None = None,
    approver_id: str | None = None,
    approved_by: str | None = None,
) -> str | None:
    """Resolve the supported identity spellings without accepting ambiguity."""

    supplied = [
        value
        for value in (
            actor,
            identity,
            approver,
            approver_identity,
            approver_id,
            approved_by,
        )
        if value is not None
    ]
    if not supplied:
        return None
    if len(set(supplied)) != 1:
        raise ProposalError("approval identity aliases conflict")
    try:
        return _safe_identity(supplied[0], "actor")
    except ValueError as exc:
        raise ProposalError("approval identity was rejected") from exc


def _validate_optional_digest(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ProposalError(f"{field_name} must be a sha256 digest")
    return value


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
    """An explicit authorization tied to one plan and complete consent."""

    schema_: Literal["proposal-approval-token/v1"] = Field(
        default="proposal-approval-token/v1", alias="schema", serialization_alias="schema"
    )
    scope: ApprovalScope
    plan_hash: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices("plan_hash", "plan_digest", "planHash"),
    )
    actor: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices(
            "actor",
            "identity",
            "approver",
            "approver_identity",
            "approver_id",
            "approved_by",
            "approvedBy",
            "approverIdentity",
        ),
    )
    reviewer_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("reviewer_id", "reviewerId"),
    )
    review_digest: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices("review_digest", "reviewDigest"),
    )
    content_digest: str = Field(
        pattern=_HASH,
        validation_alias=AliasChoices("content_digest", "contentDigest"),
    )
    approved_at: datetime = Field(
        validation_alias=AliasChoices("approved_at", "approvedAt")
    )
    expires_at: datetime = Field(
        validation_alias=AliasChoices("expires_at", "expiresAt")
    )
    token_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("actor")
    @classmethod
    def safe_actor(cls, value: str) -> str:
        return _safe_identity(value, "actor")

    @field_validator("reviewer_id")
    @classmethod
    def safe_reviewer(cls, value: str | None) -> str | None:
        return None if value is None else _safe_identity(value, "reviewer_id")

    @field_validator("approved_at", "expires_at")
    @classmethod
    def timestamps_utc(cls, value: datetime, info: Any) -> datetime:
        return _utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_window_and_hash(self) -> ApprovalToken:
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must be after approval time")
        if self.reviewer_id is not None and self.reviewer_id == self.actor:
            raise ValueError("approval requires an independent reviewer")
        expected = _digest(
            self.model_dump(mode="json", by_alias=True, exclude={"token_hash"})
        )
        if self.token_hash is not None and self.token_hash != expected:
            raise ValueError("token_hash does not match canonical contents")
        object.__setattr__(self, "token_hash", expected)
        return self

    @property
    def identity(self) -> str:
        """Compatibility spelling for callers that use identity terminology."""

        return self.actor

    @property
    def approver_identity(self) -> str:
        """Canonical consent identity under the descriptive spelling."""

        return self.actor

    @property
    def plan_digest(self) -> str:
        """Compatibility spelling for the hash-bound plan identity."""

        return self.plan_hash


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
    actor: str | None = None,
    identity: str | None = None,
    approver: str | None = None,
    approver_identity: str | None = None,
    approver_id: str | None = None,
    approved_by: str | None = None,
    reviewer_id: str | None = None,
    review_digest: str | None = None,
    content_digest: str | None = None,
    plan_digest: str | None = None,
    plan_hash: str | None = None,
    approved_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> ApprovalToken:
    """Create an explicit authorization token; this performs no external action.

    Every token carries the approver, review, plan, and content digests.  The
    plan and content digests are checked against the hydrated proposal before
    the token is issued, so an approval cannot be moved to another payload.
    """

    hydrated = rehydrate_proposal_plan(plan)
    try:
        selected = ApprovalScope(scope)
    except Exception as exc:
        raise ProposalError("unknown approval scope") from exc
    if selected is ApprovalScope.MERGE_MAIN:
        raise ProposalError("merge_main is permanently forbidden")
    resolved_actor = _resolve_identity_aliases(
        actor=actor,
        identity=identity,
        approver=approver,
        approver_identity=approver_identity,
        approver_id=approver_id,
        approved_by=approved_by,
    )
    if resolved_actor is None:
        raise ProposalError("approval requires approver identity")
    if review_digest is None:
        raise ProposalError("approval requires review_digest")
    review_digest = _validate_optional_digest(review_digest, "review_digest")
    if content_digest is None:
        raise ProposalError("approval requires content_digest")
    content_digest = _validate_optional_digest(content_digest, "content_digest")
    supplied_plan_digests = [value for value in (plan_digest, plan_hash) if value is not None]
    if len(set(supplied_plan_digests)) > 1:
        raise ProposalError("plan digest aliases conflict")
    supplied_plan_digest = _validate_optional_digest(
        supplied_plan_digests[0] if supplied_plan_digests else None,
        "plan_digest",
    )
    expected_plan_digest = hydrated.plan_hash or ""
    if supplied_plan_digest is not None and supplied_plan_digest != expected_plan_digest:
        raise ProposalError("plan_digest does not match proposal plan")
    if content_digest != hydrated.payload.payload_hash:
        raise ProposalError("content_digest does not match proposal payload")
    if expires_at is None:
        raise ProposalError("approval requires expires_at")
    start = _utc(approved_at, "approved_at") if approved_at is not None else datetime.now(timezone.utc)
    end = _utc(expires_at, "expires_at")
    try:
        return ApprovalToken(
            scope=selected,
            plan_hash=hydrated.plan_hash or "",
            actor=resolved_actor,
            reviewer_id=reviewer_id,
            review_digest=review_digest,
            content_digest=content_digest,
            approved_at=start,
            expires_at=end,
        )
    except Exception as exc:
        if "independent reviewer" in str(exc):
            raise ProposalError("approval requires an independent reviewer") from exc
        raise ProposalError("approval token was rejected") from exc


def authorize_action(
    plan: ProposalPlan,
    action: ProposalAction | ApprovalScope | str,
    token: ApprovalToken | None = None,
    *,
    now: datetime | None = None,
    actor: str | None = None,
    identity: str | None = None,
    approver: str | None = None,
    approver_identity: str | None = None,
    approver_id: str | None = None,
    approved_by: str | None = None,
    review_digest: str | None = None,
    content_digest: str | None = None,
    plan_digest: str | None = None,
    plan_hash: str | None = None,
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
    if hydrated_token.content_digest != hydrated_plan.payload.payload_hash:
        raise ProposalError("approval content_digest does not match proposal payload")
    resolved_actor = _resolve_identity_aliases(
        actor=actor,
        identity=identity,
        approver=approver,
        approver_identity=approver_identity,
        approver_id=approver_id,
        approved_by=approved_by,
    )
    if resolved_actor is not None and hydrated_token.actor != resolved_actor:
        raise ProposalError("approval approver identity does not match caller")
    if review_digest is not None:
        expected_review_digest = _validate_optional_digest(review_digest, "review_digest")
        if hydrated_token.review_digest != expected_review_digest:
            raise ProposalError("approval review_digest does not match consent")
    supplied_plan_digests = [value for value in (plan_digest, plan_hash) if value is not None]
    if len(set(supplied_plan_digests)) > 1:
        raise ProposalError("plan digest aliases conflict")
    if supplied_plan_digests:
        expected_plan_digest = _validate_optional_digest(
            supplied_plan_digests[0], "plan_digest"
        )
        if hydrated_token.plan_hash != expected_plan_digest:
            raise ProposalError("approval plan_digest does not match proposal plan")
    if content_digest is not None:
        expected_content_digest = _validate_optional_digest(content_digest, "content_digest")
        if hydrated_token.content_digest != expected_content_digest:
            raise ProposalError("approval content_digest does not match consent")
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
