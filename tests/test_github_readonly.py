import subprocess
from types import SimpleNamespace

import pytest

from src.ingestion.github_readonly import (
    FixtureTransport,
    GitHubAdapterError,
    GitHubCIRunSignal,
    GitHubIssueSignal,
    GitHubReadOnlyAdapter,
    GhApiTransport,
    parse_ci_runs,
    parse_issues,
)
from src.ingestion.models import CIRunSignal, IssueSignal
from src.ingestion import (
    FixtureTransport as ExportedFixtureTransport,
    GitHubReadOnlyAdapter as ExportedGitHubReadOnlyAdapter,
    GhApiTransport as ExportedGhApiTransport,
    parse_ci_runs as exported_parse_ci_runs,
    parse_issues as exported_parse_issues,
)


OWNER = "example-org"
REPOSITORY = "example-repo"
RUNS_ENDPOINT = f"repos/{OWNER}/{REPOSITORY}/actions/runs"
ISSUES_ENDPOINT = f"repos/{OWNER}/{REPOSITORY}/issues"


def _run(*, output=None, conclusion="failure") -> dict:
    return {
        "id": 101,
        "name": "CI",
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": "a" * 40,
        "output": output,
        "updated_at": "2026-01-01T00:00:00Z",
        "html_url": "https://github.com/example-org/example-repo/actions/runs/101",
    }


def _issue(*, body=None) -> dict:
    return {
        "number": 7,
        "title": "Failure report",
        "body": body,
        "state": "open",
        "updated_at": "2026-01-01T00:00:00Z",
        "html_url": "https://github.com/example-org/example-repo/issues/7",
    }


def test_fixture_transport_maps_runs_and_issues_without_network() -> None:
    transport = FixtureTransport(
        {
            RUNS_ENDPOINT: {"workflow_runs": [_run(output="pytest failed")]},
            ISSUES_ENDPOINT: [_issue(body="Something broke")],
        }
    )
    adapter = GitHubReadOnlyAdapter(transport)

    runs = adapter.list_ci_runs(OWNER, REPOSITORY)
    issues = adapter.list_issues(OWNER, REPOSITORY)

    assert isinstance(runs[0], GitHubCIRunSignal)
    assert isinstance(runs[0], CIRunSignal)
    assert runs[0].run_id == "101"
    assert runs[0].output == "pytest failed"
    assert runs[0].metadata["raw_conclusion"] == "failure"
    assert isinstance(issues[0], GitHubIssueSignal)
    assert isinstance(issues[0], IssueSignal)
    assert issues[0].issue_id == "7"
    assert issues[0].body == "Something broke"
    assert transport.calls == [
        ("gh", "api", "--method", "GET", "--hostname", "github.com", RUNS_ENDPOINT, "--input", "-"),
        ("gh", "api", "--method", "GET", "--hostname", "github.com", ISSUES_ENDPOINT, "--input", "-"),
    ]


def test_empty_output_and_body_are_valid_and_raw_conclusion_is_retained() -> None:
    run = parse_ci_runs({"workflow_runs": [_run(output=None, conclusion="timed_out")]})[0]
    issue = parse_issues([_issue(body=None)])[0]

    assert run.output == ""
    assert run.conclusion == "timed_out"
    assert run.metadata["raw_conclusion"] == "timed_out"
    assert issue.body == ""


@pytest.mark.parametrize("status", ["waiting", "requested", "pending"])
def test_common_github_run_statuses_are_preserved_in_metadata(status: str) -> None:
    payload = _run(output=None)
    payload["status"] = status

    run = parse_ci_runs({"workflow_runs": [payload]})[0]

    assert run.status == status
    assert run.metadata["raw_status"] == status


def test_unknown_github_run_status_fails_closed() -> None:
    payload = _run(output=None)
    payload["status"] = "future_status"

    with pytest.raises(GitHubAdapterError):
        parse_ci_runs({"workflow_runs": [payload]})


def test_missing_conclusion_is_distinct_from_empty_output() -> None:
    run = parse_ci_runs({"workflow_runs": [_run(output=None, conclusion=None)]})[0]

    assert run.output == ""
    assert run.conclusion == ""
    assert run.metadata["raw_conclusion"] is None


def test_pull_requests_from_issues_endpoint_are_not_relabelled() -> None:
    issue = _issue(body="issue")
    pull_request = {**_issue(body="pr"), "number": 8, "pull_request": {"url": "..."}}

    result = parse_issues([issue, pull_request])

    assert [item.issue_id for item in result] == ["7"]


def test_gh_transport_uses_fixed_get_argv_and_does_not_capture_stderr(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return SimpleNamespace(returncode=0, stdout=b"[]")

    monkeypatch.setattr("src.ingestion.github_readonly.subprocess.run", fake_run)

    assert GhApiTransport().run(
        ("gh", "api", "--method", "GET", "--hostname", "github.com", RUNS_ENDPOINT, "--input", "-")
    ) == []
    assert calls == [
        (
            ("gh", "api", "--method", "GET", "--hostname", "github.com", RUNS_ENDPOINT, "--input", "-"),
            {
                "check": False,
                "input": b"",
                "stdout": -1,
                "stderr": subprocess.DEVNULL,
                "shell": False,
                "timeout": 30.0,
            },
        )
    ]


def test_transport_rejects_write_or_pull_request_before_subprocess(monkeypatch) -> None:
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr("src.ingestion.github_readonly.subprocess.run", fake_run)
    transport = GhApiTransport()

    with pytest.raises(GitHubAdapterError):
        transport.run(("gh", "api", "--method", "POST", "--hostname", "github.com", ISSUES_ENDPOINT, "--input", "-"))
    with pytest.raises(GitHubAdapterError):
        transport.run(("gh", "api", "--method", "GET", "--hostname", "github.com", f"repos/{OWNER}/{REPOSITORY}/pulls", "--input", "-"))
    assert called is False


def test_transport_does_not_include_stderr_or_token_in_errors(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b"")

    monkeypatch.setattr("src.ingestion.github_readonly.subprocess.run", fake_run)

    with pytest.raises(GitHubAdapterError) as error:
        GhApiTransport().run(
            ("gh", "api", "--method", "GET", "--hostname", "github.com", RUNS_ENDPOINT, "--input", "-")
        )
    assert "secret" not in str(error.value).lower()
    assert "bearer" not in str(error.value).lower()


def test_repository_path_components_are_bounded() -> None:
    adapter = GitHubReadOnlyAdapter(FixtureTransport({}))

    with pytest.raises(GitHubAdapterError):
        adapter.list_issues("../org", REPOSITORY)
    with pytest.raises(GitHubAdapterError):
        adapter.list_issues(OWNER, "repo/escape")


def test_malformed_fixture_response_fails_closed() -> None:
    with pytest.raises(GitHubAdapterError):
        parse_ci_runs({"workflow_runs": ["bad"]})


def test_adapter_api_is_exported_from_ingestion_package() -> None:
    assert ExportedFixtureTransport is FixtureTransport
    assert ExportedGitHubReadOnlyAdapter is GitHubReadOnlyAdapter
    assert ExportedGhApiTransport is GhApiTransport
    assert exported_parse_ci_runs is parse_ci_runs
    assert exported_parse_issues is parse_issues
