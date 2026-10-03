@echo off
setlocal enabledelayedexpansion
title WorkBuddy - Sync then Launch
cd /d "%~dp0"

set "CFG=%LOCALAPPDATA%\wb-sync"
if not exist "%CFG%" mkdir "%CFG%" >nul 2>&1
set "PF86=%ProgramFiles(x86)%"

REM ---------------------------------------------------------------
REM  1) Wait until OneDrive has delivered this folder (max 90s)
REM ---------------------------------------------------------------
set /a WAIT=0
:wait_od
if exist "%~dp0wb_sync.py" goto od_ok
set /a WAIT+=2
if !WAIT! GEQ 90 goto od_timeout
timeout /t 2 /nobreak >nul
goto wait_od

:od_timeout
echo [WARN] OneDrive folder not ready after 90s - cloud sync skipped.
set "SYNC_OK=0"
goto find_py

:od_ok
set "SYNC_OK=1"

REM ---------------------------------------------------------------
REM  2) Locate the Python runtime that ships with WorkBuddy
REM ---------------------------------------------------------------
:find_py
set "PY="
for /f "delims=" %%D in ('dir /b /ad /o-n "%USERPROFILE%\.workbuddy\binaries\python\versions" 2^>nul') do (
  if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe"
  )
)
if not defined PY for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"

REM ---------------------------------------------------------------
REM  3) PULL  (WorkBuddy is not running yet - database is free)
REM ---------------------------------------------------------------
if "!SYNC_OK!"=="1" if defined PY (
  echo.
  echo [1/2] Pulling cloud changes BEFORE WorkBuddy starts ...
  echo.
  "!PY!" "%~dp0wb_sync.py" pull
  set "RC=!errorlevel!"
  echo [%DATE% %TIME%] pull rc=!RC! >> "%CFG%\launch.log"
  echo.
  echo [1/2] Pull finished ^(exit !RC!^).
) else (
  echo [1/2] Cloud sync skipped.
)

REM ---------------------------------------------------------------
REM  4) Start WorkBuddy  (always, even if the sync above failed)
REM ---------------------------------------------------------------
set "WBEXE="
if exist "%CFG%\workbuddy-exe.txt" set /p WBEXE=<"%CFG%\workbuddy-exe.txt"
if not defined WBEXE if exist "%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe" set "WBEXE=%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe"
if not defined WBEXE if exist "%ProgramFiles%\WorkBuddy\WorkBuddy.exe" set "WBEXE=%ProgramFiles%\WorkBuddy\WorkBuddy.exe"
if not defined WBEXE if exist "%PF86%\WorkBuddy\WorkBuddy.exe" set "WBEXE=%PF86%\WorkBuddy\WorkBuddy.exe"

if not defined WBEXE (
  echo.
  echo [ERROR] WorkBuddy.exe not found. Please start it manually.
  echo         Expected at: %LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe
  timeout /t 8 >nul
  exit /b 1
)

echo [2/2] Starting WorkBuddy ...
start "" "!WBEXE!"
exit /b 0
