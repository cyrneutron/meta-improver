"""Deterministic replay of local CI, Issue, and log fixtures."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, TypeAlias

from src.ingestion.models import (
    CIRunSignal,
    IssueSignal,
    LocalLogSignal,
    normalize_ci_run,
    normalize_issue,
    normalize_local_log,
)
from src.ingestion.service import IngestionService
from src.models import Attempt
from src.storage import Ledger


FixtureSignal: TypeAlias = CIRunSignal | IssueSignal | LocalLogSignal
FixtureValue: TypeAlias = FixtureSignal | Mapping[str, Any]


def _parse_fixture(value: FixtureValue) -> FixtureSignal:
    if isinstance(value, (CIRunSignal, IssueSignal, LocalLogSignal)):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("fixture value must be a CI, Issue, or local-log signal or mapping")
    payload = dict(value)
    source = payload.pop("source", payload.pop("kind", None))
    if source == "ci":
        return CIRunSignal.model_validate(payload)
    if source == "issue":
        return IssueSignal.model_validate(payload)
    if source == "local_log":
        return LocalLogSignal.model_validate(payload)
    raise ValueError("fixture mapping requires source=ci, issue, or local_log")


def replay_fixture_signals(
    values: Iterable[FixtureValue],
    ledger: Ledger,
    *,
    base_commit: str,
    strategy_version: str = "replay-v1",
    model_version: str = "fixture-model",
    prompt_version: str = "fixture-prompt",
) -> list[Attempt]:
    """Normalize and ingest fixture values in order, returning canonical attempts.

    The function performs no network or filesystem input beyond writes made by the
    supplied local ledger. Replaying the same bounded values is idempotent.
    """
    # Validate and normalize the complete fixture batch before touching the ledger.
    events = []
    for raw_value in values:
        value = _parse_fixture(raw_value)
        if isinstance(value, CIRunSignal):
            event = normalize_ci_run(value)
        elif isinstance(value, IssueSignal):
            event = normalize_issue(value)
        else:
            event = normalize_local_log(value)
        events.append(event)

    service = IngestionService(
        ledger,
        base_commit=base_commit,
        strategy_version=strategy_version,
        model_version=model_version,
        prompt_version=prompt_version,
    )
    return [service.ingest(event) for event in events]


__all__ = ["FixtureSignal", "FixtureValue", "replay_fixture_signals"]
