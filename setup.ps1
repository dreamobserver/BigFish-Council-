# BigFish Council - 一键安装并自检（Windows）
# 用法： powershell -ExecutionPolicy Bypass -File setup.ps1
#
# rc4 修正：PowerShell 调原生命令（python/pip/...）失败**不会**抛异常，
# 只是把退出码放进 $LASTEXITCODE。以前这些调用没查退出码，pip 装失败、
# 单测失败、自检失败都能走到最后一行打印「安装完成」，属于假成功。
# 现在每条外部调用后面都跟 Assert-Ok，非 0 立刻 Write-Error 并 exit 1。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Assert-Ok {
    param([Parameter(Mandatory = $true)][string]$What)
    $rc = $LASTEXITCODE
    if ($null -eq $rc) { return }          # 该命令没设退出码（纯 cmdlet），无从判断
    if ($rc -ne 0) {
        # 函数内改 $ErrorActionPreference 不会污染脚本全局；Write-Error 在 Stop 下
        # 会直接抛终止错误，那样 exit 1 就跑不到，退出码不可控。
        $ErrorActionPreference = "Continue"
        Write-Error ("$What 失败（退出码 $rc）")
        exit 1
    }
}

Write-Host "== 1/5 Python ==" -ForegroundColor Cyan
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine("找不到 python，请先装 Python 3.10+ 并加入 PATH")
    exit 1
}
$ver = (python -c "import sys; print('%d.%d' % sys.version_info[:2])") 2>$null
Assert-Ok "检测 Python 版本"
if (-not $ver) {
    [Console]::Error.WriteLine("python 存在但拿不到版本号，请确认它是可用的 Python 3.10+")
    exit 1
}
Write-Host "Python $ver"

Write-Host "== 2/5 虚拟环境 ==" -ForegroundColor Cyan
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    python -m venv .venv
    Assert-Ok "创建虚拟环境"
    if (-not (Test-Path $py)) {
        [Console]::Error.WriteLine("venv 声称建好了但找不到 $py")
        exit 1
    }
}
& $py -m pip install --upgrade pip | Out-Null
Assert-Ok "升级 pip"

Write-Host "== 3/5 依赖 ==" -ForegroundColor Cyan
& $py -m pip install -r requirements.txt
Assert-Ok "安装 requirements.txt"

Write-Host "== 4/5 单元测试 ==" -ForegroundColor Cyan
& $py -m unittest discover -s tests
Assert-Ok "单元测试"

Write-Host "== 5/5 自检 ==" -ForegroundColor Cyan
& $py tools\selfcheck.py
Assert-Ok "自检 tools/selfcheck.py"

Write-Host ""
Write-Host "安装完成。启动： .\start-gui.ps1" -ForegroundColor Green
exit 0
