@echo off
title Zero Stack Launcher
cd /d C:\Users\Ghost\Downloads\zero-main\zero-main\lotl

:: Windows - disable iMessage, enable WhatsApp only
set ENABLE_IMESSAGE=false
set ENABLE_WHATSAPP=true

echo ========================================
echo   Starting Zero Stack (WhatsApp Mode)
echo ========================================
echo.

:: Start LotL Controller in new window
echo [1/3] Starting LotL Controller...
start "LotL Controller" cmd /k "cd /d C:\Users\Ghost\Downloads\zero-main\zero-main\lotl && node lotl-controller-v3.js"
timeout /t 3 /nobreak >nul

:: Start Orchestrator in new window
echo [2/3] Starting Orchestrator...
start "Orchestrator" cmd /k "cd /d C:\Users\Ghost\Downloads\zero-main\zero-main\imessage_orchestrator && set ENABLE_IMESSAGE=false && set ENABLE_WHATSAPP=true && C:\Users\Ghost\Downloads\zero-main\.venv\Scripts\python.exe main.py"
timeout /t 2 /nobreak >nul

:: Start Streamlit UI in new window
echo [3/3] Starting Streamlit UI...
start "Streamlit UI" cmd /k "cd /d C:\Users\Ghost\Downloads\zero-main\zero-main\imessage_orchestrator && C:\Users\Ghost\Downloads\zero-main\.venv\Scripts\streamlit.exe run ui.py --server.port 8501"
timeout /t 3 /nobreak >nul

:: Open browser
echo.
echo Opening UI in browser...
start http://127.0.0.1:8501

echo.
echo ========================================
echo   Stack Started!
echo ========================================
echo.
echo   LotL Controller: http://127.0.0.1:3000
echo   Streamlit UI:    http://127.0.0.1:8501
echo.
echo Close this window when done.
pause
