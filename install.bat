@echo off
rem DaiPulse - one-shot environment installer
rem Finds (or installs) a usable Python, installs dependencies.
rem Launch the widget afterwards with run.bat.
setlocal EnableDelayedExpansion

rem Optional: set your python.exe path here to skip auto-detection
set "PYTHON_PATH="
set "PYTHON="

rem 1) manual override
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

rem 4) common conda locations
if not defined PYTHON if exist "%USERPROFILE%\miniconda3\python.exe" set "PYTHON=%USERPROFILE%\miniconda3\python.exe"
if not defined PYTHON if exist "%USERPROFILE%\anaconda3\python.exe" set "PYTHON=%USERPROFILE%\anaconda3\python.exe"
if not defined PYTHON if exist "C:\ProgramData\miniconda3\python.exe" set "PYTHON=C:\ProgramData\miniconda3\python.exe"
if not defined PYTHON if exist "C:\ProgramData\anaconda3\python.exe" set "PYTHON=C:\ProgramData\anaconda3\python.exe"

rem 5) no Python at all -> winget installs the official CPython 3.12
if not defined PYTHON (
    echo [INFO] No usable Python found; trying winget (official CPython 3.12^)...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    if not errorlevel 1 (
        for %%D in ("%LOCALAPPDATA%\Programs\Python\Python312" "%ProgramFiles%\Python312") do (
            if exist "%%~D\python.exe" set "PYTHON=%%~D\python.exe"
        )
    )
)

if not defined PYTHON (
    echo [ERROR] Python could not be found or installed.
    echo         Install CPython 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [INFO] Using Python: %PYTHON%

rem install project dependencies
"%PYTHON%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [ERROR] pip install failed. Check network/proxy and re-run.
    pause
    exit /b 1
)

"%PYTHON%" -c "import textual, httpx, PIL" 2>nul
if errorlevel 1 (
    echo [ERROR] Dependency import check failed.
    pause
    exit /b 1
)

echo [OK] Environment ready. Launch the widget with run.bat.
pause
exit /b 0
