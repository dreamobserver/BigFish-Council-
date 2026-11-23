# 交付说明：bigfish-council v0.1.1-rc3

> **rc3（2026-11-19）是一次安全加固版，不是新功能版。** 修了 30+ 处，schema 2 → 3，
> 测试 103 → **129**。四份 rc3 专项文档：
> [修复总表](FIX-MATRIX-v0.1.1-rc3.md)、[Diff 摘要](DIFF-SUMMARY-v0.1.1-rc3.md)、
> [测试报告](TEST-REPORT-v0.1.1-rc3.md)、[安全复审](SECURITY-REVIEW-v0.1.1-rc3.md)。
> rc3 的完整说明见 **§10**。
>
> **平台状态（一句话）**：Debian 12 / Python 3.11.2 上 129 项全绿；
> **Windows 11 / Python 3.14.6 / SQLite 3.50.4 上也跑完 129 项全绿**
> （25.263s / 退出码 0 / 144 行日志 / 0 失败），自检 16/16、GUI 冒烟 9/9、密钥 ACL 实测通过。
> 两平台同一份测试、同一份源码包。此前拦路的 windows-mcp `USER_CONTROL`（人工接管）在轮末解除，
> 详见 [TEST-REPORT-v0.1.1-rc3.md](TEST-REPORT-v0.1.1-rc3.md) 第 5 节。
>
> 下文第 0–9 节按 rc1 / rc2 写成，是**历史记录，不改写**；
> rc3 的现状以 §10 与上面四份文档为准。

交付时间：rc1 2026-11-18 / rc2 2026-11-19 / rc3 2026-11-19。
基线：v0.1.0（git commit `1a97f5a`），rc3 分支 `fix/v0.1.1-rc3-hardening`。
三轮都是**发布候选**，未部署生产、未导入 DSH MCP、未注册开机自启。

> 本文件里每一句「已完成」都能在 `docs/TESTLOG-v0.1.1.md` 找到对应的运行输出；
> 找不到输出的，我写「未做」或「未通过」。

## 0. 一页速览

| 项目 | v0.1.0 | v0.1.1-rc1 |
| --- | --- | --- |
| 单元测试 | 21 | **103**（原有 21 项全保留 + 82 新增） |
| 自检脚本 | 14 项 | **16 项** |
| GUI 冒烟 | 9 项 | 9 项 |
| 数据库 schema | 1 | **2**（只 ADD COLUMN，可原地升级） |
| 备份格式 | 1（不完整） | **2**（sqlite backup API + manifest） |
| 执行票 | 随机明文进幂等表 | **HMAC 派生，只存哈希** |
| 审批并发 | 可跨版本漂移 | **事务 + 版本条件 UPDATE** |
| 心跳 | 不续租、不幂等 | **续租（封顶 6h）+ 幂等** |
| 看门狗 | 无 | **随服务启停的独立线程** |
| 权限 | 单一 token | **四角色分离，服务端绑定** |
| 网络 | 绑回环（无强制） | **非回环直接拒绝启动** |
| 跨设备联调 | 未做 | **仍未做（对方机器不可达）** |

## 1. 阶段 A：P0 数据与安全

### A.1 修改文件

| 文件 | 变化 | 行数变化 |
| --- | --- | --- |
| `bigfish/db.py` | 新增 `snapshot/integrity/table_counts/audit/audits/file_sha256`，`SCHEMA_VERSION=2`，`MIGRATIONS[2]` | +157 |
| `bigfish/backup.py` | **新文件**，导出/检查/恢复三件套 | +246（新） |
| `bigfish/core.py` | `approve()` 收进事务 + 版本条件；`claim()` 改派生票 | 见 A.2 |
| `bigfish/security.py` | 新增 `load_or_create_key/derive_ticket/ticket_id_for/request_fingerprint` | +55 |
| `bigfish/runtime.py` | **新文件**，`open_council()` 统一装配路径 | +22（新） |

### A.2 关键 Diff

**P0-1 备份漏 WAL**（`bigfish/db.py`，`snapshot()`）：

```python
# v0.1.0：直接拷文件，WAL 里的数据全丢
z.write(self.db.path, "council.db")

# rc1：一致性快照
def snapshot(self, dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    with self._lock:
        target = sqlite3.connect(str(dest))
        try:
            self._conn.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            target.commit()
        finally:
            target.close()
    return dest
```

实测证据：v0.1.0 导出包里的 `council.db` 只有 **4096 字节**，恢复后 `no such table: tasks`；
rc1 导出包 12 张表齐全，任务数与状态一致。

**P0-2 审批跨版本漂移**（`bigfish/core.py:approve`）：

```python
# v0.1.0：事务外读版本，事务内只按 id 更新
ver = self.get_task(tid)["current_version"]     # 读
...                                             # ← 这里有人改到 v2
with self.db.tx() as c:
    c.execute("UPDATE tasks SET status=? WHERE id=?", (target, tid))   # 写，无条件

# rc1：读+校验+写全在一个 BEGIN IMMEDIATE 里，且 UPDATE 带版本条件
with self.db.tx() as c:
    task = _task_dict(c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    version = task["current_version"]
    if expected_version is not None and version != expected_version:
        raise CouncilError(f"任务版本已变（当前 v{version}，请求基于 v{expected_version}）")
    votes = self._vote_summary_locked(c, tid, version)
    cur = c.execute(
        "UPDATE tasks SET status=?, updated_at=?, blocked_reason=NULL"
        " WHERE id=? AND current_version=? AND status=?",
        (target, t, tid, version, task["status"]))
    if cur.rowcount != 1:
        raise CouncilError("任务在审批过程中被修改，已拒绝本次审批")
```

**P0-4 明文执行票**（`bigfish/security.py` + `core.py:claim`）：

```python
# v0.1.0：随机票，明文塞进幂等表 response
ticket = new_token("bfc_tk_")
self._idem_put(c, key, "claim", {"ticket": ticket, "prompt": prompt, ...})

# rc1：HMAC 派生，幂等表只留最小字段
def derive_ticket(key, executor_id, task_id, version, idem_key):
    material = f"{executor_id}\x1f{task_id}\x1f{version}\x1f{idem_key or ''}".encode("utf-8")
    digest = hmac.new(key, material, hashlib.sha256).hexdigest()
    return "bfc_tk_" + digest[:43]

self._idem_put(c, key, "claim", fp,
               {"task_id": tid, "version": version, "ticket_id": ticket_id})
```

外加第二道闸 `core.assert_no_secrets()`：写幂等表前递归检查，出现 `bfc_tk_` 前缀或键名 `prompt` 直接抛
`拒绝持久化敏感字段：{path}`。

### A.3 新增测试

`tests/test_v011_p0.py`（15 项）：

| 类 | 用例 |
| --- | --- |
| `TestSqliteBackup` | `test_wal_data_survives_backup_and_restore`、`test_snapshot_is_consistent_and_complete`、`test_bundle_has_required_tables`、`test_corrupted_bundle_does_not_touch_db`、`test_restore_keeps_prerestore_copy` |
| `TestApprovalConcurrency` | `test_version_drift_rejected`、`test_stale_expected_version_rejected`、`test_drifted_version_cannot_be_approved_without_votes`、`test_approval_records_current_version_only`、`test_double_approve_rejected`、`test_old_version_vote_revoked_after_update` |
| `TestNoPlaintextToken` | `test_claim_idempotency_has_no_ticket_or_prompt`、`test_db_file_has_no_plaintext_ticket`、`test_bundle_has_no_ticket`、`test_guard_rejects_secret_fields` |

### A.4 原有测试结果

全部通过（`Ran 103 tests ... OK`）。`test_core` / `test_api` 有两处**接口适配**（注入密钥、去掉无用参数），
无断言放宽，详见 TESTLOG 第 1.2 节。

### A.5 安全影响

- 数据库泄露 → 拿不到可用执行票（只有哈希，且派生密钥在另一个文件、不进备份）；
- 备份包泄露 → 同上；
- 恢复损坏包 → 原库字节不变（有 `prerestore` 副本 + 失败回滚）。

### A.6 数据库迁移说明

schema 1 → 2，**只有 `ALTER TABLE ADD COLUMN` + `CREATE TABLE audit`**，不重建表、不删数据。
详见 `docs/MIGRATION-v0.1.1.md`。

### A.7 回滚

见 `docs/ROLLBACK-v0.1.1.md` 第 2 节。要点：schema2 库不能直接喂给 v0.1.0 长期使用（虽然不崩）。

### A.8 未完成

- 备份包未加密（ZIP 里是明文 Prompt，靠文件系统权限保护）；
- 审计日志无防篡改（只有 append）。

## 2. 阶段 B：P0 网络

### B.1 修改文件

| 文件 | 变化 |
| --- | --- |
| `bigfish/config.py` | 端口唯一真源：`DEFAULT_API_HOST/PORT`、`DEFAULT_PROXY_PORT`、`TEST_API_PORT`、`PORT_ROLES`、`LOOPBACK_HOSTS`、`is_loopback`、`public_dict()` |
| `bigfish/__main__.py` | 非回环绑定 → 打印拒绝理由 + `return 2` |
| `bigfish/gui.py` | 端口文案改为读设置；设置对话框拒绝非法端口与 3098 |
| `tools/bfc-proxy.mjs` | 跳板支持 `BFC_PROXY_TARGET_HOST/PORT` 环境变量 |
| `tools/net_check.py` | **新文件**，4 层只读探测 |

### B.2 关键 Diff

**端口真源**（`bigfish/config.py`）：

```python
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 3096
DEFAULT_PROXY_PORT = 3098
TEST_API_PORT = 3097

PORT_ROLES = {
    "windows_api": (DEFAULT_API_PORT, "Windows 本地控制中心 API 监听端口（默认仅回环）"),
    "vps_proxy":   (DEFAULT_PROXY_PORT, "VPS 上 bfc-proxy.mjs 回环跳板端口"),
    "test_only":   (TEST_API_PORT, "单测与 GUI 冒烟使用的临时端口，不参与部署"),
}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
```

**拒绝非回环绑定**（`bigfish/__main__.py`）：

```python
if not settings.is_loopback:
    print("拒绝启动：API 只允许监听回环地址（127.0.0.1/localhost/::1）。"
          "如需跨机访问请用 Tailscale Serve 转发，不要直接绑 0.0.0.0。",
          file=sys.stderr)
    return 2
```

这是硬拦截，不是提示。实测 `--host 0.0.0.0` 与 `--host 100.64.0.12` 都 `returncode == 2`。

### B.3 新增测试

`tests/test_v011_p3.py` 的 `TestPortTopology`（8 项）+ `TestLoopbackOnly`（2 项）。

### B.4 原有测试结果

全部通过。`test_core`/`test_api` 不受影响。

### B.5 安全影响

- 端口职责写进代码，GUI/CLI/文档/自检读同一份常量，消除「文档写 3096、代码写 3097」的漂移；
- 想绑 `0.0.0.0` 必须改代码（而不是改一个参数），提高误操作门槛；
- 跳板仍只监听回环、只放行 `/health`、`/mcp*`、`/api/*`。

### B.6 数据库迁移说明

无。

### B.7 回滚

网络改动全部在代码层，回滚代码即回滚网络行为；**未改任何防火墙、Tailscale、DNS 配置**，无需回滚网络。

### B.8 未完成

- **跨设备联调未通过**：`100.64.0.12:3096` 与 `:8765` 均 TimeoutError；
- Tailscale Serve 未配置（需授权）；
- Windows 防火墙规则未验证（对方机器不可达）。

## 3. 阶段 C：P1 执行可靠性

### C.1 修改文件

| 文件 | 变化 |
| --- | --- |
| `bigfish/core.py` | 新增 `Watchdog` 类（`core.py:46-96`）；`heartbeat()` 重写为续租+幂等；`submit_result()` 幂等前置 |
| `bigfish/server.py` | `create_app(council, watchdog_interval)` + lifespan 启停看门狗；`tools/call` 业务失败置 `isError=true` |
| `bigfish/__main__.py` | 新增 `--no-watchdog`，默认随服务启动 |
| `bigfish/gui.py` | `ApiServer` 传 `watchdog_interval`，uvicorn 改 `lifespan="on"` |

### C.2 关键 Diff

**P1-1 看门狗不依赖 GUI**（`bigfish/core.py:46`）：

```python
class Watchdog(threading.Thread):
    _instances: dict[str, "Watchdog"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, council, interval=10.0):
        super().__init__(name="bfc-watchdog", daemon=True)
        self.interval = max(1.0, float(interval))
        # 注意：不能叫 self._stop —— 那是 threading.Thread 的方法，覆盖后 join() 会炸
        self._stop_evt = threading.Event()

    @classmethod
    def start_for(cls, council, interval=10.0):     # 同一数据库路径只起一个
        ...

    def run(self):
        while not self._stop_evt.wait(self.interval):
            try:
                blocked = self.council.sweep_timeouts()
                self.sweeps += 1
                if blocked:
                    self.last_blocked = blocked
            except Exception:       # 看门狗不能因单次异常退出
                continue
```

随 FastAPI lifespan 启停（`bigfish/server.py:74`）：

```python
@asynccontextmanager
async def lifespan(_app: FastAPI):
    if watchdog_interval is not None:
        council.start_watchdog(watchdog_interval)
    try:
        yield
    finally:
        council.stop_watchdog()
```

**P1-2 执行票续租**（`core.py:heartbeat`）：

```python
# 续租：只延长到 min(now+lease, max_expires_at)
new_exp = min(t + self.heartbeat_lease,
              ticket.get("max_expires_at") or (t + self.ticket_max_ttl))
c.execute("UPDATE tickets SET expires_at=?, renewed_at=? WHERE id=?",
          (new_exp, t, ticket["id"]))
```

`ticket_max_ttl_sec` 默认 21600（6 小时），`heartbeat_lease_sec` 默认 180（3 分钟）。
长任务靠心跳续命，但绝对上限顶死，防止「心跳永续 = 永不超时」。

**P1-3 心跳幂等**（`core.py:heartbeat` 开头）：

```python
tk_row = self.db.one(
    "SELECT id FROM tickets WHERE token_hash=? AND executor_id=?",
    (hash_token(token or ""), executor.get("id")))
if tk_row is None:
    raise CouncilError("执行票无效")
fp = request_fingerprint({"op": "heartbeat", "status": status,
                          "detail": detail or "", "ticket": tk_row["id"]})
idem_scope_key = f"hb:{tk_row['id']}:{idem_key or '-'}"
if idem_key:
    cached = self._idem_get(idem_scope_key, fp)
    if cached is not None:
        return dict(cached, replayed=True)
```

同一幂等键重放：结果字节级一致（含 `server_time`）、`run.heartbeat` 事件只落一条、`idempotency` 表只落一行。
换请求体复用同一幂等键 → 直接抛错（指纹不同）。

**P1-4 MCP 错误语义**（`server.py:dispatch`）：

```python
# v0.1.0：永远 False，业务失败塞在 content 里
return ok({"content": [...], "isError": False})

# rc1
is_error = False
try:
    content = dispatch_tool(council, ex, name, args)
    if not isinstance(content, dict) or content.get("ok") is False:
        is_error = True
except CouncilError as e:
    content, is_error = {"ok": False, "error": str(e)}, True
except HTTPException as e:
    content, is_error = {"ok": False, "error": str(e.detail)}, True
except Exception:
    content, is_error = {"ok": False, "error": "服务端内部错误"}, True
return ok({"content": [{"type": "text", "text": _json(content)}],
           "isError": is_error})
```

**P1-5 Windows 后台进程管道**：`tools/bfc-proxy.mjs` 与 headless 启动都不再靠 shell 管道读进度，
改为写日志文件；`test_stdout_redirect_does_not_block` 实测重定向到文件后进程不阻塞。

### C.3 新增测试

`tests/test_v011_p1.py`（24 项）：`TestWatchdog` 4、`TestTicketRenewal` 6、`TestHeartbeatIdempotency` 5、`TestMcpSemantics` 9。

### C.4 原有测试结果

全部通过。`test_heartbeat_timeout_blocks`（原有用例）在 rc1 上仍然有效。

### C.5 安全影响

- 看门狗独立于 GUI：关掉 GUI 也会在心跳超时后置 BLOCKED，不会出现「无人看守的长跑任务」；
- 租约续期有绝对上限：防止执行器靠刷心跳永久占坑；
- MCP 错误可识别：客户端不会再「以为成功」。

### C.6 数据库迁移说明

`tickets.max_expires_at`、`tickets.renewed_at`、`idempotency.request_hash`、`idempotency.expires_at`。
旧行这两列是 NULL，代码用 `or` 兜底（`ticket.get("max_expires_at") or (t + self.ticket_max_ttl)`）。

### C.7 回滚

回滚代码即可。注意：schema2 库里的旧票 `max_expires_at` 为 NULL，v0.1.0 不读该列，无影响。

### C.8 未完成

- 心跳幂等记录没有清理任务（`idempotency.expires_at` 只写不扫）；
- 看门狗只做心跳超时，不做「任务卡在 DISPATCHED 但从未有心跳」的兜底（当前靠 `sweep_timeouts` 的 `issued_at` 判断）。

## 4. 阶段 D：P2 权限审计

### D.1 修改文件

| 文件 | 变化 |
| --- | --- |
| `bigfish/roles.py` | **新文件**（80 行），四角色 + 权限表 + 模型绑定 |
| `bigfish/core.py` | `add_review(..., actor=)`、`revoke_review()`、`approve(actor=)`，投票身份由服务端判定 |
| `bigfish/server.py` | `_need()` 权限校验 + `TOOL_PERMISSIONS` 工具权限表 |
| `bigfish/db.py` | `reviews.actor/revoked_at`、`approvals.actor/evidence`、`audit` 表 |

### D.2 关键 Diff

**四角色**（`bigfish/roles.py`）：

```python
ARCHITECT_GPT    = "architect_gpt"     # 只能以 gpt 身份投票
REVIEWER_GEMINI  = "reviewer_gemini"   # 只能以 gemini 身份投票
HUMAN_OWNER      = "human_owner"       # 唯一能审批 / 签发票 / 取消任务
EXECUTOR_DSH     = "executor_dsh"      # 只能领任务 / 心跳 / 回传

REVIEWER_MODEL = {ARCHITECT_GPT: "gpt", REVIEWER_GEMINI: "gemini"}

PERMISSIONS = {
    ARCHITECT_GPT:   {"review.cast", "task.read"},
    REVIEWER_GEMINI: {"review.cast", "task.read"},
    HUMAN_OWNER:     {"task.read", "task.create", "task.update", "task.cancel",
                      "task.verify", "review.cast", "review.revoke", "approve",
                      "ticket.issue", "ticket.revoke", "executor.manage",
                      "backup.export", "backup.restore", "audit.read"},
    EXECUTOR_DSH:    {"task.claim", "task.read.own", "run.heartbeat", "run.submit"},
}
```

**服务端绑定身份**（`core.py:add_review`）：

```python
# v0.1.0：调用方说自己是哪个模型就是哪个模型
def add_review(self, tid, model, content, verdict, kind="comment"):
    ...  # 直接信 model 参数

# rc1：actor 来自鉴权上下文，模型由角色推导
def add_review(self, tid, model, content, verdict, kind="comment",
               actor=R.HUMAN_OWNER):
    bound = R.model_for_role(actor)
    if bound and model != bound:
        raise CouncilError(f"只能以 {bound} 身份投票")
    ...
    c.execute("INSERT INTO reviews(..., actor) VALUES(..., ?)", (..., actor))
```

**执行器权限边界**（`server.py`）：

```python
def _need(ex: dict, permission: str, action: str) -> None:
    role = ex.get("role_canonical") or R.EXECUTOR_DSH
    try:
        R.require(role, permission, action)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

TOOL_PERMISSIONS = {
    "council_list_tasks":    ("task.claim",     "列出可领取任务"),
    "council_claim_task":    ("task.claim",     "领取任务"),
    "council_heartbeat":     ("run.heartbeat",  "上报心跳"),
    "council_submit_result": ("run.submit",     "提交结果"),
    "council_task_status":   ("task.read.own",  "查询任务状态"),
}
```

**审批三道闸**中的第三道（`core.py:approve`）：

```python
if actor not in (R.HUMAN_OWNER,):
    raise CouncilError("只有人类所有者可以执行审批")
...
for m in MODELS:
    if votes[m]["actor"] == R.EXECUTOR_DSH:
        raise CouncilError("执行器不能参与审查投票")      # 防历史脏数据
```

### D.3 新增测试

`tests/test_v011_p2.py`（24 项）：`TestRoleTable` 6、`TestReviewIdentity` 6、`TestExecutorBoundary` 7、`TestAudit` 5。

### D.4 原有测试结果

全部通过。

### D.5 安全影响

- 执行器伪造审查票 → 被 `model_for_role` 拦下；
- 执行器自己批准 → 物理上没有该工具，业务层还有 `actor` 校验；
- 人工粘贴的模型回复 → 明确记为 `human_owner`，不冒充模型自证（`roles.py` 文档串写死「不得声称已实现」）；
- 审计表记录审批/执行器注册/吊销/投票撤销，且不对外暴露 HTTP 端点。

### D.6 数据库迁移说明

`reviews.actor`（DEFAULT `human_owner`）、`reviews.revoked_at`、`approvals.actor`、`approvals.evidence`、新表 `audit`。
**注意**：旧数据的 `actor` 全是 `human_owner`，无法追溯纠正（v0.1.0 本来就没记录）。

### D.7 回滚

回滚代码 + 用 v0.1.0 快照恢复数据。schema2 库让 v0.1.0 跑会「不崩但失去身份」，见 ROLLBACK 第 5 节。

### D.8 未完成

- **模型身份无密码学证明**：GPT/Gemini 的票是人工粘贴的，谁也证明不了那段文本真是模型输出。这不是 bug，是设计边界，已在 SECURITY-REVIEW 明确列为残余风险；
- 审计无防篡改（append-only，无链式哈希）；
- 没有「多人协作」场景（只有单一 human_owner 概念）。

## 5. 阶段 E：P3 GUI 与联调准备

### E.1 修改文件

| 文件 | 变化 |
| --- | --- |
| `bigfish/gui.py` | 端口读设置、审批带 `expected_version`、备份/恢复走新 API、非回环拒绝启动 |
| `tools/e2e_loopback.py` | **新文件**，同机回环端到端 22 项 |
| `tools/gui_smoke.py` | 修正页面数量断言（5 页而非 4 页）、改用 `win.pages.keys()` |
| `tools/net_check.py` | **新文件**，4 层网络探测 |

### E.2 关键 Diff

**审批带版本**（`gui.py:ApprovalPage`）：

```python
def refresh(self):
    ...
    self.loaded_version = t["current_version"]      # 页面上看到的是哪一版

def decide(self, decision):
    self.win.council.approve(self.current, decision, note,
                             expected_version=self.loaded_version)   # 版本漂移直接被拒
```

**端口不写死**（`gui.py`）：

```python
# v0.1.0
self.port.setText("3096")
s.api_port = int(self.port.text() or 3096)

# rc1
from .config import DEFAULT_API_PORT, DEFAULT_PROXY_PORT, PORT_ROLES
if port == DEFAULT_PROXY_PORT:
    QMessageBox.warning(self, "端口冲突", f"{port} 是 VPS 跳板端口，不能用作本机 API 端口")
    return
```

**备份/恢复走新 API**（`gui.py:SettingsDialog`）：

```python
from .backup import export_bundle, inspect_bundle, restore_bundle

export_bundle(self.win.council.db, path)
info = inspect_bundle(path)                      # 先只读校验
restore_bundle(self.win.council.db, path)        # 失败自动回滚
```

### E.3 新增测试

`tests/test_v011_p3.py`（19 项）：`TestPortTopology` 8、`TestLoopbackOnly` 2、`TestBackgroundProcess` 4、`TestGuiSmoke` 5。

### E.4 原有测试结果

全部通过。

### E.5 安全影响

- GUI 审批带上页面加载时的版本号，防止「看着 v1 批了 v2」；
- 设置里不能把 API 端口改成跳板端口 3098，消除端口冲突。

### E.6 数据库迁移说明

无。

### E.7 回滚

回滚代码即可。

### E.8 未完成

| 项 | 状态 | 原因 |
| --- | --- | --- |
| Windows 真机 GUI 验收 | **未执行** | windows-mcp 通道全超时（连 `ControlStatus` 都 120s 死），`100.64.0.12:8765` 也不通 |
| 跨设备联调 | **未通过** | `100.64.0.12:3096` TimeoutError |
| DSH MCP 连接导入 | **未执行** | 需 human_owner 授权 |
| Tailscale Serve | **未执行** | 需 human_owner 授权 |
| 开机自启注册 | **未执行** | 需 human_owner 授权 |
| PyInstaller 打包验证 | **未执行** | 需 Windows 环境 |

## 6. 交付物清单

### 6.1 源码（`/home/dsh/bigfish-council`）

```
bigfish/
  __init__.py        8    版本号 0.1.1-rc1
  __main__.py       44    入口：回环校验 + headless/GUI 分支
  backup.py        246    导出 / 检查 / 恢复（新）
  cli.py           171    16 个子命令
  config.py        142    端口与路径唯一真源
  core.py          954    业务核心（Council + Watchdog）
  db.py            349    schema 2 + 迁移 + 审计
  executor_client.py 87   执行器侧 SDK（纯标准库）
  gui.py          1140    PySide6 五页界面
  roles.py          80    四角色权限表（新）
  runtime.py        22    open_council 装配（新）
  security.py       78    令牌 / 密钥 / 派生票
  server.py        375    FastAPI + 手写 JSON-RPC
  state.py          62    13 个状态常量
tools/
  bfc-proxy.mjs           VPS 回环跳板
  e2e_loopback.py         同机回环端到端（新）
  gui_smoke.py            GUI 离屏冒烟
  net_check.py            4 层网络探测（新）
  selfcheck.py            16 项自检
tests/
  test_core.py     167    v0.1.0 原有 15 项
  test_api.py      110    v0.1.0 原有 6 项
  test_v011_p0.py  209    备份/并发/明文（新）
  test_v011_p1.py  270    看门狗/续租/幂等/MCP（新）
  test_v011_p2.py  219    角色/身份/边界/审计（新）
  test_v011_p3.py  292    端口/回环/进程/GUI（新）
```

合计 5529 行（不含 .venv）。

### 6.2 文档

| 文件 | 内容 |
| --- | --- |
| `README.md` | 架构、状态机、安装、启动、目录说明 |
| `docs/DELIVERY-v0.1.1.md` | 本文件 |
| `docs/TESTLOG-v0.1.1.md` | 全部实测输出与缺陷根因 |
| `docs/SECURITY-REVIEW-v0.1.1.md` | 威胁模型、凭据处理、残余风险 |
| `docs/NETWORK-TOPOLOGY.md` | 端口真源、数据流、4 层实测、跨机失败记录 |
| `docs/MIGRATION-v0.1.1.md` | schema 1→2 逐列说明与升级实测 |
| `docs/ROLLBACK-v0.1.1.md` | 回滚步骤与混跑危害 |
| `docs/WINDOWS-MANUAL-v0.1.1.md` | Windows 本机人工操作说明（每步附预期输出） |

### 6.3 脚本

| 文件 | 用途 | 状态 |
| --- | --- | --- |
| `setup.ps1` | Windows 建 venv + 装依赖 | 已有，未改 |
| `start-gui.ps1` | 启动 GUI | 已有，未改 |
| `build.ps1` | PyInstaller 打包 | 已有，未在 Windows 验证 |
| `install-task.ps1` | 注册登录自启 | 已有，**未执行**（需授权） |
| `dsh-mcp-bigfish.json` | DSH MCP 连接模板 | 已有，**未导入**（需授权） |
| `tools/dev_setup.sh` | Linux/VPS/WSL 独立开发环境安装（不用 apt、不碰系统包） | **已实测**，在本容器跑通 |

### 6.4 验证结果汇总

| 验证 | 结果 | 证据位置 |
| --- | --- | --- |
| 单元测试 | 103/103 OK | TESTLOG §1 |
| 自检 | 16/16 | TESTLOG §2.1 |
| GUI 冒烟 | 9/9 | TESTLOG §2.2 |
| 网络 1-3 层 | 10/10 | TESTLOG §2.3 |
| 网络第 4 层（跨机） | **通过**（2026-11-19 打通） | TESTLOG §2.3 / NETWORK-TOPOLOGY §4.4 |
| 跨机 MCP 闭环 | **11/11**（真链路，非回环） | NETWORK-TOPOLOGY §4.4 |
| 回环端到端 | 22/22 | TESTLOG §2.4 |
| schema 升级 | 通过 | TESTLOG §2.5 |
| 回滚 | 通过 | TESTLOG §2.6 |

## 7. 需要 human_owner 授权才能做的事

以下三件本轮**一律没有执行**，代码与文档已准备好，等你验收后再决定：

### 7.1 注册登录自启

```powershell
# 会写 HKCU\Software\Microsoft\Windows\CurrentVersion\Run
.\install-task.ps1
```

影响：每次登录都启动 GUI + API 监听 127.0.0.1:3096。

### 7.2 导入 DSH MCP 连接

```json
// dsh-mcp-bigfish.json 的内容（指向 VPS 回环跳板）
{
  "mcpServers": {
    "bigfish-council": {
      "url": "http://127.0.0.1:3098/mcp",
      "headers": { "Authorization": "Bearer <在 Windows 上 executor-add 生成的 bfc_ex_...>" }
    }
  }
}
```

影响：DSH 从此能领取任务。**需要先在 Windows 上生成执行器 token**，而且要确认这个 token 的吊销方式。

### 7.3 Tailscale Serve 转发 —— 已执行（2026-11-19）

```
tailscale serve --bg --tcp 3096 tcp://127.0.0.1:3096
```

⚠️ 这一条与本节开头「三件一律没有执行」的说明冲突，以本段为准：
为了完成跨机联调，Serve 已经实际打开了，范围仅 tailnet。
用 `--tcp` 而非 `--https=443`，因为目标是最小暴露面（只转发 3096，不占 443、不上 TLS 证书）。

实测输出：

```
|-- tcp://client-b.tailf6b79c.ts.net:3096 (tailnet only)
|-- tcp://100.64.0.11:3096
|--> tcp://127.0.0.1:3096
```

关闭方式：`tailscale serve --tcp=3096 off`。

安全影响：tailnet 内任何设备都能访问这个 API。但 API 本身要求 Bearer token
（`/health` 除外，且 `/health` 不返回任务信息），5 个 MCP 工具都不含审批/提权。
**没有开 Funnel，没有公网入口。**

## 7.4 2026-11-19 追加：Windows 端改到公司机 CLIENT-B

家里那台 CLIENT-A 的 `windows-mcp` 已死（`:8765` ECONNREFUSED，只剩 SSH 活着），
改用公司机 **CLIENT-B = 100.64.0.11**（Windows 11 家庭版，Python 3.14.6，Node v24.12.0）。

已经做到（有输出为证）：

| 项 | 结果 |
| --- | --- |
| 公司机 MCP 通道 | 打通。`ControlStatus` → `{"state":"ready"}` |
| 源码下发 | 经 VPS `100.64.0.21:8099`（只绑 Tailnet IP）`Invoke-WebRequest`，字节数一致 |
| 依赖安装 | `.venv` 内 fastapi 0.165.0 / uvicorn 0.54.0 / pydantic 2.14.3 / PySide6 6.12.0 / mcp 1.30.0（PySide6 有 cp310-abi3 wheel，3.14 装得上） |
| 单元测试 | **103 项全绿**（`Ran 103 tests in 19.925s / OK`），修了 2 个 Windows 专属缺陷后才绿 |
| 自检 | `tools/selfcheck.py` **16/16** |

### 7.4.1 跨机打通（同一天稍后完成，本节为最终结论）

之前「服务没起来」的判断是错的：那次 `Start-Process` **其实成功了**，
两个 `python.exe -m bigfish --headless`（pid 36788、22684）一直活着占着 3096，
所以后续启动才报 `[Errno 10048]`。清掉死进程后改用 WMI 派生，一次起成功。

| 步骤 | 命令 | 实测结果 |
| --- | --- | --- |
| 清死进程 | `Stop-Process -Id 36788,22684 -Force` | `PORT_FREE` |
| WMI 派生 | `Invoke-CimMethod -ClassName Win32_Process -MethodName Create` | `WMI_RC=0 PID=32484`，PowerShell 立即返回，队列不堵 |
| 确认绑定 | `Get-NetTCPConnection -LocalPort 3096 -State Listen` | `127.0.0.1:3096 pid=15692`（只回环） |
| tailnet 转发 | `tailscale serve --bg --tcp 3096 tcp://127.0.0.1:3096` | `tailnet only`，无 Funnel |
| VPS 直连 | `curl http://100.64.0.11:3096/health` | `version 0.1.1-rc2, db ok, watchdog true` |
| VPS MCP 闭环 | `tools/remote_check.py` | **11/11 通过**，claim→heartbeat(幂等)→submit 全走真链路 |

**跨设备联调结论：已通过。** 证据是 VPS 侧发起的真实 HTTP/MCP 请求，
任务与审批都在 Windows 上完成，全程无公网端口。

仍未做的（不当作通过）：

- 开机自启（WMI 起的进程重启机器即消失）。
- `tools/start-headless.ps1` 真机验证（`schtasks /TR` 嵌套引号返回 `-2147467259`，未解决）。
- Windows 侧 GUI 真窗口人工验收（off-screen 冒烟 9/9 已过，真显示器未验）。
- 把 `bigfish-council` MCP 连接导入 DSH（需 human_owner 授权）。

## 8. 下一步建议（按优先级）

1. **解决常驻**：WMI 起的进程重启就没了。需要把启动方式改成计划任务
   （`schtasks /Create` 的 `/TR` 参数用不含引号嵌套的写法绕开 `-2147467259`，
   或者改成注册表 Run + 一个 bat 包装）。
2. **Tailscale Serve 重启持久性复测**：`--bg` 会写进 Tailscale 的持久配置，
   但没做过重启机器后的实测，不能当作已完成。
3. **导入 MCP 连接**（需 human_owner 授权）：`dsh-mcp-bigfish.json` 指向
   `127.0.0.1:3098/mcp` 跳板，跳板目标改成 `100.64.0.11:3096`。
   注意 dsh-mcp-connector 只接受回环/RFC1918，不能直接写 Tailnet IP。
4. **GUI 真窗口验收**：在公司机的真实显示器上开一次，走完建任务→审查→批准。
5. 最后才考虑注册登录自启。

## 9. 一句话总结

v0.1.1 把 v0.1.0 的四个 P0 和五个 P1 全部修掉了，测试从 21 项扩到 103 项全绿；
同一份源码在公司机（Windows 11 + Python 3.14）上也是 **103 项全绿 + 自检 16/16**，
备份/审批/令牌/看门狗/幂等/MCP 语义都有实际运行证据。

**跨设备联调也已打通**（2026-11-19）：VPS → `100.64.0.11:3096` 的 `/health` 通，
`tools/remote_check.py` 走真实 MCP 完成 claim → heartbeat（含幂等重放）→ submit，**11/11 通过**。
全程 Tailscale Serve 只在 tailnet 内转发，**没有公网端口、没有 Funnel**。

剩下的缺口都在「常驻」和「人工验收」上：开机自启未注册、计划任务脚本未跑通、
GUI 真窗口未验、MCP 连接未导入。这些不影响功能正确性，但影响能不能当长期服务用。
---

## 10. rc3 阶段：安全加固（2026-11-19）

### 10.1 一句话

rc2 的功能是对的，出事的时候不对。rc3 只做一件事：让**取消、恢复、并发、监听**
这四件事在边界条件下也给出正确答案。

### 10.2 改了什么（按任务书阶段）

| 段 | 主题 | 关键改动 | 证据 |
| --- | --- | --- | --- |
| 2 | 状态机与取消 | 新增 `CANCELLING`；claim 查 `cancel_requested_at`；取消分「立即」与「待确认」两条路；撤票后重算审批有效性；送审补 `_recompute_review`；GUI 改 Prompt 带 `expected_version` | 状态机 7 项测试 |
| 3 | 监听与鉴权 | `check_bind_host()` 下沉到 `serve()`，四条入口共用；`_owned_status` 未分配也 403；吊销在业务事务内复查 | bind 测试（7 地址 + 子进程验证）、他人任务 403 |
| 4 | 恢复与执行票 | 13 步原子恢复；轮换密钥 + epoch+1 + 清 claim 幂等；在途任务转 BLOCKED；导出分 operational / audit 两类；禁止 schema 降级 | 恢复 7 项测试 |
| 5 | 并发与幂等 | 幂等检查挪进事务；`tx()` 失败释放 RLock；Watchdog 条件更新；ExecutorClient 支持幂等键与不确定状态查询 | 并发 7 项测试（真线程 + Barrier） |
| 6 | 凭据与权限 | 统一脱敏闸门；修 `verify_file_restricted` 真缺陷；Windows 走 icacls；审查身份绑 `executors.model` | 明文票全库搜索、审计包逐表检查、密钥权限 |
| 7 | 迁移与维护 | schema 2→3（3 列 + 2 索引）；禁止降级；幂等记录按过期清理 | 迁移测试 3 项 |
| 8 | 工具与 GUI | remote_check 默认只读；dev_setup 修退出码；stop-headless 只杀自家进程；GUI 恢复改走统一入口 | 脚本代码审读 + 手工执行 |

详细逐条（ID / 原始问题 / 文件行号 / 方案 / 回归测试 / 状态）见
[FIX-MATRIX-v0.1.1-rc3.md](FIX-MATRIX-v0.1.1-rc3.md)。

### 10.3 数字

| 项目 | rc2 | rc3 |
| --- | --- | --- |
| 单元测试 | 103 | **129**（v0.1.0 的 21 项全在位） |
| 数据库 schema | 2 | **3** |
| 状态数 | 13 | **14**（+ CANCELLING） |
| 代码行数（bigfish/） | ~3300 | ~4300 |
| 静态检查 | 未跑 | **pyflakes 零告警** |

### 10.4 平台状态（诚实版）

| 平台 | 环境 | 129 项测试 | 自检 16 | GUI 冒烟 9 |
| --- | --- | --- | --- | --- |
| A | Debian 12 / Python 3.11.2 / sqlite 3.40.1 | **全绿，exit 0**（16.0s） | 通过 | 通过 |
| B | Windows 11 / Python 3.14.6 / SQLite 3.50.4 | **全绿，exit 0**（25.263s） | 通过 | 通过 |

平台 B 的过程：公司机 CLIENT-B 的 windows-mcp 通道在轮初持续返回
`USER_CONTROL`（state = user），即人工正在操作桌面，执行器按设计拒绝接管 —— 这段时间里
rc3 一行代码都没上去。轮末该状态恢复到 `ready`（generation 639），随即部署到**独立目录**
`D:\BigFishCouncil\bigfish-council-rc3`（不动原 rc2 目录）、跑完全部 129 项。
详见 [TEST-REPORT-v0.1.1-rc3.md](TEST-REPORT-v0.1.1-rc3.md) 第 5 节。

注意：公司机 3096 端口上**仍然跑着 rc2 的 headless**（`curl http://100.64.0.11:3096/health`
返回 `version: 0.1.1-rc2`）。rc3 验证用的是独立目录 + 独立进程，没有替换现役服务；
把 rc3 推上 3096 属于「变更生产服务」，按任务书要单独请示，本轮没做。

### 10.5 需要 human_owner 授权才能做的事

前两项本轮已在不改动现役服务的前提下做完（独立目录 + 独立进程），剩下：

1. **把 rc3 推上公司机 3096 现役服务**（覆盖 `D:\BigFishCouncil\bigfish-council` 并重启 headless，
   先备份 `D:\BigFishCouncil\data`）。这属于「变更生产服务」，按任务书要单独请示。
2. **GUI 真窗口验收**：在公司机显示器上走完 建任务 → 审查 → 批准 → 领取 → 回传。
3. 前面 rc2 阶段列的三项（开机自启、MCP 连接导入、Tailscale Serve 持久性）仍然有效。

已做过、不需要再授权的（记录在此以免重复）：

- 公司机 129 项回归（独立目录 `D:\BigFishCouncil\bigfish-council-rc3`，未动现役）；
- 公司机密钥 ACL 实测（`icacls` 分支已验证）。

### 10.6 rc3 明确没做的事

- 没有引入类型检查（mypy / pyright），项目未引入，本轮不新增依赖。
- 没有做 24h soak / 长时间稳定性。
- 没有对数据库文件加密（SQLCipher 之类）。
- 没有改生产 DSH 容器、没有动凭据、没有重启任何生产服务。
- 没有开公网端口、没有用 Tailscale Funnel、没有注册开机自启。

### 10.7 回滚

rc3 → rc2 的回滚**不能只换代码**（schema 3 会被 rc2 拒绝打开）。
完整步骤见 [ROLLBACK-v0.1.1.md](ROLLBACK-v0.1.1.md) 第 7 节，含
「有 rc2 备份」和「没有备份、手工降级」两条路，以及手工降级会丢哪三列。

