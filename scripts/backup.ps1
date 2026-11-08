# Consistent SQLite backup (safe while the server is running, WAL included).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Force -Path "data\backups" | Out-Null
python -c "from app import config, db; import datetime,pathlib; config.load_dotenv(); p=pathlib.Path('data/backups')/f'trader-{datetime.datetime.now():%Y%m%d-%H%M%S}.db'; print('backup:', db.backup(p))"