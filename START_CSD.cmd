@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title CSD Attendance

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\client\Start-Here.ps1" -Mode Local
set "CSD_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%CSD_EXIT_CODE%"=="0" (
    echo CSD Attendance stopped because of an error. Read the message above or contact your technician.
) else (
    echo CSD Attendance has stopped.
)
echo.
pause
exit /b %CSD_EXIT_CODE%
