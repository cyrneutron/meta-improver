# Contributing

## Pull Requests

Every pull request must use the repository PR template. The description is part
of the review record, not a release-note placeholder. A reviewer should be able
to determine the intended behavior, evidence, risk, and rollback path without
reconstructing the work from chat or local state.

### Required information

The following sections must be completed for every PR:

- **Summary**: concrete change and motivation.
- **Scope And Risk**: affected layers, impact, risk level, and exclusions.
- **Traceability**: issue, task, decision, base commit, and related changes.
- **Behavioral Contract**: old behavior, new behavior, and rejection behavior.
- **Verification**: exact commands and results, including targeted and full tests.
- **Review Notes**: residual risk, limitations, and reviewer focus.

Write `N/A` with a reason when a field does not apply. Do not leave required
fields empty or replace evidence with claims such as "tested locally".

### Evidence requirements

Changes touching ingestion, contracts, ledger, pipeline, acceptance, sandbox,
scheduler, or publication must also complete **Evidence And Integrity** and
**Security And Operations**. Include stable fixture names, attempt IDs, hashes,
receipt IDs, or other replayable references where available. Raw secrets,
tokens, private URLs, and unredacted external input must never be pasted into a
PR.

Target-repository changes must identify the target repository, target task,
accepted execution ID, accepted commit, and validation evidence. A GitHub CI
result alone does not replace the Harness task evidence or acceptance record.

### Testing standard

Run the same checks as CI before requesting review:

```text
uv sync --frozen
uv run pytest -q
uv run python -m compileall -q src tests
```

Add focused tests for new behavior and rejection paths. Security-sensitive
changes must test malformed input, path/ref boundaries, secret redaction,
timeouts, output limits, retries, and cleanup as applicable. Platform-specific
behavior must either be tested on the affected platform or documented as a
remaining gap.

### Safety and merge policy

PRs must preserve the project's fail-closed behavior. No PR may silently add
automatic merge, protected-branch mutation, unrestricted shell execution,
unbounded subprocess output, host-secret access, or network access to a target
execution environment. Publication changes require explicit acceptance evidence
and a documented rollback path.

Generated projections, local SQLite history, credentials, temporary worktrees,
and unrelated formatting changes must not be committed. Keep each PR focused;
split independent changes so reviewers can evaluate them separately.
