## Summary

<!-- 用 2-5 条说明变更了什么，以及为什么现在需要变更。不要只复制 commit message。 -->
- 

## Scope And Risk

- Change type: <!-- bug fix / feature / security / refactor / docs / test / dependency -->
- Affected layers: <!-- ingestion / contracts / ledger / pipeline / sandbox / publication / scheduler / CLI / CI -->
- User-visible or operational impact:
- Risk level: <!-- low / medium / high -->
- Out of scope:

## Traceability

- Issue / task / decision:
- Related PRs or commits:
- Base commit and target branch:
- For target-repository work: repository, target task, and accepted execution ID:

## Behavioral Contract

- Previous behavior:
- New behavior:
- Rejected or fail-closed cases:
- Compatibility or migration impact:

## Evidence And Integrity

<!-- 对涉及 pipeline、proposal、acceptance、ledger 或 target publication 的改动必须填写。 -->

- Input/signal snapshot or fixture:
- Baseline result and evidence reference:
- Attribution / candidate / acceptance evidence:
- Relevant hashes or immutable IDs:
- Replay or idempotency result:
- Secrets and untrusted-input handling:

## Verification

<!-- 填写实际执行的命令和结果；不要只写“tests pass”。 -->

```text
Command:
Result:
```

- [ ] Targeted tests passed
- [ ] Full regression passed
- [ ] `python -m compileall -q src tests` passed when source or tests changed
- [ ] Failure paths and fail-closed behavior tested
- [ ] Platform-specific behavior tested or explicitly explained

## Security And Operations

- [ ] No new shell-string execution or unbounded subprocess output
- [ ] Paths, refs, argv, credentials, and external input remain bounded and validated
- [ ] Network, secrets, permissions, and resource limits reviewed
- [ ] No automatic merge or protected-branch mutation added
- [ ] Timeout, retry, concurrency, crash-recovery, and cleanup impact reviewed
- Operational rollout / monitoring:
- Rollback procedure:

## Review Notes

- Known limitations or residual risk:
- Follow-up work:
- Reviewer focus areas:

## Checklist

- [ ] This PR is limited to one coherent change
- [ ] Documentation and contracts are updated where behavior changed
- [ ] Tests cover the changed behavior and important rejection paths
- [ ] Generated state, local databases, secrets, and unrelated files are not included
- [ ] I have reviewed the complete diff and can explain every changed file
