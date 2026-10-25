@echo off
rem Starts Asset Archaeologist and opens it in your browser. Keep this window open while you use it.
title Asset Archaeologist
cd /d "%~dp0"
if not exist ".venv\Scripts\asset-archaeologist.exe" (
  echo Asset Archaeologist is not set up yet. Double-click setup-windows.bat first.
  pause
  exit /b 1
)
echo Starting Asset Archaeologist. Your browser will open in a moment.
echo Keep this window open while you use it; close it to stop.
echo.
".venv\Scripts\asset-archaeologist.exe" serve --open
pause
