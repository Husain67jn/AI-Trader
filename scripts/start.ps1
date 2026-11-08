# Thin wrapper: scripts\serve.py does the work, so this behaves identically from
# PowerShell, cmd and Git Bash.
# Running a .ps1 requires an execution policy that a default Windows 10 install does
# not have; use scripts\start.cmd if 'running scripts is disabled on this system'.
#   scripts\start.ps1          laptop only (127.0.0.1:8000)
#   scripts\start.ps1 -Lan     phone on the same Wi-Fi (requires TRADER_OWNER_PASSWORD)
param([switch]$Lan, [int]$Port = 8000)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$args = @("scripts\serve.py", "start", "--port", "$Port")
if ($Lan) { $args += "--lan" }
python @args
