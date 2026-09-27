@echo off
rem Double-click to start the hike-finder web UI (Windows).
rem
rem Finds a Python, runs the map server straight from this checkout's src\ (no
rem `pip install` needed - the UI's only dependency is `requests`), and opens it in
rem your browser. Close this window to stop the server.
rem
rem The first time, it asks for your email: the OpenStreetMap servers want a contact
rem address from every program. The answer is remembered in your own user folder
rem (%LOCALAPPDATA%\hike-finder\contact.txt), never in this folder. A HIKE_OVERPASS_UA
rem you set yourself wins and skips the question. Any arguments are forwarded
rem (e.g. --port 9000).
rem Double-clicking it again while it runs just reopens the page.
rem
rem Python is taken from, in order: .venv\ in this folder, `python`, `py -3` - the
rem first that is 3.10+ AND has `requests`. Each is probed first, because `python`
rem may be the Microsoft Store placeholder that only opens the Store.
setlocal
cd /d "%~dp0"

if not exist "src\hike_finder\launch.py" (
    echo This file has to stay in the hike-finder folder, next to "src".
    goto :fail
)

set "HF_SRC=%CD%\src"
if defined PYTHONPATH set "HF_SRC=%HF_SRC%;%PYTHONPATH%"
set "PYTHONPATH=%HF_SRC%"

set "HF_OK=import sys, requests; sys.exit(sys.version_info < (3, 10))"
set "PY="
if exist ".venv\Scripts\python.exe" call :try ".venv\Scripts\python.exe"
if not defined PY call :try python
if not defined PY call :try py -3
if defined PY goto :run

rem Nothing passed. Say which half is missing - no Python at all, too old, or no
rem `requests` - and only install anything if you say yes.
set "HF_ANY="
if exist ".venv\Scripts\python.exe" call :any ".venv\Scripts\python.exe"
if not defined HF_ANY call :any python
if not defined HF_ANY call :any py -3
if not defined HF_ANY (
    echo Python was not found. Install Python 3.10 or newer from
    echo   https://www.python.org/downloads/
    echo tick "Add python.exe to PATH" in the installer, then double-click this file again.
    goto :fail
)
%HF_ANY% -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 (
    echo Your Python is older than 3.10. Install a newer one from
    echo   https://www.python.org/downloads/
    goto :fail
)
echo hike-finder needs the Python package "requests", which is not installed.
echo It would be installed with:  %HF_ANY% -m pip install requests
choice /c YN /m "Install it now"
if errorlevel 2 goto :fail
%HF_ANY% -m pip install requests
if errorlevel 1 goto :fail
set "PY=%HF_ANY%"

:run
echo hike-finder - your browser will open shortly. Close this window to stop it.
echo.
%PY% -m hike_finder.launch %*
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
pause
exit /b 1

:try
%* -c "%HF_OK%" >nul 2>&1 && set "PY=%*"
exit /b 0

:any
%* -c "import sys" >nul 2>&1 && set "HF_ANY=%*"
exit /b 0
