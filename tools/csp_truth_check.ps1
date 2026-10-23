# READ-ONLY check of what Windows really has on this CSP PC, to compare with what the monitor reports.
# Changes nothing. Writes csp_truth_check.txt on the Desktop. Never prints the agent's API key.
$out = Join-Path ([Environment]::GetFolderPath('Desktop')) 'csp_truth_check.txt'
& {
  "=== time: $(Get-Date -Format o)  pc: $env:COMPUTERNAME"

  "`n=== 1. monitor config (key removed)"
  $cfgPath = Join-Path $env:LOCALAPPDATA 'CSPDeviceMonitor\agent\agent_config.json'
  if (Test-Path $cfgPath) {
    $c = Get-Content $cfgPath -Raw | ConvertFrom-Json
    $c.PSObject.Properties | Where-Object { $_.Name -notmatch 'key' } | ForEach-Object { "{0} = {1}" -f $_.Name, ($_.Value | ConvertTo-Json -Compress -Depth 5) }
  } else { "agent_config.json NOT FOUND at $cfgPath" }

  "`n=== 2. monitor's own latest status"
  try { $s = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 http://127.0.0.1:5057/status).Content; $s.Substring(0, [Math]::Min(4000, $s.Length)) } catch { "local monitor page did not answer: $_" }

  "`n=== 3. installed programs (Ingenico / LLT / Telium / printer makers)"
  Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -match 'ingenico|llt|telium|loader|olivetti|epson|tvs|wep|pr2|passbook' } |
    ForEach-Object { "{0} | version {1} | {2}" -f $_.DisplayName, $_.DisplayVersion, $_.InstallLocation }

  "`n=== 4. LLT / Ingenico program files"
  foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, 'C:\')) {
    if ($base) { Get-ChildItem -Path $base -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'ingenico|llt|telium' } | ForEach-Object { $_.FullName } }
  }
  Get-ChildItem -Path ${env:ProgramFiles(x86)}, $env:ProgramFiles -Recurse -Depth 3 -Include '*llt*.exe' -ErrorAction SilentlyContinue | ForEach-Object { "{0} | {1}" -f $_.FullName, $_.VersionInfo.FileVersion }

  "`n=== 5. connected devices (Ingenico, printers, COM ports, USB printing)"
  Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
    Where-Object { $_.Class -in 'Printer','PrintQueue','Ports','USB','HIDClass' -or $_.FriendlyName -match 'ingenico|telium|printing|olivetti|epson|tvs|pr2|passbook' -or $_.Manufacturer -match 'ingenico' } |
    ForEach-Object { "{0} | class {1} | {2} | maker {3} | {4}" -f $_.FriendlyName, $_.Class, $_.Status, $_.Manufacturer, $_.InstanceId }

  "`n=== 6. Ingenico drivers Windows knows"
  Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue | Where-Object { $_.Manufacturer -match 'ingenico' -or $_.DeviceName -match 'ingenico|telium' } |
    ForEach-Object { "{0} | maker {1} | driver {2} | {3}" -f $_.DeviceName, $_.Manufacturer, $_.DriverVersion, $_.DeviceID }
  Get-WindowsDriver -Online -ErrorAction SilentlyContinue | Where-Object { $_.ProviderName -match 'ingenico' } |
    ForEach-Object { "driver store: {0} | {1} | {2}" -f $_.OriginalFileName, $_.ProviderName, $_.Version }

  "`n=== 7. Windows printers"
  Get-CimInstance Win32_Printer -ErrorAction SilentlyContinue | ForEach-Object { "{0} | port {1} | status {2} | offline {3}" -f $_.Name, $_.PortName, $_.PrinterStatus, $_.WorkOffline }
} *>&1 | Tee-Object -FilePath $out
"`nSaved to $out"
