# 代码 Diff 摘要：v0.1.1-rc2 → v0.1.1-rc3

基线 commit '02a3415'（master）。本轮全部改动在分支 'fix/v0.1.1-rc3-hardening'。
规模：19 个文件改动 + 1 个新文件，1421 insertions(+), 427 deletions(-)（不含新增测试文件）。

    bigfish/__init__.py        |   2 +-
    bigfish/__main__.py        |  28 ++-
    bigfish/backup.py          | 352 +++++++++++++++++++-------
    bigfish/cli.py             |  67 ++++--
    bigfish/config.py          |  21 +-
    bigfish/core.py            | 571 +++++++++++++++++++++++++++++---------
    bigfish/db.py              | 167 +++++++++---
    bigfish/executor_client.py | 102 ++++++--
    bigfish/gui.py             |  35 ++-
    bigfish/roles.py           |  89 ++++---
    bigfish/runtime.py         |  19 +-
    bigfish/security.py        | 150 +++++++++-
    bigfish/server.py          |  85 +++++--
    bigfish/state.py           |  26 ++-
    tests/test_v011_p1.py      |  21 +-
    tools/dev_setup.sh         |  12 +-
    tools/net_check.py         |   2 +-
    tools/remote_check.py      |  49 +++-
    tools/stop-headless.ps1    |  50 +++-
    tests/test_v011_rc3.py     |  新增（约 640 行，26 项）

下面只列改了语义的代码，不含格式化与注释。

---

## 1. bigfish/state.py — 新增 CANCELLING 与状态集合

    # rc3 新增
    CANCELLING = "CANCELLING"

    TERMINAL         = {DONE, REJECTED, CANCELLED}
    CANCEL_IMMEDIATE = {DRAFT, IN_REVIEW, REVIEW_PASSED, PENDING_APPROVAL,
                        APPROVED, RETURNED, SUBMITTED, BLOCKED}
    CANCEL_NEEDS_ACK = {DISPATCHED, RUNNING}          # 必须先让执行器确认停
    RUNNING_STATES   = {DISPATCHED, RUNNING, CANCELLING}
    REVIEW_GATED     = {IN_REVIEW, REVIEW_PASSED, PENDING_APPROVAL, APPROVED}  # rc3 加了 APPROVED

REVIEW_GATED 加进 APPROVED 是 P1-2.2 的关键：只有把 APPROVED 也纳入「可被重算」的范围，
撤票才能把它降级回 IN_REVIEW。

---

## 2. bigfish/core.py — 状态机主体（571 行改动，最大一块）

### 2.1 claim() 三级门禁（:647）

    with self.db.tx() as c:                                  # BEGIN IMMEDIATE，全段在事务内
        self._assert_executor_active(c, executor)            # 事务内复验执行器仍启用
        if idem_key:
            cached = self._idem_get_locked(c, idem_scope_key, fp, for_update=True)
            if cached is not None:
                return self._replay_claim(cached, tid, ex_id)
        ...
        if control.get("cancel_requested") or task.get("cancel_requested_at"):
            raise CouncilError("任务已被请求取消，不能领取")
        if task["status"] != S.APPROVED:
            raise CouncilError(f"任务当前状态「{S.label(task['status'])}」不可领取")
        ...
        cur = c.execute(
            "UPDATE tasks SET status=?, executor_id=?, updated_at=?"
            " WHERE id=? AND status=? AND current_version=?"
            " AND cancel_requested_at IS NULL",                 # rc3 新增这一条
            (S.DISPATCHED, ex_id, t, tid, S.APPROVED, version))
        if cur.rowcount != 1:
            raise CouncilError("任务已被其他执行器领取")

三处改动：① 幂等查询挪进事务并用 for_update；② 显式拒绝已取消任务；
③ UPDATE 条件里加 cancel_requested_at IS NULL，取消与领取在数据库层面互斥。

### 2.2 request_cancel() / confirm_cancel()（:964 / :1008）

    def request_cancel(self, tid, actor=R.HUMAN_OWNER):
        with self.db.tx() as c:
            task = ...
            if task["status"] in S.TERMINAL:
                raise CouncilError("任务已结束")
            if task["status"] == S.CANCELLING:
                return task                                  # 幂等
            target = S.CANCELLING if task["status"] in S.CANCEL_NEEDS_ACK else S.CANCELLED
            cur = c.execute(
                "UPDATE tasks SET control=?, status=?, cancel_requested_at=?"
                " WHERE id=? AND status=?",                  # 条件更新，并发只一个赢
                (dumps(control), target, t, tid, task["status"]))
            if cur.rowcount != 1:
                raise CouncilError("任务状态已变化，取消请求未生效")
            if target == S.CANCELLED:
                c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=? AND revoked_at IS NULL",
                          (t, tid))                          # 立即吊销全部票
        self.log_event(tid, "task.cancelled" if target == S.CANCELLED else "task.cancel_requested")

confirm_cancel() 由执行器在停下之后调用，把 CANCELLING 推到 CANCELLED；
失联则由看门狗把 CANCELLING 转 BLOCKED。

### 2.3 _recompute_review() 覆盖 APPROVED（:496）

    # rc2：只对 IN_REVIEW / REVIEW_PASSED 生效，APPROVED 之后撤票不重算
    # rc3
    if task["status"] not in S.REVIEW_GATED:      # 现在含 APPROVED
        return
    votes = self._vote_summary_locked(c, tid, version)
    if votes["all_approved"]:
        ...  # IN_REVIEW / REVIEW_PASSED -> PENDING_APPROVAL
    else:
        if task["status"] in (S.PENDING_APPROVAL, S.APPROVED):
            c.execute("UPDATE tasks SET status=? WHERE id=? AND status=?",
                      (S.IN_REVIEW, tid, task["status"]))
            c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=? AND revoked_at IS NULL", (t, tid))
            events.append(("approval.invalidated", "撤票导致审批失效"))

### 2.4 revoke_review() 按状态分支（:407）

    if task["status"] in (S.PENDING_APPROVAL, S.APPROVED):
        target = S.IN_REVIEW
    elif task["status"] in (S.DISPATCHED, S.RUNNING, S.CANCELLING):
        target = S.BLOCKED                       # 已派工：吊销执行授权并阻断

### 2.5 latest_approval() 增加 effective 派生（:607）

    def latest_approval(self, tid, with_effective=True):
        row = self.db.one("SELECT * FROM approvals WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,))
        if row is None:
            return None
        d = dict(row)                            # 历史记录永不删除（证据链）
        if with_effective:
            task = self.db.one("SELECT status, current_version FROM tasks WHERE id=?", (tid,))
            d["effective"] = bool(
                task is not None
                and d["version"] == task["current_version"]
                and d["decision"] == "approve"
                and task["status"] not in (S.DRAFT, S.IN_REVIEW, S.RETURNED,
                                           S.REJECTED, S.CANCELLED))
        return d

这一处是测试逼出来的真设计问题：撤票后审批记录必须留着当证据，
但不能再被当成有效授权。

### 2.6 sweep_timeouts() 条件更新（:1050）

    with self.db.tx() as c:
        for row in c.execute("SELECT * FROM tasks WHERE status IN (?,?,?)", (...)):
            deadline = ...
            cur = c.execute(
                "UPDATE tasks SET status=?, blocked_reason=?, updated_at=?"
                " WHERE id=? AND status=?",                # 事务内二次确认状态未变
                (S.BLOCKED, reason, t, tid, task["status"]))
            if cur.rowcount != 1:
                continue                                   # 别人刚改过，跳过不误杀

### 2.7 幂等：事务内检查 + 请求指纹

    def _idem_get_locked(self, c, key, fingerprint=None, for_update=False):
        row = c.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if fingerprint and row["request_hash"] and row["request_hash"] != fingerprint:
            raise CouncilError("同一幂等键被用于不同请求")
        return loads(row["response"])

（SQLite 没有 FOR UPDATE，串行化靠 BEGIN IMMEDIATE 拿写锁；for_update 参数只作语义标记。）

### 2.8 恢复：restore_from_bundle() 走 backup 层

    def restore_from_bundle(self, path, before_replace=None):
        out = restore_bundle(self.db, path, key_path=self.key_path,
                             before_replace=before_replace)
        self.key = load_or_create_key(Path(self.key_path))   # 换了密钥，内存里的也要换
        return out

---

## 3. bigfish/backup.py — 恢复重构（352 行）

核心顺序（restore_bundle）：

    1.  解 zip 到同目录 staging（original_dir / (target.name + ".restore-new")）
    2.  probe = sqlite3.connect(staging); _verify_version(probe)  ← try/finally close
    3.  Database(staging).init_schema()        # 旧版本备份在这里有序迁移
    4.  _verify_snapshot + _verify_relational  # 关系完整性
    5.  db.snapshot(council.db.prerestore)     # 当前库留一份可回滚副本
    6.  before_replace()                       # 测试注入故障的钩子
    7.  db.close() + 删 -wal/-shm
    8.  os.replace(staging, target); swapped = True
    9.  db._reopen()
    10. new_epoch = max(captured_epoch, db.restore_epoch()) + 1   # 世代不随快照回滚
    11. _block_inflight(c, new_epoch, now())   # 阻断在途任务 + 删业务幂等
    12. rotate_key(key_path)                   # 轮换票派生密钥
    13. db.audit("human_owner", "db.restored", ...)

    def _block_inflight(conn, epoch, t):
        blocked = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status IN (?,?,?)",
                               ("DISPATCHED", "RUNNING", "CANCELLING")).fetchone()["n"]
        conn.execute("UPDATE tasks SET status='BLOCKED', blocked_reason=?"
                     " WHERE status IN ('DISPATCHED','RUNNING','CANCELLING')",
                     ("数据库从备份恢复，需人工确认副作用后重新审批",))
        conn.execute("UPDATE tickets SET revoked_at=? WHERE revoked_at IS NULL", (t,))
        cur = conn.execute("DELETE FROM idempotency WHERE scope IN ('claim','heartbeat','submit')")
        return {"blocked": blocked, "tickets_revoked": ..., "idempotency_purged": cur.rowcount}

失败回滚只在 swapped 为真时才动原库：

    except Exception:
        if swapped and backup_of_original and backup_of_original.exists():
            rollback = original_dir / (target.name + ".rollback")
            shutil.copyfile(backup_of_original, rollback)
            for suffix in ("-wal", "-shm"): ...unlink()
            os.replace(rollback, target)          # 原子，不留半个库
        try:
            db._reopen()                          # 无条件重建，否则上层拿到已关闭的 Database
        except Exception:
            pass
        raise

审计包与完整备份分家：

    AUDIT_TABLES = ("tasks", "task_versions", "reviews", "approvals", "events", "audit")
    AUDIT_DENY_COLUMNS = ("token_hash", "ticket", "response", "request_hash", "evidence")
    KIND_OPERATIONAL = "operational"     # 完整，可恢复，含任务正文，不外发
    KIND_AUDIT       = "audit"           # 脱敏，不可恢复

---

## 4. bigfish/security.py — 密钥、脱敏、ACL（150 行）

    def write_key_atomic(path, key):        # O_EXCL 0600 + fsync + os.replace
    def rotate_key(path):                   # 换新密钥，原子写入
    def secure_file(path):                  # Windows: icacls /inheritance:r /grant:r USER:F
    def verify_file_restricted(path):       # 只读校验
    def scrub(text) / sanitize(obj)         # 统一脱敏
    def contains_secret(text)               # 判定

脱敏规则覆盖 bfc_tk_ / bfc_ex_ / bfc_rv_ / bfc_ad_ / Bearer … /
-----BEGIN … PRIVATE KEY----- / sk- / ghp_ 等前缀。

ACL 校验这轮修了个真缺陷（原来 user 取了没用）：

    for broad in ("everyone", "\\users:", "authenticated users", "interactive",
                  "builtin\\users"):
        if broad in out:
            return False, f"ACL 仍对宽泛主体开放（{broad}）"
    if not user or user not in out:
        return False, f"ACL 未包含当前用户（{user or '未知用户'}）"   # rc3 新增

---

## 5. bigfish/db.py — 世代、超时迁移、锁释放（167 行）

    SCHEMA_VERSION = 3

    def schema_version(self) -> int
    def restore_epoch(self) -> int          # 读 meta.restore_epoch，缺省 0
    def bump_restore_epoch(self) -> int
    def _reopen(self) -> None               # 重置 _depth=0 + 重设 4 个 PRAGMA

    # _Tx.__enter__ 两条失败路径都先放锁
    try:
        self.db._conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        self.db._lock.release()            # rc3：不能因数据库忙让线程永久持锁
        raise

迁移：MIGRATIONS[3] 全部是 ADD COLUMN + CREATE TABLE IF NOT EXISTS，不做表重建；
current > SCHEMA_VERSION 抛 DatabaseVersionError。

---

## 6. bigfish/server.py — 监听校验下沉 + isError 语义（85 行）

    class BindError(RuntimeError):
        pass

    def check_bind_host(host: str | None) -> str:
        h = (host or "").strip().strip("[]")
        if not is_loopback_host(h):
            raise BindError(
                "拒绝启动：API 只允许监听回环地址（127.0.0.1/localhost/::1）。"
                "如需跨机访问请用 Tailscale Serve 转发，不要直接绑 0.0.0.0。")
        return h

    def serve(council, host=DEFAULT_API_HOST, port=DEFAULT_API_PORT, watchdog_interval=10.0):
        host = check_bind_host(host)        # 所有入口共用这一处校验

    def _owned_status(council, ex, task_id):
        task = council.get_task(task_id)
        if not task.get("executor_id") or task["executor_id"] != ex["id"]:
            raise PermissionError("任务不属于该执行器")      # rc3：未分配也 403，不再是放行
        return {...只允许字段...}

MCP tools/call 业务失败现在返回 isError: true，不再是永远 False。

---

## 7. bigfish/executor_client.py — 幂等键与不确定网络（102 行）

    def new_idem_key(prefix="idem"):
        return f"{prefix}-{uuid.uuid4().hex[:16]}"

    class _UncertainNetworkError(RuntimeError):
        """请求可能已经成功但响应丢了——重试前必须先查状态。"""

    def _call(self, method, path, body=None, ...):
        # 写操作（有 body 且带 idempotency_key）重试时复用同一 body
        ...
        except (socket.timeout, ConnectionError) as exc:
            raise _UncertainNetworkError(...)

    def safe_submit(self, ticket, **kwargs):
        try:
            return self.submit(ticket, **kwargs)
        except _UncertainNetworkError:
            return self.task_status(...)      # 不盲目重发，先问服务端最终状态

---

## 8. bigfish/config.py / runtime.py / roles.py

    # config.py
    DEFAULT_API_HOST = "127.0.0.1"
    DEFAULT_API_PORT = 3096
    DEFAULT_PROXY_PORT = 3098
    TEST_API_PORT = 3097
    LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
    def is_loopback_host(host) -> bool
    # Settings 新增：ticket_max_ttl_sec=21600 / heartbeat_lease_sec=180 /
    #               watchdog_interval_sec=10 / key_path
    # 环境变量 BIGFISH_API_HOST / BIGFISH_API_PORT 优先级最高

    # runtime.py —— 唯一装配路径
    def open_council(settings, key=None) -> tuple[Database, Council]:
        ensure_dirs -> Database -> init_schema -> load_or_create_key -> Council
    def reload_key(settings, council) -> bytes     # 恢复后换密钥用

    # roles.py
    def role_for_db_row(db_role, name="", model=None) -> str   # 角色从库字段派生，不从名字猜

---

## 9. bigfish/cli.py / __main__.py / gui.py

CLI 从 11 个子命令扩到 16 个：新增 cancel / verify / purge-idem / audit-export
（另 review-start 与 allowed-ports 是 rc1 就有、rc3 保留）。

    # __main__.py：非回环直接 return 2（stdout 说明原因，不抛栈）
    try:
        host = check_bind_host(args.host or settings.api_host)
    except BindError as e:
        print(e, file=sys.stderr)
        return 2

    # gui.py:327 改 Prompt 带版本
    self.win.council.update_prompt(self.current, txt, "GUI 修改",
                                   expected_version=t["current_version"])

    # gui.py:957 恢复走 council 层（自动轮换密钥/阻断在途/递增世代）
    out = self.win.council.restore_from_bundle(path)

    # gui.py:1133 启动前校验监听地址
    def run(data_dir=None, port=None, host=None) -> int:
        try:
            check_bind_host(host or settings.api_host)
        except BindError as e:
            print(e); return 2

---

## 10. tools/ 三个脚本

    # remote_check.py：删掉自动挑任务，未指定只读
    def parse_task_id(argv):        # 同时支持 --task-id x 与 --task-id=x
        ...
    if not task_id:
        print("[skip] 闭环未执行：本脚本默认只读")
        return 0

    # dev_setup.sh --test：不再让管道吞掉退出码
    "$PY" -m unittest discover -s tests -t . >"$LOG" 2>&1
    RC=$?
    tail -6 "$LOG"
    [ $RC -ne 0 ] && exit $RC

    # stop-headless.ps1：只杀确实属于 BigFish Council 的进程
    $procs = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like 'python*' -and $_.CommandLine -match 'bigfish|BigFishCouncil' }
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force }
    # 其余占端口的进程进 $skipped 并打印「不是 BigFish Council，未终止」

---

## 11. 新增 tests/test_v011_rc3.py（26 项）

四组：TestStateMachine(7) / TestConcurrency(7) / TestRestore(7) / TestSecurityBoundary(5)。
全部并发测试使用 threading.Barrier 做真实同步点，没有用 sleep 编排时序。

---

## 12. 本次唯一的测试改动（说明清楚）

tests/test_v011_p1.py:143 test_revoked_ticket_rejected → 改名
test_cancel_while_dispatched_needs_ack。

旧断言期待「吊销票后心跳直接失败」，这是 rc2 的语义。rc3 按任务书 2.1c 要求
执行中取消必须先确认停止，所以新测试检查：

1. request_cancel 后状态是 CANCELLING 且 final 为假；
2. 心跳必须成功，且 cancel_ack_required=True、cancel=True；
3. submit_result(completed) 必须被拒；
4. confirm_cancel 后 task_status == CANCELLED；
5. 之后再心跳必须失败。

断言项从 1 条变成 5 条，是加强不是放宽。其余 102 项一字未改。

---

## 13. 验证范围（2026-11-19 补记）

上面所有 Diff 都已在两个平台跑过同一套 129 项断言：

| 平台 | 结果 |
| --- | --- |
| Debian 12 / Python 3.11.2 / sqlite 3.40.1 | Ran 129 tests in 16.093s / OK，退出码 0 |
| Windows 11 / Python 3.14.6 / SQLite 3.50.4 | Ran 129 tests in 25.263s / OK，退出码 0 |

Windows 上额外实测到的平台差异：`os.chmod` 的 mode 仍是 `0o666`（Windows 无效），
权限真正靠 `icacls` 生效，`verify_file_restricted()` 走 ACL 分支返回 `(True, "acl limited")`；
`subprocess.terminate()` 退出码是 1 而非 POSIX 的 -15（rc2 已加 `os.name == "nt"` 分支）。
详见 [TEST-REPORT-v0.1.1-rc3.md](TEST-REPORT-v0.1.1-rc3.md) 第 5 节。
