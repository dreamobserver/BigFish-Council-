# BigFish Council 安全复核 — v0.1.1-rc5

| 项 | 值 |
| --- | --- |
| 版本 | `0.1.1-rc5`（`bigfish/__init__.py:6`） |
| 分支 / HEAD | `fix/v0.1.1-rc4-hardening` / `b8f6de1` |
| 对比基线 | rc4 发布点 `72e8535` |
| 差异规模 | 20 文件 +2276/-61（代码侧 `72e8535..b8f6de1`） |
| 库结构 | `SCHEMA_VERSION = 4`（`bigfish/db.py:22`）、`BACKUP_FORMAT_VERSION = 4`（`bigfish/db.py:23`） |
| 全量测试 | `Ran 244 tests` 约 38s / OK（rc4 基线 `Ran 162 tests` 26.6s） |
| 自检 | `tools/selfcheck.py` 通过 16 项、失败 0 项；`tools/gui_smoke.py` 通过 9 项、失败 0 项 |
| 静态检查 | `pyflakes bigfish tests tools` 零输出；`compileall` RC=0 |
| 平台 | Linux（offscreen）实测；Windows 本批次未跑 |

本文只记录 rc5 这一批修复的安全语义。威胁模型编号沿用 rc4 文档的 T1–T8，不新增编号；rc4 已确认的结论若不涉及本批改动，本文不再重复论证。

---

## 一、本批修复的九个审计项

第三方审计在 rc5 之前提出九项，逐条复核为真后修复。九项的共同特征是：**单看每一处都是小改动，但它们在真实事故链上是串联的**——恢复旧快照（R1）会把已作废的凭据复活，而没有排空（R8）会让另一实例继续往旧 inode 写，收据缺失（R5）又让服务端在事后无法对账。因此本文按「事故链」而不是按代码位置组织。

| 编号 | 一句话 | 主要落点 |
| --- | --- | --- |
| R1 | 恢复后授权没有作废，旧凭据、旧票、旧派工全部复活 | `bigfish/backup.py:319`、`bigfish/core.py:246`、`bigfish/core.py:843` |
| R2 | 双模型通过时 `task.review_passed` / `task.pending_approval` 事件永不落库 | `bigfish/core.py:597` |
| R3 | 撤一条普通评论就能把已批准任务打回、把运行中任务打成 BLOCKED | `bigfish/core.py:497` |
| R4 | 心跳/提交的时间戳取在等锁之前，写出比现在更早的过期时间 | `bigfish/core.py:932`、`bigfish/core.py:998` |
| R5 | 执行端没有「开工收据」，服务端库一恢复就无从对账 | `bigfish/executor_client.py:41` |
| R6 | 审计导出用列黑名单，未来新增的敏感列会自动进包 | `bigfish/backup.py:56` |
| R7 | `bfc-proxy` 的监听地址直接取环境变量，可把跳板绑到 `0.0.0.0` | `tools/bfc-proxy.mjs:39` |
| R8 | 恢复替换期间没有排空，旧连接继续写旧 inode / Windows 替换失败 | `bigfish/db.py:298`、`bigfish/backup.py:462` |
| R9 | 密钥自愈换了钥却没吊销旧票，旧票照样能认证 | `bigfish/backup.py:693` |

---

## 二、威胁模型现状（T1–T8）

T1–T8 的表述沿用 rc4 文档，逐字不改；「rc5 变化」列只写本批实际发生的动作。

| 编号 | 威胁 | rc4 状态 | rc5 变化 |
| --- | --- | --- | --- |
| T1 | 库或备份包泄露，明文执行票外流 | 票只存 `token_hash`，库里无明文 | 审计包改列白名单（R6），泄露面进一步收窄；但库文件本身仍未加密 |
| T2 | 执行器伪造审查票 | 票由服务端签发、按 `(执行器, 任务, 版本, 幂等键)` 确定性派生 | 无变化 |
| T3 | 执行器自己批准 | 审批只认人类所有者角色 | 无变化 |
| T4 | 审批跨版本降级 | 审批绑定 `current_version`，改提示词即重审 | 无变化 |
| T5 | 恢复把库搞坏 | 有 `.prerestore` 回滚副本 | 新增排空（R8）、恢复后授权作废（R1）、恢复失败保留维护模式 |
| T6 | 长期任务被误判死（看门狗） | 看门狗容错扫描异常 | 心跳时间戳移入事务（R4），续租不再写出过去的过期时间 |
| T7 | 网络暴露到公网 | 服务默认只绑回环 | 代理进程加回环闸门（R7），非回环地址直接退出 |
| T8 | 模型身份可被密码学证明 | 明确不实现 | 无变化（仍不做） |

---

## 三、R1–R9 逐条

### R1 恢复后授权没有作废

**攻击场景**

人类所有者发现库被改坏，从一份较早的 `.bfc` 备份恢复。恢复动作把 `tasks`、`tickets`、`executors`、`idempotency` 全部换回快照里的样子。

**修复前会发生什么**

「恢复旧快照」在语义上等于「把过去发出去的所有凭据一起复活」：

1. 快照里 `executors.token_hash` 是当时那批 token 的哈希。恢复后这些哈希原样回到库里，而执行端手里的 token 一直没变——于是**认证重新通过**。当初因为怀疑泄露而作废的凭据，恢复一次就全回来了。
2. 快照里 `tickets` 的 `revoked_at` 是 `NULL`（快照是在作废之前打的）。旧票重新可领取、可心跳。
3. 快照里状态为 `APPROVED` 的任务重新变成「等待派工」，执行器可以直接领走。而 `_block_inflight` 当时只处理在途任务（`DISPATCHED`/`RUNNING`/`CANCELLING`），对 `APPROVED` 完全没有动作。
4. `idempotency` 表一起回来了。执行端用同一个 `idem_key` 重试，命中旧的 claim 缓存，把一张**已被吊销的票**当作正常响应发回去——`_replay_claim` 当时不校验取消、归属、状态、吊销，只查幂等缓存。

结果就是：所有者以为「恢复了干净状态」，实际上恢复了「一份过去发出去的授权清单」。

**修复后如何被挡住**

- `_block_inflight`（`bigfish/backup.py:319`）扩展为五步，全部在同一事务里完成：
  1. 在途 `DISPATCHED`/`RUNNING`/`CANCELLING` → `BLOCKED`，原因「数据库已从备份恢复，执行授权全部作废，需人工确认副作用后重新审批」；
  2. `APPROVED` → `BLOCKED`，原因含「冻结」字样：「数据库已从备份恢复，该任务的派工许可已冻结；人工核对后需重新提交审批才可再次派工」（`bigfish/backup.py:344-351`）；
  3. `UPDATE tickets SET revoked_at=? WHERE revoked_at IS NULL`（`bigfish/backup.py:352`）—— 全部活票作废；
  4. `UPDATE executors SET enabled=0, disabled_reason=? WHERE enabled=1`（`bigfish/backup.py:354-356`），`DISABLED_REASON_RESTORE` 定义在 `bigfish/backup.py:72`：不删行，保留审计证据；
  5. 清 `scope IN ('claim','heartbeat','submit')` 的幂等缓存（`bigfish/backup.py:357-358`），重放路径失去缓存来源。

- 认证侧把「查得到行」和「能用」拆开。`authenticate`（`bigfish/core.py:188`）与 `authenticate_any`（`bigfish/core.py:210`）在 `token_hash` 命中但 `enabled=0` 时抛 `_disabled_message`（`bigfish/core.py:241`）：「该身份已被禁用：{原因}；必须由人类所有者重新授权并更换凭据」。不是笼统的「令牌无效」——运维需要这句文案才知道下一步是 reenable 而不是重新配置。

- **为什么 reenable 必须同时换 token**：只把 `enabled` 翻回 1，等于让快照里那份 `token_hash` 继续有效，而执行端手里的旧 token 正是它的原像——**旧凭据原地复活**。所以 `reenable_executor`（`bigfish/core.py:246`）在事务内先 `new_token(prefix)`，再 `UPDATE executors SET enabled=1, disabled_reason=NULL, token_hash=?, last_seen=NULL`：`token_hash` 被物理替换，旧 token 在库里再也找不到对应的行。新 token 由 CLI `reenable`（`bigfish/cli.py:57`、`bigfish/cli.py:223`）一次性打印给人类，不入日志。

- `_replay_claim`（`bigfish/core.py:843`）重写为顺序校验：先查 `cancel_requested_at` / `control.cancel_requested`，再比 `executor_id` 归属，再查 `status IN (DISPATCHED,RUNNING,CANCELLING)`，再比 `current_version`，最后取 `tickets` 行交给 `_check_ticket_row`（`bigfish/core.py:900`）校验 `revoked_at` / `epoch` / `expires_at` / `max_expires_at`。任何一项不过就拒绝，不再「有缓存就照发」。

**证据**

- `bigfish/backup.py:319` `_block_inflight`；`bigfish/backup.py:352`、`bigfish/backup.py:354`、`bigfish/backup.py:357`；`bigfish/backup.py:411` `_prepare_staging` 在 staging 上一次性完成 epoch+1、作废、写 `db.restored` 审计、写 `key_sha256`。
- `bigfish/core.py:241` `_disabled_message`；`bigfish/core.py:246` `reenable_executor`；`bigfish/core.py:843` `_replay_claim`；`bigfish/core.py:900` `_check_ticket_row`。
- `bigfish/db.py:97` `disabled_reason TEXT`；`bigfish/db.py:207` 迁移 4。
- 测试：`tests/test_v011_rc5_restore_authority.py` 的 `TestRestoreDisablesIdentities::test_restore_disables_every_executor`、`::test_restored_access_token_no_longer_authenticates`、`::test_reviewer_token_also_rejected_after_restore`；`TestReenableRotatesCredential::test_reenable_rotates_token_and_old_one_dies`（断言新旧 token 不相等且旧 token 物理不可用）、`::test_reenable_keeps_role_and_model_and_writes_audit`、`::test_disabled_reason_cleared_after_reenable`；`TestRestoreFreezesApproved::test_approved_task_frozen_not_claimable`、`::test_inflight_task_blocked_and_ticket_revoked`；`TestClaimReplayAfterRestore::test_replay_rejected_after_restore`、`::test_replay_rejected_after_cancel`。语义更新：`tests/test_v011_rc3.py::test_restore_older_schema_migrates`（现断言 `APPROVED`→`BLOCKED` 且 `blocked_reason` 含「冻结」）、`::test_revoke_and_submit_race`（现断言「该身份已被禁用」+「重新授权」）。

---

### R2 `_recompute_review` 提前 return 吞掉审计事件

**攻击场景**

两个审查模型都投了通过票，任务从 `IN_REVIEW` 推进到 `PENDING_APPROVAL`。

**修复前会发生什么**

`_recompute_review` 的 `all_approved` 分支在更新完任务状态后直接 `return`，跳过了函数末尾统一落库的 `log_event` 循环。于是 `task.review_passed` 和 `task.pending_approval` 两条事件永远不写入 `events` 表。后果不是「显示错误」，而是**审计链路断裂**：事后只看事件表的人会认为这个任务从来没有通过审查，也就无法解释它为什么会被批准、被派工。这种缺失在事故复盘中比一条错误记录更难处理，因为它没有任何痕迹。

**修复后如何被挡住**

`bigfish/core.py:597` 的 `_recompute_review` 删掉 `all_approved` 分支里的 `return`（`bigfish/core.py:619` 附近的注释写明了这个历史缺陷），把未通过分支改成 `elif`，让控制流无论如何都走到 `bigfish/core.py:639-640` 的 `for kind, detail in events: self.log_event(...)`。事件收集与事件落库重新变成「一条必经路径」，而不是「某几个分支才走的分支」。

**证据**

- `bigfish/core.py:597` `_recompute_review`、`bigfish/core.py:619`、`bigfish/core.py:623`、`bigfish/core.py:639-640`。
- 测试：`tests/test_v011_rc5_review_audit.py::TestReviewPassedEvents::test_events_written_on_all_approved`、`::test_status_still_reaches_pending_approval`、`::test_events_written_when_votes_cast_before_review`（先投票后送审的路径同样不能丢事件）。

---

### R3 `revoke_review` 不判 kind

**攻击场景**

一个审查模型对某任务留了一条**普通评论**（`kind=comment`），随后撤回它。

**修复前会发生什么**

`revoke_review` 只看 `reviews` 行是否存在，不看 `kind`。撤回一条评论会触发与撤回投票完全相同的降级逻辑：已批准（`APPROVED`）的任务被打回 `IN_REVIEW`，正在执行（`DISPATCHED`/`RUNNING`/`CANCELLING`）的任务被打成 `BLOCKED`。这等于**用评论行使了否决权**：任何能留评论的角色，都能通过「留一条评论再撤回」让一条正在执行的任务停摆，而这个动作在审计里看起来只是撤回了一条文字。

**修复后如何被挡住**

`bigfish/core.py:497` 的 `revoke_review` 先读 `kind`（`bigfish/core.py:513`，老库缺列时按 `vote` 处理保持向后兼容），若 `kind != "vote"` 则把 `task_row` 置空（`bigfish/core.py:521`），`bigfish/core.py:523-538` 的降级逻辑只在 `vote` 时才执行。撤回评论仍然会写审计、仍然把评论标记为已撤回，只是不再触碰任务状态。同时 `add_review`（`bigfish/core.py:464`）本来就限定 `kind` 只能是 `comment` 或 `vote`，避免出现第三种语义。

**证据**

- `bigfish/core.py:497`、`bigfish/core.py:513`、`bigfish/core.py:521`、`bigfish/core.py:523-538`、`bigfish/core.py:464`。
- 测试：`tests/test_v011_rc5_review_audit.py::TestRevokeComment::test_revoke_comment_does_not_touch_approved_task`、`::test_revoke_comment_does_not_block_running_task`（并断言票据仍然可用、心跳仍能推进到 `RUNNING`）；反向保护 `::test_revoke_vote_still_invalidates`（撤真票必须照旧生效，防止本项修过头）。

---

### R4 心跳/提交的时间戳取在事务之外

**攻击场景**

多个执行器同时心跳，SQLite 写锁竞争，某个事务在 `busy_timeout`（5000ms）里排队等待。

**修复前会发生什么**

`heartbeat` 与 `submit_result` 在进入 `with self.db.tx() as c:` **之前**就调用了 `now()`。等锁期间这个 `t` 已经固定，等真正拿到锁、写入数据库时，它已经比「现在」早了最多数秒。续租公式 `new_exp = t + heartbeat_lease` 于是算出一个**比实际写入时刻更早的** `expires_at`。任务越繁忙、锁竞争越激烈，票据的有效期被侵蚀得越多。这是一种典型的「负载越高越容易误判」的失效方式：看门狗看到票过期，把正在正常执行的任务判死。

**修复后如何被挡住**

把 `t = now()` 移到事务内第一行：`bigfish/core.py:938-942`（`heartbeat`）、`bigfish/core.py:1009`（`submit_result`）。续租仍用 `min(t + heartbeat_lease, max_expires_at)`（`bigfish/core.py:973-976`），但因为 `t` 是拿到锁之后才取的，`expires_at` 必然晚于写入时刻。副作用是 `renewed_at` 也变成真实续租时刻，而不是「排到队之前」。

**证据**

- `bigfish/core.py:932` `heartbeat`、`bigfish/core.py:938-942`、`bigfish/core.py:973-976`、`bigfish/core.py:998` `submit_result`、`bigfish/core.py:1009`。
- 测试：`tests/test_v011_rc5_review_audit.py::TestTimestampInsideTransaction::test_heartbeat_lease_is_in_the_future`（断言 `expires_at > renewed_at`，且返回的 `lease_expires_at` 与库内一致）、`::test_heartbeat_keeps_lease_alive_repeatedly`（连续五次续租 `expires_at` 单调不减）。

---

### R5 执行端没有「开工收据」

**攻击场景**

执行器领到任务、开始跑、写到一半机器崩了。或者：服务端库被恢复成几小时前的快照，而执行器那边其实已经执行完了。

**修复前会发生什么**

所有执行痕迹只存在于服务端库里。一旦库回退，「这个派工到底跑没跑、跑到哪一步」就永久丢失——执行器手里只有一张票，票只能证明「它曾经被授权」，不能证明「它做过什么」。这是事故善后里最危险的一种空白：**副作用可能已经发生，但没有任何一方能拿出记录**。而恢复动作（R1）恰好会作废所有票，让判断依据同时消失。

**修复后如何被挡住**

执行端新增 `ReceiptLog`（`bigfish/executor_client.py:41`），本地 JSONL 追加写：

- 事件白名单 `EVENTS = ("claimed","started","finished","failed","blocked","cancelled","uncertain")`（`bigfish/executor_client.py:54`），终态集合 `CLOSED_EVENTS`（`bigfish/executor_client.py:57`）；
- `record()`（`bigfish/executor_client.py:68`）每写一行就 `f.flush(); os.fsync(f.fileno())`（`bigfish/executor_client.py:94-95`，顺序不能反，先 flush 再 fsync 才落盘）——收据的价值全在「断电也在」，缓冲里的收据等于没收据；
- `read_all()`（`bigfish/executor_client.py:98`）坏行跳过而不是整份报错，崩在半行也不能让整本账作废；
- `state_of()`（`bigfish/executor_client.py:123`）给出四级判定「已完成／结果未知／未开工／未知」；
- `summary()`（`bigfish/executor_client.py:159`）按 `dispatch_id` 去重统计。

收据要和服务端的派工号对上，所以服务端下发 `dispatch_id`：`claim` 返回 `"dispatch_id": ticket_id`（`bigfish/core.py:838`），`_replay_claim` 返回缓存里的 `ticket_id`（`bigfish/core.py:884`）——**幂等重放派工号不变**（同一轮执行不该被记成两次），而重新批准后的再领取会换新票号，也就换了新派工号（多轮执行可区分）。执行端 `dispatch_id_of()`（`bigfish/executor_client.py:208`）对老服务端回退到 `ticket_id`。

收据开关三态用哨兵 `_UNSET`（`bigfish/executor_client.py:176`）区分：不传则读环境变量 `BFC_RECEIPTS`；显式传 `None`/`False` 是内存 no-op（测试与一次性脚本绝不落文件）；传路径或 `ReceiptLog` 实例则落盘。`reconcile()`（`bigfish/executor_client.py:337`）列出「结果未知」的 `dispatch_id`，供人工对账。

**证据**

- `bigfish/executor_client.py:41`、`:54`、`:57`、`:68`、`:94-95`、`:98`、`:123`、`:159`、`:176`、`:208`、`:337`。
- `bigfish/core.py:838`（claim 返回 dispatch_id）、`bigfish/core.py:884`（重放返回同一 dispatch_id）。
- 测试：`tests/test_v011_rc5_receipts.py` 的 `TestReceiptLog`（6 项）、`TestStateOf`（6 项）、`TestDurability::test_record_is_flushed_to_disk_immediately`、`::test_reader_in_new_process_sees_rows`（新进程能读到，证明真落盘）、`TestRunOnceReceipts::test_run_once_writes_claimed_started_finished_in_order`、`::test_run_once_records_uncertain_and_reraises`、`::test_reconcile_lists_unknown_inflight`、`::test_reconcile_without_any_receipt`、`::test_dispatch_id_falls_back_to_ticket_id`、`TestClientWiring`（5 项）；`tests/test_v011_rc5_dispatch_id.py::TestDispatchId::test_claim_returns_dispatch_id`、`::test_replay_returns_same_dispatch_id`、`::test_redispatch_gets_new_dispatch_id`。

---

### R6 审计导出用列黑名单

**攻击场景**

未来某个版本给 `tasks` 表加一列 `executor_token`（用于排障），然后导出审计包交给第三方。

**修复前会发生什么**

导出逻辑用 `AUDIT_DENY_COLUMNS` 做黑名单：`SELECT *` 之后把命中的敏感列剔掉。黑名单的失败方向是**漏一列 = 漏一次凭据**：新加的列不在名单里，就自动进了包。这个问题不会在测试里自己暴露，因为「包里多了一列」看起来完全正常——除非有人正好知道那列是敏感的。审计导出的东西是要外发的，一次失误就是永久外泄。

**修复后如何被挡住**

改成列白名单 `AUDIT_COLUMNS`（`bigfish/backup.py:56`），六张表逐列显式列出（`tasks` 13 列、`task_versions` 5 列、`reviews` 11 列、`approvals` 7 列、`events` 5 列、`audit` 6 列）。导出循环（`bigfish/backup.py:165-175`）只取 `allow = set(AUDIT_COLUMNS.get(table, ()))` 内的列，其余进 `skipped` 并计入 `dropped`。

**失败方向的差异**是本项的核心：黑名单漏一列 = 漏一次凭据（外泄）；白名单漏一列 = 少导出数据（外发的包不完整，人工一看就发现，可重导）。前者静默、不可逆，后者显式、可重做。`AUDIT_DENY_COLUMNS`（`bigfish/backup.py:69`）保留下来仅为兼容旧引用，不再是判定依据。

manifest 也从「声称」变成「可核对」：`redaction = {policy: "allowlist", allowed_columns, dropped_tables: [executors, tickets, idempotency, runs, artifacts, meta], dropped_columns, string_scrub}`（`bigfish/backup.py:192-199`）。拿着包的人可以自己比对「包里出现的每个列名是否都在 `allowed_columns` 里」，不需要相信实现。

**证据**

- `bigfish/backup.py:50` `AUDIT_TABLES`；`bigfish/backup.py:56` `AUDIT_COLUMNS`；`bigfish/backup.py:69`；`bigfish/backup.py:165-175`；`bigfish/backup.py:192-199`。
- 测试：`tests/test_v011_rc5_review_audit.py::TestAuditBundleAllowlist::test_every_exported_column_is_on_the_allowlist`（逐个 JSON 逐个列名比对白名单）、`::test_no_sensitive_column_names_leak`（不看注释不看 manifest，直接把包里所有键名拉出来查 `token/hash/ticket/secret/key/password`）、`::test_manifest_declares_allowlist_policy`、`::test_dropped_columns_reported_when_present`、`::test_prompt_body_scrubbed_of_ticket`（往事件里塞一张真票，断言包里既看不到明文票、又能看到 `REDACTED`）。

---

### R7 `bfc-proxy` 监听地址无回环校验

**攻击场景**

`tools/bfc-proxy.mjs` 被脚本或快捷方式拉起，环境里 `BFC_PROXY_LISTEN_HOST=0.0.0.0`（可能是复制粘贴、可能是别的工具留下的）。

**修复前会发生什么**

`LISTEN_HOST` 直接取自环境变量，没有任何校验，`server.listen` 照单全收。这就把本该只在本机工作的 MCP 跳板绑到了所有网卡上，正好对应 T7：局域网内任何主机都能连上来，并在认证层之外先摸到接口。更糟的是它发生在一个「辅助脚本」里，使用者根本不会去审计它。

**修复后如何被挡住**

两层处理：

- `stripBrackets`（`tools/bfc-proxy.mjs:26`）把 `[::1]` 归一成 `::1`（Node 的 `listen` 不接受方括号形式，否则一个合法地址会被误判成非法）；
- `isLoopback`（`tools/bfc-proxy.mjs:39`）白名单判定：接受 `127.0.0.1`、`localhost`、`::1`，以及 `127.0.0.0/8`（按四段数值比较）；拒绝空串、`0.0.0.0`、`::`、`[::]`、任意主机名、任一段大于 255 的伪地址。

判定放在 `server.listen` **之前**（`tools/bfc-proxy.mjs:64-75`），不合法时用 `fs.writeSync(2, msg + '\n')` 同步写 stderr 再 `process.exit(1)`。这里必须用同步写：在管道或重定向下 `console` 是异步的，`process.exit` 会把最后一行错误吞掉，使用者只看到一个空失败。默认值仍为 `127.0.0.1`，只有未设置时才生效；显式设成空串按不合法处理，不静默回退。

**证据**

- `tools/bfc-proxy.mjs:26` `stripBrackets`；`:39` `isLoopback`；`:54-55` 默认值与归一；`:64-75` 判定与退出；`:78-97` 路由白名单（只放行 `/health`、`/mcp*`、`/api/*`，其余 404）。
- 测试：`tests/test_v011_rc5_proxy.py::ProxyListenGateTest` 共 9 项：`test_any_address_is_rejected`、`test_ipv6_any_is_rejected`、`test_ipv6_any_bracketed_is_rejected`、`test_empty_value_is_rejected`、`test_lan_address_is_rejected`、`test_public_address_is_rejected`、`test_default_loopback_is_allowed`、`test_loopback_range_and_hostname_are_allowed`、`test_bracketed_ipv6_is_not_rejected_by_gate`。

---

### R8 恢复替换期间没有排空

**攻击场景**

GUI 或 CLI 正在从一个备份恢复数据库，同时：

- 另一个进程（另一个实例、或常驻的 API 服务）仍持有旧数据库文件的连接；或
- 同一台机器上服务还在监听端口，前端仍在发写请求。

**修复前会发生什么**

两种平台各坏一套：

- POSIX：替换文件（rename）只是换了目录项，**已打开旧 inode 的连接继续往旧文件写**。新库从这一刻起是「恢复出来的状态」，而所有在途写入都进了那个已经没人看的旧 inode。数据静默分叉，没有任何报错，事后对不上账也找不到断点。
- Windows：文件被别的进程打开，替换直接失败。恢复动作卡在半路，库可能处于「staging 已生成、正式文件还没换」的中间态。

两种情况下 GUI/CLI 都**不会先停服务**——恢复动作和一个正在跑的 API 服务并行。

**修复后如何被挡住**

- **排空靠的是拿到写锁，不是等待**。`Database.tx()` 在整个事务期间持有 `self._lock`；`enter_maintenance`（`bigfish/db.py:298`）调用 `self._lock.acquire(timeout=timeout)`——**拿到锁这件事本身就是证明**：说明当前没有任何在途写事务。用 `sleep` 猜测的问题在于，它只能证明「我等了一会儿」，不能证明「队列空了」；锁竞争激烈时睡多久都可能不够，负载空闲时又白等。锁是可判定的，时间是猜的。拿不到锁就抛错：「有写事务在超时时间内没有结束，无法排空进入维护模式」——宁可拒绝恢复，也不在半排空状态替换文件。

- 进入维护模式后，`query` / `one` / `execute` 首行 `self._guard()`（`bigfish/db.py:448`、`:453`、`:458`），`_Tx.__enter__` 首行也是 `self.db._guard()`（`bigfish/db.py:470-471`）——维护模式下连事务都不抢锁，新写入直接 `MaintenanceError`（`bigfish/db.py:217`）。恢复自己要写库，所以有 `_allow_during_maintenance()`（`bigfish/db.py:332`）这个线程本地开关，只放行恢复流程本身。

- `restore_bundle`（`bigfish/backup.py:462`）全程持维护模式：`bigfish/backup.py:500` 进入，`bigfish/backup.py:509` 用 `_allow_during_maintenance` 包住恢复事务。`before_replace` 回调提前到生成 `.prerestore` 副本**之前**（`bigfish/backup.py:552-553`，副本生成在 `:554-559`）——先停服务再打快照，否则快照里可能混入正在写的半截状态。

- **为什么回滚失败时要保留维护模式**：`bigfish/backup.py:646-652` 在「替换已失败、回滚也失败」时置 `keep_maintenance = True` 并抛双错，`bigfish/backup.py:660-662` 据此跳过 `exit_maintenance`。此时库的实际状态是未知的——磁盘上可能是半旧半新，也可能是换了一半的文件。退出维护模式等于让服务恢复写入一个**状态不明的库**，写入会以最难追查的方式污染数据。保留维护模式则是让接口持续返回 503：服务不可用是明确的、可观测的、可恢复的代价；让状态不明的库接写是不可观测、可能不可逆的代价。宁可 503。

- 对外表现统一为「暂时不可用」而不是「业务错误」：`bigfish/server.py:126-133` 把 `MaintenanceError` 映射为 503 + `Retry-After: 15` + `{"ok": false, "maintenance": true}`；`bigfish/server.py:255-257` 的 `/mcp` 回 `-32000`（不返回 `result`，避免客户端把维护态当成成功）；`bigfish/server.py:136-152` 的 `/health` 报 `status="maintenance"`。

- 调用方也做了配合：`bigfish/cli.py:41` `_port_busy` 在 restore 前探测 `api_host:api_port`（`bigfish/cli.py:212-219`），有服务在监听就打印「拒绝恢复：…请先停掉它…确实要强行恢复请加 --force。」并返回退出码 2；`bigfish/gui.py:1093-1101` 在恢复前 `pause_services()`、把 `before_replace=self.win.pause_services` 传进恢复流程、`finally` 里 `resume_services()`（`bigfish/gui.py:1222`、`bigfish/gui.py:1244`，幂等：停 ApiServer + 停看门狗）。

**证据**

- `bigfish/db.py:217`、`:288`、`:298`、`:332`、`:448`、`:453`、`:458`、`:470-471`。
- `bigfish/backup.py:462`、`:500`、`:509`、`:552-559`、`:646-652`、`:660-662`；`bigfish/backup.py:366` `_restore_lock`。
- `bigfish/server.py:126-133`、`:136-152`、`:255-257`；`bigfish/cli.py:41`、`:212-219`、`:126`；`bigfish/gui.py:1093-1101`、`:1222`、`:1244`。
- 测试：`tests/test_v011_rc5_restore_authority.py::TestMaintenanceMode::test_writes_rejected_inside_maintenance`、`::test_reads_recovered_after_exit`、`::test_maintenance_survives_exception`、`::test_enter_waits_for_inflight_transaction`（真实起一个长事务，验证排空确实等到了它）、`::test_restore_always_exits_maintenance`、`::test_restore_failure_exits_maintenance`；`TestMaintenanceHttpSemantics::test_api_returns_503_during_maintenance`、`::test_mcp_returns_error_not_success_during_maintenance`、`::test_dispatch_tool_raises_maintenance_not_business_error`、`::test_health_reports_maintenance`；`tests/test_v011_rc5_cli_guard.py::TestCliRestoreGuard::test_refuses_when_api_port_is_busy`、`::test_force_bypasses_the_guard`、`::test_port_busy_helper`。

---

### R9 换了密钥，旧票却没失效

**攻击场景**

密钥文件被外部改动（或被删），`ensure_key_consistency` 在启动时检测到指纹不一致，触发自愈。

**修复前会发生什么**

自愈分支把密钥轮换了，文案也写着「恢复前的执行票全部失效」——但函数里**根本没有碰 `tickets` 表**。这并不是疏忽文案，而是对机制的误解：

票是确定性派生：`"bfc_tk_" + hmac.new(key, material, sha256).hexdigest()[:43]`（`bigfish/security.py:196`），其中 `material = f"{executor_id}\x1f{task_id}\x1f{version}\x1f{idem_key}"`。关键在于**认证时不重算 HMAC**——`heartbeat` / `submit_result` 拿到的票会被哈希成 `token_hash`，然后去 `tickets` 表里**查行**。也就是说，票的有效性完全取决于「票行还在不在、`revoked_at` 是不是空、`epoch` 对不对」，和当前密钥是什么无关。

所以「换了密钥」不等于「旧票失效」：旧票的行还在，`revoked_at` 还是 `NULL`，认证照样通过。换密钥唯一的实际效果是「以后新签的票不一样」，对已经发出去的那批票一点作用都没有。这正是最容易被误判成「已经处理了」的那种修复——文案写了、代码动了、风险还在。

**修复后如何被挡住**

`ensure_key_consistency`（`bigfish/backup.py:670`）分成三种动作并交给 `_heal_key`（`bigfish/backup.py:693`）：

- 密钥文件不存在 → `action="new-key"`（按换钥处理，因为旧密钥已经拿不回来了）；
- `meta` 里没有 `key_sha256`（老库第一次升级到 rc5）→ `action="recorded"` 且 `rotate=False`：**只补记指纹，不动票**。这时票还是同一把密钥签的，作废它们等于无故中断在途执行，是纯粹的误伤；
- 指纹不一致 → `action="rotated"`。

只有 rotate 分支才执行吊销：`bigfish/backup.py:713-722` 里 `UPDATE tickets SET revoked_at=? WHERE revoked_at IS NULL` 并清 `claim/heartbeat/submit` 幂等（否则重放会用缓存把已吊销的票再发一次，与 R1 同类问题），然后写 audit `db.key_healed`（`bigfish/backup.py:723-730`），detail 里写明吊销了多少张。

吊销票不去改任务状态——任务是不是继续，由看门狗或人工判断，自愈只负责让凭据失效。

**证据**

- `bigfish/security.py:196` `derive_ticket`（`bigfish/security.py:199` 拼 `material`，`bigfish/security.py:200-201` 做 HMAC 并加 `bfc_tk_` 前缀）；`bigfish/backup.py:670` `ensure_key_consistency`；`bigfish/backup.py:693` `_heal_key`；`bigfish/backup.py:713-722` 吊销与清幂等；`bigfish/backup.py:723-730` 审计。
- 测试：`tests/test_v011_rc5_key_heal.py::TestKeyFingerprintBootstrap::test_first_run_records_fingerprint_without_touching_tickets`（补记指纹不动票，且在途票仍能心跳到 `RUNNING`）、`::test_second_run_is_noop`；`TestKeyRotationInvalidatesTickets::test_rotated_key_revokes_every_live_ticket`、`::test_old_ticket_cannot_authenticate_after_rotation`（核心断言：换钥后旧票必须被 `CouncilError` 拒绝，且任务状态不被顺手改动）、`::test_rotation_purges_claim_idempotency`、`::test_rotation_leaves_audit_trail`（审计 detail 含「吊销」）、`::test_missing_key_file_regenerates_and_revokes`。

---

## 四、测试质量整改

审计同时点名三个既有测试：它们要么恒真、要么过宽，等于没测。处理方式是**按语义重写**，不是放宽断言。

| 测试 | 原问题 | 现在断言什么 |
| --- | --- | --- |
| `tests/test_v011_p0.py::test_db_file_has_no_plaintext_ticket` | 把 prompt 从 blob 里减掉再断言它不在——恒真 | 明文票与 `bfc_tk_` 前缀都不得出现在主库 + WAL 的任何字节里；`tickets.token_hash` 必须等于 `sha256(明文票)`；没有列以 `bfc_tk_` 开头 |
| `tests/test_v011_p1.py::test_watchdog_survives_sweep_error` | 只断言 stop 之后 `_watchdog` 变空 | 连续多次扫描抛异常线程仍存活、`errors` 计数跟上、换回正常 sweep 后能把超时任务真的打成 `BLOCKED` |
| `tests/test_v011_p3.py::test_clean_shutdown` | 只接受 `(0, -15)`，被信号打死也算过 | 新增 `test_graceful_shutdown_on_sigint`（POSIX）：SIGINT 后进程自己收尾、`rc == 0`、WAL 已 checkpoint 到 0 字节 |

「恒真」这类问题的共性是把断言写成了对**输入**的检查而不是对**行为**的检查：既然你从数据里减掉了 X，那 X 当然不在。现在的写法直接从磁盘字节和数据库列里取证据，绕开了被测代码的自述。

第一条还补了一个反向测试 `test_prompt_body_written_to_task_versions`：证明 prompt **确实落库**。否则「库里找不到 X」也可能只是因为「根本没写」，两条断言合起来才构成完整结论。

两处既有断言按语义更新（是收紧不是放宽）：`tests/test_v011_rc3.py::test_revoke_and_submit_race` 改断「该身份已被禁用」+「重新授权」；`::test_restore_older_schema_migrates` 改断恢复后 `APPROVED`→`BLOCKED` 且 `blocked_reason` 含「冻结」。

---

## 五、残余风险（rc5）

以下问题本批次**明确没有解决**，是已知且接受的风险面。

| 编号 | 残余风险 | 说明 |
| --- | --- | --- |
| S1 | 恢复锁只在同一文件系统内有效 | `_restore_lock`（`bigfish/backup.py:366`）依赖 `fcntl.flock` / `msvcrt.locking`，是本地文件锁。两台机器同时恢复同一个共享盘上的库，锁机制无从协调；网络文件系统上的锁语义也不保证 |
| S2 | CLI `--force` 仍可绕过「另一个实例在跑」的检查 | `bigfish/cli.py:126`。这是**给人工留的逃生门**：进程崩了留下僵尸端口、或服务已经不可管理时，需要一条不依赖探测结果的路径。代价是它把「确认没有别的实例」的责任完全交回给人 |
| S3 | 执行端回传的自由文本只做正则脱敏 | `log` / `diff` / `tests` / `note` 在写入 `artifacts` 前一律过 `scrub`（调用点 `bigfish/core.py:1051-1055`；实现 `bigfish/security.py:66-73`，底层是 `bigfish/security.py:34-42` 的六条正则；对外发布前的拦截另有 `contains_secret`（`bigfish/security.py:87-93`）按 `SECRET_PREFIXES` 前缀判定）。**拆半拼接的密钥抓不到**：把 `bfc_tk_abc...` 写成 `bfc_tk_ab` + `c...` 分两行，任何单行正则都匹配不到。脱敏是降低误外泄概率，不是保证 |
| S4 | 数据库文件本身仍未加密 | 票只存 `token_hash` 缓解了 T1 的明文外流，但 `tasks` / `task_versions` 的内容、审计记录都是明文 SQLite。拿到文件的人能读全部业务内容（SQLCipher 一类方案自 rc3 起就是明确排除项） |
| S5 | Windows 平台本批次未跑 | 全部 244 项测试、`selfcheck`、`gui_smoke` 都在 Linux 上跑。Windows 特有的路径（`msvcrt.locking`、文件替换、ACL 检查）只有代码路径，没有实机验证 |
| S6 | 看门狗与维护模式的交界只测了「进入后写入被拒」 | 恢复期间看门狗是停的（GUI `pause_services`），但「看门狗刚好在进入维护模式的那一瞬扫描」没有专门的并发测试 |
| S7 | 「回滚也失败 → 保留维护模式」这条分支没有测试覆盖 | 触发它的三个入口 `bigfish/backup.py:630`（库回滚失败）、`:635`（密钥回滚失败）、`:639`（重开库失败）都标着 `# pragma: no cover`，因此 `bigfish/backup.py:646-652` 置 `keep_maintenance = True` 的分支同样跑不到——它要求「替换失败 + 回滚也失败」同时发生，构造代价高。现有测试只覆盖了 `before_replace` 抛错时正常退出维护模式（`test_restore_failure_exits_maintenance`），真正「卡在维护模式里等人处理」的路径靠代码审阅而非自动化验证 |

---

## 六、本轮没有做的事

明确列出，避免把「没做」误读成「做了但没写」：

1. **没有改生产 DSH。** 所有验证都在本仓库与本地数据目录内进行，没有触碰正在运行的 DSH 实例、配置或会话数据。
2. **没有开任何端口。** 没有把 `bfc-proxy`、API 服务或 `/mcp` 暴露到本机回环之外；R7 的闸门是代码层面的修复，不是靠「反正没开」规避。
3. **没有注册开机自启。** 没有写计划任务、systemd 单元、注册表 Run 项或启动目录快捷方式。
4. **没有导入任何 MCP 连接。** 没有向外部的 MCP server 注册、也没有把本机连接器导入任何外部目录。
5. **没有做 GUI 真窗口验收。** `tools/gui_smoke.py` 以 `QT_QPA_PLATFORM=offscreen` 运行（通过 9 项），覆盖的是构造与信号连接，不是人眼看着点一遍。布局、焦点顺序、真实 DPI 下的显示问题都还没有人验收。
6. **没有跑 Windows。** 见 S5。
7. 没有改代码逻辑、没有提交、没有打 tag。本文档是这批改动的唯一交付物。

---

## 七、语义变更与兼容性

| 项 | rc4 | rc5 |
| --- | --- | --- |
| `SCHEMA_VERSION` | 3 | 4（`executors.disabled_reason` + `idx_executors_enabled`） |
| `BACKUP_FORMAT_VERSION` | 3 | 4 |
| 审计包列策略 | 黑名单 `AUDIT_DENY_COLUMNS` | 白名单 `AUDIT_COLUMNS`，manifest 声明 `policy: allowlist` |
| 恢复后 `APPROVED` 任务 | 保持 `APPROVED`，可被领取 | 冻结为 `BLOCKED`，需重新提交审批 |
| 恢复后执行器身份 | 保持 `enabled=1` | 全部 `enabled=0` + `disabled_reason`，必须 `reenable` 且强制换 token |
| 重新启用执行器 | 翻回 `enabled=1` | `reenable_executor`：同时轮换 `token_hash`，写 `executor.reenabled` 审计 |
| 幂等重放 | 命中缓存即重发 | 先校验取消/归属/状态/版本/吊销，全部通过才发 |
| 恢复期并发写入 | 允许（会写旧 inode） | `MaintenanceError` → HTTP 503 + `Retry-After`，MCP `-32000` |
| 代理监听地址 | 直接采信环境变量 | 非回环直接 `exit(1)` |
| 执行端可观测性 | 无本地痕迹 | `ReceiptLog` JSONL（flush + fsync），`dispatch_id` 对账 |

`SCHEMA_VERSION` 从 3 升到 4 是**单向**的：rc5 打开过的库，rc4 会以「schema 版本高于本程序支持」拒绝启动（`bigfish/db.py:248-256`）。升级前如需回退，必须先保留 rc4 时期的备份副本。

---

## 八、复核方式

```bash
cd /home/dsh/bigfish-council

# 全量测试（244 项）
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .

# 自检与 GUI 冒烟
.venv/bin/python tools/selfcheck.py
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/gui_smoke.py

# 静态检查
.venv/bin/python -m pyflakes bigfish tests tools
.venv/bin/python -m compileall -q bigfish tests tools
```

按审计项定位测试：R1 → `tests/test_v011_rc5_restore_authority.py`；R2/R3/R4/R6 → `tests/test_v011_rc5_review_audit.py`；R5 → `tests/test_v011_rc5_receipts.py` + `tests/test_v011_rc5_dispatch_id.py`；R7 → `tests/test_v011_rc5_proxy.py`；R8 → `tests/test_v011_rc5_restore_authority.py` + `tests/test_v011_rc5_cli_guard.py`；R9 → `tests/test_v011_rc5_key_heal.py`。
