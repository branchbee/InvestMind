@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

cd /d "%~dp0"

echo ============================================================
echo   A-Share Hunter - PyInstaller Build Script (Windows)
echo ============================================================
echo.

REM ---------- 参数解析 ----------
REM 用法: build.bat              默认 onedir + 无控制台
REM       build.bat console      onedir + 控制台(便于调试)
REM       build.bat onefile      单 exe + 无控制台
REM       build.bat onefile-console  单 exe + 控制台
set "BUILD_MODE=%~1"
if "%BUILD_MODE%"=="" set "BUILD_MODE=normal"

set "ONEFILE=0"
set "CONSOLE=0"
if /I "%BUILD_MODE%"=="onefile" set "ONEFILE=1"
if /I "%BUILD_MODE%"=="console" set "CONSOLE=1"
if /I "%BUILD_MODE%"=="onefile-console" (
    set "ONEFILE=1"
    set "CONSOLE=1"
)

echo [INFO] Build mode: %BUILD_MODE%
echo.

REM ---------- 检查 Python ----------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Please install Python 3.10+ first.
    exit /b 1
)

REM ---------- 检查/安装 PyInstaller ----------
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] PyInstaller not found, installing...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller. Check your network/pip config.
        exit /b 1
    )
)
echo [INFO] PyInstaller ready.

REM ---------- 清理旧产物 ----------
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
echo [INFO] Cleaned old build artifacts.

REM ---------- 组装打包参数 ----------
set "ARGS=--noconfirm --clean --name A-Share-Hunter"
if "%ONEFILE%"=="1" (
    set "ARGS=%ARGS% --onefile"
) else (
    set "ARGS=%ARGS% --onedir"
)
if "%CONSOLE%"=="0" (
    set "ARGS=%ARGS% --windowed"
)

REM 配置文件作为数据文件打包进根目录
set "ARGS=%ARGS% --add-data .env.example;."

REM 关键依赖整包收集: futu 含 184 个 pb2 文件, langgraph/langchain 使用插件式动态导入
set "ARGS=%ARGS% --collect-all futu"
set "ARGS=%ARGS% --collect-all langgraph"
set "ARGS=%ARGS% --collect-all langchain_core"
set "ARGS=%ARGS% --collect-all langsmith"
set "ARGS=%ARGS% --collect-submodules ashare_hunter"

REM 显式声明易被漏掉的隐式导入
set "ARGS=%ARGS% --hidden-import google.protobuf"
set "ARGS=%ARGS% --hidden-import packaging"
set "ARGS=%ARGS% --hidden-import requests"
set "ARGS=%ARGS% --hidden-import dotenv"

echo [INFO] Running PyInstaller...
echo [CMD] python -m PyInstaller %ARGS% gui.py
echo.

python -m PyInstaller %ARGS% gui.py
if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller build FAILED. See the log above.
    exit /b 1
)

REM ---------- 复制运行所需文件 ----------
if "%ONEFILE%"=="1" (
    set "OUTDIR=dist"
) else (
    set "OUTDIR=dist\A-Share-Hunter"
)
if not exist "%OUTDIR%" (
    echo [ERROR] Output directory not found: %OUTDIR%
    exit /b 1
)
copy /Y .env.example "%OUTDIR%\.env.example" >nul

echo.
echo ============================================================
echo   BUILD SUCCESS
echo ============================================================
echo [INFO] Output: %cd%\%OUTDIR%
echo [INFO] EXE:     %cd%\%OUTDIR%\A-Share-Hunter.exe
echo.
echo [NEXT] Before running:
echo   1. Install and start Futu OpenD (127.0.0.1:11111 by default)
echo   2. Copy .env.example to .env and fill in your settings
echo   3. Run A-Share-Hunter.exe
echo.
exit /b 0
