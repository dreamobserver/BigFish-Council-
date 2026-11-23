# 测试报告：bigfish-council v0.1.1-rc3

## 0. 一句话

平台 A（Debian 12 / Python 3.11.2 / Node 22）跑完 **129 项全绿、退出码 0**；
平台 B（Windows 11 / Python 3.14.6）也跑完 **129 项全绿、退出码 0**，见第 5 节。
两平台用的是同一份 129 项测试、同一份源码包，没有为通过而改断言。

---

## 1. 平台 A — Debian 12 / Python 3.11.2

环境：

    OS        Debian GNU/Linux 12 (bookworm)，kernel 5.15.0-187-generic
    Python    3.11.2
    Node      v22.23.2
    SQLite    3.40.1
    venv      /home/dsh/bigfish-council/.venv
    GUI 依赖  LD_LIBRARY_PATH=/home/dsh/.local/libbfc（52 个 deb 解包出来的 .so）
              QT_QPA_PLATFORM=offscreen

### 1.1 全量单元测试

    $ cd /home/dsh/bigfish-council
    $ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests -t .
    ...
    Ran 129 tests in 15.984s

    OK
    $ echo $?
    0

分组：

| 文件 | 用例数 | 覆盖 | 本轮变化 |
| --- | --- | --- | --- |
| tests/test_core.py | 15 | 状态机、审查门禁、审批、执行票、心跳超时 | **原样保留** |
| tests/test_api.py | 6 | HTTP 端点、MCP 往返、不暴露审批工具 | **原样保留** |
| tests/test_v011_p0.py | 15 | SQLite 备份/恢复、审批并发、无明文执行票 | **原样保留** |
| tests/test_v011_p1.py | 24 | Watchdog、执行票续期、心跳幂等、MCP 错误语义 | 1 项改写（第 4 节） |
| tests/test_v011_p2.py | 24 | 四角色表、审查身份绑定、执行器边界、审计 | **原样保留** |
| tests/test_v011_p3.py | 19 | 端口拓扑、只允许回环、后台进程、GUI 冒烟 | **原样保留** |
| tests/test_v011_rc3.py | 26 | 状态机 7 / 并发 7 / 恢复 7 / 安全边界 5 | **本轮新增** |
| **合计** | **129** | | v0.1.0 的 21 项全部在位 |

### 1.2 新增 26 项的逐项结果

    $ .venv/bin/python -m unittest tests.test_v011_rc3 -v
    Ran 26 tests in 3.351s
    OK

| 组 | 用例 | 结果 | 它到底证明了什么 |
| --- | --- | --- | --- |
| 状态机 | test_cancel_after_approved_cannot_claim | ok | 取消后 claim 抛错，且 tickets 表没有新行 |
| | test_cancel_before_approval_cannot_approve | ok | CANCELLED 状态下 approve 被拒 |
| | test_cancel_and_claim_concurrent | ok | 真实双线程 + Barrier；两种合法结局外没有第三种，取消方赢时 tickets 表 0 行 |
| | test_revoke_vote_reopens_approval | ok | 撤票后回 IN_REVIEW、审批 effective=False、不能再 claim |
| | test_votes_before_submit_reach_pending | ok | DRAFT 期双票 → 送审直接 PENDING_APPROVAL |
| | test_cancel_while_dispatched_needs_ack | ok | 执行中取消进 CANCELLING，心跳要求确认，completed 回传被拒，确认后才 CANCELLED |
| | test_cancel_while_dispatched_executor_lost_blocks | ok | 执行器失联时 CANCELLING 被看门狗转 BLOCKED |
| 并发 | test_two_threads_claim_same_task_only_one_wins | ok | 双线程同 Claim，只有一个拿到票，另一个抛错 |
| | test_same_idem_key_claim_returns_identical | ok | 同键重放返回同一张票（确定性派生） |
| | test_same_idem_key_heartbeat_no_duplicate_events | ok | 重放不新增 run.heartbeat 事件、不新增 Run |
| | test_same_idem_key_submit_no_duplicate_runs | ok | 两次响应完全相等、artifact 只 1 条；同键换体抛错 |
| | test_watchdog_and_heartbeat_race | ok | 真实并发 1.2s，持续心跳的任务没被误杀 |
| | test_revoke_and_claim_race | ok | 吊销与领取并发只有一个合法结果 |
| | test_revoke_and_submit_race | ok | 吊销与回传并发只有一个合法结果 |
| 恢复 | test_old_ticket_invalid_after_restore | ok | 恢复前签发的票在恢复后一律不能用 |
| | test_old_claim_idempotency_purged_after_restore | ok | 旧 claim 幂等记录被清，重放拿不回历史票 |
| | test_inflight_task_blocked_after_restore | ok | 恢复前 DISPATCHED 的任务变 BLOCKED，票全吊销 |
| | test_restore_failure_keeps_original_db | ok | 插入点抛异常后原库仍可读写 |
| | test_restore_older_schema_migrates | ok | schema 2 的备份恢复后升到 3 |
| | test_restore_future_schema_rejected | ok | schema 99 被拒，不降级 |
| | test_restore_corrupted_bundle_rejected | ok | 假 ZIP、只有 manifest 的包都被拒 |
| 安全 | test_all_entry_points_reject_public_bind | ok | 7 个非回环地址全 BindError；子进程验证 rc=2 且端口仍可 bind |
| | test_executor_cannot_read_other_task | ok | 他人任务 403、未分配任务也 403 |
| | test_ticket_not_plaintext_in_storage | ok | events/runs/整个 db 文件里都搜不到明文票 |
| | test_audit_bundle_internal_tables | ok | 逐表解析后无 deny 列；审计包不能用来恢复 |
| | test_key_file_permissions | ok | 密钥文件必须受限，POSIX 断言 0600 |
### 1.3 轻量验证脚本

    $ .venv/bin/python tools/selfcheck.py
    通过 16 项，失败 0 项
    自检全部通过，可以启动 GUI。

    $ QT_QPA_PLATFORM=offscreen .venv/bin/python tools/gui_smoke.py
    通过 9 项，失败 0 项
    GUI 冒烟通过。

    $ .venv/bin/python -m bigfish.cli --data-dir /tmp/e2e-rc3 serve --port 3097 &
    $ .venv/bin/python tools/e2e_loopback.py --url http://127.0.0.1:3097/mcp --data-dir /tmp/e2e-rc3
    通过 22 项，失败 0 项

⚠️ e2e_loopback 的 22 项**全部跑在同一台机器的回环上**，它证明的是协议链路、权限边界、
状态机；**不能**当作跨设备联调成功。

### 1.4 静态检查

    $ .venv/bin/python -m pyflakes bigfish tools tests
    （无输出）
    $ echo $?
    0

    $ .venv/bin/python -m compileall -q bigfish tools tests && echo COMPILE_OK
    COMPILE_OK

本轮清掉 14 处未使用导入/变量。其中一处是**真缺陷**：security.verify_file_restricted
的 user 变量取了从来没用，导致「ACL 里没有宽泛主体就算通过」，哪怕文件根本没授权给当前用户。
现在要求「既无宽泛主体，又必须包含当前用户」，两条都满足才算通过。

### 1.5 CLI 冒烟

    $ .venv/bin/python -m bigfish.cli allowed-ports
    {
      "windows_api": {"port": 3096, "role": "Windows 本地控制中心 API 监听端口（默认仅回环）"},
      "vps_proxy":   {"port": 3098, "role": "VPS 上 bfc-proxy.mjs 回环跳板端口"},
      "test_only":   {"port": 3097, "role": "单测与 GUI 冒烟使用的临时端口，不参与部署"}
    }

16 个子命令全部可解析（init / overview / allowed-ports / task-create / task-list /
task-show / executor-add / review-start / review / approve / cancel / verify / sweep /
purge-idem / serve / watchdog / export / audit-export / inspect / restore）。

---

## 2. 并发测试是怎么写的（不是 sleep 编排）

全部 7 项并发测试用 threading.Barrier 做同步点。以「同键 Submit」为例：

    b = threading.Barrier(2)
    results = []
    def worker():
        b.wait()                      # 两个线程在同一时刻冲进临界区
        try:
            results.append(self.c.submit_result(...))
        except Exception as exc:
            results.append(exc)
    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    self.assertEqual(len(results), 2)
    # 两个都不该是异常，且响应必须完全相同
    self.assertTrue(all(isinstance(r, dict) for r in results))
    self.assertEqual(results[0], results[1])

Watchdog 与 Heartbeat 的并发测试：主线程持续心跳 1.2 秒（间隔 0.1s），
同时看门狗以 1.0s 间隔扫描；断言任务**始终没有**进入 BLOCKED。

---

## 3. 日志留存

完整输出已保存在 /home/dsh/bfc-rc3-logs/：

| 文件 | 内容 |
| --- | --- |
| unittest-linux.txt | 129 项全量输出 |
| rc3-only.txt | rc3 26 项 verbose 输出 |
| selfcheck.txt | 16 项自检 |
| gui-smoke.txt | 9 项冒烟 |
| e2e-loopback.txt | 22 项回环端到端 |
| pyflakes.txt | 静态检查（Linux，无输出即通过） |
| unittest-windows.txt | 平台 B 129 项全量（144 行 / 15523 字节） |
| selfcheck-windows.txt | 平台 B 自检 16 项（**转录**，原始见 .raw） |
| gui-smoke-windows.txt | 平台 B GUI 冒烟 9 项（**转录**，原始见 .raw） |

### 3.1 平台 B 日志的编码残损（不当成完美日志）

平台 B 的日志经 windows-mcp 的 PowerShell 通道回传，中间被转了两次码：

- `unittest-windows.txt`：**结论行与全部 129 个 `... ok` 都是纯 ASCII，可信**；
  但 144 行里有 8 行含 261 个 U+FFFD，全部落在测试的中文 docstring 上。
  「跑了什么、过没过」是准的，「用例的中文描述」是残的。
- `selfcheck-windows.txt` / `gui-smoke-windows.txt`：原始字节已全被替换字符吃光
  （`od -c` 是成片的 `357 277 275`），不可还原。当前内容是当次 PowerShell 控制台
  直接返回的文本**转录**，不是磁盘原始字节；原始文件保留为 `*.txt.raw` 作为物证。

原因：PowerShell 的 `*>` 用控制台代码页（GBK）写盘，`[IO.File]::ReadAllBytes` 拿到的已是
GBK 字节，回传时又整体按 UTF-8 解一次，越界字节变 U+FFFD，不可逆。正确做法是在 Windows 侧
先设 `$env:PYTHONIOENCODING="utf-8"` 并用 `Out-File -Encoding utf8`——轮末对 selfcheck /
gui_smoke 补做了（所以那两项拿到了干净中文输出），unittest 全量跑在更早，没赶上。

目录里另有 `README.md` 逐文件标注平台、内容与是否为原始字节。

---

## 4. 唯一改动的既有测试（不是放宽）

tests/test_v011_p1.py:143 原名 test_revoked_ticket_rejected，改名为
test_cancel_while_dispatched_needs_ack。

原因：rc2 的语义是「吊销票后心跳直接失败」；rc3 按任务书 2.1c 要求，
执行中取消必须走「人类请求 → 执行器停止 → 明确确认 → 服务端完成取消」四步。
旧断言在新语义下必然失败，因为它检查的正是被设计改掉的那个行为。

新断言比旧的多：

    # 旧（rc2）
    self.assertRaises(CouncilError, self.c.heartbeat, p["ticket"], self.ex, "running")

    # 新（rc3）
    r = self.c.request_cancel(tid)
    self.assertEqual(self.c.get_task(tid)["status"], S.CANCELLING)
    self.assertFalse(r["final"])                              # 还没结束
    hb = self.c.heartbeat(p["ticket"], self.ex, "running")
    self.assertTrue(hb["cancel_ack_required"])                # 要求确认
    self.assertTrue(hb["cancel"])
    with self.assertRaises(CouncilError):
        self.c.submit_result(p["ticket"], self.ex, "completed", ...)  # 不许谎报完成
    out = self.c.confirm_cancel(p["ticket"], self.ex)
    self.assertEqual(out["task_status"], S.CANCELLED)
    with self.assertRaises(CouncilError):
        self.c.heartbeat(p["ticket"], self.ex, "running")     # 确认后票失效

其余 102 项一字未改。

---
## 5. 平台 B — Windows 11 / Python 3.14.6（已执行，129/129）

### 5.1 结果

| 项 | 结果 |
| --- | --- |
| 全量单测 | **Ran 129 tests in 25.263s / OK**，退出码 0 |
| 日志行数 | 144 行，129 个 ok 标记，0 个 FAIL / ERROR |
| 并发测试组单跑 | tests.test_v011_rc3.TestConcurrency → Ran 7 tests in 1.663s / OK |
| 自检 | tools/selfcheck.py → 通过 16 项，失败 0 项 |
| GUI 冒烟 | tools/gui_smoke.py → 通过 9 项，失败 0 项（QT_QPA_PLATFORM=offscreen） |
| 静态检查 | **未做**：公司机 venv 未装 pyflakes（No module named pyflakes，退出码 1） |

### 5.2 环境（实测）

```text
VER 0.1.1-rc3
PY 3.14.6
SQLITE 3.50.4
OS Windows-11-10.0.26100-SP0
python: D:\BigFishCouncil\bigfish-council\.venv\Scripts\python.exe
```

主机 CLIENT-B。部署到**独立验证目录** `D:\BigFishCouncil\bigfish-council-rc3`（57 文件），
没有覆盖原有 rc2 目录 `D:\BigFishCouncil\bigfish-council`。
源码包从 VPS 的临时分发服务 `http://100.64.0.21:8099/bfc-src-v011rc3.zip` 拉取，224195 字节。

说明这次打包与最终包的关系，避免数字对不上：

- Windows 上跑的是 **224195 字节**那一版（57 文件）。
- 那之后本轮只改了文档（`git status` 里改动全是 `*.md`），代码文件一个没动；
  最终重打包仍是 **57 文件**，体积约 228 KB（随文档更新略有变化，以实际文件为准）。
- 代码是否真的一致，用两个可复核的事实钉住：①重打包前后 `bigfish`+`tests`+`tools` 三目录的
  聚合 sha256 在 Linux 侧完全相同（`5e02f625…17b3`）；②重打包后把 ZIP 解到 `/tmp/pkgv6` 独立跑了一遍
  `Ran 129 tests in 15.978s / OK`，且该副本的三目录聚合哈希与源码目录逐字节相同。
- **未做的核对**：没有在 Windows 侧对同一批代码再算一次聚合哈希（控制权在写这份报告时又交回给人工了）。
  所以「Windows 跑的就是最终包里的代码」这一点，证据是文档级 diff + Linux 侧哈希，不是 Windows 侧哈希。
- 临时分发服务（`100.64.0.21:8099`）在下载完成后已关闭，`ps aux` 确认无残留在跑。

### 5.3 跑的具体命令

```powershell
$root='D:\BigFishCouncil\bigfish-council-rc3'
$py='D:\BigFishCouncil\bigfish-council\.venv\Scripts\python.exe'
Set-Location $root
& $py -m unittest discover -s tests -t . -v *> D:\temp\rc3-win.log
# EXIT=0

$env:QT_QPA_PLATFORM='offscreen'
& $py tools\selfcheck.py   # 16/16
& $py tools\gui_smoke.py   # 9/9
```

日志留存：`/home/dsh/bfc-rc3-logs/unittest-windows.txt`（15523 字节 / 144 行）、
`selfcheck-windows.txt`、`gui-smoke-windows.txt`。

### 5.4 Windows 上与 POSIX 不同的两点（都已实测）

**1）密钥文件权限**。之前这条在 FIX-MATRIX 里标的是「未验证（仅审读）」，现在实测：

```text
KEYLEN 32
SECURE {'ok': True, 'mode': '0o666', 'detail': '', 'acl': 'granted'}
VERIFY (True, 'acl limited')
```

`mode` 仍是 `0o666` —— Windows 上 `os.chmod` 对 ACL 无效，真正生效的是
`security.secure_file()` 里走的 `icacls /inheritance:r /grant:r USER:F`。
`verify_file_restricted()` 返回 `(True, "acl limited")`，即验证逻辑走的是 ACL 分支而不是 mode 分支。
这是 rc3 里 R2 残余风险的验证结论：**Windows 上靠 ACL，不靠 mode，且已被验证函数真实验证**。

**2）进程退出码**。`test_v011_p3` 里 `subprocess.terminate()` 在 Windows 上走
`TerminateProcess`，退出码是 **1** 而不是 POSIX 的 **-15**。这条在 rc2 就做了
`os.name == 'nt'` 分支处理（见 TESTLOG 缺陷 15），本轮在 Windows 上跑通即验证该分支确实生效。

### 5.5 之前为什么一直没跑成

公司机 windows-mcp 通道反复返回 `{"code":"USER_CONTROL","status":{"state":"user",...}}`，
即人工正在操作桌面，执行器按设计拒绝接管。本轮末尾 `ControlStatus` 恢复到
`{"state":"ready","generation":639,"wait_seconds":0.0}`，才拿到控制权跑完全部测试。
**未跑与已跑的分界就是这一个状态位**，不是代码问题。

## 6. 失败处理流程（本轮实际用过的）

1. 先看失败断言是**业务语义问题**还是**测试写错**。
2. 业务问题 → 改产品代码，测继续跑。
3. 测试写错 → 改测试，但**不许降低断言强度**，改完要在报告里写清楚改了什么、为什么。
4. 绝不用 skip / expectedFailure / try-except-pass 让红变绿。

本轮实例：test_revoke_vote_reopens_approval 一开始断言 latest_approval(tid) is None，
认为「撤票后不该有审批记录」。实际跑出记录还在——这是**设计正确**（历史证据必须留），
是我的断言错了。处理方式：不删测试，改断言为「记录必须存在，但 effective 必须为 False，
且不能再 claim」，同时在 core.latest_approval() 里补了 effective 派生字段，
让「保留证据」与「不再授权」两件事同时成立。

---

## 7. 未跑的检查（明确列出）

| 检查 | 状态 | 原因 |
| --- | --- | --- |
| Windows 平台全量测试 | **已执行，129/129 通过** | 见第 5 节 |
| Windows ACL 实测 | **已执行，通过**（acl limited / KEYLEN 32） | 见第 5.4 节 |
| Windows GUI 真窗口验收 | 未执行 | 只做了 offscreen 冒烟，真人坐在机器前点一遍没做 |
| 跨机 MCP 闭环（rc3 代码） | 未执行 | 公司机 3096 上跑的仍是 rc2 headless，闭环只能证明 rc2 |
| Windows 静态检查（pyflakes） | 未做 | 公司机 venv 未装 pyflakes |
| 类型检查（mypy/pyright） | 未做 | 项目未引入，本轮不新增依赖 |
| 长时间稳定性（soak） | 未做 | 无 24h 窗口 |

### 7.1 这些「未做」意味着什么

上表 7 条里，「Windows 全量测试」和「Windows ACL」已从「未做」变成「已做」，
这是本轮最实质的状态变化：rc3 的核心改动（状态机、并发、恢复、密钥）在两个平台都跑过同一套断言。
仍然空着的 3 条都有明确原因，不是靠猜补上的：

- GUI 真窗口：offscreen 能证明控件能构造、页面能切换、绑定能生效，
  证明不了真显示器下的 DPI 缩放、托盘图标、关闭行为。要人来点。
- 跨机 MCP 闭环：公司机 3096 上还是 rc2。要让闭环证明 rc3，
  得先把 rc3 部署到公司机并重启 headless —— 这一步属于「变更生产服务」，
  按任务书第九条要单独请示，本轮不做。
- soak：没有 24 小时窗口，也只能标未做。
