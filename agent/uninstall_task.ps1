# Removes the Startup-folder entry created by install_task.ps1, and stops the
# agent if it's currently running. Does not delete agent_config.json or
# agent.log - re-running install_task.ps1 later picks up the same
# configuration.
$StartupDir = [Environment]::GetFolderPath("Startup")
$LauncherPath = Join-Path $StartupDir "CSPDeviceMonitorAgent.bat"
if (Test-Path $LauncherPath) { Remove-Item $LauncherPath -Force }

Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*agent.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "Removed the Startup entry (if it existed) and stopped the agent (if it was running)."
