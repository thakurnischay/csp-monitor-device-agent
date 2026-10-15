@echo off
REM Switches this PC's installed agent to the new server address.
REM Safe: checks the new address first, keeps the CSP code/key/devices, and
REM puts the old address back if the new server does not accept the report.
cd /d "%~dp0"
set "PY=python"
python -c "import sys" >nul 2>nul
if errorlevel 1 set "PY=py -3"
%PY% change_address.py
echo.
echo ============================================
echo If it says SUCCESS you are done. If it says FAILED, send a photo of this
echo window (or the file address-change-result.txt) to your admin.
echo ============================================
pause
