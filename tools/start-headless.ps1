<#
  start-headless.ps1 -- 启动 BigFish Council 后台服务（Windows）

  两个坑，这版都是按着踩过的现场修的：

  1) schtasks /Create /TR 的嵌套引号
     /TR 的值本身要用引号包住，里面又要给带空格的路径加引号，于是变成
     ""C:\...\pythonw.exe" -m bigfish ..."。schtasks 解析这层嵌套直接返回
     -2147467259 (E_FAIL)，任务根本没建出来。
     主路径改成 WMI 派生：
       Invoke-CimMethod -ClassName Win32_Process -MethodName Create
     进程由 WmiPrvSE 创建，绕开 schtasks 的引号规则，也不继承当前
     PowerShell 会话的句柄。schtasks 只作备选保留（-UseScheduledTask），
     而且备选路径把命令写进一个 .cmd 包装文件，/TR 只指向包装文件，
     彻底不出现嵌套引号。

  2) 子进程不重定向会把 windows-mcp 卡死
     windows-mcp 的 PowerShell 工具要读到 powershell.exe 的 stdout 管道 EOF
     才认为命令结束。派生的 python 会继承这个管道句柄，只要它还活着管道就
     不 EOF，整个执行器队列被堵死（后续调用一律 Request timed out）。
     所以命令行里必须显式 >> log 2>&1，让子进程的 stdout/stderr 落到文件。

  用法（项目根目录下）：
    powershell -ExecutionPolicy Bypass -File tools\start-headless.ps1
    powershell -ExecutionPolicy Bypass -File tools\start-headless.ps1 -Port 3096 -DataDir D:\BigFishCouncil\data
    powershell -ExecutionPolicy Bypass -File tools\start-headless.ps1 -UseScheduledTask   # 备选路径
#>
param(
  [string]$Root      = (Split-Path -Parent $PSScriptRoot),
  [string]$DataDir   = 'D:\BigFishCouncil\data',
  [int]$Port         = 3096,
  [string]$TaskName  = 'BigFishCouncilHeadless',
  [string]$Python    = '',            # 留空则自动找 .venv 里的 pythonw / python
  [int]$HealthTries  = 10,            # 最多轮询次数
  [double]$HealthIntervalSec = 1,     # 每次间隔（秒）
  [switch]$UseScheduledTask           # 备选：走 schtasks 而不是 WMI
)

$ErrorActionPreference = 'Continue'   # 退出码由本脚本显式控制，不靠异常冒泡

# ---- 辅助函数 -----------------------------------------------------------------

function Resolve-Python {
  param([string]$Root, [string]$Python)
  if ($Python) {
    if (Test-Path $Python) { return (Resolve-Path $Python).Path }
    return $null
  }
  foreach ($n in @('pythonw.exe', 'python.exe')) {
    $p = Join-Path (Join-Path $Root '.venv\Scripts') $n
    if (Test-Path $p) { return (Resolve-Path $p).Path }
  }
  return $null
}

function New-InnerCommand {
  param([string]$Python, [string]$Root, [string]$DataDir, [int]$Port, [string]$LogPath)
  # 这段是 cmd 里的真实命令行：先 cd 到项目根（相对导入要靠它），再起服务，
  # 末尾把 stdout/stderr 全部重定向到日志——见文件头第 2 条，不重定向会卡死上级。
  return ('cd /d "' + $Root + '" && "' + $Python + '" -m bigfish --headless --data-dir "' + $DataDir + '" --port ' + $Port + ' >> "' + $LogPath + '" 2>&1')
}

function Test-Health {
  param([int]$Port, [int]$TimeoutSec = 2)
  try {
    $r = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $Port + '/health') -TimeoutSec $TimeoutSec -UseBasicParsing -ErrorAction Stop
    if ($r.StatusCode -eq 200) { return @{ Ok = $true; Body = [string]$r.Content } }
    return @{ Ok = $false; Body = ('HTTP ' + $r.StatusCode) }
  } catch {
    return @{ Ok = $false; Body = $_.Exception.Message }
  }
}

function Wait-Health {
  param([int]$Port, [int]$Tries = 10, [double]$IntervalSec = 1)
  for ($i = 1; $i -le $Tries; $i++) {
    $h = Test-Health -Port $Port
    if ($h.Ok) { return @{ Ok = $true; Tries = $i; Body = $h.Body } }
    Write-Host ('  [{0}/{1}] /health 未就绪：{2}' -f $i, $Tries, $h.Body)
    if ($i -lt $Tries) { Start-Sleep -Seconds $IntervalSec }
  }
  return @{ Ok = $false; Tries = $Tries; Body = '' }
}

function Show-LogTail {
  param([string]$LogPath, [int]$Lines = 40)
  Write-Host '---- 日志尾部 ----'
  if (-not (Test-Path $LogPath)) {
    Write-Host ('（日志文件还不存在：' + $LogPath + '）')
    return
  }
  try {
    Get-Content -Path $LogPath -Tail $Lines -ErrorAction Stop | ForEach-Object { Write-Host $_ }
  } catch {
    Write-Host ('（读取日志失败：' + $_.Exception.Message + '）')
  }
  Write-Host '---- 日志结束 ----'
}

# ---- 0. 前置检查 ---------------------------------------------------------------
$py = Resolve-Python -Root $Root -Python $Python
if (-not $py) {
  Write-Host '[失败] 找不到 .venv\Scripts\pythonw.exe 或 python.exe。'
  Write-Host '       先跑 setup.ps1 建虚拟环境，或用 -Python 指定解释器路径。'
  exit 1
}
if (-not (Test-Path $DataDir)) {
  New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
}
$dataDirFull = (Resolve-Path $DataDir).Path
$logPath = Join-Path $dataDirFull 'headless.log'

# 幂等：已经在跑就别再起一份（两份抢同一个端口会互相打死）
$pre = Test-Health -Port $Port
if ($pre.Ok) {
  Write-Host ('端口 ' + $Port + ' 上已有 BigFish Council 在跑，跳过启动。')
  Write-Host ('health: ' + $pre.Body)
  exit 0
}

$inner = New-InnerCommand -Python $py -Root $Root -DataDir $dataDirFull -Port $Port -LogPath $logPath
Write-Host ('解释器: ' + $py)
Write-Host ('数据目录: ' + $dataDirFull)
Write-Host ('日志: ' + $logPath)

# ---- 1. 起进程 -----------------------------------------------------------------
$started = $false
$mode = ''

if ($UseScheduledTask) {
  # 备选路径：命令写进包装 .cmd，/TR 只指向文件，不再有嵌套引号。
  $wrapper = Join-Path $Root ('tools\.bfc-headless-' + $TaskName + '.cmd')
  $body = "@echo off`r`n" + $inner + "`r`n"
  Set-Content -Path $wrapper -Value $body -Encoding ASCII
  Write-Host ('[备选] 包装脚本: ' + $wrapper)
  schtasks /Create /TN $TaskName /TR ('"' + $wrapper + '"') /SC ONCE /ST 00:00 /F /RL LIMITED | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Write-Host ('[失败] schtasks /Create 退出码 ' + $LASTEXITCODE)
    exit 1
  }
  schtasks /Run /TN $TaskName | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Write-Host ('[失败] schtasks /Run 退出码 ' + $LASTEXITCODE)
    exit 1
  }
  $started = $true
  $mode = ('计划任务 ' + $TaskName)
} else {
  # 主路径：WMI 派生。cmd /d /s /c "..." —— /s 让 cmd 只剥掉最外层一对引号，
  # 里面的路径引号原样保留，不必做二次转义。
  $cmdLine = 'cmd.exe /d /s /c "' + $inner + '"'
  try {
    $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdLine } -ErrorAction Stop
  } catch {
    $res = $null
    Write-Host ('[WMI] 调用失败：' + $_.Exception.Message)
  }
  if ($res -and $res.ReturnValue -eq 0) {
    Write-Host ('[WMI] 已派生 cmd.exe，pid ' + $res.ProcessId)
    $started = $true
    $mode = ('WMI 派生 (pid ' + $res.ProcessId + ')')
  } else {
    $rv = if ($res) { $res.ReturnValue } else { 'null' }
    Write-Host ('[WMI] Win32_Process.Create 返回 ' + $rv + '，转 schtasks 备选')
    $wrapper = Join-Path $Root ('tools\.bfc-headless-' + $TaskName + '.cmd')
    $body = "@echo off`r`n" + $inner + "`r`n"
    Set-Content -Path $wrapper -Value $body -Encoding ASCII
    schtasks /Create /TN $TaskName /TR ('"' + $wrapper + '"') /SC ONCE /ST 00:00 /F /RL LIMITED | Out-Null
    if ($LASTEXITCODE -ne 0) {
      Write-Host ('[失败] WMI 与 schtasks 都没起来，schtasks /Create 退出码 ' + $LASTEXITCODE)
      exit 1
    }
    schtasks /Run /TN $TaskName | Out-Null
    if ($LASTEXITCODE -ne 0) {
      Write-Host ('[失败] schtasks /Run 退出码 ' + $LASTEXITCODE)
      exit 1
    }
    $started = $true
    $mode = ('计划任务 ' + $TaskName + '（WMI 失败后回退）')
  }
}

if (-not $started) {
  Write-Host '[失败] 进程没有起来。'
  Show-LogTail -LogPath $logPath
  exit 1
}

# ---- 2. 轮询 /health（最多 $HealthTries 次，每次间隔 $HealthIntervalSec 秒）--------
Write-Host ('等待 /health（最多 ' + $HealthTries + ' 次，每 ' + $HealthIntervalSec + ' 秒一次）...')
$h = Wait-Health -Port $Port -Tries $HealthTries -IntervalSec $HealthIntervalSec

if ($h.Ok) {
  Write-Host ('[成功] ' + $mode + '，端口 ' + $Port + '，第 ' + $h.Tries + ' 次探测通过')
  Write-Host ('health: ' + $h.Body)
  Write-Host ('日志: ' + $logPath)
  exit 0
}

Write-Host ('[失败] ' + $HealthTries + ' 次轮询后 /health 仍未就绪。')
Show-LogTail -LogPath $logPath
Write-Host '排查建议：看上面日志尾部；端口被占先跑 tools\stop-headless.ps1 -WhatIfOnly 看是谁。'
exit 1
