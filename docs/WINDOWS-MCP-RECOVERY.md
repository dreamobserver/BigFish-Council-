# windows-mcp 卡死恢复

## 症状

`council_*` 之外，Windows MCP 的 `PowerShell` / `FileSystem` / `Process` / `Wait` /
`Snapshot` 全部超时（30s+ 无响应），但 `ControlStatus` 仍能在 0.4 秒内正常返回。
`tools/list` 也正常。

## 根因

Windows 上的 `windows-mcp` 用**单队列**串行执行动作类工具。一旦某个调用永不返回，
后续所有调用都在队列里等着。

触发条件：用 `Start-Process` 启动 `pythonw.exe`（或任何带 GUI/常驻的服务）时，
子进程**继承**了父进程的 stdout/stderr 管道。MCP 服务读这个管道等 EOF，
而常驻子进程不退出，EOF 永不出现 → 该次工具调用永久挂起。

## 恢复步骤（在本机 PowerShell 里执行）

```powershell
# 1. 杀掉那个占着管道不放的 pythonw / python
Get-Process pythonw,python -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -like "*BigFishCouncil*" } | Stop-Process -Force

# 2. 查看还有哪些 python 进程，重启 windows-mcp（按平时的方式；计划任务就重启该任务）
Get-Process | Where-Object { $_.ProcessName -match 'python' } |
  Select-Object Id, ProcessName, Path

# 3. 验证：Action 类工具能秒回
Invoke-WebRequest http://127.0.0.1:8765/mcp -Method POST -Headers @{
  Authorization = "Bearer <token>"
  "Content-Type" = "application/json"
} -Body '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"Process","arguments":{"mode":"list","limit":3}}}'
```

## 以后怎么避免

启动 BigFish Council 时**不要**让它的 stdout 继承 MCP 的管道。三种正确姿势：

```powershell
# A. 显式重定向到文件
Start-Process -FilePath "$dir\.venv\Scripts\pythonw.exe" -ArgumentList "-m","bigfish" `
  -WorkingDirectory $dir -RedirectStandardOutput "$env:TEMP\bfc.out" `
  -RedirectStandardError "$env:TEMP\bfc.err"
```

```powershell
# B. 用 start-gui.ps1（内部已做重定向）
.\start-gui.ps1
```

```powershell
# C. 直接双击 build.ps1 打出来的 BigFishCouncil.exe（窗口子系统，不继承控制台管道）
```

关键点：`-RedirectStandardOutput` 必须指向**文件**，不能是 `$null`（`$null` 也会继承句柄）。
