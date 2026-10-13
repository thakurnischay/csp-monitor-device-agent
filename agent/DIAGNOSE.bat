@echo off
REM CSP Device Monitor - device diagnostic. Double-click this file.
REM Read-only: changes nothing and sends nothing. It writes agent-diagnostic.txt
REM next to this file - send that file to your admin.
cd /d "%~dp0"
echo ============================================
echo   CSP Device Monitor - Device Diagnostic
echo ============================================
echo This takes about a minute. Please wait...
echo.
python diagnose.py
if errorlevel 1 py -3 diagnose.py
echo.
echo ============================================
echo Done. Send the file  agent-diagnostic.txt  from this folder to your admin.
echo ============================================
pause
