@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title CSD Attendance - LAN Server

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\client\Start-Here.ps1" -Mode Lan -Graphical
set "CSD_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%CSD_EXIT_CODE%"=="0" (
    echo The LAN server stopped because of an error. Read the message above or contact your technician.
) else (
    echo The CSD Attendance LAN server has stopped.
)
echo.
pause
exit /b %CSD_EXIT_CODE%
