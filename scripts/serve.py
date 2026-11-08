"""Start, stop or inspect the demo dashboard.

One launcher, stdlib only, so it runs from PowerShell, cmd or Git Bash without
changing the machine's PowerShell execution policy (scripts/start.ps1 is blocked
on a default Windows 10 install, which is why this exists).

    python scripts/serve.py start [--lan] [--port 8000]
    python scripts/serve.py stop
    python scripts/serve.py status
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PID_FILE = ROOT / "data" / "trader.pid"
LOG_FILE = ROOT / "data" / "server.log"


def _has_owner_password() -> bool:
    env = ROOT / ".env"
    if not env.exists():
        return False
    return any(line.strip().startswith("TRADER_OWNER_PASSWORD=") and line.split("=", 1)[1].strip()
               for line in env.read_text(encoding="utf-8").splitlines())


def _pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="ascii").strip().splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


def _running(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True).stdout if os.name == "nt" else ""
    if os.name == "nt":
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start(lan: bool, port: int) -> int:
    if _pid() and _running(_pid()):
        print(f"Already running (PID {_pid()}). Run 'stop' first.")
        return 1
    if lan and not _has_owner_password():
        print("Refusing --lan: set TRADER_OWNER_PASSWORD in .env first "
              "(see docs/ENV_VARS.md). Never expose the dashboard without it.")
        return 1
    # LAN mode is plain HTTP unless a TLS proxy terminates it. That is allowed - the
    # phone workflow depends on it - but only deliberately, and it is reported on the
    # dashboard and in the audit log once the app starts (app/api.py transport_security).
    https = os.getenv("TRADER_HTTPS") == "1"
    if lan and not https and os.getenv("TRADER_ALLOW_INSECURE_LAN") != "1":
        print("Refusing --lan over plain HTTP. The session cookie and the owner "
              "password would cross the network unencrypted.\n"
              "  * set TRADER_HTTPS=1 if a trusted reverse proxy terminates TLS, or\n"
              "  * set TRADER_ALLOW_INSECURE_LAN=1 to accept it on a network you trust.")
        return 1
    host = "0.0.0.0" if lan else "127.0.0.1"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "ab")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    env = dict(os.environ, TRADER_LAN="1" if lan else "0")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.api:create_app", "--factory",
         "--host", host, "--port", str(port), "--log-level", "info"],
        cwd=ROOT, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        creationflags=flags)
    PID_FILE.write_text(f"{proc.pid}\n", encoding="ascii")
    print(f"Started PID {proc.pid} on {host}:{port}. Log: {LOG_FILE}")
    print("Dashboard: http://localhost:%d" % port)
    print("Note: the kill switch starts PAUSED after every restart.")
    if lan:
        print("LAN mode: allow Python through the firewall, then open "
              f"http://<this-pc-ip>:{port} on your phone.")
        if not https:
            print("WARNING: plain HTTP on the LAN (TRADER_ALLOW_INSECURE_LAN=1). "
                  "Use TLS or a VPN for anything you care about.")
    return 0


def stop() -> int:
    pid = _pid()
    if pid is None:
        print("No pid file; nothing to stop.")
        return 0
    if not _running(pid):
        print(f"PID {pid} is not running; removing the stale pid file.")
        PID_FILE.unlink(missing_ok=True)
        return 0
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
    else:
        os.kill(pid, 15)
    PID_FILE.unlink(missing_ok=True)
    print(f"Stopped PID {pid}.")
    return 0


def status() -> int:
    pid = _pid()
    if pid and _running(pid):
        print(f"running (PID {pid}), log {LOG_FILE}")
        return 0
    print("not running")
    return 1


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "status"
    port = 8000
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if action == "start":
        return start("--lan" in argv, port)
    if action == "stop":
        return stop()
    if action == "status":
        return status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
