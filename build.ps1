<#
.SYNOPSIS
    InvestMind（A-Share Hunter）一键打包脚本，基于 PyInstaller。

.DESCRIPTION
    流程：
      1. 定位 Python 解释器（优先复用它，否则在项目内新建 .venv）
      2. 安装 requirements.txt + pyinstaller
      3. 清理旧的 build / dist
      4. PyInstaller 打包
      5. 把 .env 复制到产物目录，建好 output\ 目录

.PARAMETER Entry
    入口文件，默认 gui.py（PySide6 界面版）。

.PARAMETER Name
    产物名，默认 InvestMind，生成 dist\InvestMind\InvestMind.exe。

.PARAMETER OneFile
    打包成单个 exe。体积大、启动慢，PySide6 项目建议不加。

.PARAMETER Console
    保留控制台窗口。打包 main.py 这类命令行入口时加上。

.PARAMETER SkipDeps
    跳过 pip 安装（依赖已经装好时用，省时间）。

.PARAMETER Python
    手动指定解释器路径，指定后不再创建 .venv。

.EXAMPLE
    pwsh -File build.ps1

.EXAMPLE
    pwsh -File build.ps1 -Entry main.py -Name ShareHunterCLI -Console

.EXAMPLE
    pwsh -File build.ps1 -SkipDeps -Python "C:\Python312\python.exe"
#>
[CmdletBinding()]
param(
    [string]$Entry = "gui.py",
    [string]$Name = "InvestMind",
    [switch]$OneFile,
    [switch]$Console,
    [switch]$SkipDeps,
    [string]$Python
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

function Write-Step {
    param([string]$Text)
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Text)
    Write-Host "    $Text" -ForegroundColor Green
}

# ---------- 1. 定位 Python ----------
function Test-Python {
    param([string]$Exe)
    if (-not $Exe) { return $false }
    if (-not (Test-Path -LiteralPath $Exe)) { return $false }
    try {
        & $Exe -c "import sys" 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Resolve-BasePython {
    # 显式指定优先
    if ($Python) {
        if (-not (Test-Python $Python)) { throw "指定的 Python 不可用：$Python" }
        return (Resolve-Path -LiteralPath $Python).ProviderPath
    }

    $candidates = New-Object System.Collections.Generic.List[string]

    # py 启动器：py -0p 会列出所有已装版本
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $list = & $py.Source -0p 2>$null
        foreach ($line in $list) {
            if ($line -match '([A-Za-z]:\\[^\s]+python\.exe)') { $candidates.Add($Matches[1]) }
        }
    }

    # PATH 上的 python
    $pathPython = Get-Command python -ErrorAction SilentlyContinue
    if ($pathPython) { $candidates.Add($pathPython.Source) }

    # 常见安装位置
    $common = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "$env:ProgramFiles\Python312\python.exe"
    )
    foreach ($p in $common) { $candidates.Add($p) }

    foreach ($c in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $c)) { continue }
        if ($c -match '\\\.venv\\' -or $c -match 'sandbox_runtime') { continue }
        if (Test-Python $c) {
            $ver = (& $c -c "import sys;print(str(sys.version_info[0]) + '.' + str(sys.version_info[1]))" 2>$null)
            Write-Ok "使用 Python $ver ($c)"
            return (Resolve-Path -LiteralPath $c).ProviderPath
        }
    }

    throw "没有找到可用的 Python。请安装 Python 3.10+，或用 -Python <路径> 指定解释器。"
}

# ---------- 2. 准备虚拟环境 ----------
Write-Step "准备 Python 环境"

$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

if ($Python) {
    # 指定解释器就直接用，不再建 venv
    $WorkPython = (Resolve-Path -LiteralPath $Python).ProviderPath
    Write-Ok "直接使用指定解释器：$WorkPython"
}
elseif (Test-Path -LiteralPath $VenvPython) {
    $WorkPython = $VenvPython
    Write-Ok "复用已有虚拟环境：$VenvDir"
}
else {
    $BasePython = Resolve-BasePython
    Write-Ok "创建虚拟环境：$VenvDir"
    & $BasePython -m venv $VenvDir
    if (-not (Test-Path -LiteralPath $VenvPython)) { throw "虚拟环境创建失败：$VenvPython" }
    $WorkPython = $VenvPython
}

$pyVersion = (& $WorkPython -c "import sys;print(sys.version.split()[0])")
Write-Ok "Python 版本：$pyVersion"

# ---------- 3. 安装依赖 ----------
if ($SkipDeps) {
    Write-Step "跳过依赖安装（-SkipDeps）"
} else {
    Write-Step "安装依赖"
    & $WorkPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip 升级失败" }

    $req = Join-Path $ProjectRoot "requirements.txt"
    if (Test-Path -LiteralPath $req) {
        Write-Ok "pip install -r requirements.txt"
        & $WorkPython -m pip install -r $req
        if ($LASTEXITCODE -ne 0) { throw "项目依赖安装失败，请看上面的 pip 输出" }
    } else {
        Write-Host "    未找到 requirements.txt，跳过" -ForegroundColor Yellow
    }

        Write-Ok "pip install pyinstaller"
    & $WorkPython -m pip install --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 安装失败" }
}

Write-Step "检查 PyInstaller"
& $WorkPython -m PyInstaller --version
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 不可用。请去掉 -SkipDeps 重跑，或手动 pip install pyinstaller" }

# ---------- 4. 清理 ----------
Write-Step "清理旧产物"
foreach ($dir in @("build", "dist")) {
    $full = Join-Path $ProjectRoot $dir
    if (Test-Path -LiteralPath $full) {
        Remove-Item -LiteralPath $full -Recurse -Force
        Write-Ok "已删除 $dir\"
    }
}
$oldSpec = Join-Path $ProjectRoot "$Name.spec"
if (Test-Path -LiteralPath $oldSpec) { Remove-Item -LiteralPath $oldSpec -Force }

# ---------- 5. 打包 ----------
Write-Step "开始打包 $Entry -> $Name.exe"

$entryPath = Join-Path $ProjectRoot $Entry
if (-not (Test-Path -LiteralPath $entryPath)) { throw "找不到入口文件：$entryPath" }

# 用 $buildArgs，别用自动变量 $args
$buildArgs = New-Object System.Collections.Generic.List[string]
$buildArgs.Add("--noconfirm")
$buildArgs.Add("--clean")
$buildArgs.Add("--name");  $buildArgs.Add($Name)
$buildArgs.Add("--distpath"); $buildArgs.Add("dist")
$buildArgs.Add("--workpath"); $buildArgs.Add("build")
$buildArgs.Add("--specpath"); $buildArgs.Add(".")
if ($OneFile) { $buildArgs.Add("--onefile") } else { $buildArgs.Add("--onedir") }
if (-not $Console) { $buildArgs.Add("--windowed") }

# 配置文件：config.py 用 load_dotenv()，把 .env 打进包里才能取到配置
$envFile = Join-Path $ProjectRoot ".env"
if (Test-Path -LiteralPath $envFile) {
    $buildArgs.Add("--add-data"); $buildArgs.Add(".env;.")
} else {
    Write-Host "    警告：没有 .env，打包后程序会用默认配置" -ForegroundColor Yellow
}

# futu-api 带 .pyd 原生库和 protobuf 数据文件，必须整包收集
$buildArgs.Add("--collect-all"); $buildArgs.Add("futu")
# LangGraph / LangChain 子模块是动态导入的，不收集会在 build_workflow() 时报导入错误
$buildArgs.Add("--collect-all"); $buildArgs.Add("langgraph")
$buildArgs.Add("--collect-all"); $buildArgs.Add("langchain_core")

# 运行时通过 importlib.metadata 读版本号的包，需要连带元数据
foreach ($pkg in @("langchain-core", "langgraph", "pydantic", "futu-api")) {
    $buildArgs.Add("--copy-metadata"); $buildArgs.Add($pkg)
}

# PySide6 的 hook 偶尔漏 Qt 模块
foreach ($mod in @("PySide6.QtSvg", "PySide6.QtNetwork", "PySide6.QtXml")) {
    $buildArgs.Add("--hidden-import"); $buildArgs.Add($mod)
}

# 瘦身：确定用不到的模块
foreach ($mod in @("tkinter", "PyQt5", "PyQt6", "PySide2", "IPython", "pytest", "matplotlib")) {
    $buildArgs.Add("--exclude-module"); $buildArgs.Add($mod)
}

$buildArgs.Add($entryPath)

Write-Host "    PyInstaller $($buildArgs -join ' ')" -ForegroundColor DarkGray

& $WorkPython -m PyInstaller @buildArgs
if ($LASTEXITCODE -ne 0) { throw "打包失败，请检查上面的 PyInstaller 输出" }

# ---------- 6. 收尾 ----------
Write-Step "整理产物"

if ($OneFile) {
    $exePath = Join-Path $ProjectRoot "dist\$Name.exe"
    $outDir  = Join-Path $ProjectRoot "dist"
} else {
    $outDir  = Join-Path $ProjectRoot "dist\$Name"
    $exePath = Join-Path $outDir "$Name.exe"
}

if (-not (Test-Path -LiteralPath $exePath)) { throw "没有找到产物：$exePath" }

# 配置文件放一份到 exe 旁边，方便用户直接改（需要代码配合读取，见 README 打包说明）
if (Test-Path -LiteralPath $envFile) {
    Copy-Item -LiteralPath $envFile -Destination (Join-Path $outDir ".env") -Force
}
$envExample = Join-Path $ProjectRoot ".env.example"
if (Test-Path -LiteralPath $envExample) {
    Copy-Item -LiteralPath $envExample -Destination (Join-Path $outDir ".env.example") -Force
}

# 运行时报告和缓存写在这里（代码里用的是相对 cwd 的 output\）
$distOutput = Join-Path $outDir "output"
if (-not (Test-Path -LiteralPath $distOutput)) { New-Item -ItemType Directory -Path $distOutput -Force | Out-Null }

# 已有缓存直接带上，省得重新刷板块
$srcCache = Join-Path $ProjectRoot "output\futu_cache.sqlite3"
if (Test-Path -LiteralPath $srcCache) {
    Copy-Item -LiteralPath $srcCache -Destination (Join-Path $distOutput "futu_cache.sqlite3") -Force
    Write-Ok "已复用板块缓存 futu_cache.sqlite3"
}

$sizeMB = [math]::Round(((Get-ChildItem -LiteralPath $outDir -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB), 1)

Write-Host ""
Write-Host "打包完成" -ForegroundColor Green
Write-Host "  产物目录 : $outDir"
Write-Host "  可执行   : $exePath"
Write-Host "  总体积   : $sizeMB MB"
Write-Host ""
Write-Host "提示：程序里的 output\ 按当前工作目录解析，从别处启动 exe 会把报告写到那个目录。" -ForegroundColor Yellow
Write-Host "     建议快捷方式把「起始位置」设为 $outDir" -ForegroundColor Yellow
