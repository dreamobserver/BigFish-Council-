# Windows 本机人工操作说明（v0.1.1-rc3）

这份是给**人**看的：每一步都能照着敲，每步都有「应该看到什么」。
凡是需要你亲自做的，都标了 🔸。

> **rc3 状态（2026-11-19 更新）**：rc3 的 129 项回归**已经在公司机上跑通**。
> 公司机 CLIENT-B（Windows 11 / Python 3.14.6 / SQLite 3.50.4），
> 结果 `Ran 129 tests in 25.263s / OK`、退出码 0；自检 16/16、GUI 冒烟 9/9。
> 此前拦路的 windows-mcp `USER_CONTROL`（人工接管）在轮末解除。下面第 2 节现在是**已完成记录**，
> 换机器或换版本时照着重跑即可。

## 0. 前置

| 项 | 要求 |
| --- | --- |
| 系统 | Windows 10/11（本机是 Win11 家庭版中文版，16GB 内存） |
| Python | 3.10+（家里机 Miniconda 3.13.11 跑过 103 项 rc2；公司机 3.14.6 **已跑过 rc3 的 129 项全绿**） |
| 磁盘 | 至少 2GB 空闲（venv + PySide6 约 1.2GB） |
| 网络 | 无需公网；Tailscale 已登录即可 |

源码包：`bfc-src-v011rc3.zip`（**57 个文件，约 228 KB**，2026-11-19 重打，含 rc3 全部代码与 129 项测试；
在 VPS 上解包到独立目录跑过 129 项全绿）。rc2 的 `bfc-src-v011rc2.zip`
（51 文件 / 150250 字节）已被取代，别混用。

## 1. 解包并建环境

```powershell
# 假设包已放到 D:\BigFishCouncil\
cd D:\BigFishCouncil
Expand-Archive .\bfc-src-v011rc2.zip -DestinationPath .\bigfish-council -Force
cd .\bigfish-council

# 建 venv（用系统 Python 或 Miniconda 都行）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**应该看到**：pip 最后一行 `Successfully installed FastAPI-0.163.2 PySide6-6.12.0 ...`。
若 PySide6 下载慢，可加国内源 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。

## 2. 跑测试（必须先全绿再往下）

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

**应该看到**：

```
Ran 129 tests in ~20s

OK
```

**看到 FAILED 就停在这里**，把完整输出贴回来，不要继续下一步。

**2026-11-19 实测**（公司机 CLIENT-B，独立目录 `D:\BigFishCouncil\bigfish-council-rc3`）：

```text
Ran 129 tests in 25.263s

OK
```

退出码 0，日志 144 行，129 个 `... ok`，0 个 `FAIL:` / `ERROR:`。
同一目录 `D:\temp\rc3-win.log` 留存原始输出。

rc3 新增的 26 项里有 7 项是**真并发**（`threading.Barrier`），Windows 上可能比 Linux
慢几秒，只要最后是 OK 就行；如果是 TIMEOUT 或死锁，把卡住的用例名记下来。


## 3. 自检 + GUI 冒烟

```powershell
.\.venv\Scripts\python.exe tools\selfcheck.py
```

**应该看到**：`通过 16 项，失败 0 项`。

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe tools\gui_smoke.py
Remove-Item Env:\QT_QPA_PLATFORM
```

**应该看到**：`通过 9 项，失败 0 项`，其中有 `API：已监听 127.0.0.1:3097`。

## 4. 启动 GUI（真实窗口）

```powershell
.\.venv\Scripts\python.exe -m bigfish
```

**应该看到**：

- 深色窗口，左侧导航五项：总览 / 任务 / 议事 / 审批 / 监控；
- 总览页顶部显示 `API：已监听 127.0.0.1:3096`；
- 右下角托盘图标出现（关窗口会问「最小化到托盘还是退出」）。

### 4.1 确认服务真的在跑

另开一个 PowerShell：

```powershell
Invoke-RestMethod http://127.0.0.1:3096/health | ConvertTo-Json
```

**应该看到**：

```json
{
  "status": "ok",
  "service": "bigfish-council",
  "version": "0.1.1-rc2",
  "time": 1795...,
  "db": "ok",
  "watchdog": true
}
```

`watchdog: true` 是重点——说明后台看门狗跟着服务起来了。

### 4.2 确认不会绑到外网

```powershell
Get-NetTCPConnection -LocalPort 3096 | Select-Object LocalAddress,LocalPort,State
```

**应该看到**（还没开 Serve 时）：`python` 进程那一行只有 `127.0.0.1`。

开了 Tailscale Serve 之后会多出 tailscaled 的两行，**别慌，那不是 python 在绑公网**：

```
LocalAddress              Proc        OwningProcess
------------              ----        -------------
fd7a:115c:a1e0::d233:a24c tailscaled  10740   <- Serve 的 IPv6 转发
127.0.0.1                 python      15692   <- 真正的 API
100.64.0.11            tailscaled  10740   <- Serve 的 IPv4 转发
```

正确判据是「只看 python 那行」，可以用：

```powershell
Get-NetTCPConnection -LocalPort 3096 -State Listen |
  Where-Object { (Get-Process -Id $_.OwningProcess).ProcessName -like 'python*' } |
  Select-Object LocalAddress,OwningProcess
```

**应该只返回 `127.0.0.1` 一行。** 出现 `0.0.0.0` 才是真出问题了。

### 4.3 headless 常驻（推荐用于联调，不弹窗）

GUI 开着不适合长时间挂着，联调时用 headless 更稳。

⚠️ **绝对不要**通过 windows-mcp 的 `PowerShell` 工具用 `Start-Process` 后台起 python。
powershell 的 stdout 管道会被子进程继承，windows-mcp 等不到 EOF，它那个单队列执行器就会被
永久占死，之后任何工具调用都是 `Request timed out`（真实踩过，40 分钟没恢复）。

正确做法二选一：

**A. 计划任务（脚本，未真机验证）**

```powershell
powershell -ExecutionPolicy Bypass -File tools\start-headless.ps1
powershell -ExecutionPolicy Bypass -File tools\stop-headless.ps1
```

> ⚠️ 这条路**没跑通**：`schtasks /Create ... /TR` 带嵌套引号时返回码
> `-2147467259`。脚本留着但别指望它一次成，真失败就改用 B。

**B. WMI 派生（2026-11-19 实测成功）**

```powershell
$cl = 'cmd.exe /c cd /d "D:\BigFishCouncil\bigfish-council" && ' +
      '".venv\Scripts\python.exe" -m bigfish --headless ' +
      '--data-dir "D:\BigFishCouncil\data" --port 3096 > "D:\BigFishCouncil\data\headless.log" 2>&1'
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cl }
```

**应该看到**：`ReturnValue=0`、`ProcessId=<某个 pid>`，而且 PowerShell **立刻返回**
（WMI 派生的进程不继承当前 shell 的 stdout 管道，所以不会堵住 windows-mcp 的队列）。

然后确认：

```powershell
Invoke-RestMethod http://127.0.0.1:3096/health | ConvertTo-Json
```

```json
{"status":"ok","service":"bigfish-council","version":"0.1.1-rc2","db":"ok","watchdog":true}
```

> 如果这里报 `[Errno 10048]`，说明已经有一个实例在跑了。别急着起新的，
> 先 `Get-NetTCPConnection -LocalPort 3096 -State Listen` 看是哪个 pid，
> 是旧的就 `Stop-Process -Id <pid> -Force` 再起。
>
> 注意：WMI 起的进程**重启机器就没了**，要常驻得自己解决开机自启（本轮未做）。

### 4.4 D 盘数据目录

本手册统一用 `D:\BigFishCouncil\data` 作为数据目录（`--data-dir` 显式指定）。
不指定时会落在 `%LOCALAPPDATA%\BigFishCouncil`，两者都可以，但联调时建议显式指定，
方便找日志。

## 5. 走一遍完整闭环（GUI）

1. **任务页** → 新建任务：标题随便、目标写「验证 rc1」、风险 low、Prompt 写 `echo hello-rc1`；
2. **任务页** → 选中该任务 → 送审；
3. **议事页** → 左侧 GPT 栏粘一段话 → 保存评论（此时状态还是「审查中」）；
4. **议事页** → GPT 栏投 approve，Gemini 栏也投 approve → 状态变「待审批」；
5. **审批页** → 选中任务 → 看四个区（目标/范围/风险/双模型审查）→ 点「批准并派工」；
6. 状态变「已批准」。

到这一步，人类侧操作就完成了。下面要交给 DSH 领。

## 6. 🔸 生成执行器 Token（需你确认后再做）

```powershell
.\.venv\Scripts\python.exe -m bigfish.cli executor-add dsh-cloud --role executor
```

**会输出一次明文 Token**（`bfc_ex_...`）。

⚠️ 这个 Token 只显示这一次，库里只存哈希。**先别急着往 VPS 上填**，
因为它一旦填进 DSH 的 MCP 配置，DSH 就有了领取任务的能力。

## 7. 🔸 启动 VPS 跳板（需你确认）

在**VPS VPS** 上（不是 Windows）：

```bash
# 目标地址按实际情况填：公司机 100.64.0.11，家里机 100.64.0.12
BFC_PROXY_TARGET_HOST=100.64.0.11 BFC_PROXY_TARGET_PORT=3096 \
  nohup node /home/dsh/bigfish-council/tools/bfc-proxy.mjs > /tmp/bfc-proxy.log 2>&1 &

sleep 2
curl -s http://127.0.0.1:3098/health
```

**应该看到**：Windows 那台的 health JSON 被原样透传出来（`"service":"bigfish-council"`）。

如果这里 timeout，说明 VPS 到 Windows 的 3096 不通——先查 Tailscale 和 Windows 防火墙，不要往后走。

### 7.1 直接走 Tailnet 也行（2026-11-19 实测通过）

跳板只对 **dsh-mcp-connector** 有必要（它只接受回环/RFC1918，Tailnet 是 CGNAT，填 100.x 会被拒）。
普通 HTTP 可以直接打 Tailnet IP，前提是 Windows 侧开了 Serve：

```powershell
# Windows 侧（管理员或普通用户均可，实测无需管理员）
tailscale serve --bg --tcp 3096 tcp://127.0.0.1:3096
tailscale serve status
```

```
|-- tcp://client-b.tailf6b79c.ts.net:3096 (tailnet only)
|-- tcp://100.64.0.11:3096
|--> tcp://127.0.0.1:3096
```

然后 VPS 上直接：

```bash
curl -s http://100.64.0.11:3096/health
# {"status":"ok","service":"bigfish-council","version":"0.1.1-rc2","db":"ok","watchdog":true}

BFC_BASE_URL=http://100.64.0.11:3096 BFC_TOKEN=<执行器 token> \
  /home/dsh/bigfish-council/.venv/bin/python tools/remote_check.py
```

实测 **11/11 全过**（claim → heartbeat 含幂等重放 → submit）。

关闭：`tailscale serve --tcp=3096 off`。**不要**用 `tailscale funnel`。

## 8. 🔸 导入 DSH MCP 连接（需你确认）

编辑 `dsh-mcp-bigfish.json`，把 `Bearer <TOKEN>` 换成第 6 步拿到的真实 Token，然后：

```json
{"mcpServers":{"bigfish-council":{"url":"http://127.0.0.1:3098/mcp","headers":{"Authorization":"Bearer bfc_ex_..."}}}}
```

用 `mcp_connector_import_json` 导入。导入后 DSH 会多出 5 个工具，**没有 approve**。

## 9. 交叉验证清单（出问题时按这个查）

| 症状 | 先查 | 命令 |
| --- | --- | --- |
| GUI 起来但 health 连不上 | 端口被占 / 进程崩了 | `Get-NetTCPConnection -LocalPort 3096` |
| health 通但 DSH 取不到任务 | 任务不是 APPROVED | GUI 审批页看状态 |
| DSH 报 401 | Token 错或执行器被吊销 | `python -m bigfish.cli overview` |
| 领取后马上 BLOCKED | 心跳没发 / 超时 120s | 监控页看心跳时间 |
| 回传后不是 DONE | **设计如此**，必须人类验收 | 审批页点「验收通过」 |
| 跳板 timeout | VPS→Windows 3096 不通 | VPS 上 `tools/net_check.py --target-host ...` |
| windows-mcp 全部工具 Request timed out | 执行器被占死（多半是上一条命令没返回） | 在本机重启 windows-mcp；**不要**用 `Start-Process` 起长任务 |

## 10. 停止与清理

```powershell
# headless 走计划任务时，用配套脚本停（会同时释放端口）
powershell -ExecutionPolicy Bypass -File tools\stop-headless.ps1

# GUI 里点退出，或按进程名清理：
Get-Process python,pythonw -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -like "*bigfish-council*" } | Stop-Process
```

```bash
# VPS 上
pkill -f bfc-proxy.mjs
```

数据目录 `%LOCALAPPDATA%\BigFishCouncil` 不会被删。要重来就整个删掉（**先备份 secrets\ticket.key**）。

---

## 11. rc3 专项：Windows 上必须额外验的东西

以下五项在 Linux 上验不了，只有 Windows 能验。rc3 本轮**已跑三项**
（11.1 密钥 ACL、11.2 恢复文件锁、11.5 并发测试，后者已随全量一起跑过），
11.3 与 11.4 仍需人工在机器前确认。

### 11.1 密钥文件 ACL（rc3 新要求）

rc3 的 `security.secure_file()` 在 Windows 上走 `icacls`，不再依赖 `chmod`（Windows 上无效）。
验证：

```powershell
# 密钥应该在建库时自动生成
$key = "D:\BigFishCouncil\data\secrets\ticket.key"
icacls $key
```

**应该看到**：只有 `CLIENT-B\user-b:(F)`，**没有** `Everyone`、`Users`、
`Authenticated Users`、`INTERACTIVE`。

```powershell
# 用程序自己的判定再核一遍
cd D:\BigFishCouncil\bigfish-council
..venv\Scripts\python.exe -c "from pathlib import Path; from bigfish.security import verify_file_restricted; print(verify_file_restricted(Path(r'D:\BigFishCouncil\data\secrets\ticket.key')))"
```

**应该看到**：`(True, '...')`。若返回 `False`，说明 ACL 里混进了宽泛主体，
用 `icacls $key /inheritance:r /grant:r "$env:USERNAME:(F)"` 收紧后重测。

### 11.2 恢复时的文件锁（Windows 特有）

Windows 上 `os.replace()` 遇到被占用的文件会失败（WinError 32）。rc3 的恢复流程
会先 `db.close()` 再替换，但如果**同时开着第二个 GUI**，仍然会失败。

```powershell
# 先确认只有一个实例
Get-Process python* | Select-Object Id, Path

# 然后跑一次恢复（用一个 rc3 导出的 operational 包）
..venv\Scripts\python.exe -m bigfish.cli --data-dir D:\BigFishCouncil\data inspect D:\temp\bfc-backup.zip
..venv\Scripts\python.exe -m bigfish.cli --data-dir D:\BigFishCouncil\data restore D:\temp\bfc-backup.zip
```

**应该看到**：`restored=True`、`key_rotated=True`、`restore_epoch` 比之前大 1、
`blocked_inflight` 是恢复前处于执行中的任务数。

### 11.3 恢复后旧执行票必须失效（rc3 最关键的一条）

这是 rc3 的核心修复，必须在 Windows 上也验一次：

1. 在 GUI 里让一张任务走到 DISPATCHED（领取后）；
2. 记下执行票（在 VPS 的 executor_client 日志里，或在 Windows 上手工 claim 拿到）；
3. 执行一次 `restore`（用恢复前的备份）；
4. 用**旧票**发一次心跳。

**应该看到**：报错「执行票已失效（数据库已从备份恢复），请重新领取」
或「执行票已被撤销」，**绝不能**返回 `ok: true`。

### 11.4 GUI 真窗口验收（rc3 新增了 CANCELLING 展示）

离屏冒烟只能证明控件构造成功，证明不了：

- `CANCELLING` 状态在列表里显示为「取消中」且颜色正确；
- 改 Prompt 时版本冲突会弹明确提示（不是静默覆盖）；
- 恢复备份后界面上的 `restore_epoch` / `key_rotated` 显示正确；
- 撤销审批后任务回到议事中，且审批记录仍在（标为失效）。

### 11.5 并发测试在 Windows 上会不会卡

rc3 新增的 7 项并发测试用 `threading.Barrier`。Windows 的 sqlite 文件锁语义与 Linux 不同，
可能出现「两个线程都在等对方放锁」。本轮的代码在 `BEGIN IMMEDIATE` 失败时会释放 RLock 并抛错，
理论上不会死锁，但**只在 Linux 验过**。

```powershell
# 单独跑并发组，看有没有卡住
..venv\Scripts\python.exe -m unittest tests.test_v011_rc3.TestConcurrency -v
```

**应该看到**：7 项全过，总耗时几秒。若某项卡住超过 60 秒，用 `Ctrl+C` 中断并把
卡住的用例名记下来——那是需要修的 Windows 专属问题，不是「环境差异」。

