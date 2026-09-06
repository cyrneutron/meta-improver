"""Pure in-memory dispatch contracts for the proposal boundary.

The scheduler records intent and admission state only.  It never performs a
transport operation, invokes a process, or reads host state.  A later worker
may consume the receipts, but this module deliberately stops at a bounded,
hash-bound queue decision.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic import BaseModel

from src.proposal import (
    ApprovalScope,
    ApprovalToken,
    ProposalAction,
    ProposalPlan,
    ProposalError,
    authorize_action,
    rehydrate_approval_token,
    rehydrate_proposal_plan,
)


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
_SECRET = re.compile(
    r"(?:\bbearer\s+[A-Za-z0-9._~+/=-]+|(?<![A-Za-z0-9])"
    r"(?:ghp_|github_pat_|sk-|xox[baprs]-)[A-Za-z0-9_-]+|(?<![A-Za-z0-9])"
    r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|authorization)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+)",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[;&|`$<>\r\n\x00]")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _safe_identity(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or not _IDENTITY.fullmatch(value)
        or ".." in value
        or "//" in value
        or value.endswith(("/", ".", ":"))
        or _SECRET.search(value)
        or _CONTROL.search(value)
    ):
        raise ValueError(f"{field_name} must be a safe identity")
    return value


class SchedulerError(ValueError):
    """Raised when a dispatch contract cannot be safely used."""


class SchedulerPersistenceError(SchedulerError):
    """Raised when the durable dispatch journal cannot be trusted."""


class _SchedulerContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )


class DispatchAction(StrEnum):
    PROPOSAL = "proposal"
    PUSH = "push"
    CREATE_PR = "create_pr"
    MERGE_MAIN = "merge_main"


class DispatchStatus(StrEnum):
    ACCEPTED = "accepted"
    DEDUPLICATED = "deduplicated"
    LOCKED = "locked"
    BACKOFF = "backoff"
    CIRCUIT_OPEN = "circuit_open"
    REJECTED = "rejected"


class DispatchRequest(_SchedulerContract):
    """The complete, bounded identity of one proposal dispatch attempt."""

    schema_: Literal["dispatch-request/v1"] = Field(
        default="dispatch-request/v1", alias="schema", serialization_alias="schema"
    )
    idempotency_key: str = Field(min_length=1, max_length=200)
    event_id: str = Field(min_length=1, max_length=200)
    proposal_plan_hash: str = Field(pattern=_HASH)
    action: DispatchAction
    attempt: int = Field(ge=1, le=1_000)
    request_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("idempotency_key", "event_id")
    @classmethod
    def safe_keys(cls, value: str, info: Any) -> str:
        return _safe_identity(value, info.field_name)

    @model_validator(mode="after")
    def derive_hash(self) -> DispatchRequest:
        expected = _digest(
            self.model_dump(mode="json", by_alias=True, exclude={"request_hash"})
        )
        if self.request_hash is not None and self.request_hash != expected:
            raise ValueError("request_hash does not match canonical contents")
        object.__setattr__(self, "request_hash", expected)
        return self


class DispatchState(_SchedulerContract):
    """Hash-bound scheduler state for one request identity."""

    schema_: Literal["dispatch-state/v1"] = Field(
        default="dispatch-state/v1", alias="schema", serialization_alias="schema"
    )
    idempotency_key: str = Field(min_length=1, max_length=200)
    event_id: str = Field(min_length=1, max_length=200)
    request_hash: str = Field(pattern=_HASH)
    status: DispatchStatus
    attempt: int = Field(ge=1, le=1_000)
    failure_count: int = Field(ge=0, le=1_000)
    next_retry_at: datetime | None = None
    reason: str = Field(default="", max_length=2_000)
    state_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("idempotency_key", "event_id")
    @classmethod
    def safe_keys(cls, value: str, info: Any) -> str:
        return _safe_identity(value, info.field_name)

    @field_validator("next_retry_at")
    @classmethod
    def retry_time_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value, "next_retry_at")

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str) -> str:
        if _SECRET.search(value) or _CONTROL.search(value):
            raise ValueError("reason contains unsafe or secret-shaped text")
        return value

    @model_validator(mode="after")
    def derive_hash(self) -> DispatchState:
        if self.status not in {DispatchStatus.BACKOFF, DispatchStatus.CIRCUIT_OPEN} and self.next_retry_at is not None:
            raise ValueError("next_retry_at is only valid during backoff or circuit open")
        expected = _digest(
            self.model_dump(mode="json", by_alias=True, exclude={"state_hash"})
        )
        if self.state_hash is not None and self.state_hash != expected:
            raise ValueError("state_hash does not match canonical contents")
        object.__setattr__(self, "state_hash", expected)
        return self


class DispatchReceipt(_SchedulerContract):
    """A deterministic decision receipt; it contains no execution result."""

    schema_: Literal["dispatch-receipt/v1"] = Field(
        default="dispatch-receipt/v1", alias="schema", serialization_alias="schema"
    )
    request: DispatchRequest
    state: DispatchState
    status: DispatchStatus
    reason: str = Field(default="", max_length=2_000)
    receipt_hash: str | None = Field(default=None, pattern=_HASH)

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str) -> str:
        if _SECRET.search(value) or _CONTROL.search(value):
            raise ValueError("reason contains unsafe or secret-shaped text")
        return value

    @model_validator(mode="after")
    def validate_bindings_and_hash(self) -> DispatchReceipt:
        if self.request.request_hash != self.state.request_hash:
            raise ValueError("dispatch state is not bound to request")
        if self.status is not self.state.status:
            raise ValueError("receipt status does not match dispatch state")
        expected = _digest(
            self.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"})
        )
        if self.receipt_hash is not None and self.receipt_hash != expected:
            raise ValueError("receipt_hash does not match canonical contents")
        object.__setattr__(self, "receipt_hash", expected)
        return self


def _rehydrate(value: Any, model: type[_SchedulerContract], hash_field: str) -> Any:
    if not isinstance(value, model) or not getattr(value, hash_field, None):
        raise SchedulerError(f"unhashed {model.__name__} is rejected")
    canonical = _canonical(value.model_dump(mode="json", by_alias=True))
    try:
        hydrated = model.model_validate(json.loads(canonical))
    except Exception as exc:
        raise SchedulerError(f"{model.__name__} failed integrity rehydration") from exc
    if getattr(hydrated, hash_field) != getattr(value, hash_field) or _canonical(hydrated.model_dump(mode="json", by_alias=True)) != canonical:
        raise SchedulerError(f"{model.__name__} hash does not match canonical contents")
    return hydrated


def rehydrate_dispatch_request(request: DispatchRequest) -> DispatchRequest:
    return _rehydrate(request, DispatchRequest, "request_hash")


def rehydrate_dispatch_state(state: DispatchState) -> DispatchState:
    return _rehydrate(state, DispatchState, "state_hash")


def rehydrate_dispatch_receipt(receipt: DispatchReceipt) -> DispatchReceipt:
    return _rehydrate(receipt, DispatchReceipt, "receipt_hash")


def _copy_receipt(receipt: DispatchReceipt) -> DispatchReceipt:
    return rehydrate_dispatch_receipt(
        DispatchReceipt.model_validate(json.loads(_canonical(receipt.model_dump(mode="json", by_alias=True))))
    )


ApprovalInput = bool | ApprovalScope | ApprovalToken


class InMemoryDispatchCoordinator:
    """Deterministic, execution-free scheduler with in-memory safeguards."""

    def __init__(
        self,
        *,
        approval: ApprovalInput | None = None,
        approval_scope: ApprovalScope | str | None = None,
        actor_id: str | None = None,
        failure_threshold: int = 3,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 300.0,
    ) -> None:
        if failure_threshold < 1 or failure_threshold > 1_000:
            raise ValueError("failure_threshold must be between 1 and 1000")
        if base_backoff_seconds <= 0 or max_backoff_seconds <= 0 or base_backoff_seconds > max_backoff_seconds:
            raise ValueError("backoff bounds are invalid")
        if approval_scope is not None:
            try:
                selected_scope = ApprovalScope(approval_scope)
            except Exception as exc:
                raise ValueError("unknown approval scope") from exc
            if approval is not None and approval is not selected_scope:
                raise ValueError("approval and approval_scope conflict")
            approval = selected_scope
        self.approval = approval
        self.actor_id = None if actor_id is None else _safe_identity(actor_id, "actor_id")
        self.failure_threshold = failure_threshold
        self.base_backoff_seconds = float(base_backoff_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self._records_by_idempotency: dict[str, DispatchReceipt] = {}
        self._records_by_event: dict[str, DispatchReceipt] = {}
        self._locks: dict[str, str] = {}
        self._lock = RLock()

    def _persist_state(self) -> None:
        """Hook for durable coordinators; the in-memory implementation is a no-op."""

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        return _utc(value, "now") if value is not None else datetime.now(timezone.utc)

    @staticmethod
    def _state(request: DispatchRequest, status: DispatchStatus, *, failure_count: int = 0, next_retry_at: datetime | None = None, reason: str = "") -> DispatchState:
        return DispatchState(
            idempotency_key=request.idempotency_key,
            event_id=request.event_id,
            request_hash=request.request_hash or "",
            status=status,
            attempt=request.attempt,
            failure_count=failure_count,
            next_retry_at=next_retry_at,
            reason=reason,
        )

    @classmethod
    def _receipt(cls, request: DispatchRequest, status: DispatchStatus, *, failure_count: int = 0, next_retry_at: datetime | None = None, reason: str = "") -> DispatchReceipt:
        state = cls._state(request, status, failure_count=failure_count, next_retry_at=next_retry_at, reason=reason)
        return DispatchReceipt(request=request, state=state, status=status, reason=reason)

    def _authorization(
        self,
        request: DispatchRequest,
        approval: ApprovalInput | None,
        proposal_plan: ProposalPlan | None,
        now: datetime,
        actor_id: str | None = None,
        review_digest: str | None = None,
    ) -> None:
        hydrated_plan: ProposalPlan | None = None
        if proposal_plan is not None:
            try:
                hydrated_plan = rehydrate_proposal_plan(proposal_plan)
            except ProposalError as exc:
                raise SchedulerError("proposal plan failed integrity validation") from exc
            if hydrated_plan.plan_hash != request.proposal_plan_hash:
                raise SchedulerError("proposal plan hash does not match request")
        if request.action is DispatchAction.MERGE_MAIN:
            raise SchedulerError("merge_main is permanently forbidden")
        if request.action is DispatchAction.PROPOSAL:
            return
        selected = self.approval if approval is None else approval
        if selected is True:
            raise SchedulerError(
                f"{request.action.value} requires a complete approval token"
            )
        if selected is False or selected is None:
            raise SchedulerError(f"{request.action.value} requires explicit approval")
        try:
            if isinstance(selected, ApprovalToken):
                token = rehydrate_approval_token(selected)
                if token.plan_hash != request.proposal_plan_hash:
                    raise SchedulerError("approval token is bound to a different proposal plan")
                if token.scope.value != request.action.value:
                    raise SchedulerError("approval scope does not match action")
                if hydrated_plan is None:
                    raise SchedulerError("consent-bound approval requires the proposal plan")
                authorize_action(
                    hydrated_plan,
                    ProposalAction(request.action.value),
                    token,
                    approver_id=actor_id if actor_id is not None else self.actor_id,
                    review_digest=review_digest,
                    now=now,
                )
                return
            raise SchedulerError(
                f"{request.action.value} requires a complete approval token"
            )
        except SchedulerError:
            raise
        except (ProposalError, ValueError, TypeError) as exc:
            raise SchedulerError("approval authorization was rejected") from exc

    def dispatch(
        self,
        request: DispatchRequest,
        *,
        approval: ApprovalInput | None = None,
        approval_scope: ApprovalScope | str | None = None,
        proposal_plan: ProposalPlan | None = None,
        actor_id: str | None = None,
        review_digest: str | None = None,
        now: datetime | None = None,
    ) -> DispatchReceipt:
        request = rehydrate_dispatch_request(request)
        current = self._now(now)
        if approval_scope is not None:
            try:
                selected_scope = ApprovalScope(approval_scope)
            except Exception as exc:
                raise SchedulerError("unknown approval scope") from exc
            if approval is not None and approval != selected_scope:
                raise SchedulerError("approval and approval_scope conflict")
            approval = selected_scope
        with self._lock:
            existing = self._records_by_idempotency.get(request.idempotency_key)
            if existing is not None:
                if existing.request.request_hash != request.request_hash:
                    return _copy_receipt(self._receipt(request, DispatchStatus.REJECTED, reason="idempotency key conflicts with another request"))
                if existing.status is DispatchStatus.BACKOFF and existing.state.next_retry_at and current < existing.state.next_retry_at:
                    return _copy_receipt(existing)
                if existing.status is DispatchStatus.CIRCUIT_OPEN:
                    return _copy_receipt(existing)
                if existing.status is DispatchStatus.BACKOFF:
                    # A retry after the bounded window creates a new active
                    # reservation while retaining the failure count.
                    try:
                        self._authorization(
                            request,
                            approval,
                            proposal_plan,
                            current,
                            actor_id=actor_id,
                            review_digest=review_digest,
                        )
                    except SchedulerError as exc:
                        return _copy_receipt(self._receipt(request, DispatchStatus.REJECTED, reason=str(exc)))
                    retried = self._receipt(
                        request,
                        DispatchStatus.ACCEPTED,
                        failure_count=existing.state.failure_count,
                        reason="bounded retry accepted",
                    )
                    self._records_by_idempotency[request.idempotency_key] = retried
                    self._records_by_event[request.event_id] = retried
                    self._locks[request.idempotency_key] = request.request_hash or ""
                    self._persist_state()
                    return _copy_receipt(retried)
                return _copy_receipt(self._receipt(request, DispatchStatus.DEDUPLICATED, failure_count=existing.state.failure_count, reason="request was already recorded"))
            event_existing = self._records_by_event.get(request.event_id)
            if event_existing is not None:
                # Event identity is independent of the caller's idempotency
                # key.  A different payload for the same event is a conflict;
                # the same payload is a deterministic replay.
                if (
                    event_existing.request.proposal_plan_hash != request.proposal_plan_hash
                    or event_existing.request.action is not request.action
                    or event_existing.request.attempt != request.attempt
                ):
                    return _copy_receipt(self._receipt(request, DispatchStatus.REJECTED, reason="event id conflicts with another request"))
                return _copy_receipt(self._receipt(request, DispatchStatus.DEDUPLICATED, failure_count=event_existing.state.failure_count, reason="event was already recorded"))
            if request.idempotency_key in self._locks:
                if self._locks[request.idempotency_key] != request.request_hash:
                    return _copy_receipt(self._receipt(request, DispatchStatus.REJECTED, reason="dispatch key conflicts with an active lock"))
                return _copy_receipt(self._receipt(request, DispatchStatus.LOCKED, reason="dispatch key is already locked"))
            try:
                self._authorization(
                    request,
                    approval,
                    proposal_plan,
                    current,
                    actor_id=actor_id,
                    review_digest=review_digest,
                )
            except SchedulerError as exc:
                return _copy_receipt(self._receipt(request, DispatchStatus.REJECTED, reason=str(exc)))
            accepted = self._receipt(request, DispatchStatus.ACCEPTED, reason="dispatch accepted")
            self._records_by_idempotency[request.idempotency_key] = accepted
            self._records_by_event[request.event_id] = accepted
            self._locks[request.idempotency_key] = request.request_hash or ""
            self._persist_state()
            return _copy_receipt(accepted)

    submit = dispatch
    enqueue = dispatch

    def acquire_lock(self, request: DispatchRequest) -> DispatchReceipt:
        request = rehydrate_dispatch_request(request)
        with self._lock:
            if request.idempotency_key in self._locks:
                return _copy_receipt(self._receipt(request, DispatchStatus.LOCKED, reason="dispatch key is already locked"))
            self._locks[request.idempotency_key] = request.request_hash or ""
            self._persist_state()
            return _copy_receipt(self._receipt(request, DispatchStatus.ACCEPTED, reason="dispatch key locked"))

    lock = acquire_lock

    def complete(self, request: DispatchRequest) -> DispatchReceipt:
        request = rehydrate_dispatch_request(request)
        with self._lock:
            existing = self._records_by_idempotency.get(request.idempotency_key)
            if existing is None or existing.request.request_hash != request.request_hash:
                return _copy_receipt(self._receipt(request, DispatchStatus.REJECTED, reason="dispatch request is not active"))
            self._locks.pop(request.idempotency_key, None)
            self._persist_state()
            return _copy_receipt(self._receipt(request, DispatchStatus.DEDUPLICATED, failure_count=existing.state.failure_count, reason="dispatch already completed"))

    succeed = complete
    release = complete

    def record_failure(
        self,
        request: DispatchRequest,
        *,
        reason: str = "dispatch failed",
        now: datetime | None = None,
    ) -> DispatchReceipt:
        request = rehydrate_dispatch_request(request)
        current = self._now(now)
        with self._lock:
            existing = self._records_by_idempotency.get(request.idempotency_key)
            if existing is None or existing.request.request_hash != request.request_hash:
                return _copy_receipt(self._receipt(request, DispatchStatus.REJECTED, reason="dispatch request is not active"))
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2_000 or _SECRET.search(reason) or _CONTROL.search(reason):
                return _copy_receipt(self._receipt(request, DispatchStatus.REJECTED, reason="failure reason is unsafe"))
            failures = existing.state.failure_count + 1
            if failures >= self.failure_threshold:
                status = DispatchStatus.CIRCUIT_OPEN
                retry = None
                state_reason = "failure threshold opened circuit"
            else:
                status = DispatchStatus.BACKOFF
                delay = min(self.max_backoff_seconds, self.base_backoff_seconds * (2 ** (failures - 1)))
                retry = current + timedelta(seconds=delay)
                state_reason = "dispatch failure requires bounded backoff"
            updated = self._receipt(request, status, failure_count=failures, next_retry_at=retry, reason=state_reason)
            self._records_by_idempotency[request.idempotency_key] = updated
            self._records_by_event[request.event_id] = updated
            self._locks.pop(request.idempotency_key, None)
            self._persist_state()
            return _copy_receipt(updated)

    fail = record_failure
    failure = record_failure

    def reset_circuit(self, request: DispatchRequest) -> DispatchReceipt:
        request = rehydrate_dispatch_request(request)
        with self._lock:
            existing = self._records_by_idempotency.get(request.idempotency_key)
            if existing is None or existing.request.request_hash != request.request_hash:
                return _copy_receipt(self._receipt(request, DispatchStatus.REJECTED, reason="dispatch request is not recorded"))
            if existing.status is not DispatchStatus.CIRCUIT_OPEN:
                return _copy_receipt(existing)
            reset = self._receipt(request, DispatchStatus.ACCEPTED, reason="circuit reset requires a fresh dispatch")
            self._records_by_idempotency[request.idempotency_key] = reset
            self._records_by_event[request.event_id] = reset
            self._persist_state()
            return _copy_receipt(reset)


class SQLiteDispatchCoordinator(InMemoryDispatchCoordinator):
    """A local, append-only SQLite journal around the dispatch coordinator.

    The journal stores only validated receipts and lock identities.  It never
    stores approval tokens, proposal bodies, or transport data.  Each row is a
    complete snapshot, which makes replay deterministic and lets a damaged
    row fail closed rather than silently losing admission state.
    """

    JOURNAL_SCHEMA = "dispatch-journal/v1"

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        journal_path: str | Path | None = None,
        state_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        candidates = [value for value in (path, journal_path, state_path) if value is not None]
        if not candidates or any(Path(value) != Path(candidates[0]) for value in candidates[1:]):
            raise ValueError("exactly one journal path is required")
        self.path = Path(candidates[0])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(**kwargs)
        try:
            self._migrate_journal()
            self._load_journal()
        except SchedulerPersistenceError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SchedulerPersistenceError("dispatch journal could not be opened") from exc

    def _connect_journal(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    def _migrate_journal(self) -> None:
        with self._connect_journal() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS dispatch_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = db.execute("SELECT value FROM dispatch_meta WHERE key = 'version'").fetchone()
            version = int(row[0]) if row else 0
            if version > 1:
                raise SchedulerPersistenceError(f"dispatch journal schema {version} is newer than supported 1")
            db.execute(
                """CREATE TABLE IF NOT EXISTS dispatch_journal (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL
                )"""
            )
            db.execute("INSERT OR REPLACE INTO dispatch_meta(key, value) VALUES ('version', '1')")
            db.execute("COMMIT")

    def _snapshot(self) -> dict[str, Any]:
        records = [
            receipt.model_dump(mode="json", by_alias=True)
            for receipt in sorted(self._records_by_idempotency.values(), key=lambda item: item.request.idempotency_key)
        ]
        locks = [
            {"idempotency_key": key, "request_hash": request_hash}
            for key, request_hash in sorted(self._locks.items())
        ]
        return {
            "schema": self.JOURNAL_SCHEMA,
            "config": {
                "failure_threshold": self.failure_threshold,
                "base_backoff_seconds": self.base_backoff_seconds,
                "max_backoff_seconds": self.max_backoff_seconds,
            },
            "records": records,
            "locks": locks,
        }

    def _persist_state(self) -> None:
        snapshot_json = _canonical(self._snapshot())
        snapshot_hash = _digest(json.loads(snapshot_json))
        with self._connect_journal() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO dispatch_journal(snapshot_json, snapshot_hash) VALUES (?, ?)",
                    (snapshot_json, snapshot_hash),
                )
                db.execute("COMMIT")
            except sqlite3.Error:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def _config(value: Any) -> tuple[int, float, float]:
        if not isinstance(value, dict):
            raise SchedulerPersistenceError("dispatch journal config is invalid")
        try:
            threshold = int(value["failure_threshold"])
            base = float(value["base_backoff_seconds"])
            maximum = float(value["max_backoff_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SchedulerPersistenceError("dispatch journal config is invalid") from exc
        if threshold < 1 or threshold > 1_000 or base <= 0 or maximum <= 0 or base > maximum:
            raise SchedulerPersistenceError("dispatch journal config is out of bounds")
        return threshold, base, maximum

    def _apply_snapshot(self, snapshot: Any) -> None:
        if not isinstance(snapshot, dict) or snapshot.get("schema") != self.JOURNAL_SCHEMA:
            raise SchedulerPersistenceError("dispatch journal snapshot schema is invalid")
        threshold, base, maximum = self._config(snapshot.get("config"))
        raw_records = snapshot.get("records")
        raw_locks = snapshot.get("locks")
        if not isinstance(raw_records, list) or not isinstance(raw_locks, list):
            raise SchedulerPersistenceError("dispatch journal snapshot collections are invalid")
        records_by_idempotency: dict[str, DispatchReceipt] = {}
        records_by_event: dict[str, DispatchReceipt] = {}
        for raw in raw_records:
            try:
                receipt = rehydrate_dispatch_receipt(DispatchReceipt.model_validate(raw))
            except Exception as exc:
                raise SchedulerPersistenceError("dispatch journal contains an invalid receipt") from exc
            key = receipt.request.idempotency_key
            event = receipt.request.event_id
            if key in records_by_idempotency or event in records_by_event:
                raise SchedulerPersistenceError("dispatch journal contains duplicate identities")
            records_by_idempotency[key] = receipt
            records_by_event[event] = receipt
        locks: dict[str, str] = {}
        for raw in raw_locks:
            if not isinstance(raw, dict) or not isinstance(raw.get("idempotency_key"), str) or not isinstance(raw.get("request_hash"), str):
                raise SchedulerPersistenceError("dispatch journal contains an invalid lock")
            key = raw["idempotency_key"]
            try:
                _safe_identity(key, "idempotency_key")
            except ValueError as exc:
                raise SchedulerPersistenceError("dispatch journal contains an unsafe lock") from exc
            if not _HASH.fullmatch(raw["request_hash"]):
                raise SchedulerPersistenceError("dispatch journal contains an invalid lock hash")
            if key in locks:
                raise SchedulerPersistenceError("dispatch journal contains duplicate locks")
            recorded = records_by_idempotency.get(key)
            if recorded is not None and recorded.request.request_hash != raw["request_hash"]:
                raise SchedulerPersistenceError("dispatch journal lock is not bound to its receipt")
            locks[key] = raw["request_hash"]
        self.failure_threshold = threshold
        self.base_backoff_seconds = base
        self.max_backoff_seconds = maximum
        self._records_by_idempotency = records_by_idempotency
        self._records_by_event = records_by_event
        self._locks = locks

    def _load_journal(self) -> None:
        with self._connect_journal() as db:
            rows = db.execute(
                "SELECT sequence, snapshot_json, snapshot_hash FROM dispatch_journal ORDER BY sequence"
            ).fetchall()
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot_json"])
                if not isinstance(snapshot, dict) or _canonical(snapshot) != row["snapshot_json"]:
                    raise ValueError("snapshot is not canonical")
                if _digest(snapshot) != row["snapshot_hash"]:
                    raise ValueError("snapshot hash mismatch")
                self._apply_snapshot(snapshot)
            except SchedulerPersistenceError:
                raise
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SchedulerPersistenceError(
                    f"dispatch journal row {row['sequence']} failed integrity validation"
                ) from exc

    @property
    def journal_entries(self) -> int:
        """Return the number of durable snapshots, for diagnostics and tests."""
        with self._connect_journal() as db:
            return int(db.execute("SELECT COUNT(*) FROM dispatch_journal").fetchone()[0])


# The descriptive name is convenient for callers while retaining one implementation.
PersistentDispatchCoordinator = SQLiteDispatchCoordinator


plan_dispatch = DispatchRequest


__all__ = [
    "ApprovalInput",
    "DispatchAction",
    "DispatchReceipt",
    "DispatchRequest",
    "DispatchState",
    "DispatchStatus",
    "InMemoryDispatchCoordinator",
    "PersistentDispatchCoordinator",
    "SchedulerError",
    "SchedulerPersistenceError",
    "SQLiteDispatchCoordinator",
    "plan_dispatch",
    "rehydrate_dispatch_receipt",
    "rehydrate_dispatch_request",
    "rehydrate_dispatch_state",
]
