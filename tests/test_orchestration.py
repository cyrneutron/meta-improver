import pytest

from src.sandbox import (
    BoundedArgv,
    ContainerRunRequest,
    ContainerRunResult,
    ContainerRunStatus,
    FakePatchChecker,
    PatchBundle,
    PatchEntry,
    RepositorySnapshot,
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
from src.sandbox.orchestration import (
    FakeSandboxOrchestrator,
    OrchestrationError,
    SandboxOrchestrationStatus,
    complete_sandbox_orchestration,
    plan_sandbox_orchestration,
    rehydrate_sandbox_orchestration_plan,
    rehydrate_sandbox_orchestration_receipt,
)


BASE = "a" * 40
SOURCE = "/srv/ha-target"
ROOT = "/srv/mi-worktrees"
DIFF = "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-old\n+new\n"


def _chain(path: str):
    current = ""
    return [{"path": (current := current + "/" + item), "kind": "directory"} for item in path.split("/")[1:]]


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
    admission_plan = plan_sandbox_admission(worktree, patch, container)
    admission_receipt = admit_sandbox(admission_plan)
    result = ContainerRunResult(
        status=ContainerRunStatus.SUCCEEDED,
        request_digest=container.request_digest,
        exit_code=0,
        stdout="ok",
    )
    return admission_plan, admission_receipt, result


def test_fake_orchestration_composes_deterministic_hashed_protocols():
    admission_plan, admission_receipt, result = _inputs()
    first_plan = plan_sandbox_orchestration(admission_plan, admission_receipt)
    second_plan = plan_sandbox_orchestration(admission_plan, admission_receipt)
    assert first_plan.plan_hash is not None
    assert first_plan.model_dump_json(by_alias=True) == second_plan.model_dump_json(by_alias=True)

    first = complete_sandbox_orchestration(first_plan, result)
    second = FakeSandboxOrchestrator(result).run(second_plan)
    assert first.status is SandboxOrchestrationStatus.SUCCEEDED
    assert first.success
    assert first.receipt_hash is not None
    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)


def test_only_admitted_and_succeeded_are_accepted():
    admission_plan, admission_receipt, result = _inputs()
    plan = plan_sandbox_orchestration(admission_plan, admission_receipt)
    failed = result.model_copy(update={"status": ContainerRunStatus.FAILED, "exit_code": 1})
    with pytest.raises(OrchestrationError, match="succeeded container result"):
        complete_sandbox_orchestration(plan, failed)

    rejected_receipt = admission_receipt.model_copy(update={"status": SandboxAdmissionStatus.REJECTED})
    with pytest.raises(OrchestrationError):
        plan_sandbox_orchestration(admission_plan, rejected_receipt)


def test_plan_and_receipt_rehydrate_and_reject_mutation():
    admission_plan, admission_receipt, result = _inputs()
    plan = plan_sandbox_orchestration(admission_plan, admission_receipt)
    plan.admission_plan.container_request.workdir = "/workspace/changed"
    with pytest.raises(OrchestrationError, match="plan"):
        rehydrate_sandbox_orchestration_plan(plan)

    clean_plan = plan_sandbox_orchestration(admission_plan, admission_receipt)
    receipt = complete_sandbox_orchestration(clean_plan, result)
    receipt.run_result.stdout = "mutated"
    with pytest.raises(OrchestrationError, match="receipt"):
        # Rehydration catches the result hash mismatch before trusting output.
        rehydrate_sandbox_orchestration_receipt(receipt)


def test_fake_orchestrator_has_no_execution_fallback():
    admission_plan, admission_receipt, _ = _inputs()
    plan = FakeSandboxOrchestrator().plan(admission_plan, admission_receipt)
    with pytest.raises(OrchestrationError, match="result fixture"):
        FakeSandboxOrchestrator().run(plan)
