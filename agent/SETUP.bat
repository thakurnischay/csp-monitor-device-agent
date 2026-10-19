@echo off
setlocal enabledelayedexpansion
REM CSP Device Monitor - guided setup. Double-click this file.

if not exist "%~dp0installer\wizard.py" (
    echo This setup file cannot find its other files next to it.
    echo.
    echo Please right-click the zip file you were sent, choose "Extract All...",
    echo open the new folder and double-click SETUP.bat from inside it.
    echo.
    pause
    exit /b 1
)

set "PYEXE=python"
REM Actually running python is the only reliable test (Windows ships a fake python.exe placeholder).
python -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo Preparing this computer - installing Python. This happens once and may take a couple of minutes.
    set "PYINSTALLER=%TEMP%\csp_python_installer.exe"
    curl -L -o "!PYINSTALLER!" "https://www.python.org/ftp/python/3.12.6/python-3.12.6-amd64.exe"
    if errorlevel 1 (
        echo.
        echo Could not download the required software. Please check the internet connection and run this file again.
        pause
        exit /b 1
    )
    "!PYINSTALLER!" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
    del "!PYINSTALLER!" >nul 2>nul
    set "PYEXE="
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%D\python.exe" set "PYEXE=%%D\python.exe"
    )
    if not defined PYEXE (
        echo.
        echo The required software could not be installed automatically. Please contact your administrator.
        pause
        exit /b 1
    )
)

set "PYW=!PYEXE!"
if /i "!PYEXE!"=="python" (set "PYW=pythonw") else (
    set "PYW=!PYEXE:python.exe=pythonw.exe!"
)
start "" "!PYW!" "%~dp0installer\wizard.py"
exit /b 0
