@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title CSD Attendance - System Check

echo CSD Attendance - System Check
echo =============================
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\client\Check-CSD.ps1"
set "CSD_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%CSD_EXIT_CODE%"=="0" (
    echo The system check found a problem. Read the message above or contact your technician.
) else (
    echo The system check passed.
)
echo.
pause
exit /b %CSD_EXIT_CODE%
