@echo off
setlocal EnableDelayedExpansion
rem ============================================================
rem  Apply quota pill to OpenCode Desktop (patch app.asar).
rem  REQUIREMENTS: OpenCode Desktop (the GUI) must be CLOSED.
rem  Backup:  resources\app.asar.bak-quota (created on first run).
rem  Revert:  revert_quota_widget.bat
rem  Log:     apply_quota_widget.log (created next to this bat).
rem  Every step is printed to the window AND appended to the log.
rem ============================================================
set "DIR=%~dp0"
set "LOG=%DIR%apply_quota_widget.log"
set "TMP=%DIR%_repack_out.tmp"
if exist "%TMP%" del /f /q "%TMP%" >nul 2>nul

call :log "================ RUN START %date% %time% ================"
call :log "bat dir  (DIR) = %DIR%"
call :log "log file (LOG) = %LOG%"

rem --- STEP 1: locate app.asar (install folder name varies) ---
set "RES="
for %%R in (
  "%LOCALAPPDATA%\Programs\@opencode-aidesktop\resources"
  "%LOCALAPPDATA%\Programs\OpenCode\resources"
) do (
  if not defined RES if exist "%%~R\app.asar" set "RES=%%~R"
)
call :log "STEP 1: resolved RES = !RES!"
if not defined RES (
  call :err "STEP 1 FAILED: app.asar not found in any known location:"
  call :err "        %LOCALAPPDATA%\Programs\@opencode-aidesktop\resources"
  call :err "        %LOCALAPPDATA%\Programs\OpenCode\resources"
  pause
  exit /b 1
)

rem --- STEP 2: node.exe ---
if not defined NODE (
  where node >nul 2>nul && set "NODE=node"
)
if not defined NODE if exist "%ProgramFiles%\nodejs\node.exe" set "NODE=%ProgramFiles%\nodejs\node.exe"
if not defined NODE if exist "%USERPROFILE%\OneDrive - MYIZHORA\Automation\NodeJs\node-v22.17.1-win-x64\node.exe" set "NODE=%USERPROFILE%\OneDrive - MYIZHORA\Automation\NodeJs\node-v22.17.1-win-x64\node.exe"
call :log "STEP 2: NODE = !NODE!"
if not defined NODE (
  call :err "STEP 2 FAILED: node.exe not found. Install Node.js (nodejs.org, 'Add to PATH') or set NODE near the top."
  pause
  exit /b 1
)
"!NODE!" --version > "%TMP%" 2>nul
if exist "%TMP%" (
  for /f "usebackq delims=" %%V in ("%TMP%") do call :log "STEP 2: node version = %%V"
)

rem --- STEP 3: is OpenCode Desktop running? ---
call :log "STEP 3: checking whether OpenCode.exe is running..."
tasklist /fi "imagename eq OpenCode.exe" 2>nul | find /i "OpenCode.exe" >nul
set "RUNNING=!errorlevel!"
call :log "STEP 3: OpenCode.exe running? errorlevel=!RUNNING! (0 = YES, a process named OpenCode.exe is running)"
tasklist /fi "imagename eq OpenCode.exe" > "%TMP%" 2>nul
if exist "%TMP%" type "%TMP%" >> "%LOG%"
if "!RUNNING!"=="0" (
  call :err "STEP 3 FAILED: OpenCode.exe IS running. Fully CLOSE OpenCode Desktop (the GUI window, not just minimize; also check the system tray), then run this again."
  pause
  exit /b 1
)

rem --- STEP 4: app.asar sanity + backup ---
call :log "STEP 4: target = %RES%\app.asar"
if not exist "%RES%\app.asar" (
  call :err "STEP 4 FAILED: %RES%\app.asar not found (RES resolved but the file is gone)."
  pause
  exit /b 1
)
if not exist "%RES%\app.asar.bak-quota" (
  call :log "STEP 4: first run -> creating backup app.asar.bak-quota"
  copy /y "%RES%\app.asar" "%RES%\app.asar.bak-quota" >nul
  set "CP=!errorlevel!"
  call :log "STEP 4: backup copy errorlevel = !CP!"
  if not "!CP!"=="0" (
    call :err "STEP 4 FAILED: could not create backup (disk full? permissions? OneDrive sync lock?)."
    pause
    exit /b 1
  )
) else (
  call :log "STEP 4: backup app.asar.bak-quota already exists -> keeping it (NOT overwriting)"
)

rem --- STEP 5: repack (capture node output to console + log) ---
call :log "STEP 5: repacking app.asar (~140 MB)... cmd: !NODE! %DIR%repack_asar.js"
if exist "%TMP%" del /f /q "%TMP%" >nul 2>nul
"!NODE!" "%DIR%repack_asar.js" "%RES%\app.asar" "%RES%\app.asar.new" > "%TMP%" 2>&1
set "RC=!errorlevel!"
if exist "%TMP%" type "%TMP%"
if exist "%TMP%" type "%TMP%" >> "%LOG%"
call :log "STEP 5: node exit code = !RC!"
if not "!RC!"=="0" (
  if exist "%RES%\app.asar.new" del /f /q "%RES%\app.asar.new" >nul 2>nul
  call :err "STEP 5 FAILED: repack failed (node exit !RC!). Original app.asar is UNTOUCHED."
  call :err "        The node output above (and in the log) has the reason - typically one of:"
  call :err "        'index.html not found in asar' / 'no closing body tag' / 'HASH MISMATCH' / 'UNEXPECTED DIFF' / 'bad asar'."
  pause
  exit /b 1
)

rem --- STEP 6: replace ---
call :log "STEP 6: replacing app.asar with the patched app.asar.new..."
move /y "%RES%\app.asar.new" "%RES%\app.asar" >nul
set "MV=!errorlevel!"
call :log "STEP 6: move errorlevel = !MV!"
if not "!MV!"=="0" (
  call :err "STEP 6 FAILED: could not replace app.asar (exit !MV!)."
  call :err "        Usually: app.asar is locked - is OpenCode Desktop REALLY closed? Or antivirus is holding the file. Wait a few seconds, close everything, retry."
  pause
  exit /b 1
)

call :log "STEP 7: OK. Patched app.asar is in place. Start OpenCode Desktop - the quota"
call :log "        pill appears in the composer footer (left of the send button) after the first poll."
call :log "================ RUN END (success) ================"
if exist "%TMP%" del /f /q "%TMP%" >nul 2>nul
echo.
echo [OK] Done. Full log: %LOG%
pause
exit /b 0

rem ================= subroutines =================
:log
echo [i] %~1
echo [%date% %time%] [i] %~1 >> "%LOG%"
goto :eof

:err
echo [ERROR] %~1
echo [%date% %time%] [ERROR] %~1 >> "%LOG%"
goto :eof
