# AUDIT.md — 给 GPT / Gemini 的审计入口

> 版本：`0.1.1-rc9`　最后更新：2026-11-26
> 这份文件是给你（外部审计方）的入口。它告诉你项目是什么、现在到哪一步、
> 哪些**已经实测跑过**、哪些**明确没做**、以及作者自己最怀疑哪些地方。
> 请把「已验证」当成「有命令可复现」，把「未验证」当成「作者也没底」。

---

## 1. 这是什么

一个**单机 Windows 本地控制中心**，用来约束 AI 参与开发的流程：

```
  ┌────────────┐   ① 建任务 / 写 Prompt   ┌──────────────────┐
  │  人类所有者 │ ───────────────────────▶ │  BigFish Council │
  └────────────┘                          │  (Windows 本机)  │
        ▲                                 │  SQLite + GUI    │
        │ ④ 人工审批（唯一出口）           │  本地 127.0.0.1  │
        │                                 └────────┬─────────┘
  ┌─────┴──────┐   ② 登记审查意见/投票              │ ③ 派工（执行票）
  │ GPT / Gemini│ ─────────────────────────────────▶│
  └────────────┘                                    ▼
                                          ┌──────────────────┐
                                          │  DSH 执行器(远端) │
                                          │  只能领已批准的票 │
                                          └──────────────────┘
```

核心不变式（任何改动都不能破）：

| # | 不变式 | 代码位置 |
|---|---|---|
| I1 | 双模型（GPT + Gemini）对**同一版本**都投 approve，才允许人类审批 | `bigfish/core.py` `_vote_summary_locked()` |
| I2 | 人类审批是唯一能产生执行授权（ticket）的出口 | `bigfish/core.py` `approve()` / `claim()` |
| I3 | 执行器只能领 `APPROVED` 状态的任务，且同一任务只有一张活票 | `bigfish/state.py` `CLAIMABLE`、`bigfish/core.py` `claim()` |
| I4 | 执行器**无权**审批、无权改状态机、无权看别人的任务 | `bigfish/roles.py`、`bigfish/server.py` `TOOL_PERMISSIONS` |
| I5 | 回传只到 `SUBMITTED`，必须人类验收才 `DONE` | `bigfish/core.py` `submit_result()` / `verify()` |
| I6 | 库文件被替换（恢复）后，恢复前的**一切远端授权**一律失效（票、APPROVED 派工许可、执行器身份） | `bigfish/backup.py` `_prepare_staging()` + `_block_inflight()` + 密钥轮换 |
| I7 | 恢复期间不许有半份写入被当成成功：写事务要么在替换前结束，要么被明确拒绝（503） | `bigfish/db.py` `enter_maintenance()` / `_guard()`、`bigfish/server.py` 的 `MaintenanceError` 处理器 |

---

## 2. 当前版本与状态

| 项 | 值 |
|---|---|
| 版本 | `0.1.1-rc9`（`bigfish/__init__.py:6`） |
| 定性 | **发布候选（RC），不是生产版** |
| 数据库 schema | v4（`bigfish/db.py:24 SCHEMA_VERSION = 4`、`:25 BACKUP_FORMAT_VERSION = 4`） |
| rc8 → rc9 的 schema 变化 | **无**：`SCHEMA_VERSION` 仍 4、`BACKUP_FORMAT_VERSION` 仍 4，没有新 `MIGRATIONS` 项；rc8 的库直接能用，不需要迁移也不需要回滚 |
| rc7 → rc8 的 schema 变化 | **无**：`SCHEMA_VERSION` 仍 4、`BACKUP_FORMAT_VERSION` 仍 4，没有新 `MIGRATIONS` 项；rc7 的库直接能用，不需要迁移也不需要回滚 |
| rc6 → rc7 的 schema 变化 | **无**：`SCHEMA_VERSION` 仍 4，没有新增 `MIGRATIONS` 项，rc6 的库直接能用，不需要迁移也不需要回滚 |
| rc5 → rc6 的 schema 变化 | **无**：`SCHEMA_VERSION` 仍 4、`BACKUP_FORMAT_VERSION` 仍 4，没有新 `MIGRATIONS` 项；rc5 的库直接能用，不需要迁移，也不需要回滚（rc4 → rc5 那次加的 `executors.disabled_reason` 列 + `idx_executors_enabled` 索引、以及 BACKUP_FORMAT_VERSION 3 → 4 都已是历史） |
| 测试总数 | **345** 项 |
| 平台 A | Debian 12 / Python 3.11.2 / SQLite 3.40.1 → **345 项全绿**（`Ran 345 tests in 175.583s`，`OK (skipped=1)`，退出码 0） |
| 平台 B | Windows（GitHub Actions `windows-latest`，Python 3.11 与 3.13 各跑一遍）：**rc8 期已实测 322 项两遍全绿**（py3.11 `Ran 322 tests in 183.951s`、py3.13 `Ran 322 tests in 197.687s`，均 `OK (skipped=1)`）；**rc9 的 345 项待 CI 回填**——rc9 主要改 MCP 传输层（换官方 SDK），风险集中在协议层，必须有 Windows runner 的结果才算数，本文件写作时尚未推送。CI 同时跑 `tools/selfcheck.py`（16/16）与 `tools/gui_smoke.py`（9/9）。**rc8 那次 CI 第一次推翻「Windows 跑不了全量」这个错误口径**（第一次 CI run `45170828326` 的日志写着 `Ran 318 tests in 181.158s`），并撞出 R8-02 / R8-03，见第 4.4 节与第 7 节 Q11 |
| 主语言 | Python 3（标准库 + FastAPI + PySide6） |
| 许可证 | MIT |

### 测试分组

| 文件 | 项数 | 覆盖 |
|---|---|---|
| `tests/test_core.py` | 15 | v0.1.0 原始核心（审查门、审批、执行、状态机） |
| `tests/test_api.py` | 6 | HTTP API + MCP 基本语义 |
| `tests/test_v011_p0.py` | 16 | WAL 备份一致性、审批版本漂移、执行票明文、端口拓扑 |
| `tests/test_v011_p1.py` | 24 | Watchdog、票续期、心跳幂等、MCP isError、后台进程 |
| `tests/test_v011_p2.py` | 24 | 四角色权限、审查身份绑定、执行器边界、审计日志 |
| `tests/test_v011_p3.py` | 20 | 端口回环校验、后台进程、GUI 冒烟 |
| `tests/test_v011_rc3.py` | 26 | 状态机取消语义、真并发、恢复、安全边界 |
| `tests/test_v011_rc4.py` | 33 | 幂等契约、MCP 协议、多轮执行票、审查绑定、执行器客户端、辅助 |
| `tests/test_v011_rc5_restore_authority.py` | 24 | 恢复禁用全部身份、reenable 轮换凭据、冻结 APPROVED、恢复后重放拒绝、维护模式（含 rc6 加的 guard/锁原子性 2 项）、维护期 HTTP 语义 |
| `tests/test_v011_rc5_receipts.py` | 24 | `ReceiptLog` 读写与容错、`state_of` 判定、落盘耐久性、`run_once` 收据链、客户端接线 |
| `tests/test_v011_rc5_review_audit.py` | 13 | 双通过事件落库、撤评论不动审批、事务内时间戳、审计包列白名单 |
| `tests/test_v011_rc5_proxy.py` | 9 | 跳板监听硬闸门（0.0.0.0 / :: / [::] / 空值 / 内网 / 公网拒绝，回环放行） |
| `tests/test_v011_rc5_key_heal.py` | 8 | 指纹首次补记不动票、密钥轮换吊销全部活票（含 rc6 加的 fail-closed 启动拒绝） |
| `tests/test_v011_rc5_cli_guard.py` | 4 | CLI 恢复探端口拒绝、`--force` 绕过、端口探测助手（含 rc6 加的「探端口早于开库」） |
| `tests/test_v011_rc5_dispatch_id.py` | 3 | claim 下发 `dispatch_id`、重放同号、重新派工换号 |
| `tests/test_v011_rc6.py` | 16 | 跨进程恢复租约 6 + 审计凭据声明诚实性 5 + 请求体上限 2 + GUI 停服确认 3 |
| `tests/test_v011_rc7.py` | 28 | Codex 审计 BFC-01~08 各组 + 手机端只读角色（租约原子性 6 / MCP 字段上限 4 / 监听闸门 5 / manifest 枚举 3 / CLI 关库与端口 2 / 发布工程 3 / 手机角色 5） |
| `tests/test_v011_rc8.py` | 27 | 第六轮硬化：Windows 同进程并发句柄 2 + 未知角色失败关闭 7 + 子进程编码独立性 2 + Windows ACL 匹配 6 + 控制台解码 6 + 控制台编码兜底 4 |
| `tests/test_v011_rc9.py` | 15 | 未知 reviewer model 失败关闭 10（脏行拒绝 / 空名空 model 拒绝 / 双关键词拒绝 / 显式 model 仍工作 / 唯一名字仍推断 / 鉴权拒绝 / 任务不受影响 / 审计落库 / 审计不含令牌）+ 注册侧同源失败关闭 5 |
| `tests/test_v011_rc9_socket.py` | 8 | 真 uvicorn + 官方 mcp SDK 客户端：协议协商 / 服务标识 / 工具集恰五件 / inputSchema 结构 / 完整闭环 / 幂等重放 / 业务失败 isError / 鉴权拒绝与越权 |

v0.1.0 的原始 21 项（`test_core.py` + `test_api.py`）**一项没删、一项没放宽断言**。
rc3 的 26 项、rc4 的 33 项、rc5 的 80 项、rc6 的 21 项都是新增，不是替换。
rc5 新增 7 个文件共 **80 项**（21 + 24 + 13 + 9 + 7 + 3 + 3），
另有 2 项加在两个既有文件里（`test_v011_p0.py` 15 → 16、`test_v011_p3.py` 19 → 20），
合计 162 → **244**。

rc8 在这 295 之上又加了 **27 项 → 322**：新增 `tests/test_v011_rc8.py` **27 项**（R7-01 并发句柄 2 / R7-02 未知角色 7 / 子进程编码 2 / Windows ACL 匹配 6 / 控制台解码 6 / 控制台编码兜底 4）；另有一条既有测试（`test_v011_rc5_review_audit.py` 的心跳续租）改了采样时机，断言强度不变。**一项没删、一项没放宽断言。**

rc9 在这 322 之上又加了 **23 项 → 345**：新增 `tests/test_v011_rc9.py` **15 项**（未知 reviewer model 失败关闭 10 + 注册侧同源失败关闭 5）与 `tests/test_v011_rc9_socket.py` **8 项**（真 uvicorn 进程 + 官方 mcp SDK 客户端走真实 socket）。**一项没删、一项没放宽断言。**唯一被改的既有断言在工具脚本 `tools/selfcheck.py` 里（「坏 Token 应返回 200」→「应返回 401」，方向变严），它不计入 345。

rc7 在这 265 之上又加了 **30 项 → 295**：新增 `tests/test_v011_rc7.py` **28 项**（八条审计意见逐条对应 + 手机端角色 5 项 + 1 项跨进程租约），
另有 2 项补进 `tests/test_v011_p2.py`（手机角色不能执行、db_role 映射不兜底成执行器）。
**一项没删、一项没放宽断言。**

rc6 在这 244 之上又加了 **21 项 → 265**：新增 `tests/test_v011_rc6.py` **16 项**
（跨进程租约 6 + 审计凭据声明诚实性 5 + 请求体上限 2 + GUI 停服确认 3），
另有 5 项补进三个 rc5 文件（`test_v011_rc5_restore_authority.py` 21 → 24、
`test_v011_rc5_key_heal.py` 7 → 8、`test_v011_rc5_cli_guard.py` 3 → 4）。
**一项没删、一项没放宽断言**，全部是在既有语义上补强。

### rc5 改了什么（一句话版）

| 批次 | 主题 | 关键点 |
|---|---|---|
| R1 | 恢复后授权作废 | `_block_inflight` 五步（在途转 BLOCKED / 冻结 APPROVED / 吊销活票 / 禁用全部身份 / 清幂等）+ `reenable_executor` 强制轮换凭据 + `_replay_claim` 全量校验 |
| R2 | 事件落库 | `_recompute_review` 删掉提前 return，双通过的两条事件不再丢 |
| R3 | 撤评论边界 | 只有 `kind=vote` 才影响审批；撤 comment 只下架文字 |
| R4 | 锁内时间戳 | heartbeat / submit 的 `now()` 移进 `tx()`，租约基准点等于写入时刻 |
| R5 | 执行收据 | `ReceiptLog` + `state_of` + `reconcile`，服务端下发 `dispatch_id`，旧服务端降级用 `ticket_id` |
| R6 | 审计白名单 | `AUDIT_COLUMNS` 逐表列白名单 + `manifest.redaction.policy="allowlist"` + `dropped_columns` 上报 |
| R7 | 跳板硬闸门 | `bfc-proxy.mjs` 的 `isLoopback()` 在 `listen` 前判定，不合法 `exit(1)` |
| R8 | 恢复期排空 | db 维护模式（写锁屏障 + `MaintenanceError` → 503/Retry-After）+ GUI `pause_services`/`resume_services` + CLI 探端口拒绝（`--force` 逃生） |
| R9 | 密钥自愈 | `_heal_key` 轮换时吊销全部活票 / 清幂等 / 写审计；仅补记指纹时不动票 |

### rc6 改了什么（一句话版）

这一版不增功能，只做一件事：**把「我们声称的每道闸门」和「代码里真的发生的那件事」对齐**。
来源是 Astra 在 `review/rc5-hardening-20261120` 分支的 `AUDIT_REVIEW.md`（F-01～F-09），
其中 F-01～F-04 由 Astra 自己修好后本版 cherry-pick 验证，F-05～F-09 本版新写。

| # | 级别 | 主题 | 关键点 |
|---|---|---|---|
| F-01 | P1 | 维护模式 guard 与锁不是原子（Astra） | `db.query/one/execute` 的 `_guard()` 挪进 `self._lock` 内；`Database._Tx.__enter__` 改成先 `acquire()` 再 `_guard()`，任何一步失败先 `release()` 再抛；两项真线程 + `Event` 测试卡这个窗口 |
| F-02 | P1 | `restore_epoch` 被 `except: 0` 吞掉（Astra） | 恢复流程先 `enter_maintenance()` 再读 epoch，而 `restore_epoch()` 走普通访问会被自己的 `MaintenanceError` 打中 → 世代单调递增静默失效；改后从 `_allow_during_maintenance()` 里读，测试要求活库先抬到 7、恢复旧快照后必须得 8 |
| F-03 | P1 | 密钥自愈失败仍然启动（Astra） | `runtime.py` 的 `except Exception: pass` 改成 `db.close()` + 抛 `CouncilError("执行票密钥一致性校验失败，拒绝启动")`；测试断言 `load_or_create_key` 未被调用 |
| F-04 | P2 | CLI `restore` 端口预检晚于开库（Astra） | `main()` 里 `build()`（会开库）挪到后面；`restore` 分支先 `Settings.load` + `_port_busy` 预检，通过后才 `open_council()`；测试断言端口占用时库没被打开 |
| F-05 | P1/P2 | GUI 停服失败被当成成功 | `ApiServer.stop()` 检查 `th.is_alive()`，还活着就抛 `TimeoutError`；新增 `is_running()`；`pause_services()` 改成**先停成功再置** `_services_paused`，失败时尝试把 API/watchdog 接回来再抛 `RuntimeError`；3 项单测（含自造永不退出线程） |
| F-06 | P1 | 跨进程恢复互斥只是尽力而为 | 新建 `bigfish/locks.py`（157 行）：`WriterLease` + `exclusive_access()`；POSIX 在 `<db>.access.lock` 上写者持 `LOCK_SH`、恢复抢 `LOCK_EX\|LOCK_NB`；Windows 无共享锁，改为每写者一把 `<db>.access.<pid>.lock` 逐个探活；恢复自身是写者，抢排他前必须 `pause_lease()`、`finally` 无条件 `resume_lease()`；6 项测试 |
| F-07 | P1/P2 | 审计包「无明文凭据」声明过强 | `security._PATTERNS` 加 kind 字段并新增 5 类（Google API key / Slack token / Stripe live key / `password=…` / `scheme://user:pass@`）；新增 `SCRUB_KINDS` 与 `scan_secrets()`；manifest 在原有布尔之外新增 `credential_scan` 结构（`mode: pattern-based` + `patterns` + `fields_scanned` + `coverage` + `hits` + `manual_review_required: true`），`handling` 改成「可外发（需人工复核）……脱敏不等于证明」；5 项测试 |
| F-08 | P2 | `create_app` 不负责 bind | 补长 docstring 写明「本函数不 bind socket，部署必须走 `serve()`，`uvicorn.run(create_app(...), host="0.0.0.0")` 不被支持」；选择写清边界而不是假装封死，因为 `TestClient` 依赖它不绑定 |
| F-09 | P2 | 自由文本请求没有体积上限 | `server.py` 加 `MAX_BODY_BYTES`（默认 8 MiB，`BFC_MAX_BODY_BYTES` 可覆盖）与 `MAX_TEXT_FIELD`（默认 4 MiB）；纯 ASGI 的 `BodyLimitMiddleware` 先看 content-length、没有就包 `receive()` 边收边数，超限回 413；字段级 `max_length` 超限回 422；2 项测试 |

### rc9 改了什么（一句话版）

起点是 Codex 在仓库里提交的 `docs/AUDIT-CODEX-v0.1.1-rc8.md`（六条意见）。逐条复核后**四条属实、一条口径问题、一条属实但不改**。
这一版**把 `/mcp` 的传输层从手写 JSON-RPC 换成官方 `mcp` SDK**（审计第一条：`requirements.txt` 声明了依赖却全仓零 `import mcp`），
并顺手把两条「失败开放」的路径关掉。

| # | 级别 | 主题 | 关键点 |
|---|---|---|---|
| AUDIT-RC8-01 | 高 | MCP 重复造轮子 | 新增 `bigfish/mcp_adapter.py`（532 行）承载传输层，用官方 `StreamableHTTPSessionManager`；`tool_specs()` / `dispatch_tool()` 仍是唯一业务入口，鉴权、维护期 503、字段上限、审计全部保留。`server.py` 只留 `Route("/mcp", ...)` 两行挂载 |
| AUDIT-RC8-02 | 高 | 未知 reviewer model 失败开放 | `role_for_db_row()` 认不出 model 时默认 `gemini`，等于「写错一列 = 拿到审查权限」。改成失败关闭：`DENIED_ROLE` 哨兵，只有能唯一判定 gpt/gemini 才给角色 |
| R9-01 | 高 | 注册入口与解析入口判定不同源 | `register_executor()` 用的是另一套推断（默认 gemini），与 `role_for_db_row()` 不一致。改成同一套 `has_gpt`/`has_gem` 判定，两可时**拒绝注册**并要求显式 `model=` |
| AUDIT-RC8-06 | 高 | MCP 兼容性没有真实客户端覆盖 | 新增 `tests/test_v011_rc9_socket.py`：进程内起真 uvicorn，用**官方 SDK 客户端**跑完整闭环。这条测试立刻暴露了 R9-02 |
| R9-02 | 高 | MCP 鉴权失败返回 200 + error 让客户端挂死 | 官方客户端收到 `200 + {jsonrpc, id: null, error}` 时 pydantic 校验失败、**挂住 3 分钟不退出**。改成 HTTP 401（MCP 规范要求）。这是「真实客户端测试」才照得出来的 |
| R9-03 | 中 | 会话管理器每进程只能 `run()` 一次 | `StreamableHTTPSessionManager.run()` 二次调用抛 `RuntimeError`。改成每请求新建（`stateless_http=True` 本就无状态），因此也不需要 app lifespan |
| R9-04 | 中 | 维护期恢复时 MCP 仍可能排队 | 恢复期间 DB 处于维护模式，新请求应立刻 503 而不是等锁超时。在 `McpAsgi` 入口前置判 `maintenance_reason` |
| AUDIT-RC8-05 | 低 | 发布文档测试数量矛盾（318 / 322 / 295 混用） | 全部统一到 345；README 测试表补上 rc8/rc9 三行 |
| AUDIT-RC8-04 | 低 | 移动端口径 | 事实核对：`docs/ARCHITECTURE-FUTURE.md:889` 已写「apk — 未实现」，README/CHANGELOG 无虚假声明。**不改代码**，只在 README 补一句口径 |
| AUDIT-RC8-03 | — | rc8 tag 落在无父提交上 | 历史提交无法补救，**本轮不打补丁**；rc9 起改在真实线性历史上打 tag，见第 11 节 |
### rc8 改了什么（一句话版）

起点是 GPT 复核 rc7 后给出的 5 条未闭合项（其中 R7-01/R7-02 是 P1），加上第一次 GitHub Actions 跑出来的两个平台专属缺陷。
**逐条复核全部成立，逐条修**；这一版没加功能。

| # | 级别 | 主题 | 关键点 |
|---|---|---|---|
| R7-01 | 高 | Windows 同进程句柄竞态 | `locks.py` 的 `_acquire_windows()` 里「查 `_LOCAL` 表」在锁内，「抢槽位」在锁外，最后再进锁写表。两线程同时看到空表 → 各开句柄、各抢槽位 → 后者覆盖前者；先完成者 release 时 `held[0] is not fh` 直接 return，句柄与槽位双泄漏。`SLOTS = 64`，攒够 64 次所有新 `Database()` 都抛 `LeaseError`。改法：整段压进同一把 `_ACQUIRE_LOCK`，`except BaseException` 里关自己的句柄，`release()` 用同一把锁 |
| R7-02 | 高 | 未知角色回落成执行器 | `role_for_db_row()` 末尾 `return EXECUTOR_DSH`，配 `server.py` 两处 `or R.EXECUTOR_DSH`，等于「手工改一行 SQL，或旧版本留下的未登记 role = 拿到 task.claim / run.submit」。改法：新增零权限哨兵 `DENIED_ROLE`，认不出就映射到它；`authenticate()` / `authenticate_any()` 直接抛错并写 `auth.denied_unknown_role` 审计（只记身份 id，不记令牌） |
| R8-01 | 高 | Windows ACL 校验静默失效 | `icacls` 输出按代码页编码，`subprocess.run(text=True)` 解不动就抛/退化成空串。改法：新增 `run_capture()` 先拿 bytes，再按 utf-8 → gbk → cp936 → mbcs → utf-16-le 兜底解码；所有外部命令调用统一走它 |
| R8-02 | 高 | cp1252 控制台整进程退出 | **CI 实测出来的**：英文版 Windows / runner 的 `sys.stdout` 是 cp1252，`print("健康检查")` 抛 `UnicodeEncodeError`，`tools/selfcheck.py` 在第一行输出就 traceback 退出。改法：新增 `bigfish/console.py`，**只在当前编码表示不了中文时**才 `reconfigure(encoding="utf-8", errors="replace")`，cp936/UTF-8 一律不动，`BFC_NO_STDIO_FIX=1` 可关；在包导入点安装，覆盖所有入口 |
| R8-03 | 低 | 心跳续租测试的时钟假设 | **CI 实测出来的**：Windows `time.time()` 分辨率约 15.6ms，claim 与 heartbeat 落在同一 tick 时 `renewed_at` 不变，测试误判「续租没前进」。改法：在采样与心跳之间显式跨过一个 tick；断言仍是 `assertGreater`，**没有降级成 `assertGreaterEqual`** |
| CI-01/02 | 低 | 子进程编码与启动窗口写死 | 两个「拒绝公网监听」测试的子进程加 `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1`、父进程按 utf-8 解码；`test_v011_rc5_proxy` 的 `STARTUP_TIMEOUT` 3.0 → 25.0 秒 |
| MINOR-01 | 低 | CLI 缺 `mobile` 角色选项 | `executor-add --role` 的 choices 补 `mobile` |
### rc7 改了什么（一句话版）

来源是 Codex 在 `main` 上提交的 `docs/AUDIT-CODEX-v0.1.1-rc6.md`（commit `f5e955930c`，八条意见，
编号 BFC-01～BFC-08）。**逐条复核后全部确认成立**，逐条修。这一版没有加功能，全部是工程缺口收口。

| # | 级别 | 主题 | 关键点 |
|---|---|---|---|
| BFC-01 | 高 | Windows 恢复 TOCTOU | `locks.py` **整文件重写**：旧的「每 pid 一个租约文件 + 恢复前扫描」两把锁不是同一内核对象，扫描返回后新 `Database` 仍能在新文件上建锁。改成一个对象：POSIX 写者 `flock(LOCK_SH)` / 恢复 `flock(LOCK_EX)`；Windows 64 槽位（`msvcrt.locking(LK_NBLCK,1)` 占 1 字节，恢复一次锁 `[0,64)`），同进程多实例走 `_LOCAL` 共享句柄 + 引用计数。**没有检查→上锁的窗口** |
| BFC-02 | 高 | MCP 无字段级 maxLength | `_validate_args()` 校验 `maxLength`；`tool_specs()` 给 `heartbeat.detail` 与 `submit_result` 的 `log/diff/tests/note` 补 `MAX_TEXT_FIELD`（4 MiB，`BFC_MAX_TEXT_FIELD`）。超限走 `isError: true` 且不留半条 artifact |
| BFC-03 | 高 | `create_app()` 可绕过监听闸门 | 新增 `create_production_app()`（启动前过 `check_bind_host`，serve/GUI/CLI 全切过去）+ `LoopbackGuardMiddleware`（**每根连接**判 `scope["client"]` 与 `scope["server"]`，任一非回环即 403）。硬绑 0.0.0.0 也拿不到数据 |
| BFC-04 | 中 | manifest 布尔声明过强 | 去掉 `contains_plaintext_credentials: false` 这类承诺，改枚举 `credential_status`：`none` / `hashes-only` / `pattern-scrubbed-unverified` |
| BFC-05 | 中 | CLI 非 restore 分支不关库 | `main()` 统一 `try: return _dispatch(...) finally: _close_quietly(db)`；退出后写者租约立刻释放 |
| BFC-06 | 中 | allowed-ports 不打印预留端口 | 输出改 `implemented` + `reserved` 两块，3099/3100/3101/8765/18765/18766 全列出 |
| BFC-07 | 中 | 发布工程不闭环 | 新增 `requirements.lock`（39 个精确版本）与 `.github/workflows/ci.yml`（ubuntu+windows × Py3.11/3.13，pyflakes/compileall/unittest/selfcheck/gui_smoke + `security-note` 密钥与二进制扫描） |
| BFC-08 | 中 | tag 指向无父提交的根提交 | 已核实：远端 `v0.1.1-rc6` → `a2e9275751`，`parents=0`，与 `main` 历史不连通。本版起在真实提交上打带注释 tag，不再用导出重写造根提交 |
| MOBILE | — | 手机端只读角色（为 apk 预留） | `roles.MOBILE_VIEWER`，权限只有 `task.read`/`event.read`/`review.create`；`_DB_ROLE_MAP` 加 `mobile`，token 前缀 `bfc_mb_`。**`role_for_db_row()` 对未知 db_role 会兜底成执行器**，不显式登记就会让手机拿到执行器权限——已登记并有测试钉住 |

---

## 3. 目录结构与从哪读起

```
bigfish/
  core.py            领域核心：Council 类，所有状态迁移唯一入口
  db.py              SQLite 连接、schema、迁移、快照、审计表
  backup.py          备份导出 / 校验 / 原子恢复 / 脱敏审计包
  locks.py           跨进程恢复租约（rc6 新增：POSIX fcntl 共享/排他，Windows 每写者一把 pid 锁）
  security.py        token 派生、HMAC、文件权限、脱敏
  state.py           状态常量与状态集合（这是状态机的「法律条文」）
  roles.py           四角色与权限位
  server.py          FastAPI + 手写 MCP JSON-RPC（rc6 加请求体上限中间件）
  gui.py             PySide6 五个页面
  cli.py             21 个子命令（rc5 新增 reenable；rc6 把 restore 改成先探端口再开库）
  executor_client.py 纯标准库执行器客户端（含安全重试）
  config.py          端口唯一真源、设置、端口预留表
  runtime.py         打开 Council 的统一入口（rc6 起密钥校验失败直接拒绝启动）
tests/               16 个文件，见上表
tools/               自检、网络检查、远程 MCP 闭环、GUI 冒烟、启停脚本
docs/                交付、测试、安全、网络、迁移、回滚、架构预留（rc6 尚无专档，见 CHANGELOG）
```

### 建议阅读顺序（按审计目的挑）

**A. 只想快速判断「审批有没有可能被绕过」**

1. `bigfish/state.py` — 看 `CLAIMABLE = {APPROVED}`、`REVIEW_GATED`、`CANCEL_NEEDS_ACK` 这几个集合
2. `bigfish/server.py` — 搜 `TOOL_PERMISSIONS`，确认只有 5 个工具且都不含 approve
3. `bigfish/core.py` 搜 `def approve(` — 看它是否在 `with self.db.tx()` 内、是否带 expected_version、是否条件更新
4. `tests/test_v011_rc3.py` 的 `TestStateMachine` — 看取消/撤票/送审的实测断言

**B. 关心数据安全（备份、恢复、凭据）**

1. `bigfish/db.py` 搜 `def snapshot(` — 看是不是用 `sqlite3.Connection.backup()` 而不是 `shutil.copy`
2. `bigfish/backup.py` 全文 — 看 `restore_bundle` 的 swap 顺序、`swapped` 标志、失败回滚成对性
3. `bigfish/locks.py` 全文（rc7 重写，约 300 行）— 看写者租约与恢复的排他租约怎么互斥；重点看「没有检查→上锁窗口」这一点
4. `bigfish/security.py` 全文（只有几 KB）— 看 `_PATTERNS` 的 11 类模式与 `scan_secrets()`
5. `tests/test_v011_rc3.py` 的 `TestRestore` — 7 项恢复场景

**C. 关心并发与幂等**

1. `bigfish/db.py` 搜 `def tx(` — BEGIN IMMEDIATE + RLock 释放；注意 rc6 后 guard 在锁内
2. `bigfish/core.py` 搜 `idem_scope_key` — 幂等键的构造方式
3. `tests/test_v011_rc3.py` 的 `TestConcurrency` — 7 项真线程 + Barrier
4. `tests/test_v011_rc4.py` 的 `TestIdempotencyContract` — 6 项契约

---

## 4. 已跑通的验证（带证据）

### 4.1 平台 A 全量回归（本文件写作时刚跑）

```bash
# Debian 12 / Python 3.11.2 / SQLite 3.40.1
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .
# rc7 → Ran 295 tests in 60.507s / OK / exit 0
# rc8 → Ran 322 tests in 148.492s / OK (skipped=1) / exit 0
# rc9 → Ran 345 tests in 175.583s / OK (skipped=1) / exit 0
# rc6 当时 → Ran 265 tests in 41.728s / OK / exit 0
# 复核：本次改写文件时又跑了一遍，265 项同样 OK（耗时 55.093s，仅机器负载不同）
```

### 4.2 平台 B（Windows）

```powershell
# Windows 11 / Python 3.13.11 / SQLite 3.50.4 / PySide6 6.12.0
$env:QT_QPA_PLATFORM = "offscreen"   # 必须；不要设 LD_LIBRARY_PATH
.venv\Scripts\python.exe -m unittest discover -s tests -t .

# rc6 实测（修完两个 Windows 专属缺陷之后）：
#   Ran 265 tests in 59.975s / OK (skipped=1) / UT_RC=0
# rc7 实测：Ran 294 tests in 74.232s / OK (skipped=1) / UT_RC=0
# 首次跑是 FAILED (errors=2, skipped=1)，见第 5 节 U5 与第 7 节 Q9
```

### 4.4 平台 B 的证据从「一台机器」换成「CI 每次跑」

rc7 及之前，平台 B 的证据是「某台 Windows 机器上手工跑过一次」。这个口径有两个问题：**只能证明跑过的那一版**，而且机器随时可能被人占用。

rc8 起改由 `.github/workflows/ci.yml` 在 `windows-latest` 上跑，Python 3.11 与 3.13 各一遍：

```
run 45174755219（head fdd5c12，全部 5 个 job success）
  windows-latest / py3.11 → Ran 322 tests in 183.951s / OK (skipped=1)
  windows-latest / py3.13 → Ran 322 tests in 197.687s / OK (skipped=1)
  两个 job 都随后跑了 tools/selfcheck.py（通过 16 项，失败 0 项）
  与 tools/gui_smoke.py（通过 9 项，失败 0 项）
```

第一次 CI（run `45170828326`，head fa37e2e）是 `Ran 318 tests in 181.158s` 加两处失败——
它推翻了一个从 rc6 一直背到 rc7 的错误口径：**「Windows 跑不了全量」是假的，318 项全跑完了，只是挂 1 项**。
那一项（时钟粒度）与另一个 job 自检崩在 cp1252 上的问题，就是 R8-02 / R8-03。详见第 7 节 Q11。
### 4.3 其它工具

| 工具 | 结果 | 说明 |
|---|---|---|
| `tools/selfcheck.py` | 16/16 | 端到端自检：建任务→双审→批准→claim→心跳→回传→验收 |
| `tools/gui_smoke.py` | 9/9 | 离屏 GUI：主窗口构造、五页存在、五页可切换、状态栏、待审批识别 |
| `tools/e2e_loopback.py` | 22/22 | **同机回环**演练（明确声明不是跨设备） |
| `tools/net_check.py` | 分层 | 第 1–3 层本地；第 4 层跨机需真实对端 |
| `tools/remote_check.py` | 默认只读 | 真实 MCP JSON-RPC；必须显式 `--task-id` 才碰数据 |
| `pyflakes bigfish tools tests` | 无输出 | 两平台都通过（平台 B 装的是 pyflakes 4.1.0） |
| `compileall bigfish tools tests` | exit 0 | 两平台都可编 |

### 4.4 跨设备链路

VPS → Windows 客户端 `100.64.0.11:3096`（Tailscale Serve，仅 tailnet，无 Funnel）的 MCP 闭环 11/11
曾在 **rc2** 打通。**rc3 / rc4 / rc5 / rc6 都未重跑跨机闭环**，这四版只证了本地监听校验与协议语义。
请不要把同机回环（`tools/e2e_loopback.py` 的 22/22）当成跨设备证据。

---

## 5. 明确未完成（不掩盖）

| # | 项 | 现状 | 影响 |
|---|---|---|---|
| U1 | GUI 真窗口人工验收 | 只做了离屏 smoke（9/9，覆盖构造窗口与切页） | 真实渲染、托盘行为、高 DPI 未人眼确认。rc5 的 `pause_services`/`resume_services` 恢复路径在 rc6 补了 3 项单测（停服超时抛错 / 停服成功置标志 / 失败不置标志），但**真窗口一次没点过**——rc6 的 `pause_services` 停服超时路径只有单测，没有端到端证据 |
| U2 | 开机自启 | 未注册；`tools/start-headless.ps1` 真机未跑通（`schtasks /TR` 嵌套引号报 `-2147467259`） | WMI 起的进程重启即消失 |
| U3 | DSH MCP 连接导入 | 未导入（需人类授权） | 现在 DSH 尚未接上这个控制台 |
| U4 | rc3 / rc4 / rc5 / rc6 跨机闭环 | 未重跑 | 跨机证据仍是 rc2 时期的 |
| U5 | 平台 B 的完整回归 | **rc8 期已由 CI 承担**：`windows-latest` 上 Python 3.11 与 3.13 各跑一遍 322 项，两遍都是 `OK (skipped=1)`（183.951s / 197.687s），见第 4.4 节。**rc9 的 345 项尚未推 CI**，写作时只有平台 A 的结果 | 覆盖的是 GitHub 托管 runner 的 Windows Server 镜像，不是用户那台装了 PySide6 6.12.0 的 Windows 11；GUI 冒烟在 runner 上通过，真窗口仍未人眼确认（U1） |
| U9 | 平台 B 复跑新增用例 | **rc8 已做**（322 项两版本都跑完，含 rc7 的 28 项与 rc8 的 27 项）；**rc9 未做**——新增的 23 项（`test_v011_rc9.py` 15 + `test_v011_rc9_socket.py` 8）还没在 Windows 上跑过。socket 那 8 项起的是真 uvicorn 进程，Windows 上要盯端口释放与子进程编码 | rc9 的协议层修复在 Windows 上是否同样成立，目前无证据 |
| U6 | 平台 B 静态检查 | **已补齐**：`pyflakes bigfish tools tests` 零输出（退出码 0，pyflakes 4.1.0）、`compileall -q` 退出码 0 | Windows 侧的名字类检查与平台 A 同口径 |
| U7 | 类型检查（mypy / pyright） | 未接 | 只有 `compileall` 与 `pyflakes` 两道 |
| U8 | 长时运行（soak） | 未做 24h 连续跑 | 长期泄漏、票过期满 6 小时后行为未验 |
| U9 | 恢复期维护模式的跨进程行为 | 单进程内已测（`MaintenanceError` / 503 / `/health`）；跨进程 rc6 起有 `WriterLease` 租约（POSIX 6 项单测已过），CLI 端口探测仍在 | 端口探测与真正替换之间的 TOCTOU 窗口被租约收窄了，但 `--force` 仍跳过端口探测，见第 6 节 X1、U11 |
| U10 | `ReceiptLog` 的对账闭环 | 客户端侧 24 项已测（含跨进程读）；`reconcile()` 只输出「已开工未完成」的 `dispatch_id` 清单 | 清单如何回传、由谁裁决，仍是人工动作，未做自动化对账 |
| U11 | Windows `msvcrt` 恢复锁分支 | **已真机验证，并修出缺陷**：首次跑就撞上 `LeaseError: … [Errno 13] Permission denied`——`msvcrt.locking(LK_NBLCK)` 的字节区间锁**按文件句柄**记，租约文件按 pid 命名，同进程第二个 `Database` 用第二个句柄锁同一区间必失败，被误判成「有别人在用」。修法：模块级 `_win_leases`（路径 -> [句柄, 引用计数]），同进程复用句柄、计数归零才解锁 | **仍未验**：「两个真正独立的进程开着库时恢复该被拒」这一条在 Windows 上仍无实测（测试里的「另一个进程」是同进程再开一个 `Database`，对 `msvcrt` 按 pid 分文件的逻辑不等价）；见第 6 节 R3、X3 |

---

## 6. 已知残余风险（作者自述）

| ID | 风险 | 缓解 | 是否接受 |
|---|---|---|---|
| R1 | `approve` 的 actor 由调用方传入，无生物特征证明 | 本机拿到 CLI/GUI 的人可代批 | 接受（单机本地工具，威胁模型不含本机 root） |
| R2 | Windows ACL | 已实测 `secure_file` → `{'ok': True, 'mode': '0o666', 'acl': 'granted'}`、`verify_file_restricted` → `(True, 'acl limited')` | 已验证（Windows 11） |
| R3 | 恢复时若另一个进程开着 db，`os.replace` 可能失败 | 抛错 + 成对回滚，不损坏原库；rc5 起 CLI 先探端口拒绝、GUI 先停服进维护模式；**rc6 起有跨进程租约 `bigfish/locks.py`，普通写者持共享租约、恢复抢排他租约，抢不到就抛 `LeaseError` → `RestoreError`——POSIX 与 Windows `msvcrt` 均已真机跑通**（Windows 侧还修出缺陷 17，见 U11） | 接受（双平台已堵；真·双进程 Windows 场景仍未实测；`--force` 只跳过端口探测，跳不过租约） |
| R4 | Tailnet 内任何持执行器 token 的设备可操作控制台 | 只能操作被分配给自己的任务 | 接受（设计意图） |
| R5 | `heartbeat_ttl` 默认 120s，网络抖动超时会误阻断 | 进 `BLOCKED` 需人工，不会自动继续 | 接受（fail-safe 方向正确） |
| R6 | 恢复期间 GUI 未暂停看门狗 | **rc5 已修**：GUI 改走 `pause_services()`/`resume_services()`，恢复期 db 处于维护模式，普通读写直接 `MaintenanceError` | 维护模式有 6 项单测；GUI 停服确认在 **rc6 补了 3 项单测**（`stop` 超时抛 `TimeoutError`、成功才置 `_services_paused`、失败时标志仍为 False），但**真窗口仍未点过**（U1） |
| R7 | DB 文件本身未加密（无 SQLCipher） | 拿到磁盘即可读明文数据 | 接受（本地单机，且凭据不进库） |
| R8 | 审计导出包用**黑名单**列 | **rc5 已改为列白名单** `AUDIT_COLUMNS` + `manifest.redaction.policy="allowlist"`；漏列只会少导数据，且 `dropped_columns` 会如实上报 | 已缓解（见 Q7）。**rc6 又补了 `credential_scan` + `manual_review_required: true`**：manifest 里在布尔声明之外多了一段结构化说明，写明扫描模式、查了哪些模式、覆盖范围与盲区，`handling` 口径改成「可外发（需人工复核）……脱敏不等于证明」；但仍是 pattern-based，不是 DLP（见 X5） |

rc5 新增的四条残余风险（作者主动登记）：

| ID | 风险 | 缓解 | 是否接受 |
|---|---|---|---|
| X1 | `cli restore --force` 是**人工逃生门**：它绕过端口探测直接替换库文件 | 默认关闭，只在明知会数据分叉时手动加；不加时 CLI 一定拒绝、GUI 一定先停服。rc6 之后即使加了 `--force`，仍要过 `locks.py` 的排他租约（别人活着就抛 `LeaseError`） | 接受（逃生门的价值就在于能被用；误用的后果是可回滚的） |
| X2 | executor 回传的自由文本只做**正则脱敏** | `scrub()` 覆盖 `bfc_*` / `Bearer` / `sk-*` / `gh*_*` / `AKIA*` / PEM，rc6 又补了 Google API key / Slack token / Stripe live key / `password=…` / `scheme://user:pass@` 五类；但把 token 拆成两半拼接这类变形拦不住 | 接受（残余，见 Q2）。事件表是掩码而非拒写 |
| X3 | 恢复锁只在**同一文件系统内**有效 | rc5 的 `_restore_lock` 在 rc6 已被删除，换成 `locks.py` 的 `WriterLease` / `exclusive_access`（锁文件也从 `council.db.restore.lock` 换成 `<db>.access.lock`）；跨文件系统、跨主机（比如库在 SMB 共享上）仍然无法互斥 | 接受（部署前提是库在本地盘；跨机恢复本就不在威胁模型内） |
| X4 | 恢复结果的 `tickets_revoked` 曾长期是**常量 1**（不是实际吊销行数），运维若拿它对账会得到错误结论 | 已改为 `cur.rowcount` 真实取值（`bigfish/backup.py:352`），并由 `tests/test_v011_rc5_restore_authority.py::test_tickets_revoked_reports_real_count` 固定语义 | 已修（rc5 收尾时一并修） |

rc6 新增的两条残余风险：

| ID | 风险 | 缓解 | 是否接受 |
|---|---|---|---|
| X5 | 审计包的凭据检测仍是 **pattern-based 扫描，不是 DLP** | `credential_scan.coverage` 如实写明只覆盖上述已知形态，换行拆分、编码变形、私有格式的密钥不保证命中；`manual_review_required: true` 强制人工过一遍；`hits` 是脱敏后逐字段回扫的真实统计。**rc7 已把这句话删掉（BFC-04）**：改枚举 `credential_status`，`pattern-scrubbed-unverified` 明确表示"模式扫过、未经证明"。布尔读起来像保证，枚举只能说清做了什么 | 接受（残余，但声明与事实已对齐；见 Q7） |
| X6 | `create_app()` 可被第三方 `uvicorn.run(create_app(...), host="0.0.0.0")` 绕过 | **rc7 已堵（BFC-03）**：新增 `LoopbackGuardMiddleware` 在**每根连接**上判 `scope["client"]` 与 `scope["server"]`，任一非回环即 403。即使硬绑 0.0.0.0，外部也拿不到数据；启动前闸门另由 `create_production_app()` 负责 | 已解决（双层，见 Q3 的 rc7 处置） |

---

## 7. 请你重点看这几处（作者自认最可能还有问题的）

下面 **10 条**是**作者自己怀疑的地方**，不是「已经确认没问题」。每条都附两轮处置标注：
rc5 那轮在标题上（【rc5 已修】/【rc5 部分解决】/【rc5 未改动】），rc6 那轮在每条末尾的「rc6 的处置」段
（【rc6 已修】/【rc6 部分解决】/【rc6 未改动】）；标「仍有疑问」的请优先挑刺。

**Q9 是 rc6 才出现的**——它是平台 B 首次真机跑时才暴露出来的两类 Windows 语义坑，不是设计时想到的问题。
列在这里的意义是：既然 Linux 上 265 项全绿也照不出这两条，那同类的坑大概率还有，请你顺着这个方向继续挑。

> 标注含义：【X 已修】= 这一条提出的问题在该版被正面解决并有单测；【X 部分解决】= 主要路径堵住了，
> 但作者仍认为有残余；【X 未改动】= 该版一行没碰，疑问仍然成立。

### Q1. `approve()` 的双重防线是不是真的双保险　【rc5 未改动】

`bigfish/core.py` 的 `approve()` 里有两道检查：

1. `expected_version is not None and version != expected_version` → 抛「任务版本已变」
2. `UPDATE tasks SET status=?, ... WHERE id=? AND current_version=? AND status=?` → `rowcount != 1` → 抛「任务在审批过程中被修改」

**仍是疑问**：第 1 道在 `expected_version=None` 时完全不生效（CLI 默认不传）。第 2 道是真防线。
请判断：只靠第 2 道，在「两个 GUI 实例同时对同一任务 approve」时，是否**一定**只有一个能成功？
（作者的论证是 `BEGIN IMMEDIATE` 让两个事务串行，第二个读到已改的 status 后 rowcount=0；
但请你自己验，别信这段文字。）

**rc6 的处置【rc6 未改动】**：`approve()` 一行没碰，上面两道检查原样成立，`expected_version=None` 的
缺口也原样成立。rc6 只改了一处**相邻**的东西（F-01）：`Database.query/one/execute` 原来先 `_guard()`
再 `with self._lock`，中间那一瞬 `enter_maintenance()` 可能插进来——审批事务以为自己进了写锁、
其实恢复已经判定「在途写已排空」。改后 guard 在锁内，事务入口也先 `acquire()` 再 guard，
所以「审批事务与维护模式互换」这个窗口没了。但这是维护模式的原子性，**不是** approve 的并发判据，
Q1 的疑问不受影响。Astra 的复核意见与本文件一致：核心更新在 `BEGIN IMMEDIATE` 事务内，
条件 UPDATE 是额外防线，`expected_version` 只是调用方的陈旧版本保护。

### Q2. `assert_no_secrets()` 覆盖够不够　【rc5 部分解决，仍偏窄】

rc4 把前缀判断换成 `security.contains_secret()`，覆盖：
`bfc_tk_` / `bfc_ex_` / `bfc_rv_` / `bfc_ad_` / `Bearer …` / `sk-…` / `gh[pousr]_…` / `AKIA…` / PEM 私钥块。

**仍有疑问**：

- 键名检查只有 `== "prompt"` 一处，其余字段全靠值匹配；
- **调用点只有一处**：`bigfish/core.py:1225`（幂等响应落库前）。
  `log_event` / `submit_result` / `heartbeat` 走的是 `scrub()` 掩码而不是拒写。
  也就是说「值里带 token」在事件表里会被掩码成 `bfc_…[REDACTED]`，而不是拒绝写入。
  请判断这种「掩码而非拒写」是否可接受，以及有没有绕过掩码正则的写法（比如把 token 拆成两半拼接）。

**rc5 的处置**：rc5 **没有**扩大 `scrub()` 的覆盖面，也**没有**增加第二个 `assert_no_secrets()`
调用点——这一条原样成立。rc5 做的是把它**如实登记**为残余风险 X2（第 6 节）：
executor 回传的自由文本只过一遍正则，`bfc_*` / `Bearer` / `sk-*` / `gh[pousr]_*` / `AKIA*` / PEM
六类模式之外的变形（拆成两半再拼、base64、换行插入）拦不住。
另外 rc5 把审计包从黑名单改成白名单（R6），这条对 Q2 有间接帮助：
即使某个字段带着没被正则命中的凭据混进库，只要它不在 `AUDIT_COLUMNS` 里就导不出去。
**仍然请你挑刺**：掩码 vs 拒写的取舍、以及 `kind=vote` 的 `content` 落库前有没有过 scrub。

**rc6 的处置【rc6 部分解决】**：rc6 扩的是**模式表**和**声明**，不是拒写机制。
`security._PATTERNS` 从 `(pattern, repl)` 改成 `(pattern, repl, kind)` 三元组，新增五类：
Google API key（`AIza[0-9A-Za-z_-]{35}`）、Slack token（`xox[abposr]-`）、
Stripe live key（`[sr]k_live_`）、通用键值（`password|passwd|pwd|secret|api_key|access_token` + `=`/`:` + 值）、
URI userinfo（`scheme://user:pass@`）；原有的 `bfc-token` / `bearer` / `openai-key` / `github-token` /
`private-key` / `aws-key` 也各自补了 kind 名。新增 `SCRUB_KINDS` 与 `scan_secrets(text) -> list[str]`
（返回命中的种类名，去重排序），审计包的 `credential_scan.hits` 就是用它**在脱敏之后逐字段回扫**
统计出来的——预期为空，但统计口径是真实的，不是拍脑袋写 0。
**但仍不是拒写**：`assert_no_secrets()` 的调用点还是只有 `core.py:1225` 一处，
`log_event` / `submit_result` / `heartbeat` 依旧走掩码。所以这条只算部分解决：
「宣称的覆盖面」和「实际的覆盖面」对齐了，「掩码 vs 拒写」这个取舍一分没动。
**仍然请你挑刺**：把 token 拆成两半拼接、base64 后再写，这五类新模式照样拦不住。

### Q3. `check_bind_host()` 的覆盖面　【rc7 已修：两层防线】

`bigfish/server.py:41`：先剥 `[ ]`，再走 `is_loopback_host()`，后者**用 `ipaddress` 真解析**，
不用「以 127. 开头」这种字符串前缀判断。因此 `::`、`[::]`、`::0`、`0:0:0:0:0:0:0:0`、`0.0.0.0`
全部落到 `is_loopback == False` 并抛 `BindError`。

调用点三处：`__main__.py`、`cli.py serve`、`gui.py run()`；
并且 `serve()` 自己（`bigfish/server.py:454`）**再校验一次**，所以直接 `serve(council, "0.0.0.0", 3096)` 也拦得住。

**仍有疑问**：如果将来有人写 `uvicorn.run(app, host="0.0.0.0")` 绕过 `serve()`，这道闸就没了。
建议看要不要在 `create_app()` 里也留一道（作者没做，怕影响 TestClient）。

**rc5 的处置**：rc5 修的是**另一个入口**——VPS 侧的 `tools/bfc-proxy.mjs`。
它原来直接 `server.listen(PORT, LISTEN_HOST)`，一个没有鉴权的 MCP 转发完全看环境变量有没有填错。
现在 `isLoopback()` 在 `listen` 之前判定并 `exit(1)`，`0.0.0.0` / `::` / `[::]` / 空串 /
内网段 / 公网地址全拒，有 9 项单测（`tests/test_v011_rc5_proxy.py`）。
**Python 侧 `create_app()` 那道兜底仍然没有**，所以「有人绕过 `serve()` 写 `uvicorn.run(host="0.0.0.0")`」
这条疑问依旧成立，只是现在多了一个入口被堵住。

**rc6 的处置【rc6 部分解决】**：F-08 把 `create_app()` 的 docstring 写长了，明确四件事：
它只返回 app、**不 bind socket**；想对外服务必须走 `serve()`（那里有 `check_bind_host()`）；
`uvicorn.run(create_app(...), host="0.0.0.0")` 会绕过闸门、**不是官方入口、也不被支持**；
以及为什么选择「写清楚边界」而不是「假装封死」——`TestClient` 依赖 `create_app` 不绑定。
**代码层仍然没有拦**：第三方照样能自己 `uvicorn.run`。所以这条从「没人提」变成「文档写明 + 风险登记为 X6」，
性质上没变。**rc7 的处置【rc7 已修，BFC-03】**：上一轮问的「要不要拆成两个名字」，这轮做了，而且还多了一层。

1. **拆名字**：新增 `create_production_app(council, host, watchdog_interval)`，返回 `(app, 归一化host)`，
   内部先 `check_bind_host(host)`。`serve()` / `gui.ApiServer.start()` / `cli serve` 全部切过去；
   `create_app()` 保留给 `TestClient` 与进程内挂载。
2. **每根连接再判**：新增 `LoopbackGuardMiddleware`，取 `scope["client"][0]`（对端 IP）与
   `scope["server"][0]`（本端 IP）。u**任一非回环即 403**。这两个字段都由传输层填充，
   客户端伪造 Host 头改不了。所以即使有人硬写 `uvicorn.run(create_app(...), host="0.0.0.0")`，
   外部连接会在中间件这一层被挡回去，拿不到 `/health` 也拿不到任何 API。
   受信名单只有 `{"testclient", "testserver"}`——都是 TestClient 的进程内常量，不经过 socket。

**实测**（`tests/test_v011_rc7.py::TestBindGate`，5 项）：五种伪造 scope 的判定、局域网来客 403、
回环来客 200、受信名单不含任何真实地址、带闸门工厂拒掉 `0.0.0.0`/`::`/`[::]`/局域网/100.64 并放行回环。

**仍然请你挑刺**：`scope["server"]` 在 uvicorn 绑 `0.0.0.0` 且本机回环访问时是 `127.0.0.1`
（实测），所以那一侧对**本机**请求不起作用——真正拦住外部的是 `scope["client"]`。
如果将来做反向代理（Tailscale Serve 就是），`client` 会变成 `127.0.0.1`，这时中间件放行，
依赖的是「代理本身只监听 tailnet」。这个前提在 `docs/NETWORK-TOPOLOGY.md` 里，请你判断它够不够硬。

### Q4. `restore_epoch` 是否真的在快照之外　【rc4 已修】【rc5 已修（授权侧补齐）】

epoch 确实存在 `meta` 表里，会被快照带回来。rc4 的解法不是把 epoch 搬出去，而是**取最大值再加一**：

```python
# bigfish/backup.py::_prepare_staging
row = conn.execute("SELECT value FROM meta WHERE key='restore_epoch'").fetchone()
staged_epoch = int(row[0]) if row else 0
new_epoch = max(captured_epoch, staged_epoch) + 1   # captured_epoch 来自恢复前的活库
```

所以「恢复旧快照 → epoch 回退」不成立：新 epoch 一定大于恢复前活库的 epoch。
同时密钥在下一步被轮换（`secrets.token_bytes(32)`），票是 `HMAC(key, …)` 派生的，旧票必然失效。
两道独立防线，任意一道成立旧票就废。

**rc5 的处置**：rc4 只保证「票」失效，**没保证「授权」失效**——这是 R1 打的洞。
快照里的 `APPROVED` 任务在恢复后仍然是 `APPROVED`（随时可补发一张新票）、
`executors.enabled` 仍是 1（旧 token 的哈希随快照一起回来了，口令继续能过）。
rc5 在 `_prepare_staging()` 事务内加了 `_block_inflight()` 五步：在途 → `BLOCKED`、
`APPROVED` → `BLOCKED`（`blocked_reason` 写「派工许可已冻结」）、活票全置 `revoked_at`、
**全部执行器 `enabled=0` + 写 `disabled_reason`**、清 `claim/heartbeat/submit` 幂等。
**至此 Q4 的三个面都闭合了**：世代、密钥、授权。恢复后要重新派工，必须
`reenable`（换新凭据）+ 重新走审批。

**rc6 的处置【rc6 已修，含 F-02】**：上面那段「取最大值再加一」在 rc5 里**其实是坏的**——
`restore_bundle()` 先 `enter_maintenance()`，然后 `try: captured_epoch = db.restore_epoch()
except: captured_epoch = 0`。而 `restore_epoch()` 走的是普通访问路径，会被自己的 `_guard()`
打中抛 `MaintenanceError`，外层 `except` 把它吞成 0。于是 `max(0, staged_epoch) + 1` 算出来的
是「快照里的世代 + 1」，而不是「活库真实世代 + 1」——**世代单调递增这条保证静默失效**，
恢复一个很旧的快照就能让 epoch 回退。
F-02 的修法是把读取挪进 `_allow_during_maintenance()` 里：先取得跨恢复租约、再进维护直通区、再读活库世代，
不再吞异常、不再用 0 兜底。回归测试把活库先抬到 epoch 7，再恢复一个 epoch=0 的旧快照，
断言结果必须是 **8**（不是 1）。这是「三个面」里的第一面（世代）真正闭合的一刻。

### Q5. Watchdog 与 restore 的竞态　【rc5 已修】

`restore_bundle(..., before_replace=…)` 支持在关连接前回调，用来「停 API + 停看门狗」。
rc3 的测试 `tests/test_v011_rc3.py:494` 用 `before_replace=boom` 验证了异常路径。

**rc4 时的疑问**：GUI 恢复**没有传 `before_replace`**，所以 GUI 里点恢复时看门狗仍在跑。
缓解是 `Watchdog.run()` 里 catch 了所有异常，单次扫描撞上「连接已关」只会记一次 error 并继续。

**rc5 的处置（已修，两条路都补了）**：

1. **GUI 路径**：恢复前调 `win.pause_services()`（先 `disconnect` 再 `stop` API 线程与看门狗），
   并把同一个函数作为 `before_replace=` 传进 `restore_from_bundle()`（函数内部幂等，重复调无副作用）；
   `finally` 里调 `resume_services()`，**失败也接回**，否则用户界面永远连不上执行器。
2. **更强的那道闸**：恢复全程 db 处于维护模式。`Database.tx()` 整个事务期间持 `self._lock`，
   `enter_maintenance()` 先抢这把锁——**抢到即代表在途写已排空**，不是 sleep 猜；
   之后任何普通读写被 `_guard()` 直接抛 `MaintenanceError`。所以就算看门狗这一刻真在扫，
   它的写也会被稳稳拒绝，而不是撞上一个正在被 `os.replace` 的文件。
   HTTP 侧译成 503 + `Retry-After`，MCP 侧回 `-32000`，`/health` 报 `maintenance`。

仍有疑问的部分：`pause_services()` 的真窗口行为没人眼确认过（U1），
`MaintenanceError` 在看门狗内的处理是「记一次 error 继续」，恢复完是否立刻恢复心跳，请你验。

**rc6 的处置【rc6 已修，含 F-01 + F-05】**：上面第 2 条「抢到锁即代表在途写已排空」在 rc5 里
**并不完全成立**——`query/one/execute` 是先 `_guard()` 再 `with self._lock`，线程 A 可以顺利通过
guard、暂停在锁外，此时线程 B 拿到锁、开维护、放出锁，A 再拿锁进去照样写。F-01 把 guard 挪进锁内，
`_Tx.__enter__` 也改成先 `acquire()` 再 `_guard()`、失败先 `release()` 再抛，
两个新测试用真线程 + `Event` 把这个窗口卡死。
第 1 条也补了：F-05 之前 `ApiServer.stop()` `join(timeout)` 之后不看线程死没死，
`pause_services()` 又把异常吞了、还在操作**开始前**就把 `_services_paused` 置真——API 线程还活着
就开始 `os.replace`。现在 `stop()` 检查 `is_alive()` 并抛 `TimeoutError`，`pause_services()`
先停成功再置标志、失败时尝试把服务接回再抛 `RuntimeError`。三项单测（其中一项自造永不退出线程）。
**仍未做**：真窗口验收（U1）——这条的「单测通过」不等于「GUI 里点一下恢复真的不会卡住」。

### Q6. `heartbeat` 里幂等检查与续租的先后顺序　【rc4 已修】【rc5 补修时间戳取点】

rc4 的实际顺序是：

1. `_assert_executor_active(c, executor)`
2. `SELECT * FROM tickets WHERE token_hash=? AND executor_id=?`（查不到 → 「执行票无效」）
3. **`_check_ticket_row(ticket)`** ← 校验 `revoked_at` / `expires_at` / 世代 / 任务归属
4. 算 fingerprint → `_idem_get_locked(..., for_update=True)` 命中则 `return dict(cached, replayed=True)`
5. `_require_executable_task()` → run upsert → 续租 → 状态推进 → 写事件 → `_idem_put`

第 3 步在幂等命中**之前**，所以「已吊销票重放拿到成功响应」这条路已经堵死：
`tests/test_v011_rc4.py::TestMultiRoundTickets::test_old_ticket_revoked_on_new_claim` 直接断言旧票心跳抛错。

**rc5 的处置**：顺序没变，修的是**时间戳取在哪**（R4）。`heartbeat()` / `submit_result()`
原来在 `with self.db.tx():` **之前**就 `t = now()`。SQLite 单写连接下事务可能要排队，
等真正拿到写锁时 `t` 已经是过去时刻，续租算出的 `expires_at = t + ttl` 就会贴着甚至越过当前时刻——
密集心跳反而把自己续成超时。改后 `t` 移进 `tx()` 内部，基准点等于真正写入的那一刻，
新增两项断言（`test_heartbeat_lease_is_in_the_future` / `test_heartbeat_keeps_lease_alive_repeatedly`）。

**rc6 的处置【rc6 已修】**：顺序与时间戳取点都没再动——rc6 修的是这条路径**入口**上的两个隐患。
一是 F-01：`_Tx.__enter__` 原来在拿到锁之后才可能被 `_guard()` 拒绝，而它在那条路径上**没有释放锁**；
现在先 `acquire()` 再 guard，`except BaseException: release(); raise`，所以「拿到锁却因维护被拒」
不会再让那把锁留在手里（心跳赶在恢复期进来时，后续请求不会被一把丢失的锁卡死）。
二是 F-02：心跳本身不读 `restore_epoch`，但恢复期读错世代会让 `new_epoch` 变小，
而票的校验里带世代——世代一乱，心跳会开始「票无法认证」。这条在 rc6 已随 F-02 一起修掉。
新增的 `test_transaction_guard_is_atomic_with_lock` 就是钉这个入口的。

### Q7. 审计导出包用黑名单还是白名单　【rc5 已修，改成白名单】

rc4 及以前的口径：

```python
# 旧写法（rc5 已替换）
# bigfish/backup.py:52
AUDIT_DENY_COLUMNS = ("token_hash", "ticket", "response", "request_hash", "evidence")
```

两层防护：**整表丢弃**（`executors` / `tickets` / `idempotency` / `runs` / `artifacts` / `meta` 不导出）
+ **列名黑名单**（剩下的表逐列过滤）+ **字符串 scrub**。

**rc5 的处置**：改成**列白名单**了。`bigfish/backup.py:56` 现在是：

```python
AUDIT_COLUMNS: dict[str, tuple[str, ...]] = {
    "tasks": ("id", "title", "goal", "scope", "risk", "status", "current_version",
              "control", "executor_id", "blocked_reason", "cancel_requested_at",
              "created_at", "updated_at"),
    "task_versions": ..., "reviews": ..., "approvals": ..., "events": ..., "audit": ...,
}
```

导出时逐行 `if k not in allow: skipped.add(k); continue`，被跳过的列名汇总进
`manifest.redaction.dropped_columns`，`manifest.redaction.policy` 直接写 `"allowlist"`。
方向反过来了：漏一列只会**少导数据**（安全方向的失败），不会多导。
`AUDIT_DENY_COLUMNS` 留作兼容常量，注释写明「这些列名必须永远落在白名单之外」。

**仍有疑问**：白名单是**静态**的——新增一张表但忘了加进 `AUDIT_TABLES`，那张表就整个不导出
（不是泄露，是审计缺证据）。请你判断：整表丢弃该不该也在 manifest 里显式列出「本该有但没有」的表。
另外 `task_versions.prompt` 在白名单里（审计要正文），只靠 `scrub()` 兜底，见 Q2。

**rc6 的处置【rc6 已修，含 F-07】**：白名单方向没改，rc6 修的是**挂在白名单外面的那句声明**。
原来 manifest 只有一句 `contains_plaintext_credentials: false` 加一个「可外发」的 `handling`——
那是拿「没命中我们写的那几条正则」冒充「保证没有凭据」。F-07 之后，那句布尔**没删**
（测试只钉了 `handling` 的措辞，没钉这个布尔——所以它现在仍然写着 `false`，你可以据此挑刺），
但**旁边多了一段可以逐项核对的说明**：
`credential_scan` 里 `mode: "pattern-based"`、`patterns`（11 类模式名）、
`fields_scanned` 明说包含 `task_versions.prompt` 正文、`coverage` 明说「只覆盖上述已知形态；
换行拆分、编码变形、私有格式的密钥无法保证命中」、`hits`（脱敏后逐字段回扫的真实统计）、
`manual_review_required: True`。`handling` 改成「可外发（需人工复核）……脱敏不等于证明，
外发前请人工过一遍 `manifest.credential_scan`」。
两项测试直接钉这句话：`test_manifest_does_not_claim_guarantee` 断言 `handling` **不含**「无凭据」、
**含**「复核」；`test_manifest_declares_manual_review_required` 断言 `mode` 是 `pattern-based`、
不保证命中那句在、`manual_review_required` 为真、且 `patterns` 里有 `google-key`。
所以这条现在从「声明过强」变成「声明与能力对齐」——**但它仍然是 pattern-based 扫描，不是 DLP**（X5）。
**仍然请你挑刺**：`hits` 为空不等于「没有凭据」，只是「没命中这 11 类模式」，
这个语义要不要在 manifest 里再写死一点。

### Q8. `REVIEW_GATED` 不含 `DISPATCHED/RUNNING`　【rc5 部分解决：修了一条误伤支路，判据仍是状态制】

`bigfish/state.py`：`REVIEW_GATED = {IN_REVIEW, REVIEW_PASSED, PENDING_APPROVAL, APPROVED}`，
确实不含派工后状态。但它**不是靠集合推导**的，`revoke_review()` 里显式分了三支：

```python
# bigfish/core.py::revoke_review
if status in (PENDING_APPROVAL, APPROVED):        # 未派工 → 退回议事，吊销所有票
elif status in (DISPATCHED, RUNNING, CANCELLING):  # 已派工 → 吊销票 + 置 BLOCKED
                                                   #   reason=审查票被撤销，需人工确认副作用
```

**仍有疑问**：支路是 `elif` 硬编码的，将来新增「有执行授权但不在上面三个状态」的状态时，
这条支路会自动漏掉。请判断是否该改成「有活票就阻断」这种基于事实而非状态的判据。

**rc5 的处置（部分解决）**：rc5 修的是这条支路的**入口条件**，不是判据形式（R3）。
原实现不判 `kind`：撤一张 `comment` 也会走 `DISPATCHED/RUNNING/CANCELLING → BLOCKED` 并吊销执行票。
评论本来就不计票（`_vote_summary_locked` 只统计 `kind=vote`），撤它没有任何业务含义，
却能打挂一个正在跑的任务。改后先取 `kind`，`kind != "vote"` 时把 `task_row` 置空跳过整个状态评估：

```python
kind = row["kind"] if "kind" in row.keys() else "vote"
...
if kind != "vote":
    task_row = None      # 撤评论只下架文字，不碰状态机
if task_row is not None and version == task_row["current_version"]:
    ...
```

两条路径并排断言（`test_revoke_comment_does_not_block_running_task` 与
`test_revoke_vote_still_invalidates`）。

**仍是状态制**：还是 `elif` 硬编码三个状态。改成「有活票就阻断」这种事实判据，rc5 没做，
作者的顾虑是「活票 + 已撤票混在一起时，判据会变得比状态更难推理」。请你给判断。

**rc6 的处置【rc6 未改动】**：`revoke_review()` rc6 一行没碰，`elif` 三状态硬编码原样在，
rc5 那条 `kind != "vote"` 的入口修正也原样在。Astra 的复核意见与本文件一致：
对 `DISPATCHED` / `RUNNING` / `CANCELLING` 有显式阻断与吊销逻辑；
已提交 / 已完成的任务不自动回滚，属于需要在产品语义里明确的不可逆边界。
**仍然请你挑刺**：判据该不该从「状态 ∈ 三个常量」改成「有活票就阻断」。

---

### Q9. 平台 B 首次跑暴露的两个 Windows 专属缺陷（本轮新发现）　【rc6 已修】

这两条不是测试写错，是 Windows 与 POSIX 的语义差异落到了产品代码里。Linux 上的 265 项永远碰不到它们——只有真的在一台 Windows 上跑一遍才会露出来。列在这里是因为**同类问题大概率还有**，请你按这个方向继续挑。

#### Q9.1 `msvcrt.locking` 是**按文件句柄**记的，不是按文件

`bigfish/locks.py` 的 Windows 分支原本是这样：写者各持 `<db>.access.<pid>.lock`，用 `msvcrt.locking(LK_NBLCK)` 锁一个字节区间表示「我在用」。

问题：`msvcrt.locking` 的锁记在**句柄**上，不是记在文件上。同一进程里第二个 `Database` 会开第二个句柄、锁同一个文件的同一个区间——直接 `EACCES`。

```
bigfish.locks.LeaseError: 无法登记数据库写者租约
（C:\Users\...\council.db.access.32844.lock）：[Errno 13] Permission denied
```

后果是真实的：GUI 里同进程再开一个 `Database`、测试里的 `other = Database(self.db.path)`、恢复流程自己 `pause_lease()` 后再 `resume_lease()`，全都会撞上。在 Linux 上 `flock` 对同一文件开多个句柄各自 `LOCK_SH` 是合法的，所以这个分支从来没被真机检验过。

**修法**（`bigfish/locks.py`）：模块级 `_win_leases`（`锁文件路径 -> [文件句柄, 引用计数]`）。`acquire()` 命中共用句柄并计数 +1；`release()` 计数 -1，归零才 `LK_UNLCK` + `close()` + `os.unlink()`。POSIX 的 `fcntl` 分支一行未改。

**请你挑刺**：引用计数这套在「同一个进程里一个实例 `pause_lease()` 另一个还在用」时是否会算错？`os.unlink` 放在计数归零那一刻，和另一个正要打开这个文件的线程之间有没有窗口？

#### Q9.2 `cli.py` 的 restore 分支不关库

`restore_from_bundle()` 成功后直接 `return 0`。Linux 上进程退出即释放 fd；Windows 上「进程未退出前文件仍被占用」，于是调用方紧接着的删除/移动报错：

```
PermissionError: [WinError 32] 另一个程序正在使用此文件，进程无法访问: '...\council.db'
```

**修法**：restore 分支包 `try/finally` 关库。其他分支是短命只读操作，交给进程退出即可——只有恢复动的是库文件本身。

**请你挑刺**：`cli.py` 里其他分支（`task-show`、`export`、`sweep`）同样是短命只读但**都没有显式 close**。在 Windows 上若有人把它们嵌进更长的进程，会不会重现同一条 `WinError 32`？本轮只修了 restore 这一支，没做全量排查。

#### 两个缺陷的验证

| | 结果 |
| --- | --- |
| 修前（平台 B 首跑） | `FAILED (errors=2, skipped=1)` / `Ran 265 tests in 62.289s` |
| 两条 ERROR | `test_v011_rc5_cli_guard.TestCliRestoreGuard.test_force_bypasses_the_guard`、`test_v011_rc6.TestCrossProcessLease.test_second_database_on_same_file_blocks_restore` |
| 修后（平台 B） | `Ran 265 tests in 59.975s` / **`OK (skipped=1)`** / 退出码 0 |
| 修后（平台 A） | `Ran 265 tests in 41.215s` / `OK` |
| 提交 | `73104a8`（2 files / +81 / -21） |

**顺带暴露的一条口径问题**：这两个缺陷在 Linux 上 100% 不可见，而 rc6 之前所有「已验证」的结论都来自 Linux。所以「测试全绿」这句话的作用域必须写清平台——本轮之前 DELIVERY / TEST-REPORT / SECURITY-REVIEW 都写着「平台 B 未跑」，那一版是诚实的；问题在于**当时确实没有任何机制能逼出这条**，只能靠人真的去跑一遍。

---
### Q10. Codex 在仓库里提的八条（rc7 的全部来源）　【rc7 逐条已修】

`docs/AUDIT-CODEX-v0.1.1-rc6.md`（commit `f5e955930c`）给了 BFC-01～BFC-08。逐条复核结论与处置：

| # | Codex 的判断 | 我的复核 | rc7 处置 |
|---|---|---|---|
| BFC-01 | Windows 恢复互斥有 TOCTOU | **成立**：旧的 `_probe_other_writers()` 扫一遍再锁另一把锁，两把不是同一内核对象 | `locks.py` 整文件重写成单一内核对象（POSIX flock / Windows 64 槽位） |
| BFC-02 | MCP 无字段级长度上限 | **成立**：REST 有 `max_length`，MCP 只有 `type: string` | `_validate_args` 校验 `maxLength`，5 处自由文本补 `MAX_TEXT_FIELD` |
| BFC-03 | `create_app()` 可绕过监听闸门 | **成立**：上一轮已自认（X6），但只写了文档 | `create_production_app()` + `LoopbackGuardMiddleware` 双层 |
| BFC-04 | manifest 布尔声明过强 | **成立**：`contains_plaintext_credentials: false` 与 `manual_review_required: true` 自相矛盾 | 改枚举 `credential_status` 三值 |
| BFC-05 | CLI 非 restore 分支不关库 | **成立**：就是 Q9.2 作者自己挑刺的那条 | `main()` 统一 `finally` 关库 |
| BFC-06 | allowed-ports 不打印预留端口 | **成立** | 输出分 `implemented` / `reserved` |
| BFC-07 | 发布工程不闭环 | **成立**：requirements.txt 只有范围、无 lockfile、无 CI、无 exe 产物 | `requirements.lock` + `.github/workflows/ci.yml`（exe 产物仍未做，见 U2） |
| BFC-08 | tag 指向无父提交的根提交 | **成立**：`v0.1.1-rc6` → `a2e9275751`，`parents=0`，与 `main` 历史不连通 | 本版起在真实提交上打带注释 tag |

Codex 另外四条结构性意见：发布工程不闭环（BFC-07 处理一半，仍无 exe 产物）、`core.py` 与 `server.py` 耦合重（未动）、
移动端权限模型未落地（本轮只做了角色 `MOBILE_VIEWER`，REST/SSE 端点仍是 `docs/ARCHITECTURE-FUTURE.md` 的草案）、
exe/APK 分阶段路线（未动）。分层重构与 exe 产物明确不在 rc7 范围。

**仍然请你挑刺**：槽位模型只有 64 个，同一台机器开 65 个以上 `Database` 实例会全被拒，作者没实现「耗尽时排队或降级」。
另外 `_LOCAL` 是进程内字典，`fork` 出来的子进程会不会共享到父进程的引用计数，作者没验。

---

### Q11. 本地全绿、CI 一跑就红：两处平台专属缺陷（本轮新发现）　【rc8 已修】

这条不是「某个函数写错了」，而是**验证方法论上的漏洞**，比单点 bug 更值得记。

rc7 收尾时，平台 B 的证据是「某台 Windows 机器上手跑过一次 294 项」。当时还留了个尾巴：加进来的第 295 项（真·跨进程租约）没在 Windows 上跑。
到 rc8 要收口时，那台机器连不上（`ControlStatus` 一直是 `state=user`），只好把回归交给 GitHub Actions 的 `windows-latest`。

第一次跑（run `45170828326`，head `fa37e2e`）两个 Windows job 都红。但日志里最值钱的一句是：

```
Ran 318 tests in 181.158s
FAILED (failures=1, skipped=1)
```

**318 项在 Windows 上全跑完了。** 从 rc6 一路背到 rc7 的「Windows 跑不了全量」这个说法，到此证明是假的——当时只是某台机器被人占着，拿不到控制权。

两处失败：

| 失败 | 原文 | 性质 |
|---|---|---|
| `test_heartbeat_lease_is_in_the_future` | `AssertionError: 1795435663.622005 not greater than 1795435663.622005 : 续租时间没有前进` | 测试的时钟假设。Windows `time.time()` 分辨率约 15.6ms，claim 与 heartbeat 落在同一 tick |
| py3.13 job 的 `Self check` | `print("健康检查")` → `UnicodeEncodeError: 'charmap' codec can't encode characters in position 0-3` | **真产品缺陷**。英文版 Windows 跑 `tools/selfcheck.py`，第一行输出就整进程 traceback 退出 |

第二处尤其值得记：它不依赖任何特殊操作，就是「在英文版 Windows 上跑官方附带的自检脚本」。本机上（Linux + UTF-8）永远看不到，CI 用 cp1252 的 runner 一跑就露。

**修法**（细节在 `CHANGELOG.md` rc8 段与 `docs/DIFF-SUMMARY-v0.1.1-rc8.md` §9）：
`bigfish/console.py` 只在当前编码表示不了中文时才切 UTF-8 + `errors="replace"`，cp936 / UTF-8 环境一律不动；
在包导入点安装，所有入口（`-m bigfish`、CLI、GUI、每个 tools 脚本）自动覆盖。

**这轮留下的方法论**：

1. 「在 A 平台全绿」不能推「在 B 平台也绿」，只能推「B 平台没被测到」。
2. 拿不到 B 平台时，宁可写「未验证」，也不要把 A 平台的结论换个说法当成 B 的。
3. 平台 B 的证据应该由 CI 每次重跑，而不是「某年某月在某台机器上跑过一次」——后者只能证明那一版、那一时刻。
4. 第一次 CI 红不一定是代码错，也可能是「测试里的假设」错（时钟粒度）。但**不要因为它是测试就当成假警报**：另一处 cp1252 就是真缺陷，两条得分开判。

第二次 CI（run `45174755219`，head `fdd5c12`）五个 job 全 success，两个 Windows job 都是 `Ran 322 tests` + `OK (skipped=1)`，见第 4.4 节。
### Q12. 手写协议 vs 官方 SDK：为什么「自己测自己」不算兼容性证据（本轮新发现）　【rc9 已修】

rc8 及以前，`/mcp` 是一段 300 多行手写 JSON-RPC：自己拼 `initialize` 响应、自己拼 `tools/list`、自己拼 `tools/call`。
它有一整套测试（`tests/test_v011_api.py` 里 6 项 + 后来的 MCP 相关项），**全绿**。

问题是：**那套测试用的是我自己写的客户端**（`tools/selfcheck.py` 里的 `requests.post` + 手拼 JSON）。
手写服务端 + 手写客户端，两边对着同一份理解写，当然对得上。这只能证明「我认为的协议是自洽的」，
不能证明「官方客户端能连上」。换句话说——**测试在验证实现，不在验证协议**。

Codex 的审计第 6 条（【存疑】）指的就是这个。换成官方 SDK 之后，新测试 `tests/test_v011_rc9_socket.py` 干的事是：
进程内起真 uvicorn，用 `mcp.client.streamable_http.streamablehttp_client` + `ClientSession`（**官方实现**）跑完整闭环。
第一次跑就挂住 3 分钟——因为鉴权失败时我返回的是 `200 + {jsonrpc, id: null, error}`，
官方客户端的 `JSONRPCMessage.model_validate_json` 报 9 条 pydantic 错（`id` 不能为 null），然后**既不抛也不退**。
这就是 R9-02，光看服务端日志永远发现不了。

**方法论**（和 Q11 是同一类）：

1. 「自己写 A、自己写 B、测 A 和 B 能通」不构成协议兼容性证据，只构成自洽性证据。
2. 判断标准是「换一个独立实现来连」。第三方实现的存在本身就是证据。
3. 标准里写着的行为（凭据无效回 401）不是风格问题；偏离它会让真实客户端进入未定义状态（本例是挂死，不是报错）。
4. 依赖声明和实际使用要对齐：`requirements.txt` 里写了 `mcp` 却全仓零 `import mcp`，说明有一层东西被重写了——
   翻 `requirements` 和 `grep import` 的差集，是最省力的「重复造轮子」探测器。

**仍未闭合的部分**：换 SDK 之后，rc8 那套「精确到字节」的旧测试里有几处断言依赖手写实现的具体形状
（例如协议版本被硬编码成 `2025-03-26`）。SDK 的行为是**协商**——客户端要什么版本、在 `SUPPORTED_PROTOCOL_VERSIONS` 里就回什么。
所以 `test_initialize_negotiates_protocol` 改成了双断言：官方客户端拿到的版本必须在支持列表内，
且手工指名 `2025-03-26` 的请求必须原样回显。这是**语义更严**，不是放松。
## 8. 怎么跑起来

### Linux / macOS / WSL

```bash
bash tools/dev_setup.sh          # 建 venv + 装依赖（不用 apt）
bash tools/dev_setup.sh --test   # 顺带跑全量测试（失败会返回非零）
```

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

### 跑单个测试组

```bash
python -m unittest tests.test_v011_rc4.TestIdempotencyContract -v
python -m unittest tests.test_v011_rc3.TestConcurrency -v
python -m unittest tests.test_v011_rc6.TestCrossProcessLease -v   # rc6 的跨进程租约
```

---

## 9. 请按这个格式反馈

```
【结论】通过 / 有条件通过 / 打回

【P0 级问题】（会导致安全边界失效，必须修）
- 位置：文件:行号
- 问题：
- 复现：
- 建议：

【P1 级问题】（正确性/健壮性，应修）

【P2 级问题】（可维护性/文档，建议修）

【对第 7 节 8 个疑问的逐条回答】
Q1 ... Q12

【你认为还有哪些作者没想到的】
```

---

## 10. 审计边界（请你遵守）

- 本仓库**不含**任何真实凭据、真实执行票、真实内网 IP、真实主机名。
  内网地址已替换为 `100.64.0.11` / `100.64.0.12` / `100.64.0.21` 这类占位，主机名替换为 `CLIENT-A` / `CLIENT-B` / `VPS-HOST`。
- `bigfish/core.py` 里的 `TICKET_PREFIX = "bfc_tk_"`、`security.py` 里的 `SECRET_PREFIXES` 都是**代码常量**，不是真票据。
- 本项目是**单机本地工具**，威胁模型**不含**「攻击者已获得本机 root/Administrator」。
- 提交 PR 请附带：改了哪条不变式、加了哪个测试、原有 345 项是否仍全绿。

---

## 11. 变更历史摘要

| 版本 | 主题 | 测试数 |
|---|---|---|
| v0.1.0 | 可用骨架 | 21 |
| v0.1.1-rc1 | P0 数据安全（WAL 备份、审批版本漂移、执行票明文） | 103 |
| v0.1.1-rc2 | Windows 真机适配（`terminate` 退出码、端口释放判据） | 103 |
| v0.1.1-rc3 | 状态机取消语义、恢复原子性、幂等并发、监听地址、凭据脱敏 | 129 |
| v0.1.1-rc4 | 运行时所有权、库版本识别、恢复成对原子、审查绑定、多轮执行票、MCP 协议、端口预留 | 162 |
| v0.1.1-rc5 | 恢复后授权作废（R1）、事件落库（R2）、撤评论边界（R3）、锁内时间戳（R4）、执行收据（R5）、审计白名单（R6）、跳板硬闸门（R7）、恢复期排空（R8）、密钥自愈（R9）；schema 3 → 4 | **244** |
| **v0.1.1-rc9** | Codex 审计 AUDIT-RC8-01～06 收口：`/mcp` 传输层换官方 SDK（新增 `bigfish/mcp_adapter.py`，`server.py` 630 → 393 行）、未知 reviewer model 失败关闭（`DENIED_ROLE`）、注册入口与解析入口同源判定、鉴权失败改 HTTP 401、会话管理器每请求新建、维护期 MCP 前置 503；**schema 未变，仍 v4**；首次用官方 SDK 客户端走真实 socket 做兼容性测试 | **345** |
| **v0.1.1-rc8** | 第六轮硬化：R7-01 Windows 同进程句柄竞态（`locks.py` 整段串行 + 异常关句柄）、R7-02 未知角色失败关闭（`DENIED_ROLE` 哨兵 + 鉴权拒绝 + 审计）、R8-01 控制台解码兜底（`run_capture()` 五编码）、R8-02 cp1252 编码兜底（新增 `bigfish/console.py`）、R8-03 心跳续租时钟粒度；**schema 未变，仍 v4**。平台 B 改由 GitHub Actions 承担，322 项首次两个平台都完整跑完 | **322** |
| **v0.1.1-rc7** | Codex 审计 BFC-01～BFC-08 收口：`locks.py` 重写成单一内核对象（BFC-01）、MCP 字段级 `maxLength`（BFC-02）、`create_production_app` + 连接级回环闸门（BFC-03）、manifest 改 `credential_status` 枚举（BFC-04）、CLI 全分支关库（BFC-05）、`allowed-ports` 打预留端口（BFC-06）、`requirements.lock` + CI（BFC-07）、真实提交上打 tag（BFC-08）；另加手机端只读角色 `MOBILE_VIEWER`。**schema 未变，仍 v4** | **295** |
| **v0.1.1-rc6** | Astra 审计 F-01～F-09 收口：维护模式 guard/锁原子化（F-01）、`restore_epoch` 读取修复（F-02）、密钥自愈失败拒绝启动（F-03）、CLI 端口预检前移（F-04）、GUI 停服确认（F-05）、跨进程恢复租约 `locks.py`（F-06）、审计包凭据声明诚实化（F-07）、`create_app` 边界写明（F-08）、请求体上限（F-09）；**schema 未变，仍 v4** | **265** |

详见 `CHANGELOG.md`（v0.1.1-rc6 段有逐条记录）、`docs/SECURITY-REVIEW-v0.1.1-rc5.md`、
`docs/FIX-MATRIX-v0.1.1-rc4.md`、`docs/DIFF-SUMMARY-v0.1.1-rc4.md`。
rc7 的证据在 `CHANGELOG.md` 的 rc7 段、`tests/test_v011_rc7.py`（28 项）与本文件第 7 节 Q3/Q9.2/Q10。
rc8 的证据在 `CHANGELOG.md` 的 rc8 段、`tests/test_v011_rc8.py`（27 项）、`docs/TEST-REPORT-v0.1.1-rc8.md` §5（第一次 CI 两处失败原文）与本文件第 4.4 节。
rc9 的证据在 `CHANGELOG.md` 的 rc9 段、`tests/test_v011_rc9.py`（15 项）、`tests/test_v011_rc9_socket.py`（8 项）、`docs/TEST-REPORT-v0.1.1-rc9.md`、`docs/DIFF-SUMMARY-v0.1.1-rc9.md` 与本文件第 7 节 Q12。
rc6 也没有单独的 `docs/` 专档，证据散在 `CHANGELOG.md`、`tests/test_v011_rc6.py` 与第 4 节。

---

*本文件由项目作者编写，用于引导外部审计。文中所有「已验证」均可通过第 8 节的命令复现；
所有「未验证」都已在第 5 节列出。第 7 节标「仍有疑问」的地方是作者自己也没底的部分，请优先挑刺。*
