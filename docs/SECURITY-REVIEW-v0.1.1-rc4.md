# 安全复审：bigfish-council v0.1.1-rc4（逐项回答）

本文是 `docs/SECURITY-REVIEW-v0.1.1-rc3.md` 的增量版。格式沿用 rc3：威胁模型 T1-T8、残余风险 R 编号。
每问给：结论 → 代码位置（文件:行 + 实际代码行）→ 测试证据 → 残余风险。没实测的写「未验证」。

| 项 | 值 |
| --- | --- |
| 版本 | `bigfish/__init__.py:6` → `__version__ = "0.1.1-rc4"` |
| 库结构 | `bigfish/db.py:21` → `SCHEMA_VERSION = 3`（rc3 → rc4 无库结构变化） |
| 分支 | `fix/v0.1.1-rc4-hardening` |
| 实测 | Debian 12 / Python 3.11.2：`Ran 162 tests in 26.639s` / `OK`；Windows 未跑（R6） |

实测命令：`LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -t .`
测试分布：test_api 6、test_core 15、test_v011_p0 15、p1 24、p2 24、p3 19、rc3 26、rc4 33 = 162。

## 威胁模型 T1-T8 在 rc4 的现状

| 编号 | 威胁 | rc4 变化 | 现状 |
| --- | --- | --- | --- |
| T1 | 库/备份包泄露，明文执行票外流 | 无变化（HMAC 派生，只存哈希） | 保持 |
| T2 | 执行器伪造审查票 | 汇总改「每模型最新一票含撤销」 | 加强 |
| T3 | 执行器自己批准 | approve 内查 `votes[m]["actor"] == R.EXECUTOR_DSH` | 加强 |
| T4 | 审批跨版本降级 | 增加正向白名单 `EFFECTIVE_APPROVAL_STATES` | 加强 |
| T5 | 恢复把库搞坏 | 库与密钥成对替换/回滚 + 启动自愈 | 加强 |
| T6 | 长期任务被误判死 | 看门狗改按活票查心跳 | 加强 |
| T7 | 网络暴露到公网 | 无变化；新增一条不过闸门的内部构造路径（Q6） | 保持 |
| T8 | 模型身份可被密码学证明 | 无变化，仍明确不实现 | 不实现 |

---

## Q1 审批能不能绕过？

**结论：不能。** 三道闸门；rc4 把第一道改正向白名单、第三道补了执行器投票检查。

```python
# core.py:36-37  正向白名单（rc3 是黑名单，漏排 PENDING_APPROVAL/REVIEW_PASSED/BLOCKED）
EFFECTIVE_APPROVAL_STATES = {S.APPROVED, S.DISPATCHED, S.RUNNING, S.CANCELLING, S.SUBMITTED, S.DONE}
# core.py:684  判定点 →  and task["status"] in EFFECTIVE_APPROVAL_STATES)
# core.py:636-638  闸门一：调用方乐观并发（allowed 见 630-635，return 另加 IN_REVIEW）
if expected_version is not None and version != expected_version:
    raise CouncilError(f"任务版本已变（当前 v{version}，请求基于 v{expected_version}）")
# core.py:653-658  闸门二：SQL 层条件更新，没传 expected_version 也拦得住
cur = c.execute("UPDATE tasks SET status=?, updated_at=?, blocked_reason=NULL"
                " WHERE id=? AND current_version=? AND status=?", (target, t, tid, version, task["status"]))
if cur.rowcount != 1:
    raise CouncilError("任务在审批过程中被修改，已拒绝本次审批")
# core.py:639-647  闸门三：票在事务内读，执行器不能当审查员
votes = self._vote_summary_locked(c, tid, version)
for m in MODELS:
    if votes[m]["actor"] == R.EXECUTOR_DSH:
        raise CouncilError("执行器不能参与审查投票")
```

`_vote_summary_locked`（`core.py:595-605`）收事务连接 `c`，查询**不再带 `revoked_at IS NULL`**：
每模型只取最新一张票（含已撤销），最新票被撤等于立场撤退，不会回落到更早那张赞成票凑「双通过」。
`votes["complete"]` / `votes["all_approved"]` 不满足分别报「当前版本尚未集齐两个模型的审查票」/
「双模型尚未对当前版本同时投 approve，不能批准」。非事务版同逻辑见 `core.py:520-542`。

**测试**：`test_v011_rc4.py::TestReviewBinding::test_revoke_latest_approve_does_not_fall_back`、
`::test_latest_approval_not_effective_after_revoke`、`::test_revoke_after_dispatch_blocks_task`；
`test_core.py::test_cannot_approve_before_review`、`::test_needs_both_models`；
`test_v011_p0.py::test_stale_expected_version_rejected`、`::test_double_approve_rejected`；
`test_v011_p2.py::test_executor_actor_rejected_at_approval`、`::test_only_human_can_approve`；
`test_api.py::test_no_approval_tool_exposed`。**残余风险**：R1。

---

## Q2 取消后能不能被领取？

**结论：不能。** 两条路径堵死，rc4 补的是「条件 UPDATE 的 WHERE 里带取消判定」。

```python
# core.py:731-735  先查取消标记再查状态
if control.get("cancel_requested") or task.get("cancel_requested_at"):
    raise CouncilError("任务已被请求取消，不能领取")
# core.py:751-757  领取语句自身排斥已请求取消的任务
cur = c.execute("UPDATE tasks SET status=?, executor_id=?, updated_at=?"
                " WHERE id=? AND status=? AND current_version=? AND cancel_requested_at IS NULL",
                (S.DISPATCHED, ex_id, t, tid, S.APPROVED, version))
if cur.rowcount != 1:
    raise CouncilError("任务已被其他执行器领取")
```

先 SELECT 再 UPDATE 的竞态由 WHERE 兜住：取消与领取都在 `BEGIN IMMEDIATE` 下串行
（`db.py:390-408`），后到的一方必然看到前者结果。取消侧同样是条件 UPDATE + rowcount 校验
（`core.py:1059-1067`，不符报「任务状态已被其他操作修改，请刷新后重试」），
`target == S.CANCELLED` 时吊销该任务全部活票。

**测试**：`test_v011_rc3.py::test_cancel_after_approved_cannot_claim`、
`::test_cancel_before_approval_cannot_approve`、`::test_cancel_and_claim_concurrent`、
`::test_cancel_while_dispatched_needs_ack`、`::test_cancel_while_dispatched_executor_lost_blocks`；
`test_v011_p1.py::test_cancel_while_dispatched_needs_ack`。
**残余风险**：执行器失联时无法证实副作用真停了，见 R5。

---

## Q3 恢复备份后旧执行票能不能复活？

**结论：不能。** 五道锁：epoch+1、密钥轮换、票里 epoch 校验、幂等表清空、启动自愈。

```python
# backup.py:370  用旧快照回滚也不会把世代倒退
new_epoch = max(captured_epoch, staged_epoch) + 1
# backup.py:472-476  新密钥先落 .restore-new，再与库成对替换（回滚见 536-566）
key_new = Path(str(key_path) + ".restore-new")
write_key_atomic(key_new, new_key_bytes)     # security.py:111-127  O_EXCL 0600 + fsync
# core.py:830-841  票的有效性校验
if t["revoked_at"]:
    raise CouncilError("执行票已被撤销")
epoch = self.db.restore_epoch()
if int(t.get("epoch") or 0) != epoch:
    raise CouncilError("执行票已失效（数据库已从备份恢复），请重新领取")
# backup.py:292-309  _block_inflight：在途置 BLOCKED + 吊销票 + 清幂等
conn.execute("UPDATE tickets SET revoked_at=? WHERE revoked_at IS NULL", (t,))
purged = conn.execute("DELETE FROM idempotency WHERE scope IN ('claim','heartbeat','submit')").rowcount
```

只置 `revoked_at` 不够：claim 重放会用记录里的 nonce 重算票，`claim` 作用域必须一起删；
在途任务置 BLOCKED，reason 是「数据库已从备份恢复，执行授权全部作废，需人工确认副作用后重新审批」。
自愈：`ensure_key_consistency`（`backup.py:593-621`）读 `meta.key_sha256` 与磁盘密钥比对，
不一致则 `rotate_key` + 写回 + 审计 `db.key_healed`，调用点 `runtime.py:14-20`（异常被吞）。

**测试**：`test_v011_rc3.py::test_old_ticket_invalid_after_restore`、
`::test_old_claim_idempotency_purged_after_restore`、`::test_inflight_task_blocked_after_restore`、
`::test_restore_failure_keeps_original_db`；`test_v011_rc4.py::TestMultiRoundTickets::
test_reclaim_after_block_issues_new_ticket`、`::test_old_ticket_revoked_on_new_claim`。
**未验证**：自愈分支无单测（`grep key_healed tests/` 无命中），见 R9。

---

## Q4 Watchdog 会不会误杀？

**结论：不会误杀还在心跳的活票。** rc4 把心跳查询从 task_id 改成 ticket_id。

```python
# core.py:1132-1134  单事务内扫 DISPATCHED/RUNNING/CANCELLING
# core.py:1141-1151  先定位当前活票，再按 ticket_id 查心跳（rc3 按 task_id 会读到上轮遗留心跳）
tk = c.execute("SELECT id FROM tickets WHERE task_id=? AND revoked_at IS NULL"
               " ORDER BY issued_at DESC LIMIT 1", (task["id"],)).fetchone()
run = c.execute("SELECT last_beat_at FROM runs WHERE ticket_id=?"
                " ORDER BY started_at DESC LIMIT 1", (tk["id"],)).fetchone() if tk else None
last = run["last_beat_at"] if run and run["last_beat_at"] else task["updated_at"]
# core.py:1162-1167  条件更新，rowcount 不对就跳过本轮
cur = c.execute("UPDATE tasks SET status=?, blocked_reason=?, updated_at=?"
                " WHERE id=? AND status=?", (S.BLOCKED, reason, t, task["id"], task["status"]))
if cur.rowcount != 1:
    continue          # 期间被心跳/回传改过，本次不处理
```

语义从「这个任务有过心跳」变成「这一轮到底有没有人在跑」。CANCELLING 分支用
`last = max(last, cancel_requested_at)`（`core.py:1153`），避免「刚发取消就判失联」；
判定成立时同时吊销该任务所有活票。

**测试**：`test_v011_rc3.py::test_watchdog_and_heartbeat_race`；
`test_v011_p1.py::test_watchdog_blocks_without_gui`、`::test_watchdog_survives_sweep_error`、
`::test_watchdog_singleton_per_db`、`::test_heartbeat_extends_lease`、`::test_expired_lease_rejected`；
`test_core.py::test_heartbeat_timeout_blocks`。**残余风险**：阈值本身仍会误阻断，见 R5。

---

## Q5 幂等并发会不会重复执行？

**结论：不会。** 幂等读、业务写、幂等写在同一个事务里；同键异体显式拒绝。

```python
# core.py:1199-1218  事务内读幂等记录
if fingerprint is not None and row["request_hash"] and row["request_hash"] != fingerprint:
    raise CouncilError("同一幂等键被用于不同请求，已拒绝")
parts = key.split(":", 3)          # rc4：maxsplit=3，全量 split 会截断含冒号的客户端键
if len(parts) == 4 and parts[0] == "claim":
    data.setdefault("_idem", "" if parts[3] == "-" else parts[3])
# core.py:720-725  claim 内整段在事务里，读幂等用 for_update
with self.db.tx() as c:
    cached = self._idem_get_locked(c, idem_scope_key, fp, for_update=True)
    if cached is not None:
        return self._replay_claim(cached, tid, ex_id)
# core.py:1223-1229  写入前先过敏感字段闸门再 INSERT OR REPLACE
assert_no_secrets(response)
```

submit 的幂等重放**先于**票有效性校验（`core.py:952-957`），否则「首次提交吊销票」
会让重放永远拿不到历史结果。

**测试**：`test_v011_rc4.py::TestIdempotencyContract::test_idem_key_with_colon_replays_same_ticket`、
`::test_idem_key_with_colon_does_not_issue_second_ticket`、`::test_same_key_different_body_rejected`、
`::test_replay_does_not_duplicate_events`、`::test_concurrent_same_key_heartbeat_runs_once`；
`test_v011_rc3.py::test_same_idem_key_claim_returns_identical`、
`::test_two_threads_claim_same_task_only_one_wins`；
`test_v011_p1.py::test_key_reuse_with_different_body_rejected`。
**残余风险**：幂等记录 TTL 24h（`core.py:1224` 默认 `ttl=86400`），过期后同键重放当新请求。

---

## Q6 有没有非回环监听入口？

**结论：四条入口全部经 `check_bind_host`；另有一条内部构造路径不过闸门。**

```python
# server.py:41-54
if not is_loopback_host(h):
    raise BindError("拒绝启动：API 只允许监听回环地址（127.0.0.1/localhost/::1）。"
                    "如需跨机访问请用 Tailscale Serve 转发，不要直接绑 0.0.0.0。")
```

| 入口 | 位置 | 过闸门 |
| --- | --- | --- |
| `serve()` | `server.py:446-456` `host = check_bind_host(host)` 后才 `uvicorn.run` | 是 |
| `gui.run()` | `gui.py:1312` 校验，失败 `return 2` | 是 |
| `cli serve` | `cli.py:123-128` 直接调 `serve()`（注释：校验在 serve() 内统一执行） | 是（经 serve） |
| `python -m bigfish` | `__main__.py:32-37` 先校验再开库 | 是 |

反例：`ApiServer.start()`（`gui.py:97-104`）直接 `uvicorn.Config(app, host=self.host, ...)`，
自身不校验；构造点 `gui.py:1163` 传 `settings.api_host`。正常路径上它已过 `gui.run` 校验，
设置对话框只改端口（`gui.py:1032-1051`），所以可被用户触发的入口都拦住了；
**直接构造 `MainWindow` 的代码（测试、脚本）会绕过校验**，这层没有纵深防御。

**测试**：`test_v011_rc3.py::test_all_entry_points_reject_public_bind`（子进程实测
`--host 0.0.0.0` 退出码 2、stderr 含「拒绝启动」、端口未被占用）；
`test_v011_p3.py::test_refuses_public_bind`、`::test_refuses_tailnet_bind`、
`::test_default_host_is_loopback`、`::test_non_loopback_detected`、`::test_env_override_wins`。
**残余风险**：R10。

---

## Q7 凭据会不会明文落库 / 落日志 / 进导出包？

**结论：不会。** 三层闸门 rc4 都保留，导出裁列规则未放宽。

```python
# ① 只存哈希：core.py:200-203  authenticate() 返回前 d.pop("token_hash", None)
# ② 票是 HMAC 派生，不落明文 —— security.py:196-201
material = f"{executor_id}\x1f{task_id}\x1f{version}\x1f{idem_key or ''}".encode("utf-8")
return "bfc_tk_" + hmac.new(key, material, hashlib.sha256).hexdigest()[:43]
# ③ 脱敏正则六条（bfc_/Bearer/sk-/gh[pousr]_/PEM/AKIA）—— security.py:34-42；下文为节选
if any(p in text for p in SECRET_PREFIXES):   # security.py:87-93 连被截断的前缀也算，宁可拒写
# core.py:1292-1303
if contains_secret(obj) or path.rsplit(".", 1)[-1] == "prompt":
    raise CouncilError(f"拒绝持久化敏感字段：{path}")
# ④ 导出包按表裁列 —— backup.py:52 / 149
AUDIT_DENY_COLUMNS = ("token_hash", "ticket", "response", "request_hash", "evidence")
```

脱敏审计包整表丢弃 `executors` / `tickets` / `idempotency` / `runs` / `artifacts` / `meta`，
manifest 写明 `dropped_tables` 与 `dropped_columns`（`backup.py:170-175`）；完整操作备份明写
`"contains_credential_hashes": True` 与 `"handling": "敏感：…不得外发。"`（`backup.py:113-117`）。

**测试**：`test_v011_rc3.py::test_ticket_not_plaintext_in_storage`（events / runs / db 字节流）、
`::test_audit_bundle_internal_tables`、`::test_key_file_permissions`；
`test_v011_p0.py::test_guard_rejects_secret_fields`、`::test_claim_idempotency_has_no_ticket_or_prompt`、
`::test_db_file_has_no_plaintext_ticket`、`::test_bundle_has_no_ticket`；
`test_v011_p2.py::test_token_hash_not_returned`、`::test_audit_not_exposed_over_http`。
**残余风险**：库文件不加密（R7）；`scrub` 是正则白名单，未知格式密钥不命中。

---

## Q8 跨版本恢复边界？

**结论：高版本拒绝、低版本迁移、损坏包拒绝、失败不留半个库。**

```python
# db.py:224-238  先只读探测版本，再动 journal_mode（此前一上来切 WAL = 先改文件头再报错）
if ver > SCHEMA_VERSION:
    raise DatabaseVersionError(f"数据库 schema 版本 {ver} 高于本程序支持的 {SCHEMA_VERSION}，"
                               "拒绝打开（升级程序，不要降级数据库）")
# backup.py:203-208  恢复前的版本闸门：高版本备份直接判失败
# backup.py:188-200  完整性 + 行数对齐
if str(verdict).lower() != "ok":
    return False, f"integrity_check 失败：{verdict}"
for t, want in expected.items():
    if counts.get(t) != want:
        return False, f"表 {t} 行数不一致：备份 {want} / 实际 {counts.get(t)}"
```

损坏包在更早处被拒：`inspect_bundle` 校验 zip 结构、manifest 可解析、`db_sha256` 与文件一致
（`backup.py:237-280`）。低版本备份走 `staged.init_schema()` 在 staging 上迁移，成功才替换
（`backup.py:452-457`），原库全程未被触碰。

**测试**：`test_v011_rc3.py::test_restore_future_schema_rejected`、
`::test_restore_older_schema_migrates`、`::test_restore_corrupted_bundle_rejected`、
`::test_restore_failure_keeps_original_db`；`test_v011_p0.py::test_corrupted_bundle_does_not_touch_db`、
`::test_version_drift_rejected`、`::test_restore_keeps_prerestore_copy`。
**残余风险**：Windows 上 `os.replace` 的文件占用问题，见 R3。

---

## Q9 rc4 新引入的风险面

### 9.1 `run_in_threadpool` 会不会让 SQLite 并发写更混乱？

**结论：不会更混乱，代价是线程排队。**

```python
# server.py:239-240  SQLite 是同步阻塞调用，直接跑会卡住事件循环
content = await run_in_threadpool(dispatch_tool, council, ex, name, args)
# db.py:216-238  RLock + check_same_thread=False + busy_timeout=5000 + WAL
self._lock = threading.RLock()
# db.py:390-408  所有写事务拿同一把锁再 BEGIN IMMEDIATE；读也走 with self._lock（370-380）
self.db._lock.acquire()
if self.db._depth == 0:
    try:
        self.db._conn.execute("BEGIN IMMEDIATE")
    except Exception:
        self.db._lock.release()   # BEGIN 失败必须还锁，否则整库写操作永久卡死
        raise
```

改前 `dispatch_tool` 在 loop 线程同步跑，一次慢查询卡住整个 loop（`/health` 也不响应）；
改后多线程进来，但写事务串在同一把 `RLock` + `BEGIN IMMEDIATE` 上 → **排队串行**，
不是并发写同一连接。代价两处：① 线程池线程被 `RLock` 阻塞，anyio asyncio 默认上限 40
（`anyio/_backends/_asyncio.py:3257` `CapacityLimiter(40)`），槽位打满后新请求只能等；
② 长事务同时压住读路径，`/health` 一起排队。

**未验证**：没做并发压测（50 路心跳 + 领取混合），没测线程池打满后的 P99 与 `heartbeat_ttl`
的关系。这是 rc4 唯一改变运行时并发模型的地方，只有
`test_concurrent_same_key_heartbeat_runs_once`（两线程）作间接证据。

### 9.2 `safe_claim` / `safe_heartbeat` 的重试会不会放大请求？

**结论：请求数最多放大到 2 倍，不放大执行。**

```python
# executor_client.py:164-173 / 175-185  两次重试复用同一幂等键
    key = idem or new_idem_key("claim")
    try:
        return self.claim(task_id, idem=key)
    except _UncertainNetworkError:
        if retries <= 0:
            raise
        return self.safe_claim(task_id, idem=key, retries=retries - 1)
# executor_client.py:56  最多 retries+1 次物理请求
for attempt in range(retries + 1):
# executor_client.py:60-62  确定性拒绝不重试
if e.code in (401, 403, 409, 404, 422):
    raise RuntimeError(f"HTTP {e.code}: {detail}") from None
# executor_client.py:142  BLOCKED 不算成功
RECONCILED_OK = ("SUBMITTED", "DONE", "CANCELLED")
```

服务端第二次走 `_replay_claim` / 幂等重放，不产生第二张票或第二条事件。写操作遇网络错误
直接抛 `_UncertainNetworkError` 交给调用方核对（`executor_client.py:70-76`），不盲目重发；
`safe_submit` 要求显式 `task_id`。**残余风险**：两次尝试间固定 `time.sleep(0.6)`
（`executor_client.py:75-76`），大批执行器同时抖动会形成同步重试波。

**测试**：`test_v011_rc4.py::TestExecutorClient::test_safe_claim_reuses_same_key`、
`::test_blocked_is_not_treated_as_success`、`::test_safe_submit_requires_task_id`、
`::test_submitted_is_treated_as_success`。

### 9.3 `PORT_RESERVED` 里 3099-3101 未实现却占号，会不会撞？

**结论：占号是声明式的，当前不冲突；18765 / 18766 在本机已被外部进程占用。**

```python
# config.py:33-51  注释：预留即「先占号，不实现」；不并入 PORT_ROLES
#   因为 tests/test_v011_p3.py 断言 PORT_ROLES 恰好是当前三行职责
PORT_RESERVED = {3099: ("mobile_rest", …), 3100: ("mobile_sse", …), 3101: ("mcp_bridge", …),
                 8765: ("windows_mcp", …), 18765: ("wmcp_proxy_a", …), 18766: ("wmcp_proxy_b", …)}
```

`PORT_ROLES` 仍三项（`config.py:26-30`），`PORT_RESERVED` 不参与任何监听。
本机实测（对 127.0.0.1 逐个 `bind`）：3094 / 3096 / 3097 / 3098 / 3099 / 3100 / 3101 / 8765 可绑定；
**3095、18765、18766 已被占用（errno 98）**。`3099-3101` 在代码里没有任何监听点
（`grep 3099|3100|3101` 只命中 `config.py` 与 `tools/selfcheck.py` 注释），占号只影响文档与
后续实现；真实风险是别人先占：`tools/selfcheck.py:19` 原默认 3099，rc4 改成 3094，
注释写明 3095 被本容器 quota-hub 长期占用（实测证实）。

**测试**：`test_v011_rc4.py::TestAuxiliary::test_reserved_ports_do_not_collide`。
**残余风险**：R11。

### 9.4 `roles.DB_ROLES` 白名单是否真的拦住了未知 role？

**结论：没有。`DB_ROLES` 只被测试引用，注册路径用的是另一份硬编码元组。这是真实缺口。**

```python
# roles.py:40-48  注释称「在构造时拒绝未知 role（见 register_executor）」
_DB_ROLE_MAP = {"executor": EXECUTOR_DSH, "reviewer": None, "admin": HUMAN_OWNER}
DB_ROLES = frozenset(_DB_ROLE_MAP)      # 「允许出现在 executors.role 列里的取值」
# core.py:153-162  实际校验的是内联元组，不是 R.DB_ROLES
def register_executor(self, name: str, role: str = "executor", model: str | None = None) -> dict:
    ...
    if role not in ("executor", "reviewer", "admin"):
        raise CouncilError("role 只能是 executor / reviewer / admin")
# roles.py:74-83  未知 db_role 的兜底
return EXECUTOR_DSH        # 注释：未知角色一律按最低权限处理
```

`grep -rn DB_ROLES --include=*.py` 全库只有三处：`roles.py:48` 定义、
`tests/test_v011_rc4.py:466-468`（`assertIn("executor")` / `assertIn("admin")` /
`assertNotIn("mobile")`）。**生产代码零引用。** 后果两条，都是真的：

1. 往 `_DB_ROLE_MAP` 加新 role（将来实现 mobile）却忘改 `core.py:161`，注册被拒，
   报错文案还是「role 只能是 executor / reviewer / admin」，与白名单不一致。
2. 反过来，`core.py:161` 的元组一旦放宽，`DB_ROLES` 依然拦不住任何东西。

`role_for_db_row` 对未知 `db_role` 返回 `EXECUTOR_DSH`：方向是「最小权限」，但库里出现未知
role 时鉴权不报错、静默降级成执行器，还能领任务。注册路径本该拦住这种值，实际没用同一份白名单。

**处置建议（本轮未改代码）**：`core.py:161` 改为 `if role not in R.DB_ROLES:`，
并补「注册未知 role 抛错」单测。当前状态按「白名单只用于展示与测试，注册路径未强制」记录。

**测试**：`test_v011_rc4.py::TestAuxiliary::test_roles_db_whitelist` 只断言集合内容，
不涉及 `register_executor` 校验行为；`test_v011_p2.py::test_four_canonical_roles` 断言的是
业务角色表，与 `DB_ROLES` 无关。

---

## Q10 残余风险逐条现状

| # | 风险 | rc4 现状 | 判定 |
| --- | --- | --- | --- |
| R1 | `approve` 的 actor 由调用方传入，无生物特征证明 | 未变。`core.py:620-621` 只校验 `actor in (R.HUMAN_OWNER,)`；新加的 `votes[m]["actor"] == R.EXECUTOR_DSH` 挡的是「执行器投票」不是「谁点的鼠标」 | 部分（设计取舍） |
| R2 | Windows ACL 实测 | rc4 未重测，代码未改（`security.py:136-193`），rc3 的 2026-11-19 公司机结果仍适用 | 已验证（沿用 rc3） |
| R3 | 第二个 GUI 实例打开 db 时 `os.replace` 失败 | 有回滚兜底（`backup.py:536-566`）；但 GUI 恢复调用 `restore_from_bundle(path)` 没传 `before_replace`（`gui.py:1083`），恢复期间 API 线程与看门狗未停 | 部分 |
| R4 | Tailnet 内有执行器 token 的设备可操作控制台 | 未变；5 个工具、`TOOL_PERMISSIONS` 无审批类（`server.py:396-403`） | 已接受 |
| R5 | `heartbeat_ttl` 默认 120s，抖动超时会误阻断 | 未变（`config.py:87`）；判定精度改善（按活票查心跳），阈值未动 | 部分（Fail-safe） |
| R6 | Windows 平台未跑 | rc4 未在 Windows 执行，本轮只跑 Debian 12 / Python 3.11.2 的 162 项 | 未验证 |
| R7 | DB 文件本身未加密 | 未变；manifest 明写「敏感…不得外发」，库内已排除凭据明文 | 已接受 |
| R8 | `run_in_threadpool` 引入多线程，长事务阻塞读路径 | 新增。写路径靠同一把 `RLock` + `BEGIN IMMEDIATE` 串行，正确性有保证；吞吐与延迟未压测 | 未验证 |
| R9 | `ensure_key_consistency` 自愈分支无单测 | 新增。`grep key_healed tests/` 无命中；`runtime.py:20` 吞异常不阻止启动 | 未验证 |
| R10 | `ApiServer.start()` 不过 `check_bind_host` | 新增（构造点 `gui.py:1163`）。可触发入口都过闸门；直接构造 `MainWindow` 的代码会绕过 | 部分 |
| R11 | 3099-3101 未实现即占号；18765/18766 已被外部占用 | 新增。实测 3099-3101 可绑定、18765/18766 已占用；3099 与 selfcheck 的冲突已改默认端口解决 | 部分 |
| R12 | `DB_ROLES` 白名单未接入注册路径 | 新增，真实缺口。`core.py:161` 用内联元组；`DB_ROLES` 零生产引用；未知 role 静默降级成 `EXECUTOR_DSH` | 未修（见 9.4） |

R 表条目数：**12 条**（R1-R7 沿用 rc3 编号并更新现状，R8-R12 为 rc4 增量）。

### rc4 明确没做的安全项

| 项 | 说明 |
| --- | --- |
| `DB_ROLES` 接入 `register_executor` | 见 9.4 / R12，白名单只用于展示与测试 |
| `run_in_threadpool` 并发压测 | 见 R8，只有两线程功能测试，无 P99 数据 |
| `ensure_key_consistency` 单测 | 见 R9，自愈逻辑靠代码审查 |
| GUI 恢复时停服（`before_replace`） | 见 R3，参数支持但 GUI 调用点没传 |
| Windows 平台实测 | 见 R6，33 项新测试只在 Debian 12 跑过 |
| 数据库文件级加密（SQLCipher 等） | 见 R7，rc3 起就是明确排除项 |
| 执行票绑定来源 IP / 设备指纹 | 票只绑 executor + task + version + nonce，rc3 起明确排除 |
| 审批的操作系统级身份证明 | 见 R1 |
| 移动端角色与鉴权 | 只留三个预留端口号与 `ARCHITECTURE-FUTURE.md` 的契约草案 |

### 语义变更（rc4 改了判定口径的地方）

| 行为 | rc3 | rc4 |
| --- | --- | --- |
| 撤掉某模型最新赞成票 | 过滤撤销票，回落到更早的赞成票 | 每模型只取最新一票（含撤销），撤票即撤退立场 |
| 审批有效性判定 | 黑名单排状态，漏排三个状态 | 正向白名单 `EFFECTIVE_APPROVAL_STATES` |
| 无幂等键重复领取 | 同 (执行器,任务,版本) 派生完全相同的票 | 随机 nonce，每次新票，旧票吊销 |
| 看门狗心跳查询 | 按 `task_id` 取最新 run | 按当前活票的 `ticket_id` 取 run |
| 含冒号的幂等键 | 全量 split 取 `parts[3]`，被截断 | `split(":", 3)` 保留完整键 |
| `verify(accept=True)` | 未收紧 | 只接受 SUBMITTED（`core.py:1082-1107`），BLOCKED 不能点成 DONE |
