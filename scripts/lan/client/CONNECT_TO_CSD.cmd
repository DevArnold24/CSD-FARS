@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title CSD Attendance - Connect to LAN Server

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Trust-And-Open.ps1" -Graphical
set "CSD_EXIT_CODE=%ERRORLEVEL%"

echo.
if "%CSD_EXIT_CODE%"=="2" (
    echo Closed. Open this connection kit again when you are ready.
) else if not "%CSD_EXIT_CODE%"=="0" (
    echo Connection setup did not complete. Read the message above or contact your technician.
) else (
    echo This Windows user is connected to the CSD Attendance LAN server.
)
echo You can close this window.
echo.
pause
exit /b %CSD_EXIT_CODE%
