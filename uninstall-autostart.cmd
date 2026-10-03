@echo off
title WorkBuddy auto-sync - UNINSTALL
echo.
echo Removing WorkBuddy auto-sync ...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall-autostart.ps1"
echo.
pause
