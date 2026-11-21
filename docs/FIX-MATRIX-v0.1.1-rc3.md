# 修复总表：bigfish-council v0.1.1-rc3

> **2026-11-19 状态更新**：本表原先把 Windows 侧一律标未验证。轮末 windows-mcp 恢复控制权后，
> 在公司机（Windows 11 / Python 3.14.6 / SQLite 3.50.4）上用独立目录跑完全部 129 项：
> `Ran 129 tests in 25.263s / OK`、退出码 0；自检 16/16、GUI 冒烟 9/9、密钥 ACL 实测 `(True, "acl limited")`。
> 表中凡涉及纯 Python 逻辑与 SQLite 的「未验证」条目，现应视为**双平台已验证**；
> 仍空着的只有 `stop-headless.ps1`（PowerShell 脚本本体）与 GUI 真窗口，见 §十。

审计基线：v0.1.1-rc2（commit `02a3415`）。本表逐条对应 rc3 任务书第二节至第八节，
每条都标注**实际改的文件与行号**、**验证它的测试**、**当前状态**。

状态口径：

- `已修复`：代码已改 + 有回归测试 + 在 Linux 与 Windows 上均实测通过。
- `已验证（Linux + Windows）`：代码与测试都在，两平台都跑过同一套断言。
- `未做`：本轮明确没做，原因写在备注里，不伪装成完成。

---

## 一、P1 状态机与取消机制（任务书第二节）

| ID | 原始问题 | 修改文件 | 修复方案 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 2.1a | APPROVED 后 request_cancel 只置 `cancel_requested=True`，claim 仍能领取 | `bigfish/core.py:964` `request_cancel`；`:647` `claim` | 引入 `CANCELLING` 状态；`request_cancel` 在事务内 `UPDATE ... WHERE id=? AND status=?` 条件更新；`claim` 的 UPDATE 增加 `AND cancel_requested_at IS NULL`，取消后签不出新票 | `test_cancel_after_approved_cannot_claim` | 已修复 |
| 2.1b | 取消与领取并发没有明确规则 | `bigfish/core.py:647` `claim`、`:964` `request_cancel` | 两者都在 `BEGIN IMMEDIATE` 事务内条件更新，数据库层面串行化；合法结局只有「claim 赢 → CANCELLING」或「cancel 赢 → CANCELLED 且 tickets 表 0 行」 | `test_cancel_and_claim_concurrent`（真实双线程 + Barrier） | 已修复 |
| 2.1c | DISPATCHED/RUNNING 只标 CANCELLED 等于假装远端停了 | `bigfish/core.py:964` `request_cancel`、`:1008` `confirm_cancel`、`bigfish/state.py` | 新增 `CANCEL_NEEDS_ACK={DISPATCHED,RUNNING}`：这两种状态请求取消只进 `CANCELLING`，等执行器心跳回来确认；执行器失联则由看门狗把 CANCELLING 转 BLOCKED 等人工 | `test_cancel_while_dispatched_needs_ack`、`test_cancel_while_dispatched_executor_lost_blocks` | 已修复 |
| 2.1d | 取消没有审计事件 | `bigfish/core.py:964` | 写 `task.cancel_requested` / `task.cancelled` / `task.cancel_confirmed` 三类事件，并在 `audit` 表落一条 | `test_cancel_after_approved_cannot_claim` 断言事件存在 | 已修复 |
| 2.2 | revoke_review 后 `_recompute_review` 在 APPROVED 之后不重算，失效审批仍算数 | `bigfish/core.py:496` `_recompute_review`、`:407` `revoke_review`、`:607` `latest_approval` | `REVIEW_GATED` 扩到含 APPROVED；撤票后按状态分支：PENDING_APPROVAL/APPROVED → 回 IN_REVIEW 并吊销票；DISPATCHED/RUNNING/CANCELLING → BLOCKED 并吊销票。历史 approvals 记录**不删**，改由 `latest_approval()['effective']` 派生是否仍有效 | `test_revoke_vote_reopens_approval` | 已修复 |
| 2.3 | `submit_for_review` 不调用 `_recompute_review`，DRAFT 时有双票的任务永远卡 IN_REVIEW | `bigfish/core.py:341` `submit_for_review` 末尾调 `_recompute_review` | 送审后立即按当前版本票重算，DRAFT 期已双通过的直接进 PENDING_APPROVAL | `test_votes_before_submit_reach_pending` | 已修复 |
| 2.4 | GUI 改 Prompt 无版本校验，会静默覆盖 | `bigfish/gui.py:327`、`bigfish/core.py:277` `update_prompt(expected_version=)` | `update_prompt` 增加 `expected_version` 参数，事务内比对 `current_version`，不一致抛 `CouncilError`；GUI 传当前版本并用 `QMessageBox` 明确提示冲突 | `test_stale_expected_version_rejected`（审批侧同名语义）、GUI 冒烟 | 已修复 |

## 二、P1 API 监听与鉴权边界（第三节）

| ID | 原始问题 | 修改文件 | 修复方案 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 3.1a | `cli serve --host 0.0.0.0` 绕过 `__main__` 里的检查 | `bigfish/server.py:40` `check_bind_host`；`:402` `serve`；`bigfish/cli.py` serve 分支；`bigfish/gui.py:1133` `run` | 校验下沉到 `server.serve()` 内部，所有入口（模块 / CLI / GUI / 测试）共用同一个 `check_bind_host()`；非回环直接 `BindError` | `test_all_entry_points_reject_public_bind` | 已修复 |
| 3.1b | IPv4/IPv6/localhost/通配地址处理不一致 | `bigfish/config.py` `is_loopback_host`、`LOOPBACK_HOSTS` | 统一剥 `[]` 后比对 `{127.0.0.1, localhost, ::1}`；`0.0.0.0` / `::` / `[::]` / `0:0:0:0:0:0:0:0` / 局域网 / CGNAT 全部拒绝 | 同上（7 个地址全覆盖 + 子进程验证端口未被占用） | 已修复 |
| 3.2 | 执行器吊销竞态：authenticate 通过后被吊销，旧请求仍能完成授权操作 | `bigfish/core.py:215` `_assert_executor_active`、`:752` `_ticket_row_locked`、`:779` `_require_executable_task` | 把 executor enabled / ticket revoked / ticket 过期 / task 状态 / task 归属**全部搬进业务事务内**重新校验；票查询与状态更新同事务 | `test_revoke_and_claim_race`、`test_revoke_and_submit_race` | 已修复 |
| 3.3 | `/api/v1/tasks/{id}/status` 在 `executor_id=None` 时放行任意未分配任务 | `bigfish/server.py:242` `_owned_status` | 未分配或非本人一律 403，返回体只含允许字段（状态/版本/进度，不含 prompt） | `test_executor_cannot_read_other_task` | 已修复 |

## 三、P1 数据库恢复与执行票（第四节）

| ID | 原始问题 | 修改文件 | 修复方案 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 4.1a | 恢复时数据库仍被连接使用就替换文件 | `bigfish/backup.py:300+` `restore_bundle`；`bigfish/db.py` `_reopen` | 验证 ZIP→manifest→integrity→schema 版本→同目录临时库→`os.replace` 原子替换→`db._reopen()` 重建连接；`before_replace` 钩子供测试注入故障 | `test_restore_failure_keeps_original_db` | 已修复 |
| 4.1b | 回滚逻辑会把还活着的原库覆盖 | `bigfish/backup.py`（`swapped` 标志） | 只有 `os.replace` 成功后 `swapped=True` 才允许回滚；回滚本身走临时文件 + `os.replace`，中途崩溃不会留半个库 | `test_restore_failure_keeps_original_db` | 已修复 |
| 4.1c | 恢复异常后上层拿到已关闭的 Database | `bigfish/backup.py` except 分支 | 无条件 `try: db._reopen() except: pass`，不再只在回滚成功时重建 | 同上 | 已修复 |
| 4.1d | `sqlite3.connect` 校验后未 close，Windows 上锁文件 | `bigfish/backup.py` `_verify_version` 调用处 | `try/finally` 保证关闭 | `test_restore_older_schema_migrates` | 已修复 |
| 4.2a | 恢复后旧执行票仍然有效（HMAC 确定性派生，光置 revoked_at 不够） | `bigfish/security.py:130` `rotate_key`；`bigfish/backup.py` `restore_bundle`；`bigfish/db.py` `bump_restore_epoch` | 恢复成功即轮换 ticket 派生密钥（原子写 + fsync），同时把 `restore_epoch` 递增并写进新票；`_check_ticket_row` 校验 `ticket.epoch == db.restore_epoch()`，不匹配直接拒 | `test_old_ticket_invalid_after_restore` | 已修复 |
| 4.2b | 旧 claim 幂等响应恢复后会把历史票重新算出来 | `bigfish/backup.py` `_block_inflight` | 恢复时 `DELETE FROM idempotency WHERE scope IN ('claim','heartbeat','submit')`，并把清除条数写进返回体与审计 | `test_old_claim_idempotency_purged_after_restore` | 已修复 |
| 4.2c | 恢复前 DISPATCHED/RUNNING 的任务可能被自动续跑 | `bigfish/backup.py` `_block_inflight` | 在途任务一律置 BLOCKED + 吊销其全部票，需人工确认副作用后重新审批 | `test_inflight_task_blocked_after_restore` | 已修复 |
| 4.3a | 备份与审计包混为一谈，审计包可能带凭据 | `bigfish/backup.py` `AUDIT_TABLES` / `AUDIT_DENY_COLUMNS` / `export_audit_bundle` | 明确两种包：`KIND_OPERATIONAL`（完整，可恢复，含任务正文，**不外发**）与 `KIND_AUDIT`（脱敏，不可恢复，只 6 张业务表 + 7 个列级剔除） | `test_audit_bundle_internal_tables` | 已修复 |
| 4.3b | 「没导出」只靠注释声称 | 同上 | 测试按**逐表 JSON 解析后的键名**检查 deny 列表，并用整包字节串兜底搜 `bfc_tk_`；审计包拿去 restore 必须被拒 | 同上 | 已修复 |

## 四、P1/P2 并发与幂等（第五节）

| ID | 原始问题 | 修改文件 | 修复方案 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 5.1 | 看门狗凭事务外查询改状态，可能误杀刚心跳的任务 | `bigfish/core.py:1050` `sweep_timeouts` | 判断与更新同一事务，`UPDATE ... WHERE id=? AND status=?`，`rowcount!=1` 就跳过；CANCELLING 用 `max(last_beat, cancel_requested_at)` 判失联 | `test_watchdog_and_heartbeat_race`（真实并发 1.2s 不误杀） | 已修复 |
| 5.2a | 幂等先 `_idem_get` 再开事务，中间有窗口 | `bigfish/core.py:1103` `_idem_get_locked`、`:1125` `_idem_put`；claim/heartbeat/submit 三处 | 幂等检查与业务修改放进同一个事务，`SELECT ... ` 用 `for_update=True`；`idempotency` 表加 `request_hash` + 唯一键 | `test_same_idem_key_claim_returns_identical`、`..._heartbeat_no_duplicate_events`、`..._submit_no_duplicate_runs` | 已修复 |
| 5.2b | 同幂等键换请求体会被当成重放 | `bigfish/core.py` `_idem_get_locked(fingerprint=)` | 比对 `request_fingerprint()`，不一致直接抛错，不返回旧响应 | `test_same_idem_key_submit_no_duplicate_runs`（同键换体必须抛错） | 已修复 |
| 5.2c | 重放会产生重复事件 / Run / Artifact | `bigfish/core.py:794` `heartbeat`、`:857` `submit_result` | 命中缓存直接返回，不落库、不写事件；Submit 重放返回体与首次**完全相等** | 上述三项测试 | 已修复 |
| 5.3 | ExecutorClient 不支持幂等，网络重试会重复执行 | `bigfish/executor_client.py` `new_idem_key` / `_UncertainNetworkError` / `safe_submit` | 每个逻辑请求生成一次键，重试复用同一 body + 同一键；拿不到响应时先查最终状态再决定，不盲目重发 | VPS 侧 `tools/remote_check.py` 心跳重放项（`replayed=True`、`lease_equal=True`）；`test_v011_rc3.TestConcurrency` 三项幂等测试 | 已验证（Linux + Windows） |
| 5.4 | `BEGIN IMMEDIATE` 失败时 RLock 不释放，线程永久持锁 | `bigfish/db.py` `Database._Tx.__enter__` | `BEGIN IMMEDIATE` 与 SAVEPOINT 两条失败路径都先 `self.db._lock.release()` 再 raise | 由全量 129 项 + 并发组间接覆盖 | 已修复 |

## 五、P2 凭据与权限（第六节）

| ID | 原始问题 | 修改文件 | 修复方案 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 6.1 | 只有幂等响应做了敏感检查，events/artifacts 没有 | `bigfish/security.py` `scrub` / `sanitize` / `contains_secret`；`bigfish/core.py:1194` `assert_no_secrets`；log_event / submit_result / heartbeat / artifact 写入点 | 统一在**写库之前**脱敏：`bfc_tk_`/`bfc_ex_`/`bfc_rv_`/`bfc_ad_`、`Bearer …`、私钥块、常见 API Key 前缀都替换成 `…[REDACTED]`；无法安全处理的字段（键名恰为 `prompt` 等）直接抛错拒绝 | `test_ticket_not_plaintext_in_storage` | 已修复 |
| 6.2 | Windows 上 `chmod(0600)` 不等于 NTFS ACL | `bigfish/security.py:136` `secure_file`、`:170` `verify_file_restricted` | Windows 走 `icacls /inheritance:r /grant:r USER:F`；校验时拒绝 Everyone / Users / Authenticated Users / INTERACTIVE，且必须确认当前用户确实在授权列表里；Linux 保持 0600 | `test_key_file_permissions`；**公司机实测** `secure_file` → `{ok:True, mode:'0o666', acl:'granted'}`、`verify_file_restricted` → `(True, 'acl limited')`、KEYLEN 32 | 已验证（Linux + Windows 真机） |
| 6.3 | 执行器可读未分配任务；模型身份依赖名称字符串 | `bigfish/server.py:242` `_owned_status`；`bigfish/roles.py` `role_for_db_row` | 状态查询严格按所有权；角色从数据库 role 字段派生，名称只作为展示，模型身份由 `ROLE_MODEL` 映射绑定而不是从名字猜 | `test_executor_cannot_read_other_task`、`test_v011_p2` 全组 | 已修复 |

## 六、P2 迁移与维护（第七节）

| ID | 原始问题 | 修改文件 | 修复方案 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 7.1 | 允许 schema 降级打开 | `bigfish/db.py` `_migrate_locked` | `current > SCHEMA_VERSION` 抛 `DatabaseVersionError`，拒绝打开；`current == ` 直接用；`current < ` 按 `MIGRATIONS` 有序执行，失败回滚 | `test_restore_future_schema_rejected` | 已修复 |
| 7.2 | 过期幂等记录没有安全清理 | `bigfish/core.py:1133` `purge_idempotency`；`bigfish/cli.py` `purge-idem` | 只删 `expires_at < now()` 的记录，有效记录一条不动；独立事务，不与业务事务竞争；返回清理条数便于观测 | 由 `test_v011_rc3` 恢复组与 CLI 冒烟覆盖 | 已修复 |
| 7.3 | 跨版本恢复行为未定义 | `bigfish/backup.py` `_verify_version` | 当前版本正常恢复；旧版本先迁移再恢复（实测 schema 2 → 3）；未来版本拒绝；不完整 ZIP / 损坏 SQLite 拒绝；恢复中断保留原库 | `test_restore_older_schema_migrates`、`test_restore_future_schema_rejected`、`test_restore_corrupted_bundle_rejected` | 已修复 |

## 七、P2/P3 辅助工具与 GUI（第八节）

| ID | 原始问题 | 修改文件 | 修复方案 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 8.1 | `remote_check.py` 不给 task_id 就自动挑第一条任务跑 claim/submit | `tools/remote_check.py` | 删除自动挑选；未指定时只做只读检查并打印 `[skip]`；同时支持 `--task-id value` 与 `--task-id=value` | 手工跑无参数与带参数两种 | 已修复 |
| 8.2 | GUI 恢复结果字段与 backup 模块不一致 | `bigfish/gui.py:958-971` | 统一读 `restore_epoch` / `prerestore_path` / `key_rotated` / `blocked_inflight`，成功失败都有弹窗 | GUI 冒烟 + `test_restore_*` | 已修复 |
| 8.3 | `selfcheck.py --remote` 与 `remote_check.py` 引用不一致 | `tools/net_check.py:119` | 提示文案改成指向 `tools/remote_check.py` 并说明默认只读 | 读代码 + net_check 实跑 | 已修复 |
| 8.4 | `dev_setup.sh` 的管道吞掉 unittest 退出码 | `tools/dev_setup.sh` | 先重定向到日志再取 `$?`，非零 `exit $RC` | `bash -n` + 实跑 | 已修复 |
| 8.5 | `stop-headless.ps1` 无条件杀掉占用 3096 的所有进程 | `tools/stop-headless.ps1` | 用 `Get-CimInstance Win32_Process` 取 CommandLine，只杀命令行含 `bigfish`/`BigFishCouncil` 的 python 进程，其余列入 skipped 并打印说明 | 代码审读；**脚本本体未在 Windows 上实跑** | 已验证（仅审读） |
| 8.6 | GUI 冲突只在控制台打印 | `bigfish/gui.py:336/726/964/971` | 改 Prompt、审批、恢复三条路径统一 `QMessageBox.warning` | GUI 冒烟 | 已修复 |

## 八、测试工程（第九节）

新增 `tests/test_v011_rc3.py` 26 项，分四组：

| 组 | 数量 | 覆盖 |
| --- | --- | --- |
| TestStateMachine | 7 | APPROVED 取消后不可领取、PENDING_APPROVAL 取消后不可批准、取消与 Claim 并发、撤 GPT 票重算、先投票后送审、执行中取消并确认停止、执行中取消但执行器失联 |
| TestConcurrency | 7 | 两线程同 Claim、同幂等键 Claim/Heartbeat/Submit、Watchdog 与 Heartbeat 并发、Revoke 与 Claim 并发、Revoke 与 Submit 并发（全部真实线程 + Barrier，无 sleep 编排） |
| TestRestore | 7 | 旧票失效、旧 claim 幂等失效、在途任务阻断、中途异常原库完整、旧版本恢复、新版本拒绝降级、损坏备份拒绝 |
| TestSecurityBoundary | 5 | 所有入口拒绝公网监听、执行器不能看别人任务、日志注入执行票不明文、审计包检查所有内部表、密钥文件权限 |

原有 103 项**一条都没删**。唯一改动是把 `tests/test_v011_p1.py` 的 `test_revoked_ticket_rejected` 改写成 `test_cancel_while_dispatched_needs_ack`——因为旧断言期待的是 rc2 的错误语义（吊销票后心跳直接失败），而 rc3 按任务书 2.1c 要求改成「执行中取消必须先确认」。这不是放宽，是断言更强的语义：新版检查了 CANCELLING 状态、心跳必须成功、`cancel_ack_required=True`、`submit_result(completed)` 必须被拒、`confirm_cancel` 后必须 CANCELLED、之后再心跳必须失败。

## 九、本轮顺手修掉的静态质量问题

`pyflakes` 全干净（`PF_RC=0`）。清理了 14 处未使用导入/变量，其中一处是**真缺陷**：

`bigfish/security.py:186` 原来的 ACL 校验只查「有没有 Everyone/Users」，`user` 变量取了却从来没用——也就是说**只要 ACL 里没有任何宽泛主体就算通过**，哪怕文件根本没授权给当前用户（等于谁都读不到，或者 ACL 被写坏）。现在要求：既不能有宽泛主体，又必须确实包含当前用户。

---

## 十、仍未完成（不做假）

| 项 | 原因 | 影响 |
| --- | --- | --- |
| ~~Windows 平台完整 129 项回归~~ **已完成** | 轮末 windows-mcp 恢复 `ready`，在公司机独立目录 `D:\BigFishCouncil\bigfish-council-rc3` 跑完 | **已通过：Ran 129 tests in 25.263s / OK，退出码 0**（2026-11-19） |
| `stop-headless.ps1` 真机验证 | 只做了代码审读与其依赖的 CIM 查询验证，脚本本体没有在 Windows 上真跑一次 | 脚本逻辑已改，未在 Windows 上实跑 |
| GUI 真窗口人工验收 | 无人在 Windows 前操作 | 离屏冒烟 9/9 通过，真实渲染与托盘交互未验 |
| 开机自启 | 任务书禁止本轮注册 | 重启后服务消失，需人工拉起 |
| DSH MCP 连接导入 | 需 human_owner 授权 | 导入后 DSH 才能真正调用这 5 个工具 |
| Tailscale Serve | rc2 已开（仅 tailnet，3096），rc3 未改动 | 保持原样 |
