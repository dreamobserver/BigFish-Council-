# 修复总表：bigfish-council v0.1.1-rc6

| 项 | 值 |
| --- | --- |
| 版本 | `0.1.1-rc6`（`bigfish/__init__.py:6`） |
| 日期 | 2026-11-20 |
| 相对基线 | rc5 发布提交 `b8f6de1`；审计基线是 `c6d663d`（rc5 tag 点，在 `remotes/ghrc5` 上） |
| 审计来源 | `AUDIT_REVIEW.md`（Astra，122 行），审计分支 `review/rc5-hardening-20261120`，F-01~F-09 共九条 |
| 分支 / HEAD | `fix/v0.1.1-rc4-hardening` / `ae2fe82`（`fix(rc6): 处理 Astra 审计 F-01~F-09…`）。修复分三个提交：`cbf4d89`（F-01~F-04，Astra 本人写）、`f1d1ce0`（F-03 异常类型收口）、`ae2fe82`（F-05~F-09 + 顺手修） |
| 差异规模 | 任务书给的「17 文件 +389/-142」在本仓库实测不成立，见补充说明一。真实三个口径：相对 `c6d663d` 代码侧 `19 files changed, 826 insertions(+), 142 deletions(-)`（含 5 个 rc5 遗留文件）；剔除这 5 个后 `14 files changed, 818 insertions(+), 134 deletions(-)`；相对 `b8f6de1`（纯 rc6）`13 files changed, 817 insertions(+), 133 deletions(-)`。本轮另新增两个文件：`bigfish/locks.py`（157 行 / 5850 字节）、`tests/test_v011_rc6.py`（280 行 / 10646 字节） |
| 库结构 | `SCHEMA_VERSION = 4`（`bigfish/db.py:24`）、`BACKUP_FORMAT_VERSION = 4`（`bigfish/db.py:25`），**schema v4 未变** |
| 全量测试 | `Ran 265 tests` / `OK`，本轮两次实测 46.926s 与 67.168s（Debian 12 / Python 3.11.2 / SQLite 3.40.1 / offscreen） |
| 自检与冒烟 | `tools/selfcheck.py` 通过 16 项、失败 0 项；`tools/gui_smoke.py` 通过 9 项、失败 0 项 |
| 静态检查 | `pyflakes bigfish tests tools` 零输出 / exit 0；`compileall -q` exit 0 |
| 平台 | A 平台（Debian 12.15 / Python 3.11.2 / SQLite 3.40.1）已跑 `Ran 265 / OK`；B 平台（Windows 11 / Python 3.13.11 / SQLite 3.50.4）已跑 `Ran 265 tests in 59.975s / OK (skipped=1)`，退出码 0 —— 首次跑是 2 ERROR，定位出两个 Windows 专属真缺陷并修掉后才全绿（见第七节） |

状态口径：

- `已修复`：代码已改，且有能失败的回归测试，本批次在 Linux 上实测通过。
- `已修复（未覆盖）`：代码已改，自动化证据不完整，另行注明缺什么。
- `已修复（文档级）`：改的是契约与文档，本身没有可失败的自动化断言，靠别处的行为测试兜。
- 标「（Linux + Windows）」的行表示两个平台都有实机记录；只标「已验证」的行是 Linux 实测。Windows 记录来自平台 B 的 `Ran 265 tests in 59.975s / OK (skipped=1)`。

严重度口径（沿用审计给的 P1 / P2，不自行改级）：P1 = 直接破坏一条安全保证（维护屏障、世代单调、启动 fail-closed、跨进程互斥）；P2 = 不直接破坏保证，但会误导读者或放大资源/泄露面。

---

## 一、F-01~F-09 修复总表

| 编号 | 级别 | Astra 原始问题（一句话） | 修改文件（行号 / 函数） | 修复方案 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| F-01 | P1 | 维护模式 `_guard()` 与抢 `_lock` 不是原子操作：线程 A 先过 guard 再被挂起，线程 B 在这中间开维护并放锁，A 拿到锁后照样读写，维护屏障漏掉一个已排队的操作 | `bigfish/db.py:461` `query`、`:467` `one`、`:472` `execute`、`:477` `_Tx`（`:485` `__enter__`） | 三个访问器都改成「先 `with self._lock:`，再在锁内 `self._guard()`」，检查与加锁进同一临界区：`with self._lock:` → `self._guard()` → `return self._conn.execute(...)`。`_Tx.__enter__` 改成 `self.db._lock.acquire()` 之后用 `try:` 包住 `self.db._guard()` + `BEGIN IMMEDIATE`/`SAVEPOINT` + `self.db._depth += 1`，`except BaseException:`（不是 `except Exception`）统一 `self.db._lock.release()` 再 `raise`，guard / BEGIN / SAVEPOINT 任一步失败都归还锁 | `tests/test_v011_rc5_restore_authority.py::test_query_guard_is_atomic_with_lock`（:340）、`::test_transaction_guard_is_atomic_with_lock`（:343）；两例共用 `_assert_guard_lock_order`（:296），用真线程 + 两个 `threading.Event` 把操作卡在「已过 guard、未持锁」的位置，再试 `enter_maintenance(timeout=0.05)`，断言维护模式插不进去 | 已修复 |
| F-02 | P1 | `restore_bundle()` 先 `enter_maintenance()` 再读 `db.restore_epoch()`，而后者走普通访问会被自己的维护模式打成 `MaintenanceError`，外层 `except Exception` 把它吞成 0，世代「只增不减」的保证失效 | `bigfish/backup.py:441` `restore_bundle`、`:479` `enter_maintenance`、`:483` `pause_lease`、`:484` `_translate_lease(exclusive_access(...))`、`:485` `_allow_during_maintenance`、`:487` `captured_epoch = db.restore_epoch()` | 删掉 `try: captured_epoch = db.restore_epoch() except Exception: captured_epoch = 0` 这段兜底，把这一行整体挪到 `with db._allow_during_maintenance():` **里面**（旁路生效后才读），异常不再被吞。之后的 `_prepare_staging` 拿到的就是活库真实世代，`max(captured_epoch, staged_epoch) + 1` 才是单调的 | `tests/test_v011_rc5_restore_authority.py::test_restore_epoch_is_monotonic_when_restoring_an_older_snapshot`（:199）：先导出 epoch=0 的包，再把活库 `UPDATE meta SET value='7' WHERE key='restore_epoch'` 抬到 7，恢复旧包后断言 `out["restore_epoch"] == 8` 且 `db.restore_epoch() == 8` | 已修复 |
| F-03 | P1 | `runtime.py` 里 `ensure_key_consistency()` 的异常被 `except Exception: pass` 吞掉后继续 `load_or_create_key()` 并启动，磁盘密钥与库指纹不一致且修复中途失败时，程序会带着未核验的旧密钥跑起来，这正好是该检查要阻止的事；原注释还声称「失败会留在 audit / 日志里」，而那个 except 分支本身不写任何日志 | `bigfish/runtime.py:6`（导入 `CouncilError`）、`:18-21` `open_council` | 改成 fail-closed：`try: ensure_key_consistency(db, settings.key_path)` / `except Exception as exc:` → 先 `db.close()`（别把打开的连接扔给 GC）→ `raise CouncilError("执行票密钥一致性校验失败，拒绝启动") from exc`。用 `CouncilError` 而不是 `RuntimeError`，是为了让 CLI 在 `bigfish/cli.py:234` 的 `except (CouncilError, RestoreError, DatabaseVersionError)` 里统一翻成退出码 2 并打 `错误: …`。这一步是 `f1d1ce0` 收的口（`cbf4d89` 里先抛的是 `RuntimeError`） | `tests/test_v011_rc5_key_heal.py::test_open_council_refuses_to_start_when_key_repair_fails`（:140）：`patch("bigfish.runtime.ensure_key_consistency", side_effect=OSError("模拟密钥修复失败"))` + `patch("bigfish.runtime.load_or_create_key")`，断言 `assertRaisesRegex(RuntimeError, "密钥一致性校验失败")` 且 `load_key.assert_not_called()`——后半句是关键：拒绝启动之后不许再碰密钥文件 | 已修复 |
| F-04 | P2 | CLI `restore` 的端口预检发生在打开数据库之后：`main()` 开头无条件 `s, db, council = build(a.data_dir)`，而 `build()` 会走 `open_council()`（开库、建/迁 schema、跑密钥一致性自检），等这些都做完才在 restore 分支里探端口，端口保护来得太晚 | `bigfish/cli.py:130-139`（新增 restore 前置分支）、`:222` restore 分支（只剩 `dump(...)`）、`:41` `_port_busy`、`:33` `build()` 内的 `open_council` | 在 `a = p.parse_args(argv)` 之后按子命令分流：`if a.cmd == "restore":` → 先 `s = Settings.load(...)`（只读配置，不碰库）→ `if not a.force and _port_busy(s.api_host, s.api_port):` 打拒绝文案并 `return 2` → 通过后才 `db, council = open_council(s)`；`else: s, db, council = build(a.data_dir)`。原来写在 restore 分支里的那段端口预检整段删除，避免两处判定漂移 | `tests/test_v011_rc5_cli_guard.py::test_busy_port_is_checked_before_database_open`（:84）：`patch("bigfish.cli._port_busy", return_value=True)` + `patch("bigfish.cli.open_council")`，断言 `rc == 2` 且 `open_db.assert_not_called()` | 已修复 |
| F-05 | P1/P2 | `ApiServer.stop()` join 超时后不检查线程是否还活着，`pause_services()` 又在操作开始前就置 `_services_paused = True` 并把 `stop()` / `stop_watchdog()` 的异常一起吞掉，于是恢复流程会在 API 线程仍在写库时去替换数据库文件 | `bigfish/gui.py:165` `stop`、`:182` `is_running`、`:1235` `pause_services`（`:1248` 与 `:1349` 两处 `receivers` 前置判断、`:1263` 抛错、`:1266` `resume_services`） | 三处收紧：① `stop()` 在 `th.join(timeout)` 之后加 `if th.is_alive(): raise TimeoutError(f"API 线程 {timeout:.1f} 秒内没有退出，仍可能有请求在写库，已中止本次操作")`，返回类型标成 `-> None`；② 新增 `is_running()`（`return bool(th is not None and th.is_alive())`），把「停了没有」变成可断言的事实；③ `pause_services()` 先 `_services_paused = True` 的写法删掉，改成「`self.server.stop()` + `self.council.stop_watchdog()` 都成功之后才置位」，`except Exception as exc:` 里先把已停掉的 API 与 watchdog 接回来（`self.server.start()` + `self.council.start_watchdog(...)`，尽力而为）再 `raise RuntimeError(f"停服失败，已中止本次操作（服务可能仍在运行）：{exc}")`。顺手把两处 `self.server.started.disconnect(self.on_server)` 包进 `if self.server.receivers(self.server.started):`，消掉重复断开时的 `PySide6 RuntimeWarning: libpyside: Failed to disconnect` | `tests/test_v011_rc6.py::TestGuiPauseConfirms`（:202）三项：`test_stop_raises_when_thread_will_not_exit`（:233，自造 `class Stuck(threading.Thread)` 睡 3 秒 + `srv.stop(timeout=0.05)` 断言 `TimeoutError`）、`test_pause_services_stops_api_and_watchdog`（:250，断言 `_services_paused` 为真、`server.is_running()` 为假、watchdog 线程已死）、`test_pause_is_not_set_when_stop_fails`（:263，把 `server.stop` 换成抛 `RuntimeError` 的 lambda，断言报错且 `_services_paused` 仍为假）。三例都是真线程生命周期，没有用 TestClient 或离屏窗口顶替 | 已修复（单测级；真窗口未验收，见第六节） |
| F-06 | P1 | 跨进程恢复互斥只是尽力而为：`_restore_lock()` 只互斥两个 `restore` 调用，管不住另一个普通 `Database` 实例继续写；端口探测有 TOCTOU，对方可以用别的端口或直接开同一数据目录，`--force` 更是显式绕过 | 新增 `bigfish/locks.py:1-157`（`:23` `LeaseError`、`:38` `WriterLease`、`:46` `acquire`、`:66` `release`、`:89` `_probe_other_writers`、`:112-157` `exclusive_access`）；`bigfish/db.py:22` 导入、`:238` `self._lease = WriterLease(self.path)`、`:254-274` 构造失败释放、`:525` `close(release_lease=True)`、`:549` `pause_lease`、`:557` `resume_lease`；`bigfish/backup.py:42` 导入、`:484`、`:643`、`:651` `_translate_lease`；删除原 `_restore_lock()`（约 45 行） | 所有 `Database` 打开即登记一份写者租约，恢复必须拿到排他访问锁才能换文件。POSIX：同一个 `<db>.access.lock` 上写者持 `fcntl.LOCK_SH`（可多份），恢复抢 `LOCK_EX \| LOCK_NB`；Windows 的 `msvcrt` 没有共享锁，于是写者各持 `<db>.access.<pid>.lock`，恢复用 `_probe_other_writers()` 逐个试锁——锁得上说明那个 pid 已退出（文件是残留），锁不上说明还活着，直接 `LeaseError`。关键陷阱：恢复流程自己也是写者，抢排他前必须先 `db.pause_lease()` 放开自己那份 SH，`finally` 里无条件 `db.resume_lease()` 夺回（`close(release_lease=False)` 同理）；`Database.__init__` 里 `PRAGMA` 与 `_peek_schema_version()` 整段包进 `try/except BaseException:`，失败时先 `self._lease.release()` 再 `raise`，否则「库版本过高打不开」这条路径会把 flock 句柄留给 GC（实测表现为 `ResourceWarning: unclosed file '...council.db.access.lock'`）；`exclusive_access` 的 POSIX 分支自己 `try/except OSError` 再抛 `LeaseError`（第一版裸 `BlockingIOError: [Errno 11]` 直接穿透到测试）；`_translate_lease()` 把 `LeaseError` 翻成 `RestoreError`，因为锁模块不知道上层有「恢复」这个业务概念 | `tests/test_v011_rc6.py::TestCrossProcessLease`（:60）六项：`test_failed_constructor_does_not_leak_lease`（:61）、`test_second_database_on_same_file_blocks_restore`（:78）、`test_restore_succeeds_when_no_other_writer`（:89）、`test_lease_is_released_on_close`（:96）、`test_exclusive_access_fails_while_lease_held`（:106）、`test_restore_keeps_lease_after_success`（:117） | 已修复（Linux + Windows）。POSIX `fcntl` 六项单测；Windows `msvcrt` 真机跑通并因此修出缺陷 17（msvcrt 字节区间锁按句柄计，同进程第二个 `Database` 会 EACCES） |
| F-07 | P1/P2 | 审计包 manifest 直接写 `contains_plaintext_credentials: false` 并标注「可外发：无 token…」，但白名单里仍含 `task_versions.prompt` 等自由文本，脱敏只覆盖 BFC token / Bearer / 部分 `sk-` / GitHub / AWS / PEM 私钥，把「未命中已知正则」说成了「保证无凭据」 | `bigfish/security.py:36`（`_PATTERNS` 改三元组）、`:37-43`（六条原有补 kind）、`:45-51`（新增五类）、`:54` `SCRUB_KINDS`、`:78` `scrub`、`:88` `scan_secrets`、`:108` `contains_secret`；`bigfish/backup.py:183-189`（`hits` 统计）、`:214-221`（`credential_scan`）、`:223-225`（`handling` 改口径） | 正则表从 `(pattern, repl)` 改成 `(pattern, repl, kind)`，原有六条补上种类名（`bfc-token`/`bearer`/`openai-key`/`github-token`/`private-key`/`aws-key`），新增五条：`google-key`（`\bAIza[0-9A-Za-z_\-]{35}\b`）、`slack-token`（`\bxox[abposr]-[A-Za-z0-9\-]{10,}`）、`stripe-key`（`\b[sr]k_live_[A-Za-z0-9]{10,}`）、`key-value`（`password\|passwd\|pwd\|secret\|api[_-]?key\|access[_-]?token` + `=`/`:` + 值）、`uri-userinfo`（`scheme://user:pass@`）。新增 `SCRUB_KINDS` 与 `scan_secrets(text) -> list[str]`（返回命中的种类名，去重排序）。manifest 新增 `credential_scan` 块，字段为 `mode: "pattern-based"`、`patterns`、`fields_scanned: "全部导出字符串列（含 task_versions.prompt 正文）"`、`coverage: "只覆盖上述已知形态；换行拆分、编码变形、私有格式的密钥无法保证命中"`、`hits`、`manual_review_required: True`；`hits` 是脱敏**之后**再逐字段回扫统计的，不是脱敏前的命中数；`handling` 改成「可外发（需人工复核）：…脱敏不等于证明，外发前请人工过一遍 manifest.credential_scan」 | `tests/test_v011_rc6.py::TestAuditManifestHonesty`（:130）五项：`test_manifest_declares_manual_review_required`（:137，断言 `mode`/`manual_review_required`/`coverage` 含「无法保证」/`patterns` 含 `google-key`）、`test_manifest_does_not_claim_guarantee`（:145，断言 `handling` 不含「无凭据」且含「复核」）、`test_new_patterns_scrub_google_slack_stripe`（:150）、`test_uri_userinfo_is_scrubbed`（:154，`postgres://user:hunter2@db.local:5432/x` 里不许留 `hunter2`）、`test_scan_secrets_reports_kinds`（:158） | 已修复 |
| F-08 | P2 | `create_app()` 只返回 ASGI app、不限制监听地址，第三方仍可 `uvicorn.run(create_app(...), host="0.0.0.0")` 绕过 `check_bind_host()` 那道回环闸门 | `bigfish/server.py:161` `create_app`、`:162-171` 新增 docstring | 选择写清边界而不是假装封死：docstring 明确「本函数不负责监听地址；它只返回 app，不 bind socket。想真正对外提供服务，必须走 `serve()`——`serve()` 里有 `check_bind_host()` 这道回环闸门。直接 `uvicorn.run(create_app(...), host="0.0.0.0")` 会绕过闸门，这不是官方入口，也不被支持：部署只用 `serve()` / GUI / `python -m bigfish`」，并在 docstring 里写明理由：「审计指出『四个入口都校验了』不等于『app 本身安全』，与其假装封死，不如把边界说清楚，让后来的人别再自己 uvicorn.run」。不封死的原因是 `fastapi.testclient.TestClient` 依赖 `create_app` 不 bind，封死会连带废掉全部 API 测试与 GUI 内嵌用法 | 无专用用例（改的是契约文本）。护栏在既有绑定测试里：`tests/test_v011_rc3.py::test_all_entry_points_reject_public_bind` | 已修复（文档级，无回归测试） |
| F-09 | P2 | `log` / `diff` / `tests` / `detail` 等自由文本在进 `scrub` 和截断之前，整个请求体已经被解析进内存，被控制的执行器可以一次塞进超大 body 压内存 | `bigfish/server.py:38-40`（两个上限常量）、`:64-114` `BodyLimitMiddleware`、`:116` `_BodyTooLarge`、`:120-140`（三个请求模型的 `max_length`）、`:187` `add_middleware` | 两层：ASGI 层 `BodyLimitMiddleware`（默认 8 MiB，`BFC_MAX_BODY_BYTES` 可调）先看 `content-length`，超了直接回 413；没这个头（chunked）就包住 `receive()` 边收边累加 `seen`，超限 `raise _BodyTooLarge`，被外层接住回 413 + `{"ok":false,"error":"request body too large"}`，不让数据进内存。字段层 `MAX_TEXT_FIELD`（默认 4 MiB，`BFC_MAX_TEXT_FIELD` 可调）：`ClaimBody.task_id` 128、`idempotency_key` 200；`HeartbeatBody.ticket` 256、`detail` `MAX_TEXT_FIELD`、`idempotency_key` 200；`ResultBody.ticket` 256，`log`/`diff`/`tests`/`note` 全部 `MAX_TEXT_FIELD`。超字段上限由 pydantic 返回 422 | `tests/test_v011_rc6.py::TestBodyLimit`（:166）两项：`test_declared_oversize_body_is_rejected`（:183，声明 `content-length: 67108864` 但只发 10 字节，断言 413）、`test_text_field_limit_is_enforced`（:189，`detail = "d" * (MAX_TEXT_FIELD + 10)`，断言 422） | 已修复 |

### 补充说明一（差异规模口径，与任务书给的数字不一致）

任务书写的「17 文件 +389/-142」在本仓库**实测不成立**，为免后来人拿它去对账，这里把三个可复现的口径全列出来：

1. `git diff --stat c6d663d HEAD -- bigfish tests tools` → **19 files changed, 826 insertions(+), 142 deletions(-)**。这是任务书的参照区间，但它混进了 rc5 收尾时就有的改动（`tools/bfc-proxy.mjs`、`tools/dev_setup.sh`、`tools/net_check.py`、`tools/remote_check.py`、`tests/test_v011_p3.py` 五个文件，详见第三节）。
2. 剔除上述 5 个文件后 → **14 files changed, 818 insertions(+), 134 deletions(-)**。
3. 相对 rc5 发布提交 `b8f6de1`（纯 rc6 口径）→ **13 files changed, 817 insertions(+), 133 deletions(-)**；其中 `bigfish/` 单独是 **10 files changed, 444 insertions(+), 133 deletions(-)**。

为什么不是同一条线：`c6d663d`（rc5 tag 点）在 `remotes/ghrc5` 上，**不是 HEAD 的祖先**（`git merge-base c6d663d HEAD` 为空，仓库是两条独立历史线），所以「相对 c6d663d」和「相对 b8f6de1」算出来的文件集不一样。本文正文的行号与用例数一律按 HEAD `ae2fe82` 核对。

本轮另加两个新文件，它们已经算在上面的 insertions 里，这里单列是因为按行数看更直观：`bigfish/locks.py` 157 行 / 5850 字节，`tests/test_v011_rc6.py` 280 行 / 10646 字节。

### 补充说明二（谁写的哪几条）

F-01~F-04 是 Astra 在 `cbf4d89`（`fix: harden rc5 restore and startup safety`）里自己改的，连同 `AUDIT_REVIEW.md` 122 行一起提交；本仓库 cherry-pick 后逐条复跑，四条都能复现「改前失败、改后通过」。`f1d1ce0`（`fix: surface key consistency startup failure cleanly`）只动 `bigfish/runtime.py` 两行，把 `RuntimeError` 换成 `CouncilError`，目的是让 CLI 的退出码路径能接住它。F-05~F-09 在 `ae2fe82` 里改，其中 F-06 是本轮最大的一处（新增模块 + 三个文件联动）。

### 补充说明三（F-06 的两处非显然细节）

第一处：`Database.__init__` 里租约必须在 `PRAGMA` 之前登记（`bigfish/db.py:238`），但 `_peek_schema_version()` 与三条 `PRAGMA` 又要整段包进 `try/except BaseException`（`bigfish/db.py:254-274`）。原因是「库版本高于程序版本」这条路径会把 `_conn` 关掉后 `raise DatabaseVersionError`，如果不在这里顺手 `self._lease.release()`，flock 句柄就留到 GC——实测表现是 `ResourceWarning: unclosed file '.../council.db.access.lock'`，而更坏的后果是**下一个进程会以为还有人在用，从而无法恢复这个已经打不开的库**。用例 `test_failed_constructor_does_not_leak_lease` 就是钉这一条的。

第二处：恢复流程自己是写者。`restore_bundle` 在抢 `exclusive_access` 之前必须 `db.pause_lease()`，否则会把自己锁在门外（自己那份 SH 还在，`LOCK_EX` 永远拿不到）。`finally` 里无条件 `db.resume_lease()`——**恢复失败也要夺回**，否则本实例退出「在用」名单，另一个进程就能在我们眼皮底下把库换掉。用例 `test_restore_keeps_lease_after_success` 钉的是成功路径，`test_restore_succeeds_when_no_other_writer` 钉的是「自己没把自己挡住」。

---

## 二、Astra 发布门槛 5 条完成情况

`AUDIT_REVIEW.md:112-120` 列的 5 条门槛，逐条对照：

| # | 门槛原文（摘要） | 完成情况 | 证据 / 缺口 |
| --- | --- | --- | --- |
| 1 | 在 Linux 与 Windows 分别运行完整测试；实际数量和结果以运行输出为准 | **完成（双平台）** | Linux：`Ran 265 tests` / `OK`，两次 46.926s 与 67.168s。Windows 11 / Python 3.13.11：`Ran 265 tests in 59.975s / OK (skipped=1)`，退出码 0；**首次跑是 `FAILED (errors=2, skipped=1)`**，两条 ERROR 引出缺陷 17/18（见第七节），修完才全绿。审计预期 249 项，实际落地 265 项（多出的 16 项是 rc6 自己的回归，见第五节） |
| 2 | 验证 F-01 两个并发回归、F-02 epoch=7 恢复旧快照得到 8、F-03 密钥自愈失败时拒绝启动、F-04 端口占用时数据库尚未打开 | **4/4 已完成** | F-01：`test_query_guard_is_atomic_with_lock`、`test_transaction_guard_is_atomic_with_lock` 通过（真线程 + Event 卡点）；F-02：`test_restore_epoch_is_monotonic_when_restoring_an_older_snapshot` 拿到的就是 8；F-03：`test_open_council_refuses_to_start_when_key_repair_fails` 断言拒绝启动且不再调 `load_or_create_key`；F-04：`test_busy_port_is_checked_before_database_open` 断言 `open_council` 未被调用 |
| 3 | 完成 Windows 真实 GUI 窗口验收，并验证 API/watchdog 停服超时路径 | **未做** | 停服超时路径只有**单测**：`test_stop_raises_when_thread_will_not_exit` 用一个自造的 `Stuck` 线程模拟卡死，`test_pause_is_not_set_when_stop_fails` 用 lambda 替换 `server.stop` 模拟失败——两者都不经过真实 Qt 事件循环与真实 uvicorn 线程。真实 GUI 窗口（有人在 Windows 上点「恢复备份」看服务是否真停、真起）本批次没有任何记录，`gui_smoke` 9 项是离屏构造级 |
| 4 | 完成至少一次跨设备 MCP 闭环回归（rc2 的 11/11 不能替代） | **未做** | 本轮所有证据都是单机（同进程或本机多进程）。没有在 Tailscale 两侧真机之间跑 claim → heartbeat → submit 全链路；rc2 的 11/11 按期不计入 |
| 5 | 明确跨进程 restore lock 的处理方案，或把它作为有明确限制的发布阻断项 | **已完成（方案落地 + 双平台实测）** | 由 F-06 落地：`bigfish/locks.py` 的写者租约 + 排他访问锁，替换掉原来只管「restore 对 restore」的 `_restore_lock()`。POSIX 用 `fcntl.flock`（`TestCrossProcessLease` 六项）；Windows `msvcrt` 分支已在真机跑通，并修出缺陷 17——`msvcrt.locking` 是**按文件句柄**记的，租约文件按 pid 命名，同进程第二个 `Database` 必 EACCES。修法：模块级 `_win_leases`（路径 -> [句柄, 引用计数]）。**仍未验**：两个真正独立的 Windows 进程同时恢复 |

一句总结：**5 条里 3 条完成（第 1、2、5 条），第 3、4 条整条未做。** 平台 B 的全绿是修完两个 Windows 专属缺陷换来的，不是一次干净的首跑。

---

## 三、本轮顺手修的

这两处不在 F-01~F-09 里，是改 rc6 时撞见并当场处理的噪音；列出来是因为它们改变了输出，不写会让后来人对不上日志。

| # | 问题 | 修改文件（行号） | 修法 | 验证 |
| --- | --- | --- | --- | --- |
| S1 | `tests/test_v011_rc5_cli_guard.py` 的 `accept_loop()` 在 `srv.close()` 之后才调 `srv.settimeout(0.2)`，退出时抛 `OSError: [Errno 9] Bad file descriptor` 并刷栈，污染整份全量日志 | `tests/test_v011_rc5_cli_guard.py:51-57` | 把 `settimeout` 提到循环**外**并包 `try/except OSError: return`，注释写明「settimeout 在 srv.close() 之后调用会 EINVAL/EBADF；先设好再进循环，并且把 OSError 一律当成『已经被关掉』正常退出（测试收尾不该刷栈）」 | 改后该文件单跑不再出现该栈；全量 265 项日志里已无这条 |
| S2 | `tests/test_v011_rc6.py` 初稿带了两处未用导入（`from bigfish import state as S`、`import os as _os`） | `tests/test_v011_rc6.py`（提交前已清，现文件不含这两行） | 直接删掉；文件里真正用到的 `import os`（:8，供 `TestGuiPauseConfirms.setUpClass` 设 `QT_QPA_PLATFORM`）保留 | `pyflakes bigfish tests tools` 零输出 / exit 0，含该文件 |

另有一处已并入 F-05 的收尾：`bigfish/gui.py:1248` 与 `:1349` 的 `self.server.started.disconnect(self.on_server)` 加了 `if self.server.receivers(self.server.started):` 前置判断，消掉重复断开时 PySide6 打的 `libpyside: Failed to disconnect` 警告。它不算独立条目，但确实是顺手消的噪音。

**仍然存在的噪音（未修，如实记录）**：全量日志里还剩两条——`StarletteDeprecationWarning`（来自 `fastapi.testclient` 对 `httpx` 的依赖，第三方库自身的问题，不是本仓库代码）和 1 条 `ResourceWarning: unclosed file <_io.BufferedRandom name='/tmp/tmpXXXX/council.db.access.lock'>`。后者已定位到 `tests/test_v011_rc5_cli_guard.py::TestCliRestoreGuard.test_force_bypasses_the_guard`（该文件单跑复现 1 次，其余 15 个测试文件单跑均为 0）：这个用例让 `cli.main --force` 走进 `open_council` → `Database` 打开租约 → 随后读包失败抛 `RestoreError`，用例本身没关 `db`，flock 句柄留到 GC。**这是测试收尾瑕疵，不是产品缺陷，本轮未修**——修它要动那个用例的 try/finally，超出「只碰 rc6 相关文件」的范围。

---

## 四、按文件汇总

| 文件 | 承担的项 | 关键落点 |
| --- | --- | --- |
| `bigfish/locks.py` | F-06（新增） | `:1-14` 模块 docstring（POSIX 共享锁 vs Windows 按 pid 分文件）、`:23` `LeaseError`、`:30` `_lock_file`、`:38` `WriterLease`（`:41` 构造即 `acquire`、`:46` `acquire`、`:66` `release`）、`:89` `_probe_other_writers`、`:112-157` `exclusive_access` |
| `bigfish/db.py` | F-01、F-06 | F-01：`:461`/`:467`/`:472` 三个访问器、`:477` `_Tx`（`:485` `__enter__`）；F-06：`:22` 导入、`:238` 租约、`:254-274` 构造失败释放、`:525` `close(release_lease=)`、`:549`/`:557` `pause_lease`/`resume_lease`；schema 常量在 `:24`/`:25`（未变） |
| `bigfish/backup.py` | F-02、F-06、F-07 | F-02：`:479-487`；F-06：`:42` 导入、`:484`、`:643`、`:651` `_translate_lease`，删除原 `_restore_lock()`（约 45 行）；F-07：`:183-189` `hits`、`:214-221` `credential_scan`、`:223-225` `handling` |
| `bigfish/security.py` | F-07 | `:36` 三元组、`:37-43` 原有六条补 kind、`:45-51` 新增五类、`:54` `SCRUB_KINDS`、`:78` `scrub`、`:88` `scan_secrets`、`:108` `contains_secret`（元组解包跟着改） |
| `bigfish/server.py` | F-08、F-09 | F-08：`:161` `create_app`、`:162-171` docstring；F-09：`:38-40` 两个上限、`:64-114` `BodyLimitMiddleware`、`:116` `_BodyTooLarge`、`:120-140` 三个模型 `max_length`、`:187` `add_middleware` |
| `bigfish/gui.py` | F-05 | `:165` `stop`、`:182` `is_running`、`:1235` `pause_services`（`:1248`/`:1349` `receivers` 判断、`:1263` 抛错）、`:1266` `resume_services` |
| `bigfish/cli.py` | F-04 | `:130-139` 新增 restore 前置分支、`:222` restore 分支只剩 `dump(...)`、`:41` `_port_busy` |
| `bigfish/runtime.py` | F-03 | `:6` 导入 `CouncilError`、`:18-21` fail-closed |
| `bigfish/roles.py` | 无（rc5 遗留） | `:21` `EXECUTOR_DSH` 文案「执行器（VPS DSH）」→「执行器（VPS DSH）」。这行在 `b8f6de1` 里已经是「VPS」，属于 rc5 收尾的文案改动，不是 rc6 逻辑改动 |
| `bigfish/__init__.py` | 版本号 | `:6` `__version__ = "0.1.1-rc6"` |
| `tests/test_v011_rc6.py` | F-05、F-06、F-07、F-09（新增，280 行） | 4 类 16 项，见第五节 |
| `tests/test_v011_rc5_restore_authority.py` | F-01、F-02 | `:199` epoch 单调、`:296` `_assert_guard_lock_order`、`:340`/`:343` 两项 |
| `tests/test_v011_rc5_cli_guard.py` | F-04、S1 | `:84` 新增用例、`:51-57` settimeout 修法 |
| `tests/test_v011_rc5_key_heal.py` | F-03 | `:139` `TestStartupFailsClosed`、`:140` 用例 |

---

## 五、用例分布与增量

本轮实测 `grep -c 'def test_'` 计数（共 265 项）：

`test_api` 6、`test_core` 15、`test_v011_p0` 16、`test_v011_p1` 24、`test_v011_p2` 24、`test_v011_p3` 20、`test_v011_rc3` 26、`test_v011_rc4` 33、`test_v011_rc5_cli_guard` 4、`test_v011_rc5_dispatch_id` 3、`test_v011_rc5_key_heal` 8、`test_v011_rc5_proxy` 9、`test_v011_rc5_receipts` 24、`test_v011_rc5_restore_authority` 24、`test_v011_rc5_review_audit` 13、`test_v011_rc6` 16。

244 → 265 的 +21 项拆开：

| 来源 | 项数 | 对应的审计条目 |
| --- | --- | --- |
| `tests/test_v011_rc6.py`（新文件，4 类） | 16 | `TestCrossProcessLease` 6（F-06）、`TestAuditManifestHonesty` 5（F-07）、`TestBodyLimit` 2（F-09）、`TestGuiPauseConfirms` 3（F-05） |
| `tests/test_v011_rc5_restore_authority.py` | +3 | F-01 两项 + F-02 一项（Astra 写，随 `cbf4d89` 并入） |
| `tests/test_v011_rc5_cli_guard.py` | +1 | F-04（Astra 写） |
| `tests/test_v011_rc5_key_heal.py` | +1 | F-03（Astra 写） |
| 合计 | **+21** | 244 → 265 |

F-08 没有对应用例（改的是契约文本，不是行为），所以 9 条里 8 条有测试。

---

## 六、未做的部分

列清楚是为了避免把「没做」读成「做了但没写」。

1. **平台 B 只跑了全量 265 项，`selfcheck` / `gui_smoke` / `pyflakes` 没跑。** Windows 11 / Python 3.13.11 / SQLite 3.50.4 上 `Ran 265 tests in 59.975s / OK (skipped=1)`、退出码 0；`compileall` 退出码 0。但自检、GUI 冒烟、静态检查三条只有 Linux 的记录。
2. **平台 B 首次跑不是全绿，是两个真缺陷（已修）。** 第一次 `FAILED (errors=2, skipped=1)`，两条 ERROR 都是 `bigfish.locks.LeaseError: … [Errno 13] Permission denied`：
   - **缺陷 17（P1）**：`msvcrt.locking(LK_NBLCK)` 的字节区间锁**按文件句柄**记，租约文件按 pid 命名，同进程第二个 `Database` 用第二个句柄锁同一区间必 EACCES → 被误判成「有别人在用这个库」。Linux 的 `flock` 对同一文件多句柄各自 `LOCK_SH` 合法，所以这条路径从没被真机碰到。修法：模块级 `_win_leases`（路径 -> [句柄, 引用计数]），同进程复用句柄、计数归零才 `LK_UNLCK` + `close` + `unlink`。POSIX 分支一行未改。
   - **缺陷 18（P2）**：`bigfish/cli.py` 的 restore 分支恢复完直接 `return 0` 不关库。Linux 上进程退出即释放；Windows 上进程未退出前文件仍被占用，调用方紧接着删除报 `PermissionError: [WinError 32]`（测试 `tearDown` 的 `tmp.cleanup()` 就挂在这里）。修法：restore 分支包 `try/finally` 关库。
   两条修完提交 `73104a8`（2 files / +81 / -21），才拿到 265 全绿。
3. **GUI 真窗口没有验收。** F-05 的三项断言全是单测，`gui_smoke` 9 项是离屏构造级（主窗口能建、五个页面能切、状态栏文案）。没有人真的在真窗口点一次「恢复备份」，看 API 线程是否真停、watchdog 是否真退、失败时是否真接回来。
4. **跨设备 MCP 闭环没有做。** 没有在 Tailscale 两侧真机之间跑 claim → heartbeat → submit。
5. **`--force` 路径的租约收尾瑕疵未复测。** `test_force_bypasses_the_guard` 那条 `ResourceWarning` 在修 `73104a8` 之前还在（第三节末尾）。`73104a8` 正好给 restore 分支补了 `finally: db.close()`，很可能顺带消掉，但**没有单独复测，不算修好**。
6. **Windows 上两个真正独立的进程同时恢复仍未验。** `TestCrossProcessLease` 里「另一个进程」是同进程再开一个 `Database`；对 `flock` 有效（按 fd 算），但 Windows 的 `msvcrt` 分支是按 pid 分文件，真·双进程的行为没有覆盖。
5. **F-08 之后 `create_app` 仍然可以被第三方直接 bind。** 本轮只把边界写进 docstring，没有加运行时断言。要真封死需要引入一个「必须经由 `serve()` 才能构造的 app 工厂」的改造，会连带影响 `TestClient` 与 GUI 内嵌，本批次没有做。
6. **审计包默认仍然导出 `task_versions.prompt` 正文。** 审计建议「考虑默认不导出 prompt 正文或提供显式 opt-in」，本轮只做了如实声明（`fields_scanned` 里点名含 prompt 正文 + `manual_review_required: True`），没有改成默认不导出——那是一次导出面的语义变更，需要单独讨论。
7. 没有改生产 DSH、没有开任何对外端口、没有打 tag、没有 push。

---

## 七、复现与复核命令

```bash
cd /home/dsh/bigfish-council

# 全量 265 项
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

按审计项定位测试：

- F-01 → `tests/test_v011_rc5_restore_authority.py::test_query_guard_is_atomic_with_lock`、`::test_transaction_guard_is_atomic_with_lock`
- F-02 → 同文件 `::test_restore_epoch_is_monotonic_when_restoring_an_older_snapshot`
- F-03 → `tests/test_v011_rc5_key_heal.py::test_open_council_refuses_to_start_when_key_repair_fails`
- F-04 → `tests/test_v011_rc5_cli_guard.py::test_busy_port_is_checked_before_database_open`
- F-05 → `tests/test_v011_rc6.py::TestGuiPauseConfirms`（3 项）
- F-06 → `tests/test_v011_rc6.py::TestCrossProcessLease`（6 项）
- F-07 → `tests/test_v011_rc6.py::TestAuditManifestHonesty`（5 项）
- F-08 → 无专用用例，护栏见 `tests/test_v011_rc3.py::test_all_entry_points_reject_public_bind`
- F-09 → `tests/test_v011_rc6.py::TestBodyLimit`（2 项）

单跑某一类：

```bash
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest tests.test_v011_rc6 -v
```
