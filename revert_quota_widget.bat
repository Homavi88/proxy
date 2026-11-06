@echo off
setlocal EnableDelayedExpansion
rem ============================================================
rem  Revert OpenCode Desktop to the pre-widget app.asar.
rem  REQUIREMENTS: OpenCode Desktop must be CLOSED.
rem  Restores resources\app.asar from app.asar.bak-quota (kept).
rem  Log: revert_quota_widget.log (next to this bat).
rem ============================================================
set "DIR=%~dp0"
set "LOG=%DIR%revert_quota_widget.log"
set "RES="
for %%R in (
  "%LOCALAPPDATA%\Programs\@opencode-aidesktop\resources"
  "%LOCALAPPDATA%\Programs\OpenCode\resources"
) do (
  if not defined RES if exist "%%~R\app.asar.bak-quota" set "RES=%%~R"
)
call :log "revert: backup source RES = !RES!"
if not defined RES (
  call :err "No patched install found (no app.asar.bak-quota under @opencode-aidesktop or OpenCode). Nothing to revert."
  pause
  exit /b 1
)
tasklist /fi "imagename eq OpenCode.exe" 2>nul | find /i "OpenCode.exe" >nul
set "RUN=!errorlevel!"
if "!RUN!"=="0" (
  call :err "OpenCode Desktop is running. Fully close it (GUI + tray), then run this again."
  pause
  exit /b 1
)
copy /y "%RES%\app.asar.bak-quota" "%RES%\app.asar" >nul
set "CP=!errorlevel!"
call :log "revert: copy backup -> app.asar  errorlevel=!CP!"
if not "!CP!"=="0" (
  call :err "Could not restore backup (exit !CP!). Is OpenCode really closed? Is the file locked?"
  pause
  exit /b 1
)
call :log "revert: OK. Restored original app.asar. Backup kept for re-apply."
pause
exit /b 0

:log
echo [i] %~1
echo [%date% %time%] [i] %~1 >> "%LOG%"
goto :eof

:err
echo [ERROR] %~1
echo [%date% %time%] [ERROR] %~1 >> "%LOG%"
goto :eof