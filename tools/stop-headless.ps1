<#
  stop-headless.ps1 -- 停止并清理 BigFish Council 后台进程 / 计划任务

  安全要点（rc3）：不能因为「端口 3096 上有监听」就无差别杀进程。
  3096 可能被 tailscaled（Tailscale Serve）、别的 python 服务占用，
  杀错会波及无关服务。判据必须**同时**满足：
    1) 进程可执行名是 python / pythonw；
    2) 进程命令行里出现 BigFish Council 的特征串。
  特征串用的是精确模式而不是裸 "bigfish"：
    -m bigfish  /  --m bigfish  /  bigfish\\__main__  /  BigFishCouncil.exe  /  bfc-proxy.mjs
  裸 "bigfish" 子串会误伤 "my-bigfish-notes.txt" 之类的无关进程，所以不放行。
  不满足就只报告、不动手，让操作者人工确认。

  用法：
    powershell -ExecutionPolicy Bypass -File tools\stop-headless.ps1
    powershell -ExecutionPolicy Bypass -File tools\stop-headless.ps1 -Port 3096 -WhatIfOnly
#>
param(
  [string]$TaskName = 'BigFishCouncilHeadless',
  [int]$Port = 3096,
  [switch]$WhatIfOnly        # 只报告不杀，用于先看清楚谁在占端口
)

$ErrorActionPreference = "Continue"   # 本脚本是清理工具，单条失败不该中断全流程

# ---- BigFish Council 进程判据 -------------------------------------------------
# 注意：模式里不要出现裸 bigfish，见文件头说明。
$BfcPatterns = @(
  '(^|\s)-m\s+bigfish(\s|$)',              # python -m bigfish --headless ...
  'bigfish[\\/]__main__\.py',                # python path\to\bigfish\__main__.py
  'BigFishCouncil\.exe',                     # PyInstaller 打包产物
  'bfc-proxy\.mjs',                          # 跳板（同属本项目，可选清理）
  'bigfish-council[\\/]'                     # 项目目录下的脚本调用
)

function Get-CmdLine {
  param([int]$ProcId)
  $cmd = ''
  try {
    $p = Get-CimInstance Win32_Process -Filter ("ProcessId=$ProcId") -ErrorAction Stop
    if ($p) { $cmd = [string]$p.CommandLine }
  } catch { $cmd = "" }
  return $cmd
}

function Test-BfcProcess {
  param([string]$Name, [string]$CmdLine)
  # 1) 可执行名必须是 python / pythonw（或本项目的打包产物；
  #    node 只允许在命令行明确是 bfc-proxy.mjs 时放行，避免误杀其它 node 进程）
  $isPython = $Name -like 'python*'
  $isPacked = $Name -like 'BigFishCouncil*'
  $isProxyNode = ($Name -like 'node*') -and ($CmdLine -match 'bfc-proxy\.mjs')
  if (-not ($isPython -or $isPacked -or $isProxyNode)) { return $false }
  # 2) 命令行必须命中特征模式
  foreach ($pat in $BfcPatterns) {
    if ($CmdLine -match $pat) { return $true }
  }
  return $false
}

# ---- 1. 计划任务 ---------------------------------------------------------------
if (-not $WhatIfOnly) {
  schtasks /End /TN $TaskName 2>$null | Out-Null
  schtasks /Delete /TN $TaskName /F 2>$null | Out-Null
}

# ---- 2. 端口上的监听进程 -------------------------------------------------------
$killed = @()
$skipped = @()
$conns = @()
try {
  $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
} catch { $conns = @() }

foreach ($c in $conns) {
  $procId = $c.OwningProcess
  if (-not $procId -or $procId -eq 0) { continue }
  $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
  if (-not $proc) { continue }

  $cmd = Get-CmdLine -ProcId $procId
  if (Test-BfcProcess -Name $proc.ProcessName -CmdLine $cmd) {
    if ($WhatIfOnly) {
      $killed += ("[WouldStop] {0} (pid {1})" -f $proc.ProcessName, $procId)
    } else {
      Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
      if ($?) {
        $killed += ("{0} (pid {1})" -f $proc.ProcessName, $procId)
      } else {
        $skipped += ("停止失败 {0} (pid {1}, 权限不足？)" -f $proc.ProcessName, $procId)
      }
    }
  } else {
    $skipped += ("{0} (pid {1}, 命令行={2})" -f $proc.ProcessName, $procId, $cmd)
  }
}

# ---- 3. 报告 -------------------------------------------------------------------
if ($killed.Count -gt 0) {
  Write-Output ("已停止 BigFish Council 进程：" + ($killed -join '; '))
} else {
  Write-Output "没有发现属于 BigFish Council 的监听进程。"
}
if ($skipped.Count -gt 0) {
  Write-Output ("以下进程占用端口 " + $Port + " 但**不是** BigFish Council，未终止：")
  foreach ($s in $skipped) { Write-Output ("  - " + $s) }
  Write-Output "如确认需要释放端口，请人工处理后重试（脚本不会替你杀掉它们）。"
}
if ($WhatIfOnly) {
  Write-Output "[WhatIfOnly] 仅报告，未终止任何进程、未动计划任务。"
} else {
  Write-Output ("已清理计划任务 " + $TaskName)
}
exit 0
