# BigFish Council 安全复审 — v0.1.1-rc6

| 项 | 值 |
| --- | --- |
| 版本 | `0.1.1-rc6`（`bigfish/__init__.py:6`） |
| 分支 / HEAD | `fix/v0.1.1-rc4-hardening` / `ae2fe82` |
| 对比基线 | rc5 发布提交 `c6d663d`；纯 rc6 口径 `2f43346..HEAD` |
| 差异规模 | 相对 `c6d663d` 的代码侧 19 文件 +826/-142；其中真属 rc6 的 13 文件 +817/-133 |
| 库结构 | `SCHEMA_VERSION = 4`（`bigfish/db.py:24`）、`BACKUP_FORMAT_VERSION = 4`（`bigfish/db.py:25`）——**与 rc5 相同，本批未动** |
| 全量测试 | `Ran 265 tests in 41.728s` / OK（本轮复跑 49.513s，同为 OK，耗时随负载浮动） |
| 自检 | `tools/selfcheck.py` 通过 16 项、失败 0 项；`tools/gui_smoke.py` 通过 9 项、失败 0 项 |
| 静态检查 | `pyflakes bigfish tests tools` 零输出、退出码 0；`compileall -q` 退出码 0 |
| 平台 | 平台 A：Debian 12 / Linux 5.15 / Python 3.11.2 / SQLite 3.40.1（offscreen）实测。平台 B：Windows 11 / Python 3.13.11 / SQLite 3.50.4 实测 `Ran 265 tests in 59.975s / OK (skipped=1)`——但这是修完两个 Windows 专属缺陷（msvcrt 按句柄锁、CLI restore 不关库）之后的结果 |

本文只记录 rc6 这一批的安全语义。威胁模型编号沿用 rc4 文档的 T1–T8，残余风险编号沿用 rc5 系列的 S 编号。Astra 的审计原文（`AUDIT_REVIEW.md`）条目编号 F-01～F-09 直接引用，不重新编号。

阅读顺序：第一节是五问速览，第二节逐条展开 F-05～F-09，第三节把 `AUDIT.md` 第 7 节 Q1–Q8 的口径更新一遍。二三节有部分重叠但视角不同：第二节回答「Astra 问的那五条做到什么程度」，第三节回答「作者自述的八个疑点现在成立不成立」。

---

## 一、Astra 五问速览

Astra 在 rc5 上提了九条（F-01～F-09）。F-01～F-04 由 Astra 本人写补丁修掉（提交 `cbf4d89` + `f1d1ce0`），F-05～F-09 由作者在 `ae2fe82` 修。本文只展开后五条，因为它们是「Astra 明确说没解决、且质疑修法本身不够」的部分。

| 编号 | Astra 的一句话 | rc6 结论 | 主要落点 |
| --- | --- | --- | --- |
| F-05 | GUI 停服没确认线程真的退出，却当成停成功继续换库 | 已修（代码 + 3 项单测）；真窗口验收未做 | `bigfish/gui.py:165`、`bigfish/gui.py:1235` |
| F-06 | 恢复锁只和另一个 restore 互斥，管不住普通写者 | 已修；POSIX 六项单测 + **Windows `msvcrt` 分支已真机跑通**（并因此修出缺陷 17） | `bigfish/locks.py:38`、`bigfish/backup.py:483` |
| F-07 | 审计包「无凭据」的声明无法由正则证明 | 部分修：声明与能力对齐，扫描仍是 pattern-based | `bigfish/security.py:36`、`bigfish/backup.py:205` |
| F-08 | `create_app()` 不限制监听地址，第三方 `uvicorn.run` 可绕过 | 部分修：只写清边界，代码层不拦 | `bigfish/server.py:161` |
| F-09 | 请求体与自由文本字段没有上限 | 已修（REST 侧已测）；MCP 路径仍有缺口 | `bigfish/server.py:38`、`bigfish/server.py:64` |

「已修」的判据：有代码、有专门用例、用例断言的是行为而不是实现细节。「部分修」的判据：主路径有变化，但 Astra 指出的那条绕过路径仍然存在，并且已经在第六节登记。

---

## 二、F-05～F-09 逐条

### F-05 GUI 停服被当成成功

**Astra 的问题（原文摘要）**

`ApiServer.stop()` 调 `join(timeout)` 之后没有检查线程是否还活着；`pause_services()` 把异常吞掉，而且在操作**开始之前**就把 `_services_paused` 置真。于是「API 线程还活着」会被下游（恢复流程）理解成「已经排空」，接着 `os.replace` 换库。Astra 还要求：停服超时要抛错；`_services_paused` 只能在 API 与看门狗**都确认停止**之后才置；部分失败要可靠回滚或中止并报告；要有真实线程生命周期的测试，不能用 `TestClient` 或离屏渲染替代。

**rc6 怎么处理**

三处各自收紧：

1. `ApiServer.stop()` 在 `join` 之后检查 `th.is_alive()`，还活着就抛 `TimeoutError`，文案写明「仍可能有请求在写库，已中止本次操作」。
2. 新增 `is_running()`，让调用方有「问一句」的能力，而不是靠内部字段猜。
3. `MainWindow.pause_services()` 改成「先停成功、再置标志」：`server.stop()` 与 `council.stop_watchdog()` 都过了才 `_services_paused = True`；任何一步抛错，先把已经停掉的 API 与看门狗尝试接回来（尽力而为），然后抛 `RuntimeError`，让恢复流程中止。

另外两处 `self.server.receivers(self.server.started)` 判断是顺手修的噪音：没有接收者时直接 `disconnect` 会让 PySide6 打 `RuntimeWarning`，加判断之后日志干净，语义不变。

**代码位置**

- `bigfish/gui.py:165` `stop(self, timeout: float = 15.0)`；`:174` `join`；`:176-179` `if th.is_alive(): raise TimeoutError(...)`；`:180` `stop_watcher()`。
- `bigfish/gui.py:182` `is_running()`。
- `bigfish/gui.py:1235` `pause_services()`；`:1245` 幂等早退；`:1248` receivers 判断后 `disconnect`；`:1253` `server.stop()` + `council.stop_watchdog()`；`:1258-1263` 失败分支先接回再抛 `RuntimeError`（文案「停服失败，已中止本次操作（服务可能仍在运行）：…」）；`:1264` 才置 `_services_paused = True`。
- `bigfish/gui.py:1266` `resume_services()`；`:1270` 复位标志。
- 调用方 `bigfish/gui.py:1087` `restore()`：`pause_services()` → `restore_from_bundle(..., before_replace=self.pause_services)` → `finally: resume_services()`。

**测试证据**

`tests/test_v011_rc6.py::TestGuiPauseConfirms` 三项，全部用真实线程（`QT_QPA_PLATFORM=offscreen` 只影响窗口构造，不影响线程）：

- `::test_stop_raises_when_thread_will_not_exit`（`:233`）——自造一个 `sleep(3)` 的 `Stuck` 线程塞进 `srv.thread`，`srv.stop(timeout=0.05)` 必须抛 `TimeoutError`。这条正面回答「join 超时后有没有检查」。
- `::test_pause_services_stops_api_and_watchdog`（`:250`）——先 `start_watchdog(1.0)`，再 `pause_services()`，断言 `_services_paused` 为真、`server.is_running()` 为假、看门狗线程不在跑。
- `::test_pause_is_not_set_when_stop_fails`（`:263`）——把 `win.server.stop` 换成抛 `RuntimeError`，断言抛出文案含「已中止」且 `_services_paused` 仍为 `False`。这条直接钉住 Astra 说的「操作开始前就置真」那个缺陷。

**残余风险**

见 S12：`stop_watcher()`（`bigfish/gui.py:159`）只 `join` 不检查 `is_alive`，所以「看门狗确认停了」这一半比 API 那一半弱。恢复期间看门狗即使还在跑，它的写也会被维护模式拒掉（`MaintenanceError`），后果被第二道闸兜住，但「确认停止」这个语义没有对称实现。真窗口里点一次恢复仍然没人做过（第七节第 3 条）。

---

### F-06 跨进程恢复互斥

**Astra 的问题（原文摘要）**

原来的恢复锁在 `backup.py::_restore_lock`，它只互斥「多个 restore 之间」，**不阻止另一个普通 `Database` 实例继续写**。端口探测有 TOCTOU。POSIX 上 `os.replace` 之后旧连接仍可写旧 inode，造成数据静默分叉；Windows 上通常表现为替换直接失败。CLI `--force` 显式绕过端口预检。Astra 的建议是：让所有写者与恢复者共用同一套 OS 级进程锁 / 维护租约。

**rc6 怎么处理**

新建 `bigfish/locks.py`（157 行），把「谁在用这个库」变成打开即登记的租约：

- **写者租约**：`Database.__init__` 里与库有关的第一件事就是 `WriterLease(self.path)`，位置在 `sqlite3.connect` 与三条 PRAGMA **之前**（`bigfish/db.py:238`）。POSIX 上是同一把 `flock` 的 `LOCK_SH`，多个写者可同时持有；Windows 没有共享锁，退化成「一个进程一个 `<db>.access.<pid>.lock`」。
- **恢复排他**：`exclusive_access()` 在同一个 `<db>.access.lock` 上抢 `LOCK_EX | LOCK_NB`。还有写者持 `LOCK_SH` 时立刻失败，不排队、不猜测。
- **恢复流程顺序**：`enter_maintenance` → `db.pause_lease()`（先放开自己那份，否则自己挡自己）→ `with _translate_lease(exclusive_access(target)):` → `with db._allow_during_maintenance():` → 读活库 `restore_epoch` → 之后才是 staging、替换、`_reopen`。`finally` 里无条件 `db.resume_lease()`。
- **构造失败也要放租约**：`Database.__init__` 把「读版本 → 版本过高则关连接并抛 `DatabaseVersionError` → 三条 PRAGMA」整段包进 `try/except BaseException`，异常路径先 `self._lease.release()` 再 `raise`。不加这一步会漏 flock 句柄，下一个进程会以为「还有人用」，日志里还会冒 `ResourceWarning: unclosed file`。
- **错误翻译**：`_translate_lease()`（`bigfish/backup.py:651`）把 `LeaseError` 翻成 `RestoreError`，CLI / GUI / `core.restore_from_bundle` 三层看到的是同一种异常。
- `backup.py` 里原来的 `_restore_lock()`（约 30 行）被删除，职责由租约完全取代。

POSIX 分支自己 `try/except OSError` 再抛 `LeaseError` 是必须的：第一版直接让 `BlockingIOError: [Errno 11]` 穿透，调用方拿到的是 `OSError` 而不是可翻译的业务异常。

**代码位置**

- `bigfish/locks.py:23` `LeaseError`；`:30` `_lock_file()`（`os.name == "nt"` 走 `<db>.access.<pid>.lock`，否则 `<db>.access.lock`）；`:38` `WriterLease`；`:46` `acquire()`；`:66` `release()`；`:89` `_probe_other_writers()`；`:113` `exclusive_access(db_path, timeout=0.0)`；`:133` POSIX 抢锁分支。
- `bigfish/db.py:22` `from .locks import WriterLease`；`:238` 登记租约；`:252-274` 构造期异常释放；`:525` `close(release_lease=True)`；`:549` `pause_lease()`；`:557` `resume_lease()`。
- `bigfish/backup.py:42` 导入；`:483` `db.pause_lease()`；`:484` `with _translate_lease(exclusive_access(target)):`；`:651` `_translate_lease`。

**测试证据**

`tests/test_v011_rc6.py::TestCrossProcessLease` 六项，全部在真实临时目录上跑真文件锁：

- `::test_failed_constructor_does_not_leak_lease`（`:61`）——把库的 schema 版本改成 `SCHEMA_VERSION + 1`，`Database(path)` 必须抛 `DatabaseVersionError`；紧接着 `with exclusive_access(path)` 必须能拿到。这是「异常路径泄漏租约」的直接反证。
- `::test_second_database_on_same_file_blocks_restore`（`:78`）——开第二个 `Database`，`restore_from_bundle` 必须抛 `RestoreError`，正则匹配「还有进程在用这个数据库|正在恢复」。这就是 Astra 指出的「锁只管 restore 之间」的反例。
- `::test_restore_succeeds_when_no_other_writer`（`:89`）——没有别的写者时必须恢复成功，防止修过头把正常路径也拒了。
- `::test_lease_is_released_on_close`（`:96`）——`close()` 之后必须能拿到排他锁。
- `::test_exclusive_access_fails_while_lease_held`（`:106`）——只拿一个 `WriterLease`（连库都不开）也必须挡住恢复。
- `::test_restore_keeps_lease_after_success`（`:117`）——恢复成功后本实例必须把租约抢回来，否则「恢复完就没人登记了」。

**残余风险**

四条：S1（已从「Windows 未跑」降级为「同进程共享句柄已验、真·双进程未验」）、S5（租约靠标记文件，文件可删 / 可能建不出来）、S6（`timeout` 参数没有实现等待）、S7（更早版本的实例不被租约看见，实测 rc5 持库时 rc6 恢复仍然成功）。

---

### F-07 审计包无凭据声明过强

**Astra 的问题（原文摘要）**

白名单方向对，但 `task_versions.prompt` 这类自由文本仍可能漏掉 Google key、Slack token、Stripe `sk_live_`、通用 `password=`、连接串，以及换行拆分 / 编码变形后的凭据。manifest 直接写 `contains_plaintext_credentials: false` 并标「可外发」，这件事**无法由几条正则证明**。建议改成 pattern-based 声明 + `manual_review_required: true`，并考虑默认不导出 prompt 正文或改成 opt-in。

**rc6 怎么处理**

分三层，改的是「声明」，不是「能力」——先说清楚，否则容易误读成「已经能保证无凭据」。

1. **模式表扩容**：`security._PATTERNS` 从 `(pattern, repl)` 改成 `(pattern, repl, kind)` 三元组。原有六类（`bfc-token` / `bearer` / `openai-key` / `github-token` / `private-key` / `aws-key`）补上 kind 名，新增五类：`google-key`（`AIza[0-9A-Za-z_-]{35}`）、`slack-token`（`xox[abposr]-`）、`stripe-key`（`[sr]k_live_`）、`key-value`（`password|passwd|pwd|secret|api_key|access_token` + 分隔符 + 至少 6 位值）、`uri-userinfo`（`scheme://user:pass@`）。共 11 类。
2. **可枚举的扫描能力**：新增 `SCRUB_KINDS`（11 个种类名）与 `scan_secrets(text) -> list[str]`（返回命中的种类名，去重排序）。它不是「有没有凭据」的判据，是「命中了哪几类模式」的判据。
3. **manifest 如实声明**：审计包 manifest 新增 `credential_scan`——`mode: "pattern-based"`、`patterns`（11 类）、`fields_scanned` 明说包含 `task_versions.prompt` 正文、`coverage` 明说「只覆盖上述已知形态；换行拆分、编码变形、私有格式的密钥无法保证命中」、`hits`（**脱敏之后**逐字段回扫的真实统计，预期为空但口径真实）、`manual_review_required: True`。`handling` 从「可外发：无 token、无 token 哈希、无执行票、无幂等响应。」改成「可外发（需人工复核）：……自由文本已按已知模式脱敏，但脱敏不等于证明，外发前请人工过一遍 manifest.credential_scan。」

Astra 建议的「默认不导出 prompt 正文」**没有采纳**：审计要能复核「当时到底让执行器做什么」，去掉正文等于砍掉审计证据；改成「导出 + 明示盲区 + 强制人工复核」。这是取舍，不是遗漏。

**代码位置**

- `bigfish/security.py:32` `SECRET_PREFIXES`；`:36` `_PATTERNS`（11 条三元组）；`:54` `SCRUB_KINDS`；`:78` `scrub()`；`:88` `scan_secrets()`；`:108` `contains_secret()`（拒写路径用，未变）。
- `bigfish/backup.py:182-190` 脱敏后逐字段回扫统计 `hits`；`:205` `contains_plaintext_credentials: False`（**保留未删**）；`:206-217` `credential_scan`；`:223` `handling`。

**测试证据**

`tests/test_v011_rc6.py::TestAuditManifestHonesty` 五项：

- `::test_manifest_declares_manual_review_required`（`:137`）——`mode == "pattern-based"`、`manual_review_required` 为真、`coverage` 含「无法保证」、`patterns` 含 `google-key`。
- `::test_manifest_does_not_claim_guarantee`（`:145`）——`handling` **不含**「无凭据」、**含**「复核」。这是「声明过强」的直接反例。
- `::test_new_patterns_scrub_google_slack_stripe`（`:150`）——三个样本（`AIza…`、`xoxb-…`、`sk_live_…`）脱敏后必须与原串不同。
- `::test_uri_userinfo_is_scrubbed`（`:154`）——`postgres://user:hunter2@db.local:5432/x` 脱敏后不得含 `hunter2`。
- `::test_scan_secrets_reports_kinds`（`:158`）——`Authorization: Bearer abcdefghijkl` 必须报出 `bearer`。

**残余风险**

见 S2：**仍是 pattern-based 扫描，不是 DLP**。三件事要一起看：`contains_plaintext_credentials: False` 这个布尔仍在 manifest 里（测试只钉了 `handling` 的措辞，没钉这个布尔），只读那个布尔的下游脚本仍会误读成「保证无凭据」；`hits` 为空只是「没命中这 11 类模式」；拆成两半再拼、base64、换行插入、私有格式仍然拦不住。

---

### F-08 create_app 不限监听

**Astra 的问题（原文摘要）**

`serve()` / GUI / CLI 三个入口都有回环校验，但 `create_app()` 只返回一个 ASGI app。第三方仍可以 `uvicorn.run(create_app(...), host="0.0.0.0")` 把服务绑到公网。建议要么在 API 文档里写明 `create_app` 不负责 bind，要么提供唯一启动接口。

**rc6 怎么处理**

选了 Astra 给的第一条：**写清边界，不假装封死**。`create_app()` 的 docstring 明确四件事：

1. 本函数不负责监听地址，只返回 app，不 bind socket；
2. 想真正对外服务必须走 `serve()`，那里有 `check_bind_host()`；
3. `uvicorn.run(create_app(...), host="0.0.0.0")` 会绕过闸门，**不是官方入口，也不被支持**；
4. 为什么不做代码层强制——`TestClient(create_app(...))` 依赖它不绑定；给 `create_app` 加监听参数会让测试也跟着变形。

这条的实质变化是「从没人提」变成「docstring 写明 + 风险登记为 `AUDIT.md` 的 X6 / 本文 S3」。它不改变「第三方仍可绕过」这个事实，第六节单独列出。

**代码位置**

- `bigfish/server.py:161` `create_app(council, watchdog_interval=None)`；docstring `:162-171`。
- 四个官方入口的回环闸门：`bigfish/server.py:48` `check_bind_host()`（用 `ipaddress` 真解析，不是字符串前缀判断）；`bigfish/server.py:543` `serve()` 内部再校验一次；`bigfish/__main__.py:34`；`bigfish/gui.py:103`；`bigfish/gui.py:1392`。
- `bigfish/server.py:187` `app.add_middleware(BodyLimitMiddleware, ...)`（与 F-09 同一处组装点）。

**测试证据**

**没有为本条新增测试**，如实写：`create_app` 的边界是 docstring 层面的约定，没有可断言的行为差异。现有覆盖是四个官方入口——`tests/test_v011_rc3.py::TestSecurityBoundary::test_all_entry_points_reject_public_bind`（`:573`）对 `0.0.0.0` / `::` / `[::]` / `192.168.1.10` / `100.64.1.2` / `10.0.0.1` / `0:0:0:0:0:0:0:0` 逐个断言 `BindError`，并起真实子进程验证「`--host 0.0.0.0` 时服务根本没起来」。

**残余风险**

见 S3：第三方 `uvicorn.run(create_app(...), host="0.0.0.0")` 仍然可行。要真正封死，得把 `create_app` 拆成「测试用（不绑定）」与「生产用（内部再校验）」两个名字，或者让 app 在第一次请求时校验 `scope["server"]`——两条都没做。

---

### F-09 无请求体上限

**Astra 的问题（原文摘要）**

`log` / `diff` / `tests` / `detail` 这些自由文本在 scrub 与截断之前，**整个请求体已经进了内存**。本机回环 + Tailscale 的假设降低了风险，但配置错误（有人自己绑了公网）会导致内存压力。建议在 ASGI 层加 body 上限，并在字段 schema 上加长度限制。

**rc6 怎么处理**

两层，都在 `bigfish/server.py`：

1. **ASGI 中间件**：`MAX_BODY_BYTES` 默认 8 MiB（`BFC_MAX_BODY_BYTES` 可覆盖）。`BodyLimitMiddleware` 是纯 ASGI 实现，不是 Starlette 的 `BaseHTTPMiddleware`：先查 `content-length`，声明超限直接回 413；然后包住 `receive` 边收边数，处理分块传输（chunked 根本没有 `content-length`），超了立刻抛 `_BodyTooLarge` 并回 413，不让它继续进内存。响应体固定为 `{"ok":false,"error":"request body too large"}`。
2. **字段级长度**：pydantic 模型上加 `max_length`。`MAX_TEXT_FIELD` 默认 4 MiB（`BFC_MAX_TEXT_FIELD` 可覆盖）。`ClaimBody.task_id` 128、`idempotency_key` 200；`HeartbeatBody.ticket` 256、`detail` 与 `idempotency_key` 受限；`ResultBody.ticket` 256、`log` / `diff` / `tests` / `note` 各自 `MAX_TEXT_FIELD`。超限由 FastAPI 校验层回 422，不进业务代码。

**代码位置**

- `bigfish/server.py:38` `MAX_BODY_BYTES`；`:40` `MAX_TEXT_FIELD`。
- `bigfish/server.py:64` `BodyLimitMiddleware`（`:75` `__call__`；`:87` 声明超限直接 413；`:93` `limited_receive`；`:104` `except _BodyTooLarge`；`:108` `_too_large`）。
- `bigfish/server.py:116` `_BodyTooLarge`；`:120` `ClaimBody`；`:125` `HeartbeatBody`；`:132` `ResultBody`；`:187` 中间件注册。

**测试证据**

`tests/test_v011_rc6.py::TestBodyLimit` 两项：

- `::test_declared_oversize_body_is_rejected`（`:183`）——向 `/api/v1/tasks/claim` 发 10 字节内容但声明 `content-length: 67108864`，必须回 413。这条钉的是「看声明、不等收完」。
- `::test_text_field_limit_is_enforced`（`:189`）——`/api/v1/runs/heartbeat` 的 `detail` 传 `MAX_TEXT_FIELD + 10` 字节，必须回 422。

另外两条边界是一次性手工核对，不在自动化里：向 `/mcp` POST 8 MiB + 1 KiB 的 body 得到 413，说明中间件对 MCP 路径同样生效；用真票调 `/mcp` 的 `council_submit_result`，`log` 传 4 MiB + 1 KiB（即 `MAX_TEXT_FIELD + 1024`，仍小于 8 MiB 的 body 上限）时**返回 200、`isError: false`**——**MCP 路径没有字段级上限**。根因是 `_validate_args()`（`bigfish/server.py:359`）只校验必填 / 未知字段 / 基本类型 / enum，而 `tool_specs` 里 `log` / `diff` 只声明了 `{"type":"string"}`；pydantic 的 `max_length` 只挂在 REST 的两个 Body 模型上。

**残余风险**

见 S9：MCP 路径有 body 上限、没有字段上限，`MAX_TEXT_FIELD` 的量级仍会整体进内存。修法很直接（在 `_validate_args` 里按 `maxLength` 校验，或在 tool_specs 里补 `maxLength`），本批没做。

---

## 三、AUDIT.md 第 7 节 Q1–Q8 的 rc6 口径

`AUDIT.md` 第 7 节是作者自述的八个疑点（`:263` 起）。rc5 那轮在标题上标口径，rc6 在每条末尾加了「rc6 的处置」段。下表把八条压成一张，口径按 rc6 实际情况写——**注意 Q8**：`AUDIT.md` 原文写的是「rc6 未改动」，本文按「部分解决」登记，因为部分解决来自 rc5 的入口修正、rc6 没有推进，这两句话必须同时成立。

| 编号 | 疑点 | rc6 口径 | 判据有没有变 | 依据 |
| --- | --- | --- | --- | --- |
| Q1 | `approve()` 的双重防线是否真双保险 | **未改动** | 没变 | `bigfish/core.py:662` 一行没碰；rc6 改的是相邻的 F-01（维护模式原子性），不改变并发判据 |
| Q2 | `assert_no_secrets()` 覆盖够不够 | **部分解决** | 覆盖面对齐，机制没变 | 模式 6→11 类 + `scan_secrets`；`assert_no_secrets()` 调用点仍只有 `bigfish/core.py:1298` 一处，其余仍走掩码 |
| Q3 | `check_bind_host()` 覆盖面 | **部分解决** | 没变 | `create_app()` docstring 写清边界；代码层仍不拦 |
| Q4 | `restore_epoch` 是否真的在快照之外 | **已修** | 变了（真的修好了） | F-02：读取挪进 `_allow_during_maintenance()`（`bigfish/backup.py:487`），不再 `except → 0` |
| Q5 | Watchdog 与 restore 的竞态 | **已修** | 变了 | F-01（guard 在锁内）+ F-05（停服确认） |
| Q6 | `heartbeat` 里幂等检查与续租的先后 | **已修** | 不变（顺序本来就对） | 顺序与时间戳取点 rc5 后未再动；rc6 修的是入口（F-01 不泄漏锁、F-02 世代不乱） |
| Q7 | 审计导出用黑名单还是白名单 | **已修** | 变了（声明与能力对齐） | F-07 |
| Q8 | `REVIEW_GATED` 不含 `DISPATCHED/RUNNING` | **部分解决** | 没变 | **rc6 没碰 `revoke_review()`**；部分解决来自 rc5 的 `kind != "vote"` 入口修正，判据仍是状态制 |

逐条补一句，避免表格被误读：

- **Q1**：两道检查原样——`expected_version` 比对（`None` 时不生效）与条件 `UPDATE ... WHERE id=? AND current_version=? AND status=?` 的 `rowcount != 1`。rc6 只改了同文件里 `query/one/execute` 的 guard 位置与 `_Tx.__enter__` 的加锁顺序，属维护模式原子性。Astra 的复核意见与 `AUDIT.md` 一致：真正兜底的是 `BEGIN IMMEDIATE` 事务里的条件 UPDATE，`expected_version` 只是调用方的陈旧版本保护。
- **Q2**：扩的是模式表与声明，不是拒写机制。`log_event` / `submit_result` / `heartbeat` 仍然走掩码（`scrub`），不是拒绝写入。「掩码 vs 拒写」这个取舍本批一分没动。
- **Q3**：从「没人提」变成「docstring 写明 + 风险登记」，性质没变。
- **Q4**：本批唯一一条「rc5 声称修了、其实没修好」的。rc5 的 `captured_epoch = db.restore_epoch()` 在 `enter_maintenance()` 之后、普通访问路径上执行，会被自己的 `_guard()` 打中抛 `MaintenanceError`，外层 `except` 又把它吞成 `0`——于是 `max(0, staged_epoch) + 1` 只保证「快照世代 + 1」，恢复一个很旧的快照就能让 epoch 回退。F-02 把读取挪进 `_allow_during_maintenance()`，不再用 0 兜底。回归测试把活库先抬到 epoch 7，再恢复一个 epoch=0 的旧快照，断言结果必须是 **8**（不是 1）。
- **Q5**：见第二节 F-05。这条的「已修」只到单测层，真窗口没点过。
- **Q6**：顺序（票校验在幂等命中之前）与时间戳取点（`t = now()` 在 `tx()` 内部）都是 rc4/rc5 就对了，rc6 没动。rc6 修的是入口两处：拿到锁却因维护被拒时不再泄漏锁；恢复期读错世代会让票校验里的 epoch 对不上，导致心跳「票无法认证」，这条随 F-02 一起修掉。
- **Q7**：白名单方向没改（`AUDIT_COLUMNS`，`bigfish/backup.py:58`）。`contains_plaintext_credentials: false` 那个布尔**没删**，测试只钉了 `handling` 的措辞——这一点可以继续挑刺，见 S2。
- **Q8**：`revoke_review()`（`bigfish/core.py:497`）rc6 一行没碰，`if status in (PENDING_APPROVAL, APPROVED)` / `elif status in (DISPATCHED, RUNNING, CANCELLING)` 的硬编码原样；rc5 的 `kind != "vote"` 入口修正也原样。所以「部分解决」这个口径指的是 rc5 的进展，不是 rc6 的。Astra 的复核意见与本文件一致：三个已派工状态有显式阻断与吊销；已提交 / 已完成不自动回滚是需要在产品语义里写明的不可逆边界，不是遗漏。

---

## 四、威胁模型现状（T1–T8）

T1–T8 的表述沿用 rc4 文档，不改写；「rc6 变化」列只写本批实际发生的动作。

| 编号 | 威胁 | rc5 状态 | rc6 变化 |
| --- | --- | --- | --- |
| T1 | 库或备份包泄露，明文执行票外流 | 票只存 `token_hash`；审计包改列白名单 | 审计包多一段 `credential_scan` 如实声明盲区（F-07）；库文件仍未加密（S11） |
| T2 | 执行器伪造审查票 | 服务端签发、确定性派生 | 无变化 |
| T3 | 执行器自己批准 | 只认人类所有者角色 | 无变化 |
| T4 | 审批跨版本降级 | 审批绑 `current_version` | 无变化 |
| T5 | 恢复把库搞坏 | `.prerestore` 副本 + 排空 + 授权作废 | 新增跨进程租约（F-06），恢复前必须拿到排他锁；Windows 分支已真机跑通（S1 已降级） |
| T6 | 长期任务被误判死（看门狗） | 心跳时间戳移入事务 | 无变化（F-05 只影响「恢复前停服务」这条路径） |
| T7 | 网络暴露到公网 | 默认只绑回环 + 代理进程闸门 | `create_app()` 写明边界，但 app 层仍不拦（S3） |
| T8 | 模型身份可被密码学证明 | 明确不实现 | 无变化（仍不做） |

---

## 五、测试质量与新增用例

全量从 244 项涨到 265 项，净增 21 项（`def test_` 计数，与 `unittest` 的 `Ran` 数一致）：

| 文件 | 项数 | 本批变化 |
| --- | --- | --- |
| `tests/test_v011_rc6.py` | 16 | 新增（4 个类） |
| `tests/test_v011_rc5_restore_authority.py` | 24 | +3（epoch 单调、query 与 tx 的 guard 原子性） |
| `tests/test_v011_rc5_key_heal.py` | 8 | +1（启动失败必须拒绝，Astra 写） |
| `tests/test_v011_rc5_cli_guard.py` | 4 | +1（端口预检必须在开库之前，Astra 写） |
| 其余 12 个文件 | 213 | 无变化 |
| 合计 | **265** | +21 |

`tests/test_v011_rc6.py` 内部：

| 类 | 项数 | 对应 |
| --- | --- | --- |
| `TestCrossProcessLease`（`:60`） | 6 | F-06 |
| `TestAuditManifestHonesty`（`:130`） | 5 | F-07 |
| `TestBodyLimit`（`:166`） | 2 | F-09 |
| `TestGuiPauseConfirms`（`:202`） | 3 | F-05 |

本批没有「把断言写宽」的改动。两处既有测试被顺手修：`tests/test_v011_rc5_cli_guard.py` 的 `accept_loop()` 原来在 `srv.close()` 之后才 `settimeout`，收尾抛 `OSError: [Errno 9] Bad file descriptor` 并刷栈（测试本身是通过的，纯噪音），改后先设超时再进循环并吞掉 `OSError`；`tests/test_v011_rc6.py` 清掉两个未用导入，让 `pyflakes` 保持零输出。

---

## 六、残余风险（rc6）

rc5 的 S1/S3/S4/S5/S6/S7 中，S1（跨文件系统锁）已被 `locks.py` 取代并在双平台实测，S3（自由文本正则）保留并更新，S5（租约靠标记文件）保留。新增 S8–S13 是本批实测出来的。表中「实测」指本文写作时真的跑过；「代码」指只有代码路径、没有运行验证。

| 编号 | 残余风险 | 实测 / 代码 | 与 `AUDIT.md` 的对应 |
| --- | --- | --- | --- |
| S1 | **Windows `msvcrt` 分支已在真机跑通，但真·双进程仍未验**。本轮平台 B 首次跑就撞上 `[Errno 13] Permission denied`：`msvcrt.locking` 的字节区间锁**按文件句柄**记，租约文件按 pid 命名，同进程第二个 `Database` 用第二个句柄锁同一区间必失败——已被误判成「有别人在用」，Linux 的 `flock` 永远碰不到这条。修法是模块级 `_win_leases`（路径 -> [句柄, 引用计数]），同进程复用句柄、计数归零才解锁。修完 265 项全绿。**仍未验的**：两个真正独立的 Windows 进程同时恢复、`os.replace` 撞文件占用、NTFS ACL 之外的其他文件语义 | 实测（含缺陷复现 + 修复后全绿） | R3 / U11 |
| S2 | **审计包仍是 pattern-based 扫描，不是 DLP**。11 类正则命中即脱敏，`hits` 为空只说明「没命中这 11 类」。拆两半拼接、base64、换行插入、私有格式拦不住。另外 `contains_plaintext_credentials: False` 布尔仍在 manifest 里，只读它的下游脚本会误读成「保证无凭据」 | 实测（manifest 13 键、`hits` 为空、布尔为 false） | X5 / Q7 |
| S3 | **`create_app()` 仍可被第三方 `uvicorn.run(create_app(...), host="0.0.0.0")` 绕过**。docstring 写清了，代码层没拦 | 实测（`create_app` 只返回 app，不 bind） | X6 / Q3 |
| S4 | **维护模式只是单进程内屏障**。`enter_maintenance()` 用 `self._lock`（`threading.RLock`）等在途写事务，`_guard()` 抛 `MaintenanceError`——两者都只在**当前进程的线程之间**生效。另一个进程的写不会被 `MaintenanceError` 拒绝，只能靠 `exclusive_access()` 在**替换文件那一瞬**挡住；而排他锁只认得走 `bigfish.db.Database` 的写者 | 实测（见 S5/S7） | Q5 |
| S5 | **租约靠标记文件，标记文件能被删、也可能建不出来**。实测三条：① 库还开着时手工 `unlink` 掉 `council.db.access.lock`，`exclusive_access()` **立刻成功**（屏障失效，之后锁文件被重建）；② 锁所在目录不可写（`chmod 0555` 且锁已删）时 `bigfish/locks.py:51` 的 `open(target, "a+b")` 抛 **`PermissionError`**（不是 `LeaseError`，`Database.__init__` 原样上抛），rc5 在同目录能正常打开；③ 锁路径被同名**目录**占住时抛 `IsADirectoryError`，CLI 表现是 `FileExistsError: [Errno 17] File exists`、退出码 1，rc5 同样能打开 | 实测 | F-06 的边界 |
| S6 | **`exclusive_access(db, timeout=5.0)` 的 `timeout` 没有实现等待**。`bigfish/locks.py:113` 收了参数但没用：写者活着时立即失败（实测 elapsed `0.000s`），文案是「还有进程在用这个数据库（写者租约仍在生效），拒绝替换。请先关闭 GUI / 停掉 headless 服务。」调用方拿到的是「立即失败」语义，不是「最多等 5 秒」 | 实测 | F-06 |
| S7 | **更早版本的实例不被租约看见**。实测：rc5（没有 `locks.py`）进程打开同一个库并保持，rc6 的 `restore_from_bundle()` **恢复成功**（`epoch=1`），旧进程仍在跑——它没登记租约，排他锁自然拿得到。反方向（rc6 持库、rc5 打开）没有这个问题，因为 rc5 不检查锁。混跑期要按「先停干净再升级」执行，见 `docs/MIGRATION-v0.1.1-rc6.md` | 实测 | F-06 的边界 |
| S8 | **恢复会在目录里留下两个额外文件**：`council.db.prerestore`（替换前副本）与 `council.db.restore-new.access.lock`（staging 库 `<db>.restore-new` 被 `Database(staging)` 打开时登记的租约，0 字节，POSIX 分支不 unlink）。两者都不进备份包，可手工删；重复恢复不累加 | 实测 | F-06 |
| S9 | **MCP 路径没有字段级上限**。`/mcp` 的 body 受 8 MiB 中间件约束，但 `council_submit_result` 的 `log` 传 4 MiB + 1 KiB 仍返回 200、`isError: false`。`_validate_args()` 只校验必填/未知字段/类型/enum | 实测 | F-09 的残余 |
| S10 | **自由文本仍只做正则脱敏**（承 rc5 S3）。`log` / `diff` / `tests` / `note` 落库前过 `scrub()`；拆半拼接、编码变形拦不住 | 代码（模式已扩到 11 类） | X2 / Q2 |
| S11 | **数据库文件本身仍未加密**（承 rc5 S4）。票只存哈希，但任务正文、版本、审计记录都是明文 SQLite | 代码 | R7 |
| S12 | **`stop_watcher()` 只 join 不检查 is_alive**（`bigfish/gui.py:159-163`）。所以 F-05 的「两处都确认停了」在实现上是「API 强确认 + 看门狗弱确认」。看门狗即使还在跑，写入也会被维护模式拒掉，后果被兜住 | 代码 | F-05 |
| S13 | **「回滚也失败 → 保留维护模式」分支仍无自动化覆盖**（承 rc5 S7）。触发它要「替换失败 + 回滚失败」同时发生，构造代价高 | 代码 | — |

S1–S13 里最该被继续挑的是 S4 与 S5：它们决定「跨进程互斥」的真实强度——**租约挡的是「按规矩用 `bigfish.db.Database` 的进程」，不是「所有能写这个文件的东西」**。第三方脚本用裸 `sqlite3.connect()` 写同一个库，`exclusive_access()` 照样成功（实测），这一点不要被「已修」两个字盖过去。

---

## 七、本轮没有做的事

1. **平台 B 只跑了全量 265 项，没跑 selfcheck / gui_smoke / pyflakes。** Windows 11 / Python 3.13.11 上 `Ran 265 tests in 59.975s / OK (skipped=1)`、退出码 0（详见 [测试报告](TEST-REPORT-v0.1.1-rc6.md) 第 6.1 节）；但 `tools/selfcheck.py`、`tools/gui_smoke.py`、`pyflakes` 三条在平台 B 上没有第二份证据。`os.replace` 撞文件占用、NTFS ACL 之外的文件语义仍未验（S1）。
2. **没有做跨设备 MCP 闭环。** rc2 时期的 11/11 验的是 rc2 代码，不能拿来当 rc6 的证据；本批没有重跑。
3. **没有做 GUI 真窗口验收。** `tools/gui_smoke.py` 全程 `QT_QPA_PLATFORM=offscreen`（通过 9 项），覆盖的是构造与信号连接；F-05 的停服超时路径有单测（3 项），但**没有人在真窗口里点过一次恢复**。布局、焦点顺序、真实 DPI 下的表现同样没人验收。
4. **没有改生产 DSH。** 所有验证都在本仓库与临时数据目录内进行。
5. **没有开任何端口。** 没有把 API、`/mcp` 或 `bfc-proxy` 暴露到回环之外。
6. **没有导入任何 MCP 连接**，没有注册开机自启。
7. **没有提交、没有 push、没有打 tag。** 本文档是本批交付物之一；工作区里 `AUDIT.md` / `CHANGELOG.md` / `README.md` 的未提交改动不是本次文档工作产生的。

---

## 八、语义变更与兼容性

| 项 | rc5 | rc6 |
| --- | --- | --- |
| `SCHEMA_VERSION` / `BACKUP_FORMAT_VERSION` | 4 / 4 | 4 / 4（**未变**） |
| 打开数据库 | 直接 `sqlite3.connect` | 先登记写者租约（`<db>.access.lock`，Windows 为 `<db>.access.<pid>.lock`），失败抛 `LeaseError` |
| 恢复前置条件 | 端口预检 + 维护模式 | 端口预检 + 维护模式 + **排他锁**（有活写者则 `RestoreError`） |
| 恢复失败的报错类型 | 多为 `RestoreError` / `OSError` | 统一 `RestoreError`（`_translate_lease`） |
| GUI 停服超时 | 静默当成功 | 抛 `TimeoutError`，恢复中止；`_services_paused` 不置真 |
| 审计包 manifest | 12 键 | 13 键（新增 `credential_scan`），`handling` 措辞改为「可外发（需人工复核）」 |
| 凭据识别 | 6 类模式 | 11 类模式 + `SCRUB_KINDS` + `scan_secrets()` |
| 请求体 | 无上限 | 8 MiB（`BFC_MAX_BODY_BYTES`），超限 413 |
| 自由文本字段 | 无长度限制 | 4 MiB（`BFC_MAX_TEXT_FIELD`），REST 超限 422；MCP 未覆盖（S9） |
| 磁盘痕迹 | 无锁文件 | 多一个 0 字节锁文件；恢复后多 `.prerestore` 与 `.restore-new.access.lock` |
| 混跑 | — | rc6 持库时 rc5 可正常打开；**rc5 持库时 rc6 的恢复不被拦**（S7） |

数据库不需要迁移：schema 仍是 4，见 `docs/MIGRATION-v0.1.1-rc6.md`。回滚见 `docs/ROLLBACK-v0.1.1-rc6.md`。

---

## 九、复核方式

```bash
cd /home/dsh/bigfish-council

# 全量测试（265 项）
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .

# 只跑本批新增的 16 项
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest tests.test_v011_rc6 -v

# 自检与 GUI 冒烟
.venv/bin/python tools/selfcheck.py
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/gui_smoke.py

# 静态检查
.venv/bin/python -m pyflakes bigfish tests tools
.venv/bin/python -m compileall -q bigfish tests tools
```

按条目定位测试：

| 条目 | 测试 |
| --- | --- |
| F-05 | `tests/test_v011_rc6.py::TestGuiPauseConfirms`（3 项） |
| F-06 | `tests/test_v011_rc6.py::TestCrossProcessLease`（6 项） |
| F-07 | `tests/test_v011_rc6.py::TestAuditManifestHonesty`（5 项） |
| F-08 | 无新增；四个入口看 `tests/test_v011_rc3.py::TestSecurityBoundary::test_all_entry_points_reject_public_bind` |
| F-09 | `tests/test_v011_rc6.py::TestBodyLimit`（2 项） |
| F-01 / Q5 | `tests/test_v011_rc5_restore_authority.py::TestMaintenanceMode::test_query_guard_is_atomic_with_lock`、`::test_transaction_guard_is_atomic_with_lock` |
| F-02 / Q4 | `tests/test_v011_rc5_restore_authority.py::TestRestoreFreezesApproved::test_restore_epoch_is_monotonic_when_restoring_an_older_snapshot` |
| F-03 | `tests/test_v011_rc5_key_heal.py::TestStartupFailsClosed::test_open_council_refuses_to_start_when_key_repair_fails` |
| F-04 | `tests/test_v011_rc5_cli_guard.py::TestCliRestoreGuard::test_busy_port_is_checked_before_database_open` |
| Q7 白名单 | `tests/test_v011_rc5_review_audit.py` 的审计包用例 + `tests/test_v011_rc6.py::TestAuditManifestHonesty` |
