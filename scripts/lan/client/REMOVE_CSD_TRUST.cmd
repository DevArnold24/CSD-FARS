@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title CSD Attendance - Remove LAN Trust

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Remove-Trust.ps1" -Graphical
set "CSD_EXIT_CODE=%ERRORLEVEL%"

echo.
if "%CSD_EXIT_CODE%"=="2" (
    echo Cancelled. Connection trust was not removed.
) else if not "%CSD_EXIT_CODE%"=="0" (
    echo Trust removal did not complete. Read the message above or contact your technician.
) else (
    echo This Windows user no longer trusts the CSD Attendance LAN certificate.
)
echo You can close this window.
echo.
pause
exit /b %CSD_EXIT_CODE%
