import pytest

from src.sandbox import (
    BoundedArgv,
    ContainerPolicy,
    ContainerRunRequest,
    FakeContainerTransport,
    FakePatchChecker,
    PatchBundle,
    PatchCheckReceipt,
    PatchCheckStatus,
    PatchEntry,
    RepositorySnapshot,
    SandboxAdmissionError,
    SandboxAdmissionStatus,
    WorktreeDestination,
    WorktreePolicy,
    WorktreeRequest,
    admit_sandbox,
    check_patch,
    plan_patch,
    plan_sandbox_admission,
    plan_worktree,
)


BASE = "a" * 40
SOURCE = "/srv/ha-target"
ROOT = "/srv/mi-worktrees"
DIFF = "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new\n"


def _chain(path: str):
    current = ""
    result = []
    for component in path.split("/")[1:]:
        current += "/" + component
        result.append({"path": current, "kind": "directory"})
    return result


def _inputs():
    request = WorktreeRequest(
        base_commit=BASE,
        path_allowlist=["src"],
        max_files=10,
        repository_id="ha-target",
        source_root=SOURCE,
        worktree_path=f"{ROOT}/attempt-1",
        request_id="attempt-1",
    )
    snapshot = RepositorySnapshot(
        repository_id="ha-target",
        source_root=SOURCE,
        source_root_chain=_chain(SOURCE),
        base_commit=BASE,
        entries=[{"path": "src/main.py", "kind": "file"}],
    )
    policy = WorktreePolicy(
        repository_allowlist=[{"repository_id": "ha-target", "source_root": SOURCE}],
        worktree_root=ROOT,
    )
    destination = WorktreeDestination(path=f"{ROOT}/attempt-1", parent_chain=_chain(ROOT))
    worktree = plan_worktree(request, snapshot, policy, destination)
    bundle = PatchBundle(base_commit=BASE, files=[PatchEntry(path="src/main.py", patch=DIFF)])
    patch = check_patch(plan_patch(bundle, worktree, snapshot), bundle, FakePatchChecker())
    container = ContainerRunRequest(argv=BoundedArgv(command="pytest", args=["-q"]))
    return worktree, patch, container, bundle


def test_admission_plan_and_receipt_are_deterministic():
    worktree, patch, container, _ = _inputs()
    first = plan_sandbox_admission(worktree, patch, container)
    second = plan_sandbox_admission(worktree, patch, container)
    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    receipt = admit_sandbox(first)
    assert receipt.status is SandboxAdmissionStatus.ADMITTED
    assert receipt.plan_hash == first.plan_hash
    assert receipt.model_dump_json(by_alias=True) == admit_sandbox(second).model_dump_json(by_alias=True)


@pytest.mark.parametrize("status", [PatchCheckStatus.FAILED, PatchCheckStatus.REJECTED])
def test_failed_or_rejected_patch_is_fail_closed(status):
    worktree, patch, container, _ = _inputs()
    payload = patch.model_dump(mode="json", by_alias=True)
    payload.update(status=status.value, reason="checker rejected", plan_hash=None)
    rejected = type(patch).model_validate(payload)
    with pytest.raises(SandboxAdmissionError, match="patch check did not pass"):
        plan_sandbox_admission(worktree, rejected, container)


@pytest.mark.parametrize("field,value", [("base_commit", "b" * 40), ("worktree_path", "/srv/other")])
def test_mutated_patch_binding_is_rejected(field, value):
    worktree, patch, container, _ = _inputs()
    setattr(patch, field, value)
    with pytest.raises(SandboxAdmissionError, match="patch plan"):
        plan_sandbox_admission(worktree, patch, container)


def test_mutated_admission_plan_is_rejected_before_receipt():
    worktree, patch, container, _ = _inputs()
    plan = plan_sandbox_admission(worktree, patch, container)
    plan.container_request.workdir = "/workspace/changed"
    with pytest.raises(SandboxAdmissionError, match="admission plan"):
        admit_sandbox(plan)


def test_container_policy_overrides_and_unbounded_requests_are_rejected():
    worktree, patch, _, _ = _inputs()
    unsafe_policy = ContainerPolicy.model_construct(
        network_disabled=False, credentials=False, timeout_seconds=10, memory_mb=128
    )
    unsafe = ContainerRunRequest.model_construct(
        argv=BoundedArgv(command="pytest"),
        policy=unsafe_policy,
    )
    with pytest.raises(SandboxAdmissionError, match="container request"):
        plan_sandbox_admission(worktree, patch, unsafe)


def test_gate_does_not_call_real_or_fake_execution_transports():
    worktree, patch, container, _ = _inputs()
    patch_checker = FakePatchChecker()
    transport = FakeContainerTransport()
    plan = plan_sandbox_admission(worktree, patch, container)
    receipt = admit_sandbox(plan)
    assert receipt.status is SandboxAdmissionStatus.ADMITTED
    assert patch_checker.requests == []
    assert transport.requests == []


def test_admission_rejects_unhashed_or_wrong_contract_objects():
    worktree, patch, container, _ = _inputs()
    with pytest.raises(SandboxAdmissionError):
        plan_sandbox_admission(worktree.model_copy(update={"plan_hash": None}), patch, container)
    with pytest.raises(SandboxAdmissionError):
        plan_sandbox_admission(worktree, object(), container)
