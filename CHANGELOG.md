# CHANGELOG

本文件只记「对使用者有意义」的变更。每条都能在 `docs/` 里找到证据。

---

## v0.1.1-rc5 — 2026-11-20　第三轮审计 R1–R9 收口

**主题：把「恢复之后世界还是不是原来那个世界」钉死——授权、事件、时间戳、收据、导出、跳板、排空、密钥自愈八条路径上，都不许再有「看起来成功、其实没生效」。**

### 恢复后授权与排空（R1 / R8）

- **恢复等于让整库回到过去，而过去是有人拿着授权的。** 改前 `restore_bundle()` 只轮换密钥 + `epoch+1`：
  快照里的 `APPROVED` 任务原样回来（随时可被领取）、`executors.enabled` 仍是 1
  （旧 token 哈希随快照一起回来了，恢复前发出去的凭据继续能登）、`_replay_claim()` 只按
  `cached["version"]` 重算票，既不查取消也不查吊销——客户端拿旧幂等键重试就能再拿一张能用的票。
  改后 `backup._block_inflight()` 五步一次做全：在途任务转 `BLOCKED`、`APPROVED` 冻结成
  `BLOCKED`（`blocked_reason` 写明「派工许可已冻结」）、所有活票置 `revoked_at`、
  **全部执行器 `enabled=0` 并写 `disabled_reason`**、清 `claim/heartbeat/submit` 幂等记录；
  全程只翻状态位，一行不删（审计要用）。
- **重新授权必须换凭据。** 改前没有「解禁」这条路，恢复后只能 `executor-add` 重开一个身份。
  改后新增 `core.reenable_executor()`（CLI `reenable <exec-id>`）：`enabled=1` 的同时
  `token_hash` 换成新 token 的哈希、`disabled_reason` 清空、写 `executor.reenabled` 审计。
  旧 token 在库里已没有对应哈希，物理上无法再用——只翻 `enabled=1` 等于把泄露过的凭据一起复活。
- **恢复期的排空从「尽力而为」变成「有闸门」。** 改前 `restore_bundle()` 直接关连接换文件：
  本进程的看门狗可能正好在扫、HTTP 线程可能正好在写，「恢复失败但执行器以为成功」不是理论问题。
  改后 `Database.enter_maintenance()` 先抢写锁做屏障（`tx()` 整个事务期间持锁，抢到即代表在途写已排空，
  不是靠 sleep 猜），之后所有普通读写被 `_guard()` 直接抛 `MaintenanceError`；
  `server.py` 把它译成 **503 + `Retry-After: 15` + `maintenance: true`**，MCP 侧回 `-32000`
  而不是业务错误，`/health` 报 `status="maintenance"`。恢复自身用 `db._allow_during_maintenance()` 直通，
  `db.maintenance()` 保证无论成败都退出。
- **跨进程那半边也堵了。** 维护模式只管得住本进程的连接：另一个 headless/GUI 仍在写时，
  POSIX 上会继续写旧 inode（数据静默分叉），Windows 上 `os.replace` 直接失败。
  改后 CLI `restore` 先 `_port_busy(host, port)` 探回环端口，有人在听就打印处置办法并 `return 2`；
  GUI 侧走 `pause_services()`（先 `disconnect` 再 `stop` API 线程与看门狗）→ 恢复 → `resume_services()`，
  失败也接回服务。

### 审查与审计（R2 / R3 / R6）

- **双模型通过的两条事件从来没落过库。** 改前 `_recompute_review()` 在 `all_approved` 分支里
  `UPDATE` 成功后就 `return`，跳过了函数末尾的 `for kind, detail in events: self.log_event(...)`——
  只看事件表会得出「这任务从没通过审查」的结论。改后删掉那个 return，
  `task.review_passed` 与 `task.pending_approval` 正常写入。
- **撤一条普通评论能把运行中的任务打挂。** 改前 `revoke_review()` 不判 `kind`：把 `comment` 撤掉
  也会走 `DISPATCHED/RUNNING/CANCELLING → BLOCKED` 那条支路并吊销执行票——评论本来就不计票，
  撤它没有任何业务含义。改后先取 `kind`，`kind != "vote"` 时把 `task_row` 置空跳过状态评估，
  只把那段文字下架；`kind=vote` 的撤票路径一字未动，两条路径并排断言。
- **审计包的黑名单改成白名单。** 改前 `AUDIT_DENY_COLUMNS = ("token_hash", "ticket", ...)`：
  以后给 `tasks` 加一列 `executor_token`，黑名单不会自动覆盖，导出即泄露。
  改后 `backup.AUDIT_COLUMNS` 逐表列白名单（`tasks` / `task_versions` / `reviews` / `approvals` /
  `events` / `audit` 各自明列允许字段），不在名单上的列被**跳过**并记进
  `manifest.redaction.dropped_columns`，`manifest.redaction.policy` 直接写 `"allowlist"`。
  宁可少导数据（安全方向的失败），也不多导一列。`AUDIT_DENY_COLUMNS` 保留为兼容引用的常量。

### 执行可靠性（R4 / R5 / R9）

- **租约时间取在事务外，能算出「过去」的到期时间。** 改前 `heartbeat()` / `submit_result()` 在
  `tx()` 之前就取 `t = now()`，等事务真拿到写锁时已经过去一段，续租后的 `expires_at` 可能只比
  当前时刻晚一点点、甚至已经过期——密集心跳下会自伤成超时。改后 `now()` 移进 `tx()` 内部，
  租约基准点等于真正写入的那一刻。
- **执行端崩了没人知道做到哪。** 改前 `ExecutorClient` 拿到票就干，进程被杀后「到底发出去没有」
  只能靠服务端 `task_status` 反推，网络不确定时反复重试还可能重复执行。改后：
  - `executor_client.ReceiptLog` 按稳定 `dispatch_id` 本地追加落「开工 / 完成」收据，
    每行立即 flush，坏行跳过不致命，`None` 路径就是纯内存；
  - `state_of(dispatch_id)` 给三种口径：只有 claimed = 未开工、started 没 finished = **unknown**
    （最危险的那种）、finished = done；
  - `reconcile()` 把本机「已开工未完成」的 `dispatch_id` 列出来交人去服务端对账；
  - 服务端 `claim()` / `_replay_claim()` 现在都回 `dispatch_id`（取 `ticket_id`，
    重新派工会换新票所以天然换号）；旧服务端没这个字段时客户端降级用 `ticket_id`，不报错。
- **密钥自愈换了密钥却不吊销票。** 改前 `ensure_key_consistency()` 发现「库内指纹 ≠ 磁盘密钥」
  会 `rotate_key()`，但函数体里根本没碰 `tickets` 表——密钥换了、`token_hash` 还是旧值，
  旧票照样能通过认证（而文案写着「恢复前的执行票全部失效」）。改后 `_heal_key()` 在轮换分支里
  一次性吊销全部未撤销票、清 `claim/heartbeat/submit` 幂等、写 `db.key_healed` 审计并回填
  `tickets_revoked` / `idempotency_purged`；**仅补记指纹**（老库首次升到 rc5、密钥没换）时
  `rotate=False`，不动任何票——否则等于无故中断在途执行。

### 工具与协议（R7 + CLI）

- **跳板原来能绑到 0.0.0.0。** 改前 `tools/bfc-proxy.mjs` 直接 `server.listen(PORT, LISTEN_HOST)`，
  一个没有鉴权的 MCP 转发全靠环境变量没被填错。改后新增 `isLoopback()`（剥 `[ ]`、逐段解析八位组、
  只比数值），判定放在 `listen` **之前**，不合法就用 `fs.writeSync(2)` 写原因后 `process.exit(1)`——
  用同步写是因为管道下 `console.error` 是异步的，`exit` 会把最后一行吞掉，运维只看到空 stderr。
  `0.0.0.0` / `::` / `[::]` / 空串 / 内网段 / 公网地址全部拒绝，`127.0.0.1` / `localhost` / `::1` / `127.0.0.0/8` 放行。
- **CLI 多了两条人要走的路**：`reenable <exec-id>`（恢复后重新授权，同时换凭据，新 token 只显示一次）、
  `restore --force`（端口检查的人工逃生门，默认关闭）。

### 数据库

- schema 3 → **4**、BACKUP_FORMAT_VERSION 3 → **4**。`MIGRATIONS[4]` 只有两项：
  `ALTER TABLE executors ADD COLUMN disabled_reason TEXT` 与
  `CREATE INDEX IF NOT EXISTS idx_executors_enabled ON executors(enabled)`。
  不重建表、不删列、不改既有值；**不可降级**（库版本高于程序版本直接抛 `DatabaseVersionError`）。

### 测试

- 新增 7 个文件共 **80 项**：`test_v011_rc5_restore_authority.py` 21、
  `test_v011_rc5_receipts.py` 24、`test_v011_rc5_review_audit.py` 13、
  `test_v011_rc5_proxy.py` 9、`test_v011_rc5_key_heal.py` 7、
  `test_v011_rc5_cli_guard.py` 3、`test_v011_rc5_dispatch_id.py` 3；
  另有 2 项加在既有文件里（`test_v011_p0.py` 15 → 16、`test_v011_p3.py` 19 → 20）。
- 总数 162 → **244**。平台 A（Debian 12 / Python 3.11.2）全绿；**平台 B（Windows 11）本批次未跑**，
  上一次 Windows 全量仍是 rc3 的 129 项。
- `pyflakes` 零输出、`compileall` RC=0、`selfcheck` 16/16、`gui_smoke` 9/9（均在平台 A）。

### 未完成（不当作已完成）

- Windows 未跑 rc5 的 244 项（rc4 的 162 项也没补跑）。
- GUI 真窗口未验收；开机自启未注册；DSH MCP 连接未导入；跨机闭环未重跑（证据仍是 rc2 时期的）。
- `restore --force` 是人工逃生门，绕过端口检查后仍可能出现「另一个进程在写旧 inode」，属已知取舍。

---

## v0.1.1-rc4 — 2026-11-19　第二轮审计收尾

**主题：把「幂等」「多轮执行」「恢复」「协议」四件事在边界情况下钉死，并给未来的 exe / apk 预留位置。**

### 幂等与协议（E）

- `_idem_get_locked()` 改用 `key.split(":", 3)`。此前全量 split 后取 `parts[3]`，
  客户端自定义的含冒号幂等键会被**截断**，重放时派生出另一张票。
- `/mcp` 强制 `jsonrpc == "2.0"`、缺 method 回 `-32600`、只有**无 id** 才算通知。
  此前缺 method 的畸形请求会被当成通知回 202，客户端一直等。
- `ping` 带 id 必须回 `result`。此前回 202，MCP 客户端会等超时。
- `tools/call` 的参数先按 inputSchema 校验（必填 / 未知字段 / 类型 / 枚举），
  错误走 `result.isError`；业务调用改 `run_in_threadpool`，不再用同步 SQLite 卡事件循环。
- `ExecutorClient`：`RECONCILED_OK` **不含 BLOCKED**（心跳超时也会打成 BLOCKED，
  拿它当提交成功是误判）；`safe_submit(ticket, task_id=...)` 的 task_id 改成显式形参
  （旧写法 `kwargs.get("task_id")` 恒为 None）；新增 `safe_claim` / `safe_heartbeat` 复用同一幂等键重试。

### 多轮执行（D）

- `claim()` 无幂等键时引入**随机 nonce** 派生票。此前同一任务「BLOCKED → 重新批准 → 再领取」
  会派生**完全相同的票**，旧票持有者能继续用。
- 领取时先吊销同任务所有旧活票再 INSERT（不用 `INSERT OR REPLACE`，避免静默丢吊销证据）。
- `sweep_timeouts()` 改为按**当前活票**查 run 心跳。此前按 task_id 取最新 run，
  多轮执行后会读到历史心跳，把真失联误判成还活着。
- `update_prompt()` 清 `cancel_requested_at` / `blocked_reason` / `executor_id` 与
  control 里的 cancel/pause 标记，否则「取消后改一版 Prompt 再审批」永远领不到。
- `verify(accept=True)` 只接受 SUBMITTED。BLOCKED 是「副作用不明」，不能点一下通过变成 DONE。

### 审查绑定（C）

- `latest_approval()` 改用**正向白名单** `EFFECTIVE_APPROVAL_STATES`。黑名单漏排
  PENDING_APPROVAL / REVIEW_PASSED / BLOCKED，撤票退回议事后的历史批准仍显示有效。
- 投票汇总**每个模型只取最新一张票（含已撤销）**。撤掉最新赞成票 = 立场撤退，
  不能回落到更早那张赞成票凑「双通过」。

### 恢复原子性与运行所有权（A / B）

- `Database.__init__` 先只读探测 schema 版本再改文件：未来版本直接抛
  `DatabaseVersionError`，不再先把库改成 WAL 再报错。
- `init_schema()` 改为**先迁移后建表**。此前 v1 库会在 `CREATE INDEX ... idempotency(expires_at)`
  处炸 `no such column`。
- 恢复时密钥文件与库文件**成对替换、成对回滚**；两次 `os.replace` 之间被杀的半完成状态
  由 `runtime.ensure_key_consistency()` 在下次启动自愈。
- `restore_bundle()` 的 `idempotency_purged` 回填真实计数（此前写死 0）。
- `ApiServer` 加 `start()` 重入保护、捕 `BaseException`（uvicorn 端口占用抛 SystemExit）、
  用 `getsockname()` 报真实端点；`Council.stop_watchdog()` 只停自己那一个
  （此前无条件 `stop_for` 会把外部 GUI/CLI 的看门狗一起停掉）。
- GUI：`on_server` / `tick` / `refresh_all` 加 `_closed` 保护并先 `disconnect` 再 `stop`，
  修掉关窗时 `sqlite3.ProgrammingError: Cannot operate on a closed database`。

### GUI 体验

- 议事页每个模型拆成「已记录（只读）」与「本轮输入（草稿，未提交）」两块；
  草稿按 (任务, 版本, 模型) 存档，切任务/切版本不互相污染，空内容不再静默写成空评论。
- 投票徽标改读后端 `votes[model].revoked`，显示「已撤票」而不是继续显示 approve。

### 为 exe / apk 预留（只预留，未实现）

- `config.PORT_RESERVED` 平行字典登记 3099 / 3100 / 3101 / 8765 / 18765 / 18766；
  **不并入 `PORT_ROLES`**（否则等于对测试撒谎）。
- `roles.DB_ROLES` 白名单 + 注释：未知 db_role 会兜底成 EXECUTOR_DSH，移动端必须显式登记。
- 新增 `docs/ARCHITECTURE-FUTURE.md`（889 行）：端口拓扑预留表、三层 `ClientTransport` 抽象、
  apk 的 REST+SSE 八端点契约草案、onedir vs onefile、漫游三方案、`/api/v1` 版本策略、
  五阶段迁移、九条明确排除项。
- `tools/selfcheck.py` 默认端口 3099 → **3094**（3099 留给移动端网关，3095 被本容器的
  quota-hub 占用），并新增「对面是不是本服务」的校验，避免端口冲突时拿着别人的 /health 瞎判。

### 辅助脚本

- `setup.ps1` / `build.ps1` 每条原生命令后跟 `Assert-Ok` 断言 `$LASTEXITCODE`。
  此前 pip 装失败、PyInstaller 打包失败都能一路走到「完成」，是假成功。
- `tools/dev_setup.sh` 修反斜杠转义导致日志落到字面量文件、退出码取不到的真 bug。
- `tools/remote_check.py` 重写：未指定 `--task-id` **绝不自动挑任务**；
  `--allow-submit` 才提交且只提交 `cancelled`（绝不用 completed 污染看板）。
- `tools/start-headless.ps1` 改 WMI `Win32_Process.Create` 派生 + 强制重定向日志
  （不做重定向会让 windows-mcp 单队列被管道堵死）。
- `tools/stop-headless.ps1` 进程识别收紧到「python* 且命令行含 `-m bigfish`」，
  不再无差别杀占用 3096 的进程。

### 测试

- 新增 `tests/test_v011_rc4.py` **33 项**（幂等契约 6 / MCP 协议 7 / 多轮执行票 6 /
  审查绑定 4 / 执行器客户端 4 / 辅助 6），并发一律真线程 + Barrier。
- 总数 129 → **162**。平台 A 全绿；平台 B 本轮未跑。

---

## v0.1.1-rc3 — 2026-11-19　安全加固

**主题：让「取消」「恢复」「并发」「监听」四件事在出事时不出错。**

### 状态机（业务安全）

- 新增 `CANCELLING` 状态。执行中（DISPATCHED/RUNNING）取消不再直接标 CANCELLED，
  而是进 CANCELLING 等执行器确认；失联超时转 BLOCKED 等人工。
- `claim()` 的 UPDATE 增加 `cancel_requested_at IS NULL`，取消过的任务不可能被领取。
- `submit_for_review()` 补齐 `_recompute_review()`，DRAFT 期已双票的任务送审直接到 PENDING_APPROVAL。
- 撤票（`revoke_review`）后重新评估审批：未派工 → IN_REVIEW，已派工 → BLOCKED + 吊销票。
  审批记录不删（历史证据），但 `latest_approval()` 新增 `effective` 派生字段，失效审批不能再授权。
- GUI 改 Prompt 带上 `expected_version`，版本冲突明确报错，不静默覆盖。

### 数据库与备份

- schema 2 → **3**：新增 `executors.model`、`tickets.epoch`、`tasks.cancel_requested_at` + 2 个索引。
- `restore_bundle()` 重写为 13 步原子流程：校验 → 同盘临时库 → 迁移 → 阻断在途 → 原库留档
  → `os.replace` → 轮换密钥 → epoch+1 → 重开连接。失败必回滚，且**不会**覆盖还活着的原库。
- 恢复时轮换执行票派生密钥 + `epoch+1` + 清除 claim/heartbeat/submit 幂等记录，
  恢复前签发的票一律失效（此前只置 revoked_at，票仍可被确定性派生复用）。
- 恢复时把 DISPATCHED/RUNNING/CANCELLING 的任务转 BLOCKED，不自动恢复执行。
- 导出分两类：`KIND_OPERATIONAL`（可恢复，含全库）与 `KIND_AUDIT`（脱敏，禁止恢复）。
  审计包只含 6 张表，丢弃 executors/tickets/idempotency/runs/artifacts/meta，
  并剔除 token_hash / ticket / response / request_hash / evidence 列。
- 禁止 schema 降级：库版本高于程序版本直接拒绝打开。

### 并发与幂等

- `claim` / `heartbeat` / `submit_result` 的幂等检查全部挪进业务事务内（原先在事务外读，
  并发下会重复执行）。同键换体明确拒绝。
- `Database.tx()` 的 `BEGIN IMMEDIATE` 失败时正确释放 RLock（此前会让线程永久持锁）。
- Watchdog 的超时判断与状态更新在同一事务 + 条件 UPDATE，rowcount 不符就跳过，不再误杀。
- `ExecutorClient` 全面支持幂等键；网络不确定时先查 `task_status` 再决定是否重发。

### 网络与鉴权

- 监听地址校验下沉到 `server.serve()` 与 `gui.run()`，所有入口（模块 / CLI / GUI / ApiServer）
  共用同一处校验。此前 `cli serve --host 0.0.0.0` 可绕过。
- `/api/v1/tasks/{id}/status`：未分配的任务也返回 403（此前 `executor_id is None` 时放行）。
- 执行器吊销在每个关键业务事务**内**重新校验 enabled / 票状态 / 任务归属。
- 审查身份绑定到 `executors.model` 列，不再靠 name 字符串猜。

### 凭据

- 统一脱敏闸门覆盖 `log_event` / `submit_result` / `heartbeat.detail` / Artifact，
  识别 `bfc_tk_` / `bfc_ex_` / `bfc_rv_` / `bfc_ad_` / `sk-` / `Bearer` / PEM 私钥。
  先脱敏再落库，没有「先写后擦」。
- 修 `verify_file_restricted()` 的真缺陷：原先只检查「没有宽泛主体」，
  即使文件根本没授权给当前用户也算通过。现在要求既无宽泛主体、又必须含当前用户。
- Windows 密钥文件用 `icacls /inheritance:r /grant:r USER:F`（不是 chmod）。

### 工具与 GUI

- `tools/remote_check.py`：**默认只读**，必须显式 `--task-id` 才做 claim/heartbeat/submit，
  不再自动挑选第一条任务。支持 `--task-id value` 与 `--task-id=value` 两种写法。
- `tools/dev_setup.sh`：修 `| tail` 吞掉 unittest 退出码的问题。
- `tools/stop-headless.ps1`：只杀命令行确属 BigFish Council 的 python 进程，不无差别清端口。
- GUI 备份恢复改用统一的 `restore_from_bundle()`，展示 `restore_epoch` / `prerestore_path` /
  `key_rotated` / `blocked_inflight`，成功失败都有明确提示。

### 测试

- 103 → **129** 项。新增 `tests/test_v011_rc3.py` 26 项：状态机 7 / 并发 7 / 恢复 7 / 安全边界 5。
- 并发测试全部用 `threading.Barrier` 真实同步，不用 sleep 编排。
- 改写了 1 项既有测试（`test_revoked_ticket_rejected` → `test_cancel_while_dispatched_needs_ack`），
  因为业务语义被有意改掉；新断言比旧的更严，其余 102 项一字未动。
- 清理 14 处未使用导入，`pyflakes` 零告警。

### 未完成（不当作已完成）

- **Windows 平台 129 项已跑通**（25.263s / 退出码 0，独立目录 `D:\BigFishCouncil\bigfish-council-rc3`），
  此前拦路的 windows-mcp `USER_CONTROL` 在轮末解除；公司机 3096 上现役服务仍是 rc2，未替换。
- Windows ACL 已实测（`icacls` 分支，`verify_file_restricted` → `(True, "acl limited")`）。
- 仍未做：GUI 真窗口验收、开机自启、MCP 连接导入、`stop-headless.ps1` 真机实跑。
- 未引入类型检查（mypy/pyright），未做 24h soak。

---

## v0.1.1-rc2 — 2026-11-19　Windows 专属修复 + 跨机打通

- 修 `tests/test_v011_p3.py:164`：Windows 上 `subprocess.terminate()` 走 TerminateProcess，
  退出码是 1 而非 -15，断言按平台分支。
- 修端口释放判据：Windows 上进程退出后首次 `connect` 仍可能成功，改为「能否重新 bind 同一端口」。
- 新增 `tools/start-headless.ps1` / `tools/stop-headless.ps1`（计划任务路径，真机未跑通）。
- 跨设备联调打通：VPS → 公司机 CLIENT-B `CLIENT-B-TAILNET-IP:3096`，
  `tools/remote_check.py` 11/11 全过；新增 `wmcp-proxy-http.mjs` 逐请求改写 Host 头。
- 记录 windows-mcp 单队列被 `Start-Process` 堵死的根因与规避办法（改用 WMI `Win32_Process`）。

---

## v0.1.1-rc1 — 2026-11-18　第一轮安全修复

- P0-1：备份含 WAL —— 改用 `sqlite3.Connection.backup()` 一致性快照（此前 WAL 里的数据不在包里）。
- P0-2：审批版本漂移 —— `approve()` 全程 `BEGIN IMMEDIATE`，UPDATE 带版本条件。
- P0-3：端口职责单一真源（`config.PORT_ROLES`），拒绝非回环监听。
- P0-4：幂等表不再存明文执行票与明文 Prompt。
- P1-1：Watchdog 独立线程，不依赖 GUI。
- P1-2：执行票心跳续租，绝对上限 6 小时。
- P1-3：心跳幂等。
- P1-4：MCP `isError` 语义修正（业务失败返回 true）。
- P1-5：后台进程不要用 `Start-Process`（管道不 EOF 会堵死 MCP 队列）。
- P2-1/2：审查身份服务端绑定、四角色权限分离。
- P2-3：文档澄清「DSH 不是系统沙箱」。
- 测试 21 → 103 项。

---

## v0.1.0 — 2026-11-18　首个可运行版本

- Windows 本地控制中心（PySide6 五页 GUI）+ 本地 HTTP/MCP 服务（仅回环）。
- 任务状态机、双模型审查、人类审批、执行票、心跳、回传、验收。
- 跨机只能经 Tailscale，不开公网端口、不用 Funnel。
- 21 项单元测试。
