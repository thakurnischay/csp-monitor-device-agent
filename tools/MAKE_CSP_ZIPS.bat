@echo off
REM ADMIN: builds one ready-to-install zip per CSP from csp_list.csv (see csp_list.example.csv).
cd /d "%~dp0"
set "PY=python"
python -c "import sys" >nul 2>nul
if errorlevel 1 set "PY=py -3"
%PY% make_csp_zips.py
echo.
pause
