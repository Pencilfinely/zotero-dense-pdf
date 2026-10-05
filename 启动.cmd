@echo off
setlocal
cd /d "%~dp0"
py -3 -B -c "import sys, tkinter; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto launch_py

for /f "delims=" %%P in ('where.exe python.exe 2^>nul ^| findstr.exe /i /v /c:"WindowsApps"') do (
    "%%P" -B -c "import sys, tkinter; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 (
        "%%P" -B app.py %*
        if errorlevel 1 pause
        exit /b
    )
)

echo A working Python 3.10+ with tkinter was not found.
echo Install Python with tkinter, or add your Python/Anaconda folder to PATH.
pause
exit /b 1

:launch_py
py -3 -B app.py %*
if errorlevel 1 pause
