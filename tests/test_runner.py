import pytest
from pydantic import ValidationError

from src.sandbox import (
    BoundedArgv,
    ContainerPolicy,
    ContainerRunRequest,
    ContainerRunStatus,
    ContainerRunner,
    ContainerTransportReceipt,
    FakeContainerTransport,
)


def request(*args: str, **kwargs) -> ContainerRunRequest:
    return ContainerRunRequest(argv=BoundedArgv(command="pytest", args=list(args)), **kwargs)


def test_fake_runner_is_deterministic_and_preserves_empty_output() -> None:
    transport = FakeContainerTransport(default=ContainerTransportReceipt(exit_code=0, stdout="", stderr=""))
    runner = ContainerRunner(transport)
    first = runner.run(request("-q"))
    second = runner.run(request("-q"))

    assert first == second
    assert first.status is ContainerRunStatus.SUCCEEDED
    assert first.stdout == "" and first.stderr == ""
    assert transport.requests == [first.request_digest, first.request_digest]


def test_runner_normalizes_exit_code_and_redacts_output() -> None:
    transport = FakeContainerTransport(
        default=ContainerTransportReceipt(exit_code=3, stdout="token=topsecret", stderr="password=hunter2")
    )
    result = ContainerRunner(transport).run(request("-q"))

    assert result.status is ContainerRunStatus.FAILED
    assert result.exit_code == 3
    assert "topsecret" not in result.stdout
    assert "hunter2" not in result.stderr


def test_timeout_and_memory_limits_fail_closed() -> None:
    timeout = ContainerRunner(
        FakeContainerTransport(default=ContainerTransportReceipt(duration_seconds=11))
    ).run(request(policy=ContainerPolicy(timeout_seconds=10, memory_mb=128)))
    memory = ContainerRunner(
        FakeContainerTransport(default=ContainerTransportReceipt(peak_memory_mb=129))
    ).run(request(policy=ContainerPolicy(timeout_seconds=10, memory_mb=128)))

    assert timeout.status is ContainerRunStatus.TIMED_OUT
    assert memory.status is ContainerRunStatus.RESOURCE_EXCEEDED


def test_transport_policy_violation_is_rejected() -> None:
    result = ContainerRunner(
        FakeContainerTransport(default=ContainerTransportReceipt(network_disabled=False))
    ).run(request())
    assert result.status is ContainerRunStatus.REJECTED
    assert result.exit_code is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"policy": {"network_disabled": False, "timeout_seconds": 10, "memory_mb": 128}},
        {"policy": {"credentials": True, "timeout_seconds": 10, "memory_mb": 128}},
        {"environment": {"API_TOKEN": "sk-secret"}},
        {"workdir": "/workspace/../escape"},
    ],
)
def test_request_rejects_unsafe_overrides(kwargs) -> None:
    with pytest.raises((ValidationError, ValueError)):
        request(**kwargs)


def test_runner_does_not_accept_shell_string() -> None:
    with pytest.raises((ValidationError, ValueError)):
        ContainerRunRequest(argv="pytest -q")


def test_request_rejects_secret_shaped_stdin() -> None:
    with pytest.raises((ValidationError, ValueError)):
        request(stdin="token=sk-secret")


def test_receipt_requires_exit_code_unless_timed_out() -> None:
    with pytest.raises(ValidationError):
        ContainerTransportReceipt(exit_code=None)
    assert ContainerTransportReceipt(exit_code=None, timed_out=True).exit_code is None


def test_runner_rehydrates_mutated_request_before_transport() -> None:
    value = request("-q")
    value.policy.network_disabled = False

    with pytest.raises(ValueError, match="changed after validation"):
        ContainerRunner(FakeContainerTransport()).run(value)


class RaisingTransport:
    def run(self, request):
        raise RuntimeError("secret=must-not-leak")


class MutatingReceiptTransport:
    def __init__(self):
        self.receipt = ContainerTransportReceipt(exit_code=0)

    def run(self, request):
        self.receipt.timed_out = True
        self.receipt.exit_code = 0
        self.receipt.network_disabled = False
        self.receipt.stdout = "token=sk-secret"
        return self.receipt


def test_transport_exception_is_redacted_fail_closed() -> None:
    result = ContainerRunner(RaisingTransport()).run(request())

    assert result.status is ContainerRunStatus.REJECTED
    assert result.reason == "transport failure"
    assert result.stdout == "" and result.stderr == ""
    assert "secret" not in result.model_dump_json()


def test_runner_rehydrates_mutated_transport_receipt() -> None:
    result = ContainerRunner(MutatingReceiptTransport()).run(request())

    assert result.status is ContainerRunStatus.REJECTED
    assert result.reason == "transport violated deny-by-default policy"
    assert result.stdout == ""
