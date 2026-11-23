# 代码 Diff 摘要：v0.1.1-rc4 → v0.1.1-rc5

分支 `fix/v0.1.1-rc4-hardening`，基线 `72e8535`（rc4 发布点），HEAD `adc48a3`（`feat(R5+R8): 服务端下发 dispatch_id + CLI restore 拒绝在服务运行时替换库`）。仓库无 tag，rc5 仍是发布候选。

区间内三次提交：

    adc48a3  feat(R5+R8): 服务端下发 dispatch_id + CLI restore 拒绝在服务运行时替换库
    a4f0641  fix(R2+R3+R4+R6+R9): 事件落库、撤评论边界、锁内时间戳、审计白名单、密钥自愈
    4281114  fix(R1+R8): 恢复后作废一切远端授权 + 恢复期排空维护模式

规模按 `git diff 72e8535 HEAD --stat` 实际输出：**20 files changed, 2251 insertions(+), 60 deletions(-)**。

    bigfish/backup.py          165 +++++++++---
    bigfish/core.py             99 +++++--
    bigfish/db.py               85 +++++-
    bigfish/executor_client.py 231 +++++++++++++-
    bigfish/gui.py              54 +++-
    bigfish/server.py           17 +-
    bigfish/cli.py              28 +++
    bigfish/__init__.py          2 +-
    tools/bfc-proxy.mjs         59 +++-
    tests/*（13 个文件，其中 7 个新增）见第 10-17 节

平台状态（截至 HEAD）：

| 平台 | 环境 | 结果 |
| --- | --- | --- |
| A | Debian 12 / Python 3.11.2 / SQLite 3.40.1 | **244 项全绿** |
| B | Windows 11 / Python 3.14.6 / SQLite 3.50.4 | **本批次未跑**（rc3 的 129 项全绿是最后一次 Windows 实测） |

244 的构成：rc4 的 162 项 + 新增 7 个文件 80 项 + 两个既有文件各补 1 项 = 244。

> 口径提示：本文数字一律按收尾提交口径（**244 项 / 15 个文件**，`Ran 244 tests in 39.178s / OK`）。本文所有小节都按 HEAD `b8f6de1` 写，收尾前 `adc48a3` 上的 243 项只在 20.7 节作为中间态提及。

## 0. 怎么读这篇 diff

rc5 不是加功能，是把审计报告点名的 9 条缺陷（R1-R9）逐条封口，外加 3 项「测试恒真」整改。读法建议：先看每节标题后面的审计项编号，再看「为什么」——每一处改动都能对回到一条具体缺陷，没有顺手重构。

| 审计项 | 一句话 | 主要落点 | 本文节 |
| --- | --- | --- | --- |
| R1 | 从旧备份恢复后授权没有作废 | `backup._block_inflight` / `core.reenable_executor` / `core._replay_claim` | 2、3、4 |
| R2 | 双模型通过的两条事件永不落库 | `core._recompute_review` | 4 |
| R3 | 撤普通评论能打回已批准任务 | `core.revoke_review` | 4 |
| R4 | 心跳/提交时间戳取在等锁之前 | `core.heartbeat` / `core.submit_result` | 4 |
| R5 | 执行端没有开工收据 | `executor_client.ReceiptLog` | 5 |
| R6 | 审计导出用列黑名单 | `backup.export_audit_bundle` | 3 |
| R7 | bfc-proxy 监听地址无回环校验 | `tools/bfc-proxy.mjs` | 9 |
| R8 | 恢复替换期间没有排空 | `db` 维护模式 + `server` 503 + `gui` 停服 + `cli` 探端口 | 2、6、7、8 |
| R9 | 换密钥没吊销旧票 | `backup._heal_key` | 3 |

对照表另有 `docs/FIX-MATRIX-v0.1.1-rc5.md`（逐条列修改文件、修法、用例），本文只讲代码怎么变的。

---

## 1. `bigfish/__init__.py`（1 insertion，1 deletion）

    -__version__ = "0.1.1-rc4"
    +__version__ = "0.1.1-rc5"

**为什么**：版本号进位。这个值同时进 `/health` 的 `version`、备份包 manifest 的 `app_version`。

**影响面**：`BACKUP_FORMAT_VERSION` 同步升到 4（第 19 节），旧版程序打开 rc5 的包会被格式检查挡下。

**回归**：无专用用例，全量回归覆盖。

---

## 2. `bigfish/db.py`（83 insertions，2 deletions）

本文件承担 R8 的「库这一层」和 R1 的 schema 支撑。核心是给数据库加一个能真正排空的维护模式：不是靠 sleep 猜，而是靠写锁本身当屏障。

### 2.1 schema 与版本号

| 位置 | 改动 |
| --- | --- |
| :13 | 新增 `import contextlib`（给 `contextlib.contextmanager` 用） |
| :22-23 | `SCHEMA_VERSION = 4`、`BACKUP_FORMAT_VERSION = 4`（原 3 / 3） |
| :97 | `executors` 建表语句在 `enabled` 与 `created_at` 之间插入 `disabled_reason TEXT` |
| :205-209 | `MIGRATIONS[4]` 两条 SQL，见第 19 节 |

### 2.2 MaintenanceError（:217-222）

    class MaintenanceError(RuntimeError):
        """数据库处于维护模式（正在从备份恢复），普通读写一律拒绝。

        R8：恢复期间必须有人对「谁能写」负责。这个异常是唯一的拒绝信号，
        上游（HTTP / MCP / GUI）应当把它翻译成 503 + 稍后重试，而不是 500。
        """

**为什么**：维护期必须能被上层识别成「暂时不能写」，而不是「你不会写」。用独立异常类型而不是复用 `RuntimeError`，是为了在 `server.py` 的 `except` 链里能精确命中、不被业务异常吞掉。

### 2.3 维护模式的五个入口（:236-342）

`Database.__init__` 新增三个字段：

    # R8：维护模式。设置后所有普通读写立刻报 MaintenanceError（fail-fast），
    # 设置前先用 self._lock 做一次屏障等待，确保没有正在进行的写事务。
    self._maint_depth = 0
    self._maint_reason = ""
    self._maint_bypass = threading.local()

`_guard`（:288）是唯一判定点：

    def _guard(self) -> None:
        """维护模式下拒绝普通访问；内部恢复流程用 _maint_bypass 放行。"""
        if self._maint_depth <= 0:
            return
        if getattr(self._maint_bypass, "on", False):
            return
        raise MaintenanceError(
            f"数据库正在维护（{self._maint_reason or '正在从备份恢复'}），"
            "本次操作未执行，请稍后重试")

`enter_maintenance`（:298）的排空靠写锁，docstring 把理由写死：

    def enter_maintenance(self, reason: str = "", timeout: float = 15.0) -> None:
        """进入维护模式：先排空在途写事务（屏障），再对后续访问 fail-fast。

        为什么先抢锁：Database.tx() 在整个事务期间持有 self._lock，所以拿到锁
        的这一刻就已经没有正在进行的写事务了。这就是「排空」，不是靠 sleep 猜。
        """
        got = self._lock.acquire(timeout=timeout)
        try:
            if not got:
                raise MaintenanceError(
                    "有写事务在超时时间内没有结束，无法排空进入维护模式")
            self._maint_depth += 1
            if reason:
                self._maint_reason = reason
        finally:
            if got:
                self._lock.release()

`_maint_depth` 是计数器不是布尔：恢复流程可能嵌套（GUI 先 `pause_services`，`restore_bundle` 内部再进一次），嵌套退出只减不归零，最外层退出才清 `_maint_reason`（:316 `exit_maintenance`）。

`maintenance`（:323）是 contextmanager 包装，`_allow_during_maintenance`（:332）用 `threading.local` 放开当前线程——注意是**线程级**放行，不是全局放行，恢复线程能写、API 线程照旧 503。`in_maintenance()`（:341）给 `/health` 用。

### 2.4 四个访问器首行加守卫

| 位置 | 代码 |
| --- | --- |
| :448 | `def query(...)` 首行 `self._guard()` |
| :453 | `def one(...)` 首行 `self._guard()` |
| :458 | `def execute(...)` 首行 `self._guard()` |
| :471 | `_Tx.__enter__` 首行 `self.db._guard()`，注释「维护模式：连锁都不抢，直接报错」 |

**为什么**：写在 `with self._lock` **之前**。如果先抢锁再判维护，维护期所有读请求会排在恢复的写锁后面干等，等回来还是报错——白等一轮 `busy_timeout`。先判再抢，fail-fast 是真的快。

**影响面**：`Database` 的所有公开读写路径都被覆盖；`_allow_during_maintenance` 之外的任何代码在维护期都拿不到连接。`_migrate_locked`（:412）未改，仍是逐版本 `BEGIN IMMEDIATE`，把 `duplicate column name` 视为已应用——迁移本身不重入维护模式。

**回归**：`test_v011_rc5_restore_authority.py` 的 `TestMaintenanceMode` 6 项（第 10 节）。

---

## 3. `bigfish/backup.py`（139 insertions，26 deletions）

改动最多的一块，同时承担 R1（恢复作废授权）、R6（审计白名单）、R8（恢复期维护模式）、R9（密钥自愈吊销票）。

### 3.1 审计包列白名单（:52-69，R6）

    # R6：审计包改用**列白名单**，不再用黑名单。
    # 黑名单的问题是「漏一个列就等于漏一次凭据」——以后有人给 tasks 加一列
    # executor_token，黑名单不会自动覆盖。白名单的方向相反：漏列只会少导出数据
    # （安全方向的失败），而且真正被跳过哪些列会写进 manifest.redaction。
    AUDIT_COLUMNS: dict[str, tuple[str, ...]] = {
        "tasks": ("id", "title", "goal", "scope", "risk", "status", "current_version",
                  "control", "executor_id", "blocked_reason", "cancel_requested_at",
                  "created_at", "updated_at"),
        "task_versions": ("task_id", "version", "prompt", "note", "created_at"),
        "reviews": ("id", "task_id", "version", "model", "kind", "verdict", "content",
                    "source", "actor", "revoked_at", "created_at"),
        "approvals": ("id", "task_id", "version", "decision", "note", "actor",
                      "created_at"),
        "events": ("id", "task_id", "kind", "detail", "created_at"),
        "audit": ("id", "actor", "action", "target", "detail", "created_at"),
    }
    # 兼容旧引用：这些列名必须永远落在白名单之外
    AUDIT_DENY_COLUMNS = ("token_hash", "ticket", "response", "request_hash", "evidence")

`AUDIT_DENY_COLUMNS` 保留但不再参与判定，只为兼容旧引用；它在 `test_v011_rc3.py::test_audit_bundle_internal_tables` 里仍被断言「必须落在白名单之外」。

`export_audit_bundle`（:148-212）的循环体从黑名单换成白名单：

    allow = set(AUDIT_COLUMNS.get(table, ()))
    skipped: set[str] = set()
    clean = []
    for row in rows:
        item = {}
        for k, v in row.items():
            if k not in allow:
                skipped.add(k)
                continue
            item[k] = scrub(v) if isinstance(v, str) else v
        clean.append(item)
    if skipped:
        dropped[table] = sorted(skipped)
    payload[table] = clean

manifest 的 `redaction` 从「一句黑名单列表」变成「可自证的策略」：

    "redaction": {
        "policy": "allowlist",
        "allowed_columns": {t: list(c) for t, c in AUDIT_COLUMNS.items()},
        "dropped_tables": ["executors", "tickets", "idempotency", "runs",
                           "artifacts", "meta"],
        "dropped_columns": dropped,
        "string_scrub": "bfc_*/Bearer/sk-*/gh*_*/AKIA*/private-key",
    },

**为什么**：`dropped_columns` 原先是 `list(AUDIT_DENY_COLUMNS)`——把「我打算拦哪些」当成了「我实际拦了哪些」。现在是跑完扫描后的真实结果，拿到包的人能自己核对策略，不用信注释。

### 3.2 `_block_inflight` 五步（:319-363，R1）

    def _block_inflight(conn: sqlite3.Connection, epoch: int, t: float) -> dict:
        """恢复后把「快照里躺着的授权」全部作废（R1 + P1-4.2）。
        ...
          1) 在途任务（DISPATCHED/RUNNING/CANCELLING）：外部进程可能还在跑副作用，
             必须 BLOCKED 交人工确认，绝不能凭旧快照就认为它没在动；
          2) 待派工的已批准任务（APPROVED）：这是「随时可被领取」的授权。恢复把它
             原样搬回来，等于复活了撤销之前的派工许可，必须冻结；
          3) 所有远端身份（executors）：凭据在恢复前就已经发到外面了。恢复后依然
             能用旧 token 登进来（token 哈希随快照一起回来了），所以必须一律禁用；
          4) 历史执行票 + claim 幂等响应：重放路径会把快照里的历史票重新算出来。

        这里一次做全，且不删任何一行：任务、票、身份都保留原记录（审计要用），
        只翻状态位 + 写 disabled_reason。
        """

五步按顺序：

**第 1 步**，在途任务转 BLOCKED：

    in_flight = conn.execute(
        "SELECT id FROM tasks WHERE status IN ('DISPATCHED','RUNNING','CANCELLING')"
    ).fetchall()
    for r in in_flight:
        conn.execute(
            "UPDATE tasks SET status='BLOCKED', blocked_reason=?, updated_at=? WHERE id=?",
            ("数据库已从备份恢复，执行授权全部作废，需人工确认副作用后重新审批", t, r[0]))

**第 2 步**（R1-a），APPROVED 冻结：

    # R1-a：APPROVED 是「可领取」状态，快照里的它等于一张还没发出去的通行证
    frozen = conn.execute(
        "SELECT id FROM tasks WHERE status='APPROVED'").fetchall()
    for r in frozen:
        conn.execute(
            "UPDATE tasks SET status='BLOCKED', blocked_reason=?, updated_at=? WHERE id=?",
            ("数据库已从备份恢复，该任务的派工许可已冻结；"
             "人工核对后需重新提交审批才可再次派工", t, r[0]))

**第 3 步**，吊销全部活票（HEAD 是裸执行，工作树改成取 rowcount，见第 20.7 节）：

    conn.execute("UPDATE tickets SET revoked_at=? WHERE revoked_at IS NULL", (t,))

**第 4 步**（R1-b），禁用全部远端身份：

    # R1-b：所有远端身份一律禁用（凭据已外发，恢复不能把它们一起复活）
    disabled = conn.execute(
        "UPDATE executors SET enabled=0, disabled_reason=?"
        " WHERE enabled=1", (DISABLED_REASON_RESTORE,)).rowcount

**第 5 步**，清三类幂等缓存：

    purged = conn.execute(
        "DELETE FROM idempotency WHERE scope IN ('claim','heartbeat','submit')").rowcount

返回体从 3 个字段扩到 5 个：

    return {"blocked": len(in_flight) + len(frozen),
            "frozen_approved": len(frozen),
            "executors_disabled": disabled,
            "tickets_revoked": 1,
            "idempotency_purged": purged}

`blocked` 的口径变了：原来是「在途任务数」，现在是「在途 + 冻结」的合计，因为两者都已转 BLOCKED，运维看这一个数就是「恢复后需要人工过一遍的任务总数」；要分开看有 `frozen_approved`。

常量定义在 :72：

    DISABLED_REASON_RESTORE = "数据库已从备份恢复，恢复前的凭据全部作废，需人工重新授权"

### 3.3 `restore_bundle` 包维护模式（:462-663，R8）

docstring 补的这段是整节的核心：

    R8：整个恢复期数据库处于维护模式（db.maintenance）。进入时分两步排空：
      1) 先调 before_replace 让上层停掉 API 线程 / 看门狗（不再有新请求进来）；
      2) 再抢 Database 的写锁（enter_maintenance 内部完成屏障），确认没有
         在途写事务，之后所有普通读写 fail-fast 成 MaintenanceError。
    这样「执行器收到成功应答、恢复又回滚」这种时序就不会发生：在维护模式下
    任何一次提交都直接被拒（503），执行器会走不确定分支去对账，不会误以为成功。

执行体的结构变化（缩进层级也一起变了，因为外面多包了两层 `with`）：

    keep_maintenance = False      # 回滚也失败时保留维护模式

    db.enter_maintenance("正在从备份恢复数据库")
    try:
        captured_epoch = db.restore_epoch()
    except Exception:
        captured_epoch = 0
    new_epoch = captured_epoch + 1

    try:
      with _restore_lock(original_dir / (target.name + ".restore.lock")):
       with db._allow_during_maintenance():
        try:
            ...

三个具体动作：

1. **进入维护模式在 `try` 之前**（:500）。如果放进 `try` 又被 `finally` 里的 `exit` 兜住，超时拿不到锁那次也会「进入失败然后退出」——把一次进入失败走成一次退出，深度就错位了。
2. **`before_replace` 从第 4 步前移到第 3 步开头**（:552-553）。原来在「关连接 → 替换」之前调，此时 `.prerestore` 快照已经做完；现在提到快照之前，因为快照本身要读写库，得等上层真的停干净：

        # ---- 3) 排空上层 → 原库与原密钥各留一份回滚副本 ----
        if before_replace is not None:
            before_replace()
        if target.exists():
            db.snapshot(original_dir / (target.name + ".prerestore"))
            had_prerestore = True

3. **回滚也失败时保留维护模式**（:649）：

            if rollback_errors:
                # 回滚也失败：明确报双错，并**保留维护模式**——库处于未知状态时
                # 宁可让所有写请求继续 503，也不能让它带着半套凭据继续接单。
                keep_maintenance = True
                raise RestoreError(
                    "恢复失败且回滚未完全成功：" + "；".join(rollback_errors)
                    + f"（原始错误：{exc}）") from exc

退出是**有条件**的（:661-662）：

    finally:
        if not keep_maintenance:
            db.exit_maintenance()

返回体新增两个字段：

    "executors_disabled": cleanup.get("executors_disabled", 0),
    "frozen_approved": cleanup.get("frozen_approved", 0),

### 3.4 `ensure_key_consistency` 与 `_heal_key`（:670-732，R9）

`ensure_key_consistency` 三个分支全部改成调 `_heal_key`，动作名分别是 `new-key` / `recorded` / `rotated`：

    if not key_path.exists():
        # 密钥文件都没了：现在生成的密钥一定不是签发过票的那把，
        # 所以这里也要把旧票一并作废，否则「密钥换了、票还能用」。
        return _heal_key(db, key_path, action="new-key")
    actual = hashlib.sha256(key_path.read_bytes()).hexdigest()
    row = db.one("SELECT value FROM meta WHERE key='key_sha256'")
    if row is None:
        # 全新库（或 rc5 之前的库）还没有指纹：补一次，之后才有得比。
        return _heal_key(db, key_path, action="recorded", fingerprint=actual,
                         rotate=False)
    if actual == row[0]:
        return {"ok": True, "action": "consistent"}
    return _heal_key(db, key_path, action="rotated", fingerprint=actual)

`_heal_key`（:693）docstring 直接写明审计指出的两点：

    """把「库里的指纹」和「磁盘上的密钥」重新对齐，并作废一切旧执行票。

    审计项 R9 指出两件事，一起修：
    1) 自愈分支没有任何单测（原来靠人读代码）；
    2) 原文案写着「恢复前的执行票全部失效」，但函数里根本没碰 tickets 表——
       密钥换了，tickets 表里的 token_hash 还是旧值，旧票照样能通过认证。
       现在补上：轮换密钥的同时吊销全部未撤销的票、清 claim 幂等、写审计。
    不删任何行，只翻状态位。
    """

关键分叉：只有 `rotate` 才动票。

    if rotate or not key_path.exists():
        key = rotate_key(key_path)          # 内部已 mkdir + 原子替换 + 0600
        fingerprint = hashlib.sha256(key).hexdigest()
    token_hash = fingerprint or ""
    t = now()
    tickets = 0
    idem = 0
    with db.tx() as c:
        c.execute("INSERT INTO meta(key, value) VALUES('key_sha256', ?)"
                  " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (token_hash,))
        if rotate:
            # 只有真的换了密钥才动票。仅仅补记指纹（老库首次升级到 rc5）时
            # 票还是用同一把密钥签的，作废它们等于无故中断在途执行。
            cur = c.execute("UPDATE tickets SET revoked_at=? WHERE revoked_at IS NULL",
                            (t,))
            tickets = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            idem = c.execute(
                "DELETE FROM idempotency WHERE scope IN ('claim','heartbeat','submit')"
            ).rowcount

审计 detail 分两支，换钥那支带真实张数：

                   (f"库内 key 指纹与磁盘密钥不一致（{action}），已重新对齐密钥并"
                    f"吊销 {tickets} 张执行票、清理 {max(idem, 0)} 条幂等记录")
                   if rotate else
                   f"库内尚无 key 指纹（{action}），已补记、未改动任何执行票",

返回体从 `{"ok", "action"}` 扩成 4 个字段：`tickets_revoked` / `idempotency_purged` 也一并回。

**为什么 `recorded` 不能轮换**：老库第一次升到 rc5 时 `meta` 里没有指纹，但磁盘密钥是当初签发过票的那把。这时候若按「不一致」处理去轮换，等于凭「缺一条记录」把在途执行的票全废掉——这不是修 bug，是制造故障。所以补指纹必须 `rotate=False`。

**影响面**：启动路径（`runtime` 调 `ensure_key_consistency`）行为变了——以前自愈只改 `meta`，现在换钥会连带吊销票、清幂等、写审计。`consistent` 与 `no-fingerprint` 的旧返回字符串不再出现，外部若有脚本按 `action` 分支需同步。

**回归**：`test_v011_rc5_key_heal.py` 7 项 + `test_v011_rc5_review_audit.py::TestAuditBundleAllowlist` 5 项 + `test_v011_rc3.py::test_audit_bundle_internal_tables`。

---

## 4. `bigfish/core.py`（86 insertions，13 deletions）

R1、R2、R3、R4 四条的落点，都在授权与审批的边界上。

### 4.1 认证侧给可执行文案（:188-208、:210-224，R1）

`authenticate` 在 `row is None` 之后多查一次：

        h = hash_token(token)
        row = self.db.one("SELECT * FROM executors WHERE token_hash=? AND enabled=1", (h,))
        if row is None:
            # R1：恢复后被禁用的身份必须给出可执行的原因，不能只说「令牌无效」——
            # 运维要知道该重新授权还是该换凭据。
            gone = self.db.one("SELECT * FROM executors WHERE token_hash=?", (h,))
            if gone is not None and not gone["enabled"]:
                raise CouncilError(self._disabled_message(gone))
            raise CouncilError("执行器令牌无效")

`authenticate_any` 同构，只差文案（「令牌无效」）。多查一次的代价是一次索引命中，且只在认证失败路径上发生。

`_disabled_message`（:240-245）是单点文案：

    @staticmethod
    def _disabled_message(row) -> str:
        reason = (row["disabled_reason"] if "disabled_reason" in row.keys()
                  else None) or "被管理员禁用"
        return f"该身份已被禁用：{reason}；必须由人类所有者重新授权并更换凭据"

`row.keys()` 判断是给 rc4 及更早的库留的兜底：迁移前 `disabled_reason` 列不存在。

### 4.2 新增 `reenable_executor`（:246-268，R1）

    def reenable_executor(self, exec_id: str, actor: str = R.HUMAN_OWNER) -> dict:
        """人类重新授权一个被禁用的身份（R1）。

        重新启用**同时轮换凭据**：恢复前的旧 token 可能已经泄露到外部，
        只翻 enabled=1 等于把旧凭据一起复活。所以这里返回一个新 token，
        旧 token 的哈希在库里被直接替换掉，物理上无法再用。
        """
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM executors WHERE id=?", (exec_id,)).fetchone()
            if row is None:
                raise CouncilError("身份不存在")
            role = row["role"]
            prefix = {"executor": "bfc_ex_", "reviewer": "bfc_rv_",
                      "admin": "bfc_ad_"}.get(role, "bfc_ex_")
            token = new_token(prefix)
            c.execute(
                "UPDATE executors SET enabled=1, disabled_reason=NULL, token_hash=?,"
                " last_seen=NULL WHERE id=?", (hash_token(token), exec_id))
        self.db.audit(actor, "executor.reenabled", exec_id,
                      f"{role}:{row['name']}（凭据已轮换）")
        self.log_event(None, "executor.reenabled", f"{role}:{row['name']}")
        return {"id": exec_id, "name": row["name"], "role": role, "token": token,
                "model": row["model"] if "model" in row.keys() else None}

一次操作做三件事：启用、换 token（旧哈希被覆盖，物理失效）、清 `disabled_reason`。返回体里**带明文新 token**——这是全仓库唯一一次在响应里回凭据，因为它就是发给人类所有者去配置执行器的。

`_assert_executor_active`（:270-281）的事务内复验也换了文案：

        row = c.execute(
            "SELECT enabled, disabled_reason FROM executors WHERE id=?", (ex_id,)).fetchone()
        ...
        if not row["enabled"]:
            raise CouncilError(self._disabled_message(row))

原来这里是 `raise CouncilError("执行器已被吊销，操作被拒绝")`。改完「被管理员吊销」与「恢复后统一禁用」共用一条可执行文案，`test_v011_rc3.py::test_revoke_and_submit_race` 的断言也随之改（第 17.4 节）。

### 4.3 `revoke_review` 判 kind（:497-547，R3）

            kind = row["kind"] if "kind" in row.keys() else "vote"
            task_row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            t = now()
            invalidated = False
            was_status = task_row["status"] if task_row else None
            # 只有「票」才影响审批有效性。撤一条普通评论只是把那段文字下架，
            # 它本来就不计入 vote_summary；历史缺陷是这里不判 kind，撤评论也能
            # 把运行中的任务打成 BLOCKED（审计项 R3）。
            if kind != "vote":
                task_row = None

**为什么用 `task_row = None` 而不是提前 return**：函数后面还有撤评论本身的 `UPDATE reviews SET revoked_at` 和审计写入，提前 return 会把「撤评论」这件事也一起跳过——那就从「误伤任务」变成「撤不掉评论」。置空任务行，让后续所有 `if task_row is not None` 分支自然跳过，功能保留、副作用消失。

`kind` 缺省视作 `vote`：老库里 `reviews.kind` 有 null 的历史行，撤这种行时按票处理是保守方向（宁可多打回一次，也不要漏掉真票被撤）。

### 4.4 `_recompute_review` 删掉提前 return（:597-640，R2）

                if cur.rowcount == 1:
                    events.append(("task.review_passed", f"v{version} 双模型通过"))
                    events.append(("task.pending_approval", "等待人类审批"))
    -                return
    +                # 注意：这里**不能 return**。历史缺陷：提前 return 跳过了函数末尾
    +                # 的 log_event 循环，双模型通过的两条事件永远写不进库（审计只看
    +                # 事件表会以为任务从没通过审查）。
            # 未双通过：从待审批/已批准退回议事，并作废可能已存在的授权
            elif task["status"] in (S.PENDING_APPROVAL, S.APPROVED):

删 `return` 后原来下面那个 `if` 必须变 `elif`，否则双通过的分支会紧接着再执行「退回议事」的逻辑，把刚写好的 PENDING_APPROVAL 又打回 IN_REVIEW。这两处是一对，不能只改一处。

末尾还有一处条件收紧（:637）：

    -            if votes["any_rejected"]:
    +            if not votes["all_approved"] and votes["any_rejected"]:

**为什么**：双模型全通过、但只要有人投过反对票（后来又改票），原来也会进这个 UPDATE 分支——只为了「碰一下时间戳」。现在要求「不是全通过」才碰，避免无意义写入和一次多余的 `updated_at` 抖动（前端会看到任务「刚更新过」）。

函数末尾仍是事件落库循环：

        for kind, detail in events:
            self.log_event(tid, kind, detail)

### 4.5 claim 下发 `dispatch_id`（:838，R5）

            "ticket": ticket,
            "ticket_id": ticket_id,
            # R5：执行端用这个编号落「开工/完成收据」。重新派工会换新票，
            # 所以拿票号当派工号天然满足「旧收据不会被新收据覆盖」。
            "dispatch_id": ticket_id,

**为什么复用 `ticket_id` 而不是另生成一个 UUID**：重新派工必然换新票，票号本身就是派工号；另造一个编号就得维护「编号 → 票」的映射表，还要考虑两者不一致时以谁为准。

### 4.6 `_replay_claim` 全量校验（:843-888，R1）

docstring 把历史缺陷写清楚了：

        """幂等重放：重新派生同一张票，内容按记录的版本取。

        R1 修复：重放**不能**无条件把票再交出去。审计发现旧实现只 get_task(tid)
        取当前值，既不查取消、也不查票是否已被吊销/恢复作废，导致「票早就被撤销
        了，客户端重试同一个幂等键还能拿到一张能用的票」。这里把票重新过一遍
        有效性校验，并且要求任务此刻仍归属该执行器且仍在执行中。
        """

五道校验，顺序即优先级：

        version = cached["version"]
        task = self.get_task(tid)
        if (task.get("cancel_requested_at")
                or (task.get("control") or {}).get("cancel_requested")):
            raise CouncilError("任务已被请求取消，本次重放不再签发执行票")
        if task["executor_id"] != ex_id:
            raise CouncilError("任务不属于该执行器，拒绝重放执行票")
        if task["status"] not in (S.DISPATCHED, S.RUNNING, S.CANCELLING):
            raise CouncilError(
                f"任务当前状态「{S.label(task['status'])}」，重放不再签发执行票")
        if int(task["current_version"]) != int(version):
            raise CouncilError("任务版本已变更，本次执行已作废，不能重放")
        trow = self.db.one("SELECT * FROM tickets WHERE id=?", (cached["ticket_id"],))
        if trow is None:
            raise CouncilError("执行票记录不存在，拒绝重放")
        self._check_ticket_row(dict(trow))

`_check_ticket_row`（:900）本身未改，仍是四道：撤销 / 过期 / 绝对上限 / 恢复世代。它原来只在心跳与提交路径上被调，现在重放路径也要过一遍。

返回体里 `expires_at` 从「有票行就取值，没有就 None」改成直接取（:885-886），因为上面已经断言 `trow is not None`：

    -            "expires_at": trow["expires_at"] if trow else None,
    -            "max_expires_at": trow["max_expires_at"] if trow else None,
    +            "dispatch_id": cached["ticket_id"],
    +            "expires_at": trow["expires_at"],
    +            "max_expires_at": trow["max_expires_at"],

### 4.7 时间戳移进事务（:932-942、:998-1009，R4）

    -        t = now()
             with self.db.tx() as c:
    +            # R4：时间戳必须在**拿到写锁之后**取。放在 tx 之外的话，等锁期间
    +            # （最多 busy_timeout=5s，并发高时更久）算出的 t 已经过期，续租
    +            # 会写出一个「比现在还早」的 expires_at，等于刚续完就超时。
    +            t = now()

`submit_result` 是同一处理，注释更短：

            t = now()          # R4：时间戳必须在持锁之后取，理由同 heartbeat

**为什么这是真 bug 而不是洁癖**：`heartbeat` 里 `expires_at = t + self.heartbeat_lease`。t 取在等锁之前，等 5 秒才拿到锁，写进去的到期时间就比真实时间少 5 秒。并发高时连续几次心跳都这样，票会在执行器「刚续过」的状态下超时，任务被看门狗打成 BLOCKED——现象是「明明在跑却被判超时」，极难复现。

**影响面**：`now()` 调用点位置变化不改变返回值语义，只改变「哪一刻」被采到。`heartbeat` 与 `submit_result` 之外的其他 `t = now()`（:290、:384、:475、:515、:609、:670、:766、:1131、:1234、:1306）保持原位——它们要么本来就在事务内，要么只是日志时间。

**回归**：`test_v011_rc5_review_audit.py::TestTimestampInsideTransaction` 2 项（断言 `expires_at > renewed_at`、连发 5 次单调不减）。

---

## 5. `bigfish/executor_client.py`（227 insertions，4 deletions）

R5 的执行端半边。服务端下发编号只是前提，真正的证据必须在执行器本地落盘。

### 5.1 文件头文档（:6、:13-18）

新增环境变量与一段设计说明：

      BFC_RECEIPTS=<可选：本地收据 JSONL 路径，默认不落盘>
    ...
    v0.1.1-rc5（R1 决定二）：
    - 执行端按稳定 dispatch_id 本地落「开工/完成收据」（ReceiptLog）；
    - 收据**每次写完立刻 fsync**，断电前已落盘——数据库从旧备份恢复后，
      服务端不知道某次派工到底有没有真正开工，只能靠执行端本地收据对账；
    - 派工重新派发时 dispatch_id 会变，旧收据因此不会被新收据覆盖；
    - 旧服务端没有 dispatch_id 字段时回退用 ticket_id，属于降级而非报错。

### 5.2 `ReceiptLog`（:41-174）

类 docstring 说清了它为什么存在：

    """执行端本地「收据」日志：追加式 JSONL，一行一个事件。

    为什么单独落盘：真正能证明「这次派工开工了没有」的证据在执行端，不在服务端。
    服务端数据库一旦从旧备份恢复，就丢失了「是否已派发/是否已开工」的事实，
    这时候只能拿执行端本地收据去对账；因此每条收据写完必须 flush + fsync，
    保证断电之前它已经在磁盘上（这是整个类存在的意义，不要为了性能省掉）。

    path=None 时是「纯内存 no-op」：不创建任何文件，收据只留在进程内存里，
    进程结束即消失（给「不要收据」的场景用一条明确、不会偷偷落盘的路径）。
    """

事件白名单与收尾集合分开（:54-57）：

        EVENTS = ("claimed", "started", "finished", "failed", "blocked",
                  "cancelled", "uncertain")
        CLOSED_EVENTS = ("finished", "failed", "blocked", "cancelled")

`record`（:68）的两处硬要求——事件名校验 + flush/fsync 顺序：

        if event not in self.EVENTS:
            raise ValueError(
                f"未知收据事件 {event!r}，只能是 {', '.join(self.EVENTS)}")
        ...
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            # 顺序不能反：先 flush 到内核，再 fsync 到磁盘。
            # 断电时「已经返回给调用方」的收据必须真的在盘上，否则对账会漏掉开工记录。
            f.flush()
            os.fsync(f.fileno())

`read_all`（:98）容错：坏行 `continue`，因为断电可能把最后一行写一半，不能因此让整个对账炸掉。

`state_of`（:123）是四格判定，docstring 里就是判定表：

          1. 最后一条是 finished/failed/blocked/cancelled → "已完成"
             这四件事都说明执行端明确交代过结果，不需要人来判断；
          2. 有 started，且 started 之后没有收尾类事件 → "结果未知"
             开工了没收尾（掉电/被杀/网络断），正是要交给人判断的那种；
          3. 只有 claimed（没有任何 started） → "未开工"
             服务端说派了，执行端只确认收到，没真正动手；
          4. 完全没有记录 → "未知"。

注意第 2 格的措辞：有 `started` 就**一定**是「结果未知」，哪怕后面还有 `uncertain`——uncertain 不是收尾类事件。

`summary`（:159）里有个显式映射，注释解释了为什么不硬拼字符串：

        # 判定词是"已完成"，统计口径用的是"完成"——这两个词不能靠字符串硬拼，必须显式映射。
        key_of = {"已完成": "完成", "结果未知": "结果未知",
                  "未开工": "未开工", "未知": "未知"}

### 5.3 客户端接线（:176-210）

哨兵值为的是区分「没传」和「显式不要」：

    # 「没传 receipts」与「显式传 None」必须能区分：前者读环境变量，后者表示不要收据。
    _UNSET = object()

        def __init__(self, base_url: str | None = None, token: str | None = None,
                     timeout: int = DEFAULT_TIMEOUT, receipts: object = _UNSET):
            """receipts 三种写法：

            - 不传（默认哨兵 _UNSET）：用 ReceiptLog(os.environ.get("BFC_RECEIPTS"))，
              环境变量没配就是内存 no-op；
            - 传 ReceiptLog 或路径 str/Path：用它落收据；
            - 显式传 None 或 False：明确表示「不要收据」（内存 no-op，绝不建文件）。
              之所以用哨兵而不是把默认值写成 None，就是因为 None 已被占用为「关闭」。
            """

        @staticmethod
        def dispatch_id_of(resp: dict) -> str:
            """取稳定派工号；旧服务端没有 dispatch_id 时降级用 ticket_id。"""
            return resp.get("dispatch_id") or resp.get("ticket_id") or ""

### 5.4 收据打点（:263-335）

`claim` 在返回前落 `claimed`：

        resp = self._call("POST", "/api/v1/tasks/claim",
                          {"task_id": task_id, "idempotency_key": key})
        self.receipts.record(self.dispatch_id_of(resp), "claimed", task_id=task_id,
                             ticket_id=resp.get("ticket_id") or resp.get("ticket"))

`run_once` 的顺序是定死的，注释解释了 `started` 为什么在心跳之前：

        # started 放在心跳之前：一旦进程在心跳途中挂掉，收据已经能证明「开工过」。
        self.receipts.record(did, "started", task_id=task_id, ticket_id=ticket_id,
                             note="开始执行")
        hb = self.heartbeat(ticket, "running", "开始执行")

取消分支落 `cancelled`；提交包 try/except，遇到网络不确定先落 `uncertain` 再**原样抛**：

        try:
            res = self.submit(ticket, "completed", log, diff, tests, "模拟执行完成")
        except _UncertainNetworkError as exc:
            # 不吞：先落 uncertain 说明「这次提交结果不明」，再原样抛给调用方处理。
            self.receipts.record(did, "uncertain", task_id=task_id, ticket_id=ticket_id,
                                 note=f"结果未知: {exc}")
            raise

**为什么不能吞**：吞掉异常会让收据看起来像正常结束，`state_of` 判成「已完成」——比不写更糟，等于把一次真正需要人工核对的结果标成了没问题。

### 5.5 `reconcile`（:337-358）

        log = self.receipts if receipts is None else self._as_receipts(receipts)
        summary = log.summary()
        if not summary.get("total"):
            # 一条收据都没有时给一个干净的形状，免得调用方还要判空才敢读 key。
            return {"total": 0, "unknown_inflight": []}
        ...
        out["unknown_inflight"] = [d for d in seen
                                   if log.state_of(d)["verdict"] == "结果未知"]

空集也返回两个键，是为了让调用方不必先判空。

**影响面**：`ExecutorClient` 默认行为变了——不传 `receipts` 且环境变量 `BFC_RECEIPTS` 未设时仍是内存 no-op，不产生文件；设了环境变量则开始写盘（每次写都 fsync，高频调用有 IO 代价）。`claim` / `run_once` 返回值未变。`safe_submit` / `safe_claim` / `safe_heartbeat`（:367-）是 rc4 已有逻辑，rc5 未改。

**回归**：`test_v011_rc5_receipts.py` 24 项 + `test_v011_rc5_dispatch_id.py` 3 项。

---

## 6. `bigfish/server.py`（16 insertions，1 deletion）

R8 的 HTTP 翻译层，三处改动。

### 6.1 引入异常（:31）

    from .db import MaintenanceError

### 6.2 503 + Retry-After（:126-133）

    @app.exception_handler(MaintenanceError)
    async def _maint_err(_req: Request, exc: MaintenanceError):
        # R8：维护模式是「暂时不能写」，不是「你错了」。503 + Retry-After 让执行器
        # 走重试/对账分支，而不是把它当成 4xx 业务失败直接放弃。
        return JSONResponse(status_code=503,
                            content={"ok": False, "error": str(exc),
                                     "maintenance": True},
                            headers={"Retry-After": "15"})

这条处理器按异常类型注册，与 `CouncilError` 的处理器并列。要紧的是 `MaintenanceError` 与 `CouncilError` 没有继承关系，所以业务 409 不会被这条抢走。

### 6.3 `/health` 报维护态（:137-154）

        maint = council.db.in_maintenance()
        return {
            "status": "maintenance" if maint else ("ok" if ok else "degraded"),
            ...
            "maintenance": maint,

`status` 的优先级：维护 > 异常 > 正常。恢复期间库本来就可能是临时不可读的，报 `degraded` 会误导运维去查歪的地方。

### 6.4 `/mcp` 明确回错（:255-257）

            except MaintenanceError as e:
                # 维护期：明确告诉调用方「这次没执行」，别让它以为业务失败
                return _mcp_error(rid, -32000, str(e))

`-32000` 是 JSON-RPC 保留的 server error 段，不是 `isError: true` 的工具级失败。区别很重要：工具级失败意味着「调用被理解并执行了，结果是失败」，维护期是「压根没执行」。

**影响面**：HTTP 语义新增一种状态码（503），MCP 语义新增一种错误码（-32000）。`/health` 返回体多一个键（向后兼容，见第 20.2 节）。

**回归**：`test_v011_rc5_restore_authority.py::TestMaintenanceHttpSemantics` 4 项。

---

## 7. `bigfish/gui.py`（51 insertions，3 deletions）

R8 的 GUI 半边：恢复前后必须停服，否则 GUI 自己就是那个「还持有旧连接」的进程。

### 7.1 恢复对话框（:1074-1109）

            # 走 council 层：内部会轮换执行票密钥、阻断在途任务、禁用全部远端身份、
            # 递增恢复世代（P1-4.2 + R1）。R8：整个替换期间停服并进入维护模式，
            # 期间任何写请求都拿到 503，不会出现「恢复失败但执行器以为成功」。
            self.win.pause_services()
            try:
                out = self.win.council.restore_from_bundle(
                    path, before_replace=self.win.pause_services)
            finally:
                self.win.resume_services()

`pause_services` 被调了两次：外层一次（进维护模式之前先断流），再作为 `before_replace` 传进去一次（真替换前再确认一次）。两次都靠幂等收敛（见下）。`finally` 里 `resume_services` 是必须的——恢复失败不清场，用户界面就永远连不上执行器。

结果提示补了两个计数：

            frozen = out.get("frozen_approved") or 0
            disabled = out.get("executors_disabled") or 0
    ...
                f"待派工任务已冻结：{frozen} 个（需重新提交审批）\n"
                f"远端身份已禁用：{disabled} 个（需人工重新授权并更换凭据）")

### 7.2 `pause_services`（:1222-1241）

        def pause_services(self):
            """R8：恢复备份前先排空——停 API 线程与看门狗，别再放新请求进来。

            幂等：restore_bundle 内部也会把它当 before_replace 调一次。
            """
            if self._services_paused:
                return
            self._services_paused = True
            try:
                self.server.started.disconnect(self.on_server)
            except Exception:
                pass
            try:
                self.server.stop()
            except Exception:
                pass
            try:
                self.council.stop_watchdog()
            except Exception:
                pass

三段分别 try/except 而不是一把包：某一处失败（比如线程已经退了）不该让后面两处也不停。停的顺序也有讲究——先 `disconnect` 信号槽，避免 `server.stop()` 触发 `on_server` 把「服务已停」画到界面上，紧接着又被 `resume` 盖掉。

### 7.3 `resume_services`（:1243-1256）

        def resume_services(self):
            """恢复结束后把服务接回来（失败也接，否则用户界面永远连不上执行器）。"""
            if not self._services_paused or self._closed:
                return
            self._services_paused = False
            try:
                self.server.started.connect(self.on_server)
            except Exception:
                pass
            self.server.start()
            try:
                self.council.start_watchdog(self.settings.watchdog_interval_sec)
            except Exception:
                pass

`self._closed` 判断是防关闭竞态：窗口正在关，不该再把服务器拉起来。`self._services_paused` 在 `__init__` 里初始化为 `False`（:1182）。

**影响面**：GUI 执行恢复的时序变成「停服 → 进维护 → 替换 → 退维护 → 起服」。用户会看到恢复期间监控页短暂空转。**未在真窗口验收**（见第 21 节）。

**回归**：`test_v011_rc5_restore_authority.py::test_restore_failure_exits_maintenance` 覆盖了「恢复失败仍退出维护」；GUI 路径本身只有离屏冒烟。

---

## 8. `bigfish/cli.py`（28 insertions，0 deletions）

R8 的跨进程半边，外加 R1 的 `reenable` 命令。

### 8.1 `_port_busy`（:41-49）

    def _port_busy(host: str, port: int, timeout: float = 0.4) -> bool:
        """端口上是否已经有服务在监听（只探本机回环，不发数据）。"""
        import socket
        target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
        try:
            with socket.create_connection((target, int(port)), timeout=timeout):
                return True
        except OSError:
            return False

**为什么要把通配地址折成 127.0.0.1**：配置里写 `0.0.0.0` 时，`create_connection(("0.0.0.0", port))` 在一些平台上语义不确定；通配地址等价于「监听所有网卡」，探本机回环就能判断有没有实例在跑。

### 8.2 restore 拒绝（:211-220）

        # R8 的跨进程那半边：维护模式只能管住**本进程**的连接，管不住另一个
        # 正在跑的 headless/GUI。替换文件时对方仍持有旧连接，POSIX 上会继续写
        # 旧 inode（数据静默分叉），Windows 上会直接替换失败。所以先探端口。
        if not a.force and _port_busy(s.api_host, s.api_port):
            print(f"拒绝恢复：{s.api_host}:{s.api_port} 上还有服务在监听，"
                  "请先停掉它（tools/stop-headless.ps1 或关闭 GUI），"
                  "确认没有其他实例后再重试；确实要强行恢复请加 --force。",
                  file=sys.stderr)
            return 2

退出码 2 而不是 1：1 在 CLI 里是「命令没匹配上」的兜底，2 是「明确拒绝执行」。

### 8.3 `--force` 与 `reenable`

    rs.add_argument("--force", action="store_true",
                    help="本机回环端口上还有服务在跑时，仍然强行恢复（危险）")

    re_en = sub.add_parser("reenable")
    re_en.add_argument("exec_id")

    if a.cmd == "reenable":
        dump(council.reenable_executor(a.exec_id))
        return 0

`reenable` 直接复用 `Council.reenable_executor`，输出就是那个含新 token 的 dict（`dump` 是 `json.dumps(indent=2)`）。

**影响面**：CLI 多一个子命令、restore 多一个开关、restore 多一个失败退出码。`--force` 是人工逃生门：端口被占时唯一出路，代价是拿数据一致性赌。

**回归**：`test_v011_rc5_cli_guard.py` 3 项。

---

## 9. `tools/bfc-proxy.mjs`（58 insertions，1 deletion）

R7 是审计里的发布阻断项：这个跳板不做任何鉴权，安全性完全建立在「只绑回环」这一条上。

### 9.1 安全边界注释（文件头）

    // 安全边界（审计 R7，发布阻断项）：
    // 这个跳板自身不做任何鉴权，/mcp* 与 /api/ 是原样透传到 Windows 侧 MCP 端口的，
    // 所以它整套安全性完全建立在「只绑回环 + 上层 Tailscale / SSH 隧道」之上。
    // 一旦监听地址被环境变量带去 0.0.0.0，等于把未鉴权的 MCP 直接挂到公网。
    // 因此监听地址必须是硬闸门：与 bigfish/server.py 的 check_bind_host 同一口径，
    // 判定放在 listen 之前，不合法立即 exit(1)，不允许被通用错误处理吞成正常退出。

### 9.2 `stripBrackets`（:26-30）

    function stripBrackets(host) {
      let h = String(host == null ? '' : host).trim();
      if (h.startsWith('[') && h.endsWith(']')) h = h.slice(1, -1);
      return h;
    }

注释说明为什么必须归一：Node 的 `listen` 不接受带方括号的 IPv6（会 `ENOTFOUND`），所以「接受 `[::1]`」不能只在校验时放行，绑定前也必须换成裸地址。

### 9.3 `isLoopback`（:39-50）

    function isLoopback(host) {
      if (typeof host !== 'string') return false;
      let h = stripBrackets(host);
      if (!h) return false;
      const lower = h.toLowerCase();
      if (lower === '127.0.0.1' || lower === 'localhost' || lower === '::1') return true;
      const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h);
      if (!m) return false;
      const octets = m.slice(1).map(Number);
      if (octets.some((n) => n > 255)) return false;
      return octets[0] === 127;   // 127.0.0.0/8 整段都是回环
    }

**为什么不用 `startsWith('127.')`**：会放过 `127.0.0.1.evil.com` 这类主机名；正则 + 逐段数值比较同时挡掉主机名与 `999.1.1.1` 这种越界字面量。

### 9.4 取值与闸门（:54-75）

    // 未设置才用默认值；显式设成空串属于配置错误，按不合法处理（下面会拒绝）。
    // 绑定用去括号后的形式，报错回显用原始值，方便对上写错的那条配置。
    const LISTEN_HOST_RAW = process.env.BFC_PROXY_LISTEN_HOST;
    const LISTEN_HOST = LISTEN_HOST_RAW === undefined ? '127.0.0.1' : stripBrackets(LISTEN_HOST_RAW);

    // 启动闸门：放行前先判定，不合法直接退 1，绝不进入 listen。
    // 用 fs.writeSync(2) 而不是 console.error：管道下 console 是异步写，
    // process.exit 可能把最后一行错误吞掉，测试与运维都会看到空 stderr。
    if (!isLoopback(LISTEN_HOST)) {
      const msg = [
        '拒绝启动：bfc-proxy 跳板只允许监听回环地址（127.0.0.1 / localhost / ::1 / 127.0.0.0/8）。',
        '实际拿到的 BFC_PROXY_LISTEN_HOST = ' + JSON.stringify(LISTEN_HOST_RAW),
        '原因：本代理不做鉴权，安全性完全依赖「只绑回环 + 上层 Tailscale/SSH 隧道」；',
        '如需跨机访问请走隧道，不要把它绑到 0.0.0.0。',
      ].join('\n');
      try { fs.writeSync(2, msg + '\n'); } catch { /* stderr 不可用时仍要退出 */ }
      process.exit(1);
    }

三个细节都有来历：`=== undefined` 而不是 `||`（空串是配置错误，不能悄悄退默认值）；`JSON.stringify` 回显原值（空串也能显示成 `""` 而不是空白）；`fs.writeSync(2, ...)` 同步写 stderr（异步写会被 `process.exit` 吞掉，测试看到的是空错误）。

**影响面**：`BFC_PROXY_LISTEN_HOST` 显式设成非回环值时，进程**启动即退出**（退出码 1）。以前能起来的配置现在起不来——这是刻意的破坏性变更。依赖 `0.0.0.0` 的部署必须改走隧道。

**回归**：`test_v011_rc5_proxy.py` 9 项（需 `node`，无 node 时整类 skip）。

---

## 10. `tests/test_v011_rc5_restore_authority.py`（新增，360 insertions；360 行 / 15400 字节 / 21 个 test）

覆盖 R1 全部与 R8 的库/HTTP 两半。`Base`（:32）建临时库，`approved()` 走完整审批链。

| 类 | 行 | 项数 | 关键断言 |
| --- | --- | --- | --- |
| `TestRestoreDisablesIdentities` | :74 | 3 | 恢复后 `executors_disabled >= 2` 且不许删行、`disabled_reason == DISABLED_REASON_RESTORE`；旧 access token 认证失败且文案含「已被禁用」「重新授权」「更换凭据」；reviewer token 同样被拒 |
| `TestReenableRotatesCredential` | :112 | 3 | 新 token 不等于旧 token、旧 token 立刻失效；`role` / `model` 保留、audit 含 `executor.reenabled`；`disabled_reason` 清空为 `None` |
| `TestRestoreFreezesApproved` | :145 | 2 | APPROVED 冻结成 BLOCKED、`blocked_reason` 含「冻结」、`frozen_approved >= 1`、重新授权后仍 claim 不到；在途任务 BLOCKED + 票 `revoked_at` 非空 + 新身份带旧票心跳抛错 |
| `TestClaimReplayAfterRestore` | :180 | 2 | 恢复后重放被拒；取消后重放被拒且文案含「取消」 |
| `TestMaintenanceMode` | :202 | 6 | 见下 |
| `TestMaintenanceHttpSemantics` | :289 | 4 | 见下 |

`TestMaintenanceMode` 六项逐条：

- `test_writes_rejected_inside_maintenance`（:225）：三个入口全覆盖——`council.create_task`、`db.query`、`with db.tx()` 各抛 `MaintenanceError`。
- `test_reads_recovered_after_exit`（:236）：退出后能正常建任务（不是只把异常开关关掉）。
- `test_maintenance_survives_exception`（:243）：内部抛 `RuntimeError` 后仍在 `finally` 退出，且之后可写。
- `test_enter_waits_for_inflight_transaction`（:250）：起一个持锁的写线程，`enter_maintenance(timeout=0.3)` 必须抛 `MaintenanceError`（不是假装排空成功）；线程放行后 `timeout=5` 能进入。
- `test_restore_always_exits_maintenance`（:285）：成功恢复后 `in_maintenance()` 为假且可写。
- `test_restore_failure_exits_maintenance`（:292）：`before_replace` 抛 `RuntimeError` 后仍退出维护，且原库 `executors.enabled` 仍是 1（证明没动库）。

`TestMaintenanceHttpSemantics` 四项：

- `test_api_returns_503_during_maintenance`（:317）：断言 `503`、`json()["maintenance"]` 为真、`headers["retry-after"] == "15"`。
- `test_mcp_returns_error_not_success_during_maintenance`（:326）：`/mcp` 返回 503，且响应体里**不含 `result`**——回了 result 就等于告诉执行器「票到手了」。
- `test_dispatch_tool_raises_maintenance_not_business_error`（:339）：直接调 `dispatch_tool`，在身份已认证通过的前提下必须抛 `MaintenanceError` 而不是 `CouncilError`。用例注释点明区别：业务失败让执行器以为「这次不可能成功」，维护模式必须表达成「这次没执行」。
- `test_health_reports_maintenance`（:352）：`status == "maintenance"` 且 `maintenance` 为真。

---

## 11. `tests/test_v011_rc5_receipts.py`（新增，362 insertions；362 行 / 15596 字节 / 24 个 test）

覆盖 R5 的收据日志全生命周期，分 5 段。

| 类 | 行 | 项数 | 关键断言 |
| --- | --- | --- | --- |
| `TestReceiptLog` | :45 | 6 | 写入读回、缺失目录自动建、未知事件抛 `ValueError`、坏行跳过不致命、`path=None` 纯内存、`BFC_RECEIPTS` 环境变量作默认路径 |
| `TestStateOf` | :107 | 6 | 只有 claimed → 「未开工」；有 started 无收尾 → 「结果未知」；finished → 「已完成」；failed/blocked/cancelled 都算收尾；未知编号 → 「未知」；summary 计数正确 |
| `TestDurability` | :169 | 2 | 见下 |
| `TestRunOnceReceipts` | :250 | 5 | 见下 |
| `TestClientWiring` | :326 | 5 | `None` / `False` 不落盘；`str` 路径可用；`ReceiptLog` 实例原样使用；`BFC_RECEIPTS` 未设时不落盘 |

`TestDurability` 两项是这组测试里最硬的两条：

- `test_record_is_flushed_to_disk_immediately`（:170）：`record` 之后**不碰对象缓存**，直接 `read_text` 读文件，必须能看到那条记录。
- `test_reader_in_new_process_sees_rows`（:179）：起一个子进程 `python -c` 读同一个文件并打印 `dispatch_id`，断言等于刚写的值——证明不是同进程缓冲骗人。

`TestRunOnceReceipts` 五项：

- `test_run_once_writes_claimed_started_finished_in_order`（:263）：事件序列必须**恰好**是 `["claimed", "started", "finished"]`；三个事件的 `dispatch_id` 全等于服务端下发的 `_FakeServer.DISPATCH_ID`；`state_of` 判「已完成」。
- `test_run_once_records_uncertain_and_reraises`（:278）：把 `submit` 换成抛 `_UncertainNetworkError`，断言序列是 `["claimed", "started", "uncertain"]` 且异常**继续往上抛**。
- `test_reconcile_lists_unknown_inflight`（:293）：两条派工，一条有 started 无收尾，`unknown_inflight` 只含那一条。
- `test_reconcile_without_any_receipt`（:307）：空收据返回 `total=0` 与空的 `unknown_inflight`。
- `test_dispatch_id_falls_back_to_ticket_id`（:311）：旧服务端响应没有 `dispatch_id` 时降级用 `ticket_id`，不报错。

假服务端 `_FakeServer`（:198）实现 claim / heartbeat / result 三个接口，让 `run_once` 能端到端跑通。

---

## 12. `tests/test_v011_rc5_review_audit.py`（新增，258 insertions；258 行 / 10966 字节 / 13 个 test）

覆盖 R2、R3、R4、R6 四条。

| 类 | 行 | 项数 | 关键断言 |
| --- | --- | --- | --- |
| `TestReviewPassedEvents` | :67 | 3 | 双通过后 `task.review_passed` 与 `task.pending_approval` 两条事件**真在 events 表里**；状态仍到 PENDING_APPROVAL；先投票再提交审查时事件也不丢 |
| `TestRevokeComment` | :95 | 3 | 撤评论不动已批准任务、不 BLOCKED 运行中任务；撤真票仍然打回 IN_REVIEW（反向保护） |
| `TestTimestampInsideTransaction` | :140 | 2 | 心跳后 `expires_at > renewed_at`；连发 5 次到期时间单调不减 |
| `TestAuditBundleAllowlist` | :179 | 5 | 见下 |

`TestAuditBundleAllowlist` 五项：

- `test_every_exported_column_is_on_the_allowlist`（:190）：遍历包里每张表的每一行每一个键，断言 `k in AUDIT_COLUMNS[table]`。
- `test_no_sensitive_column_names_leak`（:200）：不看 manifest、不看注释，把包里所有 JSON 的键名抽出来，用 `SENSITIVE_HINTS`（`token` / `hash` / `ticket` / `secret` / `key` / `password`）逐个扫，命中即失败。注意它是递归下钻到嵌套 dict/list 的。
- `test_manifest_declares_allowlist_policy`（:222）：`redaction.policy == "allowlist"`、`allowed_columns` 的键集恰好等于 `AUDIT_TABLES`、两个 `contains_*` 都是 false。
- `test_dropped_columns_reported_when_present`（:231）：`dropped_columns` 里出现的列名必须**不在**白名单内（即「跳过的都是本该跳的」）。
- `test_prompt_body_scrubbed_of_ticket`（:242）：往事件里塞一张真票（`log_event(tid, "leak.test", f"token={p['ticket']}")`），导出包里必须找不到明文票，且必须出现 `REDACTED`——证明是脱敏过而不是没导出。

---

## 13. `tests/test_v011_rc5_proxy.py`（新增，173 insertions；173 行 / 7873 字节 / 9 个 test）

`ProxyListenGateTest`（:73）一个类 9 项，`STARTUP_TIMEOUT = 3.0`，辅助函数 `_free_port` / `_spawn` / `_reap`，整个类挂在 `unittest.skipUnless(NODE, ...)` 下。

六个拒绝用例（`0.0.0.0`、`::`、`[::]`、空串、`192.168.1.5`、`8.8.8.8`）共用 `_expect_reject`，每项四条断言：

1. 必须在 3 秒内自行退出（超时就 `self.fail`，注释「还没退出就等于闸门没拦住，进程可能已经在 listen 上了」）；
2. 退出码非 0（为 0 说明被通用错误处理吞成正常退出）；
3. 输出含「拒绝启动」或「只允许监听回环地址」；
4. 输出回显实际拿到的 host 值，且**不含 `listening on`**。

三个放行用例：`127.0.0.1`、`127.0.1.2` 与 `localhost`、`[::1]`。`[::1]` 那条只要求「不出现拒绝字样」，因为容器里 IPv6 可能真的绑不上，把「能绑」也断言进去会让测试依赖环境。

---

## 14. `tests/test_v011_rc5_key_heal.py`（新增，137 insertions；137 行 / 5971 字节 / 7 个 test）

| 类 | 行 | 项数 | 关键断言 |
| --- | --- | --- | --- |
| `TestKeyFingerprintBootstrap` | :59 | 2 | 首次运行补记指纹时**不动任何票**（`tickets_revoked == 0`）；第二次运行是 `consistent` 空操作 |
| `TestKeyRotationInvalidatesTickets` | :78 | 5 | 见下 |

`TestKeyRotationInvalidatesTickets` 五项：

- `test_rotated_key_revokes_every_live_ticket`（:79）：两张活票，轮换后 `revoked_at` 全非空，`tickets_revoked == 2`。
- `test_old_ticket_cannot_authenticate_after_rotation`（:89）：换钥后旧票心跳抛错，**且任务状态仍是 RUNNING**——用例注释点明「吊销票不该顺手改任务状态」，两个语义要分开。
- `test_rotation_purges_claim_idempotency`（:106）：`claim` 幂等记录被清空。
- `test_rotation_leaves_audit_trail`（:115）：audit 里有 `action == "db.key_healed"`，`detail` 含「吊销」与张数。
- `test_missing_key_file_regenerates_and_revokes`（:125）：密钥文件被删后 `action == "new-key"`，同样吊销旧票。

---

## 15. `tests/test_v011_rc5_cli_guard.py`（新增，110 insertions；110 行 / 3450 字节 / 3 个 test）

`TestCliRestoreGuard`（:30）一个类 3 项，`setUp` 建临时目录 + 随机端口，`_run` 直接调 `cli.main`。

- `test_refuses_when_api_port_is_busy`（:43）：起一个真 socket 在配置端口 `listen`，`_run()` 必须返回 `2`。
- `test_force_bypasses_the_guard`（:78）：同样的占用，`_run("--force")` 断言 `assertRaises(RestoreError)`。断言方式是「拿到 `RestoreError` 而不是返回码 2」——如果 `--force` 只被 argparse 收下却没生效，这里会拿到 2。
- `test_port_busy_helper`（:98）：双向断言——未监听端口返回 `False`，已监听返回 `True`。

---

## 16. `tests/test_v011_rc5_dispatch_id.py`（新增，88 insertions；88 行 / 3538 字节 / 3 个 test）

`TestDispatchId`（:52）三项，全部围绕「编号稳定性」：

- `test_claim_returns_dispatch_id`（:53）：claim 响应含 `dispatch_id` 且等于 `ticket_id`。
- `test_replay_returns_same_dispatch_id`（:59）：同一幂等键重放，`dispatch_id` 不变。
- `test_redispatch_gets_new_dispatch_id`（:67）：重新派工（新票）后 `dispatch_id` 不同。

这三条合起来就是 R5 的契约：同一次派工编号稳定（收据不会重复），不同派工编号不同（旧收据不会被覆盖）。

---

## 17. 既有测试的语义级改动（4 个文件，96 insertions，9 deletions）

这四处**不是放宽断言，是收紧**。审计点名原写法恒真或过宽，改完每一条都能真正失败。

### 17.1 `tests/test_v011_p0.py`（27 insertions，4 deletions）

`test_db_file_has_no_plaintext_ticket`（:174）原来这么写：

    -        self.assertNotIn(p["ticket"].encode(), blob, "数据库里出现明文执行票")
    -        self.assertNotIn(b"SECRET-PROMPT-BODY", blob.replace(
    -            self.db.one("SELECT prompt FROM task_versions")["prompt"].encode(), b""))

第二句是**恒真**的：它把库里读出来的 prompt 从 blob 里减掉，再断言 `SECRET-PROMPT-BODY` 不在——减掉的那份就在库里，当然不在。docstring 直接写「审计点名：原写法把 prompt 从 blob 里减掉再断言它不在，恒真。」

现在是两条独立硬断言，外加一项补证：

        self.assertNotIn(ticket.encode(), blob, "数据库里出现明文执行票")
        self.assertNotIn(b"bfc_tk_", blob, "数据库里出现执行票前缀")

        row = self.db.one("SELECT * FROM tickets WHERE id=?", (p["ticket_id"],))
        self.assertTrue(row)
        self.assertEqual(row["token_hash"], hash_token(ticket))
        for k, v in dict(row).items():
            if isinstance(v, str):
                self.assertFalse(v.startswith("bfc_tk_"),
                                 f"tickets.{k} 里存了明文执行票")

新增 `test_prompt_body_written_to_task_versions`（:201）补住另一个洞：原来「库里没有 SECRET-PROMPT-BODY」可能只是因为**根本没写进去**。现在单独确认它确实落进了 `task_versions`：

        got = self.db.one("SELECT prompt FROM task_versions WHERE task_id=? AND version=1",
                          (tid,))["prompt"]
        self.assertEqual(got, "SECRET-PROMPT-BODY")

### 17.2 `tests/test_v011_p1.py`（38 insertions，3 deletions）

`test_watchdog_survives_sweep_error`（:80）原来只 `sleep(2.2)` 然后 stop，断言线程没卡住。docstring：「原版只断言 stop 后 `_watchdog` 变空，异常吞没与线程存活都没验证。」

现在要求三件事**同时**成立，再验证恢复后还能干活：

        self.c.sweep_timeouts = boom
        wd = self.c.start_watchdog(0.2)
        ...
            self.assertGreaterEqual(calls["n"], 3, "扫描根本没被反复调用")
            self.assertGreaterEqual(wd.errors, 3, "异常没有被计数")
            self.assertTrue(wd.is_alive(), "看门狗被单次异常打死了")

        # 恢复正常扫描：必须还能把超时任务置为 BLOCKED
        ...
        del self.c.sweep_timeouts          # 还原成类方法
        self.c.heartbeat_ttl = 0           # 立刻算超时
        ...
        self.assertEqual(self.c.get_task(tid)["status"], S.BLOCKED,
                         "异常恢复后看门狗没有继续工作")

第三条（`is_alive`）是关键：原来 `stop_watchdog` 不卡住既可能是「线程活着」，也可能是「线程早死了所以没有锁要抢」，原断言分不出这两种。

### 17.3 `tests/test_v011_p3.py`（21 insertions，0 deletions）

`test_clean_shutdown` 接受 `rc in (0, -15)`，等于「被信号打死」也算过。新增独立用例 `test_graceful_shutdown_on_sigint`（:192），docstring 直接点明这一点：

    @unittest.skipIf(os.name == "nt", "Windows 无 SIGINT 语义，走 TerminateProcess")
    def test_graceful_shutdown_on_sigint(self):
        ...
        self.proc.send_signal(signal.SIGINT)
        rc = self.proc.wait(timeout=20)
        self.assertEqual(rc, 0, "SIGINT 没有走优雅退出：\n" + ...)
        wal = Path(self.data) / "council.db-wal"
        # close() 里做过 wal_checkpoint(TRUNCATE)，WAL 要么不存在要么为 0 字节
        if wal.exists():
            self.assertLessEqual(wal.stat().st_size, 0,
                                 "优雅退出后 WAL 仍留有未合并的数据")

两条断言缺一不可：rc == 0 证明「自己收尾退出」而不是被信号杀；WAL 为 0 证明收尾里真的 checkpoint 了。原来的 `test_clean_shutdown` 保留未动（它测的是别的路径），新增用例只加不删。

### 17.4 `tests/test_v011_rc3.py`（10 insertions，2 deletions）

两处断言按语义更新，都是**跟着新行为改**，不是为了让测试变绿：

`test_revoke_and_submit_race`（:381）第 411 行：

    -            self.assertIn("吊销", str(out.get("submit_err")))
    +            # R1 之后「被管理员吊销」与「恢复后统一禁用」共用一条可执行文案
    +            # （都要求人工重新授权并更换凭据），所以这里断新文案而不是旧的「吊销」二字。
    +            self.assertIn("该身份已被禁用", str(out.get("submit_err")))
    +            self.assertIn("重新授权", str(out.get("submit_err")))

从断一个词变成断两个词，覆盖的信息量更大。

`test_restore_older_schema_migrates`（:507）第 528 行：

    -        self.assertEqual(self.status(tid), S.APPROVED)
    +        # R1：恢复后的库里不许留下「可领取」的授权。备份里那张 APPROVED 必须被冻结，
    +        # 否则这次恢复等于把一个早已批准、可能已被撤销的派工许可重新发出去。
    +        self.assertEqual(self.status(tid), S.BLOCKED)
    +        self.assertIn("冻结", self.db.one(
    +            "SELECT blocked_reason AS r FROM tasks WHERE id=?", (tid,))["r"])
    +        self.assertGreaterEqual(out["frozen_approved"], 1)

这条最能说明「不是放宽」：原来断言「恢复后还是 APPROVED」，正是 R1 那个缺陷本身；改成 BLOCKED + 原因含「冻结」+ 计数，三个断言比原来一个更严。

---

## 18. 回归实测

| 项 | 命令 | 结果 | 来源 |
| --- | --- | --- | --- |
| 全量单元测试 | `LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -t .` | `Ran 244 tests` / `OK` / exit 0（39.178s） | HEAD `b8f6de1`，日志 `/home/dsh/bfc-rc5-logs/unittest-linux.txt` |
| 静态检查 | `pyflakes bigfish tools tests` | 无输出 / exit 0 | 同上 |
| 自检 | `tools/selfcheck.py` | 16/16 | 同上 |
| GUI 冒烟 | `tools/gui_smoke.py` | 9/9（离屏） | 同上 |

244 的分组构成：`test_core` 15 + `test_api` 6 + `test_v011_p0` 16 + `test_v011_p1` 24 + `test_v011_p2` 24 + `test_v011_p3` 20 + `test_v011_rc3` 26 + `test_v011_rc4` 33 + 新增 rc5 七个文件 80 = 244。rc4 实测 162，rc5 增加 82 项（80 新增 + p0 与 p3 各 1）。

最慢两组是 `test_v011_rc5_proxy`（13.3s，要等 6 次进程启动超时判定）与 `test_v011_p3`（12.6s，含 SIGINT 与后台进程用例），都属预期。

---

## 19. schema 迁移 diff

`SCHEMA_VERSION` 与 `BACKUP_FORMAT_VERSION` 同步 3 → 4（`bigfish/db.py:22-23`）。`MIGRATIONS[4]`（:205-209）两条 SQL 原文：

    # R1：恢复后所有远端身份一律禁用，必须记下「为什么被禁」供人工核对
    "ALTER TABLE executors ADD COLUMN disabled_reason TEXT",
    "CREATE INDEX IF NOT EXISTS idx_executors_enabled ON executors(enabled)",

对应 `executors` 建表语句也同步改（:97，新库直接带列）：

    CREATE TABLE IF NOT EXISTS executors (
        ...
        enabled    INTEGER NOT NULL DEFAULT 1,
        disabled_reason TEXT,
        created_at REAL NOT NULL,
        ...
    );

**为什么加列而不是复用现有列**：`enabled=0` 只说明「禁了」，不说明「为什么禁」。R1 之后至少有两种禁用来源（管理员吊销 / 恢复后统一禁用），运维必须能区分「该找谁重新授权」。另起一列比往 `model` 或 `token_hash` 里塞字符串干净。

**为什么建 `idx_executors_enabled`**：恢复路径要 `UPDATE executors ... WHERE enabled=1`，认证路径要 `WHERE token_hash=? AND enabled=1`。表很小、索引收益有限，建它是为了「以后执行器变多也不用改方案」。

**迁移方式**：`_migrate_locked`（:412）未改，逐版本 `BEGIN IMMEDIATE` + 把 `duplicate column name` 视为已应用。因此「新库建表已带列」与「老库 ALTER 加列」两条路径都能到 v4，重复执行不报错。

**不可降级**：`DatabaseVersionError` 的既有逻辑（:415-419）在 `current > SCHEMA_VERSION` 时拒绝打开。rc4 的程序打开 rc5 的库会直接报版本过高，不会静默降级——这是设计选择，不是遗漏。

---

## 20. 兼容性影响

### 20.1 API 返回体新增 `dispatch_id`

`claim` 与 `_replay_claim` 的响应各多一个字段，值等于 `ticket_id`（第 4.5、4.6 节）。

- 向后兼容：老客户端忽略未知字段，行为不变。
- 执行端降级：`dispatch_id_of` 先取 `dispatch_id`，没有则退回 `ticket_id`（:208），所以新客户端配旧服务端也能落收据，只是收据里的编号语义退化成票号——同一次派工仍然稳定、重新派工仍然换号，对账结论不受影响。
- 契约：同一次派工编号稳定，重新派工换新号。这三条由 `test_v011_rc5_dispatch_id.py` 固定。

### 20.2 `/health` 新增 `maintenance`

返回体多一个布尔字段；`status` 多一个取值 `maintenance`（第 6.3 节）。

- 向后兼容：只读现有键的监控脚本不受影响。
- 需要留意的地方：把 `status` 当枚举硬编码处理的脚本（比如只认 `ok` / `degraded`）会在维护期落到未知分支。维护期很短（一次恢复），但应当知道有这个取值。

### 20.3 CLI 新增 `reenable` 子命令与 `--force`

`bigfish reenable <exec_id>` 输出 JSON，含新 token（第 8.3 节）。

- 不破坏既有调用：新子命令不与旧子命令重名。
- `restore` 的行为变了：端口被占时**默认拒绝**（退出码 2），原来会继续走。自动化脚本若在服务运行时调 restore，现在会拿到 2，需要显式加 `--force`。这是刻意的破坏性变更，因为原来的行为会产生「POSIX 上写旧 inode / Windows 上替换失败」的静默分叉。

### 20.4 HTTP 503 新语义

维护期所有业务接口返回 `503` + `Retry-After: 15` + `maintenance: true` 的 JSON 体（第 6.2 节）。

- 与业务错误的分界：业务失败仍是 `409`（`CouncilError`），参数错误是 `4xx`。执行器应当把 503 当「重试/对账」信号，不当作「这次不可能成功」。
- MCP 侧：`/mcp` 在维护期返回 HTTP 503，JSON-RPC 层错误码 `-32000`，响应体不含 `result`（第 6.4 节）。老客户端若只看 HTTP 状态码，会看到 503 而不是 200 + `isError`，需要走重试分支。
- 未知状态码风险：只处理 200/409 的客户端会在维护期走到默认分支。维护期只在恢复期间出现，但这个分支要能被看见。

### 20.5 环境变量与配置文件

| 变量 | 变化 |
| --- | --- |
| `BFC_RECEIPTS` | 新增。未设置时不落盘（内存 no-op）；设置了才开始写 JSONL |
| `BFC_PROXY_LISTEN_HOST` | 语义收紧。显式设成非回环值 → 进程启动即退出（退出码 1），原来是照绑不误 |
| `BFC_PROXY_LISTEN_HOST` 未设 | 行为不变，仍默认 `127.0.0.1` |

`BFC_PROXY_LISTEN_HOST=""`（显式空串）从「退回默认值」变成「拒绝启动」。空串是配置错误，静默退默认值等于让错误配置看起来正常。

### 20.6 数据与备份格式

- `BACKUP_FORMAT_VERSION` 3 → 4：rc5 生成的包 manifest 写 4，rc4 的程序读会按格式不符拒绝。
- rc4 生成的包（format 3）在 rc5 里仍可读（`inspect_bundle` 的兼容分支未改），恢复后 schema 会迁到 4。
- 审计包 manifest 的 `redaction` 结构变了（新增 `policy` / `allowed_columns`，`dropped_columns` 从数组变 dict）。拿旧结构解析审计包的脚本需要同步。

### 20.7 X4：`tickets_revoked` 改为真实计数（已并入本次提交）

收尾时补的一处语义修正，已随 rc5 提交一起入库：

- `bigfish/backup.py:352`：`_block_inflight` 里 `tickets_revoked` 从常量 `1` 改成 `conn.execute(...).rowcount` 的真实取值；`restore_bundle` 返回体相应新增 `tickets_revoked`（取 `cleanup.get("tickets_revoked", 0)`）。
- `tests/test_v011_rc5_restore_authority.py:176`：新增 `test_tickets_revoked_reports_real_count`，先领两张活票再恢复，断言 `out["tickets_revoked"] == 2` 且恢复后活票数为 0。
- 因此全量从 243 涨到 **244**，`tests/test_v011_rc5_restore_authority.py` 从 20 项变 21 项。本文所有数字已按 244 口径对齐。

---

## 21. 本节没做的事

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 平台 B（Windows 11 / Python 3.14.6 / SQLite 3.50.4）跑 rc5 的 244 项 | **未跑** | 本批次只在平台 A 执行；Windows 最后一次实测是 rc3 的 129 项 |
| rc4 的 162 项在平台 B 补跑 | **未跑** | 同上 |
| GUI 真窗口验收 `pause_services` / `resume_services` | **未验收** | 只在离屏（`QT_QPA_PLATFORM=offscreen`）下随全量回归跑过，没有人在真窗口点过「恢复」按钮看服务是否真的停了又起来 |
| 维护模式的跨进程行为 | **部分覆盖** | `db.maintenance` 只管本进程连接；跨进程靠 CLI 探端口。端口探测与真正替换之间存在 TOCTOU 窗口（探完到替换之间对方可能起来），`--force` 会完全绕过检查 |
| `ReceiptLog` 的对账闭环 | **仍人工** | `reconcile()` 只给出 `unknown_inflight` 列表，怎么处置（重派 / 人工核对副作用）没有自动化，也没有 UI |
| 恢复失败且回滚失败后的处置 | **只到「保留维护模式」** | `keep_maintenance = True` 之后进程会持续 503，需要人工介入重启并决定用哪个库；没有自动恢复路径 |
| `bfc-proxy.mjs` 在无 `node` 环境下的闸门验证 | **跳过** | `test_v011_rc5_proxy.py` 整类挂 `skipUnless(NODE)`，无 node 时 9 项全 skip，等于没验证 |
| `[::1]` 真绑定 | **未断言** | 该用例只要求「不出现拒绝字样」，容器内 IPv6 可用性不定，把「能绑」断言进去会让测试依赖环境 |
| `--force` 之后的数据一致性 | **无保障** | 逃生门的定义就是「明知道可能有另一个实例在写」；没有做替换后的分叉检测 |
| 审计包 `string_scrub` 的覆盖面 | **未扩展** | 仍是 rc4 的正则集（`bfc_*/Bearer/sk-*/gh*_*/AKIA*/private-key`），X2 记录的「只做正则脱敏」未变 |
| `_restore_lock` 的跨文件系统语义 | **未改** | 仍是 `fcntl.flock` / `msvcrt.locking`，只在同一文件系统内有效（X3） |

一句话总结 rc5：**把「恢复」从一次文件替换，变成一次有维护模式、有身份作废、有对账依据的操作**。244 项里 82 项是这次新加的，全部围绕着「恢复之后谁还能动」这一个问题。
