@echo off
setlocal EnableExtensions
rem ===========================================================================
rem  GeoAI Lightning Safety MVP - double-click to run
rem  1) check Python  2) create .venv  3) install packages  4) create .env
rem  5) start the server and open the browser
rem ===========================================================================
cd /d "%~dp0"
title GeoAI Lightning Safety MVP
set PYTHONIOENCODING=utf-8

echo.
echo  GeoAI Lightning Safety MVP - Thai League 1
echo  ------------------------------------------

rem ---- 1) find Python 3.10+ ---------------------------------------------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY goto :no_python

rem ---- 2) virtual environment -------------------------------------------------
if exist ".venv\Scripts\python.exe" goto :have_venv
echo  [1/4] Creating virtual environment .venv ...
%PY% -m venv .venv
if errorlevel 1 goto :failed
:have_venv
set "VENV_PY=.venv\Scripts\python.exe"

rem ---- 3) packages --------------------------------------------------------------
echo  [2/4] Installing packages - first run needs internet, about 1-2 minutes ...
"%VENV_PY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :failed

rem ---- 4) config ------------------------------------------------------------------
if exist ".env" goto :have_env
echo  [3/4] Creating .env from .env.example - no API key needed
copy /y ".env.example" ".env" >nul
:have_env

rem ---- 5) pick a free port and start ------------------------------------------
set "PORT=8000"
netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul && set "PORT=8765"
set "URL=http://127.0.0.1:%PORT%"

echo  [4/4] Starting server at %URL%
echo.
echo  - Web page opens automatically in a few seconds.
echo  - Live mode: radar needs 1-2 minutes to load; stadiums show NO DATA until then.
echo  - Replay mode: click "Replay" to see the real storm of 16 Sep 2026.
echo  - Close this window or press Ctrl+C to stop.
echo.
if not defined NO_BROWSER start "" cmd /c "timeout /t 6 >nul & start "" %URL%"
"%VENV_PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
goto :end

:no_python
echo.
echo  [ERROR] Python 3.10 or newer was not found.
echo  Install it from https://www.python.org/downloads/
echo  and tick "Add python.exe to PATH" during setup, then double-click start.bat again.
echo.
pause
exit /b 1

:failed
echo.
echo  [ERROR] Setup failed - see the message above. Check the internet connection and try again.
echo.
pause
exit /b 1

:end
endlocal
