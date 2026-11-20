# 把 BigFish Council 注册成"登录时自启"的计划任务
#
# 为什么用计划任务而不是 Start-Process：
#   从 windows-mcp 的工具调用里 Start-Process 拉起的常驻进程会继承 MCP 服务的
#   stdout 管道，导致那次工具调用永远等不到 EOF 而卡死队列。
#   计划任务由 Task Scheduler 直接创建进程，完全不经过 MCP 的句柄，没有这个问题。
#
# 用法： powershell -ExecutionPolicy Bypass -File install-task.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $py)) { throw "找不到 $py，请先跑 setup.ps1" }

$taskName = "BigFishCouncil"
$dataDir = Join-Path $env:LOCALAPPDATA "BigFishCouncil"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$action = New-ScheduledTaskAction -Execute $py `
    -Argument ("-m bigfish --data-dir "\"" + $dataDir + "\""") `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "BigFish Council 本地控制中心（大肥鱼议事厅）" -Force | Out-Null

Write-Host "已注册计划任务： $taskName（登录时自动启动）" -ForegroundColor Green
Write-Host ""
Write-Host "立即启动： Start-ScheduledTask -TaskName $taskName"
Write-Host "查看状态： Get-ScheduledTask -TaskName $taskName | Select-Object State"
Write-Host "卸载：     Unregister-ScheduledTask -TaskName $taskName -Confirm:$false"
