@echo off
rem Asset Archaeologist setup for Windows: double-click this file.
title Asset Archaeologist setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-windows.ps1" %*
echo.
pause
