# 一键启动（Windows，PowerShell 7 或 5.1）：后台 + 网页界面，并打开浏览器。
#   .\start.ps1                         # 默认数据目录（config 里的 data_dir，默认 data\）
#   .\start.ps1 -DataDir data-dataset   # 指定数据目录
#   .\start.ps1 -NoDaemon               # 只开网页（只查不录，例如查看数据集库）
# 后台在单独的窗口里运行，关掉那个窗口或按 Ctrl+C 即停止录制进程；本窗口按 Ctrl+C 停止网页服务。
param(
    [string]$DataDir = "",
    [int]$Port = 8600,
    [switch]$NoDaemon,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
    throw "没有找到 .venv。先运行：py -3.12 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}

# 前端没构建过、或 web\src 比 web\dist 新时重新构建（需要 Node.js 20+）
$dist = Join-Path $root 'web\dist\index.html'
$src = Get-ChildItem -LiteralPath (Join-Path $root 'web\src') -Recurse -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not (Test-Path -LiteralPath $dist) -or ($src -and $src.LastWriteTime -gt (Get-Item -LiteralPath $dist).LastWriteTime)) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "需要构建网页前端，但没有找到 npm（请安装 Node.js 20+）" }
    Write-Host "构建网页前端…"
    $web = Join-Path $root 'web'
    if (-not (Test-Path -LiteralPath (Join-Path $web 'node_modules'))) {
        & npm ci --prefix $web
        if ($LASTEXITCODE) { throw "npm ci 失败" }
    }
    & npm run build --prefix $web
    if ($LASTEXITCODE) { throw "前端构建失败" }
}

$common = @()
if ($DataDir) { $common = @('--data-dir', $DataDir) }

if (-not $NoDaemon) {
    $daemonArgs = @('-m', 'screenrecall.daemon') + $common
    Start-Process -FilePath $py -ArgumentList $daemonArgs -WorkingDirectory $root -WindowStyle Normal | Out-Null
    Write-Host "后台已在新窗口启动（录制默认关闭，在网页「录制」页开始）"
}

if (-not $NoBrowser) {
    # 服务起来后再打开浏览器
    Start-Job -ScriptBlock {
        param($url)
        for ($i = 0; $i -lt 60; $i++) {
            try { Invoke-WebRequest -Uri "$url/" -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process $url; return } catch { Start-Sleep -Milliseconds 500 }
        }
    } -ArgumentList "http://127.0.0.1:$Port" | Out-Null
}

Write-Host "网页界面：http://127.0.0.1:$Port （只绑定本机）"
$webArgs = @('-m', 'screenrecall.webapi', '--port', "$Port") + $common
Push-Location -LiteralPath $root   # python -m 需要从仓库根目录找到 screenrecall 包
try { & $py @webArgs } finally { Pop-Location }
exit $LASTEXITCODE
