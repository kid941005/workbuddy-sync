@echo off
title WorkBuddy auto-sync - INSTALL
echo.
echo Installing WorkBuddy auto-sync ...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-autostart.ps1"
echo.
pause
