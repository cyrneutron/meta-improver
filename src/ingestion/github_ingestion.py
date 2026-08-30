"""Deterministic ingestion of read-only GitHub snapshots."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TypeAlias

from src.ingestion.github_readonly import (
    GitHubAdapterError,
    GitHubCIRunSignal,
    GitHubIssueSignal,
    GitHubReadOnlyAdapter,
)
from src.ingestion.models import CIRunSignal, IssueSignal
from src.ingestion.replay import replay_fixture_signals
from src.models import Attempt
from src.storage import Ledger


_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_MAX_LIMIT = 100

GitHubSnapshot: TypeAlias = CIRunSignal | IssueSignal


class GitHubIngestionError(GitHubAdapterError):
    """Raised when GitHub snapshots cannot be safely ingested."""


def _validate_repository(owner: str, repository: str) -> None:
    if not isinstance(owner, str) or not _OWNER_PATTERN.fullmatch(owner):
        raise GitHubIngestionError("invalid GitHub owner")
    if not isinstance(repository, str) or not _REPOSITORY_PATTERN.fullmatch(repository):
        raise GitHubIngestionError("invalid GitHub repository")


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
        raise GitHubIngestionError(f"limit must be an integer between 1 and {_MAX_LIMIT}")


def _validate_runs(values: Sequence[object]) -> list[GitHubCIRunSignal]:
    runs: list[GitHubCIRunSignal] = []
    for value in values:
        if not isinstance(value, GitHubCIRunSignal):
            raise GitHubIngestionError("GitHub adapter returned an unsupported CI snapshot")
        runs.append(value)
    return runs


def _validate_issues(values: Sequence[object]) -> list[GitHubIssueSignal]:
    issues: list[GitHubIssueSignal] = []
    for value in values:
        if not isinstance(value, GitHubIssueSignal):
            raise GitHubIngestionError("GitHub adapter returned an unsupported issue snapshot")
        issues.append(value)
    return issues


def _ordered_runs(values: Sequence[GitHubCIRunSignal]) -> list[GitHubCIRunSignal]:
    return sorted(values, key=lambda value: (value.observed_at.isoformat(), value.run_id))


def _ordered_issues(values: Sequence[GitHubIssueSignal]) -> list[GitHubIssueSignal]:
    return sorted(values, key=lambda value: (value.observed_at.isoformat(), value.issue_id))


def ingest_github_snapshot(
    adapter: GitHubReadOnlyAdapter,
    owner: str,
    repository: str,
    ledger: Ledger,
    base_commit: str,
    strategy_version: str = "github-v1",
    model_version: str = "github-readonly",
    prompt_version: str = "github-readonly",
    limit: int = 20,
) -> list[Attempt]:
    """Fetch, bound, order, and ingest one GitHub snapshot batch.

    Both endpoint reads complete before replay starts, so transport or mapping
    failures cannot persist a partial CI/Issue batch.
    """
    _validate_repository(owner, repository)
    _validate_limit(limit)
    try:
        runs = _validate_runs(adapter.list_ci_runs(owner, repository))
        issues = _validate_issues(adapter.list_issues(owner, repository))
    except GitHubAdapterError as exc:
        raise GitHubIngestionError("GitHub snapshot fetch failed") from exc
    except Exception as exc:
        raise GitHubIngestionError("GitHub snapshot fetch failed") from exc

    snapshots = _ordered_runs(runs) + _ordered_issues(issues)
    if len(snapshots) > limit:
        raise GitHubIngestionError("GitHub snapshot response exceeds the requested limit")
    return replay_fixture_signals(
        snapshots,
        ledger,
        base_commit=base_commit,
        strategy_version=strategy_version,
        model_version=model_version,
        prompt_version=prompt_version,
    )


__all__ = ["GitHubIngestionError", "GitHubSnapshot", "ingest_github_snapshot"]
