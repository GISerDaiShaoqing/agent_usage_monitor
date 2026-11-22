@echo off
rem DaiPulse - WorkBuddy scan login helper
rem Runs the OAuth scan flow (open browser, poll, verify refresh) and writes
rem workbuddy_refresh_token / workbuddy_uid into config.json. Double-click
rem friendly: the console stays open at the end. Manual equivalent steps are
rem documented in README, section "WorkBuddy 凭据获取".
rem Restart the TUI / widget afterwards to pick up the new credentials.
setlocal EnableDelayedExpansion

rem Optional: set your python.exe path here to skip auto-detection, e.g.
rem set "PYTHON_PATH=C:\Users\you\miniconda3\python.exe"
set "PYTHON_PATH="

set "PYTHON="

rem 1) manual override above
if defined PYTHON_PATH if exist "%PYTHON_PATH%" set "PYTHON=%PYTHON_PATH%"

rem 2) python on PATH (skip Microsoft Store stub)
if not defined PYTHON (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        echo %%P | findstr /i "WindowsApps" >nul || set "PYTHON=%%P"
    )
)

rem 3) conda python discovered from %USERPROFILE%\.conda\environments.txt
if not defined PYTHON (
    for /f "usebackq delims=" %%P in (`type "%USERPROFILE%\.conda\environments.txt" 2^>nul`) do (
        if not defined PYTHON if exist "%%P\python.exe" set "PYTHON=%%P\python.exe"
    )
)

rem 4) common conda install locations
if not defined PYTHON if exist "%USERPROFILE%\miniconda3\python.exe" set "PYTHON=%USERPROFILE%\miniconda3\python.exe"
if not defined PYTHON if exist "%USERPROFILE%\anaconda3\python.exe" set "PYTHON=%USERPROFILE%\anaconda3\python.exe"
if not defined PYTHON if exist "C:\ProgramData\miniconda3\python.exe" set "PYTHON=C:\ProgramData\miniconda3\python.exe"
if not defined PYTHON if exist "C:\ProgramData\anaconda3\python.exe" set "PYTHON=C:\ProgramData\anaconda3\python.exe"

if not defined PYTHON (
    echo [ERROR] No usable Python found. Install miniconda/conda,
    echo         or set PYTHON_PATH in this script to your python.exe.
    pause
    exit /b 1
)

"%PYTHON%" -c "import httpx" 2>nul || (
    echo Installing dependencies...
    "%PYTHON%" -m pip install -r "%~dp0requirements.txt" -q
)

rem UTF-8 console so the Python flow's Chinese output renders correctly
chcp 65001 >nul
cd /d "%~dp0"

echo ==================================================
echo  DaiPulse - WorkBuddy scan login
echo  Existing workbuddy_refresh_token will be replaced
echo ==================================================
"%PYTHON%" -X utf8 -m app.workbuddy_client --login

echo.
pause
exit /b 0
