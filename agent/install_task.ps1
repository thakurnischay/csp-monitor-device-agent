# Installs the CSP Device Monitor agent to start automatically at Windows
# logon, and starts it immediately.
#
# Uses the current user's Startup folder (built in, no extra download) rather
# than Task Scheduler: some Windows configurations block Register-ScheduledTask
# with "Access is denied" even for a normal, non-admin user account (seen on a
# real test machine), while writing one file into a folder the user already
# owns never needs elevation. Runs via pythonw.exe so no console window
# appears.
#
# -PythonPath: optional. SETUP.bat passes this in when it just installed
# Python itself moments ago - a freshly-installed program's PATH update does
# not reach an already-running cmd/PowerShell session, so relying on
# "Get-Command python" here would fail even though Python is genuinely
# present. When not given, falls back to the normal PATH lookup.
#
# Run this from an ordinary (non-admin) PowerShell prompt:
#   powershell -ExecutionPolicy Bypass -File install_task.ps1

param(
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$AgentDir = $PSScriptRoot
$AgentScript = Join-Path $AgentDir "agent.py"
$StartupDir = [Environment]::GetFolderPath("Startup")
$LauncherPath = Join-Path $StartupDir "CSPDeviceMonitorAgent.bat"

if ($PythonPath -and (Test-Path $PythonPath)) {
    $pythonPath = $PythonPath
} else {
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
}

Write-Host "Installing dependencies..."
& $pythonPath -m pip install -q -r (Join-Path $AgentDir "requirements.txt")

$pythonwPath = Join-Path (Split-Path $pythonPath) "pythonw.exe"
if (-not (Test-Path $pythonwPath)) { $pythonwPath = $pythonPath }  # fallback: console window, still works

$launcherLines = @(
    "@echo off",
    "cd /d `"$AgentDir`"",
    "start `"`" `"$pythonwPath`" `"$AgentScript`""
)
Set-Content -Path $LauncherPath -Value $launcherLines -Encoding ASCII

Write-Host "Stopping any previously-running copy of this agent..."
# Re-running SETUP.bat (e.g. after a fix, or from a different extracted copy of
# the zip) must never leave two agent.py processes running at once - each one
# would have its OWN agent_config.json, so a device picked in one copy's setup
# page would silently never reach the copy that's actually reporting. Stopping
# every "agent.py" process first, regardless of which folder it started from,
# guarantees only the one we are about to start survives.
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*agent.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

Write-Host "Starting the agent now..."
Start-Process -FilePath $pythonwPath -ArgumentList "`"$AgentScript`"" -WorkingDirectory $AgentDir

Write-Host "Installed - the agent will start automatically every time you log in to Windows."
Write-Host "Open http://127.0.0.1:5057 to finish one-time setup (server URL, CSP Code, API key, device selection)."
