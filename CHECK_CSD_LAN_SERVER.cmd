@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title CSD Attendance - LAN Server Check

echo CSD Attendance - LAN Server Check
echo =================================
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\lan\Check-CSD-LAN.ps1"
set "CSD_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%CSD_EXIT_CODE%"=="0" (
    echo The LAN server check found a problem. Read the message above or contact your technician.
) else (
    echo The LAN server check passed.
)
echo.
pause
exit /b %CSD_EXIT_CODE%
