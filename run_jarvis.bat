@echo off
rem --- J.A.R.V.I.S launcher ---
rem Double-click: starts Jarvis already listening. If Jarvis is already open,
rem the running window is brought to the front and woken instead.
set "JARVIS=C:\Vikash\Jarvis"
cd /d "%JARVIS%"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Jarvis is not set up yet: run  python setup.py  in %JARVIS%
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" main.py --wake