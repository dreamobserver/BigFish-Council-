# 测试报告：bigfish-council v0.1.1-rc5

## 0. 一句话

平台 A（Debian 12 / Python 3.11.2）本轮实测 **Ran 244 tests / OK / 退出码 0**，耗时 39.178s；
selfcheck 16 项、gui_smoke 9 项全过，pyflakes 零输出、compileall 退出码 0。
平台 B（Windows 11 / Python 3.14.6）**本轮未跑，244 项在 Windows 上状态为「未验证」**，见第 6 节。
相对 rc4 发布点 72e8535（实测 162 项），本轮增加 82 项：7 个新增 rc5 文件贡献 80 项，
`tests/test_v011_p0.py` 与 `tests/test_v011_p3.py` 各补 1 项。

---

## 1. 环境

### 1.1 平台 A（本轮实测）

    OS        Debian GNU/Linux 12 (bookworm)，kernel 5.15.0-187-generic
    平台串    Linux-5.15.0-187-generic-x86_64-with-glibc2.36
    主机名    VPS-HOST
    Python    3.11.2（/home/dsh/bigfish-council/.venv）
    SQLite    3.40.1
    Node      v22.23.2
    pyflakes  4.1.0
    版本      bigfish.__version__ = "0.1.1-rc5"（bigfish/__init__.py:6）
    库版本    SCHEMA_VERSION = 4（bigfish/db.py:22）
    分支      fix/v0.1.1-rc4-hardening
    HEAD      b8f6de1（2026-11-20 08:20:28 +0000）
    基线      rc4 发布点 72e8535（本批 diff 与用例增量都以它为起点）
    工作区    git status --short -- bigfish tests tools 为空（代码与测试均已提交）
    GUI 依赖  LD_LIBRARY_PATH=/home/dsh/.local/libbfc、QT_QPA_PLATFORM=offscreen

本轮四个提交：

    adc48a3  2026-11-20 07:49:30  feat(R5+R8): 服务端下发 dispatch_id + CLI restore 拒绝在服务运行时替换库
    a4f0641  2026-11-20 07:42:43  fix(R2+R3+R4+R6+R9): 事件落库、撤评论边界、锁内时间戳、审计白名单、密钥自愈
    4281114  2026-11-20 07:31:23  fix(R1+R8): 恢复后作废一切远端授权 + 恢复期排空维护模式
    b8f6de1  2026-11-20 08:20:28  release: v0.1.1-rc5 恢复后授权作废与恢复期排空（R1-R9 + 244 项测试 + 八份 rc5 文档）

相对 rc4 发布点的代码与测试改动（只算 bigfish/ tests/ tools/ 三个目录）：

    git diff --stat 72e8535 -- bigfish tests tools
    20 files changed, 2276 insertions(+), 61 deletions(-)

    bigfish/__init__.py         +2
    bigfish/backup.py         +165
    bigfish/cli.py             +28
    bigfish/core.py            +99
    bigfish/db.py              +85
    bigfish/executor_client.py +231
    bigfish/gui.py             +54
    bigfish/server.py          +17
    tests/test_v011_p0.py      +31
    tests/test_v011_p1.py      +41
    tests/test_v011_p3.py      +21
    tests/test_v011_rc3.py     +12
    tools/bfc-proxy.mjs        +59
    新增 tests/test_v011_rc5_*.py 7 个：110/88/137/173/362/338/258 行，共 1466 行
    （以上逐文件数字与 2251 那行同属 `adc48a3`；在 `b8f6de1` 上是 backup.py +142/-27、
     restore_authority 测试 360 行，七个新文件合计 1488 行，总计 2276 插 61 删）

### 1.2 平台 B（Windows 11 / Python 3.14.6）

**本批次未执行。** rc5 的 244 项在 Windows 上是「未验证」，不是「已知通过」。
本报告第 2 至第 5 节的全部数字只代表平台 A。第 6 节逐条列出未覆盖项。

---

## 2. 测试总量表

### 2.1 逐文件

| 测试文件 | 本轮 | rc4（72e8535） | 本轮耗时 |
| --- | --- | --- | --- |
| tests/test_api.py | 6 | 6 | 0.282s |
| tests/test_core.py | 15 | 15 | 3.717s |
| tests/test_v011_p0.py | 16 | 15 | 0.502s |
| tests/test_v011_p1.py | 24 | 24 | 6.112s |
| tests/test_v011_p2.py | 24 | 24 | 1.002s |
| tests/test_v011_p3.py | 20 | 19 | 12.571s |
| tests/test_v011_rc3.py | 26 | 26 | 4.755s |
| tests/test_v011_rc4.py | 33 | 33 | 1.199s |
| tests/test_v011_rc5_cli_guard.py | 3 | 0 | 0.275s |
| tests/test_v011_rc5_dispatch_id.py | 3 | 0 | 0.092s |
| tests/test_v011_rc5_key_heal.py | 7 | 0 | 0.456s |
| tests/test_v011_rc5_proxy.py | 9 | 0 | 13.262s |
| tests/test_v011_rc5_receipts.py | 24 | 0 | 2.655s |
| tests/test_v011_rc5_restore_authority.py | 21 | 0 | 1.288s |
| tests/test_v011_rc5_review_audit.py | 13 | 0 | 0.335s |
| **合计** | **244** | **162** | 全量 39.178s |

单文件耗时是逐个 `python -m unittest tests.<mod>` 单跑得到的，加起来大于全量耗时，
因为进程启动与一次性建库/import 成本被重复计费，不构成矛盾。

表中 7 个 `test_v011_rc5_*` 文件合计 3+3+7+9+24+21+13 = **80** 项，全部是新增。
另外 p0 与 p3 两个既有文件各多了 1 项（15→16、19→20），这两项也是新增但不在 80 里。
restore_authority 那一行 1.288s 是 20 项时单跑的数，第 21 项并入后只看全量实测 39.178s。

### 2.2 增量口径

交接口径是「rc4 164 项 → rc5 243 项，新增 79 项」。表里 rc4 那一列填的是实测值 162，
rc5 那一列按 HEAD `b8f6de1` 的实测填 244，两者差 2，下面把账走平。

    rc5 新增 7 个文件                                80 项（3+3+7+9+24+21+13）
    p0 新增 test_prompt_body_written_to_task_versions  +1
    p3 新增 test_graceful_shutdown_on_sigint           +1
    ----------------------------------------------------------------
    162 + 80 + 2 = 244

交接口径里的 164 是「244 − 80」倒推出来的数，不是 rc4 的实测值。
rc4 的实测值是 162（rc4 测试报告第 0 节即「Ran 162 tests」，本轮用
`git show 72e8535:tests/<file> | grep -c "    def test_"` 逐文件复算也是 162）。

顺带一个容易混淆的巧合：rc5 这 8 个既有文件（6+15+16+24+24+20+26+33）在 `adc48a3` 上也正好是
164 项，因为 p0/p3 各补了一条。所以「164」既不是 rc4 的实测值，又恰好等于 rc5 既有文件之和，
两种算法都落在这个数上，更容易被当成基线。基线只认 `72e8535` 上的 162。

差额的 2 项来自 p0/p3 两个既有文件里各补的 1 条，不在 7 个新文件贡献的 80 项里。
本报告以 162 → 244（+82）为准，不把倒推值写成实测值。

### 2.3 计数自洽性

除了看 unittest 打印的 `Ran 244 tests`，另用 `TestLoader().discover` 独立把用例对象递归数了一遍：

    suite = unittest.TestLoader().discover('tests', top_level_dir='.')
    # 递归展开后 TOTAL 244

两条路径互相独立，不是同一个数字抄两遍。


### 2.4 收尾那一处改动（已并入本报告的 244 项）

本批收尾时补的一处产品改动已提交进 `b8f6de1`：

    M bigfish/backup.py                         +6  -2
    M tests/test_v011_rc5_restore_authority.py  +22  +0

内容是 `_block_inflight` 里的 `tickets_revoked` 从硬编码 `1` 改成
`UPDATE tickets SET revoked_at=?` 的 `.rowcount` 并透传到 `restore_bundle` 返回值，
用例补了 `test_tickets_revoked_reports_real_count`（断 `out["tickets_revoked"] == 2`，
且恢复后该库里 `revoked_at IS NULL` 的票数为 0，防止「常量 2 换成常量 1」也算修好）。

这一条让全量从 243 涨到 244。本报告所有数字（含 2.1 的逐文件表）一律按 HEAD `b8f6de1` 的
**244 项 / 15 个文件** 写，耗时以本轮实测 `Ran 244 tests in 39.178s / OK / 退出码 0` 为准。

---

## 3. 新增 7 个 rc5 测试文件：逐节说明

rc5 修的 9 个审计项（R1–R9）落在下面 7 个文件里。每节的写法是：这个文件测什么、
为什么这样断言（换个写法还能不能过）、以及最关键的一条断言原文。

### 3.1 tests/test_v011_rc5_cli_guard.py（3 项，R8 的 CLI 侧）

**测什么。** 库里正跑着服务时 `bfc restore` 必须拒绝替换数据库文件；`--force` 必须真的能放行。
判据是 `bigfish/cli.py:41` 的 `_port_busy(host, port, timeout=0.4)`：恢复前先探
`api_host:api_port`，有人在监听就打印中文提示并 `return 2`。

**为什么这样断言。** 这里最容易被写成恒真：`--force` 被 argparse 收下这件事，
任何一次不报错的 `parse_args` 都能证明，跟「放行」没有关系。所以带 `--force` 的那条用例
反过来断：必须越过端口闸门、走到读包那一步、在那里抛 `RestoreError`。
如果 `--force` 只是被收下而没生效，代码会停在 `return 2`，`assertRaises` 就抓不到异常。

**最关键的一条断言。** `tests/test_v011_rc5_cli_guard.py:92`：

    with self.assertRaises(RestoreError):
        self._run("--force")

配套的拒绝用例断的是具体返回码，不是「有输出」：

    rc = self._run()
    self.assertEqual(rc, 2, "端口被占用时 CLI 仍然继续恢复")

另一条 `test_port_busy_helper`（`tests/test_v011_rc5_cli_guard.py:98-106`）直接断
`cli._port_busy` 在空闲端口为假、在监听端口为真，把判据从「CLI 行为」下沉到「探测器行为」
又验证了一次——这样一旦探测器本身写错（比如把 connect 超时当成「有人在听」），
前两条用例的结论也站不住。

### 3.2 tests/test_v011_rc5_dispatch_id.py（3 项，R5 服务端侧）

**测什么。** 领取与幂等重放都必须下发 `dispatch_id`，且它等于 `ticket_id`；
重新派工（第二轮）必须是新的派工号。

**为什么这样断言。** 「下发了一个字段」不等于「这个字段能对账」。所以除存在性之外还断三件事：
领取与重放的 dispatch_id 相等（否则同一张票在执行端会变成两条派工）、
重新派工后两轮的 dispatch_id 不等（否则第二轮收据会盖掉第一轮）、
dispatch_id 与 ticket_id 一致（服务端不引入第二个真源）。

**最关键的一条断言。** `tests/test_v011_rc5_dispatch_id.py:61-65`：

    a = self.c.claim(tid, self.ex, idem_key="k1")
    b = self.c.claim(tid, self.ex, idem_key="k1")
    self.assertTrue(b.get("replayed"))
    self.assertEqual(a["dispatch_id"], b["dispatch_id"],
                     "幂等重放换了派工号，收据会被当成两次派工")

多轮用例里的反向一条：

    self.assertNotEqual(first["dispatch_id"], second["dispatch_id"],
                        "重新派工复用了同一个派工号")

### 3.3 tests/test_v011_rc5_key_heal.py（7 项，R9）

**测什么。** `ensure_key_consistency` 的四种出口：`recorded`（老库首次补记指纹）、
`consistent`（指纹一致）、`rotated`（密钥被换）、`new-key`（密钥文件丢失）。
核心是把「补记指纹」和「轮换密钥」分开——只有后者才吊销执行票。

**为什么这样断言。** R9 的原始缺陷是：文案写着「恢复前的执行票全部失效」，函数里却没碰
tickets 表，换了密钥 `token_hash` 还是旧值，旧票照样能认证。所以断言不能只看返回的 `action`，
必须真的拿一张旧票去认证一次。反方向也要守住：补记指纹时若顺手吊销票，会把正在跑的执行
无故打断，所以 `recorded` 分支要断 `tickets_revoked == 0` 且旧票还能心跳。

**最关键的一条断言。** `tests/test_v011_rc5_key_heal.py:97-101`（这条就是上一轮漏的）：

    self.key_path.write_bytes(b"\xff" * 32)
    ensure_key_consistency(self.db, self.key_path)
    with self.assertRaises(CouncilError):
        self.c.heartbeat(old, self.ex, "running")

并且吊销票不许顺手改任务状态：

    status = self.c.get_task_status_row(tid)["status"]
    self.assertEqual(status, S.RUNNING,
                     "吊销票不该顺手改任务状态；状态由看门狗/人工决定")

补记分支的对应断言：

    self.assertEqual(out["action"], "recorded")
    self.assertEqual(out["tickets_revoked"], 0)

### 3.4 tests/test_v011_rc5_proxy.py（9 项，R7）

**测什么。** `tools/bfc-proxy.mjs` 的监听地址闸门：非回环地址必须在启动前退出，
回环地址不许被误杀。6 个拒绝用例（`0.0.0.0`、`::`、`[::]`、空串、`192.168.1.5`、`8.8.8.8`），
3 个放行用例（`127.0.0.1`、`127.0.1.2` 与 `localhost`、`[::1]`）。

**为什么这样断言。** 只断退出码不够——脚本可能因为别的错误（比如端口冲突）也非 0 退出，
「拒绝」这条就假通过了。所以同时断四件事：3 秒内退出（没退出就是在 listen 上了）、
非 0、输出里有拒绝字样、输出里回显了实际拿到的 host。
再加一条 `assertNotIn("listening on", ...)`，这是唯一能证明闸门发生在 listen 之前的方式。
放行用例反过来只要求「不出现拒绝字样」，允许它死在 listen 报错上——测试点只有闸门本身。

**最关键的一条断言。** `tests/test_v011_rc5_proxy.py:88-98`：

    self.assertNotEqual(proc.returncode, 0,
                        "%s（%r）以 0 退出，说明闸门被通用错误处理吞掉了；输出=%r"
                        % (label, host, combined))
    self.assertNotIn("listening on", combined,
                     "%s（%r）在被拒绝后仍然进入了 listen" % (label, host))

没装 node 时整类 `skipUnless` 跳过，这一点写在第 6 节，不能当成「已验证」。

### 3.5 tests/test_v011_rc5_receipts.py（24 项，R5 执行端侧）

**测什么。** 执行端本地收据：`ReceiptLog`（JSONL 追加 + flush + fsync）、
`state_of`/`summary` 的判定、落盘与跨进程可见、`run_once` 的事件序列、
以及 `ExecutorClient` 对 `receipts` 参数的五种接法（`None`、`False`、路径字符串、`ReceiptLog` 实例、未传时读环境变量）。

**为什么这样断言。** R5 的动机是「服务端库一旦恢复就无从对账」——收据是执行端唯一的外部记忆。
所以断言分三层：格式层（顺序、非法事件名拒绝、坏行跳过）、耐久层（写完立刻能被另一个进程读到）、
语义层（`claimed` 只有一条 = 未开工；`started` 没有 `finished` = 结果未知；
`uncertain` 不算已完成）。只在内存里断言等于没测 R5，所以有专门一条另起子进程读文件。

**最关键的一条断言。** `tests/test_v011_rc5_receipts.py:267-272`：

    rows = ReceiptLog(self.path).read_all()
    self.assertEqual([r["event"] for r in rows],
                     ["claimed", "started", "finished"],
                     f"收据顺序不对：{[r['event'] for r in rows]}")
    self.assertEqual({r["dispatch_id"] for r in rows}, {_FakeServer.DISPATCH_ID},
                     "收据没用服务端下发的 dispatch_id")

以及 `summary` 的精确相等（不是「包含」）：

    self.assertEqual(s, {"total": 3, "完成": 1, "结果未知": 1,
                         "未开工": 1, "未知": 0})

### 3.6 tests/test_v011_rc5_restore_authority.py（20 项，R1 与 R8；收尾后 21 项）

**测什么。** 恢复之后：所有远端身份被禁用且文案可执行、重新授权必须同时换凭据、
APPROVED 被冻结不可领取、在途任务的票被吊销、重放不再发票；
以及维护模式本身（排空屏障、异常路径退出、HTTP 503 语义）。

**为什么这样断言。** R1 的关键认知是「token 的哈希随快照一起回来了」——所以
「能查到 executors 行」完全不代表「能认证」，断言必须真的去 authenticate/heartbeat 一次。
R8 的关键认知是「假成功最危险」：进入维护模式要拿写锁做屏障，拿不到就必须抛错，
不能返回一个「好像进去了」的结果，否则恢复会与在途写入撞车。所以有一条专门用两线程 + Event
构造「持写事务时进维护」，断言拿到的是 `MaintenanceError` 而不是静默通过。

**最关键的一条断言。** `tests/test_v011_rc5_restore_authority.py:94-99`：

    with self.assertRaises(CouncilError) as cm:
        self.c.authenticate(old_token, role="executor")
    msg = str(cm.exception)
    self.assertIn("已被禁用", msg)
    self.assertIn("重新授权", msg)
    self.assertIn("更换凭据", msg)

排空屏障那条：

    self.assertIsInstance(err.get("e"), MaintenanceError,
                          "有在途写事务时必须拒绝进入，而不是假装排空成功")

收尾补的那一项 `test_tickets_revoked_reports_real_count`（提交 `b8f6de1`，就是 244 项里的第 244 项）
针对的是同一处的另一面：原来 `_block_inflight` 的 `tickets_revoked` 是硬编码 `1`，
恢复两份票也只报 1。改后断的是真实计数：

    live = self.db.one(
        "SELECT COUNT(*) AS c FROM tickets WHERE revoked_at IS NULL")["c"]
    self.assertEqual(live, 2)
    bk = self.bundle()
    out = self.c.restore_from_bundle(bk)
    self.assertEqual(out["tickets_revoked"], 2)

两个断言一起才成立：只断返回 2 的话，把它写死成常量 2 也能过；只断库里没有活票的话，
恢复前的票本来就可能已经吊销，看不出这一轮动没动手。

HTTP 侧「不能回一个成功的 result」：

    self.assertNotIn("result", r.json())

### 3.7 tests/test_v011_rc5_review_audit.py（13 项，R2/R3/R4/R6）

**测什么。** 四件事合成一个文件，因为它们共用同一套建库与审查流程：
双模型通过的两条事件必须落库（R2）、撤评论不许影响审批有效性（R3）、
心跳时间戳必须在事务内取（R4）、审计导出包必须是列白名单（R6）。

**为什么这样断言。** R2 的缺陷是 `_recompute_review` 提前 `return` 跳过了末尾的 `log_event` 循环，
只看任务状态看不出来（状态本来就对），只能查 events 表。R3 必须双向断：撤评论不动状态，
但撤真票仍要把任务打回 `IN_REVIEW`——只断一边会把 R3 修过头。R4 断的是
「票的过期时间必须晚于续租时刻」，这个不等式在旧实现里等锁久一点就会翻转。
R6 不看注释也不看 manifest，直接把包里所有 JSON 的键名扒出来查敏感词。

**最关键的一条断言。** 事件落库（`tests/test_v011_rc5_review_audit.py:73`）：

    self.assertIn("task.review_passed", kinds,
                  "双模型通过事件丢了：审计只看事件表会以为任务没通过审查")
    self.assertIn("task.pending_approval", kinds)

时间戳（`tests/test_v011_rc5_review_audit.py:154-156`）：

    self.assertGreater(after["expires_at"], after["renewed_at"],
                       "续租后票据必须比续租时刻更晚过期")
    self.assertAlmostEqual(r["lease_expires_at"], after["expires_at"], places=6)

白名单的键名扫描（用例 `test_no_sensitive_column_names_leak` 起于 `tests/test_v011_rc5_review_audit.py:200`，
查表循环在 `:214-217`，`SENSITIVE_HINTS` 定义在 `:176`）：

    for bad in SENSITIVE_HINTS:
        self.assertNotIn(
            bad, low,
            f"{name} 里出现了可疑列名 {k}")

这个文件里有一处踩坑留下的注释值得留着——`comment_id()` 的 docstring：
「add_review 返回的是任务快照，不是审查行；评论 id 要从 reviews 里取。」

---

## 4. 测试质量整改

审计点名了三个恒真或过宽的既有测试。整改原则是**按语义重写**，不是放宽断言，也不是删掉。

### 4.1 tests/test_v011_p0.py:174 test_db_file_has_no_plaintext_ticket

**改前（72e8535）。** 这条用例不是全错，它前面那句是有效证据：

    self.assertNotIn(p["ticket"].encode(), blob, "数据库里出现明文执行票")

审计点的是紧跟着的最后一句：

    self.assertNotIn(b"SECRET-PROMPT-BODY", blob.replace(
        self.db.one("SELECT prompt FROM task_versions")["prompt"].encode(), b""))

**为什么不算证据。** 它先把 prompt 的字节从 blob 里替换成空，再断言 prompt 不在剩下的字节里。
这一步处理之后，无论库里写了什么断言都成立——如果 prompt 真明文躺在库里，`replace` 已经抹掉它了。
换句话说这条断言测的是 `bytes.replace` 的行为，不是数据库的行为。还有第二层恒真：
如果 prompt 根本没入库，`one(...)["prompt"]` 拿到的仍是同一个字符串，减法照样成功。

**改后怎么才算证据。** 原来那句保留（它本来就是有效证据），另加字节层与表内两层硬断言：

    self.assertNotIn(ticket.encode(), blob, "数据库里出现明文执行票")
    self.assertNotIn(b"bfc_tk_", blob, "数据库里出现执行票前缀")

    row = self.db.one("SELECT * FROM tickets WHERE id=?", (p["ticket_id"],))
    self.assertEqual(row["token_hash"], hash_token(ticket))
    for k, v in dict(row).items():
        if isinstance(v, str):
            self.assertFalse(v.startswith("bfc_tk_"),
                             f"tickets.{k} 里存了明文执行票")

第一条只看字节，没有任何减法；第二条连票据前缀都禁掉，顺手覆盖「别的列里塞了票」；
第三条证明「表里存的是哈希且哈希对得上」，把「什么都没存」也排除了。

同一文件新增 `test_prompt_body_written_to_task_versions` 收尾：

    got = self.db.one("SELECT prompt FROM task_versions WHERE task_id=? AND version=1",
                      (tid,))["prompt"]
    self.assertEqual(got, "SECRET-PROMPT-BODY")

它证明 prompt 确实落库了。没有这一条，上面「库里找不到 prompt」可能只是因为根本没写。

### 4.2 tests/test_v011_p1.py:80 test_watchdog_survives_sweep_error

**改前（72e8535）。** 用例体去掉 def 那行，其余就是这样：

    def boom():
        raise RuntimeError("模拟扫描异常")
    self.c.sweep_timeouts = boom
    self.c.start_watchdog(1.0)
    time.sleep(2.2)
    self.c.stop_watchdog()      # 线程没被异常打死就不该卡住
    self.assertFalse(self.c._watchdog)

**为什么不算证据。** 断的是 `stop_watchdog()` 之后 `_watchdog` 变成空。
这是 `stop_watchdog` 自己的契约，跟「扫描抛异常时线程还活着」没有关系。
把 `start_watchdog` 删掉、把 `sweep_timeouts` 换成正常实现，这条测试一样过。

**改后怎么才算证据。** 三件事同时成立：异常被数进去、线程没被打死、换回正常扫描后还能干活。

    while time.time() < deadline and wd.errors < 3:
        time.sleep(0.1)
    self.assertGreaterEqual(calls["n"], 3, "扫描根本没被反复调用")
    self.assertGreaterEqual(wd.errors, 3, "异常没有被计数")
    self.assertTrue(wd.is_alive(), "看门狗被单次异常打死了")

收尾把 `sweep_timeouts` 还原成类方法、把 `heartbeat_ttl` 设成 0，等任务真的被打成 `S.BLOCKED`：

    del self.c.sweep_timeouts
    self.c.heartbeat_ttl = 0
    ...
    self.assertEqual(self.c.get_task(tid)["status"], S.BLOCKED)

现在 `_watchdog` 为空这条断言排在「线程真的恢复工作」之后，它是收尾检查，不再是结论本身。

### 4.3 tests/test_v011_p3.py:160 test_clean_shutdown 与新增的 SIGINT 用例

**改前的过宽之处。** POSIX 分支只接受 `(0, -15)`：

    self.assertIn(rc, (0, -15), f"退出码异常：{rc}")

`-15` 意味着进程被 SIGTERM 直接打死，根本没走自己的收尾逻辑。把「被信号打死」和「优雅退出」
并列为通过，这条测试就发现不了「关闭逻辑没被调用」这类回归。

**改后怎么才算证据。** 原用例保留（它还要断端口释放，那部分是有效证据），
另加一条只认优雅退出的用例，并在 Windows 上跳过：

    @unittest.skipIf(os.name == "nt", "Windows 无 SIGINT 语义，走 TerminateProcess")
    def test_graceful_shutdown_on_sigint(self):
        self.proc.send_signal(signal.SIGINT)
        rc = self.proc.wait(timeout=20)
        self.assertEqual(rc, 0, "SIGINT 没有走优雅退出：\n" + ...)
        wal = Path(self.data) / "council.db-wal"
        if wal.exists():
            self.assertLessEqual(wal.stat().st_size, 0,
                                 "优雅退出后 WAL 仍留有未合并的数据")

退出码 0 只能证明「没被信号打死」，不能证明「收尾做完了」。所以补 WAL 断言：
`close()` 里做过 `wal_checkpoint(TRUNCATE)`，优雅退出后 `-wal` 要么不存在要么 0 字节。
两条一起才算「自己收拾干净了」。

### 4.4 另外两处按语义更新的既有断言（不是放宽）

R1 改了吊销文案后，`tests/test_v011_rc3.py:381` 的 `test_revoke_and_submit_race` 里
旧断言 `assertIn("吊销", ...)` 会误红。更新成断新文案（:411-412），而不是把断言删掉：

    self.assertIn("该身份已被禁用", str(out.get("submit_err")))
    self.assertIn("重新授权", str(out.get("submit_err")))

`tests/test_v011_rc3.py:507` 的 `test_restore_older_schema_migrates` 原来断恢复后任务仍是
`S.APPROVED`——这正是 R1 要修的行为。新断言（:529-531）比旧的更严：

    self.assertEqual(self.status(tid), S.BLOCKED)
    self.assertIn("冻结", self.db.one(
        "SELECT blocked_reason AS r FROM tasks WHERE id=?", (tid,))["r"])
    self.assertGreaterEqual(out["frozen_approved"], 1)

---

## 5. 真实命令与输出

### 5.1 全量单元测试

    cd /home/dsh/bigfish-council
    LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
      .venv/bin/python -m unittest discover -s tests -t .

退出码 **0**。关键输出：

    Ran 244 tests in 39.178s

    OK

日志全文 11 行（点号行为排版做了截断），有效结论只有上面两行，其余是：

    /home/dsh/bigfish-council/.venv/lib/python3.11/site-packages/fastapi/testclient.py:1:
      StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated;
      install `httpx2` instead.
      from starlette.testclient import TestClient as TestClient  # noqa
    .........................Fontconfig error: Cannot load default config file: No such file: (null)
    This plugin does not support propagateSizeHints()
    ...........拒绝恢复：127.0.0.1:33541 上还有服务在监听，请先停掉它（tools/stop-headless.ps1
      或关闭 GUI），确认没有其他实例后再重试；确实要强行恢复请加 --force。

上面几行为了排版折过行，原日志里每条都是一整行。
这些警告都是环境噪音（starlette 弃用提示两行、缺 fontconfig 一行、offscreen 平台插件提示一行），不是失败。
那条中文来自 3.1 节 CLI 守卫用例的**预期输出**：用例用 `cli.main(...)` 在进程内跑，
打印走的是继承来的 stderr，所以会混进全量日志——它出现在这里，恰好说明那条用例真的
走到了守卫分支，而不是被跳过。

### 5.2 逐文件单跑

    .venv/bin/python -m unittest tests.test_v011_rc5_receipts

15 个文件全部退出码 0，逐项数字见 2.1 表。

### 5.3 轻量验证脚本

    .venv/bin/python tools/selfcheck.py

退出码 0，末两行：

    通过 16 项，失败 0 项
    自检全部通过，可以启动 GUI。

逐项覆盖：健康检查 3、MCP 协议 3、闭环 7、权限 3。

    LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
      .venv/bin/python tools/gui_smoke.py

退出码 0：

    通过 9 项，失败 0 项
    GUI 冒烟通过。

状态栏那条要看清楚它断的是什么。`tools/gui_smoke.py:61` 只断文案是字符串：

    check("状态栏文案", isinstance(win.api_label.text(), str), win.api_label.text())

文案本身随环境变：本批次跑全量时机器上有服务在监听，日志里是
`API：已监听 127.0.0.1:3097`；在 `b8f6de1` 上复核时没有服务在跑，
同一条打印的是 `API：未启动`。两次都是 9 项全过。
所以这条用例证明的是「标签能取到字符串」，不证明「服务真的起来了」——
服务能否起来由 selfcheck 的健康检查那 3 项负责，别把两件事混成一条证据。

### 5.4 静态检查

    .venv/bin/python -m pyflakes bigfish tools tests

零输出，退出码 **0**。

    .venv/bin/python -m compileall -q bigfish tools tests

无输出，退出码 **0**。

### 5.5 在收尾提交 `b8f6de1` 上复核

第 2.4 节那处收尾改动提交之后，在 `b8f6de1` 上把上面几条命令按同样方式重跑了一遍：

    Ran 244 tests in 39.178s
    OK                            # 退出码 0
    pyflakes: 零输出，退出码 0
    compileall: 无输出，退出码 0

本报告全部数字按 HEAD `b8f6de1` 的 244 项写；`adc48a3` 上的 243 项只是本批收尾前的中间状态，不再单列口径。
selfcheck 与 gui_smoke 也在 `b8f6de1` 上重跑过：

    通过 16 项，失败 0 项 / 自检全部通过，可以启动 GUI。   # 退出码 0
    通过 9 项，失败 0 项 / GUI 冒烟通过。                  # 退出码 0

与第 5.3 节同一台机器，项数与上一次完全一致。gui_smoke 里状态栏那条这次打印的是
`API：未启动`，原因见 5.3 那段——用例断的是「是字符串」，不是「服务已监听」。

---

## 6. 未覆盖 / 未验证

按影响排序，逐条写清「没做什么」以及「因此不能声称什么」。

**6.1 Windows 未跑。** rc5 的 244 项只在 Debian 12 / Python 3.11.2 上跑过。
Windows 上 Python 3.14.6 的 244 项状态是「未验证」。已知会走不同分支的至少有：
`test_v011_p3.py` 的 SIGINT 用例（`skipIf(os.name == "nt")`，Windows 上直接跳过，覆盖为空档）、
`test_clean_shutdown` 的退出码分支（Windows 走 TerminateProcess，接受 1）、
`backup._restore_lock` 的 `msvcrt.locking` 分支（POSIX 上跑不到）、
`test_v011_p3.py` 里密钥文件权限断言的 POSIX 分支。

**6.2 GUI 真窗口未做。** `tools/gui_smoke.py` 用 `QT_QPA_PLATFORM=offscreen`，
只证明主窗口能构造、5 个页面能切换、状态栏标签取得到字符串、待审批任务能被识别。
没有在真实窗口里点过恢复按钮，也没有验证 `bigfish/gui.py:1093` 的 `pause_services()` 调用与
`:1098` 的 `resume_services()` 调用（定义分别在 `:1222`、`:1243`）在真实事件循环里的时序。R8 的 GUI 侧因此是
「代码路径已覆盖、交互未验证」。

**6.3 跨机未重跑。** 没有在第二台机器上跑过。`tools/bfc-proxy.mjs` 那 9 项需要 node，
本轮 node 是 v22.23.2，其它 node 版本上的 `listen` 报错文案没有核对过
（放行用例允许死在 listen error 上，所以文案差异不会误红，但也意味着那一半没有被真正验证）。

**6.4 pyflakes 未在 Windows 跑。** pyflakes 4.1.0 只在平台 A 上跑过，零输出。
这台机器上零输出意味着没有未定义名、没有未使用导入；Windows 上若存在
`# pragma: no cover - 仅 Windows` 分支里的平台相关名字，本轮没有证据。
compileall 同理，只覆盖平台 A 的字节码编译。

**6.5 其它未测项。** 维护模式的排空屏障只在「同进程两线程」下验证过，
没有做「两个独立进程同时恢复」的实测；`Retry-After: 15` 断的是响应头字面值，
没有验证客户端真的按 15 秒退避重试。

---

## 7. 怎么复现

以下命令按顺序照抄即可，全部在 `/home/dsh/bigfish-council` 下执行。

    cd /home/dsh/bigfish-council

    # 0) 确认版本与提交
    git rev-parse --abbrev-ref HEAD
    git log --oneline -4
    .venv/bin/python -c "import bigfish; print(bigfish.__version__)"

    # 1) 全量单元测试（约 43 秒）
    LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
      .venv/bin/python -m unittest discover -s tests -t .

    # 2) 单独跑某个文件（用模块名，避免 discover 单文件时的路径前缀歧义）
    .venv/bin/python -m unittest tests.test_v011_rc5_review_audit
    .venv/bin/python -m unittest tests.test_v011_rc5_restore_authority
    .venv/bin/python -m unittest tests.test_v011_rc5_receipts
    .venv/bin/python -m unittest tests.test_v011_rc5_key_heal
    .venv/bin/python -m unittest tests.test_v011_rc5_dispatch_id
    .venv/bin/python -m unittest tests.test_v011_rc5_cli_guard
    .venv/bin/python -m unittest tests.test_v011_rc5_proxy    # 需要 node

    # 3) 独立数一遍用例总数，与 Ran 的数字对账
    .venv/bin/python -c "import unittest; \
    s=unittest.TestLoader().discover('tests', top_level_dir='.'); \
    f=lambda x: sum(f(t) for t in x) if isinstance(x, unittest.TestSuite) else 1; \
    print('TOTAL', f(s))"

    # 4) 自检（会起服务，注意端口占用）
    .venv/bin/python tools/selfcheck.py

    # 5) GUI 冒烟（offscreen）
    LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
      .venv/bin/python tools/gui_smoke.py

    # 6) 静态检查
    .venv/bin/python -m pyflakes bigfish tools tests
    .venv/bin/python -m compileall -q bigfish tools tests

    # 7) 相对 rc4 发布点的改动规模（限定代码与测试目录，docs 另有文档批次）
    git diff --stat 72e8535 -- bigfish tests tools

平台 B（Windows）本轮未执行，复现时把第 1、4、5、6 步的 `LD_LIBRARY_PATH` 与
`QT_QPA_PLATFORM` 去掉，换 Windows 侧解释器执行即可。
