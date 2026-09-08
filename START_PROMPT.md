# MI Codex 启动提示词

你现在负责在 `D:\project\meta-improver` 实施 Meta-Improver。开始任何代码工作前，必须按需阅读：

1. `PLAN.md`
2. `BOOTSTRAP.md`
3. `D:\project\harness-anything\docs-release\start\zh\02-first-loop.md`
4. `D:\project\harness-anything\docs-release\architecture\zh\02-write-path.md`
5. `D:\project\harness-anything\docs-release\architecture\zh\03-projection.md`
6. `D:\project\harness-anything\docs-release\architecture\zh\04-gates-in-the-pipeline.md`

当前已验证的基线（2026-09-08）：HA provider 为 `D:\project\harness-anything`，commit 为
`820b6a762577056ead69023259da0b84d7e7b84a`，CLI version 为 `0.0.1`，build id 为
`6c3fe263-49d5-437f-9f26-93165f5424f8`；Windows Node 为 `v24.11.1`，Python 为 `3.12.10`。
HA target 为 `D:\project\ha-target`，分支为 `codex/windows-provider-runtime-gui-20260908`，commit
为 `e39fb7a6cbe1755bf58cbd19db5c6e0ffdad2299`。优先调用
`D:\project\harness-anything\packages\cli\dist\cli\src\index.js`，不要依赖 PATH 中的全局 `ha`。

`D:\project\harness-anything` 是稳定 CLI provider，保留其既有 `.gitignore` 修改，不作为候选实现
target。`D:\project\ha-target` 是带独立 Harness ledger 和 runtime 的稳定目标路径；目标源码、测试和
目标 Harness 生命周期由该项目 Harness agent/runtime 负责。MI 只负责 target binding、跨项目编排、
证据门禁、publication 约束和人工审阅交接。MI 自己的 `harness/` 负责 MI 开发记忆；
`.improver_history/history.db` 记录 candidate attempt，不能替代任一 Harness ledger。

若未来 MI 修改 HA CLI，治理写入必须继续使用 provider 的稳定版本；候选 CLI 只在 target 沙箱中构建和测试。候选经本地检查、GitHub Actions、独立 review 和 maintainer 合入后，provider 才允许 fast-forward、重建、重跑 contract smoke 并晋升新 build id。单个 Attempt 期间禁止静默切换 provider 版本。

当前 Squad 协作采用追加式报告链，而非实时消息：Reviewer A 的 report r1 由 Reviewer B 读取并
生成 report r1，随后可派发新的 Reviewer A attempt 生成 report r2，最后由 Leader synthesis。每份
report 必须独立、不可变、绑定 attempt/round/input refs；禁止并发覆盖同一文件。当前不实现 worker
中途求助、Leader live steering、跨 provider 消息、共享 inbox/outbox 或 worker selective resume；
worker 失败继续使用 `report -> Leader -> new worker attempt`。

执行任务时：

- 按当前 task plan 确定最小改动面；只有任务明确要求时才修改 MI 环境或依赖元数据。
- 在 MI 根目录使用 provider 的固定 CLI；不要在 provider 源码目录执行 MI init 或目标实现。
- 任务开始前读取 `harness/harness.yaml`、对应 task `task_plan.md` 和其点名文件；machine-readable
  lifecycle 与 relation 使用 `ha` 命令写入，作者文档使用 doc-sync。
- 目标项目操作必须在 `D:\project\ha-target` 的稳定 target identity 下由项目 Harness agent/runtime
  执行；不要替换、重置或直接写目标 Harness ledger。
- 将每次 MI Attempt 绑定到同一 provider commit/build id；发生 provider drift 时 fail-closed。

严格边界：

- 不把 report-driven 审核实现误扩展为消息总线或 CLI session 注入能力。
- 不自动 push、创建 PR 或合入 `main`。
- 不修改 HA 源码，不把 `.harness` 投影当成真相源。
- 不执行未经容器隔离的目标仓库代码；不读取或写入 secrets。
- 所有失败都 fail-closed，并记录原因、已执行命令、残余风险和下一步建议。

工作方式：先给出简短执行计划，再逐项实施；每完成一个阶段运行相关测试并更新 `BOOTSTRAP.md` 或 HA task evidence。最终报告改动文件、命令、测试结果、未完成项和 residual risk。
