# 网络拓扑与端口配置（v0.1.1-rc1）

> 本文只记录**实测过**的事实。任何未实测的链路一律标注「未验证」，不允许用同机回环结果代替跨机结论。

## 1. 端口真源

代码里的唯一真源是 `bigfish/config.py`，文档、自检、GUI 都从这里读，不再各处写死数字。

| 角色 | 端口 | 默认绑定 | 归属 | 用途 |
| --- | --- | --- | --- | --- |
| `windows_api` | **3096** | `127.0.0.1` | Windows 本机 | 本地 API + MCP 服务，GUI 内嵌线程启动 |
| `vps_proxy` | **3098** | `127.0.0.1` | VPS-HOST | `tools/bfc-proxy.mjs` 回环跳板 |
| `test_only` | **3097** | `127.0.0.1` | 任意 | 单测 / GUI 冒烟 / 临时实例，不进任何默认配置 |

约束（有测试覆盖，见 `tests/test_v011_p3.py::TestPortTopology`）：

- API 端口 ≠ 跳板端口，否则跳板会把请求打给自己形成环路；
- `Settings.is_loopback` 只认 `127.0.0.1` / `localhost` / `::1`；
- `python -m bigfish --host 0.0.0.0` 与 `--host CLIENT-A-TAILNET-IP` 都会**直接退出码 2**，
  stderr 打印「只允许监听回环地址」；
- `Settings.public_dict()` 输出给日志/文档时不含 token / secret / password / key 字段。

覆盖方式（优先级从低到高）：默认值 → `data_dir/config.json` → 环境变量
`BIGFISH_API_HOST` / `BIGFISH_API_PORT`。

## 2. 数据流

```
┌──────────────── Windows 本机（CLIENT-A, CLIENT-A-TAILNET-IP） ────────────────┐
│  BigFishCouncil.exe / python -m bigfish                                │
│    ├─ GUI（PySide6）：唯一能做人类审批、签发执行票的地方                │
│    └─ uvicorn 127.0.0.1:3096  ← 只绑回环，不监听 Tailnet、不监听公网    │
└───────────────────────────────┬────────────────────────────────────────┘
                                │  Tailscale tailnet（100.64.0.0/10）
                                │  无公网入口、无 Funnel
┌───────────────────────────────┴── VPS-HOST / DSH 容器 ─────────────┐
│  127.0.0.1:3098  tools/bfc-proxy.mjs                                   │
│     只放行 /health、/mcp*、/api/*，其余 404；304 行纯 http 转发        │
│                        │                                               │
│  DSH MCP Connector ────┘  URL: http://127.0.0.1:3098/mcp               │
│     Bearer <执行器 Token>                                              │
└────────────────────────────────────────────────────────────────────────┘
```

## 3. 为什么必须有 3098 跳板（不能直连 100.x）

DSH 的 MCP 连接器（`dsh-mcp-connector`）**只接受回环地址或 RFC1918 私网地址**。
Tailscale 的 `100.64.0.0/10` 属于运营商级 NAT（CGNAT）段，既不是回环也不是 RFC1918，
直连会被 URL 校验拒绝。

既有 `/home/dsh/wmcp-proxy-home.mjs` 的注释里已记录同一约束，`tools/bfc-proxy.mjs` 与之同构。
跳板本身的暴露面：

- 只监听 `127.0.0.1`（环境变量可改，但默认值就是回环）；
- 路径白名单 `/health`、`/mcp` 前缀、`/api/` 前缀，其他一律 404；
- 不解析、不记录请求体，不做任何鉴权改写（鉴权仍由 Windows 侧 Bearer 校验）；
- 目标地址默认 `CLIENT-A-TAILNET-IP:3096`，可用 `BFC_PROXY_TARGET_HOST/PORT` 覆盖。

## 4. 实测结果（2026-11-18，VPS 容器内）

### 4.1 配置层与本机层 — 全部通过

```
$ .venv/bin/python tools/net_check.py --skip-cross
== 1. 配置层 ==
  [ok]   API 默认只绑回环  127.0.0.1
  [ok]   三个端口互不冲突  [3096, 3098, 3097]
  [ok]   API 端口 != 跳板端口  3096 vs 3098
== 2. 本机 API ==
  [ok]   127.0.0.1:3096 可连接  open
  [ok]   /health 可解析  {"status":"ok","service":"bigfish-council","version":"0.1.1-rc1",
                          "db":"ok","watchdog":true,...}
  [ok]   健康检查里没有任务信息  db,service,status,time,version,watchdog
== 3. VPS 跳板 ==
  [ok]   127.0.0.1:3098 可连接  open
  [ok]   跳板 /health 透传成功  ..."version":"0.1.1-rc1"...
  [ok]   跳板转发到的是 bigfish-council  bigfish-council
  [ok]   跳板报告的版本与源码一致  0.1.1-rc1
通过 10 项，失败 0 项
```

### 4.2 跨机层（VPS → Windows Tailnet） — **未打通**

```
$ .venv/bin/python tools/net_check.py --target-host CLIENT-A-TAILNET-IP --target-port 3096
== 4. 跨机（VPS -> Windows Tailnet） ==
  [FAIL] CLIENT-A-TAILNET-IP:3096 可连接  TimeoutError
通过 10 项，失败 1 项
注意：本脚本不会因为跳板本机回环通就宣布跨机联调成功。
```

历史对比（同一容器，v0.1.0 阶段测过）：`CLIENT-A-TAILNET-IP:8765` 曾经 OPEN，
本轮 `:8765` 与 `:3096` 都是 TimeoutError；同一时段 `CLIENT-B-TAILNET-IP:8765` 仍 OPEN。
说明 VPS 侧 Tailscale 网络本身没坏，是 **CLIENT-A 这一端**（windows-mcp 服务挂了 /
Tailscale 掉线 / Windows 防火墙拦了新端口）不通。

### 4.3 换到公司机 CLIENT-B（CLIENT-B-TAILNET-IP）后的实测（2026-11-19）

端口扫描（VPS 容器内 node net.connect，4s 超时）：

```
CLIENT-B-TAILNET-IP:8765  OPEN          <- windows-mcp 活着
CLIENT-B-TAILNET-IP:445   OPEN
CLIENT-B-TAILNET-IP:5040  OPEN
CLIENT-B-TAILNET-IP:22    ECONNREFUSED
CLIENT-B-TAILNET-IP:3096  ECONNREFUSED  <- BigFish API 没起来
CLIENT-B-TAILNET-IP:3097  ECONNREFUSED
CLIENT-B-TAILNET-IP:3098  ECONNREFUSED
CLIENT-B-TAILNET-IP:18765 ECONNREFUSED  <- 该端口在 VPS 侧，不在公司机
```

公司机 MCP 通道经 VPS 跳板 `127.0.0.1:18765` 已打通（`ControlStatus` → `ready`），
并在那台机器上跑完 **103 项单元测试全绿 + 自检 16/16**。

当时 `python -m bigfish --headless` 没有成功常驻，`3096` 始终 ECONNREFUSED。
根因是启动方式：用 `mcp__windows__PowerShell` + `Start-Process` 后台拉起 python 时，
子进程继承了 powershell 的 stdout 管道，windows-mcp 等不到 EOF，单队列执行器被永久占死。

### 4.4 打通记录（2026-11-19，全部为实测输出）

**第一步：把死进程清干净。**
所谓"没起来"其实是那次 `Start-Process` **成功了**——两个 `python.exe -m bigfish --headless`
（pid 36788、22684）一直活着并占着 3096，后续启动才报
`[Errno 10048] error while attempting to bind on address ('127.0.0.1', 3096)`。
`Stop-Process -Id 36788,22684 -Force` 之后 `PORT_FREE`。

**第二步：用 WMI 派生，不用 Start-Process。**

```powershell
$cl = 'cmd.exe /c cd /d "D:\BigFishCouncil\bigfish-council" && ' +
      '".venv\Scripts\python.exe" -m bigfish --headless ' +
      '--data-dir "D:\BigFishCouncil\data" --port 3096 > "D:\BigFishCouncil\data\headless.log" 2>&1'
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cl }
# -> WMI_RC=0 PID=32484
```

WMI 派生的进程不继承 PowerShell 的 stdout 管道，PowerShell 立刻返回，**执行器队列不阻塞**。
（`schtasks /Create ... /TR` 带嵌套引号时返回码 `-2147467259`，计划任务路线本次未走通，
脚本 `tools/start-headless.ps1` 仍随包提供，但**未经真机验证通过**。）

**第三步：确认 python 只绑回环。**

```
Get-NetTCPConnection -LocalPort 3096 -State Listen | ? { (Get-Process -Id $_.OwningProcess).ProcessName -like 'python*' }
-> 127.0.0.1  pid=15692
```

⚠️ **加了 Serve 之后，不带进程过滤会看到三行，容易误判为「绑了公网」：**

```
LocalAddress              Proc        OwningProcess
------------              ----        -------------
fd7a:115c:a1e0::d233:a24c tailscaled  10740   <- Serve 的 IPv6 转发
127.0.0.1                 python      15692   <- 真正的 API
CLIENT-B-TAILNET-IP            tailscaled  10740   <- Serve 的 IPv4 转发
```

**判据是「python 进程只出现 127.0.0.1」**，tailscaled 那两行是 Serve 在做转发，
不代表进程绑了 0.0.0.0 或 Tailnet IP。

**第四步：Tailscale Serve 只对 tailnet 暴露，不开公网。**

```
tailscale serve --bg --tcp 3096 tcp://127.0.0.1:3096
tailscale serve status
-> |-- tcp://client-b.tailf6b79c.ts.net:3096 (tailnet only)
   |-- tcp://CLIENT-B-TAILNET-IP:3096
   |--> tcp://127.0.0.1:3096
```

Serve 是**转发**，不是把 API 绑到 0.0.0.0：`Get-NetTCPConnection` 依旧只看到 127.0.0.1，
符合 `__main__.py` 的回环硬约束。Serve 配置只存在于 tailnet，没有 Funnel、没有公网入口。

**第五步：VPS 直连验证（这才是跨机证据）。**

```
$ curl -s http://CLIENT-B-TAILNET-IP:3096/health
{"status":"ok","service":"bigfish-council","version":"0.1.1-rc2","time":1795053795.497029,"db":"ok","watchdog":true}
```

**第六步：跨机 MCP 全链路（`tools/remote_check.py`，11 项全过）。**

```
$ BFC_BASE_URL=http://CLIENT-B-TAILNET-IP:3096 BFC_TOKEN=<执行器 token> \
    .venv/bin/python tools/remote_check.py
== 目标 http://CLIENT-B-TAILNET-IP:3096 ==
  [ok]   /health 可达  0.1.1-rc2
  [ok]   无 token 被拒绝  HTTP 200 code=-32001
  [ok]   MCP initialize  serverInfo={"name": "bigfish-council", "version": "0.1.1-rc2"}
  [ok]   tools/list 返回 5 个工具  council_claim_task,council_heartbeat,council_list_tasks,council_submit_result,council_task_status
  [ok]   没有审批/提权工具  无
  [ok]   council_list_tasks 可用  task-f229cea99f
  [ok]   council_claim_task 可用  ticket_id=tk-ac1279274e88
  [ok]   council_heartbeat 可用  lease=1795054117.33
  [ok]   心跳幂等重放生效  replayed=True lease_equal=True
  [ok]   council_submit_result 可用  run_id=run-b2b9ec2312 status=SUBMITTED
  [ok]   council_task_status 可用  {"status": "SUBMITTED", "status_label": "待验收"}
通过 11 项，失败 0 项
本脚本走的是真实跨机链路，不是本地回环模拟。
```

这条链路里，**任务是在 Windows 上建的、双审查和人类批准都在 Windows 上做的、
领取/心跳/回传是 VPS 侧发起的真实 HTTP**，全程没有公网端口、没有 Funnel。

**尚未验证的部分（不当作已通过）：**
- Windows 侧 GUI 真窗口（非 offscreen）人工验收。
- 开机自启 / 计划任务常驻（WMI 起的进程重启机器就没了）。
- Tailscale Serve 的**重启持久性**（`--bg` 会持久化配置，但未做重启实测）。
- `tools/start-headless.ps1` 在本机上的执行结果（`schtasks` 参数问题未解）。

## 5. 已完成的联调替代证据（明确标注为同机）

`tools/e2e_loopback.py` 在**同一台机器**上跑完整链路：
建任务 → 双模型审查 → 人类批准 → 经 `127.0.0.1:3098/mcp` 领取 → 心跳 → 回传 → 人工验收。
22 项断言全过（见 TESTLOG-v0.1.1.md §4）。

它能证明：MCP 协议握手、Bearer 鉴权、工具暴露面、权限边界、状态机、幂等与错误语义。
它**不能**证明：Tailscale 跨机连通性、Windows 防火墙放行、两端时钟/版本一致。

## 6. 跨机验收时的检查清单（Windows 侧执行）

已在本轮全部执行通过的用 ✅ 标注，未执行的保持原样。

1. ✅ `Get-NetTCPConnection -LocalPort 3096 -State Listen` 必须看到
   `LocalAddress = 127.0.0.1`（**不能**是 `0.0.0.0`）。
2. ✅ `tailscale status` 里 CLIENT-B 与 vps-host 在线（`BackendState: Running`）。
3. ✅ 在 VPS 上 `curl http://CLIENT-B-TAILNET-IP:3096/health` 通。
   **启动方式用 WMI `Win32_Process.Create`；不要用 `Start-Process` 经 windows-mcp
   后台起 python，否则会堵死单队列执行器。**
4. ✅ 跨机走 `tailscale serve --bg --tcp 3096 tcp://127.0.0.1:3096`，
   **不得**把 API 改成绑 `0.0.0.0` 或 Tailnet IP 来绕过 —— 代码会直接拒绝启动。
5. ✅ `.venv/bin/python tools/net_check.py --target-host CLIENT-B-TAILNET-IP --target-port 3096`
   第 4 层通过（跨机 /health 解析成功）。
6. ✅ `tools/remote_check.py` 11 项全过（真 MCP 闭环：claim → heartbeat → submit）。
7. ⬜ 导入 DSH MCP 连接（该操作需 human_owner 授权，本轮未做）。
8. ⬜ 开机自启常驻（WMI 起的进程重启即消失，需注册计划任务，本轮未做）。

## 6.1 端口职责（打通后的实际拓扑）

```
CLIENT-B (Windows, CLIENT-B-TAILNET-IP)
  127.0.0.1:3096  BigFish Council API（只绑回环，代码强制）
        ^
        | tailscale serve --bg --tcp 3096（tailnet 内转发，无 Funnel、无公网）
        |
  CLIENT-B-TAILNET-IP:3096  <- 只在 tailnet 内可见

vps-host VPS (VPS-TAILNET-IP, 容器共享宿主 netns)
  DSH / 执行器客户端  --- Tailnet ---> CLIENT-B-TAILNET-IP:3096
  127.0.0.1:3098  bfc-proxy.mjs 跳板（给 dsh-mcp-connector 用，它只收回环/RFC1918）
```

VPS 侧不用改任何路由：容器已经共享宿主机的 netns，能直接访问 100.64/10。
唯一限制是 **dsh-mcp-connector 不接受 CGNAT 地址**，所以 DSH 内部的 MCP 连接
还是要走 `127.0.0.1:3098` 跳板；而普通 HTTP（执行器客户端、curl）可以直接打 Tailnet IP。

## 7. 与 v0.1.0 的差异

| 项 | v0.1.0 | v0.1.1-rc1 |
| --- | --- | --- |
| 端口定义 | 散落在 `gui.py`（写死 3096）、`__main__.py`、文档 | `config.py` 单一真源 + `PORT_ROLES` |
| 非回环绑定 | 允许启动 | **拒绝启动，退出码 2** |
| 跳板路径 | 已在 `tools/bfc-proxy.mjs` | 同（未改），补了 net_check 断言 |
| 网络验证 | 手工 curl | `tools/net_check.py`（4 层，含跨机层） |
| GUI 端口文案 | 写死 `API：已监听 127.0.0.1:3096` | 读 `settings.api_host/api_port` |

---

## 8. rc3 对网络层的改动（2026-11-19）

### 8.1 端口职责没变

| 端口 | 位置 | 用途 | rc3 变化 |
| --- | --- | --- | --- |
| 3096 | Windows | 本地 API / MCP | 无变化（仍只允许回环） |
| 3098 | VPS | `bfc-proxy.mjs` 回环跳板 | 无变化 |
| 3097 | 测试机 | 单测与冒烟临时端口 | 无变化 |

### 8.2 变的是「谁能决定监听地址」

rc2 的问题：非回环拒绝写在 `__main__.py`，`python -m bigfish.cli serve --host 0.0.0.0` 绕过去了。

rc3 把校验下沉到 `server.serve()` 和 `gui.run()`，四条入口全部经过 `check_bind_host()`：

    __main__.py     ─┐
    cli.py serve    ─┼─▶ server.serve() ─▶ check_bind_host() ─▶ uvicorn
    gui.py run()    ─┤                          ▲
    ApiServer.start()─┘                          │
                                          is_loopback_host()

拒绝清单（全部有测试）：`0.0.0.0`、`::`、`[::]`、`0:0:0:0:0:0:0:0`、
任何 RFC1918（10/172.16/192.168）、任何 CGNAT（100.64/10，即 Tailnet 地址）、任何公网 IP。

接受清单：`127.0.0.1`、`localhost`、`::1`、`[::1]`、`127.0.0.2`~`127.255.255.254`。

### 8.3 测试证据

`tests/test_v011_rc3.py::test_all_entry_points_reject_public_bind`：

- 7 个非回环地址逐个调 `check_bind_host()`，全部 `BindError`；
- 再用 `subprocess` 真跑 `-m bigfish --headless --host 0.0.0.0`，断言 **rc == 2**；
- 且断言端口**仍然可以 bind**（证明服务确实没起来，不只是打印了错误就退出）。

### 8.4 跨机链路状态（rc3 未重新验证）

2026-11-19 复测：`curl http://CLIENT-B-TAILNET-IP:3096/health` →

    {"status":"ok","service":"bigfish-council","version":"0.1.1-rc2",
     "time":1795071567.4,"db":"ok","watchdog":true}

链路是通的，但**跑的是 rc2**。所以 §4.4 的 11/11 闭环证据只能证明 rc2，
不能拿来当 rc3 的跨机证据。rc3 的跨机验证**未做**（windows-mcp 被人工接管）。

### 8.5 Tailscale Serve 现状（未变）

```
$ tailscale serve status
|-- tcp://client-b.tailf6b79c.ts.net:3096 (tailnet only)
|-- tcp://CLIENT-B-TAILNET-IP:3096
|-- tcp://[fd7a:...]:3096
    -> tcp://127.0.0.1:3096
```

只有 tailnet 内可见，**没有 Funnel，没有公网入口**。关闭命令：`tailscale serve --tcp=3096 off`。

⚠️ 开 Serve 后 `Get-NetTCPConnection -LocalPort 3096 -State Listen` 会返回三行
（两个 tailscaled + 一个 python）。判断服务是否只绑回环，**必须过滤只看 python 那行**：

    Get-NetTCPConnection -LocalPort 3096 -State Listen |
      Where-Object { (Get-Process -Id $_.OwningProcess).ProcessName -like 'python*' }

tailscaled 那两行是 Serve 的转发监听，不代表绑了公网。


---

## 9. rc4 对网络层的改动（2026-11-19）

### 9.1 端口职责没有变

rc4 没有新增任何**正在监听**的端口。3096 / 3097 / 3098 三行职责与 rc3 完全一致，
`tests/test_v011_p3.py:45` 那条「`PORT_ROLES` 恰好等于三行」的断言照旧通过。

### 9.2 新增的是「预留表」，不是「新服务」

`bigfish/config.py` 新增一个**平行字典** `PORT_RESERVED`，与 `PORT_ROLES` 互不相交：

| 端口 | 键 | 说明 | 现状 |
| --- | --- | --- | --- |
| 3099 | `PORT_RESERVED[3099] = mobile_rest` | 手机端 REST 网关 | 计划中，**未实现** |
| 3100 | `mobile_sse` | 手机端 SSE / 长连接推送 | 计划中，**未实现** |
| 3101 | `mcp_bridge` | 对外 MCP 桥接 | 计划中，**未实现** |
| 8765 | `windows_mcp` | Windows 侧 windows-mcp 的 HTTP 端口 | 外部进程，本项目**只连不听** |
| 18765 | `wmcp_proxy_a` | VPS 跳板：客户端 A 的 windows-mcp 回环转发 | 外部进程，本项目只连不听 |
| 18766 | `wmcp_proxy_b` | VPS 跳板：客户端 B 的 windows-mcp 回环转发 | 外部进程，本项目只连不听 |

为什么单独建字典而不并进 `PORT_ROLES`：`tests/test_v011_p3.py:45` 断言
`PORT_ROLES` **恰好**是 `windows_api / vps_proxy / test_only` 三行。
把未实现的端口塞进去，等于让测试对现实撒谎。现在测试同时断言两者**没有交集**：

```python
# tests/test_v011_rc4.py::TestAuxiliary::test_port_reserved_disjoint
self.assertEqual(set(PORT_RESERVED) & set(PORT_ROLES), set())
```

三条铁律（写进 `config.py` 注释，代码与文档引同一份）：

1. 本项目自己监听的端口只能绑回环；
2. 预留端口在真正实现前不得出现在 `PORT_ROLES` 或任何默认值里；
3. 8765 / 18765 / 18766 属于外部进程，本项目只连不听。

### 9.3 自检端口从 3099 挪到 3094（真实故障，不是洁癖）

`tools/selfcheck.py` 原先默认 3099。3099 在 rc4 里被预留为手机端 REST 网关，
所以先改到 3095。改完立刻踩到第二个坑：**本容器里 3095 已被 quota-hub 长期监听**
（`curl http://127.0.0.1:3095/health` → `{"ok":true,"service":"quota-hub","port":3095}`）。

原来的 selfcheck 在这种情况下会：

1. uvicorn 报 `[Errno 98] address already in use`，服务根本没起来；
2. 但脚本仍然去 GET /health，**读到的是 quota-hub 的响应**；
3. 于是打印「[FAIL] 服务在线 quota-hub」，然后继续往下跑，
   在 tools/list 处 `KeyError: 'result'` 崩掉。

rc4 的修法两条：

- 默认端口改成 **3094**（实测空闲）；
- 起服务后**先验证对面是不是本服务**（`health["service"] != "bigfish-council"` 直接退出并提示用
  BFBFC_SELFCHECK_PORT` 换端口），不再拿别人的 /health 往下瞎判。

修后实测：

    $ python tools/selfcheck.py
    健康检查
      [ok]   服务在线  bigfish-council
    ...
    通过 16 项，失败 0 项
    自检全部通过，可以启动 GUI。

### 9.4 跨机链路状态（rc4 仍未重新验证）

结论与 rc3 §8.4 相同：`CLIENT-B-TAILNET-IP:3096` 当时跑的是 **rc2**，
rc3 / rc4 的跨机闭环**都没重跑**。windows-mcp 通道在此期间多次失联，
被测机 Tailscale 状态在 online / offline 之间变化。

因此 rc4 **没有任何新的跨设备证据**。`tools/remote_check.py` 在 rc4 里被重写为
「不给 `--task-id` 就只读、给 `--allow-submit` 也只提交 `cancelled`」，
正是为了避免过去那种「为了证明链路通，顺手把真实任务切成 completed」的污染。
