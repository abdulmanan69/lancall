<#
Makes LAN Call start with Windows and opens its ports in the firewall.

Run once from an Administrator PowerShell, after start_server.bat has been run
at least once (so the .venv folder exists):

    powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1
    powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1 -Port 443 -HttpPort 80

Remove again with:

    powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1 -Uninstall
#>
param(
    [int]$Port = 8443,
    [int]$HttpPort = 8080,
    [string]$Pin = "",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "LAN Call"
$RuleName = "LAN Call"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this from an Administrator PowerShell." }

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    Write-Host "Removed the startup task and firewall rule. Your data folder was left alone."
    exit 0
}

if (-not (Test-Path $Python)) { throw "Run start_server.bat once first; $Python does not exist yet." }

$arguments = "-m lancall --port $Port --http-port $HttpPort"
if ($Pin) { $arguments += " --pin $Pin" }

# Runs as SYSTEM at boot, before anyone logs in, and restarts itself if it ever stops.
$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null

# Private and Domain networks only: never expose the phone system on public Wi-Fi.
Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
$ports = @($Port) + $(if ($HttpPort) { @($HttpPort) } else { @() })
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP `
    -LocalPort $ports -Profile Private, Domain | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed. LAN Call now starts with Windows on port $Port."
Write-Host "Logs: $(Join-Path $Root 'data\lancall.log')"
