# MI Codex 启动提示词

你现在负责在 `/home/cyr/projects/meta-improver` 实施 Meta-Improver。开始任何代码工作前，必须完整阅读：

1. `PLAN.md`
2. `BOOTSTRAP.md`
3. `/home/cyr/projects/harness-anything/docs-release/start/zh/02-first-loop.md`
4. `/home/cyr/projects/harness-anything/docs-release/architecture/zh/02-write-path.md`
5. `/home/cyr/projects/harness-anything/docs-release/architecture/zh/03-projection.md`
6. `/home/cyr/projects/harness-anything/docs-release/architecture/zh/04-gates-in-the-pipeline.md`

当前已验证的基线：HA commit 为 `b47b93b1a28408ded2849792f8fad55963c30713`，CLI build id 为 `5fa644bc-4a6c-44a0-8a7d-f7897a1877a6`，Python 使用 `/home/cyr/projects/meta-improver/.venv/bin/python`（3.12.14）。优先调用 `/home/cyr/projects/harness-anything/packages/cli/dist/cli/src/index.js`，不要依赖 PATH 中可能指向其他 checkout 的全局 `ha`。

当前 `/home/cyr/projects/harness-anything` 是稳定 CLI provider：保持干净，只负责读取上游提交和提供已晋升的 `ha` CLI，不初始化目标账本，也不承载候选改动。公开源库提交可以从其 `origin` 读取。MI 自己的 `harness/` 负责 MI 开发记忆；等 MI 达到 Phase 2/3、第一次正式治理 HA 之前，再在 `/home/cyr/projects/ha-target`（或等价独立 target clone）中初始化 HA 的 `harness/`。MI 的 `.improver_history/history.db` 记录所有候选 attempt，不能替代任一 `harness/`。

若未来 MI 修改 HA CLI，治理写入必须继续使用 provider 的稳定版本；候选 CLI 只在 target 沙箱中构建和测试。候选经本地检查、GitHub Actions、独立 review 和 maintainer 合入后，provider 才允许 fast-forward、重建、重跑 contract smoke 并晋升新 build id。单个 Attempt 期间禁止静默切换 provider 版本。

你的第一轮任务只做 Phase 0 和 Phase 1：

- 检查并完善 MI 外层 Git、Python/uv 环境和 `pyproject.toml`/`uv.lock`。
- 在 MI 根目录初始化 HA：使用当前 CLI 的 `--repo-id`、`--person-id`、`--display-name` 参数，不能在 `harness-anything` 源码目录执行 init。
- 验证 `harness/`、`people.yaml`、`.harness/`、daemon registration 和基本 task/fact/projection smoke。
- 用 HA 账本记录本轮 task、事实、决策和验证证据；不要手写状态字段。
- 完成后再实现最小配置模型、Pydantic 数据契约和 SQLite 经验账本，带单元测试和迁移/幂等测试。

不要在第一轮初始化 HA 的目标项目账本；那一步属于 MI 具备只读诊断和受控沙箱能力之后的后续阶段。届时必须使用独立目标 clone/worktree，先记录 HA 源库 `base_commit`，再为每个正式改进创建 HA task 和验证证据。

严格边界：

- 不实现 CI/Issue 自动修复、proposal PR、daemon 常驻或 self-evolve，直到对应 Phase 到位。
- 不自动 push、创建 PR 或合入 `main`。
- 不修改 HA 源码，不把 `.harness` 投影当成真相源。
- 不执行未经容器隔离的目标仓库代码；不读取或写入 secrets。
- 所有失败都 fail-closed，并记录原因、已执行命令、残余风险和下一步建议。

工作方式：先给出简短执行计划，再逐项实施；每完成一个阶段运行相关测试并更新 `BOOTSTRAP.md` 或 HA task evidence。最终报告改动文件、命令、测试结果、未完成项和 residual risk。
