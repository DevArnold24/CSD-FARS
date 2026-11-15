@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title CSD Attendance - First Run

echo CSD Attendance - First Run
echo ==========================
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\client\First-Run.ps1"
set "CSD_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%CSD_EXIT_CODE%"=="0" (
    echo First Run did not complete. Read the message above or contact your technician.
) else (
    echo First Run completed. Use START_CSD.cmd whenever you want to run CSD Attendance.
)
echo.
pause
exit /b %CSD_EXIT_CODE%
