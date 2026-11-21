# 测试日志：bigfish-council v0.1.1-rc5

本文件是 rc5 这一批的原始工作日志，按时间顺序记：做到哪一步、跑了什么命令、看到什么输出、
踩了什么坑。只写实际发生的事；推断和预期会明确标出来。

时间全部是 UTC。仓库 `/home/dsh/bigfish-council`，分支 `fix/v0.1.1-rc4-hardening`。

---

## 1. 一句话结论

rc5 收了 9 个第三方审计项（R1–R9），全量从 rc4 的 162 项涨到 **244 项**，平台 A 全绿；
selfcheck 16 项、gui_smoke 9 项全过；pyflakes 零输出、compileall 退出码 0。
平台 B（Windows）本批次没跑，244 项在那边是「未验证」。

---

## 2. 时间线

### 2.1 开工：把 rc4 的发布点定住

    $ git log --oneline -4
    adc48a3 feat(R5+R8): 服务端下发 dispatch_id + CLI restore 拒绝在服务运行时替换库
    a4f0641 fix(R2+R3+R4+R6+R9): 事件落库、撤评论边界、锁内时间戳、审计白名单、密钥自愈
    4281114 fix(R1+R8): 恢复后作废一切远端授权 + 恢复期排空维护模式
    72e8535 release: v0.1.1-rc4 安全加固（运行所有权/恢复原子化/审查绑定/多轮执行/接口健壮/端口预留）

rc4 的发布点是 `72e8535`（2026-11-20 01:43:02）。后面所有「相对 rc4」的说法都对着它算。
先把 rc4 的用例数复算一遍，免得后面拿错基线：

    $ for f in $(git ls-tree --name-only 72e8535 tests/ | grep '\.py$'); do
        n=$(git show 72e8535:$f | grep -c "    def test_"); echo "$f $n"; done
    tests/__init__.py 0
    tests/test_api.py 6
    tests/test_core.py 15
    tests/test_v011_p0.py 15
    tests/test_v011_p1.py 24
    tests/test_v011_p2.py 24
    tests/test_v011_p3.py 19
    tests/test_v011_rc3.py 26
    tests/test_v011_rc4.py 33
    TOTAL 162

所以 rc4 是 162，不是交接里说的 164。164 是「244 − 80」倒推出来的，不是实测值。
差的那 2 项来自 p0/p3 这两个既有文件里各补的一条测试（p0 的
test_prompt_body_written_to_task_versions、p3 的 test_graceful_shutdown_on_sigint），
它们不在 7 个新文件贡献的 80 项里。先记在这里，全量口径是 162 → 244（+82）。

### 2.2 R1 + R8：恢复后作废授权、恢复期排空（4281114，07:31:23）

先改 `bigfish/backup.py` 的 `_block_inflight`，从一个「在途任务」扩成五步：

    在途 DISPATCHED/RUNNING/CANCELLING → BLOCKED
    APPROVED                            → BLOCKED（冻结，文案带「派工许可已冻结」）
    tickets                             → revoked_at 全置位
    executors                           → enabled=0 + disabled_reason
    idempotency (claim/heartbeat/submit) → 清空

再改 `bigfish/db.py` 加维护模式。这里的判据是「排空」必须真成立：只查一下有没有人在写
是不够的——写事务还没提交时，光查会拿到「没人在写」，`enter_maintenance` 返回成功，
恢复就跟在途写入撞车。所以入口用 `写锁 acquire(timeout)` 当排空屏障，拿不到就抛
`MaintenanceError`，不返回「好像进去了」。对应测试用例是
`test_enter_waits_for_inflight_transaction`，两线程 + Event 构造「持写事务时进维护」，
它要求的是「必须抛错」而不是「等一下就好了」：

    self.assertIsInstance(err.get("e"), MaintenanceError,
                          "有在途写事务时必须拒绝进入，而不是假装排空成功")

### 2.3 R2/R3/R4/R6/R9（a4f0641，07:42:43）

这一个提交里 R9 花的时间最多，因为它牵出两个方向的错误，详见第 3.2 节。

R2 的坑很隐蔽：`_recompute_review` 的 `all_approved` 分支末尾有个 `return`，
函数最后的 `log_event` 循环永远走不到，`task.review_passed` / `task.pending_approval`
两条事件从来没落过库。任务状态是对的，所以光看状态永远发现不了，只能查 events 表：

    self.assertIn("task.review_passed", kinds,
                  "双模型通过事件丢了：审计只看事件表会以为任务没通过审查")
    self.assertIn("task.pending_approval", kinds)

R3 是「撤一条普通评论也能把已批准任务打回 IN_REVIEW」。修法是 `if kind != "vote": task_row = None`。
测试必须双向写，否则容易修过头（把撤真票也一起失效了）：

    # 撤评论：状态不许动
    self.c.revoke_review(cid)
    self.assertEqual(self.c.get_task(tid)["status"], S.DISPATCHED)
    # 撤真票：必须照旧打回
    self.c.revoke_review(vid)
    self.assertEqual(self.c.get_task(tid)["status"], S.IN_REVIEW)

### 2.4 R5 + R8 的 CLI 侧（adc48a3，07:49:30）

R5 服务端侧加 `dispatch_id`（= `ticket_id`），执行端加 `ReceiptLog`。
R8 的 CLI 侧加端口探测：恢复前先探 `api_host:api_port`，有人在监听就拒绝。
真实打印出来的文案是：

    拒绝恢复：127.0.0.1:33541 上还有服务在监听，请先停掉它（tools/stop-headless.ps1 或关闭 GUI），
    确认没有其他实例后再重试；确实要强行恢复请加 --force。

这行是测试进程继承 stderr 打出来的，所以后面全量测试日志里也能看到它——不是报错，是用例
真的走到了守卫分支。

### 2.5 全量验证（07:50 左右）

    $ cd /home/dsh/bigfish-council
    $ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests -t .
    Ran 243 tests in 42.636s

    OK
    RC=0 ELAPSED_MS=43496

日志 11 行，扣掉点号和环境噪音（starlette 弃用提示两行、缺 fontconfig、offscreen 平台插件）就没什么了：

    .../site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: ...
      from starlette.testclient import TestClient as TestClient  # noqa
    ...........Fontconfig error: Cannot load default config file: No such file: (null)
    This plugin does not support propagateSizeHints()

怕 `Ran 243` 是 discovery 重复计数，又独立数了一遍用例对象：

    $ .venv/bin/python -c "..."   # 递归展开 TestSuite
    TOTAL 243

对得上。这一跑是 `a4f0641` 时的中间态，restore_authority 还只有 20 项；收尾提交 `b8f6de1`
补了第 21 项 `test_tickets_revoked_reports_real_count`，全量变成 244 项（本轮 HEAD 实测 39.178s）。
逐文件单跑 15 个，全部退出码 0，最慢的是 `test_v011_rc5_proxy`（13.262s，
要起 node 子进程）和 `test_v011_p3`（12.571s，要起真进程等 SIGINT/SIGTERM）。

    $ .venv/bin/python tools/selfcheck.py
    通过 16 项，失败 0 项
    自检全部通过，可以启动 GUI。

    $ LD_LIBRARY_PATH=... QT_QPA_PLATFORM=offscreen .venv/bin/python tools/gui_smoke.py
    [ok]   状态栏文案  API：已监听 127.0.0.1:3097
    通过 9 项，失败 0 项
    GUI 冒烟通过。

状态栏这句当时打印的是「API：已监听 127.0.0.1:3097」，rc4 时是「API：未启动」。
一开始我以为「冒烟把服务起起来了」，后来看源码才发现不对：`tools/gui_smoke.py:61` 断的是

    check("状态栏文案", isinstance(win.api_label.text(), str), win.api_label.text())

只要求取到的是字符串。文案本身取决于机器上有没有别的东西在监听，跟这条用例断什么无关。
收尾在 `b8f6de1` 上重跑时没有服务在跑，同一条就打印「API：未启动」，照样 9 项全过。
所以这句只能当「标签能取到字符串」的证据，不能当「服务起来了」的证据——
后者归 selfcheck 的健康检查那 3 项管。报告 5.3 里按这个口径写的。

    $ .venv/bin/python -m pyflakes bigfish tools tests
    （零输出，RC=0）
    $ .venv/bin/python -m compileall -q bigfish tools tests
    （无输出，RC=0）

---

## 3. 踩的坑

这一节是这份日志的重点。每条都写：当时以为是什么、实际是什么、怎么改的。
只有写清「以为」，下次才不会再踩。

### 3.1 add_review 返回的是任务快照，不是审查行

**以为：** `c.add_review(...)` 返回的是刚插进去的那条审查记录，所以 `["id"]` 就是评论 id，
拿去 `revoke_review(...)` 天经地义。

**实际：** `bigfish/core.py:454` 的 `add_review` 末尾是 `return self.get_task(tid)`——返回的是
**任务快照**。所以 `["id"]` 拿到的是任务 id（形如 `task-d2287fcbf1`），
拿它去 `revoke_review` 会直接报「审查记录不存在」。

这一点在写 `test_v011_rc5_review_audit.py` 的撤评论用例时撞上，写测试时跑出来的现场是：

    add_review 返回的键: ['blocked_reason', 'cancel_requested_at', 'control',
                          'created_at', 'current_version', 'executor_id', 'goal', 'id'] ...
    返回体里的 id = task-d2287fcbf1
    任务 id           = task-d2287fcbf1
    两者相同: True
    revoke_review(返回体 id) -> 审查记录不存在
    reviews() 里筛出的 comment id = 3
    revoke_review(真评论 id) -> ok, status = PENDING_APPROVAL

**怎么改：** 不猜 id，从 reviews 里筛。这个 helper 最后留在了测试文件里，docstring 把坑写死：

    def comment_id(self, tid):
        """add_review 返回的是任务快照，不是审查行；评论 id 要从 reviews 里取。"""
        rows = [r for r in self.c.reviews(tid) if r["kind"] == "comment"
                and not r["revoked"]]
        return rows[0]["id"]

注意还要筛 `kind == "comment"`：同一任务上还有两条 `kind="vote"` 的投票，
按「第一条」取会拿到票而不是评论，撤的就不是评论了，这条测试也就测错了东西。

### 3.2 ensure_key_consistency 第一次跑走的是「补记指纹」，不是「轮换」

**以为：** 只要执行了 `ensure_key_consistency` 就该把票全吊销——毕竟 R9 的文案写的是
「恢复前的执行票全部失效」。

**实际：** 这个函数有四个出口，第一次跑（老库从 rc4 升到 rc5，meta 里还没有 `key_sha256`）
走的是 `recorded`：只是把当前密钥的 sha256 补记进 meta，**密钥一个字节都没变**。
这时候如果也去吊销票，就是把一堆本来还有效、还有执行进程正握着的票无缘无故废掉，
在途执行会被打断，而且没有任何安全收益（密钥没换，旧票本来就能用）。

四个出口的实现顺序（`adc48a3` 里的 `bigfish/backup.py:670`）：

    key 文件不存在      -> new-key（重建密钥）
    meta 无 key_sha256  -> recorded（补记指纹，rotate=False）
    指纹一致            -> consistent（什么都不做）
    指纹不一致          -> rotated（真轮换）

`_heal_key`（同一提交的 `bigfish/backup.py:693`）里只有 `if rotate:` 才动 tickets 与 idempotency：

    if rotate or not key_path.exists():
        key = rotate_key(key_path)
    if rotate:
        ... UPDATE tickets SET revoked_at ... / DELETE FROM idempotency ...
    # 非 rotate 时 audit detail 写「库内尚无 key 指纹（...），已补记、未改动任何执行票」

**怎么测：** 两个方向各一条，缺一不可。

补记分支要断「票没被动、执行还在继续」：

    self.assertEqual(out["action"], "recorded")
    self.assertEqual(out["tickets_revoked"], 0)
    self.assertEqual(self.c.heartbeat(p["ticket"], self.ex, "running")["task_status"],
                     S.RUNNING)

轮换分支要断「旧票真的用不了了」，而且**不许顺手改任务状态**：

    with self.assertRaises(CouncilError):
        self.c.heartbeat(old, self.ex, "running")
    self.assertEqual(status, S.RUNNING,
                     "吊销票不该顺手改任务状态；状态由看门狗/人工决定")

补记与轮换在测试里的区分办法很朴素：轮换用例开头先主动跑一次
`ensure_key_consistency`（把指纹补记上），再 `key_path.write_bytes(...)` 改坏密钥，
这样第二次调用才会真的走到 `rotated`（`tests/test_v011_rc5_key_heal.py:80` 那行注释就是
「先建立指纹」）。少了这一步，改密钥那一次仍然是首次调用，走的还是 `recorded`，
`assertEqual(out["action"], "rotated")` 会直接红——两个分支在同一个入口上，只有
meta 里有没有指纹能把它们分开。

### 3.3 CLI restore 的 --force 测试，第一版还是恒真

**以为：** 守卫测试写「带 `--force` 能解析、能跑到那一步」就是测到了。

**实际：** 那还是恒真。`--force` 被 argparse 收下这件事，任何一次不报错的 `parse_args`
都能证明；把 `cli.py` 里 `if not a.force and _port_busy(...)` 整段删掉，这条断言照样过。

**怎么改：** 反过来断——带 `--force` 时必须**越过**端口闸门、走到读包那步、
拿到 `RestoreError`；如果 `--force` 只是被收下而没生效，代码会停在 `return 2`，
`assertRaises` 就抓不到异常。测试里把这句话直接写进 docstring：

    def test_force_bypasses_the_guard(self):
        """--force 必须真的放行：端口占着也继续往下走，走到读包那步才失败。

        断言方式是「拿到的是 RestoreError 而不是返回码 2」——如果 --force 只是
        被 argparse 收下但没生效，这里会拿到 2。
        """
        with self.assertRaises(RestoreError):
            self._run("--force")

反面的拒绝用例则要断具体返回码，不能只断「有输出」：

    rc = self._run()
    self.assertEqual(rc, 2, "端口被占用时 CLI 仍然继续恢复")

### 3.4 tools.edit 的 old_string 必须与磁盘逐字一致

**以为：** 改一处断言，把旧那几行贴进 `old_string` 就行，缩进差一点、注释差一行应该也能匹配。

**实际：** 差一行就 `not found`。这轮改 `tests/test_v011_p0.py` 那条恒真断言时就被卡了一次：
把 `assertNotIn(b"SECRET-PROMPT-BODY", blob.replace(...` 那段连同上面两行上下文一起贴进去，
但磁盘上中间还夹着一行 `p = self.c.claim(tid, self.ex, idem_key="k1")`，没带，直接匹配不上。
多行 `old_string` 是按字节比对的，不是模糊匹配。

**怎么改：** 先用 `read` 把目标区间原文读出来（带行号），按行号区间复制，
再整体替换。多行改动宁可换成「整段重写这个函数」的一次 write，也不要靠记忆拼 old_string。
后面几次改动都是先读后写，没再撞。

---

## 4. 本轮没跑的东西

- Windows 11 / Python 3.14.6：244 项**未验证**。`test_v011_p3.py` 的 SIGINT 用例在
  Windows 上是 `skipIf` 跳过，`backup._restore_lock` 的 `msvcrt.locking` 分支在 POSIX 上跑不到。
- GUI 真窗口：`gui_smoke.py` 用 `offscreen`，只证明窗口能构造、页面能切、状态栏标签取得到字符串。
  恢复按钮的交互与 `pause_services/resume_services` 在真事件循环里的时序没点过。
- 跨机：没在第二台机器上跑。node 只验过 v22.23.2 一个版本。
- pyflakes / compileall 只在平台 A 跑过。

这些都写进了 `docs/TEST-REPORT-v0.1.1-rc5.md` 第 6 节。

---

## 5. 提交与工作区状态

    adc48a3  2026-11-20 07:49:30  feat(R5+R8): 服务端下发 dispatch_id + CLI restore 拒绝在服务运行时替换库
    a4f0641  2026-11-20 07:42:43  fix(R2+R3+R4+R6+R9): 事件落库、撤评论边界、锁内时间戳、审计白名单、密钥自愈
    4281114  2026-11-20 07:31:23  fix(R1+R8): 恢复后作废一切远端授权 + 恢复期排空维护模式
    b8f6de1  2026-11-20 08:20:28  release: v0.1.1-rc5 恢复后授权作废与恢复期排空（R1-R9 + 244 项测试 + 八份 rc5 文档）

    $ git status --short -- bigfish tests tools
    （空）

代码与测试全部已提交，三个代码目录在工作区里是干净的。
不限目录的 `git status --short` 同一时刻列出来的是文档批次，不在上面这个提交范围里：

    M docs/DELIVERY-v0.1.1-rc5.md
    M docs/DIFF-SUMMARY-v0.1.1-rc5.md
    M docs/FIX-MATRIX-v0.1.1-rc5.md
    M docs/TEST-REPORT-v0.1.1-rc5.md
    M docs/TESTLOG-v0.1.1-rc5.md
    ?? docs/MIGRATION-v0.1.1-rc5.md
    ?? docs/ROLLBACK-v0.1.1-rc5.md

这七条全部是文档；`bigfish/`、`tests/`、`tools/` 三个目录是空的。
本轮只写文档，没有改代码、没有 commit。

最后那个 `b8f6de1` 是本批次收尾时打的发布提交，相对 `adc48a3` 只多两处：
`bigfish/backup.py` 的 `tickets_revoked` 改成真实 rowcount，
以及 `tests/test_v011_rc5_restore_authority.py` 补的那一条用例（20 → 21 项）。
本文件全部数字按 HEAD `b8f6de1` 的 244 项写；`adc48a3` 上的 243 项只作收尾前的中间态提及。

---

## 6. 附录：本文件写作时的核对命令

写完报告后为了让数字可复算，又跑了一遍（结果与上面一致）：

    $ .venv/bin/python -c "import platform, sys, sqlite3; print(platform.platform()); ..."
    Linux-5.15.0-187-generic-x86_64-with-glibc2.36
    3.11.2
    3.40.1
    $ node -v
    v22.23.2
    $ .venv/bin/python -m pyflakes --version
    4.1.0 Python 3.11.2 on Linux
    $ git diff --stat 72e8535 -- bigfish tests tools
    20 files changed, 2251 insertions(+), 60 deletions(-)      # adc48a3

日志文件留在 `/home/dsh/bfc-rc5-logs/`：`unittest-linux.txt`（244 项，930 字节）、`selfcheck.txt`、`gui_smoke.txt`、`pyflakes.txt`、`compileall.txt`。

### 6.1 补记：收尾那一处改动（已提交进 `b8f6de1`）

写完正文后 `bigfish/backup.py` 又动了一处：`tickets_revoked` 从常量 1 改成真实
rowcount 并透传到恢复结果，`tests/test_v011_rc5_restore_authority.py` 补了
`test_tickets_revoked_reports_real_count`。这处改动后来被提交进 `b8f6de1`。
提交后在同一棵树上重跑：

    $ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests -t .
    Ran 244 tests in 39.178s
    OK
    RC=0

    $ .venv/bin/python -m pyflakes bigfish tools tests        （零输出，RC=0）
    $ .venv/bin/python -m compileall -q bigfish tools tests    （无输出，RC=0）
    $ .venv/bin/python tools/selfcheck.py                      （通过 16 项，失败 0 项，RC=0）
    $ ... tools/gui_smoke.py                                   （通过 9 项，失败 0 项，RC=0）

多出来的那 1 项就是这条新用例（该文件 20 项变 21 项、338 行变 360 行）。
本文件口径是 244 项 / 15 个文件（HEAD `b8f6de1`）。收尾提交之前 `adc48a3` 上是 243 项，差额只有这一条。
两者不冲突，差额只有这一条。

顺带核到一个细节：`gui_smoke.py` 的状态栏那条打印的东西会随环境变。
本批次跑全量时机器上有服务在监听，打印 `API：已监听 127.0.0.1:3097`；补记这次没服务在跑，
打印 `API：未启动`。两次都是 9 项全过——因为 `tools/gui_smoke.py:61` 断的是
`isinstance(win.api_label.text(), str)`，只证明标签取得到字符串，不证明服务起来了。
这一点在报告 5.3 里写清楚了。
