# 测试报告：bigfish-council v0.1.1-rc6

## 0. 一句话

平台 A（Debian 12 / Python 3.11.2）在 HEAD `73104a8` 上实测 **Ran 265 tests / OK / 退出码 0**，
16 个测试文件、265 项全部通过；selfcheck 16/16、gui_smoke 9/9、pyflakes 零输出、compileall 退出码 0。
平台 B（Windows 11 / Python 3.13.11 / SQLite 3.50.4）**已跑通**：`Ran 265 tests in 59.975s / OK (skipped=1)`、退出码 0。这次全绿是修完两个 Windows 专属真缺陷之后的结果——首次跑是 `FAILED (errors=2, skipped=1)`，详见第 6.1 节与 6.1.1 / 6.1.2 两个缺陷。

相对 rc5 发布点 `b8f6de1`（244 项 / 15 个文件），本轮 +21 项 / +1 个文件：
新文件 `tests/test_v011_rc6.py` 贡献 16 项，三个既有 rc5 测试文件各补 1～3 项共 5 项。

耗时口径要说清：发布口径记的是 `Ran 265 tests in 41.728s`（维护者记录，见 `/tmp/rc6-facts.md` 第 12 行）；
本机这段时间负载偏高，四次复跑落在 50.226s～74.674s 之间，四次都是 `OK` / 退出码 0。
**项数是硬结论，秒数只是当时那台机器的负载快照。**

---

## 1. 环境

### 1.1 平台 A（本轮实测）

    OS        Debian GNU/Linux 12 (bookworm)，kernel 5.15.0-187-generic
    平台串    Linux-5.15.0-187-generic-x86_64-with-glibc2.36
    主机名    VPS-HOST
    Python    3.11.2（/home/dsh/bigfish-council/.venv）
    SQLite    3.40.1
    Node      v22.23.2
    pyflakes  4.1.0（Python 3.11.2 on Linux）
    fastapi   0.163.2 / starlette 1.10.0 / pydantic 2.14.3
    版本      bigfish.__version__ = "0.1.1-rc6"（bigfish/__init__.py:6）
    库版本    SCHEMA_VERSION = 4（bigfish/db.py:24）、BACKUP_FORMAT_VERSION = 4（bigfish/db.py:25）
    分支      fix/v0.1.1-rc4-hardening
    HEAD      ae2fe82（2026-11-20 15:48:11 +0000）
    基线      rc5 发布提交 b8f6de1（本报告的用例增量以它为起点）
    工作区    git status --short -- bigfish tests tools 为空（代码与测试均已提交）
    GUI 依赖  LD_LIBRARY_PATH=/home/dsh/.local/libbfc、QT_QPA_PLATFORM=offscreen

本轮三个提交（全部在 HEAD 之前，工作区干净）：

    cbf4d89  2026-11-20 15:30:19 +0000  fix: harden rc5 restore and startup safety（Astra）
    f1d1ce0  2026-11-20 15:30:19 +0000  fix: surface key consistency startup failure cleanly（Astra）
    ae2fe82  2026-11-20 15:48:11 +0000  fix(rc6): 处理 Astra 审计 F-01~F-09 …

`cbf4d89` 与 `f1d1ce0` 是 Astra 在 `review/rc5-hardening-20261120` 分支上写的，
cherry-pick 进来后由本轮验证；`ae2fe82` 是本轮主体（F-05～F-09 + 版本号 + 两处测试噪音 + 顺手修）。

相对 rc5 发布点的代码与测试改动（只算 bigfish/ tests/ tools/ 三个目录）：

    git diff --stat b8f6de1 73104a8 -- bigfish tests tools
    13 files changed, 817 insertions(+), 133 deletions(-)

    bigfish/__init__.py                      +1   -1    （版本号）
    bigfish/backup.py                       +52  -55
    bigfish/cli.py                          +12  -10
    bigfish/db.py                           +67  -32
    bigfish/gui.py                          +33   -9
    bigfish/locks.py                       +157   -0    （新文件，157 行）
    bigfish/runtime.py                       +6   -5
    bigfish/security.py                     +30   -9
    bigfish/server.py                       +85  -11
    tests/test_v011_rc5_cli_guard.py        +15   -1
    tests/test_v011_rc5_key_heal.py         +15   -0
    tests/test_v011_rc5_restore_authority.py +64  -0
    tests/test_v011_rc6.py                 +280   -0    （新文件，280 行）

### 1.2 平台 B（Windows 11 / Python 3.13.11 / SQLite 3.50.4）

**已执行，265 项全绿。** 在 `D:\BigFishCouncil\bigfish-council-rc6`（Windows 11 / Python 3.13.11 / SQLite 3.50.4 / PySide6 6.12.0）上：

```
Ran 265 tests in 59.975s
OK (skipped=1)
UT_RC=0
```

唯一 skip 的是 `test_v011_p3.py::test_graceful_shutdown_on_sigint`（`skipIf(os.name == "nt")`，Windows 无可捕获 SIGTERM）。

**但首次跑不是这样。** 第一次跑是 `FAILED (errors=2, skipped=1)`，两条 ERROR 全在租约上：
`test_v011_rc5_cli_guard.TestCliRestoreGuard.test_force_bypasses_the_guard` 与
`test_v011_rc6.TestCrossProcessLease.test_second_database_on_same_file_blocks_restore`，
同样一条 `bigfish.locks.LeaseError: 无法登记数据库写者租约（…council.db.access.<pid>.lock）：[Errno 13] Permission denied`。
这不是测试写错，是两个真缺陷（见 6.1.1 / 6.1.2）。

---

## 2. 测试总量表

### 2.1 总数演进

| 版本 | 全量项数 | 文件数 | 出处（可核对） |
| --- | --- | --- | --- |
| v0.1.0 | 21 | — | `docs/TESTLOG.md:11` `Ran 21 tests ... OK`、`:19` `Ran 21 tests in 4.043s` |
| v0.1.1-rc3 | 129 | — | `docs/TEST-REPORT-v0.1.1-rc3.md:29` `Ran 129 tests in 15.984s` |
| v0.1.1-rc4 | 162 | 8 | `docs/TEST-REPORT-v0.1.1-rc4.md:5` `Ran 162 tests / OK` |
| v0.1.1-rc5 | 244 | 15 | `docs/TEST-REPORT-v0.1.1-rc5.md:5` `Ran 244 tests / OK` |
| **v0.1.1-rc6** | **265** | **16** | 本报告 4.1 节，HEAD `ae2fe82` |

涨落都对得上，没有跳变：21 → 129 → 162 → 244 → 265。

### 2.2 逐文件

| 测试文件 | 本轮 | rc5（b8f6de1） | 本轮单跑耗时 |
| --- | --- | --- | --- |
| tests/test_api.py | 6 | 6 | 0.829s |
| tests/test_core.py | 15 | 15 | 3.755s |
| tests/test_v011_p0.py | 16 | 16 | 0.769s |
| tests/test_v011_p1.py | 24 | 24 | 7.568s |
| tests/test_v011_p2.py | 24 | 24 | 1.400s |
| tests/test_v011_p3.py | 20 | 20 | 19.289s |
| tests/test_v011_rc3.py | 26 | 26 | 5.428s |
| tests/test_v011_rc4.py | 33 | 33 | 1.588s |
| tests/test_v011_rc5_cli_guard.py | 4 | 3 | 0.280s |
| tests/test_v011_rc5_dispatch_id.py | 3 | 3 | 0.132s |
| tests/test_v011_rc5_key_heal.py | 8 | 7 | 0.386s |
| tests/test_v011_rc5_proxy.py | 9 | 9 | 14.365s |
| tests/test_v011_rc5_receipts.py | 24 | 24 | 2.752s |
| tests/test_v011_rc5_restore_authority.py | 24 | 21 | 2.478s |
| tests/test_v011_rc5_review_audit.py | 13 | 13 | 0.575s |
| tests/test_v011_rc6.py | **16** | 0 | 6.472s |
| **合计** | **265** | **244** | 全量 73.287s |

单文件耗时是逐个 `python -m unittest discover -s tests -p <文件名> -t .` 单跑得到的（日志 `perfile.txt`），
16 次全部退出码 0。

这轮单跑加总 68.066s，反而比全量 73.287s **少** 约 5.2 秒，和 rc5 报告里「加总大于全量」的结论相反。
原因是全量里 `test_v011_p3.py`（后台进程 + 健康轮询）与 `test_v011_rc5_proxy.py`（起 node）串行叠加，
等待时间在同一进程里被拉长；单跑时每次都是全新进程、没有前面用例留下的负载。
所以这张表的耗时列只用来看「哪个文件慢」，不要拿来做加减法对账，项数才是硬数字。

### 2.3 增量口径把账走平

    rc5 全量                                             244 项 / 15 个文件
    新增 tests/test_v011_rc6.py                          +16 项（4 个类）
    tests/test_v011_rc5_restore_authority.py  21 → 24    +3 项（Astra，F-01 两项 + F-02 一项）
    tests/test_v011_rc5_cli_guard.py           3 → 4     +1 项（Astra，F-04）
    tests/test_v011_rc5_key_heal.py            7 → 8     +1 项（Astra，F-03）
    ----------------------------------------------------------------
    244 + 16 + 3 + 1 + 1 = 265 ✓

`tests/test_v011_rc6.py` 的 16 项按类拆：`TestCrossProcessLease` 6、`TestAuditManifestHonesty` 5、
`TestBodyLimit` 2、`TestGuiPauseConfirms` 3，合计 16。

三个既有文件的 rc5 旧值不是照抄 TABLE，而是直接在 rc5 提交上数出来的：

    git show b8f6de1:tests/test_v011_rc5_restore_authority.py | grep -c '^    def test_'   # 21
    git show b8f6de1:tests/test_v011_rc5_cli_guard.py         | grep -c '^    def test_'   # 3
    git show b8f6de1:tests/test_v011_rc5_key_heal.py          | grep -c '^    def test_'   # 7

### 2.4 计数自洽性

除了看 unittest 打印的 `Ran 265 tests`，另用 `TestLoader().discover` 把用例对象独立数了一遍：

    .venv/bin/python -c "
    import unittest
    s = unittest.TestLoader().discover('tests', top_level_dir='.')
    f = lambda x: sum(f(t) for t in x) if isinstance(x, unittest.TestSuite) else 1
    print('TOTAL', f(s))"
    TOTAL 265

再用文件级口径交叉核一次：

    for f in tests/test_*.py; do grep -c '^    def test_' $f; done   # 16 个文件依次求和 = 265

三条路径（unittest 运行结果、TestLoader 递归、逐文件 grep）互相独立，落在同一个数上。

---

## 3. 本轮新增 21 项：逐条

写法照 rc5：每项写「测什么 / 为什么这样断言」，行号都用 `grep -n` 在 HEAD 上核对过。

### 3.1 tests/test_v011_rc6.py（16 项，280 行）

文件头注释（`:1-4`）写的是「对应审计里的 F-05 / F-06 / F-07 / F-09」。
F-08 没有专用用例（它是一条契约文本），所以这个文件只覆盖四条。

#### 3.1.1 TestCrossProcessLease（6 项，`:60`，F-06）

**测什么。** 「谁在用这个数据库」不再是靠探端口猜，而是每个 `Database` 打开时在
`bigfish/locks.py` 的 `<db>.access.lock` 上登记一把共享锁（POSIX `fcntl.flock LOCK_SH`，
`bigfish/locks.py:59`），恢复流程抢排他锁（`:136` `LOCK_EX | LOCK_NB`）。这 6 项就是在测这条链的六种出口。

| # | 用例 | 行号 | 断言的是什么 |
| --- | --- | --- | --- |
| 1 | test_failed_constructor_does_not_leak_lease | :61 | 库版本过高抛 `DatabaseVersionError` 之后，**仍然拿得到排他锁**——否则构造函数失败会留下一个永久占住锁的句柄 |
| 2 | test_second_database_on_same_file_blocks_restore | :78 | 同文件再开一个 `Database`（模拟另一个进程）后，恢复必须抛 `RestoreError` |
| 3 | test_restore_succeeds_when_no_other_writer | :89 | 没有别的写者时恢复必须真的成功，`out["restored"]` 为真 |
| 4 | test_lease_is_released_on_close | :96 | `close()` 之后能直接抢到排他锁 |
| 5 | test_exclusive_access_fails_while_lease_held | :106 | 只持有一个 `WriterLease`（连库都没打开）也足以挡住恢复 |
| 6 | test_restore_keeps_lease_after_success | :117 | 恢复成功后**必须把写者租约抢回来**，否则别的进程能在我们眼皮底下换库 |

**为什么这样断言。** 第 2 和第 6 项是一对反向约束，缺一条就会漏掉半个 bug：
只有第 2 项，把租约写成「一旦释放就不再加回」也能全绿（恢复完成后再也挡不住别人）；
只有第 6 项，把租约写成「永远不放」也能全绿（`close()` 之后还占着锁）。
两条同时成立，才逼出正确的 `pause_lease()` / `resume_lease()` 编排
（`bigfish/db.py:549` / `:557`，调用点在 `bigfish/backup.py:483` 与 `:643`）。

第 1 项对应的是一个真踩过的坑：租约在 `Database.__init__` 里登记（`bigfish/db.py:238`，
在三条 PRAGMA 之前），如果后面版本校验抛错而不释放，flock 句柄会跟着对象一起等 GC，
全量日志里就出现 `ResourceWarning: unclosed file '.../council.db.access.lock'`。
现在构造函数把 `_peek_schema_version()` + `DatabaseVersionError` + 三条 PRAGMA 整段包进
`try/except BaseException: self._lease.release(); raise`（`bigfish/db.py:254` 的 `try` 到 `:274` 的 `raise`，释放动作在 `:271`）。

**最关键的一条断言。** `tests/test_v011_rc6.py:117-124`：

    def test_restore_keeps_lease_after_success(self):
        bundle = self.root / "b.zip"
        export_bundle(self.db, bundle)
        self.c.restore_from_bundle(bundle)
        with self.assertRaises(LeaseError):
            with exclusive_access(self.db.path):
                pass

第 2 项的匹配文案也是刻意放宽成两种出口，避免把断言钉死在单一路径上：

    with self.assertRaisesRegex(RestoreError, "还有进程在用这个数据库|正在恢复"):

#### 3.1.2 TestAuditManifestHonesty（5 项，`:130`，F-07）

**测什么。** 审计包的 manifest 原来写死「无明文凭据」，但白名单里含 `task_versions.prompt` 这种自由文本，
脱敏只覆盖已知正则。改法是把声明本身改成如实的：`credential_scan` 写清 mode / patterns / fields_scanned /
coverage / hits，并强制 `manual_review_required: True`（`bigfish/backup.py:214-222`），
`handling` 改成「可外发（需人工复核）……脱敏不等于证明」（`:223-225`）。

| # | 用例 | 行号 | 断言的是什么 |
| --- | --- | --- | --- |
| 7 | test_manifest_declares_manual_review_required | :137 | `mode == "pattern-based"`、`manual_review_required` 为真、`coverage` 里含「无法保证」、`patterns` 里含 `google-key` |
| 8 | test_manifest_does_not_claim_guarantee | :145 | `handling` **不含**「无凭据」、**含**「复核」 |
| 9 | test_new_patterns_scrub_google_slack_stripe | :150 | 三类新凭据样本（`AIza`+35、`xoxb-`、`sk_live_`）都必须被改写 |
| 10 | test_uri_userinfo_is_scrubbed | :154 | `postgres://user:hunter2@db.local/x` 里的口令不许留在输出里 |
| 11 | test_scan_secrets_reports_kinds | :158 | `scan_secrets` 对 Bearer 样本报出 `bearer` 这个种类名 |

**为什么这样断言。** 第 7、8 两项断的是**文案**，看起来最「虚」，但它们恰好是这条审计项的实质：
F-07 的问题不是「脱敏漏了某种密钥」（那永远补不完），而是**声明超出了能力**。
所以断言必须落在「声明里有没有承认自己扫不全」上：`assertIn("无法保证", scan["coverage"])`
和 `assertNotIn("无凭据", m["handling"])` 这两条，改回旧文案立刻红。

第 9、10 两项断的是能力，不是文案：`assertNotEqual(SEC.scrub(sample), sample)` 只要求「被改写过」，
不要求改成某个特定字符串——这样改脱敏替换词（`[REDACTED GOOGLE KEY]` 之类）不会误红。

第 11 项是给 `scan_secrets` 的返回值立契约：`bigfish/security.py:88` 返回的是**种类名列表**（去重排序），
不是布尔、也不是命中位置。manifest 里的 `hits` 就是靠它统计的。

**最关键的一条断言。** `tests/test_v011_rc6.py:145-148`：

    def test_manifest_does_not_claim_guarantee(self):
        m = self._manifest()
        self.assertNotIn("无凭据", m["handling"])
        self.assertIn("复核", m["handling"])

这条是整个 F-07 的验收点：只要有人为了「让报告好看」把 handling 改回「导出包无明文凭据」，
它立刻失败。

#### 3.1.3 TestBodyLimit（2 项，`:166`，F-09）

**测什么。** 两个上限分别在两层：`MAX_BODY_BYTES = 8 MiB`（`bigfish/server.py:38`，
`BFC_MAX_BODY_BYTES` 可覆盖）挡整个请求体；`MAX_TEXT_FIELD = 4 MiB`（`:40`，`BFC_MAX_TEXT_FIELD` 可覆盖）
挡单个自由文本字段（`log` / `diff` / `tests` / `detail` / `note`，`:128`、`:136-139`）。

| # | 用例 | 行号 | 断言的是什么 |
| --- | --- | --- | --- |
| 12 | test_declared_oversize_body_is_rejected | :183 | `content-length: 64 MiB` 但实际只发 10 字节 → 必须 **413** |
| 13 | test_text_field_limit_is_enforced | :189 | `detail` 长度 `MAX_TEXT_FIELD + 10` → 必须 **422** |

**为什么这样断言。** 第 12 项故意让声明和实际不一致：`content=b"x"*10` 配一个 64 MiB 的
`content-length`。如果实现是「先读完再检查」，这条会等到超时或者根本读不完；
现在 `BodyLimitMiddleware`（`bigfish/server.py:64`）在进应用之前就看头，超了直接回 413
（`bigfish/server.py:87-89`），连 `receive()` 都不调。所以「10 字节的请求拿到 413」这件事，
证明的是**判断发生在读取之前**，不是「服务器能处理大包」。

第 13 项反过来只断 422，不断文案：pydantic 的 `max_length` 报错结构属于第三方实现细节，
钉死它会让升级 pydantic 变成一次错误的红。

**最关键的断言对。** `tests/test_v011_rc6.py:183-196`：

    r = self.client.post("/api/v1/tasks/claim",
                         content=b"x" * 10,
                         headers={"content-length": str(64 * 1024 * 1024)})
    self.assertEqual(r.status_code, 413)
    ...
    body = {"ticket": "nope", "status": "running",
            "detail": "d" * (MAX_TEXT_FIELD + 10)}
    r = self.client.post("/api/v1/runs/heartbeat", json=body,
                         headers={"authorization": "Bearer " + ex["token"]})
    self.assertEqual(r.status_code, 422)

一个 413 一个 422，把「体上限」和「字段上限」分成两条独立证据，
不会出现「某一个上限生效了，另一个其实没装」还全绿的情况。

#### 3.1.4 TestGuiPauseConfirms（3 项，`:202`，F-05）

**测什么。** GUI 停服这一个动作，原来有两个缺陷叠在一起：`ApiServer.stop()` join 超时后不检查线程死活，
`pause_services()` 又先置 `_services_paused` 再停服。结果就是「服务还活着，界面认为已经停了」，
接着恢复流程会在 API 线程还在写库的时候换掉数据库文件。

| # | 用例 | 行号 | 断言的是什么 |
| --- | --- | --- | --- |
| 14 | test_stop_raises_when_thread_will_not_exit | :233 | 线程卡住时 `stop(timeout=0.05)` 必须抛 `TimeoutError` |
| 15 | test_pause_services_stops_api_and_watchdog | :250 | 停服成功后 `_services_paused` 为真、`server.is_running()` 为假、看门狗线程已退出 |
| 16 | test_pause_is_not_set_when_stop_fails | :263 | `server.stop` 抛错时 `_services_paused` **仍为假**，且抛出含「已中止」的 `RuntimeError` |

**为什么这样断言。** 第 14 项没有用 mock 顶替线程，而是自己造了一个只有 `sleep(3)` 的
`class Stuck(threading.Thread)`，塞进 `srv.thread`。用 mock 顶替的话，测的就成了
「mock 会不会抛异常」；用真线程才能证明 `stop()` 确实 join 过、确实检查了 `is_alive()`
（`bigfish/gui.py:174-179`）。

第 15 项同时断三件事（置位、API 线程、看门狗），因为 `pause_services()`（`:1235`）的语义是
「两个服务都停干净」，只断置位会漏掉「置位了但看门狗还在扫库」。

第 16 项是这一组里最关键的：它是唯一能证明「先停成功、再置位」这个顺序的用例。
如果实现退回「先置位后停服」，第 15 项照样全绿（停服成功的路径看不出差别），只有第 16 项会红。

**最关键的一条断言。** `tests/test_v011_rc6.py:263-271`：

    win.server.stop = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟停不下来"))
    with self.assertRaisesRegex(RuntimeError, "已中止"):
        win.pause_services()
    self.assertFalse(win._services_paused, "停服失败却置了 paused")

**这里有一个必须写明的坑：这 3 项在缺 `LD_LIBRARY_PATH` 时会整类跳过，而不是失败。**
`setUpClass`（`:204-209`）在导入 PySide6 失败时 `raise unittest.SkipTest(...)`。实测：

    .venv/bin/python -m unittest tests.test_v011_rc6
    Ran 13 tests in 1.959s
    OK (skipped=1)

    跳过原因：libGL.so.1: cannot open shared object file

    LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
      .venv/bin/python -m unittest tests.test_v011_rc6
    Ran 16 tests in 6.252s
    OK

第二条才是有效跑法。**少 3 项还报 OK**，这种「安静的覆盖缺口」必须靠比对项数发现，
不能只看最后那个 OK。全量跑法同理——必须带上 `LD_LIBRARY_PATH`，否则那 3 项会被静默跳过
（全量在本环境下的这种退化跑法本轮未单独复核，只核到了上面这个 13 项 + 1 skip 的单文件结果）。

### 3.2 三个既有 rc5 文件里的 5 项（Astra 写，本轮验证）

#### 3.2.1 tests/test_v011_rc5_restore_authority.py（21 → 24，+3）

| # | 用例 | 行号 | 对应审计项 | 断言的是什么 |
| --- | --- | --- | --- | --- |
| 17 | test_restore_epoch_is_monotonic_when_restoring_an_older_snapshot | :199 | F-02 | 活库先把 `meta.restore_epoch` 抬到 7，恢复一个旧快照后 `out["restore_epoch"]` 与 `db.restore_epoch()` 都必须是 **8** |
| 18 | test_query_guard_is_atomic_with_lock | :340 | F-01 | 操作已过 `_guard()`、还没拿到 `_lock` 时，维护模式不许插进来 |
| 19 | test_transaction_guard_is_atomic_with_lock | :343 | F-01 | 同上，作用在 `_Tx.__enter__` 路径上 |

**为什么这样断言。** 第 17 项的写法是「先抬高再回退」：如果只恢复一个 epoch=0 的包，得到 1 也算通过，
看不出单调性；先手动设成 7，正确实现必须给 8，被吞成 0 的实现会给出 1（`max(0, 0) + 1`），一眼分得出。

第 18、19 两项共用 `:296` 的 `_assert_guard_lock_order`：把 `db._guard` 换成会卡在
`threading.Event` 上的版本，等工人线程到达检查点，再在主线程 `enter_maintenance(timeout=0.05)`。
**必须抛 `MaintenanceError`** 才算通过——如果维护模式能成功进入，说明检查与加锁不在同一临界区。
收尾还断 `worker_errors == []`，确保工人线程不是被异常打断才结束的。

关键断言（`:335-343`）：

    self.assertFalse(
        entered_maintenance_before_operation,
        "维护模式在操作已通过 guard、尚未取得锁时插入，说明检查与加锁不是原子的")
    self.assertEqual(worker_errors, [])

第 19 项的作用是把 `:485` 的 `_Tx.__enter__` 也钉住。改动前 `query/one/execute` 是
「先 `_guard()` 再 `with self._lock`」，事务路径同样有窗口；只修前者、
`_Tx.__enter__` 不改，第 19 项会红（`bigfish/db.py:485-501` 现在是先
`self._lock.acquire()` 再 `self._guard()`，异常时 `release()` 后 re-raise）。

#### 3.2.2 tests/test_v011_rc5_cli_guard.py（3 → 4，+1）

| # | 用例 | 行号 | 对应审计项 | 断言的是什么 |
| --- | --- | --- | --- | --- |
| 20 | test_busy_port_is_checked_before_database_open | :84 | F-04 | 端口被占时 `cli.main(...)` 返回 2，且 `open_council` **一次都没被调用** |

**为什么这样断言。** 端口被占时返回 2 这件事，改动前也是成立的——问题在于**顺序**：
旧实现先 `build(a.data_dir)`（会执行 schema 迁移、密钥自愈），然后才发现端口占用。
所以真正的证据是 `open_db.assert_not_called()`，而不是返回码：

    with patch("bigfish.cli._port_busy", return_value=True), \
         patch("bigfish.cli.open_council") as open_db:
        rc = cli.main(["--data-dir", self.dir, "restore",
                       str(Path(self.dir) / "x.zip")])
    self.assertEqual(rc, 2)
    open_db.assert_not_called()

两个 patch 缺一不可：不 patch `_port_busy`，测试要靠真去占端口（不稳定）；
不 patch `open_council`，就没法证明「库没被打开」这件事——只能证明「没崩」。
对应的实现在 `bigfish/cli.py:130-139`：`restore` 分支先 `Settings.load` + `_port_busy`，
被占用时打印中文提示并 `return 2`，通过预检才 `db, council = open_council(s)`（`:139`）。

#### 3.2.3 tests/test_v011_rc5_key_heal.py（7 → 8，+1）

| # | 用例 | 行号 | 对应审计项 | 断言的是什么 |
| --- | --- | --- | --- | --- |
| 21 | test_open_council_refuses_to_start_when_key_repair_fails | :140（类在 :139） | F-03 | 密钥自愈失败时 `open_council` 抛含「密钥一致性校验失败」的异常，且 `load_or_create_key` **未被调用** |

**为什么这样断言。** F-03 原来是 `except Exception: pass`——自愈失败就带着未核验的旧密钥继续启动。
修法（`bigfish/runtime.py:17-21`）是先 `db.close()` 再抛 `CouncilError`，注释写的是
「B2 自愈是安全边界：密钥指纹不一致且无法修复时必须拒绝启动」。

只断「抛了异常」不够：如果实现改成「捕获异常、换个新密钥继续跑」，异常也会没，
所以要补 `load_key.assert_not_called()`——它证明流程在密钥加载之前就停了，
不是「换了个密钥继续走」。

    with patch("bigfish.runtime.ensure_key_consistency",
               side_effect=OSError("模拟密钥修复失败")):
        with patch("bigfish.runtime.load_or_create_key") as load_key:
            with self.assertRaisesRegex(RuntimeError, "密钥一致性校验失败"):
                open_council(settings)
            load_key.assert_not_called()

顺带说明 `assertRaisesRegex(RuntimeError, ...)`：`CouncilError` 继承自 `RuntimeError`，
用父类断言是为了不把测试钉死在某个具体异常类上——`f1d1ce0` 就是把这里从 `RuntimeError`
收到 `CouncilError` 的（CLI 的退出码 2 路径要接得住）。

---

## 4. 真实命令与输出

### 4.1 全量单元测试

    cd /home/dsh/bigfish-council
    LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
      .venv/bin/python -m unittest discover -s tests -t .

发布口径（维护者记录，`/tmp/rc6-facts.md` 第 11-12 行）：

    Ran 265 tests in 41.728s

    OK

本机本轮复跑四次，都是 `OK` / 退出码 0：

    Ran 265 tests in 50.226s     # 本轮第一次全量
    Ran 265 tests in 52.690s     # -v 版，265 行用例名，日志 36847 字节
    Ran 265 tests in 73.287s     # unittest-linux.txt（1381 字节）
    Ran 265 tests in 74.674s     # unittest-doc-run.txt（1381 字节）

`-v` 版逐行统计：265 行以 `... ok` 结尾，`skipped` 只出现在用例名
`test_bad_line_is_skipped_not_fatal` 里，本进程 0 个 skip、0 个 expected failure。
也就是说 265 项在平台 A 上是**全部真跑过**，不是 261 项跑 + 4 条跳过凑数。

`unittest-linux.txt` 全文 14 行（点号行为排版做了截断，原文件每条都一整行）：

    /home/dsh/bigfish-council/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1:
      StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated;
      install `httpx2` instead.
      from starlette.testclient import TestClient as TestClient  # noqa
    ...........Fontconfig error: Cannot load default config file: No such file: (null)
    This plugin does not support propagateSizeHints()
    ...........拒绝恢复：127.0.0.1:3096 上还有服务在监听，请先停掉它（tools/stop-headless.ps1
      或关闭 GUI），确认没有其他实例后再重试；确实要强行恢复请加 --force。
    ./usr/lib/python3.11/traceback.py:240: ResourceWarning: unclosed file
      <_io.BufferedRandom name='/tmp/tmp7dtw6grl/council.db.access.lock'>
      tb.tb_frame.clear()
    ResourceWarning: Enable tracemalloc to get the object allocation traceback
    ..拒绝恢复：127.0.0.1:36403 上还有服务在监听，请先停掉它（…同上…）
    ..............................
    ----------------------------------------------------------------------
    Ran 265 tests in 73.287s
    OK

逐行说清哪些是噪音、哪些是证据：

- 两行 `StarletteDeprecationWarning`：starlette 对 `httpx` 的弃用提示，环境噪音，与用例无关。
- `Fontconfig error` 一行：容器里没有 fontconfig 配置，offscreen 平台加载字体时的提示，噪音。
- `This plugin does not support propagateSizeHints()`：offscreen 平台插件的固定提示，噪音。
- 两行「拒绝恢复：…」：来自 3.2.2 那条 CLI 守卫用例的**预期输出**。用例在进程内调 `cli.main()`，
  打印走继承来的 stderr，所以混进全量日志。它出现恰好说明守卫分支真的被走到了；
  两条的端口不同（3096 / 36403），因为来自两个不同用例：`test_busy_port_is_checked_before_database_open`
  把 `_port_busy` 打成恒真，走的是配置默认端口 3096（`bigfish/config.py:22 DEFAULT_API_PORT = 3096`）；
  `test_refuses_when_api_port_is_busy` 真起了 socket 并设 `BIGFISH_API_PORT`，报的是那个随手挑的空闲端口。
- `ResourceWarning: unclosed file <_io.BufferedRandom name='.../council.db.access.lock'>` 三行：
  **这条不是噪音，是已知的测试侧句柄提醒**，溯源与结论见 4.5 节。

### 4.2 逐文件单跑

用 `discover -p <文件名>` 逐文件跑，16 个文件全部退出码 0（日志 `perfile.txt`）：

    .venv/bin/python -m unittest discover -s tests -p test_v011_rc6.py -t .

    test_api.py                    Ran 6 tests  in 0.829s   OK
    test_core.py                   Ran 15 tests in 3.755s   OK
    test_v011_p0.py                Ran 16 tests in 0.769s   OK
    test_v011_p1.py                Ran 24 tests in 7.568s   OK
    test_v011_p2.py                Ran 24 tests in 1.400s   OK
    test_v011_p3.py                Ran 20 tests in 19.289s  OK
    test_v011_rc3.py               Ran 26 tests in 5.428s   OK
    test_v011_rc4.py               Ran 33 tests in 1.588s   OK
    test_v011_rc5_cli_guard.py     Ran 4 tests  in 0.280s   OK
    test_v011_rc5_dispatch_id.py   Ran 3 tests  in 0.132s   OK
    test_v011_rc5_key_heal.py      Ran 8 tests  in 0.386s   OK
    test_v011_rc5_proxy.py         Ran 9 tests  in 14.365s  OK
    test_v011_rc5_receipts.py      Ran 24 tests in 2.752s   OK
    test_v011_rc5_restore_authority.py Ran 24 tests in 2.478s OK
    test_v011_rc5_review_audit.py  Ran 13 tests in 0.575s   OK
    test_v011_rc6.py               Ran 16 tests in 6.472s   OK

模块名跑法也可以用（两种都验证过）：

    .venv/bin/python -m unittest tests.test_api          # Ran 6 tests  ... OK
    .venv/bin/python -m unittest tests.test_v011_rc6     # 见 3.1.4，注意 PySide6 依赖

### 4.3 轻量脚本

    .venv/bin/python tools/selfcheck.py

退出码 0，逐项 16 条全 `[ok]`，末两行：

    通过 16 项，失败 0 项
    自检全部通过，可以启动 GUI。

分区覆盖：健康检查 3、MCP 协议 3、闭环 7、权限 3。日志 24 行 / 715 字节（`selfcheck.txt`）。
16 条里包括 `五个执行器工具`、`没有审批类工具`、`能领取并拿到 Prompt`、`重复领取被拒`、
`回传只到待验收`、`坏 Token 被拒`、`审批只能在进程内`。

    LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
      .venv/bin/python tools/gui_smoke.py

退出码 0：

    通过 9 项，失败 0 项
    GUI 冒烟通过。

9 条依次是：主窗口构造成功、五个页面都在（`count=5 keys=5`）、五个页面可切换
（overview / tasks / council / approval / monitor）、状态栏文案、待审批任务被识别。

**状态栏这条要看清它断的是什么。** `tools/gui_smoke.py:61` 只断文案是字符串：

    check("状态栏文案", isinstance(win.api_label.text(), str), win.api_label.text())

本轮打印的是 `API：未启动`（gui_smoke 不启动服务）。所以这条证明「标签取得到字符串」，
**不证明**「API 起来了」或「停服逻辑对」——后者由 3.1.4 那 3 项单测负责，两者别混成一条证据。

### 4.4 静态检查

    .venv/bin/python -m pyflakes bigfish tools tests

零输出，退出码 **0**（pyflakes 4.1.0）。

    .venv/bin/python -m compileall -q bigfish tools tests

无输出，退出码 **0**。

两条都只覆盖平台 A 的字节码/语法层面：pyflakes 零输出意味着没有未定义名、没有未使用导入；
它不检查类型、不检查运行时行为。

### 4.5 关于 `ResourceWarning: unclosed file ...council.db.access.lock`

这是全量日志里唯一一条需要注意的非纯噪音输出，单独写清楚，避免下一轮有人误当成偶发。

**现象。** 每次全量都会出现，且文件名是 `/tmp/tmpXXXX/council.db.access.lock`——一个临时库的租约文件。

**溯源。** 用 `PYTHONTRACEMALLOC=25` 单跑 `test_v011_rc5_cli_guard.py` 拿分配栈，
省略 stdlib 的 runpy / runner / suite 层，剩下的帧序是：

    File ".../unittest/case.py", lineno 579
    File ".../tests/test_v011_rc5_cli_guard.py", lineno 107
    File ".../tests/test_v011_rc5_cli_guard.py", lineno 41
    File ".../bigfish/cli.py", lineno 139
    File ".../bigfish/runtime.py", lineno 13
    File ".../bigfish/db.py", lineno 238
    File ".../bigfish/locks.py", lineno 44
    File ".../bigfish/locks.py", lineno 51

最后两帧对应的源码是 `locks.py:44` 的 `self.acquire()` 与 `locks.py:51` 的
`fh = open(target, "a+b")`。

对应到代码：`test_force_bypasses_the_guard`（定义在 `:92`，用例体 `:99-110`）用 `--force` 调
`self._run("--force")`（`:107`，`_run` 定义在 `:40`、转发 `cli.main` 的那行在 `:41`）→ `cli.main` 的 restore 分支
`db, council = open_council(s)`（`bigfish/cli.py:139`）→ `Database(settings.db_path)`
（`bigfish/runtime.py:13`）→ `self._lease = WriterLease(self.path)`（`bigfish/db.py:238`）
→ `self.acquire()`（`bigfish/locks.py:44`）→ `fh = open(target, "a+b")`（`:51`）。

用例以 `assertRaises(RestoreError)` 结束，**CLI 的 `main()` 没有 `finally` 去 `db.close()`**，
于是 `Database` 对象连同租约文件句柄一起等 GC，解释器在收尾时打出 `ResourceWarning`。

**这条能证明什么、不能证明什么。** 能证明「CLI 的 restore 失败路径没有显式关闭数据库」；
不能证明「租约泄漏导致别的进程恢复不了」——`test_failed_constructor_does_not_leak_lease`
（`:61`）与 `test_lease_is_released_on_close`（`:96`）已经证明正常路径的释放是对的，
265 项全绿也说明没有功能层面的失败。它是**句柄卫生问题的提醒**，不是失败。

修复方向（本轮未做，只记录）：在 `cli.main` 的 restore 分支加 `try/finally: db.close()`。
本轮不改代码的原因写在第 6 节：本轮只做文档与验证，动 `bigfish/` 需要单独的提交与回归。

---

## 5. 平台 A / 平台 B 对照

| 项目 | 平台 A（Debian 12 / Python 3.11.2） | 平台 B（Windows 11 / Python 3.13.11） |
| --- | --- | --- |
| 全量 265 项 | **Ran 265 / OK / 退出码 0**（四次） | **Ran 265 / OK (skipped=1) / 退出码 0**（59.975s，修完两个缺陷后） |
| 16 个文件逐文件单跑 | 16/16 退出码 0 | 未单独跑（全量已覆盖） |
| selfcheck | 16/16 通过 | 未跑 |
| gui_smoke（offscreen） | 9/9 通过 | 未跑 |
| GUI 真窗口验收 | **未做**（只有 offscreen smoke + 单测） | **未做** |
| pyflakes 零输出 | 是（4.1.0） | 未跑（未装 pyflakes） |
| compileall 退出码 0 | 是 | **是（COMPILE_RC=0）** |
| `locks.py` 写者租约 | `fcntl.flock` 分支，6 项覆盖 | `msvcrt.locking` 分支，**已真机跑通**；并修出缺陷 17（同进程句柄） |
| `locks.py` 多写者探测 | `_probe_other_writers`（`:89`）只在 Windows 分支用到 | **已执行**（随 `test_second_database_on_same_file_blocks_restore` 与 `test_exclusive_access_fails_while_lease_held` 走到） |
| SIGINT 优雅退出用例 | 已跑（`test_v011_p3.py`） | 跳过（`skipIf(os.name == "nt")`，Windows 无可捕获 SIGTERM） |
| CLI restore 关库 | 靠进程退出，测不出差异 | **测出缺陷 18**（`PermissionError: [WinError 32]`），已修 |
| 跨设备 MCP 闭环 | **未做**（rc2 的 11/11 不算本轮） | **未做** |
| 停服超时路径 | 单测已覆盖（3 项），真窗口未点 | **未做** |

---

## 6. 未覆盖 / 未验证

按影响排序，逐条写清「没做什么」与「因此不能声称什么」。
本节同时对照 Astra 在 `AUDIT_REVIEW.md` 第 112-121 行列的 5 条发布门槛。

**6.1 Windows 全量已跑通（Astra 门槛 1 完成），但首次跑出两个真缺陷。**
平台 B 在 `D:\BigFishCouncil\bigfish-council-rc6`（Windows 11 / Python 3.13.11 / SQLite 3.50.4）上
最终 `Ran 265 tests in 59.975s / OK (skipped=1)`、退出码 0。首次跑并不是这个结果——`FAILED (errors=2, skipped=1)`，
两条 ERROR 都指向同一条 `bigfish.locks.LeaseError: … [Errno 13] Permission denied`。
顺着查下去是**两个 Windows 专属的产品缺陷**，不是测试写错，Linux 上的 265 项永远碰不到它们。

**6.1.1 缺陷 17（P1）：`msvcrt` 的字节区间锁按句柄计，同进程第二个 `Database` 被误判为「有别人在用」。**
`msvcrt.locking(LK_NBLCK)` 把锁记在**文件句柄**上；租约文件按 pid 命名（`council.db.access.<pid>.lock`），
于是同一进程里第二个 `Database` 用第二个句柄锁同一文件、同一字节区间，直接 `EACCES`。
在 Linux 上 `flock` 对同一文件开多个句柄各自 `LOCK_SH` 是合法的，所以这个分支从来没被真机检验过。
后果是真实的：GUI 里同进程再开一个 `Database`、测试里的 `other = Database(self.db.path)`、
恢复流程自己 `pause_lease()` 后再 `resume_lease()`，全都会撞上。

报错原文：

```
bigfish.locks.LeaseError: 无法登记数据库写者租约
（C:\Users\CLIENT-A\AppData\Local\Temp\tmpiwwwhd_l\council.db.access.32844.lock）：[Errno 13] Permission denied
```

两条 ERROR 分别是 `tests.test_v011_rc5_cli_guard.TestCliRestoreGuard.test_force_bypasses_the_guard`
与 `tests.test_v011_rc6.TestCrossProcessLease.test_second_database_on_same_file_blocks_restore`。

修法：`bigfish/locks.py` 加模块级 `_win_leases`（`锁文件路径 -> [文件句柄, 引用计数]`）。
`acquire()` 命中共用句柄并计数 +1；`release()` 计数 -1，归零才 `LK_UNLCK` + `close()` + `os.unlink()`。
POSIX 的 `fcntl` 分支一行未改。

**6.1.2 缺陷 18（P2）：`bigfish/cli.py` 的 restore 分支不关库。**
`restore_from_bundle()` 成功后直接 `return 0`。Linux 上进程退出即释放 fd；Windows 上进程未退出前文件仍被占用，
于是调用方紧接着的删除/移动报 `PermissionError: [WinError 32] 另一个程序正在使用此文件`
（测试 `tearDown` 的 `self.tmp.cleanup()` 就挂在这里，栈在 `shutil.py:625 _rmtree_unsafe` → `tempfile.py:955 cleanup`）。
修法：restore 分支包 `try/finally` 关库——其他分支是短命只读操作，交给进程退出即可，只有恢复动的是库文件本身。

两个缺陷修完并提交 `73104a8` 之后才拿到 265 全绿。**这两条是本轮平台 B 的真正产出，比「跑通」本身更重要。**

仍然存在的覆盖空档：`test_v011_p3.py` 的 SIGINT 用例在 Windows 上直接跳过（无可捕获 SIGTERM，覆盖为空），
`selfcheck` / `gui_smoke` / `pyflakes` 在平台 B 上没跑。

**6.2 跨设备 MCP 闭环未做（Astra 门槛 4 未完成）。** rc6 没有任何一次「两台真机 + Tailscale + MCP」
的端到端回归。rc2 时期那份 11/11 是旧版本、旧环境，Astra 的审计原文里也写明它不能替代本版验收。
因此不能声称「跨设备链路在 rc6 上仍然可用」——本轮能证明的只有 `tools/selfcheck.py` 里
那 7 条闭环（同机、进程内起服务、`TestClient`/HTTP 两种入口）。

**6.3 GUI 真窗口未验收（Astra 门槛 3 未完成一半）。** 没有在真实窗口里点过「恢复」按钮，
也没在真实事件循环里验证 `pause_services()`（`bigfish/gui.py:1235`）→ 停服失败 →
`RuntimeError`（`:1263`）→ 界面提示这条链的时序。3.1.4 的 3 项是单测级别：
真线程、真 `ApiServer`，但没有 Qt 事件循环。所以 F-05 的状态是
「**代码路径已覆盖，界面交互未验证**」。

**6.4 停服超时路径只在单测里验证过（Astra 门槛 3 的另一半）。** `test_stop_raises_when_thread_will_not_exit`
用一个 `sleep(3)` 的假线程触发超时（`timeout=0.05`）；真实场景是 uvicorn 线程卡在写库上，
超时值默认 15 秒（`bigfish/gui.py:165`）。用 0.05 秒验证的是「超时会抛错」这条逻辑，
不是「15 秒够不够」。真实卡死要多久才判定，本轮没有测。

**6.5 跨进程互斥：Windows 分支已真机跑通，真·双进程测试仍未做（Astra 门槛 5 完成但带一条保留）。**
6 项租约测试里「另一个进程」是用**同一个 Python 进程里再开一个 `Database`** 模拟的
（`tests/test_v011_rc6.py:80` `other = Database(self.db.path)`）。
这对 flock 有效——flock 按打开的文件描述符算，同进程不同 fd 也会互斥——而且平台 B 真机跑通顺带把
`msvcrt` 分支钉死了（缺陷 17）：Windows 那一支现在有真机证据，不再是「只有代码路径」。
但仍未做的：**两个真正独立的进程同时恢复**这个场景一次都没跑过。所以门槛 5 的准确状态是：
**方案已落地、POSIX 分支已测六项、Windows 分支已真机跑通并修出缺陷、真·双进程未验证**。

**6.6 pyflakes 在平台 B 未跑，compileall 已跑。** 平台 B `COMPILE_RC=0`，说明 `msvcrt` / `winreg` 这类
只在 `if os.name == "nt"` 里 import 的名字没有拼错。但平台 B 未装 pyflakes，
未使用导入/未定义名字这类问题在 Windows 上没有第二份证据。

**6.7 `ResourceWarning` 未修。** 4.5 节那条句柄提醒在 HEAD 上仍然存在。它不影响 265 项结论，
但下一轮若开启 `-W error::ResourceWarning`，全量会因为这一条变红。
待查方向已收窄：`tests/test_v011_rc5_cli_guard.py::TestCliRestoreGuard.test_force_bypasses_the_guard`，
报错栈在 `cli.main` 的 restore 分支——本轮 `73104a8` 给它补了 `finally: db.close()`，很可能已经顺带消掉，
但**没有复测验证，不算修好**。

**6.8 F-08 没有专用用例。** 它是一条 docstring 契约（`bigfish/server.py:161-171`：本函数不 bind socket，
部署必须走 `serve()`）。本轮只能核对文本存在，不能声称「无法绕过」——
护栏仍在 `test_v011_rc3.py::test_all_entry_points_reject_public_bind` 那边，
那条测的是入口点，不是 `create_app` 被第三方 `uvicorn.run` 直接调用的情况。

---

## 7. 怎么复现

以下命令按顺序照抄，全部在 `/home/dsh/bigfish-council` 下执行。

    cd /home/dsh/bigfish-council

    # 0) 确认版本与提交
    git rev-parse --abbrev-ref HEAD
    git log --oneline -3
    .venv/bin/python -c "import bigfish; print(bigfish.__version__)"

    # 1) 全量单元测试（本机 50～75 秒，发布口径 41.7 秒）
    LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
      .venv/bin/python -m unittest discover -s tests -t .

    # 2) 逐文件单跑（16 个文件都应退出码 0）
    for p in test_api.py test_core.py test_v011_p0.py test_v011_p1.py \
             test_v011_p2.py test_v011_p3.py test_v011_rc3.py test_v011_rc4.py \
             test_v011_rc5_cli_guard.py test_v011_rc5_dispatch_id.py \
             test_v011_rc5_key_heal.py test_v011_rc5_proxy.py \
             test_v011_rc5_receipts.py test_v011_rc5_restore_authority.py \
             test_v011_rc5_review_audit.py test_v011_rc6.py; do
      LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests -p "$p" -t .
    done

    # 3) 独立数一遍用例总数，与 Ran 的数字对账
    .venv/bin/python -c "import unittest; \
    s=unittest.TestLoader().discover('tests', top_level_dir='.'); \
    f=lambda x: sum(f(t) for t in x) if isinstance(x, unittest.TestSuite) else 1; \
    print('TOTAL', f(s))"

    # 4) 自检（会起服务，注意端口占用）
    .venv/bin/python tools/selfcheck.py

    # 5) GUI 冒烟（offscreen；缺 LD_LIBRARY_PATH 会少跑 3 项 GUI 单测，见 3.1.4）
    LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
      .venv/bin/python tools/gui_smoke.py

    # 6) 静态检查
    .venv/bin/python -m pyflakes bigfish tools tests
    .venv/bin/python -m compileall -q bigfish tools tests

    # 7) 相对 rc5 发布点的改动规模（限定代码与测试目录，docs 另有文档批次）
    git diff --stat b8f6de1 73104a8 -- bigfish tests tools

    # 8) 只看本轮新增的 16 项（-v 便于逐项对数）
    LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
      .venv/bin/python -m unittest -v tests.test_v011_rc6

平台 B（Windows）的执行方式（本轮实跑过的原样命令）：

    # 在 D:\BigFishCouncil\bigfish-council-rc6 下，PowerShell
    $env:QT_QPA_PLATFORM = "offscreen"
    .\.venv\Scripts\python.exe -m unittest discover -s tests -t .

要点：① **不要**设 `LD_LIBRARY_PATH`（那是 Linux 上缺 X11 库时的绕行）；② 必须设 `QT_QPA_PLATFORM=offscreen`，
否则 GUI 单测会去要真实显示器；③ 走的是 `msvcrt` 租约分支与 TerminateProcess 退出码分支，
预期 `OK (skipped=1)`（skip 的是 SIGINT 用例）。

本轮实测：`Ran 265 tests in 59.975s / OK (skipped=1)`、退出码 0。
**首次跑不是这个结果**——`FAILED (errors=2 skipped=1)`，两条 ERROR 引出的两个真缺陷见 6.1.1 / 6.1.2。
如果在一台干净的 Windows 上复现，应当先看到那两条 ERROR（若代码回退到 `73104a8` 之前），
修完后再全绿；直接全绿则说明跑的是含修复的版本。
