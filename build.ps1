# BigFish Council - PyInstaller 打包脚本
# 用法： powershell -ExecutionPolicy Bypass -File build.ps1
# 产物： dist\BigFishCouncil\BigFishCouncil.exe（onedir，启动快、体积可控）
#
# rc4 修正：原生命令（python/pip/PyInstaller）失败不会抛异常，只会写
# $LASTEXITCODE。以前只有 unittest 一处查了退出码，pip 装依赖失败、PyInstaller
# 打包失败都能一路走到「完成」，属于假成功。现在每条外部调用后都跟 Assert-Ok。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Assert-Ok {
    param([Parameter(Mandatory = $true)][string]$What)
    $rc = $LASTEXITCODE
    if ($null -eq $rc) { return }
    if ($rc -ne 0) {
        $ErrorActionPreference = "Continue"    # 让 exit 1 一定跑得到，退出码可控
        Write-Error ("$What 失败（退出码 $rc）")
        exit 1
    }
}

Write-Host "== 1/4 检查虚拟环境 ==" -ForegroundColor Cyan
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        [Console]::Error.WriteLine("找不到 python，无法创建 .venv")
        exit 1
    }
    python -m venv .venv
    Assert-Ok "创建虚拟环境"
    if (-not (Test-Path $py)) {
        [Console]::Error.WriteLine("venv 声称建好了但找不到 $py")
        exit 1
    }
}
& $py -m pip install --upgrade pip | Out-Null
Assert-Ok "升级 pip"
& $py -m pip install -r requirements.txt
Assert-Ok "安装 requirements.txt"
& $py -m pip install pyinstaller
Assert-Ok "安装 pyinstaller"

Write-Host "== 2/4 跑单元测试（不通过就不打包）==" -ForegroundColor Cyan
& $py -m unittest discover -s tests
Assert-Ok "单元测试（不通过就不打包）"

Write-Host "== 3/4 PyInstaller 打包 ==" -ForegroundColor Cyan
$entry = Join-Path $root "tools\_entry.py"
@'
import sys
from bigfish.__main__ import main
sys.exit(main())
'@ | Set-Content -Encoding UTF8 $entry

# 用参数数组调用，避免行尾反引号续行（在 ps1 里极易被编辑器/粘贴破坏）。
# PyInstaller 失败时 $LASTEXITCODE 非 0，但 dist 目录可能残留上次的产物，
# 所以必须在调用后立刻断言，不能只看 Test-Path。
$piArgs = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--name", "BigFishCouncil",
    "--windowed", "--onedir",
    "--paths", $root,
    "--hidden-import", "bigfish.gui",
    "--hidden-import", "uvicorn.logging",
    "--hidden-import", "uvicorn.loops.auto",
    "--hidden-import", "uvicorn.protocols.http.auto",
    "--hidden-import", "uvicorn.protocols.websockets.auto",
    "--hidden-import", "uvicorn.lifespan.on",
    "--collect-submodules", "bigfish",
    "--exclude-module", "tkinter",
    "--exclude-module", "matplotlib",
    $entry
)
& $py @piArgs
Assert-Ok "PyInstaller 打包"

$exe = Join-Path $root "dist\BigFishCouncil\BigFishCouncil.exe"
if (-not (Test-Path $exe)) {
    [Console]::Error.WriteLine("PyInstaller 返回 0 但没生成 $exe，请查 spec/日志")
    exit 1
}

Write-Host "== 4/4 收尾 ==" -ForegroundColor Cyan
Copy-Item README.md dist\BigFishCouncil\ -Force -ErrorAction SilentlyContinue
Copy-Item tools\bfc-proxy.mjs dist\BigFishCouncil\ -Force -ErrorAction SilentlyContinue
Remove-Item $entry -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "完成： $exe" -ForegroundColor Green
Write-Host "首次运行会在 %LOCALAPPDATA%\BigFishCouncil 建数据库。" -ForegroundColor Green
exit 0
