@echo off
setlocal enabledelayedexpansion
REM CSP Device Monitor - guided setup. Double-click this file.
set "LOG=%TEMP%\csp_setup.log"
echo [%date% %time%] setup started > "%LOG%"

if not exist "%~dp0installer\wizard.py" (
    echo This setup file cannot find its other files next to it.
    echo.
    echo Please right-click the zip file you were sent, choose "Extract All...",
    echo open the new folder and double-click SETUP.bat from inside it.
    echo.
    pause
    exit /b 1
)

REM ---- 1. find a working Python that has the window toolkit (tkinter) ----
set "PYEXE="
python -c "import tkinter" >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)"') do set "PYEXE=%%P"
)
if not defined PYEXE (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%D\python.exe" (
            "%%D\python.exe" -c "import tkinter" >nul 2>nul
            if not errorlevel 1 set "PYEXE=%%D\python.exe"
        )
    )
)
if not defined PYEXE (
    echo Preparing this computer - installing Python. This happens once and may take a couple of minutes.
    echo Python is being installed >> "%LOG%"
    set "PYINSTALLER=%TEMP%\csp_python_installer.exe"
    curl -L -o "!PYINSTALLER!" "https://www.python.org/ftp/python/3.12.6/python-3.12.6-amd64.exe"
    if errorlevel 1 (
        echo.
        echo Could not download the required software. Please check the internet connection and run this file again.
        echo python download failed >> "%LOG%"
        pause
        exit /b 1
    )
    "!PYINSTALLER!" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
    del "!PYINSTALLER!" >nul 2>nul
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%D\python.exe" set "PYEXE=%%D\python.exe"
    )
    if not defined PYEXE (
        echo.
        echo The required software could not be installed automatically.
        echo Please contact your administrator and send them the file %LOG%
        echo python install failed >> "%LOG%"
        pause
        exit /b 1
    )
)
echo python: !PYEXE! >> "%LOG%"

REM ---- 2. make sure the monitor's components are present BEFORE the window opens ----
"!PYEXE!" -c "import flask, requests" >nul 2>nul
if errorlevel 1 (
    echo Installing the components the monitor needs...
    set "PIPOK="
    if exist "%~dp0wheels" (
        "!PYEXE!" -m pip install --quiet --disable-pip-version-check --no-index --find-links "%~dp0wheels" -r "%~dp0requirements.txt" >> "%LOG%" 2>&1
        if not errorlevel 1 set "PIPOK=1"
    )
    if not defined PIPOK (
        for %%N in (1 2 3) do (
            if not defined PIPOK (
                "!PYEXE!" -m pip install --quiet --disable-pip-version-check --default-timeout=120 --retries 10 -r "%~dp0requirements.txt" >> "%LOG%" 2>&1
                if not errorlevel 1 set "PIPOK=1"
            )
        )
    )
    if not defined PIPOK (
        echo.
        echo The monitor's components could not be installed. Please check the internet connection and run this file again.
        echo If it keeps happening, send the file %LOG% to your administrator.
        pause
        exit /b 1
    )
)

REM ---- 3. open the setup window (errors are shown in a message box and saved in the log) ----
set "PYW=!PYEXE:python.exe=pythonw.exe!"
if not exist "!PYW!" set "PYW=!PYEXE!"
start "" "!PYW!" "%~dp0installer\wizard.py"
echo wizard started >> "%LOG%"
exit /b 0
