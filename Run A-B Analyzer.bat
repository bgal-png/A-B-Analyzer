@echo off
REM Launch the A/B Sales Analyzer as a desktop app (its own window, no browser).
REM Double-click this file. First run installs dependencies; later runs are instant.
REM To put it on your Desktop: right-click this file -> Send to -> Desktop (create shortcut).
cd /d "%~dp0"

REM Ensure the desktop-window dependency is present (fast no-op once installed).
python -c "import webview" 2>nul || python -m pip install -r requirements.txt

REM Launch windowless (pythonw = no console box); fall back to python if absent.
where pythonw >nul 2>nul && ( start "" pythonw "%~dp0desktop_app.py" ) || ( start "" python "%~dp0desktop_app.py" )
