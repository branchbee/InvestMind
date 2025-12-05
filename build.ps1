<#
.SYNOPSIS
    A-Share Hunter 打包脚本 (Windows / PowerShell)

.DESCRIPTION
    使用 PyInstaller 将 PySide6 GUI 应用打包为可分发的 exe。
    默认 onedir 模式（启动快、稳定），支持 onefile 单文件模式。

.PARAMETER Mode
    normal          默认，onedir + 无控制台
    console         onedir + 控制台（便于调试）
    onefile         单 exe + 无控制台
    onefile-console 单 exe + 控制台

.EXAMPLE
    .\build.ps1
    .\build.ps1 console
    .\build.ps1 onefile
#>
param(
    [ValidateSet("normal", "console", "onefile", "onefile-console")]
    [string]$Mode = "normal"
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  A-Share Hunter - PyInstaller Build Script (PowerShell)" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "[INFO] Build mode: $Mode" -ForegroundColor Green

$onefile = $Mode -like "onefile*"
$console = $Mode -like "*console"

# ---------- 检查 Python ----------
try {
    $pyVersion = & python --version 2>&1
    Write-Host "[INFO] Python: $pyVersion" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] Python not found in PATH. Please install Python 3.10+ first." -ForegroundColor Red
    exit 1
}

# ---------- 检查/安装 PyInstaller ----------
$pyiOk = $true
try {
    $null = & python -m PyInstaller --version 2>&1
    if ($LASTEXITCODE -ne 0) { $pyiOk = $false }
} catch { $pyiOk = $false }

if (-not $pyiOk) {
    Write-Host "[INFO] PyInstaller not found, installing..." -ForegroundColor Yellow
    & python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to install PyInstaller. Check network/pip config." -ForegroundColor Red
        exit 1
    }
}
Write-Host "[INFO] PyInstaller ready." -ForegroundColor Green

# ---------- 清理旧产物 ----------
foreach ($dir in @("build", "dist")) {
    if (Test-Path $dir) {
        Write-Host "[INFO] Removing old $dir ..." -ForegroundColor Yellow
        Remove-Item -Path $dir -Recurse -Force
    }
}

# ---------- 组装打包参数 ----------
$args = @("--noconfirm", "--clean", "--name", "A-Share-Hunter")
if ($onefile) { $args += "--onefile" } else { $args += "--onedir" }
if (-not $console) { $args += "--windowed" }

$args += @("--add-data", ".env.example;.")

# futu 含 184 个 protobuf 文件; langgraph/langchain 使用插件式动态导入
$args += @("--collect-all", "futu")
$args += @("--collect-all", "langgraph")
$args += @("--collect-all", "langchain_core")
$args += @("--collect-all", "langsmith")
$args += @("--collect-submodules", "ashare_hunter")

# 易被漏掉的隐式导入
$args += @("--hidden-import", "google.protobuf")
$args += @("--hidden-import", "packaging")
$args += @("--hidden-import", "requests")
$args += @("--hidden-import", "dotenv")

Write-Host "[INFO] Running PyInstaller..." -ForegroundColor Green
Write-Host "[CMD] python -m PyInstaller $($args -join ' ') gui.py" -ForegroundColor DarkGray
Write-Host ""

& python -m PyInstaller @args gui.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] PyInstaller build FAILED. See log above." -ForegroundColor Red
    exit 1
}

# ---------- 复制运行所需文件 ----------
$outDir = if ($onefile) { "dist" } else { "dist\A-Share-Hunter" }
if (-not (Test-Path $outDir)) {
    Write-Host "[ERROR] Output directory not found: $outDir" -ForegroundColor Red
    exit 1
}
Copy-Item -Path ".env.example" -Destination "$outDir\.env.example" -Force

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  BUILD SUCCESS" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "[INFO] Output: $(Join-Path (Get-Location) $outDir)"
Write-Host "[INFO] EXE:     $(Join-Path (Get-Location) "$outDir\A-Share-Hunter.exe")"
Write-Host ""
Write-Host "[NEXT] Before running:" -ForegroundColor Yellow
Write-Host "  1. Install and start Futu OpenD (127.0.0.1:11111 by default)"
Write-Host "  2. Copy .env.example to .env and fill in your settings"
Write-Host "  3. Run A-Share-Hunter.exe"
Write-Host ""
exit 0
