@echo off
rem ScreenRecall launcher: starts the main process (python -m screenrecall.app) with the repo's .venv,
rem without a console window (pythonw), and returns at once. The app opens the home page on start;
rem if it is already running, this only opens the home page again.
rem Extra arguments are passed through, e.g.  ScreenRecall.cmd --no-browser
rem Python can be overridden with the SCREENRECALL_PYTHONW environment variable.
setlocal
set "ROOT=%~dp0"
set "PYW=%SCREENRECALL_PYTHONW%"
if not defined PYW set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
if not exist "%PYW%" (
  echo Cannot find "%PYW%".
  echo Create the virtual environment first:  uv venv .venv  ^&^&  uv pip install -r requirements.txt
  pause
  exit /b 1
)
start "" /D "%ROOT%." "%PYW%" -m screenrecall.app %*
endlocal
