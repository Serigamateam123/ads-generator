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
echo  Opening browser in 3 seconds...
echo  Press Ctrl+C to stop the server.
echo.

:: Open browser after 3 second delay (in background)
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:7373"

:: Start Flask server
"C:\Users\haliza.LUXOR\AppData\Local\Programs\Python\Python312\python.exe" ui_server.py

pause
