@echo off
rem Consistent SQLite backup (safe while the server is running, WAL included).
setlocal
cd /d "%~dp0.."
python -c "from app import config, db; import datetime, pathlib; config.load_dotenv(); p = pathlib.Path('data/backups') / f'trader-{datetime.datetime.now():%%Y%%m%%d-%%H%%M%%S}.db'; print('backup:', db.backup(p))"
endlocal
