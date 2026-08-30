from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.self_evolve import (
    EvalSnapshot,
    PromptCandidate,
    SelfEvolveError,
    SelfEvolveStatus,
    evaluate_self_evolve,
    plan_self_evolve,
    rehydrate_eval_snapshot,
    rehydrate_prompt_candidate,
    rehydrate_self_evolve_plan,
    rehydrate_self_evolve_receipt,
)


HASH = "sha256:" + "a" * 64
PARENT = "b" * 40


def candidate(**overrides: object) -> PromptCandidate:
    values: dict[str, object] = {
        "parent_commit": PARENT,
        "prompt_version": "prompt-v2",
        "rules_version": "rules-v2",
        "rollback_ref": "refs/heads/mi-rollback-v1",
    }
    values.update(overrides)
    return PromptCandidate(**values)


def snapshot(
    *,
    baseline: list[float] | None = None,
    holdout: list[float] | None = None,
    cost: float = 2.0,
    dataset_hash: str = HASH,
) -> EvalSnapshot:
    return EvalSnapshot(
        dataset_hash=dataset_hash,
        baseline_scores=baseline or [0.50],
        holdout_scores=holdout or [0.50],
        cost_units=cost,
    )


def plan(**overrides: object):
    values: dict[str, object] = {
        "candidate": candidate(),
        "baseline_eval": snapshot(),
        "holdout_eval": snapshot(baseline=[0.52], holdout=[0.65]),
        "repeat_runs": 1,
        "min_gain": 0.10,
        "max_cost": 10.0,
    }
    values.update(overrides)
    return plan_self_evolve(**values)


def strict_snapshot(
    candidate_value: PromptCandidate,
    *,
    split: str = "baseline",
    manifest: str = HASH,
    evaluator_version: str = "evaluator-v1",
    **overrides: object,
) -> EvalSnapshot:
    values: dict[str, object] = {
        "dataset_manifest": manifest,
        "split": split,
        "candidate": candidate_value,
        "prompt_version": candidate_value.prompt_version,
        "rules_version": candidate_value.rules_version,
        "evaluator_version": evaluator_version,
        "parent_commit": candidate_value.parent_commit,
        "rollback_ref": candidate_value.rollback_ref,
        "baseline_scores": [0.50],
        "holdout_scores": [0.50],
        "cost_units": 1.0,
    }
    values.update(overrides)
    return EvalSnapshot(**values)


def test_acceptance_is_deterministic_and_replayable() -> None:
    first = plan()
    second = plan()
    assert first.model_dump_json(by_alias=True) == second.model_dump_json(by_alias=True)
    assert rehydrate_self_evolve_plan(first).model_dump() == first.model_dump()

    first_receipt = evaluate_self_evolve(first)
    second_receipt = evaluate_self_evolve(second)
    assert first_receipt.status is SelfEvolveStatus.ACCEPTED
    assert first_receipt.model_dump_json(by_alias=True) == second_receipt.model_dump_json(by_alias=True)
    assert rehydrate_self_evolve_receipt(first_receipt).model_dump() == first_receipt.model_dump()


def test_candidate_and_snapshot_hashes_are_canonical() -> None:
    value = candidate()
    snapshot_value = snapshot()
    assert value.candidate_hash is not None
    assert snapshot_value.eval_hash is not None
    assert rehydrate_prompt_candidate(value).model_dump() == value.model_dump()
    assert rehydrate_eval_snapshot(snapshot_value).model_dump() == snapshot_value.model_dump()


def test_strict_snapshot_binds_manifest_split_and_candidate_identity() -> None:
    value = strict_snapshot(candidate(), split="holdout")
    assert value.dataset_manifest == HASH
    assert value.dataset_hash == HASH
    assert value.candidate_hash == candidate().candidate_hash
    assert value.eval_hash is not None
    assert rehydrate_eval_snapshot(value).model_dump() == value.model_dump()


@pytest.mark.parametrize(
    "field",
    [
        "dataset_manifest",
        "split",
        "candidate_hash",
        "prompt_version",
        "rules_version",
        "evaluator_version",
        "parent_commit",
        "rollback_ref",
    ],
)
def test_strict_snapshot_missing_identity_fails_closed(field: str) -> None:
    payload = strict_snapshot(candidate()).model_dump(mode="json", by_alias=True)
    payload.pop(field)
    with pytest.raises(ValidationError):
        EvalSnapshot.model_validate(payload)


def test_strict_snapshot_rejects_oversized_identity_and_tampering() -> None:
    base = strict_snapshot(candidate()).model_dump(mode="json", by_alias=True)
    with pytest.raises(ValidationError):
        EvalSnapshot.model_validate({**base, "prompt_version": "p" * 129})
    with pytest.raises(ValidationError):
        EvalSnapshot.model_validate({**base, "rollback_ref": "r" * 257})
    with pytest.raises(ValidationError):
        EvalSnapshot.model_validate({**base, "evaluator_version": "e" * 129})

    value = strict_snapshot(candidate())
    object.__setattr__(value, "parent_commit", "c" * 40)
    with pytest.raises(SelfEvolveError, match="EvalSnapshot"):
        rehydrate_eval_snapshot(value)


def test_plan_rejects_mismatched_strict_snapshot_bindings() -> None:
    first = candidate()
    second = candidate(prompt_version="prompt-v3")
    with pytest.raises(SelfEvolveError, match="candidate"):
        plan_self_evolve(
            first,
            strict_snapshot(first, split="baseline"),
            strict_snapshot(second, split="holdout"),
        )

    with pytest.raises(SelfEvolveError, match="same dataset"):
        plan_self_evolve(
            first,
            strict_snapshot(first, split="baseline"),
            strict_snapshot(first, split="holdout", manifest="sha256:" + "c" * 64),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("parent_commit", "not-a-commit"),
        ("prompt_version", "src/../code"),
        ("rules_version", "rules;run"),
        ("rollback_ref", "../rollback"),
        ("rollback_ref", "/tmp/rollback"),
        ("rollback_ref", "refs\\heads\\rollback"),
    ],
)
def test_candidate_l1_and_rollback_fields_are_bounded(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        candidate(**{field: value})


def test_candidate_rejects_code_or_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        candidate(files=["src/unsafe.py"])
    with pytest.raises(ValidationError):
        candidate(prompt_version="prompt-v2; subprocess.run()")


@pytest.mark.parametrize(
    ("baseline_scores", "candidate_baseline_scores", "candidate_holdout_scores", "message"),
    [
        ([0.50], [0.51], [0.55], "holdout gain"),
        ([0.60], [0.55], [0.65], "baseline regression"),
    ],
)
def test_gain_and_regression_gates_reject(
    baseline_scores: list[float],
    candidate_baseline_scores: list[float],
    candidate_holdout_scores: list[float],
    message: str,
) -> None:
    value = plan(
        baseline_eval=snapshot(baseline=baseline_scores, holdout=baseline_scores),
        holdout_eval=snapshot(baseline=candidate_baseline_scores, holdout=candidate_holdout_scores),
    )
    receipt = evaluate_self_evolve(value)
    assert receipt.status is SelfEvolveStatus.REJECTED
    assert message in receipt.reason


def test_cost_gate_rejects() -> None:
    value = plan(
        baseline_eval=snapshot(cost=8),
        holdout_eval=snapshot(baseline=[0.52], holdout=[0.65], cost=3),
        max_cost=10,
    )
    receipt = evaluate_self_evolve(value)
    assert receipt.status is SelfEvolveStatus.REJECTED
    assert "cost" in receipt.reason


def test_repeatability_requires_matching_run_count_and_bounded_variance() -> None:
    value = plan(
        repeat_runs=3,
        baseline_eval=snapshot(baseline=[0.50, 0.50, 0.50], holdout=[0.50, 0.50, 0.50]),
        holdout_eval=snapshot(baseline=[0.51, 0.51, 0.51], holdout=[0.60, 0.61, 0.60]),
    )
    assert evaluate_self_evolve(value).status is SelfEvolveStatus.ACCEPTED

    unstable = plan(
        repeat_runs=3,
        baseline_eval=snapshot(baseline=[0.50, 0.50, 0.50], holdout=[0.50, 0.50, 0.50]),
        holdout_eval=snapshot(baseline=[0.51, 0.51, 0.51], holdout=[0.55, 0.90, 0.60]),
    )
    receipt = evaluate_self_evolve(unstable)
    assert receipt.status is SelfEvolveStatus.REJECTED
    assert "repeatability" in receipt.reason

    wrong_count = plan(
        repeat_runs=2,
        baseline_eval=snapshot(),
        holdout_eval=snapshot(baseline=[0.52], holdout=[0.65]),
    )
    with pytest.raises(SelfEvolveError, match="score count"):
        evaluate_self_evolve(wrong_count)


def test_any_baseline_run_regression_is_rejected_even_when_mean_improves() -> None:
    value = plan(
        repeat_runs=2,
        baseline_eval=snapshot(baseline=[0.50, 0.80], holdout=[0.50, 0.50]),
        holdout_eval=snapshot(baseline=[0.40, 1.10], holdout=[0.65, 0.65]),
    )
    receipt = evaluate_self_evolve(value)
    assert receipt.status is SelfEvolveStatus.REJECTED
    assert "baseline regression" in receipt.reason


def test_dataset_hash_must_not_mix_baseline_and_holdout() -> None:
    with pytest.raises(SelfEvolveError, match="same dataset hash"):
        plan(holdout_eval=snapshot(dataset_hash="sha256:" + "c" * 64, baseline=[0.52], holdout=[0.65]))


def test_mutation_and_supplied_hashes_fail_closed() -> None:
    value = candidate()
    object.__setattr__(value, "prompt_version", "mutated")
    with pytest.raises(SelfEvolveError, match="PromptCandidate"):
        rehydrate_prompt_candidate(value)

    value = snapshot()
    object.__setattr__(value, "holdout_scores", [0.99])
    with pytest.raises(SelfEvolveError, match="EvalSnapshot"):
        rehydrate_eval_snapshot(value)

    value = plan()
    object.__setattr__(value, "candidate", candidate(prompt_version="prompt-v3"))
    with pytest.raises(SelfEvolveError, match="plan"):
        rehydrate_self_evolve_plan(value)


def test_unknown_snapshot_fields_and_nonfinite_scores_reject() -> None:
    with pytest.raises(ValidationError):
        EvalSnapshot.model_validate({**snapshot().model_dump(), "unknown": "field"})
    with pytest.raises(ValidationError):
        EvalSnapshot(dataset_hash=HASH, baseline_scores=[float("nan")], holdout_scores=[0.5], cost_units=1)


def test_mapping_scores_are_sorted_and_supported() -> None:
    baseline = snapshot()
    candidate_eval = EvalSnapshot(
        dataset_hash=HASH,
        baseline_scores={"accuracy": 0.51, "f1": 0.52},
        holdout_scores={"accuracy": 0.70, "f1": 0.72},
        cost_units=2,
    )
    value = plan(
        baseline_eval=EvalSnapshot(
            dataset_hash=HASH,
            baseline_scores={"accuracy": 0.50, "f1": 0.50},
            holdout_scores={"accuracy": 0.50, "f1": 0.50},
            cost_units=2,
        ),
        holdout_eval=candidate_eval,
        min_gain=0.1,
    )
    assert evaluate_self_evolve(value).status is SelfEvolveStatus.ACCEPTED
