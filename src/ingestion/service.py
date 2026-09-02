"""Idempotent ingestion from normalized signals into the experience ledger."""

from __future__ import annotations

import hashlib

from src.ingestion.models import SignalEvent, SignalSource
from src.models import Attempt, AttemptStatus, InputSnapshot
from src.storage import Ledger


class IngestionService:
    """Convert one normalized signal into a durable, replay-safe Attempt."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        base_commit: str,
        strategy_version: str,
        model_version: str,
        prompt_version: str,
    ) -> None:
        self.ledger = ledger
        self.base_commit = base_commit
        self.strategy_version = strategy_version
        self.model_version = model_version
        self.prompt_version = prompt_version

    def ingest(self, event: SignalEvent) -> Attempt:
        """Record an event once and return the canonical Attempt on replay."""
        if event.signature is None:  # Defensive: SignalEvent derives this today.
            raise ValueError("normalized signal must have a signature")
        idempotency_key = ":".join((event.signature, self.base_commit, self.strategy_version))
        attempt_id = "attempt-" + hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
        ci_failed = event.source is SignalSource.CI and event.metadata.get("outcome") == "failure"
        attempt = Attempt(
            attempt_id=attempt_id,
            idempotency_key=idempotency_key,
            signal=event.signature,
            base_commit=self.base_commit,
            strategy_version=self.strategy_version,
            input_snapshot=InputSnapshot(
                source=event.source.value,
                content=event.content,
                content_sha256=hashlib.sha256(event.content.encode("utf-8")).hexdigest(),
                captured_at=event.observed_at,
                metadata={"external_id": event.external_id, **event.metadata},
            ),
            status=AttemptStatus.PROPOSED,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
            created_at=event.observed_at,
            updated_at=event.observed_at,
        )
        recorded = self.ledger.record_attempt(attempt)
        if not ci_failed:
            return recorded
        failed = Attempt.model_validate(
            {
                **recorded.model_dump(),
                "status": AttemptStatus.FAILED,
                "failure_reason": "CI conclusion was not success",
                "updated_at": event.observed_at,
            }
        )
        return self.ledger.transition_attempt(failed)
