import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.cli import app
from src.acceptance import (
    BaselineGateEvidence,
    CandidateAcceptanceAdmission,
    QualityGateEvidence,
    ValidationGateEvidence,
    accept_candidate,
    plan_candidate_acceptance,
)
from src.attribution import AttributionHypothesis, BaselineObservation, CandidateChangeEvidence
from src.ha_cli import ProcessResult
from src.storage import Ledger
from src.target_publication import (
    RequiredCheck,
    TargetPublicationConfig,
    TargetPublicationError,
    TargetPublicationReceipt,
    TargetPublicationRequest,
    TargetPublicationStatus,
    TargetPublisher,
    rehydrate_target_publication,
)


COMMIT = "a" * 40
REPOSITORY = "cyrneutron/harness-anything"
TASK_ID = "task_033c9c33ea9f3df55030b56e38"
EXECUTION_ID = "exe_target_pr_publication_20260904"


class FixtureTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected command: {argv}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def result(payload=None, *, code=0, stderr=b""):
    if isinstance(payload, bytes):
        stdout = payload
    elif payload is None:
        stdout = b""
    else:
        stdout = json.dumps(payload).encode()
    return ProcessResult(code, stdout, stderr)


def executable(tmp_path: Path, name: str, contents: str) -> tuple[Path, str]:
    path = tmp_path / name
    path.write_text(contents)
    return path, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def config(tmp_path: Path) -> TargetPublicationConfig:
    git, git_hash = executable(tmp_path, "git", "git-fixture")
    gh, gh_hash = executable(tmp_path, "gh", "gh-fixture")
    return TargetPublicationConfig(
        git_executable=git,
        git_sha256=git_hash,
        gh_executable=gh,
        gh_sha256=gh_hash,
        github_home=tmp_path,
        timeout_seconds=2,
        max_output_bytes=10_000,
    )


def admission(target_root: Path) -> CandidateAcceptanceAdmission:
    baseline = BaselineObservation(
        attempt_id="attempt-publication",
        signal_signature="signal-publication",
        base_commit=COMMIT,
        passed=False,
        command="pytest tests/test_target.py",
        summary="publication fixture baseline reproduces the failure",
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    hypothesis = AttributionHypothesis(
        baseline_hash=baseline.observation_hash,
        category="dependency",
        confidence=0.8,
        root_cause="the publication fixture dependency changed",
        affected_paths=["src/target.py"],
        model_version="model-publication",
        prompt_version="prompt-publication",
    )
    candidate = CandidateChangeEvidence(
        hypothesis_hash=hypothesis.hypothesis_hash,
        baseline_hash=baseline.observation_hash,
        patch_hash="sha256:" + "b" * 64,
        targeted_tests=["pytest tests/test_target.py"],
        regression_tests=["pytest -q"],
        residual_risk="no known publication fixture risk",
    )
    plan = plan_candidate_acceptance(
        baseline,
        candidate,
        BaselineGateEvidence(
            baseline_hash=baseline.observation_hash,
            reproduction_command="pytest tests/test_target.py",
            reproduction_evidence="exit code 1: failure reproduced",
        ),
        ValidationGateEvidence(
            candidate_hash=candidate.evidence_hash,
            hypothesis_hash=hypothesis.hypothesis_hash,
            patch_hash=candidate.patch_hash,
            targeted_test_count=1,
            regression_test_count=1,
            targeted_evidence="targeted tests passed",
            regression_evidence="regression tests passed",
        ),
        QualityGateEvidence(
            complexity_delta=0,
            cost_units=1,
            quality_evidence="quality and security checks passed",
        ),
        hypothesis=hypothesis,
    )
    return CandidateAcceptanceAdmission(
        acceptance_receipt=accept_candidate(plan),
        repository=REPOSITORY,
        target_task_id=TASK_ID,
        accepted_execution_id=EXECUTION_ID,
        accepted_commit=COMMIT,
        target_root=target_root,
        expected_remote=f"https://github.com/{REPOSITORY}.git",
    )


def request(target_root: Path | None = None, **changes) -> TargetPublicationRequest:
    values = {
        "admission": admission(target_root or Path.cwd()),
        "title": "fix: bounded publication",
        "body": "# English\nA bounded publication body with sufficient declared context.\n\n---\n\n# 中文\n这是足够长的双语说明正文，用于受控发布和门禁验证。",
        "max_poll_attempts": 2,
        "poll_interval_seconds": 0,
    }
    values.update(changes)
    return TargetPublicationRequest(**values)


def target_proof(request_value: TargetPublicationRequest):
    return [
        result((request_value.expected_origin + "\n").encode()),
        result(b""),
        result(b""),
        result((request_value.accepted_commit + "\n").encode()),
    ]


def test_publish_creates_deterministic_ref_pr_and_success_receipt(tmp_path: Path):
    value = request(tmp_path)
    transport = FixtureTransport(
        [
            *target_proof(value),
            result(None, code=1, stderr=b"HTTP 404: Not Found"),
            result({"object": {"sha": COMMIT}}),
            result([]),
            result(f"https://github.com/{REPOSITORY}/pull/19\n".encode()),
            result(
                {
                    "number": 19,
                    "url": f"https://github.com/{REPOSITORY}/pull/19",
                    "state": "OPEN",
                    "headRefName": value.head_branch,
                    "baseRefName": "main",
                    "headRefOid": COMMIT,
                }
            ),
            result([{"name": "typecheck", "state": "SUCCESS", "link": "https://github.com/run/1"}]),
        ]
    )

    receipt = TargetPublisher(config(tmp_path), transport).publish(tmp_path, value)

    assert receipt.status is TargetPublicationStatus.SUCCEEDED
    assert receipt.pr_number == 19
    assert receipt.checks == [RequiredCheck(name="typecheck", state="SUCCESS", link="https://github.com/run/1")]
    assert rehydrate_target_publication(receipt) == receipt
    assert all(call[1]["env"] == {} for call in transport.calls[:4])
    assert all(call[1]["env"] == {"HOME": str(tmp_path)} for call in transport.calls[4:])
    assert all(call[1]["cwd"] == tmp_path for call in transport.calls)
    assert transport.calls[4][0][1:] == [
        "api",
        f"repos/{REPOSITORY}/git/ref/heads/{value.head_branch}",
    ]
    assert transport.calls[5][0][1:] == [
        "api",
        "--method",
        "POST",
        f"repos/{REPOSITORY}/git/refs",
        "-f",
        f"ref=refs/heads/{value.head_branch}",
        "-f",
        f"sha={COMMIT}",
    ]
    assert "merge" not in " ".join(" ".join(call[0]) for call in transport.calls).lower()


def test_publish_reuses_matching_pr_and_appends_receipt_to_ledger(tmp_path: Path):
    value = request(tmp_path)
    existing = {
        "object": {"sha": COMMIT},
    }
    matching_pr = {
        "number": 19,
        "url": f"https://github.com/{REPOSITORY}/pull/19",
        "headRefName": value.head_branch,
        "baseRefName": "main",
        "headRefOid": COMMIT,
    }
    transport = FixtureTransport(
        [
            *target_proof(value),
            result(existing),
            result([matching_pr]),
            result({**matching_pr, "state": "OPEN"}),
            result([{"name": "required", "state": "SUCCESS", "link": "https://github.com/run/1"}]),
        ]
    )
    ledger = Ledger(tmp_path / "ledger.sqlite")

    first = TargetPublisher(config(tmp_path), transport).publish(tmp_path, value, ledger=ledger)
    replay = ledger.record_target_publication(first)

    assert first.status is TargetPublicationStatus.SUCCEEDED
    assert replay == first
    assert list(ledger.iter_target_publications(first.request_hash)) == [first]
    assert not any("pr create" in " ".join(call[0]) for call in transport.calls)


def test_publish_rejects_dirty_target_before_any_github_mutation(tmp_path: Path):
    value = request(tmp_path)
    transport = FixtureTransport(
        [
            result((value.expected_origin + "\n").encode()),
            result(b" M packages/cli/src/file.ts\n"),
        ]
    )

    receipt = TargetPublisher(config(tmp_path), transport).publish(tmp_path, value)

    assert receipt.status is TargetPublicationStatus.REJECTED
    assert "tracked changes" in receipt.reason
    assert all("gh" not in call[0][0] for call in transport.calls)


def test_publish_rejects_unknown_branch_lookup_error_before_ref_creation(tmp_path: Path):
    value = request(tmp_path)
    transport = FixtureTransport(
        [
            *target_proof(value),
            result(None, code=1, stderr=b"authentication transport unavailable"),
        ]
    )

    receipt = TargetPublisher(config(tmp_path), transport).publish(tmp_path, value)

    assert receipt.status is TargetPublicationStatus.REJECTED
    assert "branch existence" in receipt.reason
    assert len(transport.calls) == 5
    assert all("--method" not in call[0] for call in transport.calls)


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["PENDING", "IN_PROGRESS"], TargetPublicationStatus.PENDING),
        (["FAILURE"], TargetPublicationStatus.REJECTED),
        (["SKIPPED"], TargetPublicationStatus.REJECTED),
    ],
)
def test_publish_never_accepts_non_successful_required_checks(tmp_path: Path, states, expected):
    value = request(tmp_path, max_poll_attempts=2)
    matching_pr = {
        "number": 19,
        "url": f"https://github.com/{REPOSITORY}/pull/19",
        "headRefName": value.head_branch,
        "baseRefName": "main",
        "headRefOid": COMMIT,
    }
    check_responses = [
        result([{"name": "required", "state": state, "link": "https://github.com/run/1"}])
        for state in states
    ]
    transport = FixtureTransport(
        [
            *target_proof(value),
            result({"object": {"sha": COMMIT}}),
            result([matching_pr]),
            result({**matching_pr, "state": "OPEN"}),
            *check_responses,
        ]
    )

    receipt = TargetPublisher(config(tmp_path), transport).publish(tmp_path, value)

    assert receipt.status is expected
    assert receipt.status is not TargetPublicationStatus.SUCCEEDED
    assert receipt.reason


def test_publication_rejects_drifted_executable_before_target_access(tmp_path: Path):
    value = request(tmp_path)
    cfg = config(tmp_path)
    cfg.git_executable.write_text("changed")

    receipt = TargetPublisher(cfg, FixtureTransport([])).publish(tmp_path, value)

    assert receipt.status is TargetPublicationStatus.REJECTED
    assert "digest drift" in receipt.reason


def test_request_and_receipt_reject_unsafe_or_unbound_values():
    with pytest.raises(ValueError, match="admission"):
        TargetPublicationRequest(
            title="fix: missing admission",
            body="A publication request without typed acceptance evidence.",
        )
    with pytest.raises(ValueError, match="full 40-character SHA"):
        request(accepted_commit="a" * 39)
    with pytest.raises(ValueError, match="secret-shaped"):
        request(body="Bearer token-value " + "body" * 10)
    with pytest.raises(ValueError, match="safe to derive"):
        request(target_task_id="task:branch")
    with pytest.raises(ValueError, match="unsafe to derive"):
        TargetPublicationReceipt(
            status=TargetPublicationStatus.REJECTED,
            admission=admission(Path.cwd()),
            request_hash="sha256:" + "0" * 64,
            repository=REPOSITORY,
            target_task_id="task:branch",
            accepted_execution_id=EXECUTION_ID,
            accepted_commit=COMMIT,
            acceptance_plan_hash=admission(Path.cwd()).acceptance_receipt.plan_hash,
            target_root=Path.cwd(),
            expected_remote=f"https://github.com/{REPOSITORY}.git",
            head_branch="mi/task:branch",
            reason="rejected",
            observed_at="2026-09-04T00:00:00Z",
        )
    with pytest.raises(ValueError, match="not bound"):
        TargetPublicationReceipt(
            status=TargetPublicationStatus.REJECTED,
            admission=admission(Path.cwd()),
            request_hash="sha256:" + "0" * 64,
            repository=REPOSITORY,
            target_task_id=TASK_ID,
            accepted_execution_id=EXECUTION_ID,
            accepted_commit=COMMIT,
            acceptance_plan_hash=admission(Path.cwd()).acceptance_receipt.plan_hash,
            target_root=Path.cwd(),
            expected_remote=f"https://github.com/{REPOSITORY}.git",
            head_branch="mi/different-task",
            reason="rejected",
            observed_at="2026-09-04T00:00:00Z",
        )


def test_success_receipt_requires_bound_request_and_poll(tmp_path: Path):
    value = request(tmp_path)
    with pytest.raises(ValueError, match="request_hash"):
        TargetPublicationReceipt(
            status=TargetPublicationStatus.SUCCEEDED,
            admission=value.admission,
            request_hash="sha256:" + "0" * 64,
            repository=value.repository,
            target_task_id=value.target_task_id,
            accepted_execution_id=value.accepted_execution_id,
            accepted_commit=value.accepted_commit,
            acceptance_plan_hash=value.admission.acceptance_receipt.plan_hash,
            target_root=tmp_path,
            expected_remote=value.expected_origin,
            head_branch=value.head_branch,
            pr_number=1,
            pr_url=f"https://github.com/{value.repository}/pull/1",
            checks=[RequiredCheck(name="required", state="SUCCESS")],
            poll_attempts=1,
            observed_at="2026-09-04T00:00:00Z",
        )


def test_required_check_query_nonzero_is_rejected(tmp_path: Path):
    value = request(tmp_path)
    matching_pr = {
        "number": 19,
        "url": f"https://github.com/{REPOSITORY}/pull/19",
        "headRefName": value.head_branch,
        "baseRefName": "main",
        "headRefOid": COMMIT,
    }
    transport = FixtureTransport(
        [
            *target_proof(value),
            result({"object": {"sha": COMMIT}}),
            result([matching_pr]),
            result({**matching_pr, "state": "OPEN"}),
            result([{"name": "required", "state": "SUCCESS"}], code=1, stderr=b"auth failure"),
        ]
    )
    receipt = TargetPublisher(config(tmp_path), transport).publish(tmp_path, value)
    assert receipt.status is TargetPublicationStatus.REJECTED
    with pytest.raises(ValueError, match="URL is not bound"):
        TargetPublicationReceipt(
            status=TargetPublicationStatus.REJECTED,
            admission=value.admission,
            request_hash="sha256:" + "0" * 64,
            repository=REPOSITORY,
            target_task_id=TASK_ID,
            accepted_execution_id=EXECUTION_ID,
            accepted_commit=COMMIT,
            acceptance_plan_hash=value.admission.acceptance_receipt.plan_hash,
            target_root=tmp_path,
            expected_remote=value.expected_origin,
            head_branch=f"mi/{TASK_ID}",
            pr_number=1,
            pr_url="https://github.com/other/repository/pull/1",
            reason="rejected",
            observed_at="2026-09-04T00:00:00Z",
        )


def test_cli_requires_typed_admission_file(tmp_path: Path):
    body_file = tmp_path / "body.md"
    body_file.write_text("A safe sufficiently long publication body.")
    config_value = config(tmp_path)
    result_value = CliRunner().invoke(
        app,
        [
            "target",
            "publish",
            "--root",
            str(tmp_path),
            "--git-executable",
            str(config_value.git_executable),
            "--git-sha256",
            config_value.git_sha256,
            "--gh-executable",
            str(config_value.gh_executable),
            "--gh-sha256",
            config_value.gh_sha256,
            "--github-home",
            str(tmp_path),
            "--title",
            "fix: bounded publication",
            "--body-file",
            str(body_file),
        ],
    )

    assert result_value.exit_code == 2
    assert "--admission-file" in result_value.output
