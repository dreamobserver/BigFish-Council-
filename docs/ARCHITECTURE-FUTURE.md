# 架构预留：Windows .exe 桌面端 + Android .apk 手机端

> 版本基线：分支 `fix/v0.1.1-rc4-hardening`（HEAD `2ea5d1b`），包版本 `0.1.1-rc3`。
> 性质：**设计预留文档**。本文出现的新端口、新端点、新客户端**均尚未实现**；
> 每条结论都标注了「已实现 / 草案 / 预留」，不把草案写成既成事实。
> 单一事实源：`bigfish/config.py`（端口）、`bigfish/state.py`（状态机）、`bigfish/roles.py`（权限）。

---

## 1. 现有架构盘点

### 1.1 进程与监听面

当前是一个 **单进程 Python 应用**，三个启动入口共用同一套装配逻辑：

| 入口 | 路径 | 说明 |
| --- | --- | --- |
| GUI | `bigfish/gui.py` → `bigfish/__main__.py:main()` | PySide6 主窗口，内嵌 uvicorn 线程 |
| 无头 | `python -m bigfish --headless` | 只起本地 API，不开窗口 |
| 库内 | `bigfish/server.py:create_app()` | 自检脚本 `tools/selfcheck.py` 走这条 |

关键约束（已实现，有测试覆盖）：监听地址校验下沉到 `bigfish/server.py:check_bind_host()`
（`server.py:40-53`），GUI 线程、CLI、`python -m bigfish` 三个入口都必须过这道闸。
非回环地址抛 `BindError`，`__main__.py` 直接返回退出码 `2`。
`bigfish/config.py:is_loopback_host()` 用 `ipaddress.ip_address(h).is_loopback` 判定，
不做「以 `127.` 开头」这种会被 `::1` 漏掉的字符串前缀判断。

数据库：单文件 SQLite，`Settings.db_path` = `%LOCALAPPDATA%\BigFishCouncil\council.db`
（`config.py:55-62`、`:78-79`）。执行票派生密钥独立存放于
`%LOCALAPPDATA%\BigFishCouncil\secrets\ticket.key`（`config.py:87-88`），与数据库分开、
不进导出包。整个进程只开一个数据库连接（`__main__.py` 注释 A.1），
Windows 上两个连接会让恢复撞文件锁。

### 1.2 现有端口

`bigfish/config.py:27-31` 的 `PORT_ROLES` 是唯一真源，只有三项：

```python
PORT_ROLES = {
    "windows_api": (DEFAULT_API_PORT, "Windows 本地控制中心 API 监听端口（默认仅回环）"),
    "vps_proxy":   (DEFAULT_PROXY_PORT, "VPS 上 bfc-proxy.mjs 回环跳板端口"),
    "test_only":   (TEST_API_PORT, "单测与 GUI 冒烟使用的临时端口，不参与部署"),
}

DEFAULT_API_HOST = "127.0.0.1"   # config.py:21
DEFAULT_API_PORT = 3096          # config.py:22
DEFAULT_PROXY_PORT = 3098        # config.py:23
TEST_API_PORT    = 3097          # config.py:24
LOOPBACK_HOSTS   = {"127.0.0.1", "localhost", "::1"}   # config.py:33
```

仓库外还有三组端口在跑，本文一并纳入拓扑表（它们不属于本项目代码，属 `/home/dsh` 下的运维脚本）：

| 端口 | 来源文件 | 实测角色 |
| --- | --- | --- |
| 8765 | windows-mcp 服务本身 | Windows 侧 MCP 端点，`docs/WINDOWS-MCP-RECOVERY.md:30` 用 `http://127.0.0.1:8765/mcp` 探活 |
| 18765 | `/home/dsh/wmcp-proxy.mjs:8` | VPS 回环跳板 → `100.64.0.11:8765`（公司机），http 版为 `wmcp-proxy-http.mjs:15` |
| 18766 | `/home/dsh/wmcp-proxy-home.mjs:8` | VPS 回环跳板 → `100.64.0.12:8765`（家机 CLIENT-A） |

两者存在的原因写在 `docs/NETWORK-TOPOLOGY.md:40-46`：DSH 的 MCP 连接器只接受回环或
RFC1918 地址，而 Tailscale 的 `100.64.0.0/10` 是 CGNAT 段，两个条件都不满足，
所以必须在 VPS 本机用一个回环端口做纯 TCP / HTTP 转发。

`tools/bfc-proxy.mjs:13-16` 的 3098 是同一约束的另一个实例：
`LISTEN_PORT = 3098`、`TARGET_PORT = 3096`、`TARGET_HOST` 默认 `100.64.0.12`。

### 1.3 现有角色模型

`bigfish/roles.py:8-11` 定义四个字符串常量，角色绑定在 `executors.role` / `executors.model`
两个数据库列上（`db.py:89-98`），不靠名字里的 `gpt` / `gemini` 子串猜，
因此**改名不能提权**（`roles.py:59-75` 的 `role_for_db_row`）。

| 角色常量 | 值 | 中文名 | 权限集合（`roles.py:28-38`） |
| --- | --- | --- | --- |
| `ARCHITECT_GPT` | `architect_gpt` | 架构（GPT） | `review.cast`, `task.read`, `audit.read` |
| `REVIEWER_GEMINI` | `reviewer_gemini` | 审查（Gemini） | `review.cast`, `task.read`, `audit.read` |
| `HUMAN_OWNER` | `human_owner` | 人类所有者 | `task.create`, `task.edit`, `task.review_start`, `task.read`, `approve`, `task.cancel`, `task.verify`, `executor.manage`, `backup.export`, `backup.restore`, `audit.read`, `review.cast`, `idempotency.purge`, `ticket.issue` |
| `EXECUTOR_DSH` | `executor_dsh` | 执行器（VPS DSH） | `task.claim`, `run.heartbeat`, `run.submit`, `task.read.own` |

**这是 .exe / .apk 设计的硬边界**：`human_owner` 的权限里含 `approve`、`ticket.issue`、
`backup.restore`，而 HTTP/MCP 面从来不注册这些能力（`server.py:6-9` 注释；
`tools/selfcheck.py:95` 有断言 `check("没有审批类工具", ...)`）。
审批只在进程内通过 `Council.approve()` 完成。

`roles.py:40` 的 `_DB_ROLE_MAP` 把 `executor` → `EXECUTOR_DSH`、`admin` → `HUMAN_OWNER`、
`reviewer` → `None`（再按 model 细分），**未知 db_role 一律落到最低权限
`EXECUTOR_DSH`**（`roles.py:75`）。新增任何移动端角色都必须同步补这张映射，
否则手机会被当成执行器——这是真实的坑，不是理论风险。

### 1.4 现有 REST 端点清单

全部在 `bigfish/server.py:96-243` 的 `create_app()` 里注册。FastAPI 关闭了
`docs_url` / `redoc_url` / `openapi_url`（`server.py:110-111`），所以没有 `/docs`、
`/openapi.json` 这类元数据端点可供侦察。

| 方法 | 路径 | 鉴权 | 权限检查 | 返回 |
| --- | --- | --- | --- | --- |
| GET | `/health` | 无 | 无 | `{status, service, version, time, db, watchdog}` |
| GET | `/api/v1/tasks/available` | Bearer | `task.claim` | `{ok, tasks[]}`（不含 prompt） |
| POST | `/api/v1/tasks/claim` | Bearer | `task.claim` | `{ok, task_id, version, title, goal, scope, risk, prompt, control, ticket, ticket_id, expires_at, max_expires_at}` |
| POST | `/api/v1/runs/heartbeat` | Bearer | `run.heartbeat` | `{ok, task_id, run_id, server_time, task_status, heartbeat_ttl, lease_expires_at, max_expires_at, cancel, pause, cancel_ack_required}` |
| POST | `/api/v1/runs/result` | Bearer | `run.submit` | 任务快照 |
| GET | `/api/v1/tasks/{task_id}/status` | Bearer | `task.read.own` | `{ok, task_id, status, status_label, version, control:{pause,cancel}, blocked_reason}` |
| POST | `/mcp` | Bearer | 见 1.5 | JSON-RPC 2.0 |
| GET | `/mcp` | — | — | 固定 405 `{"error": "只接受 POST"}` |

请求体模型（`server.py:56-75`）：

- `ClaimBody`: `task_id: str`, `idempotency_key: str | None`
- `HeartbeatBody`: `ticket: str`, `status: "running"|"paused"`, `detail: str`, `idempotency_key`
- `ResultBody`: `ticket: str`, `status: "completed"|"failed"|"blocked"|"cancelled"`, `log`, `diff`, `tests`, `note`, `idempotency_key`

错误语义（已实现）：`CouncilError` 统一被 exception_handler 转成 **HTTP 409** +
`{"ok": false, "error": "<中文原因>"}`（`server.py:120-122`）；鉴权失败 401；
越权 403（`_need()`，`server.py:78-85`）；未分配任务对执行器一律 403
（`_owned_status()`，`server.py:246-252`，对应 P1-3.3）。

### 1.5 /mcp 的五个工具

协议版本 `MCP_PROTOCOL = "2025-03-26"`（`server.py:32`）。工具顺序写死在
`TOOL_ORDER`（`server.py:278-284`），规格在 `tool_specs()`（`:287-352`），
权限映射在 `TOOL_PERMISSIONS`（`:356-362`），分发在 `dispatch_tool()`（`:365`）。

| 工具名 | 必需权限 | 作用 |
| --- | --- | --- |
| `council_list_tasks` | `task.claim` | 列出已批准且可领取的任务，**不含 Prompt 正文** |
| `council_claim_task` | `task.claim` | 领取任务，返回不可变 Prompt 与执行票 |
| `council_heartbeat` | `run.heartbeat` | 续租 + 读 `cancel` / `pause` 指令 |
| `council_submit_result` | `run.submit` | 回传 log/diff/tests，完成只到 `SUBMITTED` |
| `council_task_status` | `task.read.own` | 查自己名下任务状态 |

JSON-RPC 错误码（`server.py:171-241`）：`-32001` 鉴权失败，`-32700` 非法 JSON，
`-32600` 批量请求不支持，`-32601` 未知方法/工具，`-32602` arguments 非对象。
业务失败**也是 HTTP 200**，靠 `isError: true` 表达（`server.py:225-235`）——
这一点对移动端很关键，见 3.3。

### 1.6 状态机

任务书写「13 个状态」，实际代码是 **14 个**（`bigfish/state.py:19-38`）——
v0.1.1-rc3 加了 `CANCELLING`，任务书可能还停在改动前的数字。以下按代码为准。

```
DRAFT → IN_REVIEW → REVIEW_PASSED → PENDING_APPROVAL → APPROVED
                                                      │ (claim)
                                                      ▼
                                                 DISPATCHED → RUNNING
                                                      └────► CANCELLING
SUBMITTED ──(verify accept)──► DONE          （completed 只到 SUBMITTED）
任何环节 → BLOCKED / RETURNED / REJECTED / CANCELLED
```

| 状态 | 中文（`state.py:61-76`） | 关键集合（`state.py:40-59`） |
| --- | --- | --- |
| `DRAFT` | 草稿 | `CANCEL_IMMEDIATE` |
| `IN_REVIEW` | 议事中 | `CANCEL_IMMEDIATE`, `REVIEW_GATED` |
| `REVIEW_PASSED` | 双审通过 | `CANCEL_IMMEDIATE`, `REVIEW_GATED` |
| `PENDING_APPROVAL` | 待人类审批 | `CANCEL_IMMEDIATE`, `REVIEW_GATED` |
| `APPROVED` | 已批准/待派工 | `CLAIMABLE`（唯一可开发行票的状态） |
| `DISPATCHED` | 已派工 | `EXECUTOR_ACTIVE`, `RUNNING_STATES`, `CANCEL_NEEDS_ACK` |
| `RUNNING` | 执行中 | 同上 |
| `CANCELLING` | 取消中（待执行器确认） | `EXECUTOR_ACTIVE`, `RUNNING_STATES` |
| `SUBMITTED` | 待验收 | 只有 `verify()` 能出去 |
| `DONE` / `REJECTED` / `CANCELLED` | 已完成 / 已拒绝 / 已取消 | `TERMINAL` |
| `BLOCKED` | 已阻断 | 副作用不明确，交人工 |
| `RETURNED` | 退回修改 | 可重新送审 |

不可动摇的语义（`state.py:1-16` 头注释 + `core.py` 实现）：

1. `comment` 与 `vote` 分离，只有 `vote` 计票（`core.py:381-423`）；
2. GPT 与 Gemini 必须对**同一版本**都投 `approve` 才能进 `REVIEW_PASSED`（`_vote_summary_locked`，`core.py:561`）；
3. `approve` 之后才允许 `claim` 发行票（`state.py:43` 的 `CLAIMABLE = {APPROVED}`）；
4. `completed` 只到 `SUBMITTED`，**绝不自动 DONE**（`core.py:936-940`）；
5. 已派工的取消先进 `CANCELLING`，等执行器心跳读 `cancel=true` 再用 `status=cancelled`
   确认；失联由看门狗置 `BLOCKED`（`core.py:sweep_timeouts`，`:1074`）。

配套常量：`MAX_PARALLEL_PER_EXECUTOR = 4`（`core.py:44`），
`heartbeat_ttl_sec=120`、`ticket_ttl_sec=900`、`ticket_max_ttl_sec=21600`、
`heartbeat_lease_sec=180`、`watchdog_interval_sec=10`（`config.py:70-74`）。
**这五个数字将来必须原样传给 exe 端 GUI 的「已派工超时」倒计时**，
否则手机端与桌面端会显示不同的剩余时间。

### 1.7 GUI 构成（决定 exe 打包范围）

`bigfish/gui.py` 共 1241 行，五个页面类：`OverviewPage:157`、`TaskPage:258`、
`CouncilPage:455`、`ApprovalPage:645`、`MonitorPage:778`，`MainWindow:1010` 负责组装与状态栏。

GUI 与 Council 是**同进程直接调用**，不走 HTTP：`self.win.council.approve(...)`（`gui.py:759`）、
`self.win.council.verify(...)`（`gui.py:770`）、`self.win.council.request_cancel(...)`（`gui.py:888`）。
这是「审批只能在进程内」的实现方式，也是 exe 端能保留审批能力、HTTP 面不能的根本原因。

GUI 自己起 API：`ApiThread`（`gui.py:100-153`）用
`uvicorn.Config(app, host, port, log_level="warning", ...)`，并在 `_resolve_endpoint()`
里读真实 socks 地址解决 `port=0` 的情况；绑定失败必须捕获 `BaseException`
（uvicorn 端口占用抛 `SystemExit`，`gui.py:110`）。

---
## 2. 端口与进程拓扑预留表

### 2.1 三个铁律

1. **所有服务默认只绑 `127.0.0.1`**。`check_bind_host()` 已强制这一点，任何新端口都要复用同一个闸门，而不是各写一份校验。
2. **跨设备一律走 Tailscale tailnet**（`100.64.0.0/10`），禁止 `0.0.0.0`。`python -m bigfish --host 0.0.0.0` 当前直接退出码 2（`docs/NETWORK-TOPOLOGY.md:21-23`）。
3. **不引入 Funnel**（`README.md:6`：「全程不开放任何公网端口，不使用 Tailscale Funnel」）。手机在外网时走 Tailscale 客户端接入 tailnet，不把服务暴露到公网。

### 2.2 端口表

「已实现」判定标准：代码里有监听点或明确的客户端调用点。

| 端口 | 绑定地址 | 协议 | 用途 | 属主进程 | 已实现 | 允许跨机 |
| --- | --- | --- | --- | --- | --- | --- |
| **3096** | `127.0.0.1` | HTTP/1.1 | Windows 本地 API + `/mcp` | `BigFishCouncil.exe`（内嵌 uvicorn 线程） | 是 | 否（仅回环；跨机由 3098 代转） |
| **3097** | `127.0.0.1` | HTTP/1.1 | 单测 / GUI 冒烟临时实例 | `tests/*`、`tools/gui_smoke.py:34` | 是 | 否 |
| **3098** | `127.0.0.1` | HTTP/1.1 | VPS 回环跳板 → Windows `:3096` | `tools/bfc-proxy.mjs`（Node） | 是 | 是（转发目标为 tailnet IP） |
| **3099** | `127.0.0.1` | HTTP/1.1 | **预留**：移动端 REST 网关（配对鉴权、分页、JSON） | 预留：exe 内新增 asyncio 服务 | 否 | 否（由 Tailscale Serve 暴露 3100） |
| **3100** | `127.0.0.1` | HTTP/1.1 + SSE/WS | **预留**：移动端实时事件流 | 预留：同上进程 | 否 | 否 |
| **3101** | `127.0.0.1` | HTTP/1.1 | **预留（可选）**：exe 内嵌静态资源（图标、离线帮助、版本说明） | 预留：`ThreadingHTTPServer` 或直接读文件系统 | 否 | 否 |
| **8765** | `127.0.0.1`（Windows 侧） | HTTP/1.1 | windows-mcp 桌面自动化 MCP | `mcp-server-windows`（独立进程） | 是（仓库外） | 是（经 18765/18766 跳板） |
| **18765** | `127.0.0.1`（VPS 侧） | TCP / HTTP | VPS 跳板 → `100.64.0.11:8765` | `/home/dsh/wmcp-proxy.mjs` | 是（仓库外） | 是 |
| **18766** | `127.0.0.1`（VPS 侧） | TCP / HTTP | VPS 跳板 → `100.64.0.12:8765`（家机） | `/home/dsh/wmcp-proxy-home.mjs` | 是（仓库外） | 是 |

### 2.3 3099 的现存冲突（必须先解决）

`tools/selfcheck.py:19` 已经占用了 3099 作为默认自检端口：

```python
PORT = int(os.environ.get("BFC_SELFCHECK_PORT", "3099"))
```

它没有写进 `PORT_ROLES`，所以文档里看不出来，但**运行时 3099 被自检脚本用掉**。
把 3099 划给移动端 REST 网关之前，必须先改 `tools/selfcheck.py` 的默认值
（建议改 `BFC_SELFCHECK_PORT` 默认 `3095`，或直接默认 `0` 让内核分配——自检本来不需要固定端口）。
这是第 0 阶段的一号动作，见 7.1。

### 2.4 不要直接往 PORT_ROLES 里加预留项

`tests/test_v011_p3.py:45` 有一条断言：

```python
def test_roles_cover_three_ports(self):
    ports = {v[0] for v in PORT_ROLES.values()}
    self.assertEqual(ports, {DEFAULT_API_PORT, DEFAULT_PROXY_PORT, TEST_API_PORT})
```

把 3099/3100/3101 塞进 `PORT_ROLES` 会让这条立刻失败，也会让
`python -m bigfish allowed-ports`（`cli.py:119-121`）输出不存在的端口。
正确做法是**新增一个平行字典**：

```python
# 提案：bigfish/config.py 末尾新增，不改动 PORT_ROLES
PORT_RESERVED = {
    "mobile_rest":     (3099, "预留：apk 移动端 REST 网关"),
    "mobile_events":   (3100, "预留：apk 移动端 SSE/WebSocket 事件流"),
    "embedded_assets": (3101, "预留（可选）：exe 内嵌静态资源"),
}
```

并加新测试：`set(PORT_ROLES) & set(PORT_RESERVED) == set()`，
以及「所有端口去重后与 8765 / 18765 / 18766 不冲突」。这样预留是**声明式**的，不污染运行时。

### 2.5 手机端从哪里进

```
Android apk（Tailscale 在线）─ 100.64.0.0/10 ─► Windows tailnet IP:3100
        └─ 仅当 Tailscale Serve 显式开启，本地转发到 ↓
127.0.0.1:3100 SSE 端点 ─┐
127.0.0.1:3099 REST 网关 ┴► 同一个 Council 实例（单 DB 连接）
```

注意：`127.0.0.1` 是绑定点，`100.x` 是 Tailscale Serve 的**对外呈现**。
Serve 的配置属 Windows 侧运维资产，不进仓库；仓库只需保证端口常量对得上。
手机端不直连 3096，因为 3096 的鉴权模型是「执行器 Bearer」，语义是机器身份，不适合人手持有。

---

## 3. 三层客户端抽象

三层指：**GUI 桌面端**（现状，进程内直调）、**exe 打包端**（同上，只变分发形态）、
**apk 手机端**（纯 HTTP 客户端）。三者共用同一套服务端语义，差异只在传输实现。

### 3.1 ClientTransport 接口

Python 侧协议定义（提案，放新增文件 `bigfish/client/transport.py`）：

```python
from typing import Protocol, Iterator

class ClientTransport(Protocol):
    # ---- 只读 ----
    def health(self) -> dict:
        """GET /health。无鉴权。返回 {status, service, version, time, db, watchdog}。"""

    def list_tasks(self, status: str | None = None, keyword: str = "",
                   offset: int = 0, limit: int = 50) -> dict:
        """已批准且可领取的任务。不含 prompt 正文字段。"""

    def get_task(self, task_id: str, version: int | None = None) -> dict:
        """单个任务快照：含 status / current_version / votes / runs / latest_approval。"""

    def status(self, task_id: str) -> dict:
        """执行器视角的轻量状态：{status, version, control:{pause,cancel}, blocked_reason}。"""

    # ---- 写 ----
    def claim(self, task_id, idempotency_key=None) -> dict:
        """领取任务。返回 {ticket, ticket_id, prompt, expires_at, max_expires_at, ...}。"""

    def heartbeat(self, ticket, status="running", detail="", idempotency_key=None) -> dict:
        """续租。返回 {cancel, pause, cancel_ack_required, lease_expires_at}。"""

    def submit(self, ticket, status, log="", diff="", tests="", note="",
               idempotency_key=None) -> dict:
        """回传结果。status ∈ {completed, failed, blocked, cancelled}。"""

    # ---- 流 ----
    def events(self, task_id: str, since: int = 0) -> Iterator[dict]:
        """增量事件流。since 是上一条事件的 events.id；返回 dict 迭代器。"""
```

八组方法与任务书列的 `health/list_tasks/get_task/claim/heartbeat/submit/status/events`
一一对应；`get_task` 与 `status` 刻意分开，因为**权限不同**：`get_task` 走 `task.read`
（人类/审查角色），`status` 走 `task.read.own`（只能看自己名下任务）。
合并成一个方法会让执行器有机会从「查状态」越权到「看别的任务」。

### 3.2 executor_client.py 如何升级成这一层

现状（`bigfish/executor_client.py`，148 行）：一个类 `ExecutorClient`，构造时从
`BFC_BASE_URL` / `BFC_TOKEN` 环境变量取值（`:33-39`），只有 `urllib.request` 一个后端。

升级路线（**只描述设计，不改代码**）：

1. **抽接口**：新增 `bigfish/client/transport.py` 放 `ClientTransport` 协议（上文）。
2. **搬实现**：把现有 `ExecutorClient` 整体改名为 `HttpTransport`，方法名保持
   `health/list_tasks/claim/heartbeat/submit/task_status` 不变，只把 `task_status`
   换成协议里的 `status`（保留旧名做 `alias = status`）。这样 `run_once()` 与
   `safe_submit()` 的组合逻辑（`:120-148`）不用重写。
3. **保留幂等语义**：`_call()` 里「写操作超时抛 `_UncertainNetworkError`，不盲目重发」
   （`:66-77`）必须留在 transport 层，不能下沉到调用方——这是 v0.1.1-rc3 的 P1-5.3 修复，
   剥离它就等于把「重复执行」的洞开回来。
4. **加第二种实现**：`InProcessTransport`，构造参数是 `Council` 对象，方法体直接
   `self.council.claim(...)`。GUI 改用它之后，`gui.py` 里的 `self.win.council.xxx`
   调用点可逐个替换，测试则注入 `FakeTransport`（返回固定 dict），不再需要起 uvicorn。
5. **不动鉴权**：`Authorization: Bearer` 头格式与
   `council.authenticate(token, role="executor")`（`server.py:113-118`）保持原样，
   避免动 `security.hash_token` 与 `executors.token_hash`。

收益：`tools/selfcheck.py` 与 `tools/e2e_loopback.py` 可只依赖 transport 协议，
在**没有网络**的情况下跑完整闭环（现在必须真起一个 uvicorn）。

### 3.3 手机端为什么不走 MCP

`/mcp` 是 streamable HTTP + JSON-RPC 2.0，协议负担包括：

- `initialize` 三阶段握手与 `notifications/initialized` 通知（`server.py:202-213`）；
- 工具描述 schema 下发与 `tools/call` 的 `content[0].text` 二次 JSON 解析（`server.py:234`）；
- **业务失败藏在 HTTP 200 里**（`isError: true`），Kotlin 侧每个调用点都要多写一层判定；
- 协议版本 `2025-03-26` 一旦升级，客户端要跟着改。

手机端真正需要的只有五件事：登录、看总览、看任务、看事件流、投审查票。
前四件用 REST 一行 OkHttp 调用即可，第五件用 `POST /api/v1/tasks/{id}/review`。
所以：**MCP 只留给 DSH 执行器**（它的连接器天生会说 MCP），手机端与 exe 都走 REST。

### 3.4 apk 的两种技术选型对比

| 维度 | Kotlin + Jetpack Compose | Flutter |
| --- | --- | --- |
| SSE | `OkHttp EventSource` 或 `kotlinx-coroutines` 手写流 | 需 `flutter_client_sse` 之类三方包 |
| 后台保活 | `WorkManager` + `ForegroundService`，系统级 | 同上，但要额外处理 Dart isolate |
| 二进制体积 | 基线约 8–15 MB | 基线约 18–25 MB |
| 与现有 Python 代码复用 | 无 | 无 |
| 建议 | **首选**，事件流与通知最省事 | 将来要 iOS 再切 |

两种都不在手机端跑 Python，见 5.2。

---
## 4. 为 apk 预留的接口契约（草案）

**状态声明：以下端点全部未实现。当前已实现的只有 `GET /health`、`POST /mcp` 与 1.4 表里那五个执行器端点。本节是定稿待实现的契约，不是既有能力。**

统一约定：前缀 `/api/v1`（与既有执行器端点同前缀，不另开 `/mobile`）；请求
`Content-Type: application/json`，响应 UTF-8 JSON；时间戳沿用现有风格 **Unix 秒 float**
（`db.py:now()`），不用 ISO 8601；所有响应带 `server_version`（`bigfish.__version__`）
与 `schema_version`（见 6.2）；错误体统一
`{"ok": false, "code": "<机器可读>", "error": "<中文>"}`。
现有 `CouncilError` handler 只有 `{ok, error}`（`server.py:122`），**新增 `code` 是向后兼容的加字段**。

### 4.1 POST /api/v1/auth/token

用一次性配对码换长期 token，避免用户在手机上手动粘贴 Bearer。

```http
POST /api/v1/auth/token

{"pairing_code": "482913", "device_name": "Pixel 8",
 "client_version": "0.2.0", "device_id": "a1b2c3d4e5f6"}
```

`pairing_code` 6 位数字，Windows GUI 显示，TTL 300 秒；`device_name` 落库到 `executors.name`；
`device_id` 设备指纹，用于同设备重配对。响应 200：

```json
{"ok": true, "token": "bfc_mb_9f3c...", "token_id": "ex-7f2a1b3c",
 "role": "human_owner", "expires_at": 1767225600.0,
 "server_version": "0.1.1-rc3", "api_version": 1}
```

`token` 明文只返回一次，与 `register_executor()` 同语义（`core.py:153-187` 注释）；
`expires_at` 180 天，null 表示不过期（不建议）。

| HTTP | code | 含义 |
| --- | --- | --- |
| 401 | `pairing_invalid` | 不存在或已消费 |
| 410 | `pairing_expired` | 超过 300 秒 |
| 429 | `pairing_locked` | 同 IP 连续失败 5 次后锁 15 分钟 |
| 403 | `device_revoked` | `executors.enabled=0` |
| 426 | `client_too_old` | 见 6.3 |

配对码由 `human_owner` 在 GUI 主动生成（建议在 `gui.py:OverviewPage` 加按钮），内存里只存
`hash_token(code)` + 过期时间；落库复用 `executors` 表（新增 `role='mobile'`），不新开表。
但**必须同步补 `roles._DB_ROLE_MAP`**（`roles.py:40`），否则未知 db_role 会收敛到最低权限
`EXECUTOR_DSH`（`roles.py:75`），手机会被当成执行器——这是真实的坑。

### 4.2 GET /api/v1/overview

```json
{"ok": true,
 "counts": {"DRAFT": 2, "PENDING_APPROVAL": 1, "RUNNING": 1, "BLOCKED": 0}, "total": 4,
 "active": [{"id": "task-1a2b3c4d5e", "title": "...", "status": "RUNNING",
             "current_version": 3, "risk": "medium", "updated_at": 1767225600.0}],
 "waiting_approval": [], "blocked": [],
 "executor_online": true, "server_version": "0.1.1-rc3", "api_version": 1}
```

字段直接对齐 `Council.overview()`（`core.py:1185-1201`），它已给出
`counts/active/waiting_approval/blocked/total`，这里只多包一层。新增 `executor_online`
需查 `runs.last_beat_at` 或 `executors.last_seen`，判定窗口用 `heartbeat_ttl_sec=120`
（`config.py:70`）。权限 `task.read`；错误 401 / 403 / 426。

### 4.3 GET /api/v1/tasks

`GET /api/v1/tasks?status=RUNNING&keyword=部署&offset=0&limit=50`

| 参数 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `status` | string | 空（全部） | 必须是 `state.ALL` 之一，否则 422 |
| `keyword` | string | 空 | 匹配 `title` 或 `goal`，沿用 `list_tasks` 的 LIKE |
| `offset` | int | 0 | ≥ 0 |
| `limit` | int | 50 | 1–200；现有 `list_tasks` 硬上限 200（`core.py:277`） |

```json
{"ok": true, "total": 37, "offset": 0, "limit": 50,
 "items": [{"id": "task-1a2b3c4d5e", "title": "部署到 staging", "goal": "...",
            "status": "PENDING_APPROVAL", "status_label": "待人类审批",
            "current_version": 3, "risk": "medium", "executor_id": null,
            "blocked_reason": null, "updated_at": 1767225600.0, "has_prompt": true}]}
```

**列表绝不返回 `prompt` 正文**——沿用 `council_list_tasks` 的既有约束（`server.py:291`；
`selfcheck.py:105` 有断言 `check("列表不含 Prompt", ...)`）。列表只给 `has_prompt` 布尔，
正文在详情页按需拉。

### 4.4 GET /api/v1/tasks/{id}

`GET /api/v1/tasks/task-1a2b3c4d5e?version=3`；`version` 可选，省略时返回
`current_version`，传旧版本时 `prompt` 取 `task_versions` 表对应行（`db.py:52-62`）。

```json
{"ok": true, "server_version": "0.1.1-rc3",
 "task": {"id": "task-1a2b3c4d5e", "title": "部署到 staging",
  "goal": "...", "scope": "...", "risk": "medium",
  "status": "PENDING_APPROVAL", "status_label": "待人类审批",
  "current_version": 3, "requested_version": 3, "prompt": "……仅详情页返回……",
  "control": {"pause_requested": false, "cancel_requested": false},
  "blocked_reason": null, "executor_id": null,
  "created_at": 1767225000.0, "updated_at": 1767225600.0,
  "votes": {"gpt": {"actor": "architect_gpt", "verdict": "approve", "at": 1767225400.0},
            "gemini": {"actor": "reviewer_gemini", "verdict": "approve", "at": 1767225450.0},
            "complete": true, "all_approved": true},
  "reviews": [{"id": 12, "model": "gpt", "kind": "vote", "verdict": "approve",
               "content": "...", "version": 3, "created_at": 1767225400.0}],
  "runs": [], "latest_approval": null, "versions": [1, 2, 3]}}
```

字段对应 `Council.get_task()`（`core.py:265-275`）已算好的
`votes/reviews/runs/latest_approval`，**不要另算一遍**，否则会出现「手机显示可批准、
点下去 409」的错位。错误：404 `task_not_found`；403 `task_forbidden`（执行器查别人任务，
对齐 `server.py:246-252`）；422 `version_not_exist`。

### 4.5 GET /api/v1/tasks/{id}/events（SSE）

```http
GET /api/v1/tasks/task-1a2b3c4d5e/events?since=0
Accept: text/event-stream
```

`since` = 上次收到的最后一条事件 `events.id`（整数，`db.py:135-141` 的
`INTEGER PRIMARY KEY AUTOINCREMENT`）；`since=0` 表示要全部历史（首次同步）。

```
event: snapshot
id: 4711
data: {"task_id":"task-1a2b3c4d5e","status":"RUNNING","version":3,"cursor":4711}

event: event
id: 4712
data: {"kind":"run.heartbeat","detail":"running 正在部署","created_at":1767225610.0}

: keepalive

event: stream_end
data: {"cursor":4713}
```

- **先快照后增量**：首帧 `snapshot` 带当前 `status/version`，随后逐条补事件。手机端存 `cursor`，断线重连用它做 `since` 恢复，不漏事件也不重复弹通知。
- **keepalive 每 15 秒一次**（注释帧，不占 id）。移动网络 NAT 超时通常 30–60 秒，15 秒是保守值；不能用空 `data:` 帧，否则部分客户端会当成空事件。
- **单连接最长 10 分钟**，之后发 `stream_end` 主动关闭，客户端带 `cursor` 重连。绕开反向代理连接时长上限，也避免长连接在手机休眠后变僵尸。
- 事件体**必须已脱敏**：落库前已过 `scrub()`（`core.py:140-146`），事件流直接复用 `Council.events(task_id, limit)`（`core.py:147-152`）的输出，**不要再拼新 detail 字符串**——任何新拼的字符串都绕过了脱敏。
- 实现：现有 `create_app()` 全是同步 `def` 路由（`server.py:125` 等），SSE 必须写成 `async def` + `StreamingResponse`，用 `asyncio.sleep` 轮询 `events.id > since`（SQLite 没有 LISTEN/NOTIFY，间隔建议 1 秒）。

错误：404 / 403；同一 `task_id` 已有 5 条活跃流时 429 `too_many_streams`。

### 4.6 POST /api/v1/tasks/{id}/review

人类代投审查票（可转述 GPT / Gemini 意见，也可直接投票）。

```http
POST /api/v1/tasks/task-1a2b3c4d5e/review

{"model": "gpt", "kind": "vote", "verdict": "approve",
 "content": "同意，风险可控", "version": 3, "expected_version": 3}
```

`kind` ∈ {comment, vote}；`version` 必须显式给，不允许隐式取 current；`expected_version`
可选，给了就做乐观并发。响应 200：

```json
{"ok": true, "review_id": 13, "task_status": "PENDING_APPROVAL", "task_version": 3,
 "votes": {"gpt": {"verdict": "approve"}, "gemini": {"verdict": "approve"},
           "complete": true, "all_approved": true}}
```

约束全部来自 `add_review()`（`core.py:381-423`）：`model` 只能 `gpt` / `gemini`；
`kind=vote` 时 `verdict` 只能 `approve` / `reject`；投完自动触发 `_recompute_review()`
（`core.py:518`）。

**关键边界**：手机端 `actor` 固定 `human_owner`（转述），服务端必须**忽略请求体里的任何
`actor` 字段**，从 token 查出。现有 `add_review` 的 `actor` 语义是「调用方鉴权上下文传入」，
HTTP 层若直接读 body 就是提权漏洞。同理 `source` 固定 `"mobile"`，不接受客户端指定。
错误：401 / 403（`review.cast`）/ 404 / 409 `version_conflict` / 422 `invalid_model`。

### 4.7 POST /api/v1/tasks/{id}/approve

必须带 `expected_version` 做乐观并发。

```http
POST /api/v1/tasks/task-1a2b3c4d5e/approve

{"decision": "approve", "expected_version": 3, "note": "同意执行，注意回滚窗口"}
```

`decision` ∈ {approve, reject, return}；`expected_version` **必填**，缺失直接 428。
响应 200：

```json
{"ok": true, "task_id": "task-1a2b3c4d5e", "status": "APPROVED",
 "status_label": "已批准/待派工", "version": 3, "approved_at": 1767225700.0}
```

| HTTP | code | 情形 |
| --- | --- | --- |
| 428 | `precondition_required` | 缺 `expected_version` |
| 409 | `version_conflict` | 版本不一致（响应体带 `current_version`） |
| 409 | `votes_incomplete` | 双票未齐 |
| 409 | `votes_not_approved` | 双票未全 approve |
| 409 | `status_not_allowed` | 状态不允许 |
| 403 | `forbidden_decision` | 非 `human_owner` |

这些 code 对应 `Council.approve()` 里的四条 `raise CouncilError`（`core.py:588-624`）。
现有实现已强制 `actor in (R.HUMAN_OWNER,)`（`core.py:587`），手机 token 必须映射到
`human_owner` 才能审批——但见 8.1，手机端建议只给 review 不给 approve。409 响应体：
`{"ok": false, "code": "version_conflict", "error": "任务版本已变（当前 v4，请求基于 v3）",
"current_version": 4, "server_version": "0.1.1-rc3"}`。

### 4.8 GET /api/v1/health

无 Authorization。响应 200：

```json
{"ok": true, "api_version": 1, "min_client_version": "0.2.0",
 "server_version": "0.1.1-rc3", "schema_version": 3,
 "features": ["overview", "tasks", "events_sse", "review"],
 "server_time": 1767225800.0}
```

**不含任何任务、令牌、执行器或配置信息**——约束与现有 `/health` 一致（`server.py:124-141`
注释明写「不返回任何任务、令牌或配置细节」；`selfcheck.py:80` 还断言
`"tasks" not in json.dumps(health)`）。`features` 是能力声明，不泄露数据；
`min_client_version` 服务于 6.3。两个健康端点**并存不合并**：`/health` 给执行器与 3098
跳板探活（`tools/bfc-proxy.mjs` 有路径白名单），`/api/v1/health` 给手机握手。

### 4.9 契约总览表

| # | 方法 | 路径 | 鉴权 | 权限 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 1 | POST | `/api/v1/auth/token` | 配对码 | — | 草案 |
| 2 | GET | `/api/v1/overview` | Bearer | `task.read` | 草案 |
| 3 | GET | `/api/v1/tasks` | Bearer | `task.read` | 草案 |
| 4 | GET | `/api/v1/tasks/{id}` | Bearer | `task.read` | 草案 |
| 5 | GET | `/api/v1/tasks/{id}/events` | Bearer | `task.read` | 草案 |
| 6 | POST | `/api/v1/tasks/{id}/review` | Bearer | `review.cast` | 草案 |
| 7 | POST | `/api/v1/tasks/{id}/approve` | Bearer | `approve` | 草案（默认关闭） |
| 8 | GET | `/api/v1/health` | 无 | — | 草案 |
| — | GET | `/health` | 无 | — | **已实现** |
| — | POST | `/mcp` | Bearer | 见 1.5 | **已实现** |
| — | 五 | `/api/v1/tasks/*`、`/api/v1/runs/*` | Bearer | 执行器四权限 | **已实现** |

---
## 5. 打包边界与体积

### 5.1 exe：PyInstaller onedir

现有 `build.ps1:28-42` 已经是 `--onedir`，方向正确，不用改。理由：

| 维度 | onedir（现用） | onefile |
| --- | --- | --- |
| 启动耗时 | 约 1–2 秒（直接 map 已解压的 Qt 库） | 约 5–15 秒（每次启动把 PySide6 解压到 `%TEMP%\_MEIxxxxx`） |
| Qt 插件 | `platforms/qwindows.dll` 等按目录结构就位 | 解压路径变化时插件定位失败，黑窗闪退 |
| 首启杀软扫描 | 只扫 exe + dll，命中率低 | 单个 200 MB+ 巨型 exe，误报率高 |
| 增量更新 | 替换 `bigfish` 目录下的 pyz，不重发 Qt | 必须整包重发 |
| 体积 | ~190–260 MB（PySide6 占大头） | 略小（压缩后），但启动时翻倍占盘 |

结论：**继续 onedir**，产物路径 `dist\BigFishCouncil\BigFishCouncil.exe`（`build.ps1:50`）。

hiddenimports 清单（在现有四条基础上补齐）：

```powershell
--hidden-import bigfish.gui --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto
--hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.http.h11_impl
--hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on
--hidden-import uvicorn.protocols.websockets.websockets_impl
--hidden-import fastapi --hidden-import pydantic --hidden-import sqlite3
--collect-submodules bigfish
--exclude-module tkinter --exclude-module matplotlib
--exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets
```

为什么 `h11_impl` 和 `websockets_impl` 要显式列：`requirements.txt` 里 uvicorn 是
`>=0.30,<1.0`，它用 importlib 动态选实现，PyInstaller 的静态分析看不到这条路径，
漏了就是启动即 `ModuleNotFoundError` 或者 SSE 直接 500。

数据目录**不变**：`%LOCALAPPDATA%\BigFishCouncil`（`config.py:59-62`）。
绝不改成 exe 旁边的相对目录——onedir 目录常在 `Program Files` 下，普通用户对该目录无写权限，
`Settings.ensure_dirs()`（`config.py:98-106`）会直接抛 `OSError`。
`chmod(0o700)` 在 Windows 上是 no-op（`try/except OSError` 兜住，`config.py:102-106`），
真正的 ACL 收紧靠安装器，不靠这行代码。

exe 端还要新增两件事（第 2 阶段）：

1. **单实例锁**：命名互斥量 `Global\BigFishCouncil`，防止两次双击起两个 uvicorn 抢 3096。
   端口占用会抛 `SystemExit`，现有代码已兜住（`gui.py:110`），但用户看到的是
   「启动失败」，体验差。
2. **退出清理**：`MainWindow.closeEvent` 里已有 `council.stop_watchdog()`（`gui.py:1184`），
   需补 uvicorn 的 `should_exit` 与 `ApiThread.stop(timeout=15)`
   （`gui.py:148-153` 已实现，只是没在所有路径调用）。

### 5.2 apk：不在手机上跑 Python

手机端是**纯 HTTP 客户端**，代码里没有 Python 运行时、没有 Chaquopy、没有 BeeWare。
理由不是体积，是一致性：

- 审批逻辑在 `core.py` 里，包含 `_vote_summary_locked`、乐观并发、执行票派生
  （`security.derive_ticket`）。在手机上复刻一份 = 两份实现 = 一定会分叉；
- 数据库在 Windows 上（`council.db` 单连接，`__main__.py` 注释 A.1），
  手机端就算跑 Python 也拿不到那个连接；
- 执行票密钥 `ticket.key` 绝不能出现在手机上（`config.py:87-88` 的设计意图
  就是「与数据库分开存放」）。

所以 apk 的 MVB 只有四屏：登录（配对码）→ 总览 → 任务列表/详情 → 事件流。

### 5.3 云端 VPS 只做跳板与中继

明确边界（对齐 `docs/NETWORK-TOPOLOGY.md:48-58` 与 `requirements.txt:1-8`）：

- VPS 上**不跑 GUI**（没有 X11，也不需要 X11）；
- VPS 上**不跑 Chromium / Electron**（`requirements.txt` 第 2 行明写
  「无 Chromium / Electron / Node 常驻组件」，唯一例外是 `tools/bfc-proxy.mjs`
  这个纯 Node 标准库转发脚本）；
- VPS 只做两件事：`bfc-proxy.mjs`（3098）与 `wmcp-proxy*.mjs`（18765/18766）；
- 手机在外网时 VPS **可以**做中继，但中继的是未加密的 HTTP 请求，
  所以中继路径必须仍在 tailnet 内部（VPS 与 Windows 都在 tailnet 上），不能有公网暴露面。

### 5.4 「远程对话漫游」的三种方案

如果手机端要做的不只是审批，而是**继续和 DSH 对话**（把 DSH 消息通道接到手机），
三种方案与各自风险：

| 方案 | 做法 | 主要风险 |
| --- | --- | --- |
| A. Tailscale Serve + REST 轮询 | Serve 把 `127.0.0.1:3099` 暴露给 tailnet，手机 `GET /api/v1/conversations/{id}/messages?since=` | 轮询延迟 1–3 秒；每次请求带 Bearer，日志若记 URL 会泄露 id；Serve 配错会意外放大暴露面（必须只 expose 3099，不 expose 3096） |
| B. 反向 WebSocket 网关 | Windows 侧主动连出到 VPS 的 WS 端点，手机也连 VPS，VPS 做配对转发 | 引入长连接状态机与重连风暴；VPS 成单点，且 WS 帧里是整个对话正文，VPS 内存里出现明文；鉴权必须两头都做，否则 VPS 被拿下 = 对话全泄 |
| C. 经 VPS 中转（HTTP 代理式） | 手机 → VPS Nginx/Caddy → tailnet → Windows | 多一跳 TLS 终止点；VPS 访问日志默认记 URI，需显式关掉；证书管理成本；本质上把「私网」变成「有一个公网入口的私网」 |

推荐顺序：**A → 暂不做 B/C**。理由：A 复用现有 Bearer 与 3099 网关，不新增信任边界；
B 和 C 都会把 VPS 从「无状态转发器」升级成「有状态中间人」，与 5.3 的边界冲突。

另外，DSH 的消息通道本身不在本仓库（本项目只提供任务板与审批），
所以「对话漫游」在架构上属于**跨仓库集成**，本文只固定端口与鉴权约定，不设计消息协议。

### 5.5 体积预算

| 组件 | 估算 | 说明 |
| --- | --- | --- |
| PySide6（onedir） | 150–210 MB | 含 `Qt6Core/Gui/Widgets`，已排除 WebEngine |
| Python 3.11 运行时 | 25–35 MB | `python311.dll` + 标准库 zip |
| fastapi / uvicorn / pydantic | 12–18 MB | pydantic v2 含编译扩展 |
| bigfish 自身 | < 1 MB | 15 个 py 文件 |
| **exe 合计** | **约 190–260 MB** | 压缩后安装包约 70–100 MB |
| apk（Compose） | 8–15 MB | 无 native 依赖时 |
| apk（Flutter） | 18–25 MB | 含 Flutter engine |

---

## 6. 版本与兼容策略

### 6.1 API 版本前缀

- 所有新端点挂 `/api/v1`。**既有五个执行器端点已经是 `/api/v1/...`**（`server.py:144-169`），
  前缀天然对齐，不需要迁移。
- 破坏性变更（删字段、改类型、改语义）升 `/api/v2`，v1 至少保留**一个版本周期**
  （一个 minor 版本的生命周期，建议 6 个月）。
- 非破坏性变更（加字段、加端点、加可选参数）**不升版本**。现有响应已带 `ok` 包装，
  客户端必须忽略未知字段——这条要写进 apk 的 JSON 解析配置（Kotlin 用
  `ignoreUnknownKeys = true`，Dart 用 `json_serializable` 的宽松模式），
  否则加一个 `executor_online` 就会崩。
- 版本路由不要用 header 协商（`Accept: application/vnd.bfc.v2+json`），用路径前缀。
  理由：手机抓包、日志、防火墙规则都按路径走，header 协商在中间设备上会被吃掉。

### 6.2 schema_version 与 API 版本解耦

两者独立演进，不互相推导：

| 版本号 | 位置 | 变更含义 | 当前值 |
| --- | --- | --- | --- |
| `__version__` | `bigfish/__init__.py:6` | 发行版语义化版本 | `0.1.1-rc3` |
| `api_version` | 响应体 / `/api/v1/health` | HTTP 契约大版本 | `1` |
| `schema_version` | SQLite `meta` 表（`db.py:30-33`） | 数据库结构版本 | 由 `Database.init_schema()` 维护 |
| `min_client_version` | `/api/v1/health` | 服务端要求的最低客户端版本 | 随发布调 |

解耦的意义：数据库加了索引（schema_version +1）不应该逼所有手机升级；
而 API 删了一个字段（api_version 2）必须逼手机升级。触发条件完全不同。

`schema_version` 的降级保护已有实现——`DatabaseVersionError`（`__main__.py:29-32`
捕获后返回退出码 `3`）。这条**必须保留**：新版写过的库被旧 exe 打开时，
宁可拒绝启动，也不能让旧代码改坏新结构。

### 6.3 客户端版本上报与拒绝

所有请求带两个头（不是 body 字段，头更省事且不会被业务校验吞掉）：

```http
X-BFC-Client: android/0.2.0
X-BFC-Device: a1b2c3d4e5f6
```

服务端逻辑（提案）：

1. `/api/v1/health` 与 `/health` 不校验版本（否则旧客户端连「为什么被拒」都问不到）；
2. 其余端点解析 `X-BFC-Client`，与 `min_client_version` 比较；
3. 版本过低返回 **426 Upgrade Required** +
   `{"code": "client_too_old", "min_client_version": "0.2.0", "download_url": "..."}`；
4. 版本解析失败按过低处理（fail-closed），不要 fail-open；
5. 比较用 `packaging.version.parse`，不要手写字符串比较
   （`0.10.0` vs `0.9.0` 会错）。

为什么用 426 而不是 403：403 的语义是「你没有权限」，会诱导用户去检查账号；
426 明确告诉客户端「你该升级了」，Kotlin 侧可统一拦截并弹更新引导。

### 6.4 数据迁移与回滚

- 迁移只允许**向前**。回滚靠备份，不靠下行 SQL——`backup.export_bundle()` 用
  sqlite3 的 backup API 做一致性快照（`core.py:1205-1207`），恢复时先备份现有库为
  `council.db.prerestore`（`gui.py:993`）。
- 密钥与数据库必须**成对替换**：`backup.ensure_key_consistency()`（`runtime.py:15-20`）
  会在指纹不一致时轮换密钥，保证旧执行票不复活。这是 B2 自愈，不能被 exe 打包改动破坏。
- exe 升级时如果 schema_version 升级失败，必须回滚到升级前的备份，而不是留下半迁移状态。

---
## 7. 迁移步骤（rc4 → exe/apk）

### 7.1 阶段 0：端口与常量预留 + 接口契约定稿（**当前 rc4 只做这一步**）

| 项 | 内容 |
| --- | --- |
| **目标** | 把 3099/3100/3101 以「预留」形式写进代码与文档；解决 3099 与 `tools/selfcheck.py:19` 的冲突；把本文第 4 章的契约冻结为评审基线。**不实现任何新端点。** |
| **改动文件** | `bigfish/config.py`（新增 `PORT_RESERVED` 字典，不动 `PORT_ROLES`）；`tools/selfcheck.py`（默认端口 3099 → 3095）；`bigfish/cli.py`（`allowed-ports` 同时打印预留表）；`tests/test_v011_p3.py`（新增两条断言）；`docs/ARCHITECTURE-FUTURE.md`（本文） |
| **验收标准** | ① `python -m bigfish allowed-ports` 输出 6 行（3 已用 + 3 预留）且端口无重复；② `python -m unittest discover -s tests` 全绿，**原 `test_roles_cover_three_ports` 不动**；③ `python tools/selfcheck.py` 仍 16/16，日志里出现的端口不再是 3099；④ 全文无新增路由，`grep -c '@app\.' bigfish/server.py` 数量不变（当前 8 个装饰器：`/health`、5 个 `/api/v1`、`POST /mcp`、`GET /mcp`） |
| **风险** | 低。纯声明式改动。唯一真实风险是有人顺手把预留项塞进 `PORT_ROLES`，触发 `test_roles_cover_three_ports` 失败——用新测试把这条路堵死 |

### 7.2 阶段 1：ClientTransport 抽象 + 进程内实现

| 项 | 内容 |
| --- | --- |
| **目标** | 把 `executor_client.py` 抽象成协议，新增 `InProcessTransport`，让 GUI 与测试都能注入 |
| **改动文件** | 新增 `bigfish/client/__init__.py`、`bigfish/client/transport.py`（协议）、`bigfish/client/http.py`（搬 `ExecutorClient`）、`bigfish/client/local.py`（`InProcessTransport`）；改 `bigfish/executor_client.py` 为向后兼容 re-export（`from .client.http import HttpTransport as ExecutorClient`）；`bigfish/gui.py` 逐步替换 `self.win.council.xxx` 调用点 |
| **验收标准** | ① `tools/selfcheck.py` 与 `tools/e2e_loopback.py` 都能只用 `ClientTransport` 完成闭环；② `from bigfish.executor_client import ExecutorClient` 仍不报错；③ 幂等与 `_UncertainNetworkError` 语义不变，`safe_submit` 行为有测试覆盖；④ GUI 冒烟 9 项仍通过（`tools/gui_smoke.py`） |
| **风险** | 中。`_call()` 的重试语义容易被「优化」掉，一旦写操作被盲目重发，就会重复执行。验收必须包含「模拟超时后只重发一次且复用同一幂等键」的用例 |

### 7.3 阶段 2：exe 打包加固

| 项 | 内容 |
| --- | --- |
| **目标** | 双击可用、单实例、干净退出、体积可控 |
| **改动文件** | `build.ps1`（补 hiddenimports、`--exclude-module`、版本资源文件）；`bigfish/gui.py`（单实例互斥量、`closeEvent` 停 API 线程）；`tools/_entry.py`（构建时生成，逻辑不变）；新增 `installer/bigfish.iss`（Inno Setup，可选） |
| **验收标准** | ① 冷启动到主窗口可见 ≤ 3 秒（onedir 实测基线）；② 双击两次只出一个窗口、只监听一个 3096；③ 关闭后 `netstat -ano | findstr 3096` 无残留；④ `%LOCALAPPDATA%\BigFishCouncil\council.db` 首次运行后存在；⑤ 在无 Python 的干净 Windows 上可运行；⑥ 体积 ≤ 260 MB |
| **风险** | 中。PySide6 的 `platforms/qwindows.dll` 与 `styles/` 插件目录若被错误的 `--add-data` 打乱，表现是「双击无反应」。必须在干净机器上验，不能只在开发机验（开发机有 Python 与 Qt 环境变量兜底） |

### 7.4 阶段 3：移动端 REST 网关 + SSE

| 项 | 内容 |
| --- | --- |
| **目标** | 实现 4.1–4.8 八个端点中的 1、2、3、4、5、8（**不实现 approve**，先只做只读 + review） |
| **改动文件** | 新增 `bigfish/mobile/__init__.py`、`bigfish/mobile/auth.py`（配对码）、`bigfish/mobile/routes.py`（FastAPI `APIRouter`）、`bigfish/mobile/sse.py`（事件流）；改 `bigfish/server.py`（`include_router`，第一版仍只绑 3096，**不新开 3099 监听**——把移动端路由挂在 3096 上，靠 Tailscale Serve 暴露）；改 `bigfish/config.py`（`mobile_enabled: bool = False`） |
| **验收标准** | ① `mobile_enabled=False` 时所有移动端路由 404，既有 8 个路由行为零变化；② 配对码 300 秒过期、5 次失败锁定有测试；③ SSE 在 10 分钟内至少发 40 次 keepalive 且 `since` 续传不丢事件；④ 移动端 token 访问 `/api/v1/tasks/available` 必须 403（人类角色没有 `task.claim`）；⑤ 事件流响应体里 grep 不到任何 `bfc_` 前缀 token |
| **风险** | 高。这是**第一次把人类可读数据放到 HTTP 面上**，此前 HTTP 只给执行器看任务摘要。三条必须守住：列表不返回 prompt；`review` 的 `actor` 只从 token 取；`approve` 本期不做。SSE 的 `async def` 与现有同步路由混用时注意 uvicorn 线程池耗尽（`create_app()` 里全是 `def`，跑在 threadpool 里） |

### 7.5 阶段 4：apk + exe 联调与发布

| 项 | 内容 |
| --- | --- |
| **目标** | 手机端四屏可用；exe 与 apk 同版本发布；`/api/v1/health` 的能力协商跑通 |
| **改动文件** | 新增 `mobile-android/`（Kotlin 工程，不进 Python 包）；`bigfish/mobile/routes.py`（补 4.7 `approve`，默认关闭，靠 `config.json` 的 `mobile_allow_approve` 开关）；`docs/DELIVERY-*.md`（发布记录） |
| **验收标准** | ① 手机扫码配对后能看总览与任务列表；② 断网 30 秒再恢复，SSE 用 `cursor` 续传且不重复弹通知；③ 手机投 review 后，Windows GUI 的「议事厅」页同一秒内可见（靠 `_recompute_review` + 事件流）；④ 手机端**没有** approve 按钮（除非显式开开关）；⑤ 旧版 apk（0.1.x）连新版服务端得到 426 而非崩溃 |
| **风险** | 高。跨网络状态同步是经典坑：手机锁屏后 SSE 断开，唤醒时若用 `since=0` 重放会把所有历史事件当新通知弹一遍。必须强制「本地存 cursor，醒来先 `GET /api/v1/tasks/{id}` 拉快照，再用 `since=cursor` 补增量」 |

### 7.6 阶段依赖图

```
阶段0（常量+契约）──► 阶段1（Transport 抽象）──► 阶段3（REST+SSE）──┐
        └──────────► 阶段2（exe 打包）──────────────────────────────┴► 阶段4
```

阶段 0 与阶段 2 可并行（一个改常量，一个改打包脚本，文件不重叠）。
阶段 1 必须先于阶段 3，否则移动端路由会直接依赖 `Council` 内部方法，抽象就白做了。

---

## 8. 明确不做什么

以下九条是**设计排除项**，不是「暂缓」也不是「以后再说」。任何一条被突破，本文的边界论证都失效。

1. **不在手机端实现审批签名。** `core.py:587` 的 `actor in (R.HUMAN_OWNER,)` 是进程内身份，token 只是 bearer 字符串，谁拿到谁就是人类。手机丢失 = 审批权丢失。所以 4.7 的 `approve` 默认关闭（`mobile_allow_approve=False`），真要开也必须叠加设备级二次确认（生物识别 + 短时 PIN），且这条二次确认**在服务端校验**，不是客户端画个框。
2. **不做公网直连。** 不开 Funnel（`README.md:6`），不绑 `0.0.0.0`（`check_bind_host()` 已在拦），不在路由器上做端口映射。手机在外网时必须先连 Tailscale，这不是体验优化，是鉴权模型的前提。
3. **不在 VPS 跑 GUI / Chromium / Electron。** VPS 只跑两个 Node 转发脚本（`tools/bfc-proxy.mjs`、`wmcp-proxy*.mjs`）。`requirements.txt:2` 的「无 Chromium / Electron / Node 常驻组件」是硬约束，新方案若需要浏览器内核才能渲染，直接否掉。
4. **不做无鉴权的局域网发现。** 不实现 mDNS / UDP 广播 / `/24` 段扫描「自动找到 Windows 主机」。发现即攻击面：同一咖啡馆 WiFi 下就能枚举出所有实例。配对只走「GUI 显示 6 位码 + 手机输入」这一条人工带外信道。
5. **不在手机端落任何执行票或密钥。** `ticket.key`（`config.py:87-88`）永不出 Windows；执行票本身（`claim()` 返回的 `ticket` 字段）也永不进移动端——手机只能看任务状态摘要。执行票是机器身份，人手持有它就是事故。
6. **不做「自动批准」。** 不因为双票都 approve 了就在服务端自动 `approve`。`state.py:6-7` 的「人类批准之后才允许派工」是这套系统的存在理由，加一条自动路径等于把四角色模型退化成三角色。
7. **不让 API 版本与 schema 版本联动升级。** 见 6.2。禁止「加了张表就把 api_version 升到 2」这种做法——那会逼所有手机在无关变更上升级。
8. **不在移动端复刻业务规则。** 手机端不做「双票齐了没」「版本对不对」的本地判断，一律以服务端返回的 `votes.complete` / `votes.all_approved` 为准（4.4）。客户端侧判断与 `_vote_summary_locked`（`core.py:561`）一旦不同步，就会出现「按钮亮着但点下去 409」的经典错位。
9. **不把 `/mcp` 当移动端 API。** 见 3.3。MCP 的 `isError: true` + HTTP 200 组合，在移动端会导致错误处理全线错位。

---

## 附录 A：文件索引与实现状态对账

| 文件 / 对象 | 关键行 | 内容 | 状态 |
| --- | --- | --- | --- |
| `bigfish/config.py` | `6`, `21-24`, `27-31`, `36-52`, `55-62`, `70-74`, `87-88` | 端口常量、`PORT_ROLES`、回环判定、数据目录、超时常量、密钥路径 | 已实现 |
| `bigfish/server.py` | `32`, `40-53`, `56-75`, `78-85`, `110-111`, `120-141`, `144-169`, `171-243`, `246-252`, `278-284`, `287-352`, `356-362` | 绑定闸门、请求模型、权限、8 个路由、MCP、五工具 | 已实现 |
| `bigfish/gui.py` | `100-153`, `157`, `258`, `455`, `645`, `759`, `770`, `778`, `888`, `1010`, `1184` | API 线程、五页面、进程内审批调用 | 已实现 |
| `bigfish/core.py` | `44`, `140-152`, `265-289`, `381-423`, `518`, `561`, `581-624`, `671-760`, `818-890`, `881-958`, `1038-1057`, `1185-1201` | 并发上限、事件、读写、投票重算、审批、领取、心跳、回传、验收、总览 | 已实现 |
| `bigfish/state.py` | `19-38`, `40-59`, `61-76` | **14 个**状态、集合、中文标签（不是 13 个） | 已实现 |
| `bigfish/roles.py` | `8-11`, `28-38`, `40`, `59-75` | 四角色、权限表、db_role 映射 | 已实现 |
| `bigfish/executor_client.py` | `24-39`, `46-77`, `80-117`, `120-148` | 幂等键、重试语义、方法签名、组合流程 | 已实现 |
| `bigfish/db.py` | `30-150` | 十一张表：`meta/tasks/task_versions/reviews/approvals/executors/tickets/runs/artifacts/events/idempotency` | 已实现 |
| `bigfish/runtime.py` | `11-32` | 装配、密钥一致性自愈 | 已实现 |
| `bigfish/__init__.py` | `6` | `__version__ = "0.1.1-rc3"` | 已实现 |
| `bigfish/__main__.py` | `1-60` | 三入口共用闸门、单连接、退出码 2/3 | 已实现 |
| `tools/selfcheck.py` | `19`, `80`, `95`, `105` | 3099 冲突、健康检查断言、无审批工具断言、列表不含 prompt | 已实现 |
| `tools/bfc-proxy.mjs` | `13-16`, `43-44` | 3098 跳板、目标 3096 | 已实现 |
| `build.ps1` | `28-42`, `50` | onedir 参数、产物路径 | 已实现 |
| `tests/test_v011_p3.py` | `41-46` | `PORT_ROLES` 三元组断言（不能破坏） | 已实现 |
| `docs/NETWORK-TOPOLOGY.md` | `7-25`, `40-58` | 端口真源、3098 存在原因、跳板暴露面 | 已实现 |
| `/home/dsh/wmcp-proxy.mjs` | `8-11` | 18765 → 100.64.0.11:8765 | 已实现（仓库外） |
| `/home/dsh/wmcp-proxy-home.mjs` | `8-11` | 18766 → 100.64.0.12:8765 | 已实现（仓库外） |
| 端口 3099 / 3100 / 3101 | — | 本文 2.2，无代码 | **未实现，仅预留** |
| `PORT_RESERVED` 字典 | — | 本文 2.4 | **未实现，待阶段 0** |
| `ClientTransport` 协议 | — | 本文 3.1 | **未实现，设计** |
| 第 4 章 8 个移动端端点 | — | 本文 4.9 状态列 | **未实现，草案** |
| 配对码鉴权 | — | 现有只有 `register_executor()` 的一次性 token | **未实现** |
| SSE 事件流 | — | 现有 `Council.events()` 只做一次性查询 | **未实现** |
| apk | — | 无 `mobile-android/` 目录 | **未实现** |
