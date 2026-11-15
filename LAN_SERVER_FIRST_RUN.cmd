@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title CSD Attendance - LAN Server First Run

echo CSD Attendance - LAN Server First Run
echo =====================================
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\lan\First-Run-LAN.ps1"
set "CSD_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%CSD_EXIT_CODE%"=="0" (
    echo LAN server setup did not complete. Read the message above or contact your technician.
) else (
    echo LAN server setup completed. Use START_CSD_LAN_SERVER.cmd to serve authorized laptops.
)
echo.
pause
exit /b %CSD_EXIT_CODE%
