@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title CSD Attendance - Disable LAN Access

echo This removes only CSD Attendance's exact Windows Firewall allow rule.
echo It does not delete the database, private keys, configuration, or recovery state.
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\lan\Disable-CSD-LAN.ps1"
set "CSD_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%CSD_EXIT_CODE%"=="0" (
    echo LAN access was not disabled. Read the message above or contact your technician.
) else (
    echo CSD Attendance LAN access is disabled.
)
echo.
pause
exit /b %CSD_EXIT_CODE%
