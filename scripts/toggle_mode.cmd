@echo off
setlocal
chcp 65001 >nul

echo ========================================================
echo   Codex Autoheal Bridge - 模式快速双向切换 (Windows)
echo ========================================================
echo.

where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_CMD=py -3"
    goto :RUN
)
where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_CMD=python"
    goto :RUN
)
where python3 >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PY_CMD=python3"
    goto :RUN
)

echo [错误] 未找到 Python，请确保已安装 Python 并加入 PATH。
pause
exit /b 1

:RUN
%PY_CMD% "%~dp0quota_failover.py" toggle --apply --restart

echo.
pause
