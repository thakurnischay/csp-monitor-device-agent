@echo off
setlocal enabledelayedexpansion
REM CSP Device Monitor - one-click setup. Double-click this file.

echo ============================================
echo   CSP Device Monitor - Setup
echo ============================================
echo.

if not exist "%~dp0requirements.txt" (
    echo This setup file cannot find its other files next to it.
    echo.
    echo This usually happens when SETUP.bat is opened directly from
    echo inside the zip file, without extracting it first.
    echo.
    echo Please do this instead:
    echo   1. Close this window.
    echo   2. Right-click the zip file you were sent and choose
    echo      "Extract All..." to create a real folder on your PC.
    echo   3. Open that newly extracted folder.
    echo   4. Double-click SETUP.bat from inside that folder,
    echo      not from inside the zip.
    echo.
    pause
    exit /b 1
)

set "PYEXE=python"
REM "where python" is not reliable here: Windows ships a fake placeholder
REM python.exe on PATH (to redirect to the Microsoft Store) even when no real
REM Python is installed, so a plain "where" check can find it and wrongly
REM think Python is present. Actually running it is the only reliable test.
python -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo Python not found on this PC - downloading and installing it automatically.
    echo This is a one-time step and may take a couple of minutes.
    echo.

    set "PYINSTALLER=%TEMP%\csp_python_installer.exe"
    curl -L -o "!PYINSTALLER!" "https://www.python.org/ftp/python/3.12.6/python-3.12.6-amd64.exe"
    if errorlevel 1 (
        echo.
        echo Could not download Python. Check your internet connection and re-run this file.
        pause
        exit /b 1
    )

    echo Installing Python, please wait...
    "!PYINSTALLER!" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
    del "!PYINSTALLER!" >nul 2>nul

    set "PYEXE="
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%D\python.exe" set "PYEXE=%%D\python.exe"
    )

    if not defined PYEXE (
        echo.
        echo Python installation could not be found afterward. Please install it manually
        echo from https://www.python.org/downloads/ and re-run this SETUP.bat.
        pause
        exit /b 1
    )
    if not exist "!PYEXE!" (
        echo.
        echo Python installation could not be verified. Please install it manually
        echo from https://www.python.org/downloads/ and re-run this SETUP.bat.
        pause
        exit /b 1
    )
    echo Python installed successfully.
    echo.
)

echo Installing required packages...
"!PYEXE!" -m pip install --quiet --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo Package install failed. Check your internet connection and try again.
    pause
    exit /b 1
)

echo Registering the background agent with Windows...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_task.ps1" -PythonPath "!PYEXE!"
if errorlevel 1 (
    echo.
    echo Setup failed while registering the agent to start automatically. See the message above.
    pause
    exit /b 1
)

echo.
echo Waiting for the agent to start...
ping -n 4 127.0.0.1 >nul

echo Opening the one-time setup page in your browser...
start "" "http://127.0.0.1:5057"

echo.
echo ============================================
echo   Setup complete.
echo   In the page that just opened, enter the
echo   Server URL, CSP Code and API key you were
echo   given, and pick your printer / micro-ATM.
echo ============================================
pause
