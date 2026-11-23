# 交付说明：bigfish-council v0.1.1-rc5

> 本文所有数字来自 2026-11-20 在平台 A 实跑，原始输出在 `/home/dsh/bfc-rc5-logs/`。
> 前序文档：[rc4 交付说明](DELIVERY-v0.1.1-rc4.md)、[rc3 交付说明](DELIVERY-v0.1.1.md)；本批安全语义的完整论证见 [rc5 安全复核](SECURITY-REVIEW-v0.1.1-rc5.md)，本文不重复推导。
> 本文只写 rc5 这一批，不改写前两版文档。范围是整批：代码三点 `4281114` / `a4f0641` / `adc48a3` 加收尾 `b8f6de1`，相对 rc4 发布点 `72e8535` 代码侧 20 文件 / +2276 / -61，全量 **244 项 / 15 个文件**。

## 1. 一句话结论

**代码层面可发布，平台 B 待验。**

判据拆开写，不含糊：

- 九项审计问题 R1–R9 全部落到代码，且每一项都能定位到具体用例（§3 逐条给「文件::测试名」，不写「有测试覆盖」这种话）。
- 平台 A 全量 244 项通过（`b8f6de1`）、16 项自检、9 项 GUI 冒烟、pyflakes 零输出、compileall 退出码 0（§6）。
- 平台 B（Windows 11 / Python 3.14.6）本批次**一项未跑**。`msvcrt.locking`、`os.replace` 撞文件占用、NTFS ACL 这三条路径只有代码路径，没有实机证据。
- GUI 只在 `QT_QPA_PLATFORM=offscreen` 下过了构造与信号连接；真窗口、DPI、焦点顺序、托盘行为都没人看过。

所以：Linux 侧可以交付、可以拿去审计；上公司机 3096 的现役服务之前，先补平台 B 与真窗口验收（§7）。

口径：全库唯一数字是 **244 项 / 15 个文件**，对应 HEAD `b8f6de1`，本轮实测 `Ran 244 tests in 39.178s / OK`（日志 `/home/dsh/bfc-rc5-logs/unittest-linux.txt`）。收尾前的 `adc48a3` 上是 243 项，只作历史中间态提及，不再作为任何结论的测量点（§2.5 有对照）。

### 状态块

| 字段 | 值 |
| --- | --- |
| 版本号 | `0.1.1-rc5`（`bigfish/__init__.py:6` 的 `__version__`，本批 +1/-1 只改这一行） |
| 分支 / HEAD | `fix/v0.1.1-rc4-hardening` / `b8f6de1`（release 提交；其父为代码提交点 `adc48a3`） |
| 对比基线 | rc4 发布点 `72e8535` |
| 本批提交 | 代码三点：`4281114`（R1+R8）、`a4f0641`（R2+R3+R4+R6+R9）、`adc48a3`（R5+R8 跨进程那半边）；收尾：`b8f6de1`（R1 `tickets_revoked` 真实计数 + 三份根目录文档 + 六份 rc5 文档） |
| 定性 | 发布候选（release candidate），不是生产版 |
| 平台 A | Debian 12 / Linux 5.15.0-187-generic / Python 3.11.2 / SQLite 3.40.1 —— `b8f6de1` 244 项全绿（39.178s） |
| 平台 B | Windows 11 / Python 3.14.6 / SQLite 3.50.4 —— **本批次未跑** |
| SCHEMA_VERSION | **4**（rc4 是 3，需要迁移，见 §4） |
| 备份格式版本 | **4**（rc4 是 3） |
| 测试规模 | **244 项 / 15 个测试文件**（`b8f6de1`）；`adc48a3` 上是 243 项。rc4：162 项 / 8 个文件；7 个新文件 79 项，收尾再加 1 项 |
| 生产部署 | **未部署**。现役服务仍是 rc3（`832f699`）；rc4 也没上过生产，rc5 更未部署 |

## 2. 本轮交付物清单

代码区间 `git diff --stat 72e8535..adc48a3` 是 20 个文件 / +2251 / -60，按性质分四组；收尾提交 `b8f6de1` 另加 11 个文件（`bigfish/backup.py` +6/-2、`tests/test_v011_rc5_restore_authority.py` +22、三份根目录文档、六份 rc5 文档），见 2.5。

### 2.1 产品代码：8 个文件

| 文件 | 改动 | 行数 | 本批职责 |
| --- | --- | --- | --- |
| `bigfish/__init__.py` | +1/-1 | 8 | 版本号改 `0.1.1-rc5` |
| `bigfish/backup.py` | +142/-27（含收尾 `b8f6de1` 的 +6/-2） | 736 | R1 五步作废（`_block_inflight` `bigfish/backup.py:319`）、R6 列白名单（`AUDIT_COLUMNS` `bigfish/backup.py:56`）、R8 排空编排（`before_replace` 提前到 prerestore 之前，`bigfish/backup.py:552-557`）、R9 自愈改口（`_heal_key` `bigfish/backup.py:695`） |
| `bigfish/cli.py` | +28/-0 | 234 | 新增 `reenable <exec-id>` 子命令（`bigfish/cli.py:57`、`bigfish/cli.py:223-224`）；`restore` 加端口探测与 `--force`（见 2.4） |
| `bigfish/core.py` | +86/-13 | 1376 | R1 认证联动（`_disabled_message` `bigfish/core.py:241`、`reenable_executor` `bigfish/core.py:246`、`_replay_claim` `bigfish/core.py:843`）、R2 删 return（`bigfish/core.py:597`）、R3 kind 判定（`bigfish/core.py:497`）、R4 时间戳入事务（`bigfish/core.py:938-942`、`bigfish/core.py:1009`）、R5 下发 `dispatch_id`（`bigfish/core.py:838`） |
| `bigfish/db.py` | +83/-2 | 624 | schema 4（`bigfish/db.py:22`、`bigfish/db.py:205-208`）、R8 维护模式（`MaintenanceError` `bigfish/db.py:217`、`enter_maintenance` `bigfish/db.py:298`、`_guard` `bigfish/db.py:288`） |
| `bigfish/executor_client.py` | +227/-4 | 408 | R5 执行端收据：`ReceiptLog`（`bigfish/executor_client.py:41`）、`record` 逐条 fsync（`bigfish/executor_client.py:94-95`）、`state_of`（`bigfish/executor_client.py:123`）、`reconcile`（`bigfish/executor_client.py:337`） |
| `bigfish/gui.py` | +51/-3 | 1395 | R8 GUI 侧：`pause_services` / `resume_services`（`bigfish/gui.py:1222`、`bigfish/gui.py:1243`），恢复对话框加「冻结 / 禁用」两行回报 |
| `bigfish/server.py` | +16/-1 | 471 | R8 对外翻译：`MaintenanceError` → 503 + `Retry-After: 15`（`bigfish/server.py:126-133`）、`/health` 报 `maintenance`（`bigfish/server.py:136-152`）、MCP 回 `-32000`（`bigfish/server.py:255-257`） |

`bigfish/` 合计 14 个 `.py`、5918 行（`wc -l` 实测，`b8f6de1`）。

### 2.2 新增测试：7 个文件、80 项、1488 行

| 文件 | 行数 | 用例数 | 对应审计项 |
| --- | --- | --- | --- |
| `tests/test_v011_rc5_restore_authority.py` | 360 | 21 | R1（恢复后授权作废、reenable 换凭据、APPROVED 冻结、重放拒绝）+ R8（维护模式、503 语义） |
| `tests/test_v011_rc5_receipts.py` | 362 | 24 | R5（收据落盘、状态判定、逐条 fsync、跨进程可读、reconcile） |
| `tests/test_v011_rc5_proxy.py` | 173 | 9 | R7（bfc-proxy 回环闸门） |
| `tests/test_v011_rc5_key_heal.py` | 137 | 7 | R9（指纹补记不动票 / 轮换必须吊销全部活票） |
| `tests/test_v011_rc5_cli_guard.py` | 110 | 3 | R8 跨进程半边（CLI restore 拒绝在有服务监听时替换库） |
| `tests/test_v011_rc5_dispatch_id.py` | 88 | 3 | R5（派工号稳定与轮换语义） |
| `tests/test_v011_rc5_review_audit.py` | 258 | 13 | R2 / R3 / R4 / R6（事件落库、撤评论边界、锁内时间戳、审计白名单） |

行数与用例数是 `b8f6de1`（当前 HEAD）的值，合计 1488 行 / 80 项；其余 6 个文件与 `adc48a3` 一致。`adc48a3` 上这个文件是 338 行 / 20 项、合计 1466 行 / 79 项——差的那 1 项是收尾提交补的 `test_tickets_revoked_reports_real_count`。

### 2.3 按语义更新的既有测试：4 个文件

这四条不是「顺手改绿」，是审计点名后的重写或收紧。断言的取舍理由见 [rc5 安全复核](SECURITY-REVIEW-v0.1.1-rc5.md) 第四节。

| 文件 | 行数变化 | 用例数 | 改了什么 |
| --- | --- | --- | --- |
| `tests/test_v011_p0.py` | +27/-4 | 15 → 16 | 重写 `test_db_file_has_no_plaintext_ticket`：原文把 prompt 从 blob 里减掉再断言它不在（恒真）；现在直接查主库 + WAL 全部字节不得出现明文票与 `bfc_tk_` 前缀，并断言 `tickets.token_hash == sha256(明文票)`。新增 `test_prompt_body_written_to_task_versions` 反证 prompt 确实落库（否则「库里没有 X」可能只是「根本没写」） |
| `tests/test_v011_p1.py` | +38/-3 | 24（未加用例，重写 1 项） | 重写 `test_watchdog_survives_sweep_error`：原版只看 `stop` 后 `_watchdog` 是否为空；现在要求连续多次扫描抛异常线程仍存活、`errors` 计数跟上、换回正常 sweep 后还真的能把超时任务打成 `BLOCKED` |
| `tests/test_v011_p3.py` | +21/-0 | 19 → 20 | 新增 `test_graceful_shutdown_on_sigint`（POSIX）：SIGINT 后进程自己收尾 `rc == 0`、WAL 已 checkpoint 到 0 字节。原 `test_clean_shutdown` 接受 `(0, -15)`，被信号打死也算过 |
| `tests/test_v011_rc3.py` | +10/-2 | 26（断言更新 2 项） | `test_revoke_and_submit_race` 改断「该身份已被禁用」+「重新授权」（R1 之后吊销与恢复后禁用共用一条可执行文案）；`test_restore_older_schema_migrates` 改断恢复后 `APPROVED` → `BLOCKED` 且 `blocked_reason` 含「冻结」 |

用例数核对：162（rc4）+ 80（7 个新文件）+ 2（p0 / p3 各加 1）= **244**（`b8f6de1`）。p1 与 rc3 只重写、不增项。`adc48a3` 上是 162 + 79 + 2 = 243，差的 1 项就是 §2.2 里那条 `test_tickets_revoked_reports_real_count`。

### 2.4 工具与 CLI 变更：3 处

| 变更 | 位置 | 为什么算交付物 |
| --- | --- | --- |
| `bfc-proxy` 监听地址闸门 | `tools/bfc-proxy.mjs`（+58/-1）：`stripBrackets`（`tools/bfc-proxy.mjs:26`）、`isLoopback`（`tools/bfc-proxy.mjs:39`）、listen 前判定与 `process.exit(1)`（`tools/bfc-proxy.mjs:63-75`） | R7 的修复本体在这儿，不在 `bigfish/` 里。它是唯一一个不在 Python 包内的产品级改动 |
| CLI `restore` 端口探测 + `--force` | `bigfish/cli.py:41`（`_port_busy`）、`bigfish/cli.py:126`（`--force`）、`bigfish/cli.py:212-219`（拒绝并 return 2） | R8 的跨进程那半边。维护模式只管得住本进程的连接，管不住另一个正在跑 headless / GUI |
| CLI `reenable <exec-id>` | `bigfish/cli.py:57`、`bigfish/cli.py:223-224`，落到 `core.reenable_executor`（`bigfish/core.py:246`） | R1 的出口。恢复后所有远端身份被禁用，没有这条命令，人类只能重新 `executor-add` 开新身份 |

### 2.5 文档与收尾提交

六份 rc5 文档、三份根目录文档，连同 `tickets_revoked` 的真实计数改动，已在收尾提交 `b8f6de1` 里一并落库（`git show --name-only b8f6de1` 列出 11 个文件）。所以下面不再有「未纳管 / 未提交」，只有「尚未写」。

| 文件 | 状态 |
| --- | --- |
| `docs/SECURITY-REVIEW-v0.1.1-rc5.md` | 已提交（403 行）。安全语义的完整论证以它为准 |
| `docs/FIX-MATRIX-v0.1.1-rc5.md` | 已提交（182 行）。R1–R9 的逐条对应与严重度口径以它为准，本文 §3 是交付视角的重述 |
| `docs/TEST-REPORT-v0.1.1-rc5.md` | 已提交（594 行），工作区已扩到 651 行（口径统一为 244 项、2.2/2.3/2.4 重写）。测试环境与逐文件明细以它为准 |
| `docs/TESTLOG-v0.1.1-rc5.md` | 已提交（330 行），工作区 365 行。原始日志摘录 |
| `docs/DIFF-SUMMARY-v0.1.1-rc5.md` | 已提交（1332 行）。逐文件 diff 讲解，§2 / §3 的代码细节以它为准 |
| `docs/DELIVERY-v0.1.1-rc5.md` | 本文。`b8f6de1` 里那版 464 行；当前工作区是 474 行（§1 口径、§2.5、§3 行号、§6 矩阵都按 HEAD 重写），`git status` 显示 `M` |
| `docs/ROLLBACK-v0.1.1-rc5.md` | 已生成、待提交（404 行 / 20849 字节）。三条硬约束与两条回滚路线以它为准 |
| `docs/MIGRATION-v0.1.1-rc5.md` | 已生成、待提交（430 行 / 25614 字节）。schema 3→4 的两条 SQL、失败回滚实测、key 指纹三分支以它为准 |
| `AUDIT.md` / `CHANGELOG.md` / `README.md` | 已提交（`+159/-49`、`+106`、`+88/-29`）：AUDIT 新增 I7（维护期不许半份写入被当成成功），CHANGELOG 加 rc5 段，README 换版本说明 |
| `bigfish/backup.py` / `tests/test_v011_rc5_restore_authority.py` | 已提交：`tickets_revoked` 从硬编码 `1` 改为真实 `rowcount` 并透传到 `restore_bundle` 返回值（`bigfish/backup.py:352`、`bigfish/backup.py:599`），配 `::test_tickets_revoked_reports_real_count`。这是 243 → 244 的唯一差别 |

## 3. 逐项交付（R1–R9）

每条四段：问题 → 改动 → 测试证据 → 安全影响。行号对当前 HEAD `b8f6de1` 实测（`b8f6de1` 只动了 `bigfish/backup.py` 的两三行，其余文件行号与 `adc48a3` 一致）。

### R1 恢复后授权未作废

**问题**

从旧备份恢复等于「把过去发出去的所有凭据一起复活」。改前 `restore_bundle` 只做密钥轮换 + `epoch+1`，于是四件事同时成立：快照里 `executors.token_hash` 原样回来（旧 token 继续能认证）、`tickets.revoked_at` 回到 `NULL`（旧票重新可领）、`APPROVED` 任务重新变成「等待派工」、`idempotency` 表一起回来（`_replay_claim` 只查缓存，把已吊销的票当正常响应发回）。

**改动**

- `_block_inflight`（`bigfish/backup.py:319`）扩成五步、同一事务：在途 → `BLOCKED`；`APPROVED` → `BLOCKED` 且 `blocked_reason` 含「冻结」（`bigfish/backup.py:344-351`）；活票全 `revoked_at` 并把真实影响行数作为 `tickets_revoked` 返回（`bigfish/backup.py:352-353`，收尾提交 `b8f6de1` 把原来的硬编码 `1` 换成 `rowcount`，再由 `bigfish/backup.py:599` 透传到 `restore_bundle` 返回值）；`executors` 全 `enabled=0` + `disabled_reason`（`bigfish/backup.py:354-357`，常量 `DISABLED_REASON_RESTORE` 在 `bigfish/backup.py:72`）；清 `claim/heartbeat/submit` 幂等（`bigfish/backup.py:358-359`）。只翻状态位，一行不删（审计要用）。
- 认证侧把「查得到行」和「能用」拆开：`authenticate`（`bigfish/core.py:188`）与 `authenticate_any`（`bigfish/core.py:210`）在 `token_hash` 命中但 `enabled=0` 时抛 `_disabled_message`（`bigfish/core.py:241`）：「该身份已被禁用：{原因}；必须由人类所有者重新授权并更换凭据」。
- `reenable_executor`（`bigfish/core.py:246`）在同一事务里先 `new_token`、再 `UPDATE ... SET enabled=1, disabled_reason=NULL, token_hash=?, last_seen=NULL`，写 `executor.reenabled` 审计（`bigfish/core.py:264-266`）。只翻 `enabled=1` 等于让快照里那份 `token_hash` 的旧 token 原地复活，所以必须换哈希。
- `_replay_claim`（`bigfish/core.py:843`）改成顺序校验：取消 → 归属 → 状态 → 版本 → `_check_ticket_row`（`bigfish/core.py:900`，校验 `revoked_at` / `epoch` / `expires_at` / `max_expires_at`）。任何一项不过就拒绝。

**测试证据**

- `tests/test_v011_rc5_restore_authority.py::TestRestoreDisablesIdentities::test_restore_disables_every_executor`
- `tests/test_v011_rc5_restore_authority.py::TestRestoreDisablesIdentities::test_restored_access_token_no_longer_authenticates`、`::test_reviewer_token_also_rejected_after_restore`
- `tests/test_v011_rc5_restore_authority.py::TestReenableRotatesCredential::test_reenable_rotates_token_and_old_one_dies`（断言新旧 token 不等且旧 token 物理不可用）、`::test_reenable_keeps_role_and_model_and_writes_audit`、`::test_disabled_reason_cleared_after_reenable`
- `tests/test_v011_rc5_restore_authority.py::TestRestoreFreezesApproved::test_approved_task_frozen_not_claimable`、`::test_inflight_task_blocked_and_ticket_revoked`
- `tests/test_v011_rc5_restore_authority.py::TestClaimReplayAfterRestore::test_replay_rejected_after_restore`、`::test_replay_rejected_after_cancel`
- `tests/test_v011_rc5_restore_authority.py::TestRestoreDisablesIdentities::test_tickets_revoked_reports_real_count`（收尾提交补的：断言返回值是真实吊销张数，不是常量 1）
- 语义更新：`tests/test_v011_rc3.py::TestRestore::test_restore_older_schema_migrates`、`tests/test_v011_rc3.py::TestConcurrency::test_revoke_and_submit_race`

**安全影响**

恢复动作从「回退数据」变成「回退数据 + 作废全部已发出的授权」。人类必须显式 `reenable` 才能让某个执行器重新上线，而且这个动作会强制换凭据——「以为恢复了干净状态、实际恢复了一张授权清单」这条路被堵死。

### R2 `_recompute_review` 提前 return 吞掉事件

**问题**

双模型都投通过票时，`_recompute_review` 的 `all_approved` 分支更新完任务状态就 `return`，跳过了函数末尾统一落库的 `log_event` 循环。`task.review_passed` 与 `task.pending_approval` 两条事件永远不落 `events` 表。这不是显示问题，是审计链断裂：事后只看事件表的人会认为这任务从没通过审查。

**改动**

`bigfish/core.py:597` 删掉 `all_approved` 分支的 `return`，未通过分支改 `elif`，控制流无论如何走到 `bigfish/core.py:639-640` 的事件落库循环。事件收集与落库重新变成必经路径。

**测试证据**

`tests/test_v011_rc5_review_audit.py::TestReviewPassedEvents::test_events_written_on_all_approved`、`::test_status_still_reaches_pending_approval`、`::test_events_written_when_votes_cast_before_review`（先投票后送审的路径同样不能丢事件）。

**安全影响**

审批动作重新具备完整的事件证据链。缺事件的后果比错事件更麻烦——错事件能看出来，缺事件看起来一切正常。

### R3 撤普通评论能打挂运行中任务

**问题**

`revoke_review` 只看 `reviews` 行在不在，不看 `kind`。撤一条 `kind=comment` 的评论会触发与撤投票完全相同的降级：`APPROVED` 打回 `IN_REVIEW`，`DISPATCHED` / `RUNNING` / `CANCELLING` 打成 `BLOCKED`。等于任何能评论的角色都能用「留一条评论再撤回」行使否决权。

**改动**

`bigfish/core.py:497` 先读 `kind`（`bigfish/core.py:513`，老库缺列时按 `vote` 处理保持向后兼容）；`kind != "vote"` 时把 `task_row` 置空（`bigfish/core.py:521`），`bigfish/core.py:524-538` 的降级只在 `vote` 时执行。撤回评论照旧写审计、照旧标记撤回，只是不碰任务状态。

**测试证据**

`tests/test_v011_rc5_review_audit.py::TestRevokeComment::test_revoke_comment_does_not_touch_approved_task`、`::test_revoke_comment_does_not_block_running_task`（并断言票据仍可用、心跳仍能推进到 `RUNNING`）；反向保护 `::test_revoke_vote_still_invalidates`——撤真票必须照旧生效，防止修过头。

**安全影响**

评论权限与审批权限彻底分开。撤回仍是可审计的动作，但不再具备否决力。

### R4 心跳/提交的时间戳取在事务外

**问题**

`heartbeat` 与 `submit_result` 在进入 `with self.db.tx()` 之前就调了 `now()`。等 SQLite 写锁（`busy_timeout` 5000ms）期间这个 `t` 已固定，续租公式 `new_exp = t + heartbeat_lease` 于是算出比写入时刻更早的 `expires_at`。负载越高，票据有效期被侵蚀越多，看门狗把正常执行的任务判死。

**改动**

`t = now()` 移到事务内第一行：`bigfish/core.py:938-942`（`heartbeat`，函数在 `bigfish/core.py:932`）、`bigfish/core.py:1009`（`submit_result`，函数在 `bigfish/core.py:998`）。续租仍是 `min(t + heartbeat_lease, max_expires_at)`（`bigfish/core.py:973-976`），但 `t` 已是持锁后的时刻，`expires_at` 必然晚于写入时刻。

**测试证据**

`tests/test_v011_rc5_review_audit.py::TestTimestampInsideTransaction::test_heartbeat_lease_is_in_the_future`（断言 `expires_at > renewed_at`，且返回的 `lease_expires_at` 与库内一致）、`::test_heartbeat_keeps_lease_alive_repeatedly`（连续五次续租 `expires_at` 单调不减）。

**安全影响**

失效方向从「并行度越高越容易误杀」变成「与负载无关」。这是可用性问题，也是安全边界问题——被误杀的任务会带着副作用的空白进入人工判断。

### R5 执行端没有开工收据

**问题**

所有执行痕迹只在服务端库里。库一回退，「这次派工跑没跑、跑到哪一步」永久丢失。票只能证明「它曾经被授权」，不能证明「它做过什么」。而 R1 的恢复恰好会作废所有票，让判断依据同时消失。

**改动**

- 执行端 `ReceiptLog`（`bigfish/executor_client.py:41`）：JSONL 追加写，事件白名单 `EVENTS`（`bigfish/executor_client.py:54`）与终态集 `CLOSED_EVENTS`（`bigfish/executor_client.py:57`）；`record()`（`bigfish/executor_client.py:68`）每写一行就 `flush()` + `os.fsync()`（`bigfish/executor_client.py:94-95`）——收据的价值全在「断电也在」；`read_all()`（`bigfish/executor_client.py:98`）坏行跳过；`state_of()`（`bigfish/executor_client.py:123`）给四级判定；`summary()`（`bigfish/executor_client.py:159`）按 `dispatch_id` 去重。
- 服务端下发稳定派工号：`claim` 返回 `dispatch_id = ticket_id`（`bigfish/core.py:838`），`_replay_claim` 返回缓存里的 `ticket_id`（`bigfish/core.py:884`）——幂等重放派工号不变（同一轮执行不该被记两次），重新批准后再领取会换新票号、也就换了新派工号。
- 执行端 `dispatch_id_of()`（`bigfish/executor_client.py:208`）对老服务端回退到 `ticket_id`（降级而非报错）。开关三态用哨兵 `_UNSET`（`bigfish/executor_client.py:176`）区分「不传（读环境变量 `BFC_RECEIPTS`）/ 显式不要（内存 no-op）/ 给路径」。

**测试证据**

- `tests/test_v011_rc5_receipts.py`：`TestReceiptLog`（6 项）、`TestStateOf`（6 项）、`TestDurability::test_record_is_flushed_to_disk_immediately`、`TestDurability::test_reader_in_new_process_sees_rows`（新进程能读到，证明真落盘）、`TestRunOnceReceipts`（5 项）、`TestClientWiring`（5 项）
- `tests/test_v011_rc5_dispatch_id.py::TestDispatchId::test_claim_returns_dispatch_id`、`::test_replay_returns_same_dispatch_id`、`::test_redispatch_gets_new_dispatch_id`

**安全影响**

事故善后第一次有了「执行端视角」的证据。服务端库回退后，人工可以拿 `reconcile()` 列出「结果未知」的 `dispatch_id` 逐个核对副作用，而不是靠猜。

### R6 审计包用列黑名单

**问题**

导出逻辑 `SELECT *` 后用 `AUDIT_DENY_COLUMNS` 剔敏感列。黑名单的失败方向是「漏一列 = 漏一次凭据」：以后给 `tasks` 加一列 `executor_token`，它自动进包，而「包里多了一列」看起来完全正常。审计包是要外发的，一次失误就是永久外泄。

**改动**

改列白名单 `AUDIT_COLUMNS`（`bigfish/backup.py:56`），六张表逐列显式列出（`tasks` 13 列、`task_versions` 5 列、`reviews` 11 列、`approvals` 7 列、`events` 5 列、`audit` 6 列）；导出循环（`bigfish/backup.py:165-175`）只取 `allow` 内的列，其余计入 `dropped`。`AUDIT_DENY_COLUMNS`（`bigfish/backup.py:69`）保留仅为兼容旧引用，不再是判定依据。

失败方向反过来了：白名单漏一列 = 少导出数据（外发包不完整，人工一看就发现，可重导），黑名单漏一列 = 凭据外泄（静默、不可逆）。

manifest 从「声称」变「可核对」：`redaction = {policy: "allowlist", allowed_columns, dropped_tables, dropped_columns, string_scrub}`（`bigfish/backup.py:192-199`），`dropped_tables` 固定列出 `executors` / `tickets` / `idempotency` / `runs` / `artifacts` / `meta`。拿包的人可以自己比对「包里出现的每个列名是否都在 `allowed_columns` 里」，不需要相信实现。

**测试证据**

`tests/test_v011_rc5_review_audit.py::TestAuditBundleAllowlist::test_every_exported_column_is_on_the_allowlist`（逐个 JSON 逐个列名比对）、`::test_no_sensitive_column_names_leak`（不看注释不看 manifest，直接把包里所有键名拉出来查 `token` / `hash` / `ticket` / `secret` / `key` / `password`）、`::test_manifest_declares_allowlist_policy`、`::test_dropped_columns_reported_when_present`、`::test_prompt_body_scrubbed_of_ticket`（往事件里塞一张真票，断言包里既看不到明文票又能看到脱敏标记）。

**安全影响**

外发组件的泄露面从「随 schema 演进而扩大」变成「只随显式白名单扩大」。新加敏感列默认进不了包。

### R7 `bfc-proxy` 监听地址无校验

**问题**

`LISTEN_HOST` 直接取 `BFC_PROXY_LISTEN_HOST`，`server.listen` 照单全收。设成 `0.0.0.0` 就把本该只在本机工作的 MCP 跳板绑到所有网卡上，局域网内任何主机都能在认证层之外先摸到接口。它发生在「辅助脚本」里，使用者根本不会去审计它。

**改动**

两层：`stripBrackets`（`tools/bfc-proxy.mjs:26`）把 `[::1]` 归一成 `::1`（Node 的 `listen` 不接受方括号形式）；`isLoopback`（`tools/bfc-proxy.mjs:39`）白名单判定，接受 `127.0.0.1` / `localhost` / `::1` / `127.0.0.0/8`，拒绝空串、`0.0.0.0`、`::`、`[::]`、任意主机名、任一段大于 255 的伪地址。判定放在 `server.listen` **之前**（`tools/bfc-proxy.mjs:63-75`），不合法用 `fs.writeSync(2, msg)` 同步写 stderr 再 `process.exit(1)`——管道 / 重定向下 `console` 是异步的，`process.exit` 会把最后一行错误吞掉。

**测试证据**

`tests/test_v011_rc5_proxy.py::ProxyListenGateTest` 共 9 项：`test_any_address_is_rejected`、`test_ipv6_any_is_rejected`、`test_ipv6_any_bracketed_is_rejected`、`test_empty_value_is_rejected`、`test_lan_address_is_rejected`、`test_public_address_is_rejected`、`test_default_loopback_is_allowed`、`test_loopback_range_and_hostname_are_allowed`、`test_bracketed_ipv6_is_not_rejected_by_gate`。本机 `node --version` = `v22.23.2`，9 项全部 ok（13.824s）。

**安全影响**

T7（网络暴露）从「靠默认值」变成「靠闸门」。默认值仍是被允许的那一个，但显式设成非回环会直接起不来，而不是静默绑上去。

### R8 恢复期没有排空

**问题**

两种平台各坏一套。POSIX：`os.replace` 只换目录项，已打开旧 inode 的连接继续往旧文件写——新库是「恢复出来的状态」，在途写入全进那个没人看的旧 inode，数据静默分叉。Windows：文件被占用，替换直接失败，库卡在「staging 已生成、正式文件还没换」的中间态。两种情况 GUI / CLI 都不会先停服务。

**改动**

- **排空靠拿到写锁，不靠等**。`Database.tx()` 整个事务期间持 `self._lock`；`enter_maintenance`（`bigfish/db.py:298`）调 `self._lock.acquire(timeout=timeout)`——拿到锁本身就是「当前没有在途写事务」的证明。用 `sleep` 猜的问题是：它只能证明「我等了一会儿」。拿不到锁就抛错，宁可拒绝恢复也不在半排空状态替换文件。
- 维护模式下 `query` / `one` / `execute` 首行 `self._guard()`（`bigfish/db.py:447`、`452`、`457`），`_Tx.__enter__` 首行也是 `_guard()`（`bigfish/db.py:470`）——连事务都不抢锁，直接 `MaintenanceError`（`bigfish/db.py:217`）。恢复自身用 `_allow_during_maintenance()`（`bigfish/db.py:332`）直通；`db.maintenance()`（`bigfish/db.py:323`）保证无论成败都退出。
- `restore_bundle`（`bigfish/backup.py:463`）全程持维护模式（`bigfish/backup.py:501` 进入、`bigfish/backup.py:510` 包 `_allow_during_maintenance`）；`before_replace` 提前到生成 `.prerestore` **之前**（`bigfish/backup.py:552-553`，副本在 `bigfish/backup.py:555-560`）——先停服务再打快照，否则快照里可能混入正在写的半截状态。
- 替换失败且回滚也失败时置 `keep_maintenance = True` 再抛双错（`bigfish/backup.py:648-654`），`bigfish/backup.py:662-664` 跳过 `exit_maintenance`。此时库的实际状态未知，退出维护等于让服务往状态不明的库里写。宁可 503。
- 对外统一成「暂时不可用」：`bigfish/server.py:126-133` 回 503 + `Retry-After: 15` + `{"ok": false, "maintenance": true}`；`bigfish/server.py:255-257` 的 `/mcp` 回 `-32000`（不返回 `result`，避免客户端把维护态当成功）；`bigfish/server.py:136-152` 的 `/health` 报 `status="maintenance"`。
- 跨进程：`bigfish/cli.py:41` 的 `_port_busy` 在 restore 前探 `api_host:api_port`（`bigfish/cli.py:212-219`），有人监听就打印处置办法并返回 2；GUI 侧 `bigfish/gui.py:1093-1101` 先 `pause_services()`、把 `before_replace=self.win.pause_services` 传进恢复流程、`finally` 里 `resume_services()`（`bigfish/gui.py:1222`、`bigfish/gui.py:1243`，幂等：停 ApiServer + 停看门狗）。

**测试证据**

- `tests/test_v011_rc5_restore_authority.py::TestMaintenanceMode`：`test_writes_rejected_inside_maintenance`、`test_reads_recovered_after_exit`、`test_maintenance_survives_exception`、`test_enter_waits_for_inflight_transaction`（真实起一个长事务，验证排空确实等到了它）、`test_restore_always_exits_maintenance`、`test_restore_failure_exits_maintenance`
- `tests/test_v011_rc5_restore_authority.py::TestMaintenanceHttpSemantics`：`test_api_returns_503_during_maintenance`、`test_mcp_returns_error_not_success_during_maintenance`、`test_dispatch_tool_raises_maintenance_not_business_error`、`test_health_reports_maintenance`
- `tests/test_v011_rc5_cli_guard.py::TestCliRestoreGuard::test_refuses_when_api_port_is_busy`、`::test_force_bypasses_the_guard`、`::test_port_busy_helper`
- GUI 侧 `pause_services` / `resume_services` **没有专用自动化用例**：`tools/gui_smoke.py` 的 9 项只覆盖构造、五页切换、状态栏与待审批识别，不覆盖恢复路径。这是 §8 的一条未完成项。

**安全影响**

「恢复成功」现在是一个可判定的状态：写请求要么在替换前结束，要么被明确拒绝（503 而不是假装成功）。判据是锁，不是时间。

### R9 `ensure_key_consistency` 自愈后旧票仍能认证

**问题**

自愈分支换了密钥，文案也写着「恢复前的执行票全部失效」，但函数里根本没碰 `tickets` 表。票是确定性派生（`bigfish/security.py:196` 的 `derive_ticket`，`bigfish/security.py:201` 拼 `material`），而认证**不重算 HMAC**——票被哈希成 `token_hash` 后去 `tickets` 表查行。票的有效性只取决于「行还在不在、`revoked_at` 是不是空、`epoch` 对不对」，与当前密钥无关。所以换密钥唯一的效果是「以后新签的票不一样」，对已发出的那批一点作用都没有。

**改动**

`ensure_key_consistency`（`bigfish/backup.py:672`）分三种动作交给 `_heal_key`（`bigfish/backup.py:695`）：密钥文件不存在 → `action="new-key"`（按换钥处理，旧密钥拿不回来）；`meta` 里没有 `key_sha256`（老库首次升级到 rc5）→ `action="recorded"` 且 `rotate=False`，**只补记指纹、不动票**（票还是同一把密钥签的，作废等于无故中断在途执行）；指纹不一致 → `action="rotated"`。

只有 rotate 分支执行吊销：`bigfish/backup.py:719-724` 里 `UPDATE tickets SET revoked_at=? WHERE revoked_at IS NULL` 并清 `claim/heartbeat/submit` 幂等（否则重放会用缓存把已吊销的票再发一次，与 R1 同类），然后写 audit `db.key_healed`（`bigfish/backup.py:725-732`），detail 写明吊销了多少张。吊销票不改任务状态——任务是否继续由看门狗或人工判断。

**测试证据**

`tests/test_v011_rc5_key_heal.py::TestKeyFingerprintBootstrap::test_first_run_records_fingerprint_without_touching_tickets`（补记指纹不动票，且在途票仍能心跳到 `RUNNING`）、`::test_second_run_is_noop`；`TestKeyRotationInvalidatesTickets::test_rotated_key_revokes_every_live_ticket`、`::test_old_ticket_cannot_authenticate_after_rotation`（核心断言：换钥后旧票必须被 `CouncilError` 拒绝，且任务状态不被顺手改动）、`::test_rotation_purges_claim_idempotency`、`::test_rotation_leaves_audit_trail`（detail 含「吊销」）、`::test_missing_key_file_regenerates_and_revokes`。

**安全影响**

「换了钥」与「旧票失效」这两件事终于绑在一起。同时保留了一条不该误伤的路径：老库首次升级只补指纹，不会平白中断在途执行。

## 4. 数据库迁移说明

### 4.1 schema 3 → 4 的全部内容

`bigfish/db.py:22` 的 `SCHEMA_VERSION = 4`，`bigfish/db.py:23` 的 `BACKUP_FORMAT_VERSION = 4`。迁移体只有两条 SQL（`bigfish/db.py:205-208`）：

```sql
ALTER TABLE executors ADD COLUMN disabled_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_executors_enabled ON executors(enabled);
```

只加一列 + 建一个索引，**不重建任何表**：没有 `CREATE TABLE ... AS SELECT`、没有 `DROP TABLE`、不搬数据。`disabled_reason` 是 `TEXT` 且无默认值，存量行读出 `NULL`，语义正好是「没有被禁用」——不需要回填。索引 `idx_executors_enabled` 服务于 R1 的 `UPDATE executors ... WHERE enabled=1` 与列表页。

### 4.2 迁移怎么执行

`_migrate_locked`（`bigfish/db.py:412`）：

1. 先 `_peek_schema_version_locked()` 探测版本，**再决定是否动文件**；
2. `current > SCHEMA_VERSION` 直接抛 `DatabaseVersionError`（`bigfish/db.py:254`），一个字节都不写；
3. `current == SCHEMA_VERSION` 直接返回，老库启动不产生任何写入；
4. 逐版本迁移，每一步单独 `BEGIN IMMEDIATE` / `COMMIT`（`bigfish/db.py:423`），失败就停在原版本，不留半迁移状态；
5. `duplicate column name` 视为成功（`bigfish/db.py:430`），所以迁移可重复执行。

### 4.3 本轮实测

| 场景 | 做法 | 结果 |
| --- | --- | --- |
| rc4 库（schema 3）升级 | 造一个「删掉 `disabled_reason` 列与索引、`meta.schema_version` 写 3」的库，再用 rc5 打开 | `schema_version` 3 → 4；`executors` 列尾多出 `disabled_reason`；`idx_executors_enabled` 建好；13 张表数量不变 |
| 存量数据是否保留 | 升级前 `executors` 里放一行 `enabled=1` | 升级后该行仍在，`enabled` 仍为 1，`disabled_reason` 为 `NULL` |
| 迁移幂等 | 同一个库再打开一次 | `schema_version` 仍为 4，无报错、无重复写入 |

### 4.4 备份格式与包的两处硬约束

- `BACKUP_FORMAT_VERSION` 3 → 4：新导出的操作包与审计包 manifest 都写 `backup_format: 4`（`bigfish/backup.py:122`、`bigfish/backup.py:185`）。
- 恢复一个**更高** schema 的备份到低版本程序会被 `_verify_version`（`bigfish/backup.py:230-235`）在 `inspect_bundle` 阶段拒绝：「备份 schema 版本 N 高于当前程序支持的 M，请先升级程序，禁止把高版本库恢复进低版本程序」。这条检查发生在实际替换文件之前，所以拒绝不会留下半截状态。

## 5. 回滚步骤

完整手册已落在 **`docs/ROLLBACK-v0.1.1-rc5.md`**（404 行，工作区未提交）。本节只留硬约束与最小步骤；逐步命令、两条回滚路线、回滚后必做三项一律以手册为准。

### 5.1 三条硬约束

1. **schema 4 的库不能被低版本程序打开，也不能降级。** 本轮实测：把库升到 schema 4 后，用 `SCHEMA_VERSION` 回退到 rc4 的值（3）的读取路径去打开，得到
   `数据库 schema 版本 4 高于本程序支持的 3，拒绝打开（升级程序，不要降级数据库）`（`bigfish/db.py:254`）。
   拒绝发生在任何写入之前：库文件 sha256 与 mtime 前后一致，目录里没有多出任何文件，`PRAGMA journal_mode` 仍是 `wal`。
   所以「降级代码」这件事在 rc5 之后**必须配一份 schema 3 的库副本**，要么就别降。
2. **密钥轮换过就不能回滚**。`_heal_key` 一旦走 `rotated`，旧密钥签的全部票作废，代码回滚不会把票恢复回来。
3. **恢复过的执行器必须重新 `reenable`**。R1 把 `executors` 全禁用并替换了 `token_hash`，回滚代码也一样要继续 `reenable`（它会再换一次 token）。不要试图直接 `UPDATE executors SET enabled=1`——那等于让泄露过的那份 token 复活。

### 5.2 最小回滚步骤（代码层，数据库不动）

```bash
# 1) 先备份，别跳过（子命令是 export，不是 backup）
cd /home/dsh/bigfish-council
.venv/bin/python -m bigfish.cli export /tmp/rc5-before-rollback.bfc

# 2) 停掉一切在写库的进程（headless / GUI）。CLI restore 的端口探测会替你挡一次，
#    但回滚代码本身不做这件事。确认没有监听：ss -lntp | grep 3096

# 3) 退到 rc4 发布点。必须整棵树切换，不要用 git checkout 72e8535 -- bigfish tools：
#    那种局部回退会把 rc5 的 7 个测试文件留在树里，它们 import 的 reenable_executor /
#    ReceiptLog / dispatch_id 在 rc4 代码里都不存在（已核对：两者在 72e8535 中为 0 处）。
git stash -u                       # 若有未提交改动（含 docs/ 下后续修订）会一起收起，
                                   # 回滚后要取回：git stash pop
git checkout 72e8535               # detached HEAD，工作树整体回到 rc4

# 4) 换回一份 schema 3 的库副本（没有副本就只能停在 rc5，见 5.1 第 1 条）

# 5) 验证
.venv/bin/python -m unittest discover -s tests -t .        # 期望回到 162 项
.venv/bin/python tools/selfcheck.py

# 6) 要回到 rc5 分支时
git checkout fix/v0.1.1-rc4-hardening
```

### 5.3 如果只是想撤销一次恢复

恢复失败不会动原库：`restore_bundle` 在替换前先 `db.snapshot(...prerestore)`（`bigfish/backup.py:555-560`），返回值里 `prerestore_path` 指向它。把这份副本换回 `council.db`（先删 `-wal` / `-shm`）即可回到恢复前状态。密钥文件同理由 `key_backup_path` 给出——库与密钥必须成对回退，`bigfish/backup.py:612-655` 的失败路径就是按这个原则写的。

## 6. 验证矩阵

平台 A：Debian 12 / Linux 5.15.0-187-generic / Python 3.11.2 / SQLite 3.40.1。
下表全部按 HEAD `b8f6de1` 的 244 项实测给数（`Ran 244 tests in 39.178s / OK`，日志 `/home/dsh/bfc-rc5-logs/unittest-linux.txt`）。收尾前的 `adc48a3` 只有 243 项，不再单列。
所有 GUI 相关命令必须带 `LD_LIBRARY_PATH=/home/dsh/.local/libbfc`（本机缺 `libGL.so.1`）与 `QT_QPA_PLATFORM=offscreen`。

| 项目 | 命令 | 平台 A | 原始输出 | 平台 B |
| --- | --- | --- | --- | --- |
| 全量单元测试 | `python -m unittest discover -s tests -t .` | **Ran 244 tests / OK**，39.178s，退出码 0（`b8f6de1`） | `/home/dsh/bfc-rc5-logs/unittest-linux.txt`（930 字节，`FAIL:` / `ERROR:` 均 0 行） | 未跑 |
| 自检 | `python tools/selfcheck.py` | **通过 16 项，失败 0 项**，退出码 0（`b8f6de1` 现跑复核） | `/home/dsh/bfc-rc5-logs/selfcheck.txt` | 未跑 |
| GUI 冒烟 | `python tools/gui_smoke.py`（offscreen） | **通过 9 项，失败 0 项**，退出码 0（`b8f6de1` 现跑复核） | `/home/dsh/bfc-rc5-logs/gui_smoke.txt` | 未跑 |
| 静态检查 | `python -m pyflakes bigfish tests tools` | 零输出，退出码 0（`b8f6de1` 现跑复核） | `/home/dsh/bfc-rc5-logs/pyflakes.txt`（0 行） | 未跑 |
| 字节码编译 | `python -m compileall -q bigfish tests tools` | 零输出，退出码 0（`b8f6de1` 现跑复核） | `/home/dsh/bfc-rc5-logs/compileall.txt`（0 行） | 未跑 |
| 回环 e2e | `tools/e2e_loopback.py` | 本批未跑 | — | 未跑 |
| 跨机 Tailscale 闭环 | `tools/remote_check.py` | 本批未跑 | — | 未跑 |
| GUI 真窗口人工验收 | 人工 | 未做 | — | 未做 |
| 类型检查（mypy / pyright） | 未引入 | 未做 | — | 未做 |
| 长稳 soak | — | 未做 | — | 未做 |

按审计项定位测试文件：

| 审计项 | 测试文件 |
| --- | --- |
| R1 | `tests/test_v011_rc5_restore_authority.py`（+ `tests/test_v011_rc3.py` 的 2 项语义更新） |
| R2 / R3 / R4 / R6 | `tests/test_v011_rc5_review_audit.py` |
| R5 | `tests/test_v011_rc5_receipts.py` + `tests/test_v011_rc5_dispatch_id.py` |
| R7 | `tests/test_v011_rc5_proxy.py`（依赖本机 `node`，缺失时整类跳过） |
| R8 | `tests/test_v011_rc5_restore_authority.py` + `tests/test_v011_rc5_cli_guard.py` |
| R9 | `tests/test_v011_rc5_key_heal.py` |

### 6.1 已知噪音（不是失败）

三条已知输出，均为环境性，出现时不应判为回归：

1. `Fontconfig error: Cannot load default config file: No such file: (null)` —— 精简容器里没有 fontconfig 配置，offscreen Qt 也会打印。
2. `StarletteDeprecationWarning` —— 依赖侧弃用提示（`httpx` 与 `starlette.testclient`），本项目未用 `httpx2`。
3. `This plugin does not support propagateSizeHints()` —— offscreen 平台的窗口提示不支持。

## 7. 需 human_owner 授权才能做的事

以下四件本轮**一律没有执行**，代码与文档就绪，等授权。标注里的「需授权」对应本项目的人工所有者角色 `human_owner`，执行器角色无权触发其中任何一项。

| 编号 | 事项 | 需授权的原因 | 不做的现状 | 执行前必须确认 |
| --- | --- | --- | --- | --- |
| H1 | 把 rc5 部署到公司机 3096 现役服务 | 覆盖正在跑的 headless 进程、重启服务、让执行器重连，属变更现役服务 | 现役仍是 rc3（`832f699`）。rc4 与 rc5 都停在发布点、从未上过生产 | （1）先在平台 B 跑完全量（`b8f6de1` 是 244 项）+ 16 自检 + 9 冒烟；（2）备份 `council.db` 与 `secrets/ticket.key`（后者不进备份包，必须单独拷）；（3）关旧进程再上新版，**不要并行混跑**；（4）上线后逐个 `reenable`（若期间做过恢复） |
| H2 | 导入 bigfish-council MCP 连接 | 对外可见的配置变更，DSH 从此能领取已批准任务 | `dsh-mcp-bigfish.json` 里的 `Authorization` 仍是占位符（`Bearer <把 executor-add 打印出来的 bfc_ex_... 填在这里>`），DSH 现在没有连接本服务的能力；文件里的 url 是 `http://127.0.0.1:3098/mcp`（跳板端口，不是 API 的 3096） | 先跑 `executor-add` 生成真实 `bfc_ex_...` 并确认吊销方式；确认 `tools/list` 只有五个工具（`council_list_tasks` / `council_claim_task` / `council_heartbeat` / `council_submit_result` / `council_task_status`），**不含审批**；确认 3098 上确实是 `bfc-proxy.mjs` 而不是别的东西 |
| H3 | 注册开机自启 | 注册 Windows 计划任务（`Register-ScheduledTask`，`AtLogOn` 触发），属系统级持久化变更 | 没有任何自启项；机器重启后服务不会自己起来 | 先确认 `install-task.ps1` 的三处实参：任务名 `BigFishCouncil`、可执行文件 `.venv\Scripts\pythonw.exe`、数据目录 `%LOCALAPPDATA%\BigFishCouncil`，端口走默认 `127.0.0.1:3096`；准备卸载命令 `Unregister-ScheduledTask -TaskName BigFishCouncil -Confirm:$false` |
| H4 | GUI 真窗口验收 | 需要人在真实显示器上看着点一遍，机器无法代劳 | `tools/gui_smoke.py` 只证明构造与信号连接（offscreen 9 项），DPI、焦点顺序、托盘、关窗确认框、恢复对话框都没人看过 | 在 Windows 真显示器上逐页过：五页切换、恢复流程的 `pause` / `resume`（本轮完全没有自动化覆盖）、审批对话框、托盘菜单 |

四件都不做也不影响「代码层面可发布」这个结论；但 H1 之前 H4 与平台 B 必须补完。

## 8. 未完成事项与下一步建议

### 8.1 明确未完成

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 平台 B（Windows 11 / Python 3.14.6）全量验证 | 未跑 | 244 项（`b8f6de1`）、16 自检、9 冒烟全部只在 Linux 上跑过。`msvcrt.locking`（`bigfish/backup.py:367`）、`os.replace` 撞文件占用、NTFS ACL 三条路径只有代码 |
| GUI 真窗口验收 | 未做 | 见 H4 |
| GUI 恢复路径自动化 | 无用例 | `pause_services` / `resume_services`（`bigfish/gui.py:1222`、`bigfish/gui.py:1243`）只有代码审读与 offscreen 冒烟间接覆盖 |
| 看门狗与维护模式的交界 | 无并发用例 | 只测了「进入维护后写入被拒」；「看门狗恰好在进入维护那一瞬扫描」没有专项测试 |
| `docs/ROLLBACK-v0.1.1-rc5.md` | 已生成、待提交 | 404 行 / 20849 字节；schema 4 不可降级、`restore_epoch` 不能退回、换库后必须吊销全部活票这三条硬约束与两条回滚路线都已写全 |
| `docs/FIX-MATRIX-v0.1.1-rc5.md` / `docs/DIFF-SUMMARY-v0.1.1-rc5.md` | 已提交 | 182 行 / 1332 行，均在 `b8f6de1` 里 |
| `docs/MIGRATION-v0.1.1-rc5.md` | 已生成、待提交 | 430 行 / 25614 字节；MIGRATIONS[4] 两条 SQL 原文、迁移失败回滚实测、key 指纹三分支、升级库与全新库的索引数量差异都记在里面 |
| `bigfish/backup.py` / `tests/test_v011_rc5_restore_authority.py` 的 tickets_revoked 改动 | 已提交 | 在 `b8f6de1` 里；`tickets_revoked` 改成真实 `rowcount` 并透传，配 1 条用例，HEAD 现跑 244 项 OK |
| `AUDIT.md` / `CHANGELOG.md` / `README.md` / 六份 rc5 文档 | 已提交 8 份、待提交 2 份 | 前 8 份在 `b8f6de1` 里；`MIGRATION` / `ROLLBACK` 两个 rc5 版本已写盘但尚未 git add |
| 打 tag | **未打** | 本批四个提交（`4281114` / `a4f0641` / `adc48a3` / `b8f6de1`），`git tag` 输出为空，rc5 没有发布标签 |
| 回环 e2e / 跨机 Tailscale 闭环 | 本批未跑 | rc3 期间打通过一次，rc4、rc5 都没重跑 |
| mypy / pyright | 未引入 | `pyflakes` + `compileall` 不是类型检查。rc5 改了大量函数签名（`reenable_executor`、`_heal_key`、`restore_bundle` 的 `before_replace`） |
| 24h soak | 未做 | 长稳、文件增长、内存驻留全未观测 |
| 打包 exe | 未做 | `build.ps1` 未执行 |
| 恢复的故障注入（`os.replace` 之间 SIGKILL） | 未做 | 可在临时目录做，不碰真实数据 |

### 8.2 下一步建议

| 优先级 | 事项 | 为什么排这里 | 前置 |
| --- | --- | --- | --- |
| P0 | 平台 B 跑全量（244 项）+ 16 自检 + 9 冒烟 | 这是「可发布」结论唯一缺的一块，其余都已实证 | Windows 建 venv 装 `requirements.txt`；设 `PYTHONIOENCODING=utf-8` 并重定向到文件（rc3 踩过 GBK 转码） |
| P0 | GUI 真窗口人工验收（H4） | 本批新增了恢复期的 pause / resume 与两条对话框文案，一次都没人看过 | 真显示器 |
| P1 | 提交两份 rc5 手册（`docs/MIGRATION-v0.1.1-rc5.md`、`docs/ROLLBACK-v0.1.1-rc5.md`） | schema 4 不可降级的唯一操作依据现在只在工作区 | 与其余五份修订一次性提交 |
| P1 | 打 rc5 发布 tag | 回滚与审计都要指一个确定标签，现在只有裸提交号 `b8f6de1` | 先过平台 B 与 H4，再 `git tag -a v0.1.1-rc5 b8f6de1` |
| P1 | 提交工作区里剩下的文档修订 | `docs/DELIVERY` / `TEST-REPORT` / `TESTLOG` / `FIX-MATRIX` / `DIFF-SUMMARY` 五份修订 + 两份新手册，`git status` 显示 5 M + 2 ?? | 定稿后一次性提交，避免多次 amend |
| P1 | 提交工作区里剩下的文档修订 | `docs/DELIVERY` / `TEST-REPORT` / `TESTLOG` / `FIX-MATRIX` / `DIFF-SUMMARY` 五份在 `b8f6de1` 之后还有措辞与数字修订（`git status` 显示 `M`） | 定稿后一次性提交，避免多次 amend |
| P1 | 给 GUI 恢复路径补一条自动化用例 | R8 的 GUI 那半边目前只有代码审读 | 可在 offscreen 下用假 `ApiServer` 注入验证 pause / resume 幂等 |
| P2 | 补「看门狗 vs 维护模式」并发用例 | 边界上最容易出静默问题 | 需要能控制扫描时机的注入点 |
| P2 | 重跑回环 e2e 与跨机 Tailscale 闭环 | 网络层本批没动，但也没验；上一轮通过是 rc3 | 对端先上 rc5 |
| P2 | 引入 mypy 或 pyright | 本批改了多个跨文件签名，`pyflakes` 查不出类型错误 | 先定要不要加依赖 |
| P3 | 24h soak | 长稳全未观测；本批新增维护模式与收据落盘，都带 I/O 行为 | 需要一个空转的稳定环境 |
| P3 | 恢复故障注入（`os.replace` 之间 SIGKILL） | `keep_maintenance` 这条分支只有代码审读（`bigfish/backup.py:648-654`） | 临时目录即可，人工核对 |
| P4 | 实现 exe / apk 形态 | `ARCHITECTURE-FUTURE.md` 仍是草案 | 前几项先落地 |

---

一句话收尾：Linux 上这 244 项是把九项审计问题钉死在地上的证据；Windows 上还什么都没有，所以它现在是一份可以被审计的交付物，还不是一次可以上线的发布。
