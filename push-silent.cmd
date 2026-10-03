@echo off
REM Non-interactive push, for the Windows Task Scheduler.
REM Output is appended to %LOCALAPPDATA%\wb-sync\push.log
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "CFG=%LOCALAPPDATA%\wb-sync"
if not exist "%CFG%" mkdir "%CFG%" >nul 2>&1

REM rotate the log at 1 MB so it never grows forever
if exist "%CFG%\push.log" for %%A in ("%CFG%\push.log") do if %%~zA GTR 1048576 del "%CFG%\push.log" >nul 2>&1

set "PY="
for /f "delims=" %%D in ('dir /b /ad /o-n "%USERPROFILE%\.workbuddy\binaries\python\versions" 2^>nul') do (
  if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe"
  )
)
if not defined PY for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"

if not defined PY (
  echo [%DATE% %TIME%] ERROR python not found >> "%CFG%\push.log"
  exit /b 1
)

if not exist "%~dp0wb_sync.py" (
  echo [%DATE% %TIME%] ERROR wb_sync.py not found - OneDrive not ready? >> "%CFG%\push.log"
  exit /b 2
)

echo. >> "%CFG%\push.log"
echo ===== %DATE% %TIME% ===== >> "%CFG%\push.log"
"!PY!" "%~dp0wb_sync.py" push >> "%CFG%\push.log" 2>&1
set "RC=!errorlevel!"
echo [rc] push rc=!RC! >> "%CFG%\push.log"
exit /b !RC!
