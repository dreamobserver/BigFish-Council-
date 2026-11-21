# 测试日志：bigfish-council v0.1.1-rc6

按时间顺序记：做到哪一步、跑了什么命令、看到什么输出、踩了什么坑。
只写实际发生的事；推断和预期会明确标出来。
时间统一 UTC，日均为 2026-11-20。命令里的路径一律写绝对路径，方便照抄。
凡是「作者侧修复」的动作（提交时间取自 `git log`），都标明是事后核对所得，
不是这份日志的作者当场敲的；其余每一条都是本轮实跑。

---

## 1. 一句话结论

平台 A（Debian 12 / Python 3.11.2）在 HEAD `ae2fe82` 上跑到
**Ran 265 tests / OK / 退出码 0**（四次全量，耗时 50.226s / 52.690s / 73.287s / 74.674s），
16 个文件逐文件单跑全绿，selfcheck 16/16、gui_smoke 9/9、pyflakes 零输出、compileall 退出码 0。
Windows 侧一次没跑，265 项在平台 B 上是「未验证」。

中途踩了 6 个坑，其中三个是**本轮修复过程中真实踩到又修掉的**（租约句柄刷 `ResourceWarning`、
`accept_loop` 的 `OSError [Errno 9] Bad file descriptor` 刷栈、pyflakes 报两个未用导入），
另外三个是**验证过程本身的方法坑**（模块名写法、缺 `LD_LIBRARY_PATH` 静默跳过、
`tracemalloc` 插桩把 5 项后台进程用例拖红）。第 3 节逐条写清。

---

## 2. 时间线

### 2.1 定基线：rc5 发布点与 rc6 的三个提交（15:30 前后）

先确认起点的提交号。rc5 的发布提交是 `b8f6de1`（分支上的 tag 点），GitHub main 上另有一个
`c6d663d`（同内容的发布提交，时间更晚，不是本分支的祖先，只作对照）：

    $ cd /home/dsh/bigfish-council
    $ git log -1 --format='%h %ad %s' --date=iso b8f6de1
    b8f6de1 2026-11-20 08:20:28 +0000 release: v0.1.1-rc5 恢复后授权作废与恢复期排空（R1-R9 + 244 项测试 + 八份 rc5 文档）

    $ git log -1 --format='%h %ad %s' --date=iso c6d663d
    c6d663d 2026-11-20 08:57:38 +0000 v0.1.1-rc5: R1-R9 恢复后授权作废与恢复期排空（244 项测试全绿）

rc6 的三个提交（作者侧动作，时间取自 `git log`；本日志作者事后核对）：

    $ git log -3 --format='%h AD=%ad CD=%cd %s' --date=iso
    ae2fe82 AD=2026-11-20 15:48:11 +0000 CD=2026-11-20 15:48:11 +0000 fix(rc6): 处理 Astra 审计 F-01~F-09 …
    f1d1ce0 AD=2026-11-20 17:28:16 +0800 CD=2026-11-20 15:30:19 +0000 fix: surface key consistency startup failure cleanly
    cbf4d89 AD=2026-11-20 17:26:39 +0800 CD=2026-11-20 15:30:19 +0000 fix: harden rc5 restore and startup safety

`cbf4d89` / `f1d1ce0` 的作者时区是 +0800，committer 时间 15:30:19 UTC——它们是 Astra 在
`review/rc5-hardening-20261120` 分支上写、cherry-pick 进来的。`ae2fe82` 是本轮主体。

两条 cherry-pick 的规模（真实输出）：

    $ git show --stat --oneline cbf4d89
    cbf4d89 fix: harden rc5 restore and startup safety
     AUDIT_REVIEW.md                          | 122 ++++++++++++++++++
     bigfish/backup.py                        |   8 +-
     bigfish/cli.py                           |  22 +++---
     bigfish/db.py                            |  34 ++++-----
     bigfish/runtime.py                       |   9 ++-
     tests/test_v011_rc5_cli_guard.py         |   9 +++
     tests/test_v011_rc5_key_heal.py          |  15 ++++
     tests/test_v011_rc5_restore_authority.py |  64 ++++++++++++++++
     8 files changed, 245 insertions(+), 38 deletions(-)

    $ git show --stat --oneline f1d1ce0
    f1d1ce0 fix: surface key consistency startup failure cleanly
     bigfish/runtime.py | 4 ++--
     1 file changed, 2 insertions(+), 2 deletions(-)

`f1d1ce0` 只改两行，但很关键：`runtime.py` 里抛的异常类型从 `RuntimeError` 收成 `CouncilError`，
否则 CLI 顶层的 `except (CouncilError, RestoreError, DatabaseVersionError)` 接不住，
退出码 2 那条路径会变成未捕获异常。

### 2.2 本轮修复过程中踩到的三个坑（作者侧，事后按代码与日志回溯）

这三条是本轮真踩到又真修掉的，不修的话全量日志会脏到没法看。
时间点在 `ae2fe82` 之前（15:48 之前），具体时刻没有留存，这里只记顺序。

**(1) 租约句柄泄漏 → `ResourceWarning: unclosed file`。**
`bigfish/db.py` 把 `self._lease = WriterLease(self.path)` 提到三条 PRAGMA 之前（现在是 `:238`）后，
如果后面的版本校验抛错，flock 句柄会跟着对象一起等 GC，全量就刷
`ResourceWarning: unclosed file <_io.BufferedRandom name='.../council.db.access.lock'>`。
改法是把这整段包进 `try/except BaseException: self._lease.release(); raise`（现在是 `:254` 的 `try` → `:271` 的 `release()` → `:274` 的 `raise`）。
现在剩余的这一条来自另一条路径（CLI 的 `--force` 恢复失败路径），第 3.3 节单独写。

**(2) `accept_loop` 在 `srv.close()` 之后调 `settimeout` → `OSError [Errno 9] Bad file descriptor` 刷栈。**
`tests/test_v011_rc5_cli_guard.py` 的旧写法是线程里第一行 `srv.settimeout(0.2)`，
而主线程收尾会先 `srv.close()`，于是这对竞态在收尾时刷栈。改法见 3.1。

**(3) pyflakes 报两个未用导入（`tests/test_v011_rc6.py`）。**
新文件第一版带进来 `from bigfish import state as S` 和 `import os as _os`，两个都没用到。
`pyflakes bigfish tools tests` 直接报两行，退出码 1。删掉即可。复现与机制见 3.2。

### 2.3 全量首跑：50.226s（约 15:45）

确认 HEAD 和工作区后第一次跑全量：

    $ git rev-parse --short HEAD
    ae2fe82
    $ git status --short -- bigfish tests tools
    （空）

    $ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests -t .

    Ran 265 tests in 50.226s

    OK

退出码 0。这一次是「先确认能跑通」，日志没单独留存。

### 2.4 带 `-v` 再跑一遍，逐项留证（15:52:50）

为了逐项核对「265 项全部真跑过」，跑 `-v` 并把输出存盘：

    $ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests -t . \
        > /home/dsh/bfc-rc6-logs/unittest-verbose.txt 2>&1

    $ grep -n 'Ran ' /home/dsh/bfc-rc6-logs/unittest-verbose.txt
    316:Ran 265 tests in 52.690s

    $ grep -c 'ok$' /home/dsh/bfc-rc6-logs/unittest-verbose.txt
    265

    $ grep -n 'skipped' /home/dsh/bfc-rc6-logs/unittest-verbose.txt
    230:test_bad_line_is_skipped_not_fatal (tests.test_v011_rc5_receipts.TestReceiptLog.test_bad_line_is_skipped_not_fatal) ... ok

265 行以 `ok` 结尾，唯一的 `skipped` 字样出现在用例名里，不是 skip 计数。
本进程 0 skip、0 expected failure。文件 318 行 / 36847 字节。

### 2.5 `tracemalloc` 插桩跑全量：494.169s，5 项假失败（15:56–16:03:38）

为了给 `ResourceWarning` 找分配栈，加了 `PYTHONTRACEMALLOC=15` 跑全量：

    $ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
        PYTHONTRACEMALLOC=15 .venv/bin/python -m unittest discover -s tests -t . \
        > /tmp/rc6-trace.log 2>&1

    $ grep -n 'Ran \|^FAILED' /tmp/rc6-trace.log
    100:Ran 265 tests in 494.169s
    102:FAILED (failures=5)

5 个失败全在 `tests/test_v011_p3.py.TestBackgroundProcess`，全是同一条断言：

    42:FAIL: test_clean_shutdown          ...
    53:FAIL: test_data_dir_created        ...
    64:FAIL: test_graceful_shutdown_on_sigint ...
    76:FAIL: test_stdout_redirect_does_not_block ...
    88:FAIL: test_watchdog_runs_in_headless ...

    AssertionError: 后台服务没起来：

原因：`tracemalloc` 给每一次内存分配都记账，把子进程启动拖慢到超过这些用例
`wait_health()` 的 30 秒窗口。同一个 HEAD、同一条命令、去掉 `PYTHONTRACEMALLOC` 后这 5 项全过
（2.3 与 2.6 的两次全量都是全绿）。**所以这 5 个 FAIL 是插桩导致的环境假失败，
不写进「测试失败」，也不允许拿它当回归证据。**

分配栈拿到了，末 6 帧指得很清楚：

    File ".../tests/test_v011_rc5_cli_guard.py", lineno 107
    File ".../tests/test_v011_rc5_cli_guard.py", lineno 41
    File ".../bigfish/cli.py", lineno 139
    File ".../bigfish/runtime.py", lineno 13
    File ".../bigfish/db.py", lineno 238
    File ".../bigfish/locks.py", lineno 44

（完整链条与结论见 3.3；报告 4.5 节给了同一份证据。）

### 2.6 正式全量两跑：73.287s / 74.674s（16:05:45 / 16:05:51）

日志落进正式目录：

    $ mkdir -p /home/dsh/bfc-rc6-logs
    $ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests -t . \
        > /home/dsh/bfc-rc6-logs/unittest-linux.txt 2>&1
    $ echo "RC=$?"
    RC=0
    $ wc -lc /home/dsh/bfc-rc6-logs/unittest-linux.txt
      14 1381 /home/dsh/bfc-rc6-logs/unittest-linux.txt

同一份命令再存一份 `unittest-doc-run.txt`（也是 1381 字节）。`diff` 下来只有 3 行不同——
临时目录名（`/tmp/tmp7dtw6grl` vs `/tmp/tmpti51nyf2`）、随手挑到的端口（36403 vs 38881）、
以及耗时秒数；其余逐行一致：

    Ran 265 tests in 74.674s
    OK

两次都是 `OK` / 退出码 0，`FAIL:` / `ERROR:` 各 0 行。14 行日志里有 4 类内容：

    StarletteDeprecationWarning（2 行）               环境噪音（httpx 弃用提示）
    Fontconfig error（1 行）+ propagateSizeHints（1 行）  offscreen 环境噪音
    拒绝恢复：127.0.0.1:3096 / 36403（2 条）             CLI 守卫两条用例的预期输出（一条打桩走默认 3096，一条真起 socket 走临时端口）
    ResourceWarning unclosed ...access.lock（3 行）      已知测试侧句柄提醒，见 3.3

### 2.7 逐文件单跑 16 次（16:08–16:12:02）

先记一个**走错的写法**（3.4 节详述）：想用模块名跑，写成了缩写 `tests.api`，16 个文件全红：

    $ for m in api core v011_p0 ... ; do .venv/bin/python -m unittest tests.$m; done
    ===== tests.api  RC=1
    Ran 1 test in 0.000s
    FAILED (errors=1)
    ...（16 条全是这样）

单看 `Ran 1 test` 就该警觉——真跑的话不可能只有 1 项。改成 `discover -p` 之后才对：

    $ for p in test_api.py test_core.py ... test_v011_rc6.py; do
        LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
          .venv/bin/python -m unittest discover -s tests -p "$p" -t .
      done > /home/dsh/bfc-rc6-logs/perfile.txt

16 次全部 `OK` / 退出码 0：

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

顺带记一个反常：16 次单跑的耗时加总是 68.066s，比全量的 73.287s **少** 约 5.2 秒，
和 rc5 报告里「加总大于全量」的结论相反。原因是全量里 `test_v011_p3.py`（后台进程 + 健康轮询）
与 `test_v011_rc5_proxy.py`（起 node）串行叠加，等待被拉长；单跑时每个文件都是全新进程。
耗时列只能看「谁慢」，项数才是硬数字。

### 2.8 selfcheck 与 gui_smoke（16:06:14 / 16:06:17，16:07:26 / 16:07:32）

    $ .venv/bin/python tools/selfcheck.py > /home/dsh/bfc-rc6-logs/selfcheck.txt 2>&1
    $ echo "RC=$?"
    RC=0
    $ tail -2 /home/dsh/bfc-rc6-logs/selfcheck.txt
    通过 16 项，失败 0 项
    自检全部通过，可以启动 GUI。

16 条全 `[ok]`，分区是健康检查 3 / MCP 协议 3 / 闭环 7 / 权限 3。日志 24 行 / 715 字节。

    $ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
        .venv/bin/python tools/gui_smoke.py > /home/dsh/bfc-rc6-logs/gui_smoke.txt 2>&1
    $ echo "RC=$?"
    RC=0
    $ tail -2 /home/dsh/bfc-rc6-logs/gui_smoke.txt
    通过 9 项，失败 0 项
    GUI 冒烟通过。

9 条：主窗口构造成功、五个页面都在（`count=5 keys=5`）、五个页面可切换、状态栏文案、待审批任务被识别。
状态栏这条打印的是 `API：未启动`——`tools/gui_smoke.py:61` 只断
`isinstance(win.api_label.text(), str)`，**不证明服务起来了**，别当成一条额外证据。

两次都复跑过（`-doc-run.txt` 与正式名两套），项数完全一致。

### 2.9 静态检查（16:07:34）

    $ .venv/bin/python -m pyflakes bigfish tools tests > /home/dsh/bfc-rc6-logs/pyflakes.txt 2>&1
    $ echo "PYFLAKES_RC=$?"
    PYFLAKES_RC=0
    $ wc -c /home/dsh/bfc-rc6-logs/pyflakes.txt
    14 /home/dsh/bfc-rc6-logs/pyflakes.txt

14 字节就是那行 `PYFLAKES_RC=0` 本身，**pyflakes 自身零输出**。

    $ .venv/bin/python -m compileall -q bigfish tools tests > /home/dsh/bfc-rc6-logs/compileall.txt 2>&1
    $ echo "COMPILEALL_RC=$?"
    COMPILEALL_RC=0

同样，16 字节就是那行 RC 记录，`compileall` 自身无输出。
版本：`pyflakes --version` → `4.1.0 Python 3.11.2 on Linux`。

### 2.10 计数对账（16:12 之后）

    $ .venv/bin/python -c "
      import unittest
      s = unittest.TestLoader().discover('tests', top_level_dir='.')
      f = lambda x: sum(f(t) for t in x) if isinstance(x, unittest.TestSuite) else 1
      print('TOTAL', f(s))"
    TOTAL 265

    $ for f in tests/test_*.py; do printf "%-42s %s\n" "$f" "$(grep -c '^    def test_' $f)"; done
    tests/test_api.py                          6
    tests/test_core.py                         15
    tests/test_v011_p0.py                      16
    tests/test_v011_p1.py                      24
    tests/test_v011_p2.py                      24
    tests/test_v011_p3.py                      20
    tests/test_v011_rc3.py                     26
    tests/test_v011_rc4.py                     33
    tests/test_v011_rc5_cli_guard.py           4
    tests/test_v011_rc5_dispatch_id.py         3
    tests/test_v011_rc5_key_heal.py            8
    tests/test_v011_rc5_proxy.py               9
    tests/test_v011_rc5_receipts.py            24
    tests/test_v011_rc5_restore_authority.py   24
    tests/test_v011_rc5_review_audit.py        13
    tests/test_v011_rc6.py                     16

加总 265。再拿 rc5 提交上的旧值核一遍增量（直接在 `b8f6de1` 上数，不抄表）：

    $ git show b8f6de1:tests/test_v011_rc5_restore_authority.py | grep -c '^    def test_'
    21
    $ git show b8f6de1:tests/test_v011_rc5_cli_guard.py | grep -c '^    def test_'
    3
    $ git show b8f6de1:tests/test_v011_rc5_key_heal.py | grep -c '^    def test_'
    7

    244 + 16（新文件）+ 3 + 1 + 1 = 265 ✓

### 2.11 复现报告里那条「安静跳过」（16:22:36）

写报告 3.1.4 节时先怀疑「缺 `LD_LIBRARY_PATH` 会不会静默跳过 GUI 那 3 项」，当场验：

    $ .venv/bin/python -m unittest tests.test_v011_rc6
    Ran 13 tests in 1.959s
    OK (skipped=1)

    （skip 原因：libGL.so.1: cannot open shared object file）

    $ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest tests.test_v011_rc6
    Ran 16 tests in 6.252s
    OK

**13 项报 OK** 这件事本身就是坑：`setUpClass` 在导入 PySide6 失败时
`raise unittest.SkipTest(...)`（`tests/test_v011_rc6.py:204-209`），整个类被跳过，
最后的 `OK` 照样打印。所以这个文件的正确项数是 16，不是 13；报 13 就该查库路径。

另外顺手验了 `OSError [Errno 9]` 的机制（3.1 节要用），两条命令：

    $ .venv/bin/python /tmp/settimeout_after_close.py
    settimeout after close -> OSError [Errno 9] Bad file descriptor

    $ .venv/bin/python -m unittest tests.test_api
    Ran 6 tests in 1.733s
    OK

前一条是自造的最小复现（`socket` 先 `close()` 再 `settimeout()`），后一条确认
完整的模块名 `tests.test_api` 是能跑的——所以 2.7 那 16 个 RC=1 真的是名字写错。

### 2.12 一个没跑完的后台任务（16:21:55，起得比 2.11 略早）

为了给 `tracemalloc` 结论补一份「正式目录里的日志」，又起了一次插桩全量（后台任务
`bash-a3828ae9678f44ff`，输出 `unittest-tracemalloc.txt`）。跑到第 5 行时已经出现 4 个 `F`：

    ......................................................................................FFFF

这和 2.5 的结论一致（插桩必然打红 p3 的后台进程用例），继续跑只会重复同一份噪音，
于是主动终止了它：

    $ job_kill(bash-a3828ae9678f44ff)   →   cancellation-requested

留存的 `unittest-tracemalloc.txt` 只有 `wc -lc` 报的 4 行、483 字节（末行无换行，数下来 5 段文字），**不是一次完整运行**，
引用它时只能引用那 4 个 `F` 的位置，不能当耗时或项数证据。有效的那次是 2.5 的 `/tmp/rc6-trace.log`。

---

## 3. 踩的坑

六条，每条按固定三段式：**以为 / 实际 / 怎么改**。
前三条是本轮修复过程中真踩到的（1、2 已修，3 只记录未修）；后三条是验证方法的坑。

### 3.1 `accept_loop` 在 `srv.close()` 之后调 `settimeout` → `[Errno 9]` 刷栈

**以为：** 线程里的 `srv.settimeout(0.2)` 只是设个超时，什么时候调都行，最坏是个无效设置。

**实际：** 主线程收尾先 `srv.close()`，线程此时调 `settimeout` 会直接抛
`OSError: [Errno 9] Bad file descriptor`。因为异常发生在 `threading.Thread` 的 `run` 里、
没人接，`threading` 的默认 `excepthook` 会把整段栈打到 stderr，全量日志就脏了。
最小复现（`/tmp/settimeout_after_close.py`）：

    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1); s.close()
    s.settimeout(0.2)

    → settimeout after close -> OSError [Errno 9] Bad file descriptor

**怎么改：** 把 `settimeout` 提到循环外，并且 `try/except OSError: return`——
「socket 已经被关掉」在收尾阶段是正常状态，不该刷栈：

    def accept_loop():
        # settimeout 在 srv.close() 之后调用会 EINVAL/EBADF；先设好再进循环，
        # 并且把 OSError 一律当成「已经被关掉」正常退出（测试收尾不该刷栈）。
        try:
            srv.settimeout(0.2)
        except OSError:
            return
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn.close()
            except socket.timeout:
                continue
            except OSError:
                return

现在在 `tests/test_v011_rc5_cli_guard.py:51-57`。改完 `test_v011_rc5_cli_guard.py` 单跑
4 项全绿、耗时 0.280s，全量日志里也没有这行栈了。

### 3.2 pyflakes 报 `tests/test_v011_rc6.py` 两个未用导入

**以为：** 新文件是从既有用例拼出来的，import 段照抄就行，多两个导入无伤大雅。

**实际：** `pyflakes bigfish tools tests` 直接两行、退出码 1：

    'os as _os' imported but unused
    'bigfish.state as S' imported but unused

用一份临时副本复现了这两条（把两个导入加回去，再跑 pyflakes），行号落在 `:8` 和 `:21`，
退出码 1。这也说明「零输出」不是自带的，是删干净之后才有的。

**怎么改：** 删掉 `from bigfish import state as S` 与 `import os as _os`。
现在 `tests/test_v011_rc6.py:5-25` 的 import 段没有多余项，pyflakes 恢复零输出。

### 3.3 租约文件句柄没关 → `ResourceWarning`（记录，本轮未修）

**以为：** 构造函数里的租约泄漏（2.2 第 (1) 条）修掉之后，`ResourceWarning` 应该彻底消失。

**实际：** 全量日志里还剩一条，而且名字是临时库的 `council.db.access.lock`。
用 `PYTHONTRACEMALLOC=25` 单跑 `test_v011_rc5_cli_guard.py` 拿到分配栈，末 6 帧：

    tests/test_v011_rc5_cli_guard.py:107   →  self._run("--force")
    tests/test_v011_rc5_cli_guard.py:41    →  return cli.main(["--data-dir", ..., "restore", ...])
    bigfish/cli.py:139                     →  db, council = open_council(s)
    bigfish/runtime.py:13                   →  db = Database(settings.db_path)
    bigfish/db.py:238                       →  self._lease = WriterLease(self.path)
    bigfish/locks.py:44                     →  self.acquire()
    bigfish/locks.py:51                     →  fh = open(target, "a+b")

也就是 `test_force_bypasses_the_guard` 走 `--force` 把库打开了，然后靠
`assertRaises(RestoreError)` 结束——`cli.main()` 的 `db` 是局部变量，
函数里没有 `finally: db.close()`，于是句柄跟着对象等 GC，解释器收尾打 Warning。

**怎么改（本轮没做）：** 在 `cli.main` 的 restore 分支加 `try/finally: db.close()`。
本轮只做文档与验证，不动 `bigfish/`，理由写在报告第 6 节。

**这条的定位要拿准：** 它证明「CLI 的失败路径没显式关库」，**不**证明「租约泄漏会挡住别的进程」。
正常路径的释放有专门用例兜着（`test_failed_constructor_does_not_leak_lease`、
`test_lease_is_released_on_close`），265 项全绿也说明没有功能失败。
下一轮若开启 `-W error::ResourceWarning`，全量会因这一条变红——那时就得修。

### 3.4 `python -m unittest tests.<缩写>` 全红，看着像回归其实是名字错

**以为：** `tests/test_v011_rc6.py` 里的 `test_rc6` 太长，写 `tests.api`、`tests.core`
这种缩写更省事。

**实际：** 16 个文件全部 `RC=1`，每个都打印 `Ran 1 test in 0.000s / FAILED (errors=1)`。
完整报错：

    ImportError: Failed to import test module: api
    ModuleNotFoundError: No module named 'tests.api'

`tests/__init__.py` 只是个空包标记，模块名必须与文件名一致（`tests.test_api`）。
**最容易被误读的地方**：`Ran 1 test` 加一个 `FAILED` 很像「某个用例挂了」，
实际连模块都没导入进来。

**怎么改：** 两种写法都对——完整模块名 `.venv/bin/python -m unittest tests.test_api`，
或者 `discover -p test_api.py -t .`。本轮的逐文件表用的是后者（`perfile.txt`）。

### 3.5 缺 `LD_LIBRARY_PATH` 时 3 项 GUI 用例被静默跳过，仍然打印 OK

**以为：** `LD_LIBRARY_PATH=/home/dsh/.local/libbfc` 只是给 offscreen 起界面用的，
单跑某个文件不跑 GUI 就不需要。

**实际：** `tests/test_v011_rc6.py` 少了它只跑 13 项：

    Ran 13 tests in 1.959s
    OK (skipped=1)

原因是 `setUpClass` 在 `from PySide6.QtWidgets import QApplication` 失败时
`raise unittest.SkipTest(...)`，整个 `TestGuiPauseConfirms` 类被跳过，
而最后那句 `OK` 一个字都不变。

**怎么改：** 逐文件表和全量一律带上 `LD_LIBRARY_PATH` 与 `QT_QPA_PLATFORM`，
并且**对项数**（`test_v011_rc6.py` 必须是 16），不只看 OK。这类「安静缺口」
只能靠项数发现。

### 3.6 `tracemalloc` 插桩把 5 项后台进程用例拖红

**以为：** 加个 `PYTHONTRACEMALLOC` 只是多花点内存和时间，结论应该和正常跑一致。

**实际：** `Ran 265 tests in 494.169s / FAILED (failures=5)`，5 项全在
`test_v011_p3.py.TestBackgroundProcess`，失败信息都是 `AssertionError: 后台服务没起来：`。
`tracemalloc` 让子进程启动慢到超过用例的 30 秒健康检查窗口，属于插桩副作用。

**怎么改：** 这份 494 秒的日志只用来取分配栈（2.5 / 3.3），**不当测试结果**。
正式结论一律取不带插桩的那几次（50.226s / 52.690s / 73.287s / 74.674s，全 OK）。
再补一条纪律：插桩跑之前先想清楚「这次要取什么」，取完就不要再引它的项数和耗时。

---

## 4. 本轮没跑的东西

明确列出，避免下一轮有人拿本日志当「全都验过」。

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| Windows 平台 B 全量 | **未跑** | 265 项在 Windows 11 / Python 3.14.6 上没有证据 |
| `locks.py` 的 `msvcrt` 分支 | **未跑** | POSIX 侧 6 项已测；Windows 侧 `_probe_other_writers` 一次没执行 |
| GUI 真窗口验收 | **未做** | 只有 offscreen smoke（9 项）+ 单测（3 项），没在真窗口点过恢复按钮 |
| 真 15 秒停服超时 | **未做** | 单测用的是 `timeout=0.05` + `sleep(3)` 的假线程 |
| 跨设备 MCP 闭环 | **未做** | 只有同机 selfcheck 的 7 条闭环；rc2 的 11/11 不算本轮 |
| 两个独立进程同时恢复 | **未做** | 租约用例用「同进程再开一个 `Database`」模拟 |
| pyflakes / compileall on Windows | **未跑** | 只覆盖平台 A |
| `ResourceWarning` 修复 | **未做** | 只定位到根因（3.3），代码未改 |

---

## 5. 提交与工作区状态

HEAD 是 `ae2fe82`（`fix(rc6): 处理 Astra 审计 F-01~F-09…`），代码与测试目录干净：

    $ git status --short -- bigfish tests tools
    （空）

整个工作区有 6 项，全在文档侧，与本次测试结论无关：

    $ git status --short
     M AUDIT.md
     M CHANGELOG.md
     M README.md
    ?? docs/DELIVERY-v0.1.1-rc6.md
    ?? docs/DIFF-SUMMARY-v0.1.1-rc6.md
    ?? docs/FIX-MATRIX-v0.1.1-rc6.md

（加上本日志与报告，docs/ 下会再多两个未跟踪文件；本轮没有 commit、没有 push，
也不改 `bigfish/` / `tests/` / `tools/` 下的任何文件。）

相对 rc5 发布点的改动规模：

    $ git diff --stat b8f6de1 ae2fe82 -- bigfish tests tools
    13 files changed, 817 insertions(+), 133 deletions(-)

其中 `bigfish/locks.py` 是新增的 157 行，`tests/test_v011_rc6.py` 是新增的 280 行。

日志与产物清单（`/home/dsh/bfc-rc6-logs/`）：

    unittest-verbose.txt        36847 B   265 行用例名，Ran 265 in 52.690s
    unittest-linux.txt           1381 B   Ran 265 in 73.287s
    unittest-doc-run.txt         1381 B   Ran 265 in 74.674s
    perfile.txt                   941 B   16 个文件单跑结果
    selfcheck.txt                 715 B   16/16
    gui_smoke.txt                 462 B   9/9
    pyflakes.txt                   14 B   零输出（只有 RC 行）
    compileall.txt                 16 B   无输出（只有 RC 行）
    unittest-tracemalloc.txt      483 B   未跑完的插桩任务（4 行），只留 4 个 F 的位置

另有 `/tmp/rc6-trace.log`（6124 B，494.169s 那次完整插桩全量，含 5 个 FAIL 详情）
和 `/tmp/settimeout_after_close.py`（195 B，3.1 的最小复现）。

---

## 6. 附录：本文件写作时的核对命令

写完后要复核数字时照着敲，全部只读：

    cd /home/dsh/bigfish-council

    # 版本 / 提交 / 工作区
    .venv/bin/python -c "import bigfish; print(bigfish.__version__)"     # 0.1.1-rc6
    git rev-parse --short HEAD                                          # ae2fe82
    git status --short -- bigfish tests tools                           # 空

    # 用例总数三条独立路径
    .venv/bin/python -c "import unittest; \
      s=unittest.TestLoader().discover('tests', top_level_dir='.'); \
      f=lambda x: sum(f(t) for t in x) if isinstance(x, unittest.TestSuite) else 1; \
      print('TOTAL', f(s))"                                             # TOTAL 265
    grep -c 'Ran 265 tests' /home/dsh/bfc-rc6-logs/unittest-linux.txt    # 1
    for f in tests/test_*.py; do grep -c '^    def test_' $f; done | awk '{s+=$1} END{print s}'   # 265

    # 增量对账（rc5 旧值直接数提交，不抄表）
    git show b8f6de1:tests/test_v011_rc5_restore_authority.py | grep -c '^    def test_'   # 21
    git show b8f6de1:tests/test_v011_rc5_cli_guard.py | grep -c '^    def test_'           # 3
    git show b8f6de1:tests/test_v011_rc5_key_heal.py | grep -c '^    def test_'            # 7

    # 日志里那几类噪音各几行
    grep -c 'ResourceWarning' /home/dsh/bfc-rc6-logs/unittest-linux.txt   # 2（:6 是 unclosed file，:8 是 Enable tracemalloc）
    grep -c '拒绝恢复' /home/dsh/bfc-rc6-logs/unittest-linux.txt           # 2
    grep -c 'FAIL:\|ERROR:' /home/dsh/bfc-rc6-logs/unittest-linux.txt     # 0

    # 关键行号（改过的话这里会变）
    grep -n 'self._lease = WriterLease' bigfish/db.py                     # 238
    grep -n 'db, council = open_council(s)' bigfish/cli.py                # 139
    grep -n 'def exclusive_access' bigfish/locks.py                       # 113
    grep -n 'MAX_BODY_BYTES =\|MAX_TEXT_FIELD =' bigfish/server.py        # 38 / 40
    grep -n 'def pause_services\|def stop(' bigfish/gui.py                # 1235 / 165
    grep -n 'def test_busy_port_is_checked' tests/test_v011_rc5_cli_guard.py   # 84
    grep -n 'class TestStartupFailsClosed' tests/test_v011_rc5_key_heal.py     # 139
