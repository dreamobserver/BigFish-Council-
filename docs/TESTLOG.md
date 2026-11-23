# 实测记录

环境：Debian 12 / kernel 5.15.0-187-generic，Python 3.11.2（VPS）与 Python 3.13.11（CLIENT-A），
Node v22.23.2，DSH 0.2.2-rc.1。

## 1. 单元测试

### VPS 侧
```
$ .venv/bin/python -m unittest discover -s tests -v
Ran 21 tests ... OK
```

### Windows 侧（CLIENT-A，D:\BigFishCouncil\bigfish-council）
```
$ .venv\Scripts\python.exe -m unittest discover -s tests
.....................
----------------------------------------------------------------------
Ran 21 tests in 4.043s

OK
```

覆盖点：单模型 approve 不推进、comment 不计票、双通过进 PENDING_APPROVAL、旧版本票失效、
未审批不可领取、重复 claim 失败、回传只到 SUBMITTED、坏 token 401、吊销票失效、
幂等提交、心跳超时 BLOCKED、MCP 无 approve 工具、/health 不泄露任务。

## 2. 本机闭环（进程内，127.0.0.1:3097）

```
status after votes: PENDING_APPROVAL
health: {"status": "ok", "service": "bigfish-council", "version": "0.1.0", "db": "ok"}
available: ['task-1d03349d9c']
prompt leaked? False
hb: {"ok": true, "task_status": "RUNNING", "heartbeat_ttl": 120}
submit: {"ok": true, "task_status": "SUBMITTED"}
status: {"status": "SUBMITTED", "status_label": "待验收"}
bogus token rejected: HTTP 401: {"detail":"执行器令牌无效"}
overview: {"counts": {"SUBMITTED": 1}, "total": 1}
```

## 3. MCP 闭环（走 127.0.0.1:3098 回环跳板 —— DSH 实际走的路径）

```
initialize: bigfish-council 2025-03-26
tools: council_list_tasks, council_claim_task, council_heartbeat, council_submit_result, council_task_status
无审批工具? true
available: {"ok":true,"tasks":[{"id":"task-eb403220ff","title":"P3 联调：README 补一节","risk":"low","status":"APPROVED","current_version":1}]}
prompt 不在列表里? true
claim: {"task_id":"task-eb403220ff","version":1,"has_prompt":true,"ticket":"bfc_tk_Dbl..."}
重复领取被拒? true {"ok":false,"error":"任务当前状态「已派工」不可领取"}
heartbeat: {"ok":true,"task_status":"RUNNING","heartbeat_ttl":120,"cancel":false,"pause":false}
heartbeat idem: true
submit: {"ok":true,"task_status":"SUBMITTED"}
status: {"status":"SUBMITTED","status_label":"待验收"}
```

这条链路证明：DSH 侧只要把 MCP 指向 `http://127.0.0.1:3098/mcp` 并带上执行器 Token，
就能完成「列任务 → 领取（拿 Prompt）→ 心跳 → 回传 → 查状态」全流程，且拿不到任何审批能力。

## 4. 网络实测（只读）

- VPS 容器可直接访问 Tailnet IP：`CLIENT-A-TAILNET-IP:8765 OPEN`、`:22 OPEN`、`:445 OPEN`。
- MagicDNS 不可用（`getent client-a` 失败），必须用 IP。
- 容器内无 `tailscale` CLI / 无 `/dev/net/tun`，但与宿主共享 netns，所以 100.x 可达。

## 5. GUI 离屏冒烟（VPS 无显示器，QT_QPA_PLATFORM=offscreen）

VPS 是精简 Debian 12 容器，没有 GL/X11 运行库，也不允许写 `/var/lib/apt/lists`，
所以按 Debian bookworm 的 `Packages.gz` 索引逐个取 deb、`dpkg-deb -x` 解到
`~/.local/libbfc`，运行时用 `LD_LIBRARY_PATH` 指过去。补齐 libGL → libxkbcommon →
libGLdispatch → libxcb → libdbus 这批之后即可离屏拉起 Qt。

```
$ LD_LIBRARY_PATH=~/.local/libbfc QT_QPA_PLATFORM=offscreen .venv/bin/python tools/gui_smoke.py
  [ok]   主窗口构造成功
  [ok]   五个页面都在  count=5 keys=5
  [ok]   页面可切换：overview
  [ok]   页面可切换：tasks
  [ok]   页面可切换：council
  [ok]   页面可切换：approval
  [ok]   页面可切换：monitor
  [ok]   状态栏文案  API：已监听 127.0.0.1:3097
  [ok]   待审批任务被识别

通过 9 项，失败 0 项
GUI 冒烟通过。
```

说明：这一步只能证明「五个页面能构造、能切换、能刷新、内嵌 API 线程起得来、
退出路径不崩」。真实渲染效果与托盘图标行为仍需在 CLIENT-A 桌面上人眼确认。

## 6. 未完成 / 已知问题

- **CLIENT-A 上的 windows-mcp 动作类工具（PowerShell / FileSystem / Process / Wait）在
  `pythonw -m bigfish` 启动之后开始全部超时**，只有 `ControlStatus` 仍能 0.4s 返回。
  现象符合「工具执行队列被一个永不返回的调用占死」：用 `Start-Process` 拉起的
  `pythonw` 继承了 MCP 服务的 stdout 管道，父进程读 EOF 永远等不到。
  影响：无法再通过该通道在 Windows 上执行命令。
  处置：见 `docs/WINDOWS-MCP-RECOVERY.md`（需要在本机重启一次 windows-mcp）。
- 因此「Windows 作为服务端、DSH 跨机取任务」这一段只完成了协议与代码验证
  （本机回环模拟），尚未在真实 Windows 主机上跑通端到端。
  2026-11-18 03:0x 复测：`ControlStatus` 523ms 正常，`Wait` / `FileSystem` / `Process`
  仍然 12s 超时 —— 该队列依旧被占，只能由本机重启 windows-mcp 解除。
- GUI 只在离屏模式验证过构造与切换，未做真实窗口截图与托盘交互验证。
