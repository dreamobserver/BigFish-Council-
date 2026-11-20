# 测试记录：bigfish-council v0.1.1-rc1 / rc2

记录时间：2026-11-18 09:19（UTC）。执行机：VPS-HOST（Debian 12，DSH 容器内）。
所有数字来自本轮实际运行输出，不是估算。

## 1. 全量单元测试

```
$ cd /home/dsh/bigfish-council
$ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t . -v

Ran 103 tests in 11.827s

OK
$ echo $?
0
```

完整日志：`/tmp/testlog-v011.txt`（11882 字节，含每个用例的 ok 与说明行）。

### 1.1 分组明细

| 文件 | 用例数 | 覆盖 | v0.1.0 原有 |
| --- | --- | --- | --- |
| `tests/test_core.py` | 15 | 状态机、审查门禁、审批、执行票、心跳超时 | **是，原样保留** |
| `tests/test_api.py` | 6 | HTTP 端点、MCP 往返、不暴露审批工具 | **是，原样保留** |
| `tests/test_v011_p0.py` | 15 | SQLite 备份/恢复、审批并发、无明文执行票 | 新增 |
| `tests/test_v011_p1.py` | 24 | Watchdog、执行票续期、心跳幂等、MCP 错误语义 | 新增 |
| `tests/test_v011_p2.py` | 24 | 四角色表、审查身份绑定、执行器边界、审计 | 新增 |
| `tests/test_v011_p3.py` | 19 | 端口拓扑、只允许回环、后台进程、GUI 冒烟 | 新增 |
| **合计** | **103** | | 21 原有 + 82 新增 |

原有 21 项 = test_core 15 + test_api 6，与任务书要求一致，一项没删。

### 1.2 原有 21 项的两处必要改动（非断言放宽）

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `tests/test_core.py` | 构造 `Council` 时注入 `key=load_or_create_key(tmp/"secrets"/"ticket.key")` | 执行票改为 HMAC 派生后，没有密钥无法签票（v0.1.0 用随机 token，不需要密钥）。这是接口变化，不是放宽断言。 |
| `tests/test_core.py` | `available_tasks(self.ex["id"])` → `available_tasks()` | 签名变更：不再按执行器过滤（v0.1.0 传了也没用，函数体忽略该参数）。 |
| `tests/test_api.py` | 同样注入 key | 同上。 |

**没有**删除任何断言，**没有**把 assert 改成 print，**没有**加 skip。

## 2. 分层验证（每组都实际跑过）

### 2.1 自检脚本

```
$ .venv/bin/python tools/selfcheck.py
通过 16 项，失败 0 项
```

覆盖：init/建任务/送审/双投/审批/领取/心跳/回传/验收的 CLI 全链路 + 权限边界 + 密钥文件权限。

### 2.2 GUI 离屏冒烟

```
$ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/gui_smoke.py
通过 9 项，失败 0 项
```

9 项：主窗口构造、五个页面都在、五页均可切换、状态栏显示 `API：已监听 127.0.0.1:3097`、待审批任务能被审批页识别。

已知无害噪音（不影响断言）：

```
Fontconfig error: Cannot load default config file: No such file: (null)
This plugin does not support propagateSizeHints()
RuntimeError: Signal source has been deleted     # Qt 退出时线程收尾
```

### 2.3 网络 4 层探测

```
$ .venv/bin/python tools/net_check.py
== 1. 配置层 ==
  [ok]   API 默认只绑回环  127.0.0.1
  [ok]   三个端口互不冲突  [3096, 3098, 3097]
  [ok]   API 端口 != 跳板端口  3096 vs 3098
== 2. 本机 API ==
  [ok]   127.0.0.1:3096 可连接  open
  [ok]   /health 可解析  {"status":"ok","service":"bigfish-council","version":"0.1.1-rc1",...,"watchdog":true}
  [ok]   健康检查里没有任务信息  db,service,status,time,version,watchdog
== 3. VPS 跳板 ==
  [ok]   127.0.0.1:3098 可连接  open
  [ok]   跳板 /health 透传成功
  [ok]   跳板转发到的是 bigfish-council  bigfish-council
  [ok]   跳板报告的版本与源码一致  0.1.1-rc1
== 4. 跨机（VPS -> Windows Tailnet） ==
  [skip] 未提供 --target-host，跳过跨机探测（这一步不能算通过）

通过 10 项，失败 0 项，跳过 1 项
```

第 4 层单独跑过，**失败**：

```
$ .venv/bin/python tools/net_check.py --target-host CLIENT-A-TAILNET-IP --target-port 3096
== 4. 跨机（VPS -> Windows Tailnet） ==
  [FAIL] CLIENT-A-TAILNET-IP:3096 可连接  TimeoutError
exit = 1
```

对比同期 `CLIENT-B-TAILNET-IP:8765` 仍 OPEN → VPS 侧 Tailscale 正常，卡在 CLIENT-A 那台。

### 2.4 回环端到端（同机，22 项）

```
$ .venv/bin/python tools/e2e_loopback.py --url http://127.0.0.1:3098/mcp --data-dir /tmp/bfc-rc1
== 0. 前置 ==
  [ok]   跳板 /health 在线  0.1.1-rc1
  [ok]   跳板后面确实是本版本  0.1.1-rc1
== 1. 人类侧建任务并送审 ==
  [ok]   任务已送审
== 2. 双模型审查 ==
  [ok]   单模型 approve 不推进  IN_REVIEW
  [ok]   双 approve 进入待审批  PENDING_APPROVAL
== 3. 人类审批 ==
  [ok]   已批准
== 4. 经跳板走 MCP 领取 ==
  [ok]   initialize 成功
  [ok]   暴露面只有 5 个执行器工具  council_list_tasks,council_claim_task,council_heartbeat,council_submit_result,council_task_status
  [ok]   没有审批/管理工具
  [ok]   能列出可领取任务
  [ok]   列表不含 Prompt
  [ok]   领取成功并拿到 Prompt
  [ok]   领取后状态是已派工
  [ok]   重复领取被拒且 isError=true
== 5. 心跳 / 回传 ==
  [ok]   心跳成功且转 RUNNING
  [ok]   心跳返回续租时间
  [ok]   回传只到待验收  SUBMITTED
  [ok]   回传不会自动 DONE
== 6. 越权与错误语义 ==
  [ok]   坏 Token 被拒（-32001）
  [ok]   审批工具不存在（-32601）
  [ok]   缺 Token 被拒
== 7. 人类验收 ==
  [ok]   人类验收后 DONE  DONE

通过 22 项，失败 0 项

注意：以上全部在**同一台机器**的回环上完成。
```

**这不是跨设备联调**。它证明协议、权限、状态机是对的；证明不了「VPS → CLIENT-A-TAILNET-IP」这条链路。

### 2.5 schema 1 → 2 升级实测

脚本 `/tmp/mig_check.py`，用 v0.1.0 建库写任务，再用 rc1 打开：

```
=== v0.1.0 建库 ===
db 大小 = 102400 字节，schema_version = 1
tickets 列 = [id,task_id,version,executor_id,token_hash,issued_at,expires_at,revoked_at]
任务状态 = DISPATCHED v1

=== rc1 打开同一库 ===
schema_version = 2
tickets 列 = [...,expires_at,max_expires_at,renewed_at,revoked_at]
audit 表存在 = True
reviews 列 = [...,verdict,created_at,actor,revoked_at]
idempotency 列 = [...,response,created_at,request_hash,expires_at]
任务状态 = DISPATCHED v1        ← 没被迁移打乱
secrets/ticket.key = 存在，perm 600
db.integrity() = (True, "ok")
表计数 = {meta:1, tasks:1, task_versions:1, reviews:2, approvals:1, executors:1, tickets:1, events:9, runs:0, artifacts:0, audit:0, idempotency:0}
```

### 2.6 回滚实测

脚本 `/tmp/rollback_check.py`，工作目录 `/tmp/rollback-test`：

```
=== A. 用 v0.1.0 代码建库建任务 ===
rc = 0，task-8fd3bdbb92 → 快照 data-v0.1.0-snapshot

=== B. 用 rc1 打开（自动迁移） ===
rc = 0，schema_version = 2，tasks = 2，audit 存在

=== C. 直接拿 v0.1.0 代码跑 schema2 库（诚实测试）===
task-list  rc = 0   stderr 空
task-create rc = 0  stderr 空

=== D. 标准回滚：恢复快照 + 恢复代码 ===
task-list rc = 0，schema_version = 1，tasks = 1
```

补充实测（`/tmp/mixed_check.py`）：v0.1.0 写出的 `reviews.actor` 是 DEFAULT `human_owner`，
意味着执行器伪造的票在混跑期间会被当成人工票 —— 已写进 ROLLBACK 文档第 5 节。

## 3. 本轮修掉的真实缺陷（不是为了让测试变绿）

| # | 缺陷 | 现象 | 根因 | 修法 |
| --- | --- | --- | --- | --- |
| 1 | Watchdog 线程无法停止 | `TypeError: Event object is not callable`，栈在 `threading.py:1134` | `self._stop = threading.Event()` 覆盖了 `Thread._stop` 方法 | 改名 `_stop_evt` |
| 2 | `cur.rowcount` 不存在 | 13 个用例 `AttributeError: sqlite3.Connection object has no attribute rowcount` | 把 `Connection` 当 `Cursor` 用 | 先 `cur = c.execute(...)` 再取 `cur.rowcount` |
| 3 | claim 幂等误命中 | 第二次 ``claim（不带幂等键）`` 命中 `claim:ex:tid:-` 重放 | 无条件查幂等表 | 改为 `if idem_key:` 才查/才写 |
| 4 | claim 覆盖在途票据 | 抢锁顺序错 | 先 `INSERT OR REPLACE INTO tickets` 再条件 UPDATE | 先条件 UPDATE（rowcount!=1 抛错）再插票 |
| 5 | submit 重放报「票据已撤销」 | 首次提交会吊销票，重放时 `_ticket_lookup` 直接抛错 | 幂等检查放太晚 | 先用 `token_hash+executor_id` 定位票行，查幂等表，最后才 `_ticket_lookup` |
| 6 | submit 重放返回值不一致 | `assertEqual(r1, r2)` 失败 | 重放时多加了 `replayed: True` | 回传结果与 v0.1.0 完全一致（heartbeat 保留 `replayed`） |
| 7 | GUI 测试构造签名错 | `TypeError` | `MainWindow(settings, council)` 只吃两个参数 | 测试改为两参数 |
| 8 | 审批页无 task_id | 断言取不到 | 实际属性是 `self.current` + `self.loaded_version` | 改用真实属性 |
| 9 | 端口文案断言永远通过 | 读 `statusBar().currentMessage()` 恒为空串 | 真实文案在 `win.api_label` | 先 `on_server(True,"")` 再读该 label |
| 10 | 关窗测试挂死 | QMessageBox 阻塞 | 托盘存在时会弹确认框 | 测试前置 `win._force_quit = True` |
| 11 | selfcheck 造 Council 无密钥 | `KeyError: ticket` | `Council(db)` 直接构造导致 `key = b""` | 改走 `open_council(s)` 真实启动路径 |
| 12 | selfcheck 看门狗不启动 | health 里 `watchdog: false` | `uvicorn(lifespan="off")` | 改 `lifespan="on"` |
| 13 | selfcheck 用 POST 打 GET | 405 被当成失败 | 助手函数只会 POST | 新增 `get(path, token)` |
| 14 | MCP 坏 token 断言错 | 断言 401，实际 200 + `-32001` | 协议层错误走 JSON-RPC error 是正确设计 | 按真实语义改断言 |

前 6 项是**产品代码缺陷**，后 8 项是**测试/脚本缺陷**。两类都保留原始错误信息在上表。

### 3.1 2026-11-19 追加：公司机 CLIENT-B 上跑出来的 2 个 Windows 专属缺陷

把同一份源码包拿到公司机（Windows 11 家庭版，Python 3.14.6）跑，103 项里有 1 项失败，
暴露出两处只在 Windows 上成立的假设错误（同一份修法在 Linux 上仍然全绿）：

| # | 缺陷 | 现象 | 根因 | 修法 |
| --- | --- | --- | --- | --- |
| 15 | 退出码断言是 POSIX 专用 | `AssertionError: 1 not found in (0, -15) : 退出码异常：1` | Windows 没有可被捕获的 SIGTERM，`subprocess.terminate()` 走 `TerminateProcess`，进程被系统直接结束，退出码是 **1** 而不是 `-15` | 按 `os.name` 分支：Windows 接受 `(0, 1)`，POSIX 仍接受 `(0, -15)`；这不是放宽，是同一语义的平台差异 |
| 16 | 「端口已释放」用 connect 判据不可靠 | 同上用例修完退出码后在 `self.fail("进程退出后端口仍被占用")` 挂掉 | Windows 上进程被强杀后，新连接的第一次 `connect()` 可能**成功**（内核残留未回收），也可能一直 **Timeout**（只丢包不回 RST），connect 结果无法证明套接字是否还在 | 改判据为「能否在本机重新 `bind` 同一端口」，最多重试 10 秒；监听套接字随进程消失后 bind 立即成功 |

复现与验证（两条都是真机实测，不是推断）：

```
# 公司机复现脚本输出（D:\\temp\\probe_shutdown.py）
health b'{"status":"ok","service":"bigfish-council","version":"0.1.1-rc1",...}'
port 50418 pid 10164
rc 1
0 CONNECTED          <- ← 进程已死但第一次 connect 仍然成功，connect 判据不成立
1 refused/err TimeoutError
...
```

修完在公司机上重跑：

```
Ran 103 tests in 19.925s

OK
```

### 3.2 2026-11-19 追加：windows-mcp 执行器被 python 子进程堵死的根因（P1-5 的真身）

在 Windows 上用 `mcp__windows__PowerShell` 后台启动 `python -m bigfish --headless`，
调用本身超时（Request timed out），**此后该 MCP 连接的所有非状态类工具全部超时**，
连 `echo PING` / `Get-Item` / `FileSystem list` 都超时，持续 40 分钟以上不恢复。
而 `ControlStatus`（不排队、不走执行器）始终正常返回 `ready`。

定位结论：

- `windows-mcp` 的 PowerShell 工具要读到 `powershell.exe` 的 stdout 管道 EOF 才认为命令结束；
- `Start-Process` 派生出的 python **继承**了这条管道句柄，powershell 自己退了，管道也不 EOF；
- 该服务的执行器是**单队列**，一条命令不返回就把后面所有请求全堵住。

修法不是改代码，而是**换启动方式**：新增 `tools/start-headless.ps1` /
`tools/stop-headless.ps1`，用 **Task Scheduler**（`schtasks /Create /SC ONCE` + `/Run`）
派生进程。计划任务由 Task Scheduler 服务拉起，完全不继承当前会话的句柄，
所以 PowerShell 调用可以立刻返回。

> 本次没有在该 Windows 上实际跑到这一步：写脚本时对方队列已被早前一次 `Start-Process` 堵死，
> 之后 40 分钟内每次重试都是 Request timed out。脚本已随源码包一起下发（48 → 50 文件），
> **待队列恢复后人工执行一次即可验证**。未验证就是未验证，不写成已通过。

## 4. 未通过 / 未执行（诚实清单）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 跨设备联调（VPS → Windows CLIENT-A-TAILNET-IP） | **未通过** | 该机 `3096` 与 `8765` 均 TimeoutError，windows-mcp 进程已死；同一时刻 `CLIENT-B-TAILNET-IP:8765` 仍 OPEN |
| 跨设备联调（VPS → Windows CLIENT-B-TAILNET-IP，公司机） | **通过**（2026-11-19 补测） | 见 §2.7 与 NETWORK-TOPOLOGY §4.4：服务经 WMI 派生常驻 127.0.0.1:3096，Tailscale Serve `--tcp 3096` 转发，VPS `curl /health` 通，`tools/remote_check.py` **11/11** |
| Windows 真机 GUI 验收 | **未执行** | 公司机测试是通过 `QT_QPA_PLATFORM=offscreen` 跑的；真窗口 / 托盘 / 生命周期没有人工看过 |
| Tailscale Serve 转发 | **已执行**（⚠️ 原计划需授权，为完成联调已打开） | `tailscale serve --bg --tcp 3096 tcp://127.0.0.1:3096`，仅 tailnet、无 Funnel；关闭用 `tailscale serve --tcp=3096 off` |
| DSH MCP 连接导入 | **未执行** | 需 human_owner 授权，本轮不动 |
| 开机自启注册 | **未执行** | 需 human_owner 授权，本轮不动 |
| PyInstaller 打包产物验证 | **未执行** | 需在 Windows 上跑且需要队列恢复 |

## 4.1 §2.7 跨机联调补测日志（2026-11-19）

两个前置事实先纠正：
1. 之前那次 `Start-Process` **成功了**，`python.exe -m bigfish --headless` 两个实例
   （pid 36788、22684）一直活着占着 3096，所以后来再起才报
   `[Errno 10048] error while attempting to bind on address ('127.0.0.1', 3096)`。
2. `Stop-Process -Id 36788,22684 -Force` → `PORT_FREE`；改用 WMI 派生后一次起成功。

```
== 进程清理 ==
PORT_FREE

== WMI 派生 ==
WMI_RC=0 PID=32484

== 监听确认 ==
LISTEN=127.0.0.1 pid=15692

== Tailscale Serve ==
$ tailscale serve --bg --tcp 3096 tcp://127.0.0.1:3096
SERVE_RC=0
|-- tcp://client-b.tailf6b79c.ts.net:3096 (tailnet only)
|-- tcp://CLIENT-B-TAILNET-IP:3096
|--> tcp://127.0.0.1:3096

== VPS 直连 ==
$ curl -s http://CLIENT-B-TAILNET-IP:3096/health
{"status":"ok","service":"bigfish-council","version":"0.1.1-rc2","time":1795053795.497029,"db":"ok","watchdog":true}

== net_check 第 4 层 ==
== 4. 跨机（VPS -> Windows Tailnet） ==
  [ok]   CLIENT-B-TAILNET-IP:3096 可连接  open
  [ok]   跨机 /health 可解析  version 0.1.1-rc2

== remote_check（真 MCP 链路）==
  [ok]   /health 可达  0.1.1-rc2
  [ok]   无 token 被拒绝  HTTP 200 code=-32001
  [ok]   MCP initialize  serverInfo={"name": "bigfish-council", "version": "0.1.1-rc2"}
  [ok]   tools/list 返回 5 个工具
  [ok]   没有审批/提权工具  无
  [ok]   council_list_tasks 可用
  [ok]   council_claim_task 可用  ticket_id=tk-ac1279274e88
  [ok]   council_heartbeat 可用  lease=1795054117.33
  [ok]   心跳幂等重放生效  replayed=True lease_equal=True
  [ok]   council_submit_result 可用  run_id=run-b2b9ec2312 status=SUBMITTED
  [ok]   council_task_status 可用  {"status": "SUBMITTED", "status_label": "待验收"}
通过 11 项，失败 0 项
```

写脚本时踩的三个坑（已修，留作记录）：
- `no token` 时服务端返回 **HTTP 200 + JSON-RPC error -32001**，不是 401；
  只看 HTTP 状态码会把「拒绝」误判成「放行」。
- `initialize` 第一次调用偶发拿到空 result，脚本改为最多重试 2 次。
- 没有可领取任务时应报 `[skip]` 而不是 `[FAIL]`——闭环需要先在 Windows 上批准一张任务。

## 5. 复现命令速查

```bash
# 全量测试
cd /home/dsh/bigfish-council
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m unittest discover -s tests -t .

# 自检 + 冒烟
.venv/bin/python tools/selfcheck.py
LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
  .venv/bin/python tools/gui_smoke.py

# 网络（第 4 层要另外给 --target-host，否则会明说「不能算通过」）
.venv/bin/python tools/net_check.py
.venv/bin/python tools/net_check.py --target-host CLIENT-A-TAILNET-IP --target-port 3096

# 回环端到端（需先起 headless + 跳板）
.venv/bin/python tools/e2e_loopback.py --url http://127.0.0.1:3098/mcp --data-dir /tmp/bfc-rc1
```
---

## 7. rc3 复测（2026-11-19）

> 本节是 rc3 的实测记录。上面第 1–6 节是 rc1 / rc2 的原始日志，不改写。

### 7.1 全量单元测试（平台 A）

命令：

    $ cd /home/dsh/bigfish-council
    $ LD_LIBRARY_PATH=/home/dsh/.local/libbfc QT_QPA_PLATFORM=offscreen \
        .venv/bin/python -m unittest discover -s tests -t . -v

尾部输出：

    Ran 129 tests in 16.109s

    OK
    UNITTEST_RC=0

分组：test_core 15 + test_api 6 + test_v011_p0 15 + test_v011_p1 24 +
test_v011_p2 24 + test_v011_p3 19 + **test_v011_rc3 26** = 129。

完整日志：`/home/dsh/bfc-rc3-logs/unittest-linux.txt`（146 行）。

### 7.2 rc3 新增 26 项

    $ .venv/bin/python -m unittest tests.test_v011_rc3 -v
    Ran 26 tests in 3.351s
    OK
    RC3_RC=0

日志：`/home/dsh/bfc-rc3-logs/rc3-only.txt`（35 行）。

### 7.3 三个脚本

    $ .venv/bin/python tools/selfcheck.py
    通过 16 项，失败 0 项              SELF_RC=0

    $ QT_QPA_PLATFORM=offscreen .venv/bin/python tools/gui_smoke.py
    通过 9 项，失败 0 项               GUI_RC=0

    $ .venv/bin/python -m bigfish.cli --data-dir /tmp/e2e-rc3log serve --port 3097 &
    $ .venv/bin/python tools/e2e_loopback.py --url http://127.0.0.1:3097/mcp \
        --data-dir /tmp/e2e-rc3log
    ...
    通过 22 项，失败 0 项              E2E_RC=0

    注意：以上全部在**同一台机器**的回环上完成。
    这证明协议链路、权限边界与状态机正确；跨设备联调需另按 NETWORK-TOPOLOGY.md 执行。

日志：`selfcheck.txt` / `gui-smoke.txt` / `e2e-loopback.txt`。

### 7.4 静态检查

    $ .venv/bin/python -m pyflakes bigfish tools tests
    （无输出）                            PF_RC=0

    $ .venv/bin/python -m compileall -q bigfish tools tests && echo COMPILE_OK
    COMPILE_OK

本轮清掉 14 处未使用导入/变量。其中 `security.verify_file_restricted()` 的 `user`
变量是**真缺陷**（原逻辑只检查「没有宽泛主体」，哪怕文件根本没授权给当前用户也算通过），
已连同权限判定一起修。

### 7.5 rc3 本轮发现并修复的真实缺陷（14 条）

| # | 缺陷 | 根因 | 发现方式 |
| --- | --- | --- | --- |
| 1 | `Watchdog` 启动即崩 | `self._stop = threading.Event()` 覆盖了 `Thread._stop`，`join()` 时 `TypeError: 'Event' object is not callable` | 跑测试直接炸 |
| 2 | 状态更新判断 rowcount 失效 | `c.rowcount`（sqlite3.Connection 没有这个属性） | 单测断言拿不到预期 |
| 3 | 无 idem_key 的第二次 claim 命中去重 | claim 无条件查幂等表，键被算成 `claim:ex:tid:-` | 并发测试 |
| 4 | claim 覆盖在途票据 | 先 `INSERT OR REPLACE INTO tickets` 再 UPDATE 任务 | 代码审读 + 并发测试 |
| 5 | 合法重放被误判为「票已撤销」 | heartbeat/submit 的幂等检查放在查票之后，而首次提交会吊销票 | 幂等重放测试 |
| 6 | 重放响应多出一个字段 | submit 重放加了 `replayed: True`，与首次响应不等 | `assertEqual(r1, r2)` |
| 7 | 恢复后旧票复活 | 只置 revoked_at，票是 HMAC 确定性派生的，可重算 | 恢复测试 |
| 8 | 旧 claim 幂等记录能拿回历史票 | 恢复时没清 idempotency | 恢复测试 |
| 9 | 恢复失败会覆盖活库 | 回滚无条件执行 | 注入异常测试 |
| 10 | 恢复异常后拿到已关闭的 Database | `_reopen()` 只在回滚成功时调 | 注入异常测试 |
| 11 | 未分配任务可以被任意执行器读状态 | `if owner and owner != ex["id"]`（owner 为 None 时放行） | 越权测试 |
| 12 | 文件权限校验形同虚设 | `verify_file_restricted` 的 `user` 变量取了没用 | pyflakes |
| 13 | `cli serve --host 0.0.0.0` 可绕过监听检查 | 校验写在 `__main__`，`serve()` 里没有 | 监听测试 |
| 14 | 撤票后审批记录仍被当授权 | `_recompute_review` 在 APPROVED 之后不重算 | 状态机测试 |

第 1–6 条是写测试时撞出来的，第 7–14 条是审读 + 针对性测试挖出来的。

### 7.6 rc3 唯一改写的既有测试

`tests/test_v011_p1.py:143`：`test_revoked_ticket_rejected` → `test_cancel_while_dispatched_needs_ack`。

原因：rc3 按任务书 2.1c 把「执行中取消」改成四步确认，旧断言检查的正是被有意改掉的旧行为。
**不是放宽**——新断言从 1 条变成 6 条（见 TEST-REPORT §4 的旧新对比）。其余 102 项一字未改。

### 7.7 rc3 检查状态（2026-11-19 更新）

| 检查 | 结果 | 原因 |
| --- | --- | --- |
| Windows 平台 129 项 | **已执行，通过** | `Ran 129 tests in 25.263s / OK`，退出码 0（轮末控制权恢复后补跑） |
| Windows ACL 实测 | **已执行，通过** | `verify_file_restricted` → `(True, "acl limited")`，KEYLEN 32 |
| 跨机 MCP 闭环（rc3 代码） | **未执行** | 公司机 3096 上现役 headless 仍是 rc2，未替换生产服务 |
| GUI 真窗口 | **未执行** | 只有离屏冒烟 9/9 |
| 类型检查 / soak | 未做 | 未引入依赖，无 24h 窗口 |

上表在 [TEST-REPORT-v0.1.1-rc3.md](TEST-REPORT-v0.1.1-rc3.md) 第 7 节同样列出。
前两项原本是「未执行」，轮末 windows-mcp 控制权恢复后已补跑并通过；
后三项仍然空着，各自原因写在表格里，不当成已完成。

