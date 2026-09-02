# Meta-Improver 实施方案

本文件是 MI 项目在当前阶段的执行入口。先完成 Phase 0/1，再逐步开放后续能力；任何阶段失败都 fail-closed，不得跳过门禁扩大权限。

## 目标与边界

`meta-improver` 是运行在 Harness Anything（HA）之上的外部改进器，用于摄入 CI/Issue/local log 信号，生成根因假设和候选补丁，在隔离环境中验证并产出可审查证据。

必须遵守：

- HA `harness/` 文档、canonical events 和任务 artifacts 是真相源；`.harness/` SQLite/HTML 只是可重建投影。
- MI 默认 proposal-only：只生成报告、patch 和 PR payload；显式批准后才允许 push/create PR，永不自动合入 `main`。
- Worktree 只提供 Git 隔离。目标代码必须在 Docker/Podman 容器中运行，默认禁网、无 secrets，并设置 CPU/内存/磁盘/时间限制。
- 每次尝试记录 `base_commit`、输入快照哈希、模型/Prompt 版本、patch 哈希、测试结果、幂等键和回滚点。
- self-evolve 首阶段只允许离线修改 L1 Prompt/规则；权限、审计、门禁、沙箱和凭据处理属于 immutable policy core。

## 当前基线（2026-09-02）

- HA commit：`dbf7182ac68f0555169c80df8cabb84b70e786b4`
- HA CLI version：`0.0.1`
- CLI build id：`852a87e8-a6a5-4c99-9275-160a9cefb704`
- Node.js：`24.18.0`
- Python：使用 `/home/cyr/projects/meta-improver/.venv/bin/python`，版本 `3.12.14`
- HA 本地 CLI：`/home/cyr/projects/harness-anything/packages/cli/dist/cli/src/index.js`
- HA target commit：`241396c2c6730be1b5c2836c9ad08343657cfc05`
- 详细 bootstrap 验证：见 [BOOTSTRAP.md](./BOOTSTRAP.md)

不要依赖 PATH 中的全局 `ha`；它可能来自另一份 checkout。升级 HA 时必须重新记录 commit/build id 并重跑 contract/smoke tests。

## 双账本与源库边界

当前 `/home/cyr/projects/harness-anything` 是一个干净的独立 clone，可作为 MI 的 HA 原材料和 CLI 构建来源；它目前没有 `harness/` 或 `.harness/`。其 `origin` 指向上游 `FairladyZ625/harness-anything`，`fork` 指向开发 Fork `cyrneutron/harness-anything`，但 MI 配置必须使用明确的仓库 URL，不依赖 remote 名称语义。

源库的公开提交可以通过该 clone 读取。每次 MI 处理 HA 任务时，应先记录源库 `base_commit`，再从上游同步/校验目标 checkout；候选变更只在目标 checkout 的临时 worktree 中进行。源库提交是代码基线，不是 MI 或 HA 的记忆来源。

系统同时维护三类记忆，职责不能混淆：

- **MI 项目的 `harness/`**：记录 MI 自身的开发任务、设计决策、事实、评审和结项。
- **HA 目标项目的 `harness/`**：在 MI 第一次正式治理 HA 之前初始化；记录每个 HA 改进任务、复现事实、候选变更、验证证据、review 和 closeout。该目录是私有嵌套账本，不默认进入公开代码 PR。
- **MI 的 `.improver_history/history.db`**：记录所有候选 attempt（成功、失败、拒绝、重试）、模型/Prompt 版本、输入和 patch 哈希；这是 MI 的运行时经验库，不替代任一 HA 账本。

公开 PR、GitHub Actions 结果和 issue 讨论是审查证据。它们需要被 MI 脱敏、引用并写入相应 task/evidence，但不能替代 `harness/` 的权威记录。GitHub Actions 通过也不会自动完成 HA task；仍需按当前 HA 版本完成 execution、review、consent 和 completion 门禁。

## 稳定 CLI 提供者与可变目标 clone

HA 使用两个彼此隔离的 checkout：

- **CLI provider**：`/home/cyr/projects/harness-anything`。保持干净，只用于读取上游提交、构建并提供当前已晋升的稳定 `ha` CLI。它不初始化 HA 目标账本，也不承载 MI 的候选代码改动。
- **HA target**：建议 `/home/cyr/projects/ha-target`。从 `cyrneutron/harness-anything` 克隆，并添加 `FairladyZ625/harness-anything` 为 upstream；在此目录初始化 HA 目标账本，MI 的候选分支和临时 worktree 从明确的 upstream base commit 创建。

MI 的治理写入始终调用 CLI provider 的稳定 CLI。若 MI 修改 HA CLI 本身，候选 CLI 只能在 HA target 的受控沙箱中构建和测试，不能负责裁决或记录自己的改动。

稳定 CLI 的晋升流程：

1. CLI provider 固定稳定版本 `N` 的 commit/build id，并治理 HA target 上的候选 `N+1`。
2. 候选在本地容器通过 contract、回归和安全检查，再创建 proposal PR。
3. 上游 GitHub Actions 和独立 review 通过，PR 由 maintainer 合入。
4. CLI provider 执行 `git fetch` 和 `git merge --ff-only` 更新到已合入 commit，重新 `npm ci`、build，并记录新的 commit/build id。
5. 重跑 bootstrap/contract smoke；全部通过后才把新 CLI 晋升为稳定版本，供下一轮 MI 任务使用。

单个 Attempt 从开始到完成必须绑定同一个 CLI provider commit/build id；运行中检测到 provider 漂移时 fail-closed，不能静默切换版本。

## Phase 0：项目与 HA 初始化

1. 确认本目录是独立私有 Git 仓库，配置 Git author。
2. 使用固定 HA 本地 CLI 初始化：

   ```bash
   node /home/cyr/projects/harness-anything/packages/cli/dist/cli/src/index.js \
     --root /home/cyr/projects/meta-improver init \
     --repo-id meta-improver \
     --person-id <stable-person-id> \
     --display-name "<display-name>" \
     --name meta-improver --json
   ```

3. 确认 `harness/`、`people.yaml`、`.harness/` 和 daemon registration 正常生成。
4. 在临时 fixture 中验证 init、task create/show、fact record/search、`daemon projection rebuild`、`daemon status`。
5. 初始化 MI 自己的 HA task，记录本阶段决策和验证事实；不要修改 HA 源码。
6. MI 达到 Phase 2/3、具备只读诊断和受控沙箱能力后，在 `/home/cyr/projects/ha-target`（或等价独立 target clone）中初始化 HA 自己的 `harness/`；`/home/cyr/projects/harness-anything` 只保留为稳定 CLI provider。

## Phase 1：骨架与数据契约

- 使用 Python 3.12、`uv` 和 `uv.lock`。
- 最小依赖优先：Pydantic、Typer/Rich、LiteLLM、PyYAML、pytest；GitHub client 与 SQLite 扩展不要重复引入。
- 实现 `src/config.py`、`src/models/` 和 `src/storage/ledger_db.py`。
- 数据模型使用 UTC aware datetime、Enum/Literal、`default_factory`；输入限长、脱敏，账本支持 schema migration、幂等键、去重和并发锁。
- MutationProposal 支持多文件，记录 base commit、patch hash、模型/Prompt 版本和测试证据。

## Phase 2：只读摄入与诊断

- 实现 CI、Issue、local log 摄入；先脱敏、限长，再生成稳定 signature。
- HA adapter 读取 `harness/` 权威文档和 artifacts，投影只做新鲜度校验和加速。
- 只输出诊断报告，不修改目标仓库，不创建 PR；用 fixture 证明可重放。

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
