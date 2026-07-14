@echo off
title Static Ads Generator
cd /d "C:\Users\haliza.LUXOR\.claude\skills\static-remix"

:: Kill any old instance on port 7373
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":7373 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul

:: Start the server
echo.
echo  ==========================================
echo   Static Ads Generator  ^|  localhost:7373
echo  ==========================================
echo.
echo  Browser will open automatically once the server is ready.
echo  Press Ctrl+C to stop the server.
echo.

:: NOTE: ui_server.py itself opens the browser tab once the server is ready
:: (see threading.Timer(...).start() in its __main__ block). Do NOT also call
:: `start http://localhost:7373` here — that was opening a second, redundant tab.

:: Start Flask server
"C:\Users\haliza.LUXOR\AppData\Local\Programs\Python\Python312\python.exe" ui_server.py

pause
