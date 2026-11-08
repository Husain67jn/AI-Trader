@echo off
rem Start the demo dashboard. Works without changing the PowerShell execution policy.
rem   scripts\start.cmd          laptop only (127.0.0.1:8000)
rem   scripts\start.cmd -Lan     phone on the same Wi-Fi (requires TRADER_OWNER_PASSWORD in .env)
setlocal
cd /d "%~dp0.."
python scripts\serve.py start %*
endlocal
