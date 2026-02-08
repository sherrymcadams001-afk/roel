@echo off
setlocal

REM Desktop-friendly launcher for Project Zero on Windows.
REM This runs Start-ProjectZero.ps1 with ExecutionPolicy Bypass.

set SCRIPT_DIR=%~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Start-ProjectZero.ps1" %*

endlocal
