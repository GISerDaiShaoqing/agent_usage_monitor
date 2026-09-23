@echo off
rem OpenCode Go quota tracker - unified launcher
rem Starts the floating desktop widget (no console window left behind).
rem TUI dashboard can be opened from the widget right-click menu,
rem or manually: python -X utf8 -m app.main
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

"%PYTHON%" -c "import textual, httpx, PIL" 2>nul || (
    echo Installing dependencies...
    "%PYTHON%" -m pip install -r "%~dp0requirements.txt" -q
)

rem pythonw = no console window stays open after launch
set "PYTHONW=%PYTHON:python.exe=pythonw.exe%"
if not exist "%PYTHONW%" set "PYTHONW=%PYTHON%"

cd /d "%~dp0"
start "" "%PYTHONW%" -X utf8 -m app.widget
exit /b 0
