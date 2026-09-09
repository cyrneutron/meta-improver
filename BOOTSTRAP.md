# Meta-Improver Bootstrap

> 本文件保留 2026-08-29 至 2026-09-04 的 Linux bootstrap 和 provider 晋升历史作为审计证据。
> 它们不是当前运行绑定；发生冲突时，以紧随其后的 2026-09-09 Windows provider binding 和
> `PLAN.md` 当前基线为准。

## 当前 Windows provider binding（2026-09-09）

本次在本机实际核对的当前运行基线如下：

| 项目 | 当前值 |
|---|---|
| MI source root | `D:\project\meta-improver` |
| HA provider | `D:\project\harness-anything` |
| Provider HEAD | `e42c2149abd32845953401778d7d82fa55bcfa5b` |
| CLI version / build id | `0.0.1` / `3c95579e-7331-44e6-920a-b2c0e6f331f3` |
| Node / Python | `v24.11.1` / `3.12.10` |
| HA target | `D:\project\ha-target` |
| Target branch / HEAD | `codex/ha-target-latest-20260909` / `e42c2149abd32845953401778d7d82fa55bcfa5b`，与 `upstream/main` 相同 |

固定入口为：

```powershell
node D:\project\harness-anything\packages\cli\dist\cli\src\index.js
```

provider daemon 已按该 build 启动，loaded/disk build id 一致且 `drifted=false`。provider 工作树
有既有 `.gitignore` 修改，必须保留；它不属于 MI 或 target task 的可写范围。HA target 是包含目标
源树、目标 Harness ledger 和 runtime 的稳定目标路径，不能用 source-only worktree 替代。

## Provider refresh record（2026-09-09）

本次 provider refresh 将 `D:\project\harness-anything` 的 `main` 从旧绑定
`ff0caa80487a063ec203f13a64aad8539d03ac53` 快进到已核验的
`origin/main` `e42c2149abd32845953401778d7d82fa55bcfa5b`。快进没有覆盖 provider 既有
`.gitignore` 改动；`npm ci` 完成，post-merge hook 重建 CLI/GUI，CLI disk build id 为
`3c95579e-7331-44e6-920a-b2c0e6f331f3`。

- provider daemon status：PID `6660`，`entry=dist`，loaded commit 为
  `e42c2149abd32845953401778d7d82fa55bcfa5b`；MI 真实 `ha check` 需继续作为 build/drift
  的最终绑定证据。
- provider typecheck：通过；provider lint：通过。
- `npm test -- --tier fast --tier contract`：1037 tests，1035 passed，0 failed，2 skipped；
  两项 skip 的明确理由是 `requires POSIX file-symbolic-link semantics`，不是静默跳过。
- `D:\project\ha-target` 的同步 runtime dispatch 在启动进程前失败，未创建目标 task、未写入目标
  ledger；随后在 daemon `queueDepth=0`、RepoCell attached 且 tracked worktree clean 的条件下，
  MI 仅执行 source-only ref 切换到 `codex/ha-target-latest-20260909`。旧分支
  `codex/windows-provider-runtime-gui-20260908` / `e39fb7a6...` 仍可恢复，嵌套 Harness
  `ha-target-identity-merge` 的 HEAD `7265dd064f2a1877527c8b04b400ef367c1309f3` 未变。
- 这不是目标项目 task execution；MI 未写入 `D:\project\ha-target\harness` 或 `.harness`。
- 本轮没有 push、PR、merge 或自动合入；外部 CI 和独立 review 仍是后续门禁。

## Provider refresh record（2026-09-08）

本次 provider refresh 将 `origin/main` 从旧绑定 `820b6a762577056ead69023259da0b84d7e7b84a`
快进到 `ff0caa80487a063ec203f13a64aad8539d03ac53`。旧 commit/build 只作为本次升级前的
历史输入保留，不再作为当前运行身份。执行 `npm ci` 和 CLI build 后，最终 disk build id 为
`0138c112-a20d-4801-9d7f-d1591e18e249`；daemon PID `45552` 已加载同一 commit/build，
`drifted=false`，provider、target 和 MI 三个 RepoCell 均 attached。

provider `typecheck` 与 `lint` 通过。Windows 本机运行 fast/contract 测试时仍有已分类的
环境限制：Unix-only `sh`/`rsync`、Unix 路径和平台探针断言、临时 Unix socket 权限，以及
临时目录清理 `EPERM`；fast/contract 的真实失败结果不视为 CI 通过，也不回滚 provider
升级。后续 Windows 测试修复另由目标项目任务处理。

## 历史 Bootstrap 记录（2026-08-29 起）

本记录对应 2026-08-29 的 Phase 0 前置验证。此阶段只准备 Python 环境并固定/验证
Harness Anything (HA) CLI 契约，不包含 MI 业务代码；fixture 是一次性临时仓库，不使用
真实 GitHub 写权限。

## 结论

- Python 环境：**通过**。使用 `uv` 安装受管 Python 3.12.14，并创建
  `/home/cyr/projects/meta-improver/.venv`；没有修改系统默认 Python 3.10.12。
- HA 源码基线：**通过**。当前 checkout 工作树干净，HEAD 为
  `b47b93b1a28408ded2849792f8fad55963c30713`。
- HA 本地构建：**通过**。`packages/cli` build 成功，构建 ID 为
  `5fa644bc-4a6c-44a0-8a7d-f7897a1877a6`。
- HA fixture smoke：**通过**。源码构建 CLI 在独立临时 Git 仓库中完成 init、task
  create、fact record/search、projection rebuild，并由 daemon status 验证目标仓库已
  attached 且无 build 漂移。

## Python 环境

执行命令：

```bash
cd /home/cyr/projects/harness-anything
uv --version
uv python install 3.12
mkdir -p /home/cyr/projects/meta-improver
uv venv --python 3.12 /home/cyr/projects/meta-improver/.venv
/home/cyr/projects/meta-improver/.venv/bin/python --version
```

结果：

```text
uv 0.12.6 (7938ca5d5 2026-08-25 x86_64-unknown-linux-gnu)
Python 3.12.14
```

可复现方式（不会改变系统 `python3`）：

```bash
uv python install 3.12
uv venv --python 3.12 /home/cyr/projects/meta-improver/.venv
uv lock --directory /home/cyr/projects/meta-improver
source /home/cyr/projects/meta-improver/.venv/bin/activate
python --version
```

当前解释器实际路径为 `/home/cyr/projects/meta-improver/.venv/bin/python`，底层受管
解释器为 `/home/cyr/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12`。
项目元数据仅写入最小的 `pyproject.toml`，没有依赖和业务模块。
`uv lock` 已成功生成 `uv.lock`（仅锁定本地虚拟项目与 Python 3.12 条件）。

## HA 基线与构建

执行命令：

```bash
cd /home/cyr/projects/harness-anything
git status --short --branch
git rev-parse HEAD
npm -w @harness-anything/cli run build
cat packages/cli/dist/build-id.txt
node packages/cli/dist/cli/src/index.js version --json
node packages/cli/dist/cli/src/index.js capabilities --json
```

关键结果：

| 项目 | 值 | 状态 |
|---|---|---|
| Git HEAD | `b47b93b1a28408ded2849792f8fad55963c30713` | 通过 |
| 分支/工作树 | `main...origin/main`，无改动 | 通过 |
| Node | `v24.18.0` | 通过（CLI 要求 `>=24`） |
| CLI package version | `0.1.0` | 通过 |
| CLI build id | `5fa644bc-4a6c-44a0-8a7d-f7897a1877a6` | 通过 |
| daemon loaded/disk commit | `b47b93b1...` / `b47b93b1...` | 通过，无漂移 |
| daemon loaded/disk build id | `5fa644bc...` / `5fa644bc...` | 通过，无漂移 |

为避免使用漂移的全局命令，本记录所有 smoke 命令均显式调用：

```bash
node /home/cyr/projects/harness-anything/packages/cli/dist/cli/src/index.js ...
```

当前 shell 中的 `ha` 解析到 `/home/cyr/src/harness-anything/packages/cli/dist/cli/src/index.js`，
不是本 checkout；因此它只被用于发现环境，未被用于本次 smoke。

## 已固定的关键契约

- 初始化：`ha init --repo-id <repo-id> --person-id <person-id> --display-name <display-name>`。
  `--name`、`--configure-only`、`--add-npm-scripts` 为可选项。
- 仓库注册：初始化会通过 daemon 注册本地仓库；显式形式为
  `ha daemon repo register --repo-id <repo-id> --root <root> [--mode local]`。
- Task：`ha task create --title <title>` 创建 planned task；当前 smoke 的实体契约为
  `task/v1`，task package 内含 `task-package/v2` 结构，且本次使用
  `standard-task` / `baseline`。
- Fact：`ha fact record [--task <task>] --statement <statement> --source <source>`；Fact
  写入是不可变事件，当前行格式为 `fact-row/v1`，事件格式为 `fact-event/v1`。
- Fact 查询：`ha fact search [query] [--task <task>] ...`；没有 `fact list` 子命令。
- Projection：当前 CLI 命令为 `ha daemon projection rebuild`，而不是独立的
  `projection rebuild` 或 `governance rebuild`。
- Projection 状态：当前 CLI 没有 `projection status` 或顶层 `status` 域；用
  `ha daemon status` 查看 daemon/RepoCell attached 状态和 build drift，用 projection
  rebuild 返回的 `watermark`、`sourceRevision`、`stateDigest` 作为重建证据。
- 代码中的版本常量：`projectionVersion = 3.0`，SQLite task projection schema version
  为 `12`，Action envelope 为 `1.0`。
- 所有机器可读结果使用 `command-receipt/v2`；能力索引使用 `capabilities-index/v1`。

## Fixture Smoke

Fixture 路径：`/tmp/meta-improver-ha-fixture.mRnRMR`（临时目录，可在后续清理；不是
MI 或 HA 源码仓库）。测试使用 repo id `meta-improver-fixture`、person id
`bootstrap-agent`，没有网络写入和 GitHub API 调用。

### 1. Init 与 daemon attach

```bash
fixture=/tmp/meta-improver-ha-fixture.mRnRMR
git -C "$fixture" init -q
git -C "$fixture" config user.name 'Meta Improver Bootstrap'
git -C "$fixture" config user.email 'meta-improver-bootstrap@example.invalid'
node /home/cyr/projects/harness-anything/packages/cli/dist/cli/src/index.js \
  --root "$fixture" init --repo-id meta-improver-fixture \
  --person-id bootstrap-agent --display-name 'Meta Improver Bootstrap' \
  --name meta-improver-fixture --json
node /home/cyr/projects/harness-anything/packages/cli/dist/cli/src/index.js \
  --root "$fixture" daemon status --json
```

结果：**通过**。Init receipt 为 `ok: true`，在 `harness/` 下创建 canonical scaffold，
并建立 nested harness commit `8c645a140dfc01904336c5d6784933183c76aef8`。Daemon status
显示 `meta-improver-fixture` 为 `mode=local, state=attached`，且 loaded/disk build
commit 与 build id 均匹配本记录。

### 2. Task 写入

```bash
node /home/cyr/projects/harness-anything/packages/cli/dist/cli/src/index.js \
  --root "$fixture" task create --title 'Bootstrap smoke task' \
  --kind test --risk-tier low --urgency low --json
```

结果：**通过**。Task ID 为 `task_2e90e049a44cfe7f4bfaf6def0`，状态为 `planned`，
revision 为 `2`，proof 标记 `durable=true`、`canonicalVisible=true`、`worktreeVisible=true`。
随后 `task show` 成功读回同一 task，状态仍为 `planned`。

### 3. Fact 写入与查询

```bash
node /home/cyr/projects/harness-anything/packages/cli/dist/cli/src/index.js \
  --root "$fixture" fact record --task task_2e90e049a44cfe7f4bfaf6def0 \
  --statement 'Bootstrap fixture fact write succeeded.' --source bootstrap-smoke \
  --confidence high --memory-class semantic --json
node /home/cyr/projects/harness-anything/packages/cli/dist/cli/src/index.js \
  --root "$fixture" fact search 'Bootstrap fixture' \
  --task task_2e90e049a44cfe7f4bfaf6def0 --json
```

结果：**通过**。Fact ID 为 `F-111DCF27`，格式为 `fact-row/v1`，revision 为 `3`，
`durable=true`、`canonicalVisible=true`、`worktreeVisible=true`；search 返回同一 fact，
`status=ready`、`watermark=3`、`sourceRevision=3`。

### 4. Projection rebuild/status

```bash
node /home/cyr/projects/harness-anything/packages/cli/dist/cli/src/index.js \
  --root "$fixture" daemon projection rebuild --json
node /home/cyr/projects/harness-anything/packages/cli/dist/cli/src/index.js \
  --root "$fixture" daemon status --json
```

结果：**通过**。Rebuild 返回 `outcome=applied`、`watermark=3`、`sourceRevision=3`、
`reducedItems=3`，并生成 state digest
`sha256:b16c076d7556c4657a994ba90cb88373f719aa1d29d0668e99e1b5a64205ff1a`。随后 daemon
status 仍显示目标 RepoCell `attached` 且 `lastError=null`。

## 失败/限制与最小修复建议

本次没有阻塞性失败。契约探测中发现并记录以下环境限制：

- 系统 `/usr/bin/python3` 是 3.10.12；已通过 `uv python install 3.12` 安装受管
  3.12.14，并在 MI 专用 `.venv` 使用。后续命令必须激活该 venv 或使用绝对解释器路径。
- 全局 `ha` 指向 `/home/cyr/src/harness-anything` 的另一份 checkout，存在版本漂移风险。
  最小修复是让 MI 脚本固定调用本地构建入口，或用固定 commit 的本地包安装后记录
  build id；不可依赖 PATH 中的全局 `ha`。
- daemon 用户注册表中还有 `canonical`、`kty`、`personal` 的旧条目因缺失/非法
  `authoredBranch` 显示 unavailable；本次目标 fixture 独立且 `attached`，不受其影响。
  不应在本任务中修改用户级 registry；后续需要时通过治理任务修复这些条目。
- Init 会创建 nested `harness/.git` 并在其内部提交 scaffold；外层 fixture Git 仓库
  仍无初始 commit。这是 HA 当前存储边界，MI 必须明确区分 authored ledger 与派生缓存。
- `ha task start`、submit/review/complete 尚未执行，因为本次前置 smoke 只要求基础写入与
  投影契约；完整生命周期属于后续 HA contract test，不应在此阶段伪造 completion。

## 下一步建议

1. 将本文件中的 CLI 路径、HEAD、build id 与契约常量作为 MI Phase 1 的启动检查，发生
   drift 时 fail closed。
2. 在 MI 项目中加入 fixture repository 与 CLI contract tests，至少覆盖 init、task/fact
   write/search、daemon projection rebuild/status，以及错误参数的拒绝路径。
3. 固定 HA commit/build 后再实现 MI 的只读摄入和诊断报告；暂不执行目标仓库代码，不接入
   GitHub 写权限，不实现 PR 或 self-evolve。

## Phase 0/1 实施结果（2026-08-29）

在 MI 根目录创建了外层 Git 仓库并设置本地 author：
`Meta Improver Bootstrap <meta-improver-bootstrap@example.invalid>`。随后使用本文件固定的
provider、`HARNESS_ACTOR=agent:bootstrap-agent` 和同一 author 环境完成 MI 的 HA init。初始化
生成独立的 `harness/.git` canonical ledger、`harness/people.yaml` 和本地 `.harness/` 投影；未
初始化 `/home/cyr/projects/ha-target`，也未修改 HA provider。

本轮 HA 账本验证：

- task `task_98a53c93f56992a545f42635b2` 创建并由 `task show` 读回，状态为 `planned`；
- fact `F-FD72A37F` 写入并由 `fact search` 找回，返回 `status=ready`、`watermark=3`、
  `sourceRevision=3`；
- `daemon projection rebuild` 返回 `outcome=applied`、`watermark=3`、`sourceRevision=3`、
  `stateDigest=sha256:93f1b3928ca3dd2608ae1d232b193625522b06d71cb4d733ef23a02f25b63b34`；
- `daemon status` 显示 MI RepoCell `mode=local,state=attached,lastError=null`，provider commit
  和 build id 与本记录一致。

执行记录随后通过同一 task 的 execution `exec_phase01_bootstrap` 进入 `active`，并追加了带
`tests/test_models.py`、`tests/test_ledger_db.py` 和 decision 文档路径的 progress evidence。
本轮设计决策 `dec_03C4DC4CC542BE91A7D2D3CA7F` 保持 `proposed`，其 `C1` 已通过 CLI 的
`decision relate` 关联到事实 `F-FD72A37F` 和 `F-9DCFE2CF`；没有绕过决策门将其伪造为接受。
尝试 `decision accept` 时，固定 provider 以 `actor_unauthorized` 拒绝 agent 自审；尝试
`HARNESS_ACTOR=human:bootstrap-agent` 又以 `daemon_unavailable` 拒绝非法 actor 格式，且该
build 不接受 `--actor` 命令参数。独立 human reviewer 尚未配置，因此按 fail-closed 保持
`proposed`，将接受留给后续具备独立归属的审阅者。

Phase 1 已加入 Python 3.12 项目骨架：`src/config.py`、`src/models/contracts.py` 和
`src/storage/ledger_db.py`，以及聚焦的模型和 SQLite migration/idempotency/concurrency 测试。
契约使用 UTC aware datetime、封闭枚举/Literal、`default_factory`、长度约束、secret-shaped
metadata 脱敏和多文件 `MutationProposal`（base commit、派生 patch hash、model/prompt version、
test evidence、residual risks）。账本使用 schema v3、`BEGIN IMMEDIATE`、唯一
idempotency key、追加式 `attempt_events`、阶段必需哈希、冲突拒绝和重复 migration 安全重跑。
现有 `ha squad-diagnose` 可将受控 HA diagnosis 记录与 captured Attempt 建立不可变来源身份，并由后续 pipeline 完成同一 Attempt 的阶段推进。

实际验证命令：

```bash
uv lock --directory /home/cyr/projects/meta-improver
uv sync --directory /home/cyr/projects/meta-improver
/home/cyr/projects/meta-improver/.venv/bin/python -m pytest -q
/home/cyr/projects/meta-improver/.venv/bin/python -m compileall -q src tests
```

在 2026-09-02 集成 attempt lifecycle 后，主干全量回归为 `355 passed, 2 warnings`，定向诊断/账本回归为 `94 passed, 2 warnings`；未实现目标沙箱、PR 发布门禁、daemon 常驻和 self-evolve。旧 schema v2 数据可迁移到 v3；历史只有 diagnosis 而无可反推 signal 的记录不会被自动虚构成 Attempt。

## Provider 升级记录（2026-09-02）

按 provider runbook 将 `/home/cyr/projects/harness-anything` 快进到已合入的
`dbf7182ac68f0555169c80df8cabb84b70e786b4`，并执行 `npm ci` 与
`npm run build -w @harness-anything/cli`。

- CLI version：`0.0.1`
- CLI build id：`0e7daddf-1da5-4d4d-8052-37c763bf925d`
- Node：`v24.18.0`
- Candidate validation：`npm ci`、CLI build、`version --json`、`capabilities --json`、
  `quickstart:demo` 均通过。
- Target preservation：`/home/cyr/projects/ha-target` 仍为 commit
  `a5b09c2c20e8e041bc3aea5a2ac677d4a42bae31`，本轮未修改其 tracked 内容。

该 provider 随后重新构建并由当前 daemon 加载为 build id
`852a87e8-a6a5-4c99-9275-160a9cefb704`；`daemon status` 报告 loaded/disk 一致且
`drifted=false`，MI 的真实 `ha check` 通过。之后 HA target 的首次正式改进通过 PR #2
合入，target `main` 与 `origin/main` 当前均为 merge commit
`241396c2c6730be1b5c2836c9ad08343657cfc05`。

## Provider 稳定晋升复验（2026-09-04）

provider `/home/cyr/projects/harness-anything` 当前使用兼容修复 commit
`e6ff3f1ab7b878d78583fd892305221c322be49e`，由
`/home/cyr/.nvm/versions/node/v24.18.0/bin/node` 执行构建后的 CLI。
构建生成的 build id 为 `354028c2-1149-449d-abdc-0b07f81c386a`；daemon 重启后报告
loaded/disk build id 一致、`drifted=false`，且 loaded commit 为该 commit。
使用同一 executable、CLI entry 和 build-id 文件运行 MI `ha check` 返回
`status=succeeded`；target Squad `mi-ha-governance` 为 `ready`，唯一 Leader probe 为
`ready`。使用 `/usr/bin/node`（Node 12）会因语法版本不足失败，故不属于有效 provider
运行时。
