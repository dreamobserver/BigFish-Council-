# 安全复审：bigfish-council v0.1.1-rc3（逐项回答）

本文回答任务书第十二节要求的安全复审 9 个问题。每问给：结论 → 代码位置 → 测试证据 →
残余风险。凡本轮未验证的，直接写「未验证」，不模糊。

版本：0.1.1-rc3　平台 A 已验证（Debian 12 / Python 3.11.2），平台 B 未验证（见 TEST-REPORT 第 5 节）。

---

## Q1 能不能绕过审批直接派工？

**结论：不能。** 三道闸门，任何一道不满足都发不出执行票。

**闸门一：状态必须是 APPROVED**

    # bigfish/core.py:630 claim()
    if task["status"] != S.APPROVED:
        raise CouncilError(f"当前状态「{S.label(task['status'])}」不允许领取")
    if task.get("cancel_requested_at"):
        raise CouncilError("任务已被请求取消，不能领取")

**闸门二：审批记录必须对当前版本有效**

approve() 全程在 `with self.db.tx()`（BEGIN IMMEDIATE）内完成，先读任务再条件更新：

    # bigfish/core.py:454 起
    cur = c.execute(
        "UPDATE tasks SET status=?, updated_at=?, blocked_reason=NULL "
        "WHERE id=? AND current_version=? AND status=?",
        (target, t, tid, version, task["status"]))
    if cur.rowcount != 1:
        raise CouncilError("任务在审批过程中被修改，已拒绝本次审批")

同时校验双模型票：

    votes = self._vote_summary_locked(c, tid, version)   # 只统计当前版本的票
    if decision == "approve" and not votes["all_approved"]:
        raise CouncilError("双模型审查未全部通过，不能批准")

**闸门三：MCP 层根本不暴露审批工具**

    # bigfish/server.py:274 TOOL_ORDER
    ("council_list_tasks", "council_claim_task", "council_heartbeat",
     "council_submit_result", "council_task_status")

执行器 token 的权限表里没有 approve / review / human_owner 任何能力，
即便手写 JSON-RPC 也调不到不存在的工具（-32601 未知工具）。

**测试证据**

- tests/test_core.py::test_claim_requires_approval —— 未审批任务 claim 抛错
- tests/test_core.py::test_single_model_vote_not_enough —— 单模型通过不能进 PENDING_APPROVAL
- tests/test_api.py 中 tools/list 断言 5 个工具且无审批类
- tests/test_v011_rc3.py::test_cancel_before_approval_cannot_approve —— CANCELLED 下 approve 被拒
- tests/test_v011_rc3.py::test_revoke_vote_reopens_approval —— 撤票后审批失效，不能再 claim

**残余风险**：`approve` 的 actor 目前由 CLI/GUI 传入（默认 human_owner），没有做
「必须是真人点过鼠标」的强证明。这是设计取舍（本地单机工具），已在
SECURITY-REVIEW 的残余风险表里列出。

---

## Q2 取消后还能不能领取 / 执行？

**结论：不能。** 取消分两种路径，两条都堵死。

**路径一：APPROVED 之前取消 → 直接 CANCELLED（立即生效）**

    # bigfish/core.py:947 request_cancel()
    if status in S.CANCEL_IMMEDIATE:
        new_status = S.CANCELLED
        c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=? AND revoked_at IS NULL", (t, tid))

claim() 的门禁里有两条：状态不是 APPROVED 直接拒；`cancel_requested_at IS NULL` 是
UPDATE 的 WHERE 条件之一，取消过的任务连 rowcount=1 都拿不到。

**路径二：已派工（DISPATCHED/RUNNING）→ 进 CANCELLING，等执行器确认**

    # bigfish/state.py
    CANCEL_NEEDS_ACK = {DISPATCHED, RUNNING}

取消请求只把状态推到 CANCELLING，**不假装远程已停**。之后：

- 心跳返回 `cancel=True, cancel_ack_required=True`，执行器知道要停；
- 执行器回传 `completed` 会被拒（`submit_result` 里 CANCELLING 只接受 `cancelled`）；
- 执行器调 `council_confirm_cancel` 后，服务端才写 CANCELLED；
- 执行器失联（超过 `heartbeat_ttl`）→ Watchdog 把 CANCELLING 转 BLOCKED 等人工，不自动放行。

    # bigfish/core.py:1033 sweep_timeouts()
    base = max(last_beat or 0, cancel_requested_at or 0)
    if now - base > self.heartbeat_ttl:
        -> BLOCKED

**取消与领取并发**：两者都在 BEGIN IMMEDIATE 事务内，且 claim 的 UPDATE 带
`cancel_requested_at IS NULL`，所以只可能有两种结局：claim 先赢（票已签但随后被取消流程吊销）、
或 cancel 先赢（claim 拿不到 rowcount=1 抛错）。没有第三种。

**测试证据**

- tests/test_v011_rc3.py::test_cancel_after_approved_cannot_claim —— 取消后 claim 抛错且 tickets 无新行
- tests/test_v011_rc3.py::test_cancel_and_claim_concurrent —— Barrier 双线程，两种合法结局之外无第三种
- tests/test_v011_rc3.py::test_cancel_while_dispatched_needs_ack —— 四步确认全链
- tests/test_v011_rc3.py::test_cancel_while_dispatched_executor_lost_blocks —— 失联进 BLOCKED
- tests/test_v011_p1.py::test_cancel_while_dispatched_needs_ack（原 test_revoked_ticket_rejected 改写）

---
## Q3 数据库恢复后，旧的执行票会不会复活？

**结论：不会。** 双重保证：密钥轮换 + epoch 校验。

**为什么单靠 revoked_at 不够**

执行票不是随机串，是确定性派生的：

    # bigfish/security.py:190 derive_ticket()
    "bfc_tk_" + hmac.new(key, f"{executor_id}\x1f{task_id}\x1f{version}\x1f{idem or ''}",
                          sha256).hexdigest()[:43]

同一个 (key, executor, task, version, idem_key) 永远算出同一张票。所以只要密钥还是那把，
恢复一份旧快照后，把 tickets.revoked_at 清掉（或快照里本来就是 NULL），旧票立刻又能用。
只置 revoked_at 是堵不住的。

**做法：恢复时轮换派生密钥**

    # bigfish/security.py:130 rotate_key()
    def rotate_key(path):
        new = secrets.token_bytes(32)
        write_key_atomic(path, new)     # O_EXCL 0600 + fsync + os.replace
        return new

    # bigfish/backup.py restore_bundle()
    if key_path is not None:
        old = load_or_create_key(key_path)
        new = rotate_key(key_path)      # 密钥先落盘（原子）
        ...
        db.rotate_epoch()               # 再把 ticket_epoch + 1 写进库

顺序是先换密钥、再换库里的 epoch。中途崩了会怎样：

- 崩在「换密钥之后、换 epoch 之前」：新密钥已生效，库里 epoch 还是旧的 → 旧票验签就不过（密钥变了），安全；
- 崩在「换 epoch 之后」不可能，因为那是最后一步；
- 崩在「换密钥之前」：什么都没变，库也没动，安全。

**epoch 是第二道闸门**

    # bigfish/core.py _check_ticket_row()
    if int(row["epoch"] or 0) != self.db.restore_epoch():
        raise CouncilError("执行票已失效（数据库已从备份恢复），请重新领取")

epoch 存在 meta 表，**不随快照回滚**（恢复后强制重置为 max(快照值, 当前值+1)）。

**同时清掉旧 claim 的幂等记录**

    # bigfish/backup.py _block_inflight()
    DELETE FROM idempotency WHERE scope IN ('claim','heartbeat','submit')

不清的话，执行器拿旧 idem_key 重放 claim，服务端会按缓存里的 version 重新派生一张票
交回去——等于绕过密钥轮换。这条是 rc3 新发现的漏洞，已补。

**在途任务不自动恢复执行**

    # backup.py _block_inflight()
    UPDATE tasks SET status='BLOCKED', blocked_reason='数据库恢复，执行授权已撤销'
    WHERE status IN ('DISPATCHED','RUNNING','CANCELLING')
    UPDATE tickets SET revoked_at=? WHERE revoked_at IS NULL

**测试证据**

- tests/test_v011_rc3.py::test_old_ticket_invalid_after_restore —— 恢复前签发的票恢复后心跳抛错
- tests/test_v011_rc3.py::test_old_claim_idempotency_purged_after_restore —— 幂等表被清、重放拿不回票
- tests/test_v011_rc3.py::test_inflight_task_blocked_after_restore —— DISPATCHED 变 BLOCKED

**残余风险**：如果攻击者同时拿到「密钥文件 + 恢复前的完整快照」，且能阻止恢复流程写 epoch，
理论上可构造旧票。但这需要已具备本地文件写权限，远超本工具的威胁模型（防误操作与执行器越权，
不防本机 root）。

---

## Q4 Watchdog 会不会误杀正在跑的任务？

**结论：不会按事务外快照误杀。** 判断与更新在同一事务、同一条件 UPDATE 里。

**rc2 的问题**：先查任务列表，再逐个 UPDATE，中间任务可能刚回过心跳 → 被误标 BLOCKED。

**rc3 做法**：单事务 + 条件更新，rowcount 不符就跳过。

    # bigfish/core.py:1033 sweep_timeouts()
    with self.db.tx() as c:
        rows = c.execute("SELECT * FROM tasks WHERE status IN ('DISPATCHED','RUNNING','CANCELLING')").fetchall()
        for row in rows:
            last = c.execute("SELECT MAX(created_at) FROM runs WHERE task_id=? AND kind='heartbeat'", (tid,)).fetchone()[0]
            base = max(last or 0, row["cancel_requested_at"] or 0)
            if now - base <= self.heartbeat_ttl:
                continue
            cur = c.execute(
                "UPDATE tasks SET status='BLOCKED', blocked_reason=? "
                "WHERE id=? AND status=? AND updated_at<=?",
                (reason, tid, row["status"], cutoff))
            if cur.rowcount != 1:
                continue      # 期间状态变了，跳过，不报错

因为整段在 BEGIN IMMEDIATE 里，心跳线程在扫描期间拿不到写锁，
不会有「扫描看到旧值→心跳写入→扫描覆盖」的窗口。

**测试证据**

- tests/test_v011_rc3.py::test_watchdog_and_heartbeat_race —— 真实双线程并发 1.2s，任务始终没进 BLOCKED
- tests/test_v011_p1.py::test_watchdog_blocks_without_gui —— 不依赖 GUI 也能超时阻断
- tests/test_v011_p1.py::test_watchdog_survives_sweep_error —— 单次扫描异常不会让看门狗线程死掉

**残余风险**：`heartbeat_ttl` 默认 120s，若执行器侧网络抖动超过这个值，正常任务会被
阻断（进 BLOCKED，需要人工介入，不会自动继续）。这是**故意的**：宁可停下来叫人，不放过。

---
## Q5 幂等并发会不会重复执行？

**结论：不会。** 幂等检查已经挪进业务事务内。

**rc2 的问题**：`_idem_get` 在事务外读，读完再开事务写。两个并发请求都能读到「没有」，
于是各写一次 → 重复执行 / 重复事件 / 重复 Artifact。

**rc3 做法**：先在事务内查，再在同一事务里写。

    # bigfish/core.py _idem_get_locked(c, scope_key, payload)
    row = c.execute("SELECT response, request_hash, expires_at FROM idempotency WHERE key=?", (k,)).fetchone()
    if row:
        if row["request_hash"] and row["request_hash"] != fp:
            raise CouncilError("幂等键被用于不同的请求体")   # 同键换体必须拒
        return loads(row["response"])
    return None

claim / heartbeat / submit_result 三者都是：`with self.db.tx() as c:` → `_idem_get_locked`
→ 业务写入 → `_idem_put_locked` → COMMIT。同一把写锁，串行化。

**唯一约束兜底**：idempotency(key TEXT PRIMARY KEY)。

**重放不产生副作用**

重放直接返回缓存响应，**不**重跑 `log_event`、**不**新建 Run、**不**新建 Artifact。
submit_result 的缓存命中判断特意放在 `_check_ticket_row` **之前**——因为首次提交会吊销票，
如果先查票就会把合法重放误判成「票已被撤销」。

**首次响应丢失怎么办**

服务端已成功、响应在网络里丢了：执行器用**同一个 idem_key** 重试 → 拿到完全一致的历史响应。
executor_client 侧：

    # bigfish/executor_client.py
    class _UncertainNetworkError(RuntimeError): ...
    def safe_submit(...):   # 网络不确定时先 task_status 查最终状态，再决定要不要重发

**测试证据**

- tests/test_v011_rc3.py::test_same_idem_key_submit_no_duplicate_runs —— 两次响应完全相等、artifact 只 1 条、同键换体抛错
- tests/test_v011_rc3.py::test_same_idem_key_heartbeat_no_duplicate_events —— 事件数不增
- tests/test_v011_rc3.py::test_same_idem_key_claim_returns_identical —— 返回同一张票
- tests/test_v011_rc3.py::test_two_threads_claim_same_task_only_one_wins —— Barrier 双线程只有一个赢
- tests/test_v011_p1.py 的心跳幂等三项（rc2 就有，本轮保留）

---

## Q6 能不能把 API 绑到 0.0.0.0 暴露出去？

**结论：不能。** 校验下沉到了真正的启动函数，所有入口共用。

**rc2 的问题**：`__main__.py` 里挡了，但 `python -m bigfish.cli serve --host 0.0.0.0` 绕过。

**rc3 做法**：把校验放进 `serve()` 本身。

    # bigfish/server.py:40 check_bind_host()
    def check_bind_host(host):
        h = (host or "").strip().strip("[]").lower()
        if not is_loopback_host(h):
            raise BindError(REFUSE_MSG)    # 只在回环地址上监听
        return h

    # bigfish/server.py:402 serve()
    def serve(council, host=DEFAULT_API_HOST, port=DEFAULT_API_PORT, watchdog_interval=10.0):
        host = check_bind_host(host)     # 唯一的真源
        ...

拒绝文案原文：`拒绝启动：API 只允许监听回环地址（127.0.0.1/localhost/::1）。如需跨机访问请用 Tailscale Serve 转发，不要直接绑 0.0.0.0。`

调用链：`__main__.py` → serve（校验）；`cli.py serve` → serve（校验）；
`gui.py run()` → 先 check_bind_host 再 open_council（校验）；
`ApiServer.start()` → serve（校验）。四条入口一条不漏。

接受的回环写法：`127.0.0.1`、`localhost`、`::1`、`[::1]`、`127.0.0.2`~`127.255.255.254`。
拒绝：`0.0.0.0`、`::`、`[::]`、`0:0:0:0:0:0:0:0`、任何 RFC1918（10/172.16/192.168）、
任何 CGNAT（100.64/10，即 Tailnet 地址）、任何公网 IP。

**测试证据**

- tests/test_v011_rc3.py::test_all_entry_points_reject_public_bind —— 7 个非回环地址全抛 BindError；
  再用 subprocess 真跑 `-m bigfish --headless --host 0.0.0.0`，断言 rc==2 **且端口仍然可 bind**
  （证明服务确实没起来，不只是打印了错误）
- tests/test_v011_p3.py::test_refuses_public_bind（rc2 就有，保留）

**残余风险**：Tailscale Serve 本身是 tailnet 内可见（不是公网），但 tailnet 内其他设备
只要拿到 token 就能访问控制台。这正是设计意图（让 VPS 上的 DSH 能取任务），
已用执行器 token 的**权限分离**限制其只能操作自己的任务。

---
## Q7 凭据会不会明文落盘 / 进日志 / 进导出包？

**结论：不会。** 三层：只存哈希、写入前统一脱敏、审计导出按表裁列。

**第一层：token 只存哈希**

    # bigfish/security.py
    def hash_token(token):  return hashlib.sha256(token.encode()).hexdigest()
    def verify_token(token, stored): return hmac.compare_digest(hash_token(token), stored or "")

执行器 token 注册时只落 hash；明文只在 `register_executor()` 的返回值里出现一次。
执行票同理：库里存 `token_hash`，明文票只在 claim 响应里出现一次。

**第二层：统一脱敏闸门（不只管幂等表）**

    # bigfish/security.py scrub_text / scrub_obj
    # 覆盖前缀：bfc_tk_（执行票）、bfc_ex_（执行器 token）、bfc_rv_（审查 token）、
    #           bfc_ad_（管理员 token）、sk- 开头的 API Key、Bearer <token>、
    #           以及 PEM 私钥块（-----BEGIN ... PRIVATE KEY-----）
    # 命中一律替换为 <PREFIX>…[REDACTED]

    # bigfish/core.py:944 assert_no_secrets(obj, path="$")
    # 递归检查任意待持久化对象，命中 bfc_tk_ 前缀或键名恰为 prompt 就抛异常

写入点全部过闸：`log_event`、`submit_result`（log/diff/tests/note 四个字段各自 scrub）、
`heartbeat.detail`、Artifact 内容。**先脱敏再写**，没有「先写后擦」的路径。

审计表 audit.detail 也过同一套 scrub。

**第三层：导出包分两种（这是 rc3 的新设计）**

| 类型 | 用途 | 内容 |
| --- | --- | --- |
| KIND_OPERATIONAL | 可恢复的完整操作备份 | 全库 snapshot，**可以**恢复，**不外发** |
| KIND_AUDIT | 可安全外发的脱敏审计包 | 只含 6 张表，禁止恢复 |

    # bigfish/backup.py
    AUDIT_TABLES = ("tasks", "task_versions", "reviews", "approvals", "events", "audit")
    AUDIT_DENY_COLUMNS = ("token_hash", "ticket", "response", "request_hash", "evidence")

被整个丢掉的表：executors（含 token_hash）、tickets（含 token_hash/明文派生材料）、
idempotency（含 response/request_hash）、runs、artifacts、meta。
审计包里字符串再做一遍 scrub。manifest 里写明 `dropped_tables` / `dropped_columns` / `string_scrub`，
让接收方知道剪了什么，而不是靠一句「没导出」的注释。

**测试证据**

- tests/test_v011_rc3.py::test_ticket_not_plaintext_in_storage —— 在 events、runs、**整个 db 文件字节流**里
  都搜不到明文票
- tests/test_v011_rc3.py::test_audit_bundle_internal_tables —— 逐表 JSON 解析后检查每个键名，
  断言 deny 列一个都不在；并断言审计包**不能**用来恢复（KIND_AUDIT 被 restore 拒绝）
- tests/test_v011_p0.py 的「导出包无明文执行票」一组（rc2 就有，保留）

**踩过的坑（写测试时）**：审计包里 manifest 的 `dropped_columns` 字段本身就含 `token_hash` 这个词，
所以**不能**对整包做字节串子串搜索（会假红）。必须逐条 JSON 解析后按**键名**检查。
测试已经这么写。

---

## Q8 跨版本恢复（旧备份 / 新备份 / 损坏包）怎么处理？

**结论：旧版本有序迁移、新版本拒绝、损坏包一律拒，恢复失败不留半个库。**

**版本闸门**

    # bigfish/db.py _migrate_locked()
    if current > SCHEMA_VERSION:
        raise DatabaseVersionError(f"数据库 schema v{current} 高于程序支持的 v{SCHEMA_VERSION}，拒绝打开")
    # current < 版本 → 按 MIGRATIONS 有序迁移，每步一个事务，失败即回滚

恢复流程里的版本检查：读 ZIP 内 manifest 的 schema_version → 与 SCHEMA_VERSION 比：

- 等于：直接替换；
- 小于：先恢复到 staging 库，跑迁移，迁移成功才替换；
- 大于：直接拒，不降级（`DatabaseVersionError`）。

**恢复是 13 步的原子流程**（bigfish/backup.py restore_bundle）

1. 校验 ZIP 结构 → 2. 读 manifest → 3. db_sha256 校验 → 4. sqlite integrity_check
→ 5. schema 版本判定 → 6. 解到**同文件系统**的临时库 → 7. 必要时迁移
→ 8. `_block_inflight`（阻断在途任务、吊销全部票、清 claim 幂等）
→ 9. `before_replace` 钩子（测试用注入点）→ 10. 原库复制到 `.prerestore`
→ 11. `os.replace(staging, target)`（同盘原子）→ 12. 轮换密钥 + epoch+1
→ 13. `db._reopen()` + 复检。

**失败一定回滚，且不会误伤活库**

    swapped = False
    try:
        ...
        os.replace(staging, target)
        swapped = True          # 只有这一步成功后才允许回滚
        ...
    except Exception:
        if swapped and backup_of_original and backup_of_original.exists():
            # 先写 council.db.rollback，再 os.replace 回来（不留半个文件）
            ...
        try: db._reopen()
        except Exception: pass   # 无论如何都要把连接交回去
        raise

rc2 的 bug：无条件回滚会把**还活着**的原库覆盖掉。rc3 用 swapped 标志修掉。

**测试证据**

- tests/test_v011_rc3.py::test_restore_older_schema_migrates —— 把 tickets.epoch / executors.model /
  tasks.cancel_requested_at 三列 DROP 掉、schema_version 置 2，恢复后升到 3 且数据完好
- tests/test_v011_rc3.py::test_restore_future_schema_rejected —— schema 99 被拒
- tests/test_v011_rc3.py::test_restore_corrupted_bundle_rejected —— 假 ZIP（manifest 哈希对不上）、
  只有 manifest 没库文件的包，两种都拒
- tests/test_v011_rc3.py::test_restore_failure_keeps_original_db —— 在 before_replace 注入 RuntimeError，
  事后原库仍可读写

**残余风险**：Windows 上 `os.replace` 遇到被其他进程打开的文件会失败（WinError 32）。
恢复流程已先 `db.close()` 再替换，但**若用户同时开着第二个 GUI 实例**仍可能失败。
这时会抛错并回滚，不会损坏数据——但需要用户关掉另一个实例。这点在
WINDOWS-MANUAL 里写了，未在真机上验证。

---
## Q9 执行器能不能越权看别人的任务 / 提到管理员？

**结论：不能。** 所有权在服务端按 token 绑定，按执行器 id 硬过滤。

    # bigfish/server.py:242 _owned_status()
    def _owned_status(council, ex, task_id):
        task = council.get_task(task_id)
        owner = task.get("executor_id")
        if not owner or owner != ex["id"]:
            raise PermissionError("无权查看该任务")     # 未分配也算无权
        return council.task_status_for(task_id, ex)

rc2 的 bug：`if owner and owner != ex["id"]` —— 未分配（owner 为 None）时直接放行。
rc3 改成 `if not owner or owner != ex["id"]`，未分配也拒。

**五种工具各自的权限边界**

| 工具 | 权限 | 只能看到 |
| --- | --- | --- |
| council_list_tasks | EXECUTOR_CLAIM | 状态 APPROVED 且未取消的任务，**不含 prompt 全文** |
| council_claim_task | EXECUTOR_CLAIM | 只管自己抢到的那一个 |
| council_heartbeat | EXECUTOR_RUN | 只认自己名下的票 |
| council_submit_result | EXECUTOR_RUN | 同上 |
| council_task_status | EXECUTOR_READ | 只有 executor_id == 自己 的任务 |

    # bigfish/server.py _need()
    def _need(ex, permission, action):
        if not roles.can(ex.get("role"), permission):
            raise PermissionError(f"执行器无权执行该操作：{action}")   # → HTTP 403

`TOOL_PERMISSIONS` 里根本没有 approve / review / admin 类权限，所以就算伪造参数也调不到。

**角色绑定不靠名字字符串**

    # bigfish/db.py MIGRATIONS[3]
    "ALTER TABLE executors ADD COLUMN model TEXT",   # gpt / gemini / NULL

审查身份从 executors.model 这条**不可随意改**的列读，不再靠 name 里带 "gpt" 猜。

**吊销竞态**

    # bigfish/core.py claim() / heartbeat() / submit_result() 开头
    self._assert_executor_active(ex_id, c)      # 在事务内重新读 enabled

rc2 的问题：authenticate 通过后管理员吊销，旧请求还能继续完成授权操作。
rc3 在每个关键业务事务**内**重新校验：executor enabled、ticket 未吊销未过期、
task 状态可执行、task 仍属于该执行器。

**测试证据**

- tests/test_v011_rc3.py::test_executor_cannot_read_other_task —— 他人任务 403、未分配任务也 403
- tests/test_v011_rc3.py::test_revoke_and_claim_race —— 吊销与领取并发只有一个合法结果
- tests/test_v011_rc3.py::test_revoke_and_submit_race —— 吊销与回传并发只有一个合法结果
- tests/test_v011_p2.py 四角色权限表 + 执行器边界（rc2 就有，保留）
- tools/selfcheck.py 断言 tools/list 只有 5 个工具、**无任何审批类工具**

---

## 附：本轮明确的残余风险（不掩盖）

| # | 风险 | 影响 | 是否可接受 |
| --- | --- | --- | --- |
| R1 | approve 的 actor 由调用方传入，无生物特征证明 | 本机拿到 CLI/GUI 的人可代批 | 是（单机本地工具，威胁模型不含本机 root） |
| R2 | Windows ACL 实测未做 | icacls 分支未经真机验证 | **已验证**：2026-11-19 公司机实测 `secure_file` → `{'ok': True, 'mode': '0o666', 'acl': 'granted'}`（mode 无效、ACL 生效）、`verify_file_restricted` → `(True, 'acl limited')`、KEYLEN 32 |
| R3 | 恢复时若有第二个 GUI 实例打开 db，os.replace 可能失败 | 抛错 + 回滚，不损坏 | 是（需用户关节点） |
| R4 | Tailnet 内任何有执行器 token 的设备可操作控制台 | 仅限自己被分配的任务 | 是（设计意图） |
| R5 | heartbeat_ttl 默认 120s，网络抖动超时会误阻断 | 进 BLOCKED 需人工，不会自动继续 | 是（Fail-safe 方向正确） |
| R6 | Windows 平台 129 项未跑 | rc3 在 Windows 上未证 | **已验证**：2026-11-19 `Ran 129 tests in 25.263s / OK`、退出码 0、0 失败；并发 7 项 1.663s 通过 |
| R7 | 没有对 DB 文件本身做加密（SQLCipher 等） | 拿到磁盘即可读明文数据 | 是（本地单机，且已排除凭据） |

---

## 附：本轮新旧语义对照（容易被误读的三处）

| 行为 | rc2 | rc3 |
| --- | --- | --- |
| 执行中取消 | 直接标 CANCELLED，假装远程已停 | 进 CANCELLING，等执行器确认；失联进 BLOCKED |
| 撤票后审批记录 | 继续当作有效授权 | 记录保留（历史证据），但 `effective=False`，不能再 claim |
| 恢复后旧票 | 只置 revoked_at，票仍可派生复用 | 轮换密钥 + epoch+1 + 清 claim 幂等，旧票彻底失效 |
