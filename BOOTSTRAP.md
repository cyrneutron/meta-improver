# Meta-Improver Bootstrap

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
test evidence、residual risks）。账本使用 schema version、`BEGIN IMMEDIATE`、唯一
idempotency key、冲突拒绝和重复 migration 安全重跑。

实际验证命令：

```bash
uv lock --directory /home/cyr/projects/meta-improver
uv sync --directory /home/cyr/projects/meta-improver
/home/cyr/projects/meta-improver/.venv/bin/python -m pytest -q
/home/cyr/projects/meta-improver/.venv/bin/python -m compileall -q src tests
```

最终测试结果为 `8 passed`；未实现 Phase 2 及后续 CI/Issue 摄入、目标沙箱、PR、daemon 常驻
和 self-evolve。SQLite 账本当前是单 schema 版本，后续 schema 扩展需要新增显式 migration。
