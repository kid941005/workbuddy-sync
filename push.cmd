@echo off
setlocal enabledelayedexpansion
title WorkBuddy Sync - PUSH (this PC to cloud)
cd /d "%~dp0"

set "PY="
for /f "delims=" %%D in ('dir /b /ad /o-n "%USERPROFILE%\.workbuddy\binaries\python\versions" 2^>nul') do (
  if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe"
  )
)
if not defined PY for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"

if not defined PY (
  echo.
  echo [ERROR] Python not found.
  echo         Open WorkBuddy once ^(it ships its own Python^), then retry.
  echo.
  pause
  exit /b 1
)

echo.
echo Using Python: !PY!
echo.
"!PY!" "%~dp0wb_sync.py" push %*
echo.
pause
