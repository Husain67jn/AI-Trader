"""One-command application verification against the real terminal.

Starts the actual ASGI app on a loopback port (the same factory entry point the launcher
uses), against the real MetaTrader 5 demo session, with a temporary database and the mock
LLM provider, then drives it over HTTP as the owner. Restarts it once against the same
database to verify recovery.

    python scripts/verify_app.py [--port 8131]

It never submits an order: the only kill-switch action it attempts is one that must be
refused, and the temp database means the project's own state is not touched.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "verify-owner-password-1"
RESULTS: list[tuple[bool, str, str]] = []
BLOCKING: list[str] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    if not ok:
        BLOCKING.append(f"{name}: {detail}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def free(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def wait_for(client, port: int, deadline: float) -> bool:
    while time.time() < deadline:
        try:
            if client.get("/healthz").status_code == 200:
                return True
        except Exception:  # noqa: BLE001 - not up yet
            pass
        time.sleep(0.5)
    return False


def start_server(port: int, data_dir: Path, log_path: Path) -> subprocess.Popen:
    env = {**os.environ,
           "TRADER_DATA_DIR": str(data_dir),
           "TRADER_DB": str(data_dir / "verify.db"),
           "TRADER_BROKER": "auto",                 # the real adapter, real terminal
           "TRADER_LLM_PROVIDER": "mock",           # no paid call, ever
           "TRADER_SCHEDULER": "1",                 # the scheduler must also be safe
           "TRADER_OWNER_PASSWORD": PASSWORD}
    env.pop("TRADER_LAN", None)
    log = open(log_path, "ab")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.api:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=log, stderr=log)
    return process


def stop_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()


def session(client) -> str:
    response = client.post("/api/auth/login", json={"password": PASSWORD})
    return response.json()["csrf"]


def _read_setting(db_path: Path, key: str) -> dict:
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        con.close()


def _credential_keys(payload, path: str = "") -> list[str]:
    """Field names that would hold a credential if anything filled them in."""
    pattern = re.compile(r"password|passwd|secret|api_key|apikey|token|salt|hash", re.I)
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            name = f"{path}.{key}" if path else str(key)
            if pattern.search(str(key)):
                found.append(name)
            found.extend(_credential_keys(value, name))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            found.extend(_credential_keys(item, f"{path}[{index}]"))
    return found


def main(argv: list[str]) -> int:
    port = 8131
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    import httpx

    if not free(port):
        print(f"port {port} is in use; stop that process first")
        return 2
    data_dir = Path(tempfile.mkdtemp(prefix="ati-verify-"))
    project_db = ROOT / "data" / "trader.db"
    before = project_db.stat().st_mtime_ns if project_db.exists() else None
    base = f"http://127.0.0.1:{port}"
    print(f"APPLICATION VERIFICATION — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    print(f"isolated database: {data_dir / 'verify.db'}\n")

    server = start_server(port, data_dir, data_dir / "server.log")
    try:
        with httpx.Client(base_url=base, timeout=60) as client:
            print("1. Start-up")
            if not wait_for(client, port, time.time() + 120):
                # Say why rather than dying on the next request: a contended MT5
                # handshake is the usual reason, and the log is the evidence.
                print("    the server did not answer within 120s. Log tail:")
                print((data_dir / "server.log").read_text(encoding="utf-8",
                                                          errors="replace")[-2000:])
                check(False, "the application starts and answers /healthz",
                      "nothing listening on the port within 120s")
                stop_server(server)
                return 1
            check(True, "the application starts and answers /healthz")
            health = client.get("/healthz").json()
            check(health["demo_only"] is True and health["db"] is True,
                  "it reports DEMO_ONLY and a healthy database", json.dumps(health))

            print("\n2. Authentication")
            check(client.get("/api/overview").status_code == 401,
                  "an unauthenticated read is refused")
            check(client.post("/api/auth/login",
                              json={"password": "wrong"}).status_code == 401,
                  "a wrong password is refused")
            csrf = session(client)
            check(client.post("/api/kill-switch",
                              json={"action": "pause"}).status_code == 403,
                  "a state change without the CSRF token is refused")

            print("\n3. Broker, account and market data")
            body = client.get("/api/overview").json()
            status = body["broker_status"]
            check(status["real_mt5"] is True and status["simulated"] is False,
                  "a REAL MT5 session is in use", status["label"])
            check(body["account"] and body["account"]["trade_mode"] == "DEMO",
                  "the account is DEMO",
                  f"balance {body['account']['balance']} {body['account']['currency']}")
            mapping = body["symbol_resolution"]
            check(mapping.get("EURUSD") == "EURUSDm"
                  and mapping.get("GBPUSD") == "GBPUSDm"
                  and mapping.get("XAUUSD") == "XAUUSDm",
                  "every configured symbol resolved to the broker's contract",
                  json.dumps(mapping))
            check(bool(body["quotes"]), "market data is being processed",
                  f"{len(body['quotes'])} instruments quoted")
            check(body["data_health"] in ("HEALTHY", "CLOSED", "STALE", "DEGRADED"),
                  "the health gate reported a definite verdict",
                  f"{body['data_health']}: "
                  f"{body['data_health_detail'].get('reason', 'session open, quotes fresh')}")
            check(body["kill_switch"] == "PAUSED" and body["trade_armed"] is False,
                  "the desk starts PAUSED and unarmed")

            print("\n4. Research pipeline (mock provider, no tokens)")
            research = client.post("/api/research/run", json={},
                                  headers={"X-CSRF-Token": csrf})
            check(research.status_code == 200, "the research endpoint responds",
                  research.text[:120])
            if research.status_code == 200:
                payload = research.json()
                check(payload["reports"] == 9,
                      "nine department reports were produced and stored",
                      str(payload["reports"]))
                check(payload["proposal"]["decision"] in
                      ("TRADE", "PROPOSE", "NO_TRADE", "INSUFFICIENT_DATA"),
                      "the CIO returned a validated decision",
                      payload["proposal"]["decision"])
            reported = client.get("/api/overview").json()
            check(any(r.get("department") for r in reported["reports"]),
                  "the reports are visible through the API")

            print("\n5. Risk engine and the arming gate")
            arm = client.post("/api/kill-switch",
                              json={"action": "arm", "confirm": "ARM DEMO"},
                              headers={"X-CSRF-Token": csrf})
            check(arm.status_code in (403, 409),
                  "arming is refused while the data is not HEALTHY",
                  f"{arm.status_code}: {arm.json().get('detail')}")
            check(client.get("/api/overview").json()["kill_switch"] == "PAUSED",
                  "the switch is still PAUSED after the attempt")
            bad = client.post("/api/settings/risk",
                              json={"limits": {"risk_per_trade_pct": "Infinity"},
                                    "confirm": "I UNDERSTAND"},
                              headers={"X-CSRF-Token": csrf})
            check(bad.status_code == 400, "an invalid risk limit is refused", bad.text[:80])

            print("\n6. Alerts, ledger and reconciliation")
            check(bool(reported["audit"]), "alerts and audit events are being written",
                  f"{len(reported['audit'])} recent rows")
            check(reported["unresolved_orders"] == []
                  and reported["unverified_orders"] == [],
                  "no order is unresolved or unverified")
            check(reported["positions"] == [], "no position is open")
            reconcile = client.post("/api/reconcile", json={},
                                    headers={"X-CSRF-Token": csrf}).json()
            check(reconcile.get("ok") is True, "reconciliation reports a clean ledger",
                  json.dumps(reconcile))

            print("\n7. Dashboard and disclosure")
            page = client.get("/")
            check(page.status_code == 200 and "Data health" in page.text
                  and "<script>" in page.text,
                  "the dashboard is served with its script")
            payload = client.get("/api/overview").json()
            text = json.dumps(payload)
            check(PASSWORD not in text, "the owner password is not in any payload")
            stored = _read_setting(data_dir / "verify.db", "owner_password")
            check(stored["hash"] not in text and stored["salt"] not in text,
                  "the stored password hash and salt are not in any payload")
            # Keys, not substrings: an audit row may legitimately mention the word
            # (action "owner_password_created"), a *field* named like a credential is
            # what would leak one.
            leaks = _credential_keys(payload)
            check(not leaks, "no field is named like a credential", ", ".join(leaks))

            print("\n8. Restart and recovery")
            orders_before = len(client.get("/api/overview").json()["proposals"])
        stop_server(server)
        server = start_server(port, data_dir, data_dir / "server.log")
        with httpx.Client(base_url=base, timeout=60) as client:
            check(wait_for(client, port, time.time() + 90),
                  "the application restarts against the same database")
            session(client)
            after = client.get("/api/overview").json()
            check(after["kill_switch"] == "PAUSED",
                  "a restart leaves the kill switch PAUSED")
            check(len(after["proposals"]) >= orders_before,
                  "the ledger survived the restart",
                  f"{len(after['proposals'])} proposals")
            check(after["unresolved_orders"] == [],
                  "the restart's reconciliation left nothing unresolved")
    finally:
        stop_server(server)

    after_mtime = project_db.stat().st_mtime_ns if project_db.exists() else None
    check(before == after_mtime, "the project database was not modified",
          f"{before} -> {after_mtime}")
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    for failure in BLOCKING:
        print(f"  FAILED: {failure}")
    log_text = (data_dir / "server.log").read_text(encoding="utf-8", errors="replace")
    check("Traceback" not in log_text, "the server log has no traceback",
          log_text[-300:] if "Traceback" in log_text else "")
    shutil.rmtree(data_dir, ignore_errors=True)
    return 1 if BLOCKING else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
