@echo off
setlocal enabledelayedexpansion
title WorkBuddy 同步助手
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
  echo         Open WorkBuddy once, then retry.
  echo.
  pause
  exit /b 1
)

echo Starting WorkBuddy 同步助手 ...
echo If the browser does not open, visit  http://127.0.0.1:8765
echo.
"!PY!" "%~dp0wb-ui.py"
echo.
echo Panel stopped.
pause
