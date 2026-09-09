# Harness Agent Entry

This file contains stable repository operating rules. Current milestone state and task-specific context belong in the active task package.

## Context Loading

- Read `harness/harness.yaml`.
- When a task is assigned, read its `task_plan.md` and only the files it names.
- Route from the task to the smallest relevant context or standard document; do not preload the whole authored tree.

## Worktree Discipline

- Use an isolated worktree and task branch for implementation work.
- Preserve unrelated changes in every checkout and stage only task-owned paths.
- Follow the task's declared base, merge, cleanup, and publication instructions.

## Kernel Workflow

- A task is the work unit and status timeline.
- A fact is an explicit, append-only promotion of a load-bearing observation; facts are optional `0..N`, not a completion quantity gate.
- A decision records the load-bearing why: choices, reversals, long-lived boundaries, and downstream work-spawning judgments.
- Prose mentions do not replace canonical facts, decisions, or relations.

## Relation Rules

- Write relations with canonical IDs.
- Use `derives` when a decision directly spawned a task and `relates` when a connection was identified later.
- Use `refines` only for decision-to-decision revision.

## Write Coordination

- Use Harness commands for machine-read fields, lifecycle changes, and relations.
- Follow repository doc-sync policy for registered authored prose.
- Generated state under `.harness/` is local-only and must not be committed.

## Harness CLI (software/coding)

- Use `ha <command>` or `npx harness-anything <command>` and inspect command help before composing writes.
- Create task packages with `ha task create --title "<title>"`; do not hand-scaffold task directories.
- Select from the effective catalog with `ha preset list`. Packages reported unavailable must not be used to publish guidance or create a task.
- Milestone creation requires an explicit `--task-class milestone`; the preset ID does not infer task class.

## Repository Scaffolds

- Context lives under `harness/context/`.
- Standards live only under `harness/governance/standards/`.
- ADR projections live under `harness/adr/`, milestone documents under `harness/milestones/`, and canonical decision packages under `harness/decisions/`.
- Read each folder's README instead of duplicating its rules here.

## Architecture-aware Changes

- Before broad source search, check for `harness/context/architecture/architecture-manifest.json`.
- If present, read the architecture README and only the relevant stable view or flow before choosing an implementation layer.
- If absent, architecture remains opt-in and ordinary coding work continues without a fabricated model.

## Governance Routing

- Repository workflow and preservation: `harness/governance/standards/repository-governance.md`.
- Decision writing: `harness/governance/standards/decision-writing.md`.
- Load only standards applicable to the current task.

## Script Discovery

- Use `ha script list` and `ha script inspect <id>` to inspect vertical script declarations.
- A declaration is not proof of execution support. Run a script only when inspection explicitly reports execution as available.

## Repository Specifics

### Terminology and Execution Routing

Do not use "HA repository" without a project qualifier. Use these terms:

- MI source repository: the Git repository containing Meta-Improver code.
- MI Harness repository: MI's own nested harness ledger and canonical project memory.
- <project> source repository: the project's code Git repository, including its remotes and refs.
- <project> Harness repository: the project's independent nested harness ledger, runtime state, and local coordination records.
- <project> stable target path: the authoritative checkout that keeps the project's source tree and its Harness identity together.
- project Harness agent/runtime: the executor attached to that project's stable target path and Harness ledger. It is not an alias for the current MI conversation or for MI itself.

Use an explicit binding sentence when discussing an operation:

> In <project>'s stable target path <absolute-path>, the project Harness agent/runtime executes the project task; MI verifies source synchronization, target Harness identity, evidence gates, publication constraints, and human-review handoff.

Use an explicit dispatch sentence when handing work to another executor:

> Dispatch this task to the <project> Harness agent/runtime in <absolute-path>; preserve the nested Harness ledger and local runtime state, and return the project task execution, commit, test evidence, review state, and residual risks to MI.

Ownership is strict: MI may supervise and verify, but must not directly write a target project's Harness ledger. A source-only linked worktree is an implementation workspace, never a replacement for the project's stable target identity.

## Execution Cadence

- Unless a task explicitly requests checkpoints, continue through all safe, authorized in-scope plan steps in one work cycle.
- Ordinary milestone completion is a reporting point, not a pause or approval gate.
- Pause only for permissions, external blockers, irreversible actions, architecture-boundary choices, or required user decisions.
- End-of-cycle reports summarize cumulative progress and do not imply that work is waiting for confirmation.
