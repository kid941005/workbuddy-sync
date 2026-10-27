@echo off
REM ============================================================
REM  wb-sync 体检：引擎版本 + 集群一致性 + 空间脏数据（只读）
REM  不写任何数据；要修复请另跑 tools\repair_paths.py --apply
REM ============================================================
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
  echo [X] 没找到 Python。请先装 Python 3.9+ 并加入 PATH。
  pause
  exit /b 1
)

echo.
echo ==================== 引擎版本 ====================
"%PY%" wb_sync.py --version

echo.
echo ==================== 集群一致性 ====================
"%PY%" tools\consistency.py --db "%USERPROFILE%\.workbuddy\workbuddy.db"

echo.
echo ==================== 空间脏数据（只读预览）====================
"%PY%" tools\repair_paths.py

echo.
echo 以上均为只读检查，未修改任何文件。
pause
