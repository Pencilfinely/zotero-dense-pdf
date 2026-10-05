@echo off
cd /d "%~dp0"
python -B app.py gui
if errorlevel 1 pause
