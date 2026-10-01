@echo off
setlocal
chcp 65001 >nul

echo ========================================================
echo   Codex Autoheal Bridge - Windows 一键自动配置
echo ========================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1"

echo.
pause
