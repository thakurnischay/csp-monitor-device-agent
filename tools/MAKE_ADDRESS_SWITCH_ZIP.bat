@echo off
REM ADMIN: builds the zip that moves installed CSP agents to a NEW server address.
REM The zip contains NO credentials - each CSP keeps the code/key already on its PC.
cd /d "%~dp0"
set "PY=python"
python -c "import sys" >nul 2>nul
if errorlevel 1 set "PY=py -3"
echo.
set /p NEWURL=Type the NEW server address (example https://new-host:8443/) :
set /p OLDURL=Only switch agents currently using this OLD address (press Enter to switch all) :
if "%OLDURL%"=="" (
  %PY% make_address_switch_zip.py --server-url "%NEWURL%"
) else (
  %PY% make_address_switch_zip.py --server-url "%NEWURL%" --old-url "%OLDURL%"
)
echo.
echo The zip is in the dist folder next to this file.
pause
