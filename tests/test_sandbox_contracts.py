import pytest
from pydantic import ValidationError

from src.sandbox.contracts import BoundedArgv, ContainerPolicy, WorktreeRequest


BASE_COMMIT = "a" * 40


def test_bounded_argv_accepts_plain_tokens_and_redacts_secret_values() -> None:
    value = BoundedArgv(command="pytest", args=["-q", "--token=sk-secret"])

    assert value.command == "pytest"
    assert value.args[0] == "-q"
    assert "secret" not in value.args[1]


@pytest.mark.parametrize(
    "value",
    [
        {"command": "pytest -q", "args": []},
        {"command": "pytest", "args": "-q"},
        {"command": "pytest", "args": ["../escape"]},
        {"command": "pytest", "args": ["/tmp/escape"]},
        {"command": "pytest", "args": ["--path=../escape"]},
        {"command": "pytest", "args": ["bad\\path"]},
        {"command": "pytest", "args": ["bad\x00path"]},
        {"command": "pytest;rm", "args": []},
    ],
)
def test_bounded_argv_rejects_shell_strings_and_unsafe_paths(value) -> None:
    with pytest.raises(ValidationError):
        BoundedArgv.model_validate(value)


def test_bounded_argv_rejects_unbounded_arguments() -> None:
    with pytest.raises(ValidationError):
        BoundedArgv(command="pytest", args=["x"] * 65)
    with pytest.raises(ValidationError):
        BoundedArgv(command="pytest", args=["x" * 1_001])


def test_worktree_request_accepts_hex_base_and_relative_allowlist() -> None:
    value = WorktreeRequest(base_commit=BASE_COMMIT, path_allowlist=["src", "tests/test_contracts.py"], max_files=20)

    assert value.base_commit == BASE_COMMIT
    assert value.path_allowlist == ["src", "tests/test_contracts.py"]
    assert value.max_files == 20


@pytest.mark.parametrize("path", ["/absolute", "../escape", "src/../tests", "src\\tests", "src\x00tests", "src//tests"])
def test_worktree_request_rejects_unsafe_allowlist_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        WorktreeRequest(base_commit=BASE_COMMIT, path_allowlist=[path], max_files=1)


def test_worktree_request_rejects_bad_base_or_file_bound() -> None:
    with pytest.raises(ValidationError):
        WorktreeRequest(base_commit="not-a-commit", path_allowlist=["src"], max_files=1)
    with pytest.raises(ValidationError):
        WorktreeRequest(base_commit=BASE_COMMIT, path_allowlist=["src", "src"], max_files=1)
    with pytest.raises(ValidationError):
        WorktreeRequest(base_commit=BASE_COMMIT, path_allowlist=["src"], max_files=1_001)


def test_container_policy_is_default_deny_and_bounded() -> None:
    value = ContainerPolicy(timeout_seconds=60, memory_mb=512)

    assert value.network_disabled is True
    assert value.credentials is False

    with pytest.raises(ValidationError):
        ContainerPolicy(network_disabled=False, timeout_seconds=60, memory_mb=512)
    with pytest.raises(ValidationError):
        ContainerPolicy(credentials=True, timeout_seconds=60, memory_mb=512)
    with pytest.raises(ValidationError):
        ContainerPolicy(timeout_seconds=3_601, memory_mb=512)
    with pytest.raises(ValidationError):
        ContainerPolicy(timeout_seconds=60, memory_mb=15)
