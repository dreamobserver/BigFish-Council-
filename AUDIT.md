# AUDIT.md — 给 GPT / Gemini 的审计入口

> 版本：`0.1.1-rc5`　最后更新：2026-11-20
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
| 版本 | `0.1.1-rc5`（`bigfish/__init__.py:6`） |
| 定性 | **发布候选（RC），不是生产版** |
| 数据库 schema | v4（`bigfish/db.py:22 SCHEMA_VERSION = 4`） |
| rc4 → rc5 的 schema 变化 | **有**：`MIGRATIONS[4]` = 加 `executors.disabled_reason` 一列 + 建 `idx_executors_enabled` 索引；BACKUP_FORMAT_VERSION 3 → 4；**不可降级** |
| 测试总数 | **244** 项 |
| 平台 A | Debian 12 / Python 3.11.2 / SQLite 3.40.1 → **244 项全绿** |
| 平台 B | Windows 11 / Python 3.14.6 / SQLite 3.50.4 → rc3 的 **129 项**全绿；**rc5 的 244 项本批次未跑**（rc4 的 162 项也未补跑） |
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
| `tests/test_v011_rc5_restore_authority.py` | 21 | 恢复禁用全部身份、reenable 轮换凭据、冻结 APPROVED、恢复后重放拒绝、维护模式、维护期 HTTP 语义 |
| `tests/test_v011_rc5_receipts.py` | 24 | `ReceiptLog` 读写与容错、`state_of` 判定、落盘耐久性、`run_once` 收据链、客户端接线 |
| `tests/test_v011_rc5_review_audit.py` | 13 | 双通过事件落库、撤评论不动审批、事务内时间戳、审计包列白名单 |
| `tests/test_v011_rc5_proxy.py` | 9 | 跳板监听硬闸门（0.0.0.0 / :: / [::] / 空值 / 内网 / 公网拒绝，回环放行） |
| `tests/test_v011_rc5_key_heal.py` | 7 | 指纹首次补记不动票、密钥轮换吊销全部活票 |
| `tests/test_v011_rc5_cli_guard.py` | 3 | CLI 恢复探端口拒绝、`--force` 绕过、端口探测助手 |
| `tests/test_v011_rc5_dispatch_id.py` | 3 | claim 下发 `dispatch_id`、重放同号、重新派工换号 |

v0.1.0 的原始 21 项（`test_core.py` + `test_api.py`）**一项没删、一项没放宽断言**。
rc3 的 26 项、rc4 的 33 项都是新增，不是替换。
rc5 新增 7 个文件共 **80 项**（21 + 24 + 13 + 9 + 7 + 3 + 3），
另有 2 项加在两个既有文件里（`test_v011_p0.py` 15 → 16、`test_v011_p3.py` 19 → 20），
合计 162 → **244**。

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

---

## 3. 目录结构与从哪读起

```
bigfish/
  core.py            领域核心：Council 类，所有状态迁移唯一入口
  db.py              SQLite 连接、schema、迁移、快照、审计表
  backup.py          备份导出 / 校验 / 原子恢复 / 脱敏审计包
  security.py        token 派生、HMAC、文件权限、脱敏
  state.py           状态常量与状态集合（这是状态机的「法律条文」）
  roles.py           四角色与权限位
  server.py          FastAPI + 手写 MCP JSON-RPC
  gui.py             PySide6 五个页面
  cli.py             21 个子命令（rc5 新增 reenable）
  executor_client.py 纯标准库执行器客户端（含安全重试）
  config.py          端口唯一真源、设置、端口预留表
  runtime.py         打开 Council 的统一入口
tests/               15 个文件，见上表
tools/               自检、网络检查、远程 MCP 闭环、GUI 冒烟、启停脚本
docs/                交付、测试、安全、网络、迁移、回滚、架构预留
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
3. `bigfish/security.py` 全文（只有几 KB）
4. `tests/test_v011_rc3.py` 的 `TestRestore` — 7 项恢复场景

**C. 关心并发与幂等**

1. `bigfish/db.py` 搜 `def tx(` — BEGIN IMMEDIATE + RLock 释放
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
# → Ran 244 tests / OK / exit 0
```

### 4.2 平台 B（Windows）

```powershell
# Windows 11 / Python 3.14.6 / SQLite 3.50.4
.venv\Scripts\python.exe -m unittest discover -s tests -t .
# rc3 时期实测：→ Ran 129 tests in 25.263s / OK / exit 0
# rc4 的 162 项与 rc5 的 244 项：本批次均未在 Windows 上跑（见第 5 节 U5）
```

### 4.3 其它工具

| 工具 | 结果 | 说明 |
|---|---|---|
| `tools/selfcheck.py` | 16/16 | 端到端自检：建任务→双审→批准→claim→心跳→回传→验收 |
| `tools/gui_smoke.py` | 9/9 | 离屏 GUI：主窗口构造、五页存在、五页可切换、状态栏、待审批识别 |
| `tools/e2e_loopback.py` | 22/22 | **同机回环**演练（明确声明不是跨设备） |
| `tools/net_check.py` | 分层 | 第 1–3 层本地；第 4 层跨机需真实对端 |
| `tools/remote_check.py` | 默认只读 | 真实 MCP JSON-RPC；必须显式 `--task-id` 才碰数据 |
| `pyflakes bigfish tools tests` | 无输出 | 平台 A 通过；**平台 B 未装 pyflakes，未做** |
| `compileall bigfish tools tests` | exit 0 | 两平台都可编 |

### 4.4 跨设备链路

VPS → Windows 客户端 `100.64.0.11:3096`（Tailscale Serve，仅 tailnet，无 Funnel）的 MCP 闭环 11/11
曾在 **rc2** 打通。**rc3 / rc4 / rc5 都未重跑跨机闭环**，这三版只证了本地监听校验与协议语义。
请不要把同机回环（`tools/e2e_loopback.py` 的 22/22）当成跨设备证据。

---

## 5. 明确未完成（不掩盖）

| # | 项 | 现状 | 影响 |
|---|---|---|---|
| U1 | GUI 真窗口人工验收 | 只做了离屏 smoke（9/9，覆盖构造窗口与切页） | 真实渲染、托盘行为、高 DPI 未人眼确认；rc5 新增的 `pause_services`/`resume_services` 恢复路径**既没有单测覆盖，也没在真窗口点过**——`gui_smoke.py` 不触发恢复流程 |
| U2 | 开机自启 | 未注册；`tools/start-headless.ps1` 真机未跑通（`schtasks /TR` 嵌套引号报 `-2147467259`） | WMI 起的进程重启即消失 |
| U3 | DSH MCP 连接导入 | 未导入（需人类授权） | 现在 DSH 尚未接上这个控制台 |
| U4 | rc3 / rc4 / rc5 跨机闭环 | 未重跑 | 跨机证据仍是 rc2 时期的 |
| U5 | 平台 B 的 rc5 244 项 | **本批次未跑**（rc3 的 129 项跑过，rc4 的 162 项也没补跑） | Windows 侧 rc5 无任何回归证据；`disabled_reason` 新列的迁移、`msvcrt.locking` 恢复锁分支都没在 Windows 上实跑 |
| U6 | 平台 B 静态检查 | 未装 pyflakes | Windows 侧只有单测证据 |
| U7 | 类型检查（mypy / pyright） | 未接 | 只有 `compileall` 与 `pyflakes` 两道 |
| U8 | 长时运行（soak） | 未做 24h 连续跑 | 长期泄漏、票过期满 6 小时后行为未验 |
| U9 | 恢复期维护模式的跨进程行为 | 单进程内已测（`MaintenanceError` / 503 / `/health`）；跨进程只做了 CLI 端口探测 | 端口探测与真正发生替换之间仍有时间窗（TOCTOU），见第 6 节 X1 |
| U10 | `ReceiptLog` 的对账闭环 | 客户端侧 24 项已测（含跨进程读）；`reconcile()` 只输出「已开工未完成」的 `dispatch_id` 清单 | 清单如何回传、由谁裁决，仍是人工动作，未做自动化对账 |

---

## 6. 已知残余风险（作者自述）

| ID | 风险 | 缓解 | 是否接受 |
|---|---|---|---|
| R1 | `approve` 的 actor 由调用方传入，无生物特征证明 | 本机拿到 CLI/GUI 的人可代批 | 接受（单机本地工具，威胁模型不含本机 root） |
| R2 | Windows ACL | 已实测 `secure_file` → `{'ok': True, 'mode': '0o666', 'acl': 'granted'}`、`verify_file_restricted` → `(True, 'acl limited')` | 已验证（Windows 11） |
| R3 | 恢复时若另一个进程开着 db，`os.replace` 可能失败 | 抛错 + 成对回滚，不损坏原库；rc5 起 CLI 先探端口拒绝、GUI 先停服进维护模式 | 接受（仍需用户关掉重复实例；`--force` 会绕过这道检查） |
| R4 | Tailnet 内任何持执行器 token 的设备可操作控制台 | 只能操作被分配给自己的任务 | 接受（设计意图） |
| R5 | `heartbeat_ttl` 默认 120s，网络抖动超时会误阻断 | 进 `BLOCKED` 需人工，不会自动继续 | 接受（fail-safe 方向正确） |
| R6 | 恢复期间 GUI 未暂停看门狗 | **rc5 已修**：GUI 改走 `pause_services()`/`resume_services()`，恢复期 db 处于维护模式，普通读写直接 `MaintenanceError` | 维护模式有 6 项单测；GUI 停服/接回这条路径**没有单测，也没在真窗口点过**（U1） |
| R7 | DB 文件本身未加密（无 SQLCipher） | 拿到磁盘即可读明文数据 | 接受（本地单机，且凭据不进库） |
| R8 | 审计导出包用**黑名单**列 | **rc5 已改为列白名单** `AUDIT_COLUMNS` + `manifest.redaction.policy="allowlist"`；漏列只会少导数据，且 `dropped_columns` 会如实上报 | 已缓解（见 Q7） |

rc5 新增的四条残余风险（作者主动登记）：

| ID | 风险 | 缓解 | 是否接受 |
|---|---|---|---|
| X1 | `cli restore --force` 是**人工逃生门**：它绕过端口探测直接替换库文件 | 默认关闭，只在明知会数据分叉时手动加；不加时 CLI 一定拒绝、GUI 一定先停服 | 接受（逃生门的价值就在于能被用；误用的后果是可回滚的） |
| X2 | executor 回传的自由文本只做**正则脱敏** | `scrub()` 覆盖 `bfc_*` / `Bearer` / `sk-*` / `gh*_*` / `AKIA*` / PEM；但把 token 拆成两半拼接这类变形拦不住 | 接受（残余，见 Q2）。事件表是掩码而非拒写 |
| X3 | 恢复锁只在**同一文件系统内**有效 | `_restore_lock` 用 `fcntl.flock` / `msvcrt.locking` 锁 `council.db.restore.lock`；跨文件系统、跨主机（比如库在 SMB 共享上）无法互斥 | 接受（部署前提是库在本地盘；跨机恢复本就不在威胁模型内） |
| X4 | 恢复结果的 `tickets_revoked` 曾长期是**常量 1**（不是实际吊销行数），运维若拿它对账会得到错误结论 | 已改为 `cur.rowcount` 真实取值（`bigfish/backup.py:352`），并由 `tests/test_v011_rc5_restore_authority.py::test_tickets_revoked_reports_real_count` 固定语义 | 已修（rc5 收尾时一并修） |

---

## 7. 请你重点看这几处（作者自认最可能还有问题的）

下面 8 条是**作者自己怀疑的地方**，不是「已经确认没问题」。每条都附 rc5 的处置标注
（【rc5 已修】/【rc5 部分解决】/【rc5 未改动】）；标「仍有疑问」的请优先挑刺。

> 标注含义：【rc5 已修】= 这一条提出的问题在 rc5 被正面解决并有单测；【rc5 部分解决】= 主要路径堵住了，
> 但作者仍认为有残余；【rc5 未改动】= rc5 一行没碰，疑问仍然成立。

### Q1. `approve()` 的双重防线是不是真的双保险　【rc5 未改动】

`bigfish/core.py` 的 `approve()` 里有两道检查：

1. `expected_version is not None and version != expected_version` → 抛「任务版本已变」
2. `UPDATE tasks SET status=?, ... WHERE id=? AND current_version=? AND status=?` → `rowcount != 1` → 抛「任务在审批过程中被修改」

**仍是疑问**：第 1 道在 `expected_version=None` 时完全不生效（CLI 默认不传）。第 2 道是真防线。
请判断：只靠第 2 道，在「两个 GUI 实例同时对同一任务 approve」时，是否**一定**只有一个能成功？
（作者的论证是 `BEGIN IMMEDIATE` 让两个事务串行，第二个读到已改的 status 后 rowcount=0；
但请你自己验，别信这段文字。）

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

### Q3. `check_bind_host()` 的覆盖面　【rc5 部分解决：多堵了一个入口，原疑问仍在】

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

---

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
Q1 ... Q8

【你认为还有哪些作者没想到的】
```

---

## 10. 审计边界（请你遵守）

- 本仓库**不含**任何真实凭据、真实执行票、真实内网 IP、真实主机名。
  内网地址已替换为 `100.64.0.11` / `100.64.0.12` / `100.64.0.21` 这类占位，主机名替换为 `CLIENT-A` / `CLIENT-B` / `VPS-HOST`。
- `bigfish/core.py` 里的 `TICKET_PREFIX = "bfc_tk_"`、`security.py` 里的 `SECRET_PREFIXES` 都是**代码常量**，不是真票据。
- 本项目是**单机本地工具**，威胁模型**不含**「攻击者已获得本机 root/Administrator」。
- 提交 PR 请附带：改了哪条不变式、加了哪个测试、原有 244 项是否仍全绿。

---

## 11. 变更历史摘要

| 版本 | 主题 | 测试数 |
|---|---|---|
| v0.1.0 | 可用骨架 | 21 |
| v0.1.1-rc1 | P0 数据安全（WAL 备份、审批版本漂移、执行票明文） | 103 |
| v0.1.1-rc2 | Windows 真机适配（`terminate` 退出码、端口释放判据） | 103 |
| v0.1.1-rc3 | 状态机取消语义、恢复原子性、幂等并发、监听地址、凭据脱敏 | 129 |
| v0.1.1-rc4 | 运行时所有权、库版本识别、恢复成对原子、审查绑定、多轮执行票、MCP 协议、端口预留 | 162 |
| **v0.1.1-rc5** | 恢复后授权作废（R1）、事件落库（R2）、撤评论边界（R3）、锁内时间戳（R4）、执行收据（R5）、审计白名单（R6）、跳板硬闸门（R7）、恢复期排空（R8）、密钥自愈（R9）；schema 3 → 4 | **244** |

详见 `CHANGELOG.md`、`docs/SECURITY-REVIEW-v0.1.1-rc5.md`、`docs/FIX-MATRIX-v0.1.1-rc4.md`、`docs/DIFF-SUMMARY-v0.1.1-rc4.md`。

---

*本文件由项目作者编写，用于引导外部审计。文中所有「已验证」均可通过第 8 节的命令复现；
所有「未验证」都已在第 5 节列出。第 7 节标「仍有疑问」的地方是作者自己也没底的部分，请优先挑刺。*
