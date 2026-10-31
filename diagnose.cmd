@echo off
REM wb-sync diagnose: engine version + cluster consistency + dirty workspaces
REM All checks are READ-ONLY. To repair, run tools\repair_paths.py --apply
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="
for /f "delims=" %%D in ('dir /b /ad /o-n "%USERPROFILE%\.workbuddy\binaries\python\versions" 2^>nul') do (
  if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe"
  )
)
if not defined PY for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"
if not defined PY (
  echo [X] Python not found. Install Python 3.9+ and add it to PATH.
  pause
  exit /b 1
)

echo.
"%PY%" wb_sync.py --version

echo.
"%PY%" tools\consistency.py --db "%USERPROFILE%\.workbuddy\workbuddy.db"

echo.
"%PY%" tools\repair_paths.py

echo.
echo [done] read-only checks finished, nothing was modified.
pause
