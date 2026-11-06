@echo off
setlocal enabledelayedexpansion

echo ============================================
echo  Think Streaming LITE Proxy
echo  Raw SSE pass-through (no parsing!)
echo ============================================
echo.
echo Starting proxy server on http://localhost:8081
echo Log file: think_streaming_lite.log
echo Crash log: crash_log.txt
echo.
echo Press Ctrl+C to stop
echo ============================================
echo.

set RESTART_COUNT=0

:restart_loop
set /a RESTART_COUNT+=1
echo [%date% %time%] Restart attempt #!RESTART_COUNT! >> crash_log.txt
python think_streaming_lite.py
set EXIT_CODE=%ERRORLEVEL%
echo [%date% %time%] Python exited with code %EXIT_CODE% >> crash_log.txt
echo.
echo ============================================
echo [!] Proxy exited with code %EXIT_CODE%
echo     Restarting in 3 seconds... (attempt #!RESTART_COUNT!)
echo     Press Ctrl+C to stop
echo ============================================
echo.

timeout /t 3 /nobreak >nul
if errorlevel 1 exit /b %EXIT_CODE%

goto restart_loop