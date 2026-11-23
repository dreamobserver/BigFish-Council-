# 启动 BigFish Council GUI
# 关键：stdout/stderr 重定向到文件，避免子进程继承调用者的管道（见 docs/WINDOWS-MCP-RECOVERY.md）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $py)) { throw "找不到 $py，请先建虚拟环境并安装依赖" }

$dataDir = Join-Path $env:LOCALAPPDATA "BigFishCouncil"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$out = Join-Path $env:TEMP "bigfish-council.out.log"
$err = Join-Path $env:TEMP "bigfish-council.err.log"

Start-Process -FilePath $py `
    -ArgumentList "-m", "bigfish", "--data-dir", $dataDir `
    -WorkingDirectory $root `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err

Write-Host "BigFish Council 已启动（数据目录 $dataDir）"
Write-Host "日志： $err"
Start-Sleep -Seconds 3
try {
    $h = Invoke-WebRequest "http://127.0.0.1:3096/health" -TimeoutSec 5 -UseBasicParsing
    Write-Host "本地服务正常： $($h.Content)" -ForegroundColor Green
} catch {
    Write-Host "本地服务未响应，请查看日志： $err" -ForegroundColor Yellow
    Get-Content $err -ErrorAction SilentlyContinue | Select-Object -Last 20
}
