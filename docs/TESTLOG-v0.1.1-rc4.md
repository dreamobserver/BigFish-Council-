# 测试日志：bigfish-council v0.1.1-rc4

记录时间：2026-11-19 17:03–17:16 UTC
执行机：VPS-HOST（Debian GNU/Linux 12 bookworm，Linux 5.15.0-187-generic x86_64，DSH 容器内）
分支：`fix/v0.1.1-rc4-hardening`，工作区含未提交改动（见 §9.9）

本文所有数字来自本轮**实际执行**的输出，逐条贴原始关键行；未跑的一律标「未执行」。
同一台机器上的回环验证只写成回环验证，不升格成跨设备结论。

---

## 1. 结论

| 项目 | 实测结果 | 证据位置 |
| --- | --- | --- |
| 全量单元测试 | **Ran 162 tests in 26.502s / OK**，退出码 **0** | §3 |
| 单文件分跑 | 8 个文件全部 RC=0，合计 162 项 | §4 |
| 自检 `tools/selfcheck.py` | 通过 16 项 / 失败 0 项，RC=0 | §6.1 |
| GUI 冒烟 `tools/gui_smoke.py` | 通过 9 项 / 失败 0 项，RC=0 | §6.2 |
| `pyflakes` | 无告警，RC=0 | §6.3 |
| `compileall` | 无输出，RC=0 | §6.4 |

162 项 = 129 项（rc3 存量）+ 33 项（`tests/test_v011_rc4.py` 新增）。
版本号 `0.1.1-rc4`（`bigfish/__init__.py`），`SCHEMA_VERSION = 3`（`bigfish/db.py:21`），本轮没有新增迁移段。

范围限定（别读错）：

- 本轮只在 Debian 12 / Python 3.11.2 上跑，**Windows 的 162 项没有跑**（rc3 时期在 Windows 上跑过的是 129 项）。
- 跨机链路本轮没有复测，§7 单列。
- 全量 26.502s 是 unittest 自报的测试内耗时；进程墙钟 27.332s（含解释器启动与导入）。

---

## 2. 环境与命令

### 2.1 运行时环境（实测）

```
$ .venv/bin/python -V
Python 3.11.2

$ uname -a
Linux VPS-HOST 5.15.0-187-generic #197-Ubuntu SMP Fri Jul 17 19:17:01 UTC 2026 x86_64 GNU/Linux

$ head -3 /etc/os-release
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
VERSION_ID="12"

$ .venv/bin/python -c "import sqlite3; print(sqlite3.sqlite_version, sqlite3.version)"
3.40.1 2.6.0
```

| 组件 | 版本 |
| --- | --- |
| Python | 3.11.2 |
| SQLite 库 / 模块 | 3.40.1 / 2.6.0 |
| PySide6 | 6.12.0 |
| FastAPI | 0.163.2 |
| Starlette | 1.10.0 |
| httpx | 0.28.1 |
| uvicorn | 0.54.0 |
| pyflakes | 4.1.0 |
| Node（`tools/bfc-proxy.mjs` 用，本轮未跑） | v22.23.2 |

环境变量沿用 rc3 的做法：`LD_LIBRARY_PATH=/home/dsh/.local/libbfc`（libbfc 动态库）、
`QT_QPA_PLATFORM=offscreen`（容器内没有 X11，Qt 只能离屏）。

### 2.2 执行命令

```bash
cd /home/dsh/bigfish-council

# 1) 全量
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .

# 2) 单文件（用模块名，避免 discover 单文件时的路径前缀歧义）
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest tests.test_v011_rc4

# 3) 自检（默认端口 3094）
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/selfcheck.py

# 4) GUI 冒烟
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/gui_smoke.py

# 5) 静态检查
.venv/bin/python -m pyflakes bigfish tools tests; echo RC=$?
.venv/bin/python -m compileall -q bigfish tools tests; echo RC=$?
```

### 2.3 端口占用实测（写下来是因为它直接影响自检结论）

```
3094 free
3095 IN USE 98
3096 free
3097 free
3098 free
3099 free

$ curl -s -m 3 http://127.0.0.1:3095/health
{"ok":true,"service":"quota-hub","port":3095}
```

3095 被容器里的 `quota-hub` 占着，且它的 `/health` 长得**很像**本服务（同样带 `ok` 与 `service` 字段）。
这正是 §9.1 那条缺陷的现实土壤：老自检会拿别人的 `/health` 判自己活着。

---

## 3. 全量日志

命令见 §2.2 第 1 条。原始输出共 9 行，全部贴出（长串点号做截断标注）：

```
/home/dsh/bigfish-council/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
  from starlette.testclient import TestClient as TestClient  # noqa
[88 个点号]Fontconfig error: Cannot load default config file: No such file: (null)
This plugin does not support propagateSizeHints()
[74 个点号]
----------------------------------------------------------------------
Ran 162 tests in 26.502s

OK
```

退出码与墙钟：

```
RC=0
ELAPSED_MS=27332
```

关键行解读：

- 两段点号 88 + 74 = **162**，与 `Ran 162 tests` 自洽，没有 `s`（跳过）标记。
- 没有 `F` / `E`，没有 traceback，结尾是 `OK` 而不是 `FAILED (...)。
- 三行噪音（Fontconfig / StarletteDeprecationWarning / propagateSizeHints）都来自依赖与本机环境，
  不是断言失败，来源与无害性见 §8。

---

## 4. 分层验证表

每个文件都用 `-m unittest tests.<模块名>` 单跑；「测试内耗时」取 `Ran N tests in X` 的自报值，
墙钟（含解释器启动与导入）另列一栏。

| 测试文件 | 项数 | 结果 | 测试内耗时 | 墙钟 | exit code |
| --- | --- | --- | --- | --- | --- |
| `tests/test_core.py` | 15 | OK | 3.648s | 4015ms | 0 |
| `tests/test_api.py` | 6 | OK | 0.715s | 3327ms | 0 |
| `tests/test_v011_p0.py` | 15 | OK | 0.623s | 775ms | 0 |
| `tests/test_v011_p1.py` | 24 | OK | 4.743s | 6831ms | 0 |
| `tests/test_v011_p2.py` | 24 | OK | 1.243s | 2192ms | 0 |
| `tests/test_v011_p3.py` | 19 | OK | 9.799s | 10139ms | 0 |
| `tests/test_v011_rc3.py` | 26 | OK | 4.151s | 4790ms | 0 |
| `tests/test_v011_rc4.py` | 33 | OK | 1.192s | 2517ms | 0 |
| **合计** | **162** | **全 OK** | 26.114s | — | 全部 0 |

两处数字差，说清楚免得被当成估数：

- 表内「测试内耗时」合计 26.114s，全量跑是 26.502s，差 0.388s：全量时所有用例在同一个进程里，
  `TemporaryDirectory` 与 SQLite 初始化会累积；单跑则各自从冷进程起算。两个数都是实测值。
- 墙钟差主要来自 `tests/test_v011_p3.py`：要起 `QApplication` 与后台进程，光导入 PySide6 就吃掉 1–2s。

按 `unittest` 收集结果核对项数（不靠文件名猜）：

```
TOTAL 162
  6  tests.test_api.TestApi
  4  tests.test_core.TestApproval
  6  tests.test_core.TestExecution
  5  tests.test_core.TestReviewGate
  6  tests.test_v011_p0.TestApprovalConcurrency
  4  tests.test_v011_p0.TestNoPlaintextToken
  5  tests.test_v011_p0.TestSqliteBackup
  5  tests.test_v011_p1.TestHeartbeatIdempotency
  8  tests.test_v011_p1.TestMcpSemantics
  7  tests.test_v011_p1.TestTicketRenewal
  4  tests.test_v011_p1.TestWatchdog
  5  tests.test_v011_p2.TestAudit
  7  tests.test_v011_p2.TestExecutorBoundary
  6  tests.test_v011_p2.TestReviewIdentity
  6  tests.test_v011_p2.TestRoleTable
  4  tests.test_v011_p3.TestBackgroundProcess
  5  tests.test_v011_p3.TestGuiSmoke
  2  tests.test_v011_p3.TestLoopbackOnly
  8  tests.test_v011_p3.TestPortTopology
  7  tests.test_v011_rc3.TestConcurrency
  7  tests.test_v011_rc3.TestRestore
  5  tests.test_v011_rc3.TestSecurityBoundary
  7  tests.test_v011_rc3.TestStateMachine
  6  tests.test_v011_rc4.TestAuxiliary
  4  tests.test_v011_rc4.TestExecutorClient
  6  tests.test_v011_rc4.TestIdempotencyContract
  7  tests.test_v011_rc4.TestMcpProtocol
  6  tests.test_v011_rc4.TestMultiRoundTickets
  4  tests.test_v011_rc4.TestReviewBinding
```

rc3 存量 129 = 15 + 6 + 15 + 24 + 24 + 19 + 26，与 rc3 日志一致，没有用例被删。

---

## 5. rc4 的 33 项逐项清单

用例名直接取自 `tests/test_v011_rc4.py` 的 `def test_*`，分组对应文件里 A–F 六段注释。
「断言什么」写该用例实际 `assert*` 的核心，不复述用例名。

### A. 幂等契约（`TestIdempotencyContract`，6 项）

| # | 用例名 | 测什么 | 断言什么 |
| --- | --- | --- | --- |
| A1 | `test_idem_key_with_colon_replays_same_ticket` | 幂等键自带冒号（`claim:tenant:retry-1`）时重放 | 两次 `claim` 返回**同一张票**，第二次带 `replayed` |
| A2 | `test_idem_key_with_colon_does_not_issue_second_ticket` | 同上，直接看库 | `tickets` 表中该任务行数 == 1 |
| A3 | `test_same_key_same_body_returns_identical_response` | 同键同体心跳 | `server_time` 完全一致（不是「约等于」），第二次带 `replayed` |
| A4 | `test_same_key_different_body_rejected` | 同键异体 | 抛 `CouncilError`，文案含「不同请求」 |
| A5 | `test_replay_does_not_duplicate_events` | 重放的事件副作用 | `events` 里 `run.heartbeat` 计数 == 1 |
| A6 | `test_concurrent_same_key_heartbeat_runs_once` | 4 线程 + `Barrier` 同键并发 | 无异常、`server_time` 去重后只剩 1 个、事件计数 == 1 |

### B. MCP 协议语义（`TestMcpProtocol`，7 项）

| # | 用例名 | 测什么 | 断言什么 |
| --- | --- | --- | --- |
| B1 | `test_ping_with_id_returns_result` | 带 id 的 ping | HTTP 200，body 有 `result`，`id` 回显为 7 |
| B2 | `test_ping_as_notification_gets_202` | 无 id 的 ping（通知） | HTTP 202 |
| B3 | `test_missing_method_is_invalid_request` | 有 id 无 method | `error.code == -32600`（不能当通知吞掉） |
| B4 | `test_wrong_jsonrpc_version_rejected` | `jsonrpc: "1.0"` | `error.code == -32600` |
| B5 | `test_tool_arg_type_error_is_isError` | `task_id` 传 int | `result.isError` 为真，文案含「字符串」 |
| B6 | `test_tool_enum_error_is_isError` | `status` 传 `nonsense` | `result.isError` 为真，文案列出合法值 `running` |
| B7 | `test_unknown_argument_rejected` | 传 `bogus` 字段 | `result.isError` 为真，文案回显 `bogus` |

### C. 多轮执行票（`TestMultiRoundTickets`，6 项）

| # | 用例名 | 测什么 | 断言什么 |
| --- | --- | --- | --- |
| C1 | `test_reclaim_after_block_issues_new_ticket` | BLOCKED → 退回 → 改 Prompt → 重新批准 → 再领取 | 第二张票 ≠ 第一张；旧票 `heartbeat` 抛 `CouncilError` |
| C2 | `test_old_ticket_revoked_on_new_claim` | 旧票落库状态 | `tickets.revoked_at` 非 NULL；旧票报错含「撤销」；新票可用 |
| C3 | `test_no_idem_key_uses_random_nonce` | 无幂等键时两轮领取 | 两张票不同（旧实现会算出同一张） |
| C4 | `test_derive_ticket_is_deterministic_per_nonce` | 派生函数本身 | 同 nonce 相等、异 nonce 不等、前缀 `bfc_tk_` |
| C5 | `test_verify_accepts_only_submitted` | 正常闭环验收 | SUBMITTED → `verify(True)` → DONE |
| C6 | `test_verify_cannot_accept_blocked` | 对 BLOCKED 直接点通过 | 抛 `CouncilError`，状态**仍是 BLOCKED**（没被写成 DONE） |

### D. 审查绑定（`TestReviewBinding`，4 项）

| # | 用例名 | 测什么 | 断言什么 |
| --- | --- | --- | --- |
| D1 | `test_revoke_latest_approve_does_not_fall_back` | 同模型两张赞成票，撤掉最新一张 | `votes["gpt"]["verdict"] is None`、`revoked` 为真、`all_approved` 为假 |
| D2 | `test_latest_approval_not_effective_after_revoke` | 撤票后的 `latest_approval` | `effective` 变假（退回议事的历史批准不再算数） |
| D3 | `test_votes_before_submit_go_straight_to_pending` | 先投票后送审 | 送审后直接 `PENDING_APPROVAL`，不卡在议事中 |
| D4 | `test_revoke_after_dispatch_blocks_task` | 已派工后撤票 | 状态进 `BLOCKED` 或 `IN_REVIEW`；该票 `heartbeat` 抛 `CouncilError` |

### E. 执行器客户端（`TestExecutorClient`，4 项）

| # | 用例名 | 测什么 | 断言什么 |
| --- | --- | --- | --- |
| E1 | `test_safe_submit_requires_task_id` | 响应丢失且没给 `task_id` | 抛 `RuntimeError`，文案含 `task_id`（明确「无法核对」，不静默成功） |
| E2 | `test_blocked_is_not_treated_as_success` | 核对到 BLOCKED | 抛 `RuntimeError`，文案含「不能证明」 |
| E3 | `test_submitted_is_treated_as_success` | 核对到 SUBMITTED | `ok` 与 `reconciled` 均为真 |
| E4 | `test_safe_claim_reuses_same_key` | 第一次网络不确定，重试 | 两次调用的 `idem` **完全相同**（换键等于双发） |

### F. 辅助（`TestAuxiliary`，6 项）

| # | 用例名 | 测什么 | 断言什么 |
| --- | --- | --- | --- |
| F1 | `test_list_tasks_statuses_filter` | 多状态过滤 | DRAFT 的在结果里，已送审的不在 |
| F2 | `test_list_tasks_offset_paging_is_stable` | 5 条任务，limit=2 翻两页 | 4 条无重复、无遗漏 |
| F3 | `test_list_tasks_order_is_deterministic` | 同一秒建 4 条 | 两次查询顺序一致，且等于 `updated_at DESC, id DESC` |
| F4 | `test_get_task_status_row_is_light` | 轻量状态查询 | 有 `status` / `status_label`，**不含** `reviews`；不存在返回 None |
| F5 | `test_reserved_ports_do_not_collide` | `PORT_RESERVED` 与 `PORT_ROLES` | 两集合无交集；`PORT_ROLES` 恰好是 3096 / 3098 / 3097 |
| F6 | `test_roles_db_whitelist` | `roles.DB_ROLES` | 含 `executor`、`admin`，不含未实现的 `mobile` |

### 5.1 与测试文件头注释的偏差（如实记下）

`tests/test_v011_rc4.py` 开头注释写的是「A 6 / B 6 / C 5 / D 4 / E 4 / F 5」，合计 30 项；
实际收集到的是 **6 / 7 / 6 / 4 / 4 / 6 = 33 项**。

差异来源：B 段多了 `test_ping_as_notification_gets_202`，C 段多了
`test_derive_ticket_is_deterministic_per_nonce`，F 段多了 `test_reserved_ports_do_not_collide`，
文件头注释没跟着更新。**以本文表格与 §4 的收集结果为准**，用例本身全部通过。
这三处都在 `tests/` 目录下，本轮按约束未改动。

---
## 6. 工具与静态检查

### 6.1 `tools/selfcheck.py`

```
健康检查
  [ok]   服务在线  bigfish-council
  [ok]   health 不含任务信息
  [ok]   看门狗已随服务启动
MCP 协议
  [ok]   initialize 成功
  [ok]   五个执行器工具  council_list_tasks,council_claim_task,council_heartbeat,council_submit_result,council_task_status
  [ok]   没有审批类工具
闭环
  [ok]   能列出已批准任务
  [ok]   列表不含 Prompt
  [ok]   能领取并拿到 Prompt
  [ok]   重复领取被拒
  [ok]   心跳上报
  [ok]   回传只到待验收
  [ok]   状态可查
权限
  [ok]   坏 Token 被拒
  [ok]   HTTP 坏 Token 被拒
  [ok]   审批只能在进程内

通过 16 项，失败 0 项
自检全部通过，可以启动 GUI。
RC=0
```

自检自己起 uvicorn 绑 `127.0.0.1:3094`，走真实 HTTP + 真实 MCP 协议，不是 `TestClient` 的内存调用，
所以这一条能覆盖「端口能否绑定」这一层。它仍然是**本机回环**。

### 6.2 `tools/gui_smoke.py`

```
  [ok]   主窗口构造成功
  [ok]   五个页面都在  count=5 keys=5
  [ok]   页面可切换：overview
  [ok]   页面可切换：tasks
  [ok]   页面可切换：council
  [ok]   页面可切换：approval
  [ok]   页面可切换：monitor
  [ok]   状态栏文案  API：未启动
  [ok]   待审批任务被识别

通过 9 项，失败 0 项
GUI 冒烟通过。
RC=0
```

stderr 有一行 `Fontconfig error: Cannot load default config file: No such file: (null)`，
来源见 §8.1，不影响退出码。

### 6.3 `pyflakes`

```
$ .venv/bin/python -m pyflakes bigfish tools tests; echo RC=$?
RC=0
```

零告警。这一条在本轮有实际判别力：§9.3 的死变量就是它抓出来的（该节有修复前后的对照输出）。

### 6.4 `compileall`

```
$ .venv/bin/python -m compileall -q bigfish tools tests; echo RC=$?
RC=0
```

`-q` 模式下无输出即无语法错误。它只证明能编译，不证明逻辑正确——逻辑由 §3 / §4 的 162 项覆盖。

---

## 7. 未执行项

| 项目 | 状态 | 原因 / 说明 |
| --- | --- | --- |
| Windows 上跑 162 项 | **未执行** | 本轮只在 Debian 12 上跑。Windows 侧最近一次实测是 rc3 的 129 项（`Ran 129 tests in 25.263s / OK`，公司机独立目录），rc4 新增的 33 项**尚未在 Windows 上跑过** |
| 跨机闭环（VPS → 公司机 CLIENT-B-TAILNET-IP:3096） | **未复测** | 本轮全部是回环验证，没有发起跨机连接。`tools/e2e_loopback.py` 文件头自己写着「同一台机器上，不能当作跨设备联调成功」；跨机结论要看 `tools/net_check.py` 第 4 节的人工实测 |
| 类型检查（mypy / pyright） | **未执行** | 虚拟环境没装（`.venv/bin/python -m mypy` 报 `No module named mypy`）。本轮不引入新依赖，所以只有 pyflakes 这一层 |
| 24h soak / 长时间稳定性 | **未执行** | 没有 24 小时窗口，也没有可持续观测的常驻负载 |
| PowerShell 脚本（`setup.ps1` / `build.ps1` / `start-headless.ps1` / `stop-headless.ps1`） | **未执行** | 容器里没有 pwsh，Windows 专属脚本只能到 Windows 上验；本轮对它们的改动只做了文本层面的检查 |
| `tools/bfc-proxy.mjs`（Node 跳板） | **未执行** | 本轮没碰网络层，跳板未启动 |

上面六项都不影响 §1 的口径：本文只说「Debian 12 上 162 项全绿」，
不说「两平台 162 项全绿」，也不说跨机已经通了。

---

## 8. 已知噪音

三行噪音在 rc3 时期就有，与 rc4 的改动无关。逐条给来源，不糊成「都是正常的」。

### 8.1 `Fontconfig error: Cannot load default config file: No such file: (null)`

- **出现位置**：`tests/test_v011_p3.py` 起 `QApplication` 时（它是唯一带 GUI 用例的文件），
  以及 `tools/gui_smoke.py` 的 stderr。全量日志里它粘在点号行中间，看着像 88 个点号的尾巴。
- **来源**：容器里没有 fontconfig 配置目录（`ls /etc/fonts/` → `No such file or directory`），
  Qt 在 offscreen 平台下仍会问一次默认字体配置，问不到就打这一行。
- **无害性**：只是字体回退，离屏渲染不依赖具体字体。用例断言的是「页面能构造、能切换、
  状态栏文案正确」，没有一条依赖字形度量。162 项全绿说明它没拖垮任何断言。

### 8.2 `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated`

- **出现位置**：`.venv/lib/python3.11/site-packages/fastapi/testclient.py:1`，
  即 `from starlette.testclient import TestClient as TestClient  # noqa` 这一行。
  只要有用例 import `fastapi.testclient` 就打一次，出现在 `test_api` / `test_v011_p1` /
  `test_v011_p2` / `test_v011_rc3` / `test_v011_rc4` 五个文件里（逐文件 grep 计数实测）。
- **来源**：本机是 Starlette 1.10.0 + httpx 0.28.1，上游在这组版本上建议换 `httpx2`。
  这是**依赖之间**的版本提示，不是本项目代码的告警。
- **无害性**：`TestClient` 仍按原语义工作，所有走 `TestClient` 的用例都通过。
  消掉它得装 `httpx2` 或降版本，属于改依赖，不在本轮范围。

### 8.3 `This plugin does not support propagateSizeHints()`

- **出现位置**：同样是 `tests/test_v011_p3.py` 与 `tools/gui_smoke.py`，Qt offscreen 平台插件。
- **来源**：offscreen 插件不实现窗口尺寸提示传播，`QWidget` 在布局调整时调到就打一行。
- **无害性**：GUI 冒烟的 9 项断言（页面数量、逐个切换、状态栏文案、待审批识别）全通过，
  说明布局与切页逻辑没受影响。

三条共同点：都打 **stderr**，都不改变退出码，都不出现在 unittest 的 `FAIL` / `ERROR` 段落。
判断标准就一条——退出码 0 + 结尾 `OK`，失败段为空。本轮三条都满足。

---

## 9. rc4 期间修掉的真实缺陷

每条给「缺陷是什么 → 证据 → 现在的实现位置」。证据优先用可复现命令的输出，
代码位置用 `文件:行号`，行号对应本轮工作区（含未提交改动，见 §9.9）。

### 9.1 自检端口 3099 → 3094，且原实现会在端口冲突时拿别人的 `/health` 瞎判

**缺陷**：老 `tools/selfcheck.py` 默认 `PORT = 3099`，拉起 uvicorn 后直接
`urlopen("http://127.0.0.1:3099/health")` 读响应。端口被别人占着时 uvicorn bind 失败，
但 `/health` 仍然返回**那个别人**的 JSON；老实现只 `json.loads` 完就当自己的健康数据用。

**复现（本轮实跑，本机回环；用一个假服务占住 3093 端口，返回 `"service": "quota-hub"`）**：

```
$ curl -s http://127.0.0.1:3093/health
{"status": "ok", "service": "quota-hub", "watchdog": true}

# 老实现（git show 832f699:tools/selfcheck.py 另存后执行）
$ BFC_SELFCHECK_PORT=3093 .venv/bin/python /tmp/old_selfcheck.py
ERROR:    [Errno 98] error while attempting to bind on address ('127.0.0.1', 3093): address already in use
健康检查
  [ok]   服务在线  quota-hub          <-- 别人的服务被判成「服务在线」
  [ok]   health 不含任务信息
  [ok]   看门狗已随服务启动
MCP 协议
Traceback (most recent call last):
  ...
KeyError: 'serverInfo'
```

前三项全 `[ok]`，到 MCP 段才崩——端口冲突时它先给出**三条假绿**。

**现在的实现**：`tools/selfcheck.py:22` 默认端口改 3094；读完健康数据先校验身份
（`tools/selfcheck.py:80` 起的 `if health.get("service") != "bigfish-council"` 分支）：

```
$ BFC_SELFCHECK_PORT=3093 .venv/bin/python tools/selfcheck.py
健康检查
  [FAIL] 端口 3093 上跑的不是本服务（service='quota-hub')

端口被别的程序占用，用 BFC_SELFCHECK_PORT 换一个空闲端口。
RC=1
```

同机同假服务：老实现给假绿，新实现在第一条就 `[FAIL]` 并返回 1。
选 3094 而不是留 3099 的理由写进了代码注释：3099 已登记为手机端 REST 网关（§9.7），
3095 在本容器被 `quota-hub` 长期占用（§2.3 实测），自检不能占这两个号。

> 这条验证是**本机回环 + 假服务**，证明的是「端口被占用时的判定逻辑」，不是跨设备联调。

### 9.2 幂等键含冒号时被截断，重放会派生出另一张票

**缺陷**：`_idem_get_locked()` 用 `key.split(":")` 全量切分后取 `parts[3]`。
幂等键格式是 `claim:{ex}:{task}:{idem}`，而 `{idem}` 本身可能带冒号（客户端自定义前缀），
切完 `parts[3]` 只剩前半截。

**复现（纯字符串运算，可重复）**：

```
key = "claim:dsh-cloud:task-9f3a:tenant:retry-1"
old parts = ['claim', 'dsh-cloud', 'task-9f3a', 'tenant', 'retry-1'] -> parts[3] = 'tenant'
new parts = ['claim', 'dsh-cloud', 'task-9f3a', 'tenant:retry-1']  -> parts[3] = 'tenant:retry-1'
旧写法取到的 nonce 丢了后半截: 'tenant' != 真实: 'tenant:retry-1'
```

派生票是 `HMAC(key, ex|task|version|nonce)`，nonce 变了票就变 → 重放拿到**另一张票**，
等于幂等失效、可能双发。

**现在的实现**：`bigfish/core.py:1215` 改 `key.split(":", 3)`，且 `len(parts) == 4` 才认，
再用 `setdefault` 而不是覆盖已写入的 `nonce`。用例 A1 / A2 钉住这条。

### 9.3 `bigfish/backup.py` 的死变量 `key_relocated`

**缺陷**：`restore_bundle()` 里写了 `key_relocated = False` → `... = True`，之后再没被读过。
它不影响运行，但会让人以为有个开关在起作用，读代码时判断「到底换没换密钥」被带偏。

**证据（用 pyflakes 对比修复前后的同一文件）**：

```
$ git show 2ea5d1b:bigfish/backup.py > /tmp/old_backup.py
$ .venv/bin/python -m pyflakes /tmp/old_backup.py
/tmp/old_backup.py:478:17: local variable 'key_relocated' is assigned to but never used
RC=1

$ .venv/bin/python -m pyflakes bigfish/backup.py
RC=0
```

**现在的实现**：工作区已删掉这两行（`git diff bigfish/backup.py` 就是两行删除），
所以 §6.3 的 `RC=0` 与本条互为印证。

### 9.4 `idempotency_purged` 在旧实现里被写死成 0

**缺陷**：rc3 的 `restore_bundle()` 里先有
`cleanup = {"blocked": 0, "idempotency_purged": 0}` 这段占位，而清理计数在恢复路径上没有可靠回填，
对外返回值与审计日志里的 `idempotency_purged` 可能恒为 0——
审计会显示「恢复了但一条幂等记录都没清」，掩盖真实清理量。

**证据（rc3 版本，注意占位那行）**：

```
$ git show 832f699:bigfish/backup.py | sed -n '387,406p'
        cleanup = {"blocked": 0, "idempotency_purged": 0}
        with db.tx() as c:
            cleanup = _block_inflight(c, new_epoch, now())
        blocked = cleanup["blocked"]
        ...
                 f"idem_purged={cleanup.get('idempotency_purged', 0)} "
```

**现在的实现**：`bigfish/backup.py:309` 的 `_block_inflight` 直接返回
`DELETE FROM idempotency ...` 的 `.rowcount` 真实值；`bigfish/backup.py:386` 的 staging 阶段拿到 `cleanup`，
`bigfish/backup.py:391` 写进审计、`bigfish/backup.py:528` 作为返回值。
当前文件已无 `"idempotency_purged": 0` 这类硬编码，占位写法整个消失。

### 9.5 恢复不原子：库换了密钥没换（半完成状态）

**缺陷**：rc3 的顺序是「先 `os.replace(staging, target)` 换库 → 重开连接 → 再 `rotate_key()`」。
两次 replace 之间被杀就留下「新库 + 旧密钥」；执行票是 `HMAC(key, ...)` 确定性派生的，旧票会全部复活。

**现在的实现**（`bigfish/backup.py`）：

| 环节 | 位置 | 做法 |
| --- | --- | --- |
| 排他锁 | `bigfish/backup.py:312` `_restore_lock()` | POSIX `fcntl.flock` / Windows `msvcrt.locking`，拿不到锁直接报错，不「先替换后打架」 |
| 暂存迁移 | `bigfish/backup.py:357` `_prepare_staging()` | 在 staging 上先迁移、先写世代与 `key_sha256`，失败不碰现役库 |
| 成对替换 | `bigfish/backup.py:408` `restore_bundle()` | 新密钥先写 `*.restore-new`，库与密钥成对 replace，失败成对回滚（`.prerestore` 副本） |
| 启动自愈 | `bigfish/backup.py:593` `ensure_key_consistency()` | 比对 `meta.key_sha256` 与实际密钥指纹，不一致就轮换并记 `db.key_healed` 审计 |

`bigfish/runtime.py:18` 在 `open_council()` 里调 `ensure_key_consistency`，
所以「上次恢复被杀」的场景在下次启动就会被发现。
对应用例：`tests/test_v011_rc3.py` 的 `TestRestore` 7 项（含
`test_old_ticket_invalid_after_restore`、`test_restore_failure_keeps_original_db`）。

### 9.6 数据库打开顺序错误：先改文件再判版本

**缺陷**：`Database.__init__` 一上来就 `PRAGMA journal_mode=WAL`——这会改写文件头并建 `-wal`。
对一个「比本程序更新」的库，本该只读拒绝，结果先把人家文件动了。

**现在的实现**（`bigfish/db.py`）：

- `bigfish/db.py:242` `_peek_schema_version()` 只读探测：不建表、不写 `meta`、不改 `journal_mode`；
  返回 0 表示全新库。
- `bigfish/db.py:228` 起：探测到 `ver > SCHEMA_VERSION` 就 `close()` 并抛 `DatabaseVersionError`
  （异常定义在 `bigfish/db.py:206`），**发生在 WAL 之前**。
- `bigfish/db.py:268` `init_schema()` 改成「先迁移后建表」：全新库直接 `executescript(SCHEMA)`，
  已有库先 `_migrate_locked(current)` 再补最新 SCHEMA，最后校验版本必须等于 `SCHEMA_VERSION`。

`SCHEMA_VERSION` 本轮保持 3（`bigfish/db.py:21`），没有新增迁移段。
对应用例：`test_restore_future_schema_rejected`、`test_restore_older_schema_migrates`。

### 9.7 端口预留与角色白名单（先占号，不实现）

这两条不是修 bug，是防止后面加 exe / apk 时踩坑；验证方式可执行，所以列在这里。

**端口**：`bigfish/config.py:37` 新增 `PORT_RESERVED`，登记 3099 / 3100 / 3101 / 8765 / 18765 / 18766。
关键是**没有**并进 `PORT_ROLES`——`tests/test_v011_p3.py` 断言 `PORT_ROLES` 恰好三行职责，
把未实现的端口塞进去等于让测试说谎。用例 F5 断言两集合无交集，并校验 `PORT_ROLES` 仍是 `{3096, 3098, 3097}`。

**角色**：`bigfish/roles.py:48` 新增 `DB_ROLES = frozenset(_DB_ROLE_MAP)`。
背景是 `role_for_db_row()` 末尾兜底成 `EXECUTOR_DSH`，将来加 `mobile` 若忘记登记，
手机连上来会直接拿到执行器权限。现在显式登记 `executor` / `reviewer` / `admin`，
`mobile` 故意不登记。用例 F6 断言「含 executor、admin，不含 mobile」。

### 9.8 其它同批修掉、由存量用例覆盖的项

| 缺陷 | 位置 | 现象 | 覆盖 |
| --- | --- | --- | --- |
| `verify(accept=True)` 接受 BLOCKED | `bigfish/core.py:1092` | 点一下「通过」把「副作用不明」改成 DONE | rc4 C6 |
| 无幂等键时派生同一张票 | `bigfish/core.py:746` | 多轮执行后旧票复活 | rc4 C1 / C3 |
| 领取用 `INSERT OR REPLACE` | `bigfish/core.py:760` 前先吊销再 `INSERT` | REPLACE 静默覆盖历史票，丢掉吊销证据 | rc4 C2 |
| watchdog 按 `task_id` 取最新 run | `bigfish/core.py` 的 `sweep_timeouts` | 多轮执行后读到历史心跳，把真失联判成活着 | rc3 并发组 + rc4 C 组 |
| `latest_approval` 用黑名单判生效 | `bigfish/core.py:36` `EFFECTIVE_APPROVAL_STATES` | 黑名单漏排 PENDING_APPROVAL / REVIEW_PASSED / BLOCKED | rc4 D2 |
| 投票汇总回落旧票 | `bigfish/core.py:524` / `bigfish/core.py:598` | 撤掉最新赞成票后回落到更早那张，凑出「双通过」 | rc4 D1 |
| `safe_submit` 的 `kwargs.get("task_id")` 恒为 None | `bigfish/executor_client.py:144` 改显式形参 | 「不确定时永远直接抛」，等于没做核对 | rc4 E1 |
| `RECONCILED_OK` 含 BLOCKED | `bigfish/executor_client.py:142` | 心跳超时也会打成 BLOCKED，被当成提交成功 | rc4 E2 |
| `update_prompt` 不清取消标记 | `bigfish/core.py:358` | 取消后改一版 Prompt 再审批，永远领不到（`cancel_requested_at IS NULL` 一直挡） | rc4 C1 路径 |
| `stop_watchdog()` 无条件 `stop_for` | `bigfish/core.py:1181` | 关服务时把外部 GUI / CLI 的看门狗一起停掉 | rc3 watchdog 组 |
| GUI 关窗后仍回调 | `bigfish/gui.py:1203` / `:1216` / `:1224` / `:1265` 的 `_closed` 守卫 | `sqlite3.ProgrammingError: Cannot operate on a closed database` | p3 GUI 冒烟 |
| `ApiServer` 不捕 `SystemExit` | `bigfish/gui.py:97` `start()` 里捕 `BaseException` | 端口占用时（uvicorn 抛 SystemExit）界面卡在「启动中」 | p3 端口组 |
| `tools/dev_setup.sh` 反斜杠转义 | `tools/dev_setup.sh` 的 `"${LOG}"` | 日志落到字面量文件 `$LOG`，退出码取不到真实值 | 人工审阅（未在 CI 跑） |
| PowerShell 缺 `Assert-Ok` | `setup.ps1:12` 起每条原生命令后跟断言 | pip / PyInstaller 失败也能一路走到「完成」，是假成功 | 未在 Windows 执行（§7） |

### 9.9 版本与提交状态（别被 HEAD 误导）

```
$ git show HEAD:bigfish/__init__.py | grep __version__
__version__ = "0.1.1-rc3"

$ grep -n __version__ bigfish/__init__.py    # 工作区
__version__ = "0.1.1-rc4"
```

也就是说 **rc4 的主体改动目前还在未提交的工作区**，已提交的只有 `2ea5d1b`（B2 恢复原子化）
和几条 docs 提交。本轮按约束不允许 `git add / commit / push`，状态就是「工作区已实现并跑绿，尚未提交」。
§9 引用的行号都是工作区版本；复核某条请在当前工作区看，不要 `git show HEAD`。

---

## 附：本轮可复现的最小命令集

```bash
cd /home/dsh/bigfish-council
export LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen

.venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3
.venv/bin/python tools/selfcheck.py | tail -2; echo RC=$?
.venv/bin/python tools/gui_smoke.py | tail -2; echo RC=$?
.venv/bin/python -m pyflakes bigfish tools tests; echo RC=$?
.venv/bin/python -m compileall -q bigfish tools tests; echo RC=$?
```
