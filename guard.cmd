@echo off
REM wb-sync guard: pull only when WorkBuddy is NOT running.
REM Triggered by the scheduled task every 15 minutes; no-op while WorkBuddy is up.
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "CFG=%LOCALAPPDATA%\wb-sync"
if not exist "%CFG%" mkdir "%CFG%" >nul 2>&1

if exist "%CFG%\guard-run.log" for %%A in ("%CFG%\guard-run.log") do if %%~zA GTR 1048576 del "%CFG%\guard-run.log" >nul 2>&1

set "PY="
for /f "delims=" %%D in ('dir /b /ad /o-n "%USERPROFILE%\.workbuddy\binaries\python\versions" 2^>nul') do (
  if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe"
  )
)
if not defined PY for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"
if not defined PY (
  echo [%DATE% %TIME%] python not found >> "%CFG%\guard-run.log"
  exit /b 1
)

"%PY%" wb_sync.py guard >> "%CFG%\guard-run.log" 2>&1
exit /b %ERRORLEVEL%
