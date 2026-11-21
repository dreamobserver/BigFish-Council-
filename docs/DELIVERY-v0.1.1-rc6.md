# 交付说明：bigfish-council v0.1.1-rc6

> 本文数字来自 2026-11-20 平台 A 实跑与 2026-11-21 平台 B 实跑，原始输出在 `/home/dsh/bfc-rc6-logs/`（平台 B 首次跑出两个 Windows 专属缺陷，修完后全绿，见 §10.2）。
> 前序文档：[rc5 交付说明](DELIVERY-v0.1.1-rc5.md)；本批修复的逐条对照见 [rc6 修复总表](FIX-MATRIX-v0.1.1-rc6.md)，代码级 diff 讲解见 [rc6 Diff 摘要](DIFF-SUMMARY-v0.1.1-rc6.md)，本文不重复推导。
> 本文只写 rc6 这一批。范围是三个提交 `cbf4d89` / `f1d1ce0` / `ae2fe82`，加上平台 B 修复提交 `73104a8`。相对 rc5 发布提交 `c6d663d`（GitHub main 上的 v0.1.1-rc5）代码侧 **19 个文件 / +826 / -142**，其中真属 rc6 的是 **13 个文件 / +817 / -133**，其余 6 个是 rc5 就存在的基线漂移（§3.4）；`73104a8` 再改 2 个文件 / +81 / -21。全量 **265 项 / 16 个文件**，双平台全绿。

## 1. 一句话结论

**不可发布。Astra 的 5 条发布门槛满足 3 条，仍缺真窗口验收与跨设备闭环**（详见 §10）。

判据拆开写，不含糊：

- 审计 `AUDIT_REVIEW.md` 的九条 F-01～F-09 全部落到代码，每条都能定位到具体用例（§2.1 给「文件::测试名」，不写「有测试覆盖」这种话）。
- 平台 A 全量 265 项通过、16 项自检、9 项 GUI 冒烟、pyflakes 零输出、compileall 退出码 0（§4）。
- 平台 B（Windows 11 / Python 3.13.11 / SQLite 3.50.4）**已跑通 265 项**：`Ran 265 tests in 59.975s / OK (skipped=1)`，退出码 0。但这次跑通是**修完两个 Windows 专属缺陷之后**的跑通——首次跑是 2 ERROR，见 §10.2 缺陷 17/18。
- GUI 只在 `QT_QPA_PLATFORM=offscreen` 下过了构造与信号连接；真窗口、停服超时的人眼确认都没人做过。
- 跨设备 MCP 闭环本轮没重跑（rc2 的 11/11 验的是 rc2 代码）。

口径：全库唯一数字是 **265 项 / 16 个文件**，对应 HEAD `73104a8`。本轮三次实战实测分别是 `Ran 265 tests in 49.584s`（`unittest-linux.txt`）、`52.690s`（`unittest-verbose.txt`）、`74.674s`（`unittest-doc-run.txt`），三次都是 `OK` / 退出码 0。耗时随机器负载浮动，项数不变。

### 状态块

| 字段 | 值 |
| --- | --- |
| 版本号 | `0.1.1-rc6`（`bigfish/__init__.py:6` 的 `__version__`，本批 +1/-1 只改这一行） |
| 分支 / HEAD | `fix/v0.1.1-rc4-hardening` / `73104a8`（`fix(windows): 同进程多实例共享写者租约 + CLI restore 关库`；其父 `b01261f`） |
| 对比基线 | rc5 发布提交 `c6d663d`；**不是 HEAD 的祖先**（`git merge-base c6d663d HEAD` 为空，仓库是两条独立历史线），所以另给纯 rc6 口径 `2f43346..HEAD` |
| 本批提交 | `cbf4d89`（F-01～F-04，Astra 本人写）、`f1d1ce0`（F-03 异常类型收口）、`ae2fe82`（F-05～F-09 + 版本号 + 两处噪音 + 顺手修）、`73104a8`（平台 B 真机跑出的两个 Windows 专属缺陷） |
| 定性 | 发布候选（release candidate），不是生产版 |
| 平台 A | Debian 12 / Linux 5.15.0-187-generic / Python 3.11.2 / SQLite 3.40.1 / fastapi 0.163.2 / starlette 1.10.0 / pydantic 2.14.3 —— 265 项全绿 |
| 平台 B | Windows 11 / Python 3.13.11 / SQLite 3.50.4 / PySide6 6.12.0 —— **265 项全绿**（`Ran 265 tests in 59.975s`，`OK (skipped=1)`，退出码 0）；跑通前修了两个 Windows 专属缺陷（§10.2） |
| SCHEMA_VERSION | **4**（未变，`bigfish/db.py:24`） |
| 备份格式版本 | **4**（未变，`bigfish/db.py:25`） |
| 测试规模 | **265 项 / 16 个测试文件**；rc5 是 244 项 / 15 个文件，本批 +21 项（新文件 16 + 既有文件 5） |
| 生产部署 | **未部署**。现役服务仍是 rc3（`832f699`）；rc4 / rc5 / rc6 都没上过生产 |

## 2. 本轮做了什么

一句话：**把 Astra 在 rc5 上审出来的九条问题（F-01～F-09）全部落到代码，其中最大的一处是把「谁在用这个数据库」从「探端口猜」换成「打开即登记的写者租约」。**

### 2.1 九条清单

| # | 级别 | 一句话问题 | 落点 | 回归测试 | 状态 |
| --- | --- | --- | --- | --- | --- |
| F-01 | P1 | 维护模式 `_guard()` 与抢 `_lock` 不在同一临界区，已过 guard 的线程会在维护模式生效后照样读写 | `bigfish/db.py:461` `query`、`:467` `one`、`:472` `execute`、`:477` `_Tx`（`:485` `__enter__`） | `test_v011_rc5_restore_authority.py::test_query_guard_is_atomic_with_lock`（:340）、`::test_transaction_guard_is_atomic_with_lock`（:343） | 已修复 |
| F-02 | P1 | `restore_bundle` 在维护模式内读 `restore_epoch`，被自己的 `MaintenanceError` 打成 0 又被吞掉，世代「只增不减」失效 | `bigfish/backup.py:487`（挪进 `:485` 的 `_allow_during_maintenance()` 内） | `test_v011_rc5_restore_authority.py::test_restore_epoch_is_monotonic_when_restoring_an_older_snapshot`（:199，活库抬到 7，恢复旧包必须得 8） | 已修复 |
| F-03 | P1 | 密钥一致性自愈失败仍继续启动（`except Exception: pass`），带着未核验的旧密钥跑起来 | `bigfish/runtime.py:18` → `:21` `raise CouncilError("执行票密钥一致性校验失败，拒绝启动")`，先 `db.close()` | `test_v011_rc5_key_heal.py::TestStartupFailsClosed::test_open_council_refuses_to_start_when_key_repair_fails`（:139 / :140） | 已修复 |
| F-04 | P2 | CLI `restore` 的端口预检排在开库之后，端口保护来得太晚 | `bigfish/cli.py:130-139`（先 `Settings.load` + `_port_busy` 再 `open_council`）、`:41` `_port_busy`、`:222` restore 分支只剩 `dump` | `test_v011_rc5_cli_guard.py::test_busy_port_is_checked_before_database_open`（:84） | 已修复 |
| F-05 | P1/P2 | GUI `stop()` join 超时后不检查线程死活，`pause_services()` 又先置位后停服，恢复会在 API 线程还写库时换文件 | `bigfish/gui.py:165` `stop`（:178 抛 `TimeoutError`）、`:182` `is_running`、`:1235` `pause_services`（:1263 抛 `RuntimeError`、`:1266` `resume_services`） | `test_v011_rc6.py::TestGuiPauseConfirms`（:202，三项 :233 / :250 / :263） | 已修复（单测级，真窗口未验收） |
| F-06 | P1 | 跨进程恢复互斥只是尽力而为：`_restore_lock` 只互斥「restore 对 restore」，管不住普通 `Database` 继续写 | 新增 `bigfish/locks.py`（157 行：`:23` `LeaseError`、`:38` `WriterLease`、`:46` `acquire`、`:66` `release`、`:89` `_probe_other_writers`、`:113` `exclusive_access`）；`bigfish/db.py:238` / `:525` / `:549` / `:557`；`bigfish/backup.py:484` / `:643` / `:651`；删掉原 `_restore_lock()` | `test_v011_rc6.py::TestCrossProcessLease`（:60，六项） | 已修复（POSIX 已测；Windows `msvcrt` 分支已真机跑通，并在过程中修出一个真缺陷——msvcrt 字节区间锁按句柄计算，同进程第二个实例会 EACCES，见 §10.2 缺陷 17） |
| F-07 | P1/P2 | 审计包 manifest 写死「无明文凭据」，而白名单里含 `task_versions.prompt` 等自由文本，脱敏只覆盖已知正则 | `bigfish/security.py:36`（正则表改三元组）、`:54` `SCRUB_KINDS`、`:88` `scan_secrets`；`bigfish/backup.py:214` `credential_scan`（`:221` `manual_review_required: True`）、`:225` `handling` 改口 | `test_v011_rc6.py::TestAuditManifestHonesty`（:130，五项） | 已修复 |
| F-08 | P2 | `create_app()` 不负责 bind，第三方可 `uvicorn.run(create_app(...), host="0.0.0.0")` 绕过回环闸门 | `bigfish/server.py:161` 的 docstring 写清边界（本函数不 bind，部署只走 `serve()` / GUI / `python -m bigfish`） | 无专用用例（契约文本）；护栏在 `test_v011_rc3.py::test_all_entry_points_reject_public_bind` | 已修复（文档级） |
| F-09 | P2 | `log` / `diff` / `tests` / `detail` 等自由文本整个请求体先被读进内存，被控执行器可一次压爆 | `bigfish/server.py:38` / `:40`（两个上限）、`:64` `BodyLimitMiddleware`、`:116` `_BodyTooLarge`、`:121-140` 字段 `max_length`、`:187` `add_middleware` | `test_v011_rc6.py::TestBodyLimit`（:166，两项 :183 / :189） | 已修复 |

### 2.2 另外两处顺手修（不在 F 编号里）

| # | 问题 | 修法 | 影响 |
| --- | --- | --- | --- |
| S1 | `tests/test_v011_rc5_cli_guard.py` 的 `accept_loop()`（:51）在 `srv.close()` 之后才 `settimeout`，收尾抛 `OSError: [Errno 9] Bad file descriptor` 刷栈（测试本身是过的，纯噪音） | `settimeout` 提到循环外并 `try/except OSError` | 全量日志干净一屏 |
| S2 | `tests/test_v011_rc6.py` 初稿带两个未用导入 | 直接删（现文件不含 `from bigfish import state as S` 与 `import os as _os`） | `pyflakes` 归零 |

## 3. 改动文件清单

`git diff --name-status c6d663d..HEAD -- bigfish tests tools` → **19 个文件：17 M + 2 A**。按分组列：`bigfish/` 10 个（9 改 + 1 新增）、`tests/` 5 个（4 改 + 1 新增）、`tools/` 4 个（全是改，且全是漂移）。

### 3.1 `bigfish/`：10 个文件（9 改 + 1 新增）

| 文件 | 改动 | 行数 | 本批职责 |
| --- | --- | --- | --- |
| `bigfish/__init__.py` | +1/-1 | 8 | 版本号改 `0.1.1-rc6` |
| `bigfish/backup.py` | +52/-55 | 733 | F-02 世代读取（`:487`）、F-06 租约编排（`:483` / `:484` / `:643`）、`_translate_lease`（`:651`）、F-07 审计包声明（`:214` / `:221` / `:225`）、删掉整个旧 `_restore_lock()`（约 45 行） |
| `bigfish/cli.py` | +12/-10 | 236 | F-04：`restore` 前置分流（`:130-139`），`_port_busy`（`:41`），旧端口预检从 `:222` 分支删除 |
| `bigfish/db.py` | +67/-32 | 659 | F-01 锁内 guard（`:461` / `:467` / `:472` / `:485`）、F-06 租约（`:238` 登记、`:254-274` 构造失败释放、`:525` `close(release_lease=)`、`:549` `pause_lease`、`:557` `resume_lease`） |
| `bigfish/gui.py` | +33/-9 | 1419 | F-05：`stop`（`:165`，超时抛错 `:177-179`）、`is_running`（`:182`）、`pause_services`（`:1235`，先停成功再置位、失败抛 `:1263`）、`resume_services`（`:1266`）；两处 `receivers` 前置判断（`:1248` / `:1349`）消掉 PySide6 断开告警 |
| `bigfish/locks.py` | **新增** +157/-0 | 157（5850 字节） | F-06 本体：写者租约 + 排他访问锁 |
| `bigfish/runtime.py` | +6/-5 | 41 | F-03 fail-closed（`:18` → `:21`），先 `db.close()` 再抛 `CouncilError`（`f1d1ce0` 收的口：原来抛 `RuntimeError`，CLI 的退出码 2 路径接不住） |
| `bigfish/security.py` | +30/-9 | 233 | F-07：`_PATTERNS` 三元组（`:36`）、新增五类正则、`SCRUB_KINDS`（`:54`）、`scan_secrets`（`:88`） |
| `bigfish/server.py` | +85/-11 | 545 | F-08 docstring（`:161-171`）、F-09 两层上限（`:38` / `:40` / `:64` / `:116` / `:121-140` / `:187`） |

纯 rc6 口径 `git diff --shortstat 2f43346..HEAD -- bigfish` 是 **9 files changed, 443 insertions(+), 132 deletions(-)**（不含 `roles.py` 那行基线漂移）。

### 3.2 `tests/`：5 个文件（4 改 + 1 新增）

| 文件 | 改动 | 行数 | 用例数 | 内容 |
| --- | --- | --- | --- | --- |
| `tests/test_v011_rc6.py` | **新增** +280/-0 | 280（10646 字节） | **16** | 四个类：`TestCrossProcessLease`（:60，6）、`TestAuditManifestHonesty`（:130，5）、`TestBodyLimit`（:166，2）、`TestGuiPauseConfirms`（:202，3） |
| `tests/test_v011_rc5_restore_authority.py` | +64/-0 | 424 | 20 → **24** | F-01 两项（`:340` / `:343`，共用 `_assert_guard_lock_order` 于 `:296`）+ F-02 一项（`:199`）+ 既有计数变动，均为 Astra 在 `cbf4d89` 里加 |
| `tests/test_v011_rc5_key_heal.py` | +15/-0 | 152 | 7 → **8** | F-03：`TestStartupFailsClosed`（`:139`）一项（Astra 写） |
| `tests/test_v011_rc5_cli_guard.py` | +15/-1 | 124 | 3 → **4** | F-04 一项（`:84`）+ S1 的 `accept_loop` 收尾整理（`:51-57`） |
| `tests/test_v011_p3.py` | +2/-2 | — | 20（不变） | **基线漂移**：`CLIENT-A-TAILNET-IP` 换成真实 IP `CLIENT-A-TAILNET-IP`，不是 rc6 改动 |

用例数核对：244（rc5）+ 16（新文件）+ 5（既有文件各加 1）= **265**。

### 3.3 `tools/`：4 个文件（全改，全是漂移）

| 文件 | 改动 | 定性 |
| --- | --- | --- |
| `tools/bfc-proxy.mjs` | +2/-2 | 基线漂移：IP 占位符换真实值 |
| `tools/dev_setup.sh` | +0/-0 | 仅 `chmod +x`（文件模式变了，内容没变） |
| `tools/net_check.py` | +1/-1 | 基线漂移：同上 |
| `tools/remote_check.py` | +3/-3 | 基线漂移：同上 |

### 3.4 差异规模的真实三口径

| 口径 | 命令 | 结果 |
| --- | --- | --- |
| 相对 `c6d663d`（含 6 个漂移文件） | `git diff --shortstat c6d663d..HEAD -- bigfish tests tools` | **19 files changed, 826 insertions(+), 142 deletions(-)** |
| 剔除 6 个漂移文件（真 rc6） | 同上减 `roles.py` / `test_v011_p3.py` / `tools/*` | **13 files changed, 817 insertions(+), 133 deletions(-)** |
| 纯 rc6（相对 rc5 文档收口点 `2f43346`） | `git diff --shortstat 2f43346..HEAD -- bigfish tests tools` | **13 files changed, 817 insertions(+), 133 deletions(-)**，与上一行一致 |

`bigfish/roles.py` 的 +1/-1 是文案（「执行器（VPS DSH）」→「执行器（VPS DSH）」），属基线漂移；**它不在 rc6 三个提交里**。这 6 个文件一律不要算进 rc6 的工作量。

## 4. 测试证据

全部是本轮在平台 A 实跑，命令与输出照抄。

### 4.1 全量单元测试

```bash
cd /home/dsh/bigfish-council
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen   .venv/bin/python -m unittest discover -s tests -t .
```

三次实测：

| 次 | 输出 | 退出码 | 日志 |
| --- | --- | --- | --- |
| 1 | `Ran 265 tests in 49.584s` + `OK` | 0 | `/home/dsh/bfc-rc6-logs/unittest-linux.txt`（1381 字节，`FAIL:` / `ERROR:` 均 0 行） |
| 2 | `Ran 265 tests in 52.690s` + `OK` | 0 | `unittest-verbose.txt`（36847 字节，265 行用例名） |
| 3 | `Ran 265 tests in 74.674s` + `OK` | 0 | `unittest-doc-run.txt`（本文写前复核） |

`-v` 版逐行统计（`unittest-verbose.txt`，265 行 `ok`，0 个 `skipped` / 0 个 `expected failure`）：

| 文件 | 项数 | 文件 | 项数 |
| --- | --- | --- | --- |
| `test_api` | 6 | `test_v011_rc5_proxy` | 9 |
| `test_core` | 15 | `test_v011_rc5_receipts` | 24 |
| `test_v011_p0` | 16 | `test_v011_rc5_restore_authority` | 24 |
| `test_v011_p1` | 24 | `test_v011_rc5_review_audit` | 13 |
| `test_v011_p2` | 24 | `test_v011_rc5_key_heal` | 8 |
| `test_v011_p3` | 20 | `test_v011_rc5_cli_guard` | 4 |
| `test_v011_rc3` | 26 | `test_v011_rc5_dispatch_id` | 3 |
| `test_v011_rc4` | 33 | `test_v011_rc6` | 16 |

合计 **265**。

### 4.2 自检、GUI 冒烟、静态检查、字节码编译

```bash
export LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen
.venv/bin/python tools/selfcheck.py                   # 通过 16 项，失败 0 项 / exit 0
.venv/bin/python tools/gui_smoke.py                   # 通过 9 项，失败 0 项 / exit 0
.venv/bin/python -m pyflakes bigfish tests tools      # 零输出 / exit 0（0 字节）
.venv/bin/python -m compileall -q bigfish tests tools # 零输出 / exit 0（0 字节）
```

| 项目 | 结果 | 日志 |
| --- | --- | --- |
| 自检 16 项 | 通过 16 / 失败 0，末行「自检全部通过，可以启动 GUI。」 | `selfcheck.txt`（700 字节）、`selfcheck-doc-run.txt` |
| GUI 冒烟 9 项 | 通过 9 / 失败 0，末行「GUI 冒烟通过。」 | `gui_smoke.txt`（447 字节）、`gui_smoke-doc-run.txt` |
| `pyflakes` | 0 字节输出，退出码 0 | `pyflakes.txt`（0 字节）、`pyflakes-doc-run.txt` |
| `compileall -q` | 0 字节输出，退出码 0 | `compileall.txt`（0 字节）、`compileall-doc-run.txt` |

### 4.3 租约行为的手工实测（本文写前复核）

不是单测，是 REPL 里直接跑产品代码，用来证明 F-06 的行为是真的：

| 场景 | 命令要点 | 实测输出 |
| --- | --- | --- |
| 打开即登记租约 | `Database(d/"council.db")` 后看目录 | 多出 `council.db.access.lock`（0 字节）；`close()` 后只剩 `council.db` 与该 lock |
| 两个实例共存 | 同一文件开两个 `Database` | 都成功（POSIX `LOCK_SH` 可多份） |
| 有人用时拒绝恢复 | 持租约时调 `exclusive_access(dbp)` | 抛 `LeaseError`：「还有进程在用这个数据库（写者租约仍在生效），拒绝替换。请先关闭 GUI / 停掉 headless 服务。」 |
| 都关了才允许 | 两个实例 `close()` 后再 `exclusive_access` | 成功进入 with 块 |
| 构造失败不泄漏 | 造一个 `schema_version=99` 的库再 `Database(p)` | 抛 `DatabaseVersionError`（「数据库 schema 版本 99 高于本程序支持的 4，拒绝打开」），随后 `exclusive_access` **仍能拿到锁**（说明租约已释放，不是被 GC 挂着） |
| 备份包不含锁文件 | `export_bundle` 后看 `zipfile.namelist()` | 只有 `['manifest.json', 'council.db']` |
| 恢复往返 + 世代递增 | `restore_bundle(db, 包)` 连做三次 | `restore_epoch` 依次 1 / 2 / 3；返回值里 `restore_epoch` 与 `db.restore_epoch()` 一致 |

### 4.4 已知噪音（不是失败）

五条，出现时不应判为回归：

1. `Fontconfig error: Cannot load default config file: No such file: (null)` —— 精简容器里没有 fontconfig 配置，offscreen Qt 也会打印。
2. `StarletteDeprecationWarning` —— 依赖侧弃用提示（`httpx` 与 `starlette.testclient`）。
3. `This plugin does not support propagateSizeHints()` —— offscreen 平台不支持窗口提示。
4. `ResourceWarning: unclosed file <_io.BufferedRandom name='/tmp/…/council.db.access.lock'>` —— 只在 `tests.test_v011_rc5_cli_guard.TestCliRestoreGuard.test_force_bypasses_the_guard` 单跑时必现（全量里也打印一次）。已定位：该用例让 `cli.main --force` 走进 `open_council` → 登记租约 → 随后读包失败抛 `RestoreError`，用例本身没走 `db.close()`，flock 句柄留给 GC。**测试收尾瑕疵，不是产品缺陷；本轮未修**（要改那个用例的 `try/finally`，超出「只碰 rc6 相关文件」的范围）。
5. 恢复后磁盘上会留下 `council.db.restore-new.access.lock` —— staging 库在 `staged.close()` 时释放了租约，但 POSIX 的锁文件不删（删了会引入竞态）。0 字节，无业务数据，不影响下一次恢复（§4.3 连做三次实测）。

## 5. 安全影响

一句话：**五处收紧、一处「把强声明改弱但更真实」、一处只写清边界不动行为。没有任何一处放宽了访问控制。**

| 改动 | 收紧 / 放宽 | 一句话说明 |
| --- | --- | --- |
| F-01 锁内 guard | **收紧** | 维护屏障从「大多数情况生效」变成「与加锁同临界区」，堵掉「已过 guard 还在排队」那个窗口；同时 `_Tx.__enter__` 改用 `except BaseException`，`KeyboardInterrupt` / `SystemExit` 也不会把库锁永久挂死 |
| F-02 世代读取挪进旁路 | **收紧** | 恢复后 `restore_epoch` 真正单调递增；此前被打成 0 时，旧世代票据理论上能重新通过 `epoch` 校验 |
| F-03 fail-closed | **收紧** | 密钥与库指纹对不上且自愈失败时，进程拒绝启动，不再带着未核验的旧密钥接单 |
| F-04 CLI 预检前置 | **收紧** | 端口被占时，CLI 连库都不打开（不再有「先迁移 schema / 先跑密钥自愈，然后才说端口占用」） |
| F-05 GUI 停服确认 | **收紧** | 停服超时从「静默当成功」变成抛错并中止恢复；`_services_paused` 只在真停成功后才置位 |
| F-06 写者租约 | **收紧（最大的一处）** | 「有人在用这个库」从「探端口猜」变成「打开即登记的 flock」；恢复拿不到 `LOCK_EX` 就直接拒绝。原来那条路会让 POSIX 分叉出旧 inode、Windows 直接替换失败 |
| F-07 审计包声明 | **表面放宽措辞，实际收紧可信度** | `contains_plaintext_credentials:false` 这种强声明删了，改成 `manual_review_required: True` + 明写覆盖面缺陷；同时正则表新增 5 类（google / slack / stripe / key-value / uri-userinfo）。弱的是承诺，强的是检测 |
| F-08 create_app 边界 | **不变，只写清** | 没有新增校验，只把「本函数不 bind，绕过 `serve()` 不被支持」写进 docstring 与理由。封死会连带废掉 `TestClient`，所以选择诚实说明边界 |
| F-09 请求体上限 | **收紧** | ASGI 层 8 MiB（`BFC_MAX_BODY_BYTES` 可调）+ 字段层 4 MiB（`BFC_MAX_TEXT_FIELD` 可调）；`content-length` 与 chunked 两路都拦，超限 413 / 422 |

新增的磁盘痕迹（不是安全放宽，但要知情）：`council.db.access.lock` 与 Windows 侧 `council.db.access.<pid>.lock`。它们不含业务数据，进程退出即失去意义，备份包不含它们（已实测）。剩余风险与 T1–T8 现状见 [rc6 安全复审](SECURITY-REVIEW-v0.1.1-rc6.md)。

## 6. 数据库迁移说明

**schema 未变，v4 无需迁移。**

`bigfish/db.py:24` 的 `SCHEMA_VERSION = 4` 与 `:25` 的 `BACKUP_FORMAT_VERSION = 4` 本批一个字符都没动；`MIGRATIONS` 表没有新条目。rc5 → rc6 只改行为。

- rc5 及更早（v0.1.0 / rc1 / rc2 / rc3 / rc4）的库照旧走原有 `1 → 2 → 3 → 4` 链，逻辑不在本批改动范围内。
- 本批唯一新增的磁盘对象是租约文件（§5 末尾）。`export_bundle` 不会把它们打进包（实测 `namelist()` 只有 `manifest.json` + `council.db`），`restore_bundle` 也不会去找它们。
- 恢复过程会在同目录临时写 `council.db.restore-new`（staging）与 `council.db.prerestore`（回滚副本）。前者在 `finally` 里删；后者的 lock 会同舞台库一起留下（§4.4 第 5 条），可以放心删，只要确认没有进程在用它。
- 两个新环境变量（可选）：`BFC_MAX_BODY_BYTES`（默认 8 MiB）、`BFC_MAX_TEXT_FIELD`（默认 4 MiB）。不设就是默认值，存量部署无需任何配置变更。

字段级上限清单（`bigfish/server.py:121-140`）：`ClaimBody.task_id` 128、`ClaimBody.idempotency_key` 200、`HeartbeatBody.ticket` 256、`HeartbeatBody.idempotency_key` 200、`HeartbeatBody.detail` / `ResultBody.log` / `.diff` / `.tests` / `.note` 均为 `MAX_TEXT_FIELD`，`ResultBody.ticket` 256。**升级前请确认现役执行器的 `detail` / `log` 单次不超 4 MiB**，否则会从「静默截断」变成 422。

## 7. 回滚步骤

完整手册见 **`docs/ROLLBACK-v0.1.1-rc6.md`**（同批文档；以它为准，本节只留硬约束）。若该手册尚未落盘，以本节三条为准。

### 7.1 三条硬约束

1. **rc6 → rc5 是纯代码回滚，库不用换。** 两版 `SCHEMA_VERSION` 都是 4，回滚不涉及迁移、不涉及降级。这与 rc4 → rc5 那次（schema 3 → 4 不可降）完全不同，别套错。
2. **租约文件不能手工留成「活人」的形态。** POSIX 的 `council.db.access.lock` 是普通文件，进程退出后锁自然释放，文件留着无害；但**回滚期间必须先确认没有进程在用它**（`ss -lntp | grep 3096` + 确认没有 GUI / headless 在跑），否则 rc5 的 `_restore_lock` 不认识 rc6 的租约，旧的「尽力而为」互斥会重新生效。
3. **恢复过就要重新 `reenable`。** 这与 rc5 一致：恢复会禁用全部身份并轮换 `token_hash`，回滚代码不会把票和解禁状态恢复回来（密钥轮换过更不能回滚）。

### 7.2 最小回滚步骤（代码层，数据库不动）

```bash
# 1) 先备份（子命令是 export，不是 backup）
cd /home/dsh/bigfish-council
.venv/bin/python -m bigfish.cli export /tmp/rc6-before-rollback.bfc

# 2) 停掉一切在写库的进程（GUI / headless），并确认端口没人监听
ss -lntp | grep 3096

# 3) 退到 rc5 发布点。整棵树切换，不要只 checkout bigfish/：
git stash -u                       # 含 docs/ 下未提交的 rc6 文档，回滚后 git stash pop 取回
git checkout c6d663d               # 若 rc5 tag 可用，优先 git checkout v0.1.1-rc5

# 4) 验证：期望回到 244 项
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python tools/selfcheck.py

# 5) 回到本分支
git checkout fix/v0.1.1-rc4-hardening
```

第 3 步为什么必须整棵切换：局部回退会把 `tests/test_v011_rc6.py` 与 `bigfish/locks.py` 留在树里，rc5 代码不认识 `WriterLease` / `pause_lease` / `exclusive_access`，直接 `ImportError`。

### 7.3 如果只是想撤销一次恢复

与 rc5 相同：失败不会动原库，`restore_bundle` 在替换前留 `council.db.prerestore`（返回值 `prerestore_path`），密钥另有 `key_backup_path`。两者必须成对回退。库里处于维护模式时（回滚也失败的那条路径），进程重启即恢复常态，`keep_maintenance` 不落盘（`bigfish/backup.py:628` 只在本进程内保留）。

## 8. 明确未完成的

从 Astra 的 5 条发布门槛逐条对，加本轮其余缺口。**这里每一条都没做，不存在「大概跑过」的模糊地带。**

### 8.1 Astra 的 5 条发布门槛

| # | 门槛（摘要） | 完成情况 | 缺口 |
| --- | --- | --- | --- |
| 1 | Linux 与 Windows 分别跑完整测试 | **完成** | Linux：265/265 全绿（三次实测）。Windows 11 / Python 3.13.11：`Ran 265 tests in 59.975s / OK (skipped=1)`，退出码 0（日志 `D:\BigFishCouncil\ut3.txt`）。首次跑是 2 ERROR，定位出两个 Windows 专属真缺陷并修掉后才全绿（§10.2） |
| 2 | 验证 F-01 两个并发回归 / F-02 epoch=8 / F-03 拒绝启动 / F-04 端口占用时库未打开 | **完成（4/4）** | 四条用例全在，见 §2.1 对应行 |
| 3 | Windows 真实 GUI 窗口验收 + 停服超时路径 | **未做** | 停服超时只有单测（自造 `Stuck` 线程 / lambda 替换 `server.stop`），不经过真实 Qt 事件循环与真实 uvicorn 线程；真窗口无人看过 |
| 4 | 至少一次跨设备 MCP 闭环回归 | **未做** | rc2 的 11/11 验的是 rc2 代码，按审计口径不计入；本轮所有证据都是单机 |
| 5 | 跨进程 restore lock 的处理方案 | **完成（方案落地 + 双平台实测）** | `bigfish/locks.py` 的写者租约 + 排他锁，替换掉旧的 `_restore_lock()`。POSIX `fcntl` 分支六项单测；**Windows `msvcrt` 分支已在真机跑通**，并且真机跑出了一个单测在 Linux 上永远看不到的缺陷：msvcrt 的字节区间锁按**文件句柄**计，同一进程第二个 `Database` 实例锁同一区间会 EACCES，被误判成「有别人在用这个库」（§10.2 缺陷 17，已修） |

合计 **3 条完成（第 1、2、5）**，第 3、4 条未做。

### 8.2 本轮其余缺口

| 项 | 状态 | 说明 |
| --- | --- | --- |
| Windows `msvcrt` 租约分支 | **已真机验证（并修出缺陷）** | 真机上跑出「同进程第二个实例 EACCES」，已改成同进程共享句柄 + 引用计数（§10.2 缺陷 17） |
| GUI 真窗口验收 | **未做** | `tools/gui_smoke.py` 9 项是离屏构造级；`pause_services()` 的真实停服超时路径没人眼确认 |
| 跨设备 MCP 闭环 | **本轮未重跑** | 见门槛 4 |
| 平台 B 全量 265 项 | **已跑通** | `Ran 265 tests in 59.975s / OK (skipped=1)`，退出码 0；1 项 skip 是 POSIX 专属用例（SIGINT 优雅退出） |
| `ResourceWarning`（§4.4 第 4 条） | **未修** | 测试收尾瑕疵，不影响产品行为 |
| 回环 e2e（`tools/e2e_loopback.py`） | 本批未跑 | rc3 期间通过，rc4 / rc5 / rc6 都没重跑 |
| mypy / pyright | 未引入 | `pyflakes` + `compileall` 不是类型检查 |
| 24h soak | 未做 | 长稳、文件增长、内存驻留全未观测 |
| 打包 exe | 未做 | `build.ps1` 未执行 |
| 打 tag | **未打** | `git tag` 输出为空；rc6 只有裸提交号 `73104a8` |

## 9. 需要人类授权的操作

以下事项本轮**一律没有执行**。标注的「需授权」对应项目的人工所有者角色 `human_owner`，执行器角色无权触发其中任何一项。带 🔸 的是会改变现役状态或对外可见面的。

| 编号 | 事项 | 需授权的原因 | 不做的现状 | 执行前必须确认 |
| --- | --- | --- | --- | --- |
| 🔸 H1 | 把 rc6 部署到公司机 3096 现役服务 | 覆盖正在跑的 headless 进程、重启服务、让执行器重连 | 现役仍是 rc3（`832f699`）；rc4 / rc5 / rc6 都从未上过生产 | （1）先在平台 B 跑完 265 项 + 16 自检 + 9 冒烟；（2）备份 `council.db` 与 `secrets/ticket.key`（后者不进备份包，须单独拷）；（3）关旧进程再上新版，**不要并行混跑**；（4）确认执行器单次 `log`/`detail` 不超 4 MiB（F-09 字段上限）；（5）期间做过恢复则逐个 `reenable` |
| 🔸 H2 | 导入 bigfish-council MCP 连接 | 对外可见的配置变更，DSH 从此能领取已批准任务 | `dsh-mcp-bigfish.json` 的 `Authorization` 仍是占位符，DSH 现在没有连接本服务的能力 | 先 `executor-add` 生成真实 `bfc_ex_...`；确认 `tools/list` 只有五个工具且**不含审批**；确认 3098 上确实是 `bfc-proxy.mjs` |
| 🔸 H3 | 注册开机自启 | 注册 Windows 计划任务（`AtLogOn`），系统级持久化变更 | 没有自启项；重启后服务不会自己起来 | 核对 `install-task.ps1` 三处实参（任务名 `BigFishCouncil`、`.venvScriptspythonw.exe`、`%LOCALAPPDATA%BigFishCouncil`）；备好 `Unregister-ScheduledTask -TaskName BigFishCouncil -Confirm:$false` |
| 🔸 H4 | Windows 真实窗口验收 | 需要人在真实显示器上点一遍，机器代劳不了 | `gui_smoke` 只证明离屏构造与信号连接；DPI、焦点顺序、托盘、关窗确认、恢复对话框的 pause / resume 没人看过。**平台 B 全量 265 项已在真机跑通，这一格不欠了** | 真显示器 + Windows 真机；重点过「恢复备份」整条流程（含停服超时的人工构造）与五页切换 |
| 🔸 H5 | 重跑跨设备 MCP 闭环 | 需要 Tailscale 两侧真机同时在线，涉及公司机与 VPS | rc2 的 11/11 不是 rc6 的证据 | 两侧都上 rc6，claim → heartbeat → submit 全链路走一遍 |
| 🔸 H6 | 打发布 tag | 发布动作本身，对外可见 | `git tag` 为空 | 先过 H4 与 H5，再 `git tag -a v0.1.1-rc6 73104a8` |

六件都不做也不影响本文的技术结论；但 **H1 之前 H4 必须补完**，而 §10 的结论在 H4 / H5 之前不会变（平台 B 全量已补，欠的只剩真窗口这一半）。

## 10. 结论：rc6 可发布吗

**不可。**

Astra 的 5 条发布门槛，rc6 满足 **3 条**，2 条未做：

- **满足**：门槛 1（Linux 265/265 全绿 + Windows 265/265 全绿，双平台都跑过）、门槛 2（F-01～F-04 的四项回归全部到位、全部实测通过）、门槛 5（跨进程 restore lock 已落地，POSIX 六项单测 + Windows 真机跑通）。
- **未做**：门槛 3（Windows **真实窗口**验收 + 停服超时的人眼确认——全量测试不等于真窗口验收）、门槛 4（跨设备 MCP 闭环回归）。

所以准确的表述是：**这是一份可以拿去审计的 rc6，不是一次可以上线的发布。**

它现在能证明什么：九条审计问题在双平台都被钉死在具体用例里，265 项全绿可复现；租约行为有单测 + 手工实测 + 双平台真机三份证据；Windows 专属的两个真缺陷（msvcrt 同进程句柄、CLI restore 不关库）是这轮真机跑出来的，不是设计时想到的。

它现在不能证明什么：真实窗口里的停服行为（DPI、焦点、托盘、恢复对话框的 pause/resume）、跨设备 MCP 链路、NTFS ACL 之外的其他 Windows 文件语义、24h 长稳。

要翻成「可发布」，按这个顺序补：**H4 剩下的真窗口验收 → H5（跨设备闭环）→ §8.2 里挑 P1 → H1 上线**。

### 10.2 平台 B 首次跑暴露的两个真缺陷（本轮修复）

这两条不是测试写错，是 Windows 与 POSIX 的语义差异落到了产品代码里。Linux 上的 265 项永远碰不到它们。

**缺陷 17（P1）—— `bigfish/locks.py` 同进程第二个 `Database` 被误判为「有别人在用」。**

`msvcrt.locking(LK_NBLCK)` 的字节区间锁是**按文件句柄**记的。租约文件按 pid 命名（`council.db.access.<pid>.lock`），于是同一进程里第二个 `Database` 会对同一个文件、同一个字节区间用**第二个句柄**去锁，直接 `EACCES`：

```
bigfish.locks.LeaseError: 无法登记数据库写者租约
（C:\Users\...\council.db.access.32844.lock）：[Errno 13] Permission denied
```

在 Linux 上 `flock` 对同一文件开多个句柄各自 `LOCK_SH` 是合法的，所以这个分支从没被真机检验过。后果是真实的：GUI 里同进程再开一个 `Database`、测试里的 `other = Database(self.db.path)`、恢复流程自己 `pause_lease()` 后再 `resume_lease()`，全都会撞上。

修法：模块级 `_win_leases`（`锁文件路径 -> [文件句柄, 引用计数]`）。`acquire()` 命中共用句柄并计数 +1；`release()` 计数 -1，归零才 `LK_UNLCK` + `close()` + `os.unlink()`。POSIX 分支一行未改。

**缺陷 18（P2）—— `bigfish/cli.py` 的 restore 分支不关库。**

`restore_from_bundle()` 成功后直接 `return 0`。Linux 上进程退出即释放 fd；Windows 上「进程未退出前文件仍被占用」，于是调用方紧接着的删除/移动报 `PermissionError: [WinError 32] 另一个程序正在使用此文件`（测试 `tearDown` 的 `self.tmp.cleanup()` 就挂在这里）。

修法：restore 分支包 `try/finally` 关库。其他分支是短命只读操作，交给进程退出即可——只有恢复动的是库文件本身。

**验证**：修完后 Windows `Ran 265 tests in 59.975s / OK (skipped=1)`、退出码 0；Linux `Ran 265 tests in 41.215s / OK`。提交 `73104a8`。

---

一句话收尾：rc6 把 Astra 的九条全按住了，双平台的 265 项都是证据；Windows 那台机器还反过来咬出两个真缺陷（msvcrt 按句柄锁、restore 不关库），都修完才全绿。剩下的就是真窗口和跨设备两条——所以它是一份硬气的候选，还不是一次敢喊发布的东西。
