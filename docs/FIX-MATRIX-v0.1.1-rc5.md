# 修复总表：bigfish-council v0.1.1-rc5

| 项 | 值 |
| --- | --- |
| 版本 | `0.1.1-rc5`（`bigfish/__init__.py:6`） |
| 日期 | 2026-11-20 |
| 相对基线 | rc4 发布点 `72e8535` |
| 审计轮次 | 第三方二审 rc5 批次（R1–R9 九项 + 测试质量整改 3 项） |
| 分支 / HEAD | `fix/v0.1.1-rc4-hardening` / `b8f6de1`（rc5 发布提交）。修复主体分三个提交：`4281114`（R1+R8）、`a4f0641`（R2+R3+R4+R6+R9）、`adc48a3`（R5+R8），收尾修订并入 `b8f6de1` |
| 差异规模 | 代码侧（`bigfish` + `tests` + `tools`）相对 `72e8535`：`adc48a3` 为 20 文件 +2251/-60，并入收尾修订的 `b8f6de1` 为 20 文件 +2276/-61；含 `AUDIT.md` / `CHANGELOG.md` / `README.md` / `docs` 的全仓 diff 是 29 文件 +5935/-139。口径说明见补充说明三 |
| 库结构 | `SCHEMA_VERSION = 4`（`bigfish/db.py:22`）、`BACKUP_FORMAT_VERSION = 4`（`bigfish/db.py:23`） |
| 全量测试 | `Ran 244 tests / OK`，本轮多次重跑 38.380s ~ 45.639s，收尾实测 39.178s（Debian 12 / Python 3.11.2 / offscreen）；`adc48a3` 上的 243 项是收尾前一瞬的中间态，不作结论依据 |
| 自检与冒烟 | `tools/selfcheck.py` 通过 16 项、失败 0 项；`tools/gui_smoke.py` 通过 9 项、失败 0 项 |
| 静态检查 | `pyflakes bigfish tests tools` 零输出；`compileall` RC=0 |
| 平台 | 只跑了 Linux（Debian 12.15 / Linux 5.15.0-187-generic / Python 3.11.2）；Windows 本批次未跑 |

状态口径：

- `已修复`：代码已改，且有能失败的回归测试，本批次在 Linux 上实测通过。
- `已修复（未覆盖）`：代码已改，自动化证据不完整，另行注明缺什么。
- 本表不出现「已验证（Windows）」字样，因为本批次没有 Windows 实机记录。

严重度口径（本文自定，用于排序，不是审计给的等级）：高 = 可直接导致越权、授权复活或数据分叉；中 = 不直接越权，但会丢证据、丢事件或扩大泄露面。

---

## 一、R1–R9 修复总表

| 编号 | 原始问题 | 严重度 | 修改文件（行号 / 函数） | 修复方案 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | 从旧备份恢复后授权没有作废：APPROVED 任务仍可被领取，executors 仍 `enabled=1`（快照里的 `token_hash` 一起回来了，旧 token 照样能认证），幂等表里缓存着重放响应，而 `_replay_claim` 不校验取消、归属、状态、吊销就把票重发出去 | 高 | `bigfish/backup.py:319` `_block_inflight`（:337 在途、:345 APPROVED 冻结、:352-353 吊销票、:355-357 禁用身份、:358-359 清幂等）；`bigfish/backup.py:463` `restore_bundle`、`:412` `_prepare_staging`；`bigfish/core.py:188` `authenticate`、`:210` `authenticate_any`、`:241` `_disabled_message`、`:246` `reenable_executor`、`:270` `_assert_executor_active`、`:843` `_replay_claim`、`:900` `_check_ticket_row`；`bigfish/db.py:97`、`:205-208`；`bigfish/cli.py:57`、`:223-225` | `_block_inflight` 在同一事务里做五步：① `DISPATCHED/RUNNING/CANCELLING` → `BLOCKED`（原因「数据库已从备份恢复，执行授权全部作废，需人工确认副作用后重新审批」）；② `APPROVED` → `BLOCKED` 冻结（原因含「冻结」，:344 标 R1-a）；③ `UPDATE tickets SET revoked_at` 吊销全部活票；④ `UPDATE executors SET enabled=0, disabled_reason=?`，不删行、保留审计证据（:354 标 R1-b，常量 `DISABLED_REASON_RESTORE` 在 :72）；⑤ 清 `claim/heartbeat/submit` 三类幂等缓存。认证侧对被禁用身份给可执行文案（`_disabled_message`：要求人工重新授权并更换凭据）；新增 `reenable_executor`，重新启用必须同时轮换 token（前缀按 role 取 `bfc_ex_/bfc_rv_/bfc_ad_`），写 `executor.reenabled` 审计；`_replay_claim` 重写为「先校验取消（:855）→ 归属（:857）→ 状态（:858-860）→ 版本（:862）→ 票行存在（:865）→ `_check_ticket_row`（:866）」，全部通过才发。`_check_ticket_row` 增加撤销判定与恢复世代判定。schema 4 加 `executors.disabled_reason` 与 `idx_executors_enabled`。CLI 加 `reenable` 子命令 | `tests/test_v011_rc5_restore_authority.py::test_restore_disables_every_executor`、`::test_restored_access_token_no_longer_authenticates`、`::test_reviewer_token_also_rejected_after_restore`、`::test_reenable_rotates_token_and_old_one_dies`、`::test_reenable_keeps_role_and_model_and_writes_audit`、`::test_disabled_reason_cleared_after_reenable`、`::test_approved_task_frozen_not_claimable`、`::test_inflight_task_blocked_and_ticket_revoked`、`::test_replay_rejected_after_restore`、`::test_replay_rejected_after_cancel`；`tests/test_v011_rc3.py::test_old_ticket_invalid_after_restore`、`::test_old_claim_idempotency_purged_after_restore`、`::test_inflight_task_blocked_after_restore`、`::test_restore_older_schema_migrates`（断言已按语义更新）、`::test_revoke_and_submit_race`（同上） | 已修复 |
| R2 | `_recompute_review` 的 `all_approved` 分支在写完状态后提前 `return`，跳过函数末尾的 `log_event` 循环，导致 `task.review_passed` / `task.pending_approval` 两条事件永远不落库 | 中 | `bigfish/core.py:597` `_recompute_review`（:610 分支、:616-618 事件、:619-621 注释、:623 `elif`、:639-640 落库循环） | 删掉该分支末尾的 `return`；未通过分支从 `if` 改 `elif`，保证状态改写与事件收集都在同一函数内走到末尾的 `for kind, detail in events: self.log_event(...)`；代码里留注释说明「这里不能 return」的原因，避免后人重新加回去 | `tests/test_v011_rc5_review_audit.py::test_events_written_on_all_approved`、`::test_status_still_reaches_pending_approval`、`::test_events_written_when_votes_cast_before_review` | 已修复 |
| R3 | `revoke_review` 不判断 `kind`，撤掉一条普通评论（`kind=comment`）也会把已批准任务打回 `IN_REVIEW`、把运行中任务打成 `BLOCKED` 并吊销票 | 高 | `bigfish/core.py:497` `revoke_review`（:513 取 kind、:519-522 门控） | 读审查行时取 `kind`（缺省视作 `vote`），只有 `kind == "vote"` 且该票属于当前版本时才允许影响审批有效性：`PENDING_APPROVAL/APPROVED` → `IN_REVIEW` 并吊销票，`DISPATCHED/RUNNING/CANCELLING` → `BLOCKED`（原因「审查票被撤销，执行授权已作废，需人工确认副作用」）并吊销票；`kind != "vote"` 时任务行置空，只撤评论本身；真正失效时写 `approval.invalidated` 审计 | `tests/test_v011_rc5_review_audit.py::test_revoke_comment_does_not_touch_approved_task`、`::test_revoke_comment_does_not_block_running_task`、`::test_revoke_vote_still_invalidates`；`tests/test_v011_rc3.py::test_revoke_vote_reopens_approval`、`::test_revoke_and_claim_race` | 已修复 |
| R4 | `heartbeat` / `submit_result` 的 `now()` 取在 `with db.tx()` 之前，等锁期间算出的陈旧时间被写进库，`expires_at` 反而比现在更早，续租等于没续 | 中 | `bigfish/core.py:932` `heartbeat`（:938 开事务、:939-941 注释、:942 `t = now()`）；`bigfish/core.py:998` `submit_result`（:1008 开事务、:1009 `t = now()`） | 把 `t = now()` 移到事务体第一行，即持锁之后再取时间；两个写路径都留 R4 注释说明原因，防止后续重构把时间取值挪回事务外 | `tests/test_v011_rc5_review_audit.py::test_heartbeat_lease_is_in_the_future`、`::test_heartbeat_keeps_lease_alive_repeatedly`；`tests/test_v011_rc3.py::test_watchdog_and_heartbeat_race`；`tests/test_v011_p1.py::test_ticket_never_exceeds_max_ttl_from_issue` | 已修复 |
| R5 | 执行端没有「开工收据」，服务端数据库一旦从备份恢复，事后无法对账某次派工到底有没有开工；服务端响应里也没有稳定的派工标识 | 中 | `bigfish/executor_client.py:41` `ReceiptLog`（:53-57 事件白名单与收尾类事件、:59 构造、:68 `record`、:93-95 flush+fsync、:98 `read_all`、:123 `state_of`、:159 `summary`、:208 `dispatch_id_of`）、`:180` `ExecutorClient.__init__(receipts=)`、`:198` `_as_receipts`、`:272/:316/:321/:329/:332` 各事件打点、`:337` `reconcile`；`bigfish/core.py:838`（claim 返回 `dispatch_id`）、`:884`（重放返回同一 id） | 新增追加式 JSONL 收据日志，事件白名单 `claimed/started/finished/failed/blocked/cancelled/uncertain`，收尾类 `CLOSED_EVENTS` 单独列出；每条收据先 `flush` 再 `os.fsync`（顺序不能反，否则断电仍可能丢），坏行跳过不影响读；`path=None` 且环境变量 `BFC_RECEIPTS` 未设时退化为纯内存 no-op，不偷偷落盘；`ExecutorClient` 加 `receipts` 参数（`None/False` 显式禁用）与 `reconcile()`，用「有 started 没有收尾事件」判定结果未知；服务端 claim 与幂等重放都返回 `dispatch_id = ticket_id`，重新派工会换新票，旧收据不会被覆盖 | `tests/test_v011_rc5_receipts.py::test_invalid_event_raises_value_error`、`::test_started_without_finish_means_unknown`、`::test_record_is_flushed_to_disk_immediately`、`::test_reader_in_new_process_sees_rows`、`::test_run_once_writes_claimed_started_finished_in_order`、`::test_run_once_records_uncertain_and_reraises`、`::test_reconcile_lists_unknown_inflight`、`::test_dispatch_id_falls_back_to_ticket_id`、`::test_receipts_none_means_disabled`、`::test_default_reads_env_and_stays_memory_when_unset`；`tests/test_v011_rc5_dispatch_id.py::test_claim_returns_dispatch_id`、`::test_replay_returns_same_dispatch_id`、`::test_redispatch_gets_new_dispatch_id` | 已修复 |
| R6 | 审计导出用列黑名单 `AUDIT_DENY_COLUMNS`，将来表里新增的敏感列会自动进入导出包 | 中 | `bigfish/backup.py:56` `AUDIT_COLUMNS`（6 表逐列显式）、`:69` `AUDIT_DENY_COLUMNS`（仅保留兼容引用）、`:148` `export_audit_bundle`（:165 取白名单、:194 manifest） | 改成列白名单：`tasks` / `task_versions` / `reviews` / `approvals` / `events` / `audit` 六张表逐列写死，导出时按白名单取列，不在白名单的列记进 `dropped_columns`；`executors/tickets/idempotency/runs/artifacts/meta` 整表不进包，记进 `dropped_tables`；manifest 的 `redaction` 改成 `{policy: "allowlist", allowed_columns, dropped_tables, dropped_columns, string_scrub}`，让拿到包的人能自己核对策略 | `tests/test_v011_rc5_review_audit.py::test_every_exported_column_is_on_the_allowlist`、`::test_no_sensitive_column_names_leak`、`::test_manifest_declares_allowlist_policy`、`::test_dropped_columns_reported_when_present`、`::test_prompt_body_scrubbed_of_ticket`；`tests/test_v011_rc3.py::test_audit_bundle_internal_tables` | 已修复 |
| R7 | `tools/bfc-proxy.mjs` 的 `LISTEN_HOST` 直接采信环境变量、启动前无任何回环校验，一句 `BFC_PROXY_LISTEN_HOST=0.0.0.0` 就能把这个不做鉴权的跳板绑到全网卡 | 高 | `tools/bfc-proxy.mjs:26` `stripBrackets`、`:39` `isLoopback`、`:54-55` 取环境变量与默认值、`:66` 闸门、`:73` 写 stderr、`:74` `process.exit(1)` | 新增回环判定：先剥 IPv6 方括号，接受 `127.0.0.1` / `localhost` / `::1` 三个字面量与 `127.0.0.0/8` 的 IPv4 字面量（逐段解析、每段 ≤255、首段必须为 127），拒绝空串、`0.0.0.0`、`::`、`[::]` 以及任何主机名（避免 `127.0.0.1.evil.com` 这类前缀绕过）；未设置环境变量才用 `127.0.0.1` 默认值；判定在 `listen` 之前，不合法就用 `fs.writeSync(2, ...)` 打四行中文错误再 `exit(1)`（不用 `console.error`，管道下异步写会被 `exit` 吞掉） | `tests/test_v011_rc5_proxy.py::test_any_address_is_rejected`、`::test_ipv6_any_is_rejected`、`::test_ipv6_any_bracketed_is_rejected`、`::test_empty_value_is_rejected`、`::test_lan_address_is_rejected`、`::test_public_address_is_rejected`、`::test_default_loopback_is_allowed`、`::test_loopback_range_and_hostname_are_allowed`、`::test_bracketed_ipv6_is_not_rejected_by_gate`（需要 `node`，无 node 时整类跳过） | 已修复 |
| R8 | 恢复替换期间没有排空：另一个实例仍持旧连接，POSIX 上继续写旧 inode（数据静默分叉）、Windows 上替换直接失败；GUI / CLI 恢复时也不停服务 | 高 | `bigfish/db.py:217` `MaintenanceError`、`:288` `_guard`、`:298` `enter_maintenance`、`:316` `exit_maintenance`、`:323` `maintenance`、`:332` `_allow_during_maintenance`、`:341` `in_maintenance`、`:447/:452/:457` 三个访问器首行 `_guard()`、`:471` `_Tx.__enter__` 首行 `_guard()`；`bigfish/backup.py:499` `keep_maintenance`、`:501` 进入、`:510` 放行恢复自身、`:554` `before_replace`、`:651` 回滚失败保留维护模式、`:662-664` 条件退出；`bigfish/server.py:31`、`:127` `_maint_err`、`:137` `health`、`:257` `/mcp`；`bigfish/gui.py:1074` `restore`、`:1093`/`:1222` `pause_services`、`:1098`/`:1243` `resume_services`；`bigfish/cli.py:41` `_port_busy`、`:211-220` restore 分支、`:126` `--force` | 库里加维护模式：`enter_maintenance` 先用 `self._lock.acquire(timeout)` 做排空屏障（拿到锁就说明没有在途写事务，不是靠 sleep 猜），拿不到就抛「有写事务在超时时间内没有结束，无法排空进入维护模式」；此后 `query/one/execute/tx` 首行 `_guard()` 直接 `MaintenanceError`（连锁都不抢），恢复自身用 `_allow_during_maintenance()` 放行；`restore_bundle` 全程持维护模式，`before_replace` 提前到生成 `.prerestore` 之前调用，回滚也失败时 `keep_maintenance=True` 保留维护模式而不是把状态不明的库放回去接写；server 把 `MaintenanceError` 映射成 503 + `Retry-After: 15`（业务错误仍是 409），`/mcp` 回 `-32000` 且不返回 `result`，`/health` 报 `status=maintenance`；GUI 恢复前后 `pause_services` / `resume_services`（幂等）；CLI restore 先探 `api_host:api_port`，有服务在监听就拒绝并退出码 2，除非 `--force` | `tests/test_v011_rc5_restore_authority.py::test_writes_rejected_inside_maintenance`、`::test_reads_recovered_after_exit`、`::test_maintenance_survives_exception`、`::test_enter_waits_for_inflight_transaction`、`::test_restore_always_exits_maintenance`、`::test_restore_failure_exits_maintenance`、`::test_api_returns_503_during_maintenance`、`::test_mcp_returns_error_not_success_during_maintenance`、`::test_dispatch_tool_raises_maintenance_not_business_error`、`::test_health_reports_maintenance`；`tests/test_v011_rc5_cli_guard.py::test_refuses_when_api_port_is_busy`、`::test_force_bypasses_the_guard`、`::test_port_busy_helper` | 已修复 |
| R9 | `ensure_key_consistency` 的自愈分支没有单测，文案写着「恢复前的执行票全部失效」但函数里根本没碰 `tickets` 表——换了密钥，`token_hash` 还是旧值，旧票照样能认证 | 高 | `bigfish/backup.py:672` `ensure_key_consistency`、`:695` `_heal_key`（:706-708 换钥、:714 写指纹、:716 起吊销与清幂等、:725-732 审计） | 把自愈逻辑抽成 `_heal_key` 并分三种动作：密钥文件不存在 → `new-key`（按换钥处理）；`meta` 里没有 `key_sha256`（老库首次升级到 rc5）→ `recorded` 且 `rotate=False`，只补记指纹、不动任何票（同一把密钥签的票作废等于无故打断在途执行）；指纹不一致 → `rotated`。只有 rotate 分支才在事务内吊销全部活票、清 `claim/heartbeat/submit` 三类幂等、写 `db.key_healed` 审计，detail 里写清吊销张数 | `tests/test_v011_rc5_key_heal.py::test_first_run_records_fingerprint_without_touching_tickets`、`::test_second_run_is_noop`、`::test_rotated_key_revokes_every_live_ticket`、`::test_old_ticket_cannot_authenticate_after_rotation`、`::test_rotation_purges_claim_idempotency`、`::test_rotation_leaves_audit_trail`、`::test_missing_key_file_regenerates_and_revokes` | 已修复 |

补充说明一（行号口径）：本表所有行号都按 `b8f6de1` 这棵树逐一核对过，不是估值。收尾修订（`tickets_revoked` 改真实计数）已并入该提交，因此 `bigfish/backup.py` 在 `:352-353`、`:355-357`、`:358-359`、`:363`、`:599` 处的行号与当前 `HEAD` 完全一致；`core.py` / `db.py` / `server.py` / `gui.py` / `cli.py` / `executor_client.py` / `tools/bfc-proxy.mjs` 与全部测试文件同样按当前 `HEAD` 核对。

补充说明二（收尾那处产品改动）：`_block_inflight` 返回体里的 `tickets_revoked` 在 rc4 及以前是硬编码常量 `1`，不是实际吊销行数；收尾时一并改成真实 `rowcount`（`bigfish/backup.py:352-353` 取值、`:363` 返回，`restore_bundle` 在 `:599` 把它透出），并补了 `tests/test_v011_rc5_restore_authority.py::test_tickets_revoked_reports_real_count`（:176）固定语义：先领两次工造出两张活票，恢复后断言 `tickets_revoked == 2` 且活票归零。这处改动就是 `b8f6de1` 相对 `adc48a3` 的全部代码差异（`bigfish/backup.py` +6/-2、该测试文件 +22），因此该文件从 20 项变 21 项、全量从 243 项变 244 项。该计数与 `blocked` / `frozen_approved` / `executors_disabled` / `idempotency_purged` 一样都是真实取值。

补充说明三（差异规模口径）：`+2251/-60` 是 `adc48a3` 相对 `72e8535` 的代码侧数字，`+2276/-61` 是并入收尾修订的 `b8f6de1` 的同一口径数字（20 文件，`git diff --stat 72e8535 b8f6de1 -- bigfish tests tools`）。把文档也算进全仓则是 29 文件 +5935/-139（含 `AUDIT.md`、`CHANGELOG.md`、`README.md` 与八份 rc5 文档）。本表按代码侧口径写，不把文档算成代码改动。

---

## 二、测试质量整改（审计点名 3 个测试，展开为 4 行）

审计点名三个既有测试「恒真或过宽」，按语义重写，不是放宽断言。下表 Q1 / Q3 / Q4 各行对应被点名的那个测试，Q2 是 Q1 的配套反证（新增，防「因为根本没写所以查不到」这种假通过），Q4 额外加了一个 POSIX 信号收尾用例。

| 编号 | 测试 | 原来的问题 | 现在断言什么 | 文件行号 | 状态 |
| --- | --- | --- | --- | --- | --- |
| Q1 | `test_db_file_has_no_plaintext_ticket` | 先把 prompt 从主库二进制里减掉，再断言 prompt 不在里面——被减掉的字符串当然不在，恒真；它实际只验证了「执行票明文不在」，且验证方式对 prompt 是自欺 | 两条独立硬断言：① 明文票与 `bfc_tk_` 前缀都不得出现在「主库 + WAL」的任何字节里；② `tickets` 表里该行的 `token_hash` 必须等于 `hash_token(明文票)`，且该行没有任何字符串列以 `bfc_tk_` 开头 | `tests/test_v011_p0.py:174` | 已重写 |
| Q2 | `test_prompt_body_written_to_task_versions` | 不存在（新增） | 承接 Q1 的反向证明：断言 `task_versions` 中 version=1 的 `prompt` 确实等于 `SECRET-PROMPT-BODY`，否则「库里找不到 prompt」可能只是因为根本没写进去 | `tests/test_v011_p0.py:201` | 已新增 |
| Q3 | `test_watchdog_survives_sweep_error` | 只断言 stop 之后 `c._watchdog` 变空；「异常被吞掉继续跑」和「线程第一次异常就死了」都能过 | 三件事同时成立：① 连续多次扫描抛异常后线程仍 `is_alive()`；② `errors` 计数跟上（≥3），说明异常被计进去而不是静默吞掉；③ 换回正常 sweep 并把 `heartbeat_ttl` 设为 0 后，真能把超时任务打成 `BLOCKED`；末尾仍断言 stop 后 `_watchdog` 为空 | `tests/test_v011_p1.py:80` | 已重写 |
| Q4 | `test_clean_shutdown` + `test_graceful_shutdown_on_sigint` | `test_clean_shutdown` 只接受 `(0, -15)`，被信号打死也算过；判据是「连接是否被拒」，Windows 上内核丢包不回 RST 时结论不可靠 | `test_clean_shutdown` 保留但把判据换成「退出后能否立刻重新 bind 同一端口」（监听套接字随进程消失才能绑上，比 connect 更硬）；新增 `test_graceful_shutdown_on_sigint`（POSIX）：SIGINT 必须让服务自己收尾退出，`rc == 0`，且 WAL 已 checkpoint 到 0 字节 | `tests/test_v011_p3.py:160`、`tests/test_v011_p3.py:192` | 已重写 + 已新增 |

Q4 的 Windows 分支接受 `rc in (0, 1)`：Windows 没有可捕获的 SIGTERM，`terminate()` 走 `TerminateProcess`，退出码是 1 而非 -15，这不是进程自己崩了，所以另用端口能否重新绑定来证明资源回收。该分支本批次没有实机跑过。

---

## 三、按语义更新的既有断言（2 处，收紧而非放宽）

| 编号 | 测试 | 改动 | 依据 | 行号 |
| --- | --- | --- | --- | --- |
| A1 | `test_revoke_and_submit_race` | 旧断言 `assertIn("吊销", submit_err)`；新断言 `assertIn("该身份已被禁用")` + `assertIn("重新授权")`，并保留「吊销后不许再有写操作」，同时断言任务状态落在 `(BLOCKED, DISPATCHED, RUNNING)` | R1 之后「被管理员吊销」与「恢复后统一禁用」共用同一条可执行文案（都要求人工重新授权并更换凭据），旧关键字已不再出现；断言文案更具体而不是更松 | `tests/test_v011_rc3.py:381` |
| A2 | `test_restore_older_schema_migrates` | 旧断言「恢复后 status == APPROVED」；新断言恢复后 `status == BLOCKED`、`blocked_reason` 含「冻结」、`out["frozen_approved"] >= 1` | 直接对应 R1 的语义变更：快照里的 APPROVED 是「随时可领取的授权」，恢复不能把它原样放回。旧断言在 rc5 下会失败，属于必须改的语义，不是放宽 | `tests/test_v011_rc3.py:507` |

---

## 四、按文件汇总

| 文件 | 承担的项 | 关键落点 |
| --- | --- | --- |
| `bigfish/__init__.py` | 版本号 | `:6` `__version__ = "0.1.1-rc5"` |
| `bigfish/backup.py` | R1、R6、R8、R9 | R1：`:319` `_block_inflight`（:337/:345/:352-353/:355-357/:358-359）、`:412` `_prepare_staging`、`:463` `restore_bundle`；R6：`:56` `AUDIT_COLUMNS`、`:69` 兼容引用、`:148` `export_audit_bundle`（:165/:194）；R8：`:499/:501/:510/:554/:651/:662-664`；R9：`:672` `ensure_key_consistency`、`:695` `_heal_key` |
| `bigfish/core.py` | R1、R2、R3、R4、R5（服务端半边） | R1：`:188`/`:210` 认证、`:241` `_disabled_message`、`:246` `reenable_executor`、`:270` `_assert_executor_active`、`:843` `_replay_claim`、`:900` `_check_ticket_row`；R2：`:597` `_recompute_review`（:610-640）；R3：`:497` `revoke_review`（:513/:519）；R4：`:932` `heartbeat`（:942）、`:998` `submit_result`（:1009）；R5：`:838` 返回 `dispatch_id`、`:884` 重放复用 |
| `bigfish/db.py` | R1（schema 4）、R8（维护模式） | `:97` `disabled_reason` 列、`:205-208` 第 4 号迁移与索引；`:217` `MaintenanceError`、`:288` `_guard`、`:298` `enter_maintenance`、`:316` `exit_maintenance`、`:323` `maintenance`、`:332` `_allow_during_maintenance`、`:341` `in_maintenance`、`:447/:452/:457`、`:471` |
| `bigfish/executor_client.py` | R5 | `:41` `ReceiptLog`（:53-57 白名单、:59 构造、:68 `record`、:93-95 fsync、:98/:123/:159 读取视图、:208 `dispatch_id_of`）、`:180` `receipts` 参数、`:198` `_as_receipts`、`:272/:316/:321/:329/:332` 打点、`:337` `reconcile` |
| `bigfish/server.py` | R8 | `:31` 导入、`:127` `_maint_err`（503 + `Retry-After: 15`）、`:137` `health` 三态、`:257` `/mcp` 回 `-32000` |
| `bigfish/gui.py` | R8 | `:1074` `restore`、`:1093` 与 `:1222` `pause_services`（幂等）、`:1095-1096` 传 `before_replace`、`:1098` 与 `:1243` `resume_services`、`:1099-1101` 读回三项计数 |
| `bigfish/cli.py` | R1（`reenable` 子命令）、R8（restore 端口探测与 `--force`） | `:41` `_port_busy`、`:126` `--force`、`:211-220` restore 分支（拒绝时退出码 2）、`:223-225` `reenable` |
| `tools/bfc-proxy.mjs` | R7 | `:26` `stripBrackets`、`:39` `isLoopback`、`:54-55` 取值与默认、`:66` 闸门、`:73` `fs.writeSync(2, ...)`、`:74` `process.exit(1)` |
| `tests/test_v011_p0.py` | Q1、Q2 | `:174` 重写、`:201` 新增 |
| `tests/test_v011_p1.py` | Q3 | `:80` 重写 |
| `tests/test_v011_p3.py` | Q4 | `:160` 改判据、`:192` 新增 |
| `tests/test_v011_rc3.py` | A1、A2 | `:381`、`:507` |
| `tests/test_v011_rc5_restore_authority.py` | R1、R8 | 21 项（`adc48a3` 上 20 项，第 21 项 `test_tickets_revoked_reports_real_count`（:176）随收尾修订并入 `b8f6de1`），见第一节 |
| `tests/test_v011_rc5_review_audit.py` | R2、R3、R4、R6 | 新增 13 项，见第一节 |
| `tests/test_v011_rc5_receipts.py` | R5 | 新增 24 项（含纯文件系统与子进程读回） |
| `tests/test_v011_rc5_dispatch_id.py` | R5 | 新增 3 项 |
| `tests/test_v011_rc5_proxy.py` | R7 | 新增 9 项（依赖 `node`） |
| `tests/test_v011_rc5_key_heal.py` | R9 | 新增 7 项 |
| `tests/test_v011_rc5_cli_guard.py` | R8（CLI 半边） | 新增 3 项 |

用例分布（本轮 grep 计数）：`test_api` 6、`test_core` 15、`test_v011_p0` 16、`test_v011_p1` 24、`test_v011_p2` 24、`test_v011_p3` 20、`test_v011_rc3` 26、`test_v011_rc4` 33、`test_v011_rc5_cli_guard` 3、`test_v011_rc5_dispatch_id` 3、`test_v011_rc5_key_heal` 7、`test_v011_rc5_proxy` 9、`test_v011_rc5_receipts` 24、`test_v011_rc5_restore_authority` 21、`test_v011_rc5_review_audit` 13，合计 244。

---

## 五、验收对照（审计提的 8 个问题）

### 1. 能不能绕过审批直接派工

- 代码位置：`bigfish/core.py:662` `approve` 只接受 `actor == R.HUMAN_OWNER`（`:667` 判身份，否则 `:668` 抛「只有人类所有者可以执行审批」）；`:677-682` 把允许状态限死在 `{PENDING_APPROVAL, REVIEW_PASSED}`（`return` 另加 `IN_REVIEW`）；`:686-694` 的 approve 分支要求 `votes["complete"]`（:688-689）且 `votes["all_approved"]`（:690-691），并拒绝执行器参与投票（:692-694）。派工侧 `bigfish/core.py:780` 要求任务处于 `APPROVED` 才可 `claim`。
- 测试证据：`tests/test_v011_p2.py::test_only_human_can_approve`、`::test_non_human_cannot_approve`、`::test_gpt_role_cannot_vote_as_gemini`、`::test_gemini_role_cannot_vote_as_gpt`、`::test_bound_role_votes_recorded`；`tests/test_v011_p0.py::test_drifted_version_cannot_be_approved_without_votes`、`::test_double_approve_rejected`、`::test_old_version_vote_revoked_after_update`。
- rc5 是否改变结论：结论不变（仍必须人类 + 双模型双票），但收紧了——恢复旧备份后就算快照里是 `APPROVED` 也不再算数（`bigfish/backup.py:345` 冻结为 `BLOCKED`）。这是「过去的批准」不再自动续期，不是新增审批入口。

### 2. 取消之后还能不能领到票

- 代码位置：`bigfish/core.py:778-779` 在事务内检查 `control.cancel_requested` 与 `cancel_requested_at`，命中即抛「任务已被请求取消，不能领取」；`:780` 状态非 `APPROVED` 直接抛；`:798-802` 的状态更新带 `AND cancel_requested_at IS NULL`，`:803-804` 检查 `rowcount != 1` 即失败；重放路径 `bigfish/core.py:855` 同样挡取消。
- 测试证据：`tests/test_v011_rc3.py::test_cancel_after_approved_cannot_claim`、`::test_cancel_and_claim_concurrent`（真双线程 + Barrier）、`::test_revoke_and_claim_race`、`::test_cancel_before_approval_cannot_approve`；`tests/test_v011_rc5_restore_authority.py::test_replay_rejected_after_cancel`。
- rc5 是否改变结论：是。rc4 只在首次 `claim` 路径上挡，幂等重放（同一 `idem_key`）会绕过——R1 重写 `_replay_claim` 后把取消检查放到重放路径最前面（`bigfish/core.py:855`），结论从「大部分挡住」变成「两条路径都挡」。

### 3. 恢复之后旧票会不会复活

- 代码位置：`bigfish/backup.py:319` `_block_inflight` 五步（在途转 `BLOCKED`、`APPROVED` 冻结、吊销全部活票 `:352-353`、全部 executors `enabled=0` `:355-357`、清三类幂等 `:358-359`）；`bigfish/core.py:900` `_check_ticket_row` 校验撤销与恢复世代（epoch 不匹配即「执行票已失效（数据库已从备份恢复），请重新领取」）；`bigfish/core.py:188`/`:210` 认证侧对被禁用身份直接拒绝；`:246` `reenable_executor` 重新启用必须换 token。
- 测试证据：`tests/test_v011_rc5_restore_authority.py::test_restore_disables_every_executor`、`::test_restored_access_token_no_longer_authenticates`、`::test_reviewer_token_also_rejected_after_restore`、`::test_approved_task_frozen_not_claimable`、`::test_inflight_task_blocked_and_ticket_revoked`、`::test_replay_rejected_after_restore`、`::test_reenable_rotates_token_and_old_one_dies`；`tests/test_v011_rc3.py::test_old_ticket_invalid_after_restore`、`::test_old_claim_idempotency_purged_after_restore`、`::test_inflight_task_blocked_after_restore`。
- rc5 是否改变结论：是，这是本批次最实质的结论变更。rc4 时「恢复后旧票还能用」成立（票只被吊销，但快照里的身份与幂等缓存把票重新唤醒）；rc5 之后旧 token、旧票、旧派工、旧幂等响应四类全部失效，且必须人类 `reenable` 并换凭据才能恢复远端身份。

### 4. Watchdog 会不会误杀还在跑的任务

- 代码位置：`bigfish/core.py:1197` `sweep_timeouts` 把判断与更新放进同一个 `BEGIN IMMEDIATE` 事务（`:1206`），心跳时间按「当前这张活票」查（`:1214-1220` 先找 `revoked_at IS NULL` 的最新票再按 `ticket_id` 查 `runs.last_beat_at`，`:1223-1224` 缺心跳才回落到 `task.updated_at`），状态更新带 `WHERE id=? AND status=?` 且 `rowcount != 1` 就跳过（`:1239-1240`），因此「心跳刚续租」不会被事务外读到的旧时间戳误杀；R4 又把 `heartbeat` 的 `t = now()` 挪进事务（`bigfish/core.py:942`），续租不会写出比现在更早的 `expires_at`。
- 测试证据：`tests/test_v011_rc3.py::test_watchdog_and_heartbeat_race`；`tests/test_v011_p1.py::test_watchdog_survives_sweep_error`（重写后要求异常不杀线程、errors 计数、恢复后仍能把超时任务打成 `BLOCKED`）；`tests/test_v011_rc5_review_audit.py::test_heartbeat_lease_is_in_the_future`、`::test_heartbeat_keeps_lease_alive_repeatedly`。
- rc5 是否改变结论：部分改变。rc3 起「事务内条件更新」已经防住了主要的误杀路径；rc5 补的是时间基准（R4）——此前等锁期间算出的陈旧时间会让租约看起来比实际更早，属于「续租了但按旧时间判超时」的窗口，现在这个窗口关掉了。

### 5. 幂等并发会不会重复执行

- 代码位置：`bigfish/core.py:770`（claim）、`:954`（heartbeat）、`:1025`（submit）三处都在业务事务内用 `_idem_get_locked(..., for_update=True)` 先查缓存（`:1272` 定义、`:1296` `_idem_put` 写入）；`claim` 的状态迁移是条件 `UPDATE`（`:798-802`，失败判定在 `:803-804`），并发下只有一个能拿到 `rowcount == 1`；票按 `(执行器, 任务, 版本, 幂等键)` 确定性派生，重放返回同一张票。
- 测试证据：`tests/test_v011_rc3.py::test_two_threads_claim_same_task_only_one_wins`、`::test_same_idem_key_claim_returns_identical`、`::test_same_idem_key_heartbeat_no_duplicate_events`、`::test_same_idem_key_submit_no_duplicate_runs`；`tests/test_v011_p0.py::test_claim_idempotency_has_no_ticket_or_prompt`；`tests/test_v011_p1.py::test_claim_replay_returns_same_ticket`；`tests/test_v011_rc5_dispatch_id.py::test_replay_returns_same_dispatch_id`、`::test_redispatch_gets_new_dispatch_id`；`tests/test_v011_rc5_receipts.py::test_run_once_writes_claimed_started_finished_in_order`。
- rc5 是否改变结论：幂等语义不变（同一幂等键仍字节级返回同一结果、事件不重复落库）。rc5 新增的是可区分性：响应里带 `dispatch_id`（`bigfish/core.py:838`），同一张票的重放 id 不变、重新派工换新 id，执行端因此能把本地收据和服务端事件对上，不会把「两次一样」和「两次不同」混为一谈。

### 6. 非回环监听入口还有没有

- 代码位置：API 侧 `bigfish/server.py:42` `check_bind_host`（`:52` 抛 `BindError`）下沉到 `:461` `serve` 内部，模块入口 / CLI / GUI / 测试共用同一道校验；地址归一在 `bigfish/config.py:50` `LOOPBACK_HOSTS` 与 `:53` `is_loopback_host`。代理侧 `tools/bfc-proxy.mjs:39` `isLoopback`，`:66` 在 `listen` 之前判定，`:74` 直接 `process.exit(1)`。
- 测试证据：`tests/test_v011_rc3.py::test_all_entry_points_reject_public_bind`；`tests/test_v011_rc5_proxy.py` 全 9 项（`0.0.0.0`、`::`、`[::]`、空串、局域网、公网地址全部拒绝；`127.0.0.1`、`localhost`、`::1`、`[::1]` 与 `127.0.0.0/8` 放行）。`tools/selfcheck.py` 16 项是服务起停、鉴权与 MCP 闭环的自检，不含绑定地址用例，绑定回归只在上面这条 `test_all_entry_points_reject_public_bind` 与 proxy 测试里。
- rc5 是否改变结论：是。rc4 时 API 侧已有闸门，但 `tools/bfc-proxy.mjs` 是唯一直接采信环境变量的入口；rc5 把这最后一个入口补上，且判定放在 `listen` 之前，不存在「先绑上再检查」的窗口。

### 7. 凭据有没有明文持久化

- 代码位置：执行票按 `(执行器, 任务, 版本, 幂等键)` 用 HMAC 派生（`bigfish/security.py:196` `derive_ticket`，`:201` 加 `bfc_tk_` 前缀），入库只写 `hash_token`（`bigfish/security.py:49`）；`tickets` 表只有 `token_hash` 列，明文票只出现在响应体与执行端内存。密钥文件权限 0600（POSIX）。
- 测试证据：`tests/test_v011_p0.py::test_db_file_has_no_plaintext_ticket`（Q1 重写后直接对「主库 + WAL 字节」与「tickets 列值」下硬断言，且要求 `token_hash == hash_token(明文票)`）、`::test_prompt_body_written_to_task_versions`（Q2，证明 prompt 确实落库，反证 Q1 不是靠「没写」通过的）；`tests/test_v011_rc3.py::test_ticket_not_plaintext_in_storage`、`::test_key_file_permissions`；`tests/test_v011_p3.py::test_data_dir_created`（密钥 0600）。
- rc5 是否改变结论：结论不变——票仍不明文落库，库文件本身仍未加密（残余风险见 `docs/SECURITY-REVIEW-v0.1.1-rc5.md` S4）。变的只有证据强度：原来的 `test_db_file_has_no_plaintext_ticket` 对 prompt 那半段是恒真，现在取消了减法写法，改成磁盘字节 + 列值的双向硬断言；同时 R6 把审计导出改成列白名单，外发面的明文也收窄了。

### 8. 跨版本恢复还能不能用

- 代码位置：`bigfish/db.py:22` `SCHEMA_VERSION = 4`、`:23` `BACKUP_FORMAT_VERSION = 4`、`:247` 起拒绝「库版本高于程序版本」并能有序迁移旧库；`bigfish/backup.py:230` `_verify_version` 在 staging 上先验版本与结构（原库一行不动），`bigfish/backup.py:412` `_prepare_staging` 在迁移后的库上执行 R1 的作废动作。
- 测试证据：`tests/test_v011_rc3.py::test_restore_older_schema_migrates`（A2 更新断言：恢复后 `APPROVED` → `BLOCKED` 且 `blocked_reason` 含「冻结」）、`::test_restore_future_schema_rejected`（未来版本 schema 被拒且原库不动）、`::test_restore_corrupted_bundle_rejected`、`::test_restore_failure_keeps_original_db`；`tests/test_v011_rc5_restore_authority.py::test_restore_always_exits_maintenance`、`::test_restore_failure_exits_maintenance`。
- rc5 是否改变结论：是，但方向是加约束。跨版本恢复仍然可用（旧 schema 会迁移到 4），并且 schema 3→4 是单向的，rc5 打开过的库不能再被 rc4 打开；此外恢复旧包时会在迁移后的库上触发 R1，「结构能对齐」升级为「结构对齐 + 授权全部作废 + 身份全部禁用」，恢复不再是一次无副作用的结构操作。

---

## 六、未做的部分

以下为本批次明确没有做的事，列清楚是为了避免把「没做」读成「做了但没写」。

1. **Windows 平台本批次没有跑。** 全部 244 项测试、`selfcheck`、`gui_smoke` 只在 Debian 12.15 / Linux 5.15.0-187-generic / Python 3.11.2 上执行。Windows 专有路径（`msvcrt.locking`、文件替换、ACL 检查、`TerminateProcess` 退出码分支）只有代码路径，没有实机验证记录。
2. **GUI 真窗口没有验收。** `tools/gui_smoke.py` 以 `QT_QPA_PLATFORM=offscreen` 运行、通过 9 项，覆盖的是主窗口构造、五个页面存在与可切换、状态栏文案、待审批任务识别，本质是构造与信号连接，不是人眼看着点一遍。布局、焦点顺序、真实 DPI 下的显示都还没有人验收。
3. **开机自启没有注册。** 没有写计划任务、systemd 单元、注册表 Run 项或启动目录快捷方式，仓库内也没有对应代码。
4. **MCP 没有导入。** 没有向外部 MCP server 注册，也没有把本机连接器导入任何外部目录；`/mcp` 只在测试里被当 HTTP 端点打。
5. **真跨机联调本批次没有重跑。** 没有在 Tailscale 两侧真机之间跑 claim → heartbeat → submit 全链路；本轮所有证据都是单机（同一进程或多进程本机回环）产生的。
6. 没有改生产 DSH、没有开任何对外端口、没有打 tag。

---

## 七、复现与复核命令

```bash
cd /home/dsh/bigfish-council

# 全量 244 项
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

按审计项定位测试：R1 → `tests/test_v011_rc5_restore_authority.py`（21 项）+ `tests/test_v011_rc5_dispatch_id.py`（3 项）；R2 / R3 / R4 / R6 → `tests/test_v011_rc5_review_audit.py`（13 项）；R5 → `tests/test_v011_rc5_receipts.py`（24 项）+ `tests/test_v011_rc5_dispatch_id.py`；R7 → `tests/test_v011_rc5_proxy.py`（9 项，需 `node`）；R8 → `tests/test_v011_rc5_restore_authority.py` + `tests/test_v011_rc5_cli_guard.py`（3 项）；R9 → `tests/test_v011_rc5_key_heal.py`（7 项）。
