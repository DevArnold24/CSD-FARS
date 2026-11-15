@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title CSD Attendance - Start Here

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\client\Start-Here.ps1" -Graphical
set "CSD_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%CSD_EXIT_CODE%"=="0" (
    echo The selected action did not complete. Read the message above or contact your technician.
) else (
    echo Launcher finished. Read the result above for the system's status.
)
echo.
pause
exit /b %CSD_EXIT_CODE%
