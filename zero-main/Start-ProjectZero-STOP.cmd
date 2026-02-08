@echo off
setlocal

REM Best-effort stop script (kills node controller and streamlit/orchestrator python).
REM Safe to run multiple times.

powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process node,python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*zero-main*' -or $_.Path -like '*Downloads*zero-main*' } | Stop-Process -Force -ErrorAction SilentlyContinue"
echo Stopped matching Project Zero processes.

endlocal
