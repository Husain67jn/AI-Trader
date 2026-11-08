@echo off
rem Stop the server started by start.cmd / start.ps1.
setlocal
cd /d "%~dp0.."
python scripts\serve.py stop
endlocal
