import pytest

from src.ingestion.github_ingestion import GitHubIngestionError, ingest_github_snapshot
from src.ingestion.github_readonly import FixtureTransport, GitHubReadOnlyAdapter
from src.ingestion import ingest_github_snapshot as exported_ingest_github_snapshot
from src.storage import Ledger


OWNER = "example-org"
REPOSITORY = "example-repo"
RUNS_ENDPOINT = f"repos/{OWNER}/{REPOSITORY}/actions/runs"
ISSUES_ENDPOINT = f"repos/{OWNER}/{REPOSITORY}/issues"
BASE_COMMIT = "a" * 40


def _run(run_id: int, *, output=None, conclusion="failure") -> dict:
    return {
        "id": run_id,
        "name": "CI",
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": BASE_COMMIT,
        "output": output,
        "updated_at": "2026-01-01T00:00:00Z",
    }


def _issue(issue_id: int, *, title: str, body=None, pull_request: bool = False) -> dict:
    payload = {
        "number": issue_id,
        "title": title,
        "body": body,
        "state": "open",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    if pull_request:
        payload["pull_request"] = {"url": "https://example.invalid/pr"}
    return payload


def _adapter(*, runs=None, issues=None) -> GitHubReadOnlyAdapter:
    responses = {}
    if runs is not None:
        responses[RUNS_ENDPOINT] = {"workflow_runs": runs}
    if issues is not None:
        responses[ISSUES_ENDPOINT] = {"issues": issues}
    return GitHubReadOnlyAdapter(FixtureTransport(responses))


def test_ingest_github_snapshot_merges_runs_before_issues_and_preserves_empty_text(tmp_path) -> None:
    adapter = _adapter(
        runs=[_run(2, output=None), _run(1, output="pytest failed", conclusion="success")],
        issues=[_issue(9, title="Empty body", body=None), _issue(8, title="Pull request", body="ignored", pull_request=True)],
    )

    attempts = ingest_github_snapshot(adapter, OWNER, REPOSITORY, Ledger(tmp_path / "history.db"), BASE_COMMIT, limit=3)

    assert [attempt.input_snapshot.source for attempt in attempts] == ["ci", "ci", "issue"]
    assert [attempt.input_snapshot.metadata["external_id"] for attempt in attempts] == ["1", "2", "9"]
    assert [attempt.input_snapshot.content for attempt in attempts] == ["pytest failed", "", "Empty body"]
    assert attempts[0].status.value == "proposed"
    assert attempts[1].status.value == "failed"
    assert attempts[2].status.value == "proposed"


def test_repeated_github_snapshot_is_idempotent(tmp_path) -> None:
    adapter = _adapter(runs=[_run(1, output=None)], issues=[_issue(2, title="Issue", body=None)])
    ledger = Ledger(tmp_path / "history.db")

    first = ingest_github_snapshot(adapter, OWNER, REPOSITORY, ledger, BASE_COMMIT)
    replay = ingest_github_snapshot(adapter, OWNER, REPOSITORY, ledger, BASE_COMMIT)

    assert replay == first
    assert ledger.count() == 2


@pytest.mark.parametrize(
    ("owner", "repository", "limit"),
    [
        ("../org", REPOSITORY, 20),
        (OWNER, "repo/escape", 20),
        (OWNER, REPOSITORY, 0),
        (OWNER, REPOSITORY, 101),
        (OWNER, REPOSITORY, True),
    ],
)
def test_github_snapshot_rejects_unbounded_arguments(tmp_path, owner, repository, limit) -> None:
    ledger = Ledger(tmp_path / "history.db")

    with pytest.raises(GitHubIngestionError):
        ingest_github_snapshot(_adapter(runs=[], issues=[]), owner, repository, ledger, BASE_COMMIT, limit=limit)

    assert ledger.count() == 0


def test_github_snapshot_rejects_over_limit_without_silent_truncation(tmp_path) -> None:
    ledger = Ledger(tmp_path / "history.db")
    adapter = _adapter(runs=[_run(1), _run(2)], issues=[_issue(3, title="Issue", body="body")])

    with pytest.raises(GitHubIngestionError):
        ingest_github_snapshot(adapter, OWNER, REPOSITORY, ledger, BASE_COMMIT, limit=2)

    assert ledger.count() == 0


def test_github_snapshot_endpoint_failure_cannot_persist_partial_results(tmp_path) -> None:
    ledger = Ledger(tmp_path / "history.db")
    adapter = _adapter(runs=[_run(1)], issues=None)

    with pytest.raises(GitHubIngestionError):
        ingest_github_snapshot(adapter, OWNER, REPOSITORY, ledger, BASE_COMMIT)

    assert ledger.count() == 0


def test_github_snapshot_rejects_mixed_endpoint_types_without_partial_writes(tmp_path) -> None:
    class MixedAdapter:
        def list_ci_runs(self, owner, repository):
            return [object()]

        def list_issues(self, owner, repository):
            return []

    ledger = Ledger(tmp_path / "history.db")

    with pytest.raises(GitHubIngestionError):
        ingest_github_snapshot(MixedAdapter(), OWNER, REPOSITORY, ledger, BASE_COMMIT)

    assert ledger.count() == 0


def test_github_snapshot_wraps_unexpected_adapter_failures(tmp_path) -> None:
    class FailingAdapter:
        def list_ci_runs(self, owner, repository):
            return []

        def list_issues(self, owner, repository):
            raise RuntimeError("fixture failure")

    ledger = Ledger(tmp_path / "history.db")

    with pytest.raises(GitHubIngestionError):
        ingest_github_snapshot(FailingAdapter(), OWNER, REPOSITORY, ledger, BASE_COMMIT)

    assert ledger.count() == 0


def test_github_snapshot_exports_no_write_transport_behavior() -> None:
    adapter = _adapter(runs=[], issues=[])
    assert adapter.transport.calls == []
    assert exported_ingest_github_snapshot is ingest_github_snapshot
