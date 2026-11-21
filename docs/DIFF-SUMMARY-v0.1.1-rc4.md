# 代码 Diff 摘要：v0.1.1-rc3 → v0.1.1-rc4

分支 `fix/v0.1.1-rc4-hardening`，HEAD 为 `2ea5d1b`（`fix(B2): 恢复原子化——恢复锁/暂存迁移/世代与密钥成对替换/失败成对回滚/key指纹自愈`），仓库无 tag。
rc3 之后的改动分两层：HEAD 提交里已含的 B2 原子化主体，与工作树里尚未提交的 rc4 增量。

规模按 `git diff HEAD --stat` 实际输出：**21 files changed, 2421 insertions(+), 219 deletions(-)**。

    CHANGELOG.md 87+ / README.md 30 / bigfish/__init__.py 2 / bigfish/backup.py 2-
    bigfish/config.py 17+ / bigfish/core.py 136 / bigfish/executor_client.py 55
    bigfish/gui.py 132 / bigfish/roles.py 8+ / bigfish/server.py 50 / build.ps1 78
    docs/ARCHITECTURE-FUTURE.md 889+ / docs/NETWORK-TOPOLOGY.md 75+ / setup.ps1 44
    tests/test_v011_rc4.py 473+ / tools/dev_setup.sh 18 / tools/net_check.py 50
    tools/remote_check.py 153 / tools/selfcheck.py 21 / tools/start-headless.ps1 219
    tools/stop-headless.ps1 101

本文覆盖代码、工具脚本、新增测试与新增架构文档。`CHANGELOG.md`、`README.md`、`docs/NETWORK-TOPOLOGY.md` 属其他交付物，不在本文范围。

## 0. 两层改动怎么读

`bigfish/db.py`、`bigfish/runtime.py`、`bigfish/__main__.py` 在 `git diff HEAD` 里看不到，改动已在 `2ea5d1b` 内；核对要用 `git diff 832f699`（rc3 发布点，29 files changed, 1750 insertions(+), 453 deletions(-)）。

| 变更层 | 文件 | 对应节 |
| --- | --- | --- |
| HEAD `2ea5d1b` | `db.py` 97/13、`runtime.py` 7/0、`__main__.py` 21/8、`backup.py` 主体、`gui.py` 大部分 | 4、5、8 |
| 工作树 | `config.py`、`roles.py`、`core.py`、`executor_client.py`、`server.py`、工具脚本 | 1、2、3、6、7、9–17 |
| 新增 | `tests/test_v011_rc4.py`、`docs/ARCHITECTURE-FUTURE.md` | 18、19 |

---

## 1. `bigfish/config.py`（17 insertions，0 deletions）

`PORT_ROLES`（:27）之后新增**平行**字典 `PORT_RESERVED`（:37），6 条：

    PORT_RESERVED = {
        3099: ("mobile_rest",  "手机端 REST 网关（计划中，未实现）"),
        3100: ("mobile_sse",   "手机端 SSE/长连接推送（计划中，未实现）"),
        3101: ("mcp_bridge",   "对外 MCP 桥接（计划中，未实现）"),
        8765: ("windows_mcp",  "Windows 侧 windows-mcp 的 HTTP 端口（外部进程，非本项目）"),
        18765: ("wmcp_proxy_a", "VPS 跳板：客户端 A 的 windows-mcp 回环转发"),
        18766: ("wmcp_proxy_b", "VPS 跳板：客户端 B 的 windows-mcp 回环转发"),
    }

三条铁律注释（:45-49）：本项目自己监听的端口只能绑回环、跨机一律走 Tailscale Serve；预留端口在真正实现前不得出现在 `PORT_ROLES` 或任何默认值里；8765 / 18765 / 18766 属于外部进程，本项目只连不听。`LOOPBACK_HOSTS`（:50）未改。

**为什么**：不并入 `PORT_ROLES` 的原因写在注释里 —— `tests/test_v011_p3.py` 断言 `PORT_ROLES` 恰好是 `windows_api` / `vps_proxy` / `test_only` 三行职责，塞进未实现端口会让测试与文档一起失真。预留表只做「先占号、不实现」。

**影响面**：运行时不读 `PORT_RESERVED`，无代码路径依赖；`tools/selfcheck.py` 同轮把默认端口从 3099 改到 3094（第 10 节），两处互为因果；`docs/ARCHITECTURE-FUTURE.md` 第 2 节引用此表。

**回归**：`TestAuxiliary::test_reserved_ports_do_not_collide`（:459，预留端口与 `PORT_ROLES` 不相交）。

---

## 2. `bigfish/roles.py`（8 insertions，0 deletions）

    _DB_ROLE_MAP = {"executor": EXECUTOR_DSH, "reviewer": None, "admin": HUMAN_OWNER}
    DB_ROLES = frozenset(_DB_ROLE_MAP)          # :48

注释说明：未知 `db_role` 会走 `role_for_db_row()`（:73，`base = _DB_ROLE_MAP.get(db_role)`）兜底成 `EXECUTOR_DSH`，所以 `mobile` 这类未来角色必须**显式登记**，现在故意不登记。

**为什么**：兜底逻辑把不认识的 `db_role` 一律当执行器，写进库就拿到执行器权限；白名单把这个隐式行为变成可断言的边界。

**影响面**：实测 `DB_ROLES` 只被 `tests/test_v011_rc4.py:466-468` 引用（`assertIn("executor")` / `assertIn("admin")` / `assertNotIn("mobile")`）；`core.py:153-162` 的 `register_executor` 仍用手写元组 `("executor", "reviewer", "admin")` 校验，未改为引用 `DB_ROLES`，两处并存。

**回归**：`TestAuxiliary::test_roles_db_whitelist`（:465）。

---

## 3. `bigfish/core.py`（105 insertions，31 deletions；相对 rc3 为 133/35）

十二条语义改动，关键代码行与理由：

| # | 位置 | 关键代码行 | 为什么 |
| --- | --- | --- | --- |
| 1 | :36 | `EFFECTIVE_APPROVAL_STATES = {APPROVED, DISPATCHED, RUNNING, CANCELLING, SUBMITTED, DONE}`；判定改 `task["status"] in EFFECTIVE_APPROVAL_STATES`（:684） | 黑名单让新增状态默认落进「有效」一侧 |
| 2 | :276 | `list_tasks(..., statuses=None, offset=0)`；`ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?`（:296） | 同一秒创建的多条任务原排序不稳，翻页重复或漏项 |
| 3 | :300 | 新增 `get_task_status_row(tid) -> dict \| None`，只 SELECT 九列，找不到返回 `None` 不抛 | `get_task()` 一次要跑 5 条查询，高频轮询太重 |
| 4 | :319 | `ctl.pop("cancel_requested")` / `ctl.pop("pause_requested")`；UPDATE 增 `cancel_requested_at=NULL, blocked_reason=NULL, executor_id=NULL, control=?` | `claim` 带 `AND cancel_requested_at IS NULL`，取消过的任务改版后永远领不到 |
| 5 | :520/:595 | SQL 去掉 `AND revoked_at IS NULL`，每模型只留最新一张；新增 `revoked` 字段 | 撤票后会把更早那张赞成票顶上来 |
| 6 | :705 | `ticket_nonce = idem_key or ("rnd-" + uuid.uuid4().hex)`（:746）；先 `UPDATE tickets SET revoked_at=? WHERE task_id=? AND revoked_at IS NULL`（:763）再普通 INSERT | 无幂等键时派生票恒定；`INSERT OR REPLACE` 会静默覆盖历史票、丢掉吊销证据 |
| 7 | :793 | `nonce = cached.get("nonce") or cached.get("_idem") or None` | 重放必须派生同一张票，老记录没有 `nonce` 键 |
| 8 | :1082 | `allowed = (SUBMITTED,) if accept else (SUBMITTED, BLOCKED)` | `BLOCKED` 含义是「副作用不明」，点通过不能证明执行真完成 |
| 9 | :1124 | 先 `SELECT id FROM tickets WHERE task_id=? AND revoked_at IS NULL ORDER BY issued_at DESC LIMIT 1`，再按 `ticket_id` 查 `runs.last_beat_at`（:1142） | 原按 `task_id` 取最新 `runs` 行，上一轮心跳被当成本轮 |
| 10 | :79 | `Watchdog.key_for(db_path)` staticmethod，返回 `str(Path(db_path).expanduser().resolve())` | 相对路径与绝对路径被算成两个单例 |
| 11 | :1181 | `mine = self._watchdog`；`if Watchdog._instances.get(key) is mine: pop`；再 `mine.stop()` | 原来无条件 `stop_for(db_path)`，会停掉 GUI/CLI 启动的看门狗 |
| 12 | :1199 | `parts = key.split(":", 3)`；`if len(parts) == 4 and parts[0] == "claim": data.setdefault("_idem", ...)`（:1215） | 幂等键里的 `idem` 本身可能带冒号，全量 split 会截断 |

三处值得单独看原文的实现：

    # 5  vote_summary / _vote_summary_locked（:520 / :595）
    gone = bool(r["revoked_at"])
    result[r["model"]] = {"verdict": None if gone else r["verdict"],
                          "at": r["created_at"], "actor": r["actor"], "revoked": gone}
    for m in MODELS:
        result.setdefault(m, {"verdict": None, "at": None, "actor": None, "revoked": False})   # 缺省模型补齐

    # 6  claim（:705）
    ticket_nonce = idem_key or ("rnd-" + uuid.uuid4().hex)                    # :746
    ticket = self._derive_ticket(ex_id, tid, version, ticket_nonce)          # :747
    c.execute("UPDATE tickets SET revoked_at=? WHERE task_id=?", (t, tid))    # :763 先吊销旧票
    c.execute("INSERT INTO tickets(id,task_id,...) VALUES(?,?,...)")         # 原 INSERT OR REPLACE
    # 幂等响应体新增 "nonce": ticket_nonce                                   # :776

    # 3  get_task_status_row（:300）
    "SELECT id,title,status,current_version,executor_id,blocked_reason,"
    " cancel_requested_at,control,updated_at FROM tasks WHERE id=?"
    d["control"] = loads(...);  d["status_label"] = S.label(d["status"])

**影响面**：`vote_summary` 返回值多 `revoked` 键；`claim` 响应多 `nonce` 键，外部直读 `tickets` 表须处理被吊销的旧票；`list_tasks` 新增两个可选参数，位置调用仍兼容；`verify(accept=True)` 拒绝 `BLOCKED`，人工流程要先退回再重跑。

**回归**：`TestReviewBinding` 四项（1/5）、`TestMultiRoundTickets` 六项（6/7/8）、`TestAuxiliary` 四项（2/3）、`TestIdempotencyContract` 两项冒号键（12），用例名见第 18 节。第 4、9、10、11 条无专用用例，仅靠全量回归兜底。

---

## 4. `bigfish/db.py`（HEAD 内，97 insertions / 13 deletions）

**先识别版本，再决定能不能碰文件**（`__init__` :213）：

    self._closed = False                                                    # :218
    self._conn.execute("PRAGMA busy_timeout=5000")                          # :222
    ver = self._peek_schema_version()                                       # :242
    if ver > SCHEMA_VERSION:
        self._closed = True
        raise DatabaseVersionError(f"数据库 schema 版本 {ver} 高于本程序支持的 {SCHEMA_VERSION}，拒绝打开……")
    self._conn.execute("PRAGMA journal_mode=WAL")                           # 通过版本检查后才走到这里

原写法一上来就 `PRAGMA journal_mode=WAL`，那会改写文件头并生成 `-wal`；对本该只读拒绝的未来版本库，程序先把人家文件动了。新增 `_peek_schema_version()`（:242）与 `_peek_schema_version_locked()`（:297）：只读查 `sqlite_master` 的 `meta` 表与 `schema_version` 键，0 表示全新库，不建表、不写 `meta`、不改 `journal_mode`。

`init_schema()`（:268）改为**先迁移后建表**，末尾断言版本等于 `SCHEMA_VERSION`：

    current = self._peek_schema_version_locked()
    if current == 0: self._conn.executescript(SCHEMA)      # 全新库直接建
    else:
        self._migrate_locked(current)                      # 旧库先迁移
        self._conn.executescript(SCHEMA)                   # 再补最新索引/表

原顺序先 `executescript(SCHEMA)` 再迁移，v1 库会在 `CREATE INDEX ... idempotency(expires_at)` 处直接炸 `no such column`，迁移没机会跑。`_migrate_locked(current=None)`（:335）签名加参数；`MIGRATIONS` 补 `CREATE INDEX IF NOT EXISTS idx_audit_created ON audit(created_at)`（:191，`SCHEMA` :168 同名）。

`close()`（:432）幂等：`_closed` 已真直接 return（:437），`wal_checkpoint(TRUNCATE)` 失败吞掉，`conn.close()` 捕 `sqlite3.ProgrammingError`。`_reopen()`（:447）重置 `_depth = 0`、`_closed = False`（:456）。恢复流程要「关库 → 替换文件 → 重开库」，三步都可能中断或被重复调用，幂等让回滚分支能安全重入。

**影响面**：打开「未来版本库」从静默降级改为抛 `DatabaseVersionError` 退出；v1/v2 老库升级依赖 `MIGRATIONS` 顺序正确。**回归**：由 `tests/test_v011_rc3.py`（26 项）覆盖，rc4 未新增 `db.py` 用例。

---

## 5. `bigfish/backup.py`（工作树 0 insertions / 2 deletions；相对 rc3 为 292/110）

工作树删的 2 行是 `key_relocated = False` 及其置 True（已无用途，改用 `key_installed`）。实质改动都在 HEAD 提交里：

| # | 位置 | 改动 | 为什么 |
| --- | --- | --- | --- |
| 1 | :312 | `_restore_lock(lock_path)` 恢复锁 | 两个进程同时恢复会互相覆盖 |
| 2 | :357 | `_prepare_staging(...) -> tuple[int, dict]` | 纪元与计数要在替换前算准 |
| 3 | :505 / :512 | `swapped` / `key_installed` 成对替换 | 库与密钥不能只换一个 |
| 4 | :439 起 | 失败成对回滚 | 回滚不能只回一半 |
| 5 | :588 | `_count_blocked(db)` 真实回填 | 原写死 0 是假数据 |
| 6 | :593 | `ensure_key_consistency` 指纹自愈 | 库与密钥不同步被静默带过 |

**恢复锁**：POSIX `fcntl.flock` / Windows `msvcrt.locking(LK_NBLCK)`；拿不到锁抛 `RestoreError("另一个进程正在恢复这个数据库（恢复锁被占用），请稍后重试")`；使用点 `with _restore_lock(original_dir / (target.name + ".restore.lock")):`。

**暂存迁移**：在 staging 上一个事务内写 `restore_epoch = max(captured_epoch, staged_epoch) + 1`、`last_restore_at`、`key_sha256`、`_block_inflight`、`audit("db.restored", ...)`，成功后 `conn.execute("PRAGMA journal_mode=DELETE")`（:401），返回 `(new_epoch, cleanup)` —— 返回元组是为了让调用方在替换失败时按 `cleanup` 精确回填。

    swapped = True          # :505  os.replace(staging, target) 之后
    key_installed = True    # :512  os.replace(key_new, key_path) 之后

替换前把原密钥 `copyfile` 到 `key_path + ".prerestore"` 并 `secure_file`。回滚顺序：`if db_closed or swapped` → `db.close()`；`if swapped and had_prerestore` → copyfile 到 `.rollback` 再 `os.replace` 回 target；`if key_installed and key_backup` → `os.replace` 回 `key_path`；随后 `db._reopen()`。回滚自身失败抛 `RestoreError("恢复失败且回滚未完全成功：…（原始错误：…）")`；`finally` 删 staging 与残留 `key_new`。

**指纹自愈**：`ensure_key_consistency(db, key_path) -> dict` 比对 `meta.key_sha256` 与磁盘密钥 sha256，不一致则 `rotate_key` + 写回指纹 + `audit("db.key_healed")`，返回 `{"ok": True, "action": "no-key" | "no-fingerprint" | "consistent" | "rotated"}`；由 `runtime.open_council` 调用并包在 try/except 里，失败不阻断启动。`restore_bundle` 返回值新增 `key_backup_path` / `prerestore_path` / `prerestore_copy`。

**影响面**：恢复期间目标目录出现 `.restore.lock` / `.prerestore` / `.restore-new` / `.rollback` 临时文件（成功与异常路径都清理）；启动时多一次密钥 sha256。**回归**：由 `tests/test_v011_rc3.py` 覆盖，rc4 未覆盖。

---

## 6. `bigfish/executor_client.py`（46 insertions，9 deletions）

    # BLOCKED 不算：心跳超时也会把任务打成 BLOCKED，跟我们这次提交无关
    RECONCILED_OK = ("SUBMITTED", "DONE", "CANCELLED")                                    # :142
    def safe_submit(self, ticket: str, task_id: str | None = None, **kwargs) -> dict:     # :144
    def safe_claim(self, task_id: str, idem: str | None = None, retries: int = 1) -> dict:   # :164
    def safe_heartbeat(self, ticket: str, status: str = "running", detail: str = "",
                       idem: str | None = None, retries: int = 1) -> dict:               # :175

- `task_id` 由 `kwargs.get("task_id")` 改为显式形参：`submit()`（:104）没有 `task_id` 形参，旧写法恒为 `None`，等于「不确定时永远直接抛」。
- `safe_submit` 捕 `_UncertainNetworkError`（:28）后先查 `task_status`（:88），命中 `RECONCILED_OK` 才返回 `{"ok": True, "task_status": status, "reconciled": True}`；否则抛 `RuntimeError("提交结果未知：任务当前状态 …，不能证明本次结果已落库，请人工核对后再决定是否重提")`；没给 `task_id` 时抛 `RuntimeError("提交结果未知，且调用方没给 task_id，无法核对")`。
- `safe_claim` / `safe_heartbeat` 用 `key = idem or new_idem_key("claim"|"hb")`（:24），不确定时复用同一幂等键重试，服务端返回历史票，不会双发。

`BLOCKED` 进成功集合会让执行器把「心跳超时导致的状态」当成本次结果已送达；反过来，不确定时盲发会造两套结果。

**影响面**：`safe_submit` 调用点必须显式传 `task_id`；旧 `claim`（:92）/ `heartbeat`（:97）直调路径仍在，未强制迁移。**回归**：`TestExecutorClient` 四项 —— `test_safe_submit_requires_task_id`、`test_blocked_is_not_treated_as_success`、`test_submitted_is_treated_as_success`、`test_safe_claim_reuses_same_key`。

---

## 7. `bigfish/server.py`（45 insertions，5 deletions）

| 位置 | 改动 |
| --- | --- |
| :24 | `from fastapi.concurrency import run_in_threadpool` |
| :192 | `jsonrpc` 必须是字符串 `"2.0"`，否则 `-32600` |
| :199 | 缺 `method` 回 `-32600 缺少 method` |
| :201 | 真通知（无 id）回 202 |
| :219 | `ping` 带 id 回 `ok({})` |
| :240 | `dispatch_tool` 进线程池 |
| :270 | 新增 `_validate_args(spec, args)`；`_mcp_error` 在 :295 |

    if not isinstance(msg.get("jsonrpc"), str) or msg.get("jsonrpc") != "2.0":
        return _mcp_error(msg.get("id"), -32600, "jsonrpc 必须是 2.0")
    if not isinstance(method, str) or not method:
        return _mcp_error(rid, -32600, "缺少 method")
    if method == "ping":
        # ping 带 id 就是请求，必须回 result；回 202 会让客户端一直等超时   # :220
        return ok({})
    bad = _validate_args(tool_specs()[name], args)
    if bad: raise CouncilError(bad)
    content = await run_in_threadpool(dispatch_tool, council, ex, name, args)   # :240

`_validate_args` 校四件事、返回中文文案（`None` 通过）：必填缺失「缺少必填参数：…」；`additionalProperties is False` 时的未知字段「不支持的参数：…」；`type == "string"` 的类型「参数 {k} 必须是字符串」；`enum` 白名单「参数 {k} 只能是 a / b」。

原实现把 `ping` / `notifications/initialized` / `tools/list` 合在一个 `if` 里，非 `tools/list` 一律回 202，带 id 的 `ping` 收到 202 后一直等到超时；缺 `method` 的请求既非请求也非通知，回 202 等于假装已处理；类型不符会穿到 `core` 抛 `TypeError` 被兜底成「服务端内部错误」；SQLite 同步阻塞，在事件循环里跑会卡住并发心跳。

**影响面**：`ping` 带 id 回 result、不带 id 回 202；参数校验前移到 MCP 层；`-32600` 覆盖范围扩大，客户端要处理两种新文案。**回归**：`TestMcpProtocol` 七项（第 18 节）。

---

## 8. `bigfish/gui.py`（115 insertions，17 deletions；相对 rc3 为 216/32）

| 位置 | 改动 |
| --- | --- |
| :61 / :470 | `MODEL_KEYS` / `MODEL_TITLES`；`_drafts` / `_loaded` / `_vote_labels` |
| :97 | `start()` 重入保护：`if self.thread is not None and self.thread.is_alive(): return` |
| :138 | `_resolve_endpoint()` 从 `servers[0].sockets[0].getsockname()` 取真实 host/port |
| :151 | 新增 `stop_watcher(timeout=5.0)` |
| :588 / :592 / :618 | `_draft_key()` + `_stash_draft()`；`load()` 分 history 与 editor |
| :677 | `save()` 空内容拒绝 |
| :1096 / :1103 / :1203 | `MainWindow.__init__(settings, council, db=None)`、`_closed`、`on_server` / `tick` / `refresh_all` 判 `_closed` |
| :1259 / :1296 | `_shutdown()` 幂等；`run(data_dir, port, host, db, council)` |

**ApiServer 启动**：`start()` 线程已活则 return；`run()` 捕 `BaseException`（端口占用抛 `SystemExit`，只捕 `Exception` 会漏）；新增 `watch()` 线程最多轮询 20 秒，等到 `getattr(self._server, "started", False)` 为真才 `self.started.emit(True, f"http://{self.running_host}:{self.running_port}")`；`port=0` 时设置里的 0 不是实际端口，故用 `_resolve_endpoint()`。`stop_watcher()` join `_watcher` 并跳过 `threading.current_thread()`。

**`_shutdown()`（:1259）顺序**：`_closed` 幂等 return → 置 `_closed = True` → `started.disconnect(self.on_server)` → `server.stop()` → `server.stop_watcher()` → `council.stop_watchdog()` → `self.db.close()` → `tray.hide()`。顺序不能反：先停 HTTP 与看门狗、最后关连接，否则会有线程拿着已关闭的连接去查询（`sqlite3.ProgrammingError`）。

    self._drafts: dict = {}                                                     # :470
    def _draft_key(self, tid: str, version: int, model: str) -> tuple:          # :588
        """草稿的归属键：任务 + 版本 + 模型。三者任一变化都不共用草稿。"""
        return (tid, version, model)
    content = ed.toPlainText().strip()                                          # :677
    if not content:
        QMessageBox.information(self, "内容为空", "请先输入要登记的审查内容"); return

`load()` 开头先 `_stash_draft()`（:592）；历史文本进只读 `history_{model}`（标题「已记录（只读）」），草稿编辑器回填 `self._drafts.get((tid, version, model), "")`。原写法用历史文本回填编辑器，切一次任务就冲掉未提交的草稿。徽标：`revoked` → 「已撤票」`#d64545`、`approve` → `#3f9a63`、`reject` → `#d64545`、有评论无票 → 「仅评论」`#c9a227`、否则「未投票」`#8b93a3`；结论行按 `MODEL_KEYS` 拼成 `当前审查结论：GPT xxx / Gemini xxx　→　状态`。`save()` 成功后 `self._drafts.pop((cur[0], cur[1], model), None)` 并 `ed.clear()`。空投票写进 `reviews` 会污染 `vote_summary`。

**影响面**：GUI 不再二次 `open_council`（`run(...)` 接收入口传入的 db/council）；`_shutdown` 幂等后重复触发不会二次 `db.close()`；草稿只在内存，关窗即失，未落盘。**回归**：`tools/gui_smoke.py` 9/9（**用户提供结果，本轮未复跑**）；`test_v011_rc4.py` 未覆盖 GUI 层。

---

## 9. `bigfish/__init__.py`（1 insertion，1 deletion）

    -__version__ = "0.1.1-rc3"
    +__version__ = "0.1.1-rc4"

`/health` 与自检回读的版本号来源，无副作用。
---

## 10. `tools/selfcheck.py`（18 insertions，3 deletions）

    PORT = int(os.environ.get("BFC_SELFCHECK_PORT", "3094"))                  # :22
    if health.get("service") != "bigfish-council":                          # :89
        print("  [FAIL] 端口 %d 上跑的不是本服务（service=%r）" % (PORT, health.get("service")))
        print("\n端口被别的程序占用，用 BFC_SELFCHECK_PORT 换一个空闲端口。")
        return 1
    check("服务在线", health.get("status") == "ok", health.get("service", ""))   # :94

**为什么**：默认端口 3099 已被第 1 节的 `PORT_RESERVED` 预留给手机端 REST 网关；注释（:21）记的是「端口冲突会让自检认错服务」这类真实故障，故取 3094。服务身份校验补同一类漏洞：端口被别人占着时 uvicorn bind 失败，`/health` 仍返回**别人**的服务，后面每条 check 都拿着别人的响应当通过。

**影响面**：自检默认端口变化；`BFC_SELFCHECK_PORT` 优先级不变；`setup.ps1` / `build.ps1` 的自检调用不带端口参数，自动跟随。**回归**：`tools/selfcheck.py` 16/16（**用户提供结果，本轮未复跑**）。

---

## 11. `tools/remote_check.py`（104 insertions，49 deletions）

    CANCEL_PRECONDITION_HINT = (                                                # :48
        "cancelled 只能在任务已进入「取消中」之后提交：请先在 GUI 对该任务点取消，"
        "再重跑本脚本 --task-id <id> --allow-submit")
    ap.add_argument("--task-id", default=None, help="显式指定联调任务；不给则纯只读")       # :102
    ap.add_argument("--allow-submit", action="store_true",                                  # :103
                    help="允许回传结果（status 固定 cancelled，不会写 completed）")
    {"ticket": ticket, "status": "cancelled", ...}                                          # :224
    check("回传状态是 cancelled（不是 completed）", ...)                                       # :231

| 位置 | 改动 |
| --- | --- |
| :97 | 新增 `parse_args(argv)`，`--task-id xxx` 与 `--task-id=xxx` 都支持 |
| :113 | 入口改用 `parse_args(sys.argv[1:])`，删掉手写解析 |
| :185 | 无 `--task-id` 只做只读检查并打印闭环用法 |
| :188 | 无 `--task-id` 时 `--allow-submit` 记 SKIP |
| :215 | 默认不提交，只有 `--allow-submit` 才提交，且只提交 `cancelled` |
| :242 | 提交失败附 `CANCEL_PRECONDITION_HINT` |

另：`rpc` 返回体非 `isError` 但带 `error` 字典时视为失败回 `{"ok": False, "error": msg}`；`BASE` / `TOKEN` 改为模块级可变全局，按「命令行 > 环境变量」赋值。

**为什么**：自动挑任务会污染真实任务；回传 `completed` 等于替执行器宣布业务完成，脚本没这个资格，`cancelled` 的语义是「联调中止」。

**影响面**：调用方式改为具名参数。**回归**：无自动化用例；本轮未做跨机联调。

---

## 12. `tools/net_check.py`（41 insertions，9 deletions）

    OK, FAIL, SKIP = [], [], []                                                # :40
    def record_skip(name: str, extra: str = "") -> None:                       # :50
        """本机没起服务这类「本次没测到」的情况：记 SKIP，不记 FAIL。"""
        SKIP.append(name)
    print("通过 %d 项，失败 %d 项，跳过 %d 项" % (len(OK), len(FAIL), len(SKIP)))   # :152
    print("跳过项（未测到，不等于通过）：" + "，".join(SKIP))                     # :154

五处改 SKIP：本机 API 未监听（:100，附启动命令与 `ss -ltnp` 核对提示）、跳板未监听（:118，提示 `node tools/bfc-proxy.mjs`）、`--skip-cross`（:134）、未给 `--target-host`（:136）、`/health` 通但 MCP 未覆盖（:145）。

**为什么**：原文把「本机没起服务」记成 FAIL，在 VPS 上单独跑本脚本会恒失败，噪声掩盖真故障。

**影响面**：退出码语义变化 —— 只有 FAIL 非空才失败，依赖「全绿」判断的调用方需看 SKIP 计数。**回归**：无自动化用例；本轮未在 VPS 上执行。

---

## 13. `tools/dev_setup.sh`（10 insertions，8 deletions）

    -set -u
    +set -uo pipefail                                                          # :10
    LOG="${{TMPDIR:-/tmp}/bfc-dev-setup-test.log"                              # :53
    RC=0                                                                       # :56
    QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -t . >"${{LOG}" 2>&1 || RC=$?   # :57
    tail -6 "${{LOG}"                                                          # :58
    if [ "${{RC}" -ne 0 ]; then echo "== 测试失败（退出码 ${{RC}），完整日志见 ${{LOG}" >&2   # :60

原写法是 `>"\$LOG"` 与 `RC=\$?`（反斜杠把变量转义成字面量），日志落到名为 `$LOG` 的文件、`$?` 取不到真实退出码，测试失败时脚本仍返回 0。注释（:54-55）把这条写在脚本里防复发。`pip install -U pip` 也加了失败即 `exit 1`。

**影响面**：`--test` 透传失败退出码；`pipefail` 让管道中途失败也能被发现。**回归**：本轮未执行 `bash tools/dev_setup.sh --test`（会重装依赖），未验证。

---

## 14. `setup.ps1`（40 insertions，4 deletions）

    function Assert-Ok {                                                       # :12
        param($What)
        if ($null -eq $LASTEXITCODE) { return }
        if ($LASTEXITCODE -ne 0) {
            $ErrorActionPreference = 'Continue'
            Write-Error "$What 失败（退出码 $LASTEXITCODE）"
            exit 1
        }
    }

六个调用点：检测 Python 版本（:31）、创建虚拟环境（:42）、升级 pip（:49）、安装 requirements.txt（:53）、单元测试（:57）、自检 `tools/selfcheck.py`（:61）。另有 `Get-Command python` 存在性检查、venv 创建后 `$py` 复检，末尾 `exit 0`。

**为什么**：PowerShell 默认不因外部程序非 0 退出码中断，原来装依赖失败也会一路走到「安装完成」。

**影响面**：从「尽力而为」变为「首错即停」。**未在 Windows 上复跑，未验证。**

---

## 15. `build.ps1`（60 insertions，18 deletions）

同样的 `Assert-Ok`（:12）。调用点：创建虚拟环境（:31）、升级 pip（:38）、安装 requirements.txt（:40）、安装 pyinstaller（:42）、单元测试（:46，注释写明「不通过就不打包」）、PyInstaller 打包（:77）。

PyInstaller 调用从反引号续行改为参数数组 `$piArgs = @(...)` + `& $py @piArgs`；参数内容不变（`--noconfirm --clean --name BigFishCouncil --windowed --onedir --paths $root` + 若干 `--hidden-import` + `--collect-submodules bigfish` + 两个 `--exclude-module`）。打包后检查 `dist\BigFishCouncil\BigFishCouncil.exe` 是否存在，缺失时打印「PyInstaller 返回 0 但没生成 $exe，请查 spec/日志」并 `exit 1`。

**为什么**：反引号续行在参数带空格或变量展开时容易错位；退出码 0 但没产物是另一类假成功。

**影响面**：构建失败立即中断，产物体积与内容未变。**未在 Windows 上重打包，未验证。**

---

## 16. `tools/start-headless.ps1`（188 insertions，31 deletions）

| 位置 | 改动 |
| --- | --- |
| 参数区 | 新增 `-Python`、`-HealthTries`（默认 10）、`-HealthIntervalSec`（默认 1）、`-UseScheduledTask` |
| :43 | 新增 `Resolve-Python`：`.venv\Scripts\pythonw.exe` → `python.exe` |
| :63 / :74 | 新增 `Test-Health` / `Wait-Health` / `Show-LogTail` / `New-InnerCommand` |
| :153 | 主路径 WMI：`Invoke-CimMethod -ClassName Win32_Process -MethodName Create` |
| :202 | 失败时打印日志尾部 + 排查建议并 `exit 1` |

    $ErrorActionPreference = 'Continue'
    $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create \
        -Arguments @{ CommandLine = $cmdLine } -ErrorAction Stop      # :153

幂等前置：启动前先 `Test-Health`，已通则打印「端口 N 上已有 BigFish Council 在跑，跳过启动。」并 `exit 0`。WMI 的 `ReturnValue -eq 0` 视为成功（打印 pid），失败回退 `schtasks`；备用路径把命令写进 `tools\.bfc-headless-<TaskName>.cmd` 包装脚本，`/TR` 只指向该文件，`/Create` 与 `/Run` 各自检查 `$LASTEXITCODE`。结尾 `Wait-Health` 成功打印 `[成功] <mode>，端口 N，第 K 次探测通过`。

**为什么**：两条坑写在头部注释 —— `schtasks /TR` 嵌套引号导致 `-2147467259 (E_FAIL)`；子进程不重定向 stdout 会把 windows-mcp 的管道卡死。

**影响面**：从「拉起来就算成功」变为「/health 探通才算成功」；多出 `tools\.bfc-headless-*.cmd` 临时文件。**未在 Windows 上复跑，未验证。**

---

## 17. `tools/stop-headless.ps1`（81 insertions，20 deletions）

    $BfcPatterns = @(
        '(^|\s)-m\s+bigfish(\s|$)',
        'bigfish[\\/]__main__\.py',
        'BigFishCouncil\.exe',
        'bfc-proxy\.mjs',
        'bigfish-council[\\/]'
    )

新增 `Get-CmdLine(ProcId)`（`Get-CimInstance Win32_Process`）与 `Test-BfcProcess(Name, CmdLine)`：可执行名须是 `python*` / `BigFishCouncil*` /（`node*` 且命令行含 `bfc-proxy.mjs`），**且**命中任一特征模式。

| 位置 | 改动 |
| --- | --- |
| :21 | 参数删 `-Force`、增 `-WhatIfOnly`（只报告不杀） |
| :62 | 计划任务段在 `-WhatIfOnly` 下跳过 |
| :109-110 | 结尾按 `-WhatIfOnly` 分别打印并 `exit 0` |

端口段 `Get-NetTCPConnection` 用 try/catch 包并跳过 pid 0；`Stop-Process` 后按 `$?` 判断，失败进 skipped「停止失败 …（权限不足？）」；`$ErrorActionPreference = "Continue"`。

**为什么**：裸 `"bigfish"` 匹配会误伤 `my-bigfish-notes.txt` 这类进程。

**影响面**：停止范围收窄；`-Force` 被移除，旧命令行需同步改参数。**未在 Windows 上复跑，未验证。**

---

## 18. `tests/test_v011_rc4.py`（新增，473 insertions，0 deletions；472 行 / 22367 字节 / 33 个 test）

`Base`（:37）用 `tempfile` 建临时库，`Council(heartbeat_ttl=60, ticket_ttl=120, ticket_max_ttl=3600, heartbeat_lease=180)`；`tearDown` 调 `stop_watchdog()` + `db.close()`；`approved()` 辅助函数走 `create → submit_for_review → 两票 approve → approve`。分组以 unittest loader 实测为准：

| 组 | 类 | 行 | 项数 | 覆盖 |
| --- | --- | --- | --- | --- |
| 幂等契约 | `TestIdempotencyContract` | :75 | 6 | 冒号键重放同票且不重复发票、同键同体同响应、同键异体拒绝、重放不重复写事件、并发同键心跳只跑一次 |
| MCP 协议 | `TestMcpProtocol` | :150 | 7 | ping 带 id 回 result / 不带 id 回 202、缺 method、错版本、参数类型、enum、未知参数 |
| 多轮票据 | `TestMultiRoundTickets` | :208 | 6 | BLOCKED 后重领发新票、旧票被吊销、无幂等键用随机 nonce、同 nonce 派生确定、通过只收 SUBMITTED、不能通过 BLOCKED |
| 审查绑定 | `TestReviewBinding` | :296 | 4 | 撤销最新赞成票不回落、撤销后审批不再有效、提交前投票直达待审、派发后撤票打回 |
| 执行器客户端 | `TestExecutorClient` | :351 | 4 | 无 task_id 必抛、BLOCKED 不算成功、SUBMITTED 算成功、safe_claim 复用同键 |
| 辅助面 | `TestAuxiliary` | :419 | 6 | `list_tasks` 集合筛选 / offset 稳定翻页 / 顺序确定、`get_task_status_row` 轻量、预留端口不冲突、`DB_ROLES` 白名单 |

文件 docstring 与 loader 实测一致：A6 / B7 / C6 / D4 / E4 / F6 = 33。（早先 docstring 曾写成 B6 / C5，2026-11-19 已按 loader 实测修正。）

**回归**：`LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest tests.test_v011_rc4` → `Ran 33 tests in 0.680s / OK`（本轮实测）。

---

## 19. `docs/ARCHITECTURE-FUTURE.md`（新增，889 insertions，0 deletions；889 行 / 56040 字节）

为 exe / apk 与外部客户端预留接口与端口的架构预研文档，8 章 + 附录 A：

| 章 | 行 | 内容 |
| --- | --- | --- |
| 1 | :10 | 现有架构盘点 |
| 2 | :197 | 端口与进程拓扑预留表（引用 `config.PORT_RESERVED`） |
| 3 | :275 | 三层客户端抽象 |
| 4 | :374 | 为 apk 预留的接口契约（草案） |
| 5 | :616 | 打包边界与体积 |
| 6 | :721 | 版本与兼容策略 |
| 7 | :786 | 迁移步骤（rc4 → exe/apk） |
| 8 | :845 | 明确不做什么 |
| 附录 A | :861 | 文件索引与实现状态对账 |

**为什么**：rc4 的端口预留（第 1 节）与 `DB_ROLES` 白名单（第 2 节）都是为后续客户端形态做的铺垫，没有落纸的边界会在实现时被逐个绕过；第 8 章「明确不做什么」是这份文档的主要约束力来源。

**影响面**：纯文档，无运行时依赖；第 2 节与 `config.py` 的 `PORT_RESERVED` 必须同步维护。**回归**：不涉及。

---

## 20. 回归实测

| 项 | 命令 | 结果 | 来源 |
| --- | --- | --- | --- |
| 全量单元测试 | `LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -t .` | `Ran 162 tests` / `OK` / exit 0 | 本轮实测 |
| rc4 单文件 | `... -m unittest tests.test_v011_rc4` | `Ran 33 tests in 0.680s` / `OK` / exit 0 | 本轮实测 |
| 静态检查 | `pyflakes bigfish tools tests` | 无输出 / exit 0 | 本轮实测 |
| 自检 | `tools/selfcheck.py` | 16/16 | **用户提供结果，本轮未复跑** |
| GUI 冒烟 | `tools/gui_smoke.py` | 9/9 | **用户提供结果，本轮未复跑** |

162 项的分组构成：`test_core` 15 + `test_api` 6 + `test_v011_p0` 15 + `test_v011_p1` 24 + `test_v011_p2` 24 + `test_v011_p3` 19 + `test_v011_rc3` 26 + `test_v011_rc4` 33 = 162。

---

## 21. 未验证项

| 项 | 状态 | 原因 |
| --- | --- | --- |
| `setup.ps1` 六处 `Assert-Ok` | 未验证 | 本轮未在 Windows 上执行 |
| `build.ps1` 打包与产物检查 | 未验证 | 本轮未在 Windows 上重打包 |
| `start-headless.ps1` WMI 主路径 | 未验证 | 需 Windows 主机 |
| `start-headless.ps1` schtasks 回退路径 | 未验证 | 需 Windows 主机 |
| `stop-headless.ps1` 进程识别收紧 | 未验证 | 需 Windows 主机 |
| `dev_setup.sh --test` 退出码透传 | 未验证 | 执行会重装依赖，本轮未跑 |
| `remote_check.py` 跨机闭环 | 未验证 | 需远端执行器；本轮未做联调 |
| `net_check.py` 在 VPS 上的 SKIP 分支 | 未验证 | 本轮未在 VPS 上执行 |
| `selfcheck.py` 16/16 | 未复跑 | 用户提供结果 |
| `gui_smoke.py` 9/9 | 未复跑 | 用户提供结果 |
| `core.py` 第 4 / 9 / 10 / 11 条 | 无专用用例 | 仅靠全量回归兜底，本轮未构造针对性场景 |
| `db.py` / `backup.py` 的 rc4 增量 | 无 rc4 用例 | 由 `tests/test_v011_rc3.py` 覆盖 |
| 真机 apk / exe 形态 | 未实现 | `docs/ARCHITECTURE-FUTURE.md` 第 4、7 章为草案 |
