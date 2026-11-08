# Thin wrapper: scripts\serve.py does the work (see start.ps1).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
python scripts\serve.py stop
