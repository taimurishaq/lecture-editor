@echo off
rem Double-click to open the Lecture Editor window.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo The Python environment is missing. Follow "Install (Windows)" in README.md first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" app.py
