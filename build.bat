@echo off
setlocal
cd /d "%~dp0"

where pwsh >nul 2>nul
if %errorlevel%==0 (
    set "PS=pwsh"
) else (
    set "PS=powershell"
)

echo 使用 %PS% 执行 build.ps1
%PS% -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
set "RC=%errorlevel%"

echo.
if not "%RC%"=="0" (
    echo [失败] 退出码 %RC%，请查看上面的日志
) else (
    echo [完成] 产物在 dist\ 目录下
)
pause
exit /b %RC%
