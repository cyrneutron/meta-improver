# Meta-Improver 实施方案

本文件是 MI 项目在当前阶段的执行入口。先完成 Phase 0/1，再逐步开放后续能力；任何阶段失败都 fail-closed，不得跳过门禁扩大权限。

## 目标与边界

`meta-improver` 是运行在 Harness Anything（HA）之上的外部改进器，用于摄入 CI/Issue/local log 信号，生成根因假设和候选补丁，在隔离环境中验证并产出可审查证据。

### 术语与执行边界

后文中的“HA”必须带项目限定词。为避免把多个项目的身份混在一起，统一使用：

- MI 源仓库：Meta-Improver 的 Git 源码仓库。
- MI Harness 仓库：MI 自己的 harness，记录 MI 的任务、事实、决策、评审和结项。
- 项目源仓库：被改进项目的 Git 仓库、remote 和 ref。
- 项目 Harness 仓库：该项目独立的 harness 台账、.harness 运行态和本地 writer/daemon 协调状态。
- 项目稳定目标路径：同时承载项目源树和项目 Harness 身份的权威 checkout，例如 D:\project\ha-target。
- 项目 Harness agent/runtime：绑定到该项目稳定目标路径并负责项目内实现、测试和项目 Harness 生命周期的执行者；不能简称为“当前 agent”。

推荐描述目标：

> 在 <项目名> 的项目稳定目标路径 <绝对路径> 中，由该项目的 Harness agent/runtime 执行源码和测试任务；MI 负责目标绑定验证、跨项目编排、证据门禁、publication 约束和人工审阅交接。

推荐描述派工：

> 请通过 <项目名> 的 Harness agent/runtime，在 <绝对路径> 执行该项目任务；保留嵌套 Harness 台账、.harness 运行态及 writer/daemon 协调状态，并将 task execution、commit、测试证据、review 状态和 residual risk 回传 MI。

执行归属按项目边界分开：MI 自身代码和 MI Harness 任务由 MI 执行者处理；目标项目的源码、测试和目标项目 Harness 生命周期由目标项目 Harness agent/runtime 处理。MI 不直接写目标项目 Harness 仓库，也不以 source-only linked worktree 替代稳定目标路径。

必须遵守：

- HA `harness/` 文档、canonical events 和任务 artifacts 是真相源；`.harness/` SQLite/HTML 只是可重建投影。
- MI 默认 proposal-only：只生成报告、patch 和 PR payload；显式批准后才允许 push/create PR，永不自动合入 `main`。
- 执行节奏采用连续推进：在授权范围内连续完成普通计划步骤；里程碑汇报不构成暂停。仅在权限、外部阻塞、不可逆操作、架构边界或需要用户裁决时停下。
- Worktree 只提供 Git 隔离。目标代码必须在 Docker/Podman 容器中运行，默认禁网、无 secrets，并设置 CPU/内存/磁盘/时间限制。
- 每次尝试记录 `base_commit`、输入快照哈希、模型/Prompt 版本、patch 哈希、测试结果、幂等键和回滚点。
- self-evolve 首阶段只允许离线修改 L1 Prompt/规则；权限、审计、门禁、沙箱和凭据处理属于 immutable policy core。

## 当前基线（2026-09-09）

MI 当前绑定的是 Windows 本地 provider/target，而不是早期 Linux 记录：

- MI 源仓库：`D:\project\meta-improver`，当前分支 `codex/immutable-report-chain-contract`。
- HA provider：`D:\project\harness-anything`，HEAD `c4330a85d06ed3a650965b26cb86d0aad49edc48`。
- HA CLI version：`0.0.1`；CLI build id：`1942211c-e727-4c18-867a-268bba08ba12`。
- Node.js：`v24.11.1`；Python：`3.12.10`。
- HA 本地 CLI：`D:\project\harness-anything\packages\cli\dist\cli\src\index.js`，
  通过 Windows `node` 执行；provider daemon loaded/disk build id 一致，`drifted=false`。
- HA target：`D:\project\ha-target`，当前源库分支
  `codex/ha-target-latest-20260909`，HEAD 与 `upstream/main` 均为
  `c4330a85d06ed3a650965b26cb86d0aad49edc48`；旧分支
  `codex/windows-provider-runtime-gui-20260908` 及其 `e39fb7a6...` 仍保留。
- 目标同步未形成新的目标 task execution：目标 Harness runtime 在启动进程前失败，随后 MI
  在 daemon `queueDepth=0` 且目标 RepoCell attached 的条件下执行了受控 source ref fast-forward，
  未写入目标 `harness/` 或 `.harness`；嵌套项目 Harness identity 保持不变。
- 目标项目后续源码、测试和 Harness 生命周期仍由该项目 Harness agent/runtime 负责；MI 只做
  身份、同步和证据核验。
- provider 工作树保留既有 `.gitignore` 修改；MI 不覆盖、不回退该修改。
- 详细历史验证与当前 binding 记录：见 [BOOTSTRAP.md](./BOOTSTRAP.md)。

不要依赖 PATH 中的全局 `ha`；它可能来自另一份 checkout。升级 HA 时必须重新记录 commit/build id 并重跑 contract/smoke tests。

## 双账本与源库边界

当前 `D:\project\harness-anything` 是 MI 的稳定 CLI provider；它与
`D:\project\ha-target` 分离。provider 的既有工作树修改必须保留，不能把 provider 当作候选实现目标。
target 的源代码和独立 Harness 身份绑定在同一稳定路径中。MI 配置必须使用明确的绝对路径、仓库
identity、ref 和 build stamp，不依赖 PATH 中的全局 `ha` 或 remote 名称语义。

源库的公开提交可以通过该 clone 读取。每次 MI 处理 HA 任务时，应先记录源库 `base_commit`，再从上游同步/校验目标 checkout；候选变更只在目标 checkout 的临时 worktree 中进行。源库提交是代码基线，不是 MI 或 HA 的记忆来源。

系统同时维护三类记忆，职责不能混淆：

- **MI 项目的 `harness/`**：记录 MI 自身的开发任务、设计决策、事实、评审和结项。
- **HA 目标项目的 `harness/`**：位于 `D:\project\ha-target`，记录每个 HA 改进任务、复现事实、候选变更、验证证据、review 和 closeout。该目录是私有嵌套账本，不默认进入公开代码 PR。
- **MI 的 `.improver_history/history.db`**：记录所有候选 attempt（成功、失败、拒绝、重试）、模型/Prompt 版本、输入和 patch 哈希；这是 MI 的运行时经验库，不替代任一 HA 账本。

2026-09-02 起，该 ledger 使用 schema v3：`attempts` 保留最新快照，`attempt_events` 保留追加式阶段证据。`ha squad-diagnose` 会将受控 Squad diagnosis 与一条可重放的 captured Attempt 绑定，后续 pipeline 使用同一 identity 推进 baseline、attribution、patch validation 和 acceptance。当前 diagnosis 命令以 diagnosis record hash 作为 handoff signal；已存在但无法反推上游 signal 的 6 条 diagnosis 不会被自动虚构成 Attempt。

公开 PR、GitHub Actions 结果和 issue 讨论是审查证据。它们需要被 MI 脱敏、引用并写入相应 task/evidence，但不能替代 `harness/` 的权威记录。GitHub Actions 通过也不会自动完成 HA task；仍需按当前 HA 版本完成 execution、review、consent 和 completion 门禁。

MI 的每次目标改进都必须先形成目标绑定元组：项目稳定目标路径、项目源仓库及 source ref、publication repository/remote、项目 Harness root/name/revision、.harness runtime root，以及 writer lock/daemon/agent 协调状态。同步源库前先保存旧工作和绑定快照；只有确认项目 Harness 身份未被替换、账本干净且 writer 已静默，才允许在稳定目标路径上推进 source ref。目标项目的实现和测试通过项目 Harness agent/runtime 完成，MI 只消费其结构化证据并执行跨项目门禁。

## 稳定 CLI 提供者与可变目标 clone

HA 使用两个彼此隔离的 checkout：

- **CLI provider**：`D:\project\harness-anything`。只提供当前已晋升的稳定 `ha` CLI；不承载 MI 候选改动。
- **HA target**：`D:\project\ha-target`。承载目标项目源树、目标 Harness ledger 和本地 runtime；目标实现与目标 Harness 生命周期由目标项目 Harness agent/runtime 负责。

MI 的治理写入始终调用 CLI provider 的稳定 CLI。若 MI 修改 HA CLI 本身，候选 CLI 只能在 HA target 的受控沙箱中构建和测试，不能负责裁决或记录自己的改动。

稳定 CLI 的晋升流程：

1. CLI provider 固定稳定版本 `N` 的 commit/build id，并治理 HA target 上的候选 `N+1`。
2. 候选在本地容器通过 contract、回归和安全检查，再创建 proposal PR。
3. 上游 GitHub Actions 和独立 review 通过，PR 由 maintainer 合入。
4. CLI provider 执行 `git fetch` 和 `git merge --ff-only` 更新到已合入 commit，重新 `npm ci`、build，并记录新的 commit/build id。
5. 重跑 bootstrap/contract smoke；全部通过后才把新 CLI 晋升为稳定版本，供下一轮 MI 任务使用。

单个 Attempt 从开始到完成必须绑定同一个 CLI provider commit/build id；运行中检测到 provider 漂移时 fail-closed，不能静默切换版本。

## Squad 协作边界：追加式报告链

当前 HA Squad 的协作基础是 worker terminal report、Leader callback 和后续新 attempt，不是
worker 间实时消息。MI 的 Review/Verify 场景采用顺序多轮编排：

```text
Reviewer A report r1
  -> Reviewer B report r1（读取并质疑 A r1）
    -> Reviewer A report r2（读取 B r1 后复核）
      -> Leader synthesis
```

每个 report 是一个不可变 artifact，绑定 worker attempt、round、reviewer、输入 report ref 和
目标 source/Harness identity。后续 reviewer 只读取先前 artifact，并写入新的 artifact；禁止两个
worker 并发原地覆盖同一个 report。Leader 在 callback 中读取 `resultRef` 和 `reportPath`，将前序
报告的受控引用或正文纳入下一轮 prompt，并最终在唯一的 synthesis report 中给出裁决。

这不是 Message Bus：当前不建设 worker 中途求助、Leader 对运行中 worker 的 live steering、
跨 provider 的实时消息、共享 inbox/outbox，或 worker 失败后的 selective resume。worker 失败或
无法推进时，继续采用 HA 现有的 `report -> Leader -> new worker attempt` 策略；是否重试、换模型、
缩小任务或收敛由 Leader 在收到终态证据后决定。

后续若实现 Review/Verify 轮次，只需要补充顺序依赖、report schema/reference validation，以及
Leader 到下一轮 worker 的 prompt handoff 约定。只有出现无法通过终态 artifact 和新 attempt 解决的、
有明确收益的交互场景时，才单独立项评估消息总线。

## Phase 0：项目与 HA 初始化

1. 确认本目录是独立私有 Git 仓库，配置 Git author。
2. 使用固定 HA 本地 CLI 初始化：

   ```powershell
   node D:\project\harness-anything\packages\cli\dist\cli\src\index.js `
     --root D:\project\meta-improver init `
     --repo-id meta-improver `
     --person-id <stable-person-id> `
     --display-name "<display-name>" `
     --name meta-improver --json
   ```

3. 确认 `harness/`、`people.yaml`、`.harness/` 和 daemon registration 正常生成。
4. 在临时 fixture 中验证 init、task create/show、fact record/search、`daemon projection rebuild`、`daemon status`。
5. 初始化 MI 自己的 HA task，记录本阶段决策和验证事实；不要修改 HA 源码。
6. HA target 的 `harness/` 已存在于 `D:\project\ha-target`；后续目标任务必须复用该稳定目标身份，不能以 source-only worktree 替代它。`D:\project\harness-anything` 保持为稳定 CLI provider。

## Phase 1：骨架与数据契约

- 使用 Python 3.12、`uv` 和 `uv.lock`。
- 最小依赖优先：Pydantic、Typer/Rich、LiteLLM、PyYAML、pytest；GitHub client 与 SQLite 扩展不要重复引入。
- 实现 `src/config.py`、`src/models/` 和 `src/storage/ledger_db.py`。
- 数据模型使用 UTC aware datetime、Enum/Literal、`default_factory`；输入限长、脱敏，账本支持 schema migration、幂等键、去重和并发锁。
- MutationProposal 支持多文件，记录 base commit、patch hash、模型/Prompt 版本和测试证据。

## Phase 2：只读摄入与诊断

- 实现 CI、Issue、local log 摄入；先脱敏、限长，再生成稳定 signature。
- HA adapter 读取 `harness/` 权威文档和 artifacts，投影只做新鲜度校验和加速。
- 只输出诊断报告，不修改目标仓库，不创建 PR；用 fixture 证明可重放。`ha squad-diagnose` 已提供受控诊断和 captured Attempt 的共同回执，但仍不执行 patch 或提交。

## Phase 3：受控沙箱与严格补丁

- Worktree 管理与容器化 test runner 分离实现。
- 容器默认禁网、无宿主凭据、资源受限；测试命令使用受控 argv，不接受任意 shell 字符串。
- patch 先校验 base commit、路径 allowlist、symlink/submodule/path traversal、变更规模，再运行 `git apply --check` 和 `git diff --check`。
- 模糊替换默认关闭；自修复最多重试一次，且每次从干净 base 重新应用并重新校验。
- 覆盖恶意 patch、路径穿越、超时、并发、崩溃恢复和 secret redaction 测试。

## Phase 4：归因与变异

- 先复现 baseline 失败，再调用模型；模型输出必须经过结构化 schema 校验。
- 只有 targeted test 通过、全量回归无退化、复杂度/成本/安全门禁通过，才接受候选。
- 报告包含信号引用、复现结果、根因假设、变更文件、base/patch hash、模型和 Prompt 版本、完整测试结果与 residual risk。
- 当前纯 pipeline 已将各阶段回执和失败落入同一 Attempt；下一个真实 HA target 案例将验证这条路径是否足以支持外部 signal 的前置创建。

## Phase 5：proposal PR 与调度

- 默认只生成 PR payload；显式批准后才 push/create PR。
- PR 必须显式指定 `repo/head/base`，使用最小权限 token；不得自动合入主干。
- 调度器需要幂等键、事件去重、并发锁、指数退避、GitHub rate-limit 处理和失败熔断。

## Phase 6：受控 self-evolve

- 仅离线运行，只允许 L1 Prompt/规则候选。
- 使用固定 baseline 与 holdout 集，要求可重复收益、无回归、成本可接受，并保留父 commit、评测快照和回滚路径。
- 只生成候选 PR，由 maintainer 审查；不修改正在运行的 MI，不自动合入。

## 通过标准

- 清空 SQLite 投影后可以从 `harness/` 重建相同结果。
- 同一 `signal + base_commit + strategy_version` 不会产生重复 attempt。
- 恶意 Issue/日志不能改变系统指令，目标代码不能访问宿主 secrets 或网络。
- 每个候选变更都有可审查证据、HA closeout 记录和回滚路径。
