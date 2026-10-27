# Android 测试工具层 —— 一键安装/更新（在工具层根目录跑）
#
# 用法：
#   pwsh -File scripts/install.ps1              # 建 venv + 装依赖
#   pwsh -File scripts/install.ps1 -Recreate    # 删掉重建
#   pwsh -File scripts/install.ps1 -Check       # 只检查，不装
#
# 为什么单独一个脚本（不用 setup.ps1）：
#   setup.ps1 还要检查 adb/设备/u2 init，步骤多。
#   而这个只做一件事：让 `python tools/xxx.py` 能跑起来。
#
# 依赖清单见 requirements.txt。约 200MB，第一次几分钟。

[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$Recreate,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $Root '.venv'
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
$Req = Join-Path $Root 'requirements.txt'

function Step($t) { Write-Host "`n> $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "  OK  $t" -ForegroundColor Green }
function Warn($t) { Write-Host "  !!  $t" -ForegroundColor Yellow }
function Die($t) { Write-Host "  XX  $t" -ForegroundColor Red; exit 1 }

# ── 1. 检查现有环境 ────────────────────────────────────────────────
Step '检查 python 环境'
# 注意：python 一行代码请用单引号字符串（PowerShell 5.1 对双引号里的
# 百分号、方括号、圆括号会误解析，实测报 Missing closing brace）。
$pyVerOne = 'import sys;print(sys.version.split()[0])'
if (Test-Path $VenvPy) {
    $v = (& $VenvPy -c $pyVerOne 2>&1) -join ''
    Ok "已存在 .venv（Python $v）"
    $hasVenv = $true
} else {
    Warn '还没有 .venv'
    $hasVenv = $false
}

if ($Check) {
    if ($hasVenv) {
        $miss = ''
        foreach ($m in @('uiautomator2', 'adbutils', 'yaml', 'PIL')) {
            & $VenvPy -c "import $m" 2>$null
            if ($LASTEXITCODE -ne 0) { $miss = "$miss$m," }
        }
        if ($miss -eq '') { Ok '依赖齐全'; exit 0 }
        Warn "缺依赖: $miss"
        Write-Host "`n  装：pwsh -File scripts/install.ps1" -ForegroundColor Yellow
        exit 1
    }
    Warn '没有环境。装：pwsh -File scripts/install.ps1'
    exit 1
}

# ── 2. 找 base python ─────────────────────────────────────────────
if (-not $hasVenv -or $Recreate) {
    Step '找 Python（>= 3.10）'
    $cands = @()
    if ($Python) { $cands += @{ Exe = $Python; Args = @() } }
    if ($env:ANDROID_TEST_PYTHON) {
        $cands += @{ Exe = $env:ANDROID_TEST_PYTHON; Args = @() }
    }
    foreach ($v in @('3.12', '3.13', '3.11', '3.10')) {
        $cands += @{ Exe = 'py'; Args = @("-$v") }
    }
    $cands += @{ Exe = 'python'; Args = @() }
    $cands += @{ Exe = 'python3'; Args = @() }

    $base = $null
    $baseVer = $null
    foreach ($c in $cands) {
        try {
            # ⚠️ 用单引号字符串（PS 里不插值），且不含 `%` / `[` / `(`
            #    实测踩过：
            #      "%d.%d.%d" % sys.version_info[:3]  -> PS 把 % 当运算符
            #      [x for x in ...]                   -> PS 把 [ 当数组索引
            #    改成 sys.version.split() 就直接是字符串了，最省事。
            $pyVer = 'import sys;print(sys.version.split()[0])'
            $out = & $c.Exe @($c.Args) -c $pyVer 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        $parts = ($out -split '\.')
        if ($parts.Count -lt 2) { continue }
        $maj = [int]$parts[0]; $min = [int]$parts[1]
        if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 10)) {
            $base = $c; $baseVer = $out
            break
        }
    }

    if (-not $base) {
        Die '找不到 >= 3.10 的 Python。装一个：https://www.python.org/downloads/（勾选 Add to PATH）'
    }
    Ok "用 $($base.Exe) $($base.Args -join ' ')  (Python $baseVer)"

    # ── 3. 建 venv ────────────────────────────────────────────────
    if ($Recreate -and (Test-Path $VenvDir)) {
        Step '删掉旧 .venv'
        Remove-Item $VenvDir -Recurse -Force
        Ok '已删'
    }
    Step '创建 .venv'
    & $base.Exe @($base.Args) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPy)) {
        Die 'venv 创建失败'
    }
    Ok "已建：$VenvPy"
}

# ── 4. 装依赖 ─────────────────────────────────────────────────────
Step '安装依赖（约 200MB，第一次几分钟）'
Write-Host "  $Req" -ForegroundColor DarkGray
& $VenvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $VenvPy -m pip install -r $Req --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Die 'pip install 失败（看上面的报错）' }

# ── 5. 自检 ───────────────────────────────────────────────────────
Step '自检'
$miss = ''
foreach ($m in @('uiautomator2', 'adbutils', 'yaml', 'PIL')) {
    & $VenvPy -c "import $m" 2>$null
    if ($LASTEXITCODE -ne 0) { $miss = "$miss$m," }
}
if ($miss -ne '') { Die "依赖装不全，缺: $miss" }
Ok 'uiautomator2 / adbutils / yaml / PIL 都能 import'

# ── 6. 下一步 ─────────────────────────────────────────────────────
# ⚠️ 不用 here-string 里嵌双引号（PS 5.1 会解析成字符串结束）。
#    实测踩过：The string is missing the terminator: ".
#    改成 Write-Host 分多次打印，路径单独一行，最稳。
Write-Host ''
Write-Host '环境就绪。之后所有命令用这个解释器：' -ForegroundColor Green
Write-Host ''
Write-Host "  $VenvPy" -ForegroundColor Cyan
Write-Host ''
Write-Host '  例：' -ForegroundColor Green
Write-Host "    & '$VenvPy' '$Root\tools\env.py'" -ForegroundColor DarkGray
Write-Host "    & '$VenvPy' '$Root\tools\session.py' start --title ... --input ... --package ..." -ForegroundColor DarkGray
Write-Host ''
