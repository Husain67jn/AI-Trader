"""FastAPI application: authenticated local dashboard, kill switch, owner controls."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import broker, config, data, db, departments, learning, providers, risk
from . import broker as broker_module
from .broker import broker_status, can_arm
from .claude import get_provider
from .execution import ExecutionService, unresolved_orders, unverified_orders
from .risk import RiskEngine
from .scheduler import Scheduler

config.load_dotenv()

_login_failures: dict[str, list[float]] = {}
# A password shorter than this is refused by rotation. The generated first-run
# password is 24 characters, so this only bites a deliberately chosen weak one.
MIN_PASSWORD_LENGTH = 12
# How many source addresses the throttle remembers before the oldest entries are
# dropped. Without a bound the map would grow with every address that ever failed.
_FAILURE_MAP_MAX = 4096


# --- authentication helpers -------------------------------------------------
def _password_record() -> dict | None:
    raw = db.get_setting("owner_password")
    return json.loads(raw) if raw else None


def _has_password() -> bool:
    return _password_record() is not None


def _password_epoch() -> int:
    """Bumped on every rotation, so sessions issued before it stop validating."""
    record = _password_record() or {}
    try:
        return int(record.get("epoch", 0))
    except (TypeError, ValueError):
        return 0


def _store_password(password: str) -> int:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    epoch = _password_epoch() + 1
    db.set_setting("owner_password", json.dumps(
        {"salt": salt.hex(), "hash": digest.hex(), "epoch": epoch}), actor="system")
    return epoch


def ensure_owner_password() -> str | None:
    """Create the owner credential once. Returns a generated password if one was needed."""
    if _has_password():
        return None
    password = os.getenv("TRADER_OWNER_PASSWORD")
    generated = None
    if not password:
        password = secrets.token_urlsafe(18)
        generated = password
        path = config.DATA_DIR / "owner_password.txt"
        path.write_text(
            "Generated local owner password (delete after storing it safely):\n"
            + password + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    _store_password(password)
    db.log("system", "owner_password_created", {
        "generated_file": "data/owner_password.txt" if generated else None})
    return generated


def verify_password(password: str) -> bool:
    record = _password_record()
    if not record:
        return False
    try:
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(record["salt"]),
                                n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), record["hash"])
    except (KeyError, ValueError):
        return False


def load_secret() -> bytes:
    env = os.getenv("TRADER_SECRET")
    if env:
        return env.encode()
    path = config.DATA_DIR / "secret.key"
    if path.exists():
        return path.read_bytes()
    secret = secrets.token_hex(32).encode()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(secret)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_session(secret: bytes, epoch: int = 0) -> tuple[str, str, int]:
    csrf = secrets.token_urlsafe(24)
    expires = int(time.time()) + config.SESSION_TTL_S
    payload = _b64(json.dumps({"sub": "owner", "exp": expires, "csrf": csrf,
                               "epoch": epoch}, separators=(",", ":")).encode())
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}", csrf, expires


def read_session(token: str | None, secret: bytes) -> dict | None:
    if not token or "." not in token:
        return None
    payload, signature = token.rsplit(".", 1)
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        session = json.loads(_unb64(payload))
    except (ValueError, TypeError):
        return None
    if int(session.get("exp", 0)) < time.time():
        return None
    return session


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _login_failures.get(ip, []) if now - t < config.LOGIN_WINDOW_S]
    if hits:
        _login_failures[ip] = hits
    else:
        _login_failures.pop(ip, None)
    if len(_login_failures) > _FAILURE_MAP_MAX:
        # Bound the map rather than let every address that ever failed accumulate.
        for stale in sorted(_login_failures, key=lambda k: _login_failures[k][-1])[:1000]:
            _login_failures.pop(stale, None)
    return len(hits) >= config.LOGIN_MAX_ATTEMPTS


def require_owner(request: Request) -> dict:
    session = read_session(request.cookies.get(config.SESSION_COOKIE), request.app.state.secret)
    if session is None:
        raise HTTPException(status_code=401, detail="authentication required")
    # A rotated password invalidates every session issued under the old one.
    if int(session.get("epoch", 0)) != _password_epoch():
        raise HTTPException(status_code=401, detail="session is from a retired password")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        token = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(token, session.get("csrf", "")):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
    return session


# --- app factory ------------------------------------------------------------
def secure_cookie() -> bool:
    """True only when something in front of us terminates HTTPS."""
    return os.getenv("TRADER_HTTPS") == "1"


def transport_security() -> dict:
    """How this session is carried, stated rather than assumed.

    Local development is loopback HTTP and stays that way. LAN mode is plain HTTP
    unless a TLS proxy terminates it, which is a real exposure for the session cookie
    and the password, so it is named here and in the audit log instead of being left
    for the operator to infer.
    """
    https, lan = secure_cookie(), os.getenv("TRADER_LAN") == "1"
    if https:
        return {"state": "HTTPS", "secure_cookie": True, "lan_mode": lan,
                "reason": "TRADER_HTTPS=1: the session cookie is Secure-only"}
    if lan:
        return {"state": "INSECURE_LAN", "secure_cookie": False, "lan_mode": True,
                "reason": "LAN mode over plain HTTP: the cookie and the password cross "
                          "the local network unencrypted",
                "recommendation": "Put TLS in front (reverse proxy, VPN, or Tailscale) "
                                  "and set TRADER_HTTPS=1; only use plain HTTP on a "
                                  "network you trust."}
    return {"state": "LOCAL_HTTP", "secure_cookie": False, "lan_mode": False,
            "reason": "loopback HTTP only; traffic does not leave this machine"}


def connect_broker():
    return broker.connect_from_env()


# Importing this module creates no application, opens no database and connects no
# terminal: uvicorn is started on the factory (`app.api:create_app`, see
# scripts/serve.py), so `import app.api` can neither reset the kill switch nor start an
# MT5 session. Tests build the app they need with create_app(broker=..., provider=...).
def create_app(broker=None, provider=None) -> FastAPI:
    db.init_db()
    ensure_owner_password()
    broker = broker or connect_broker()
    provider = provider or get_provider()
    symbols = data.eligible_symbols(broker)
    resolution = broker_module.resolved_symbols(broker)
    execution = ExecutionService(broker, RiskEngine(risk.load_limits()))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A restart must never resume trading, and any order left in an unknown state
        # must be settled against the broker before anything new is considered.
        security = transport_security()
        if security["state"] == "INSECURE_LAN":
            db.log("startup", "alert", {"kind": "insecure_transport", **security})
        startup = execution.reconcile()
        if startup.get("unresolved_orders"):
            db.log("startup", "alert", {"kind": "unresolved_orders_at_startup",
                                        "orders": startup["unresolved_orders"]})
        if startup.get("unverified_orders"):
            # Settled as absent, but the lookup could not rule out that they executed.
            db.log("startup", "alert", {"kind": "orders_absent_unverified_at_startup",
                                        "orders": startup["unverified_orders"]})
        if os.getenv("TRADER_SCHEDULER", "1") != "0":
            app.state.scheduler = Scheduler(broker, provider, execution, symbols)
            app.state.scheduler.start()
        yield
        if getattr(app.state, "scheduler", None):
            await app.state.scheduler.stop()
        broker.disconnect()

    app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION,
                  lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.broker = broker
    app.state.provider = provider
    app.state.execution = execution
    app.state.symbols = symbols
    app.state.secret = load_secret()

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'")
        return response

    # --- public ------------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "demo_only": config.DEMO_ONLY, "db": db.ping(),
                "version": config.APP_VERSION}

    @app.get("/api/auth/required")
    def auth_required():
        return {"auth_required": True, "password_configured": _has_password()}

    @app.post("/api/auth/login")
    def login(request: Request, body: dict = Body(...)):
        ip = request.client.host if request.client else "unknown"
        if _rate_limited(ip):
            raise HTTPException(status_code=429, detail="too many attempts; wait a minute")
        if not verify_password(str(body.get("password", ""))):
            _login_failures.setdefault(ip, []).append(time.time())
            db.log("auth", "login_failed", {"ip": ip})
            raise HTTPException(status_code=401, detail="invalid credentials")
        _login_failures.pop(ip, None)
        token, csrf, expires = issue_session(app.state.secret, _password_epoch())
        response = JSONResponse({"ok": True, "csrf": csrf, "expires_at": expires})
        response.set_cookie(
            config.SESSION_COOKIE, token, max_age=config.SESSION_TTL_S,
            httponly=True, samesite="lax", secure=secure_cookie(), path="/")
        db.log("auth", "login_ok", {"ip": ip})
        return response

    @app.post("/api/auth/logout")
    def logout(session=Depends(require_owner)):
        response = JSONResponse({"ok": True})
        response.delete_cookie(config.SESSION_COOKIE, path="/")
        return response

    @app.post("/api/auth/password")
    def rotate_password(body: dict = Body(...), session=Depends(require_owner)):
        """Rotate the owner password. Requires the current one, not just a session.

        Every session issued under the old password stops validating immediately, so
        the reply carries a fresh cookie for the caller who performed the rotation.
        """
        current = str(body.get("current_password", ""))
        new_password = str(body.get("new_password", ""))
        if not verify_password(current):
            db.log("auth", "password_rotation_refused", {"reason": "current password"})
            raise HTTPException(status_code=403, detail="current password is incorrect")
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"new password must be at least {MIN_PASSWORD_LENGTH} characters")
        if hmac.compare_digest(new_password, current):
            raise HTTPException(status_code=400,
                                detail="new password must differ from the current one")
        epoch = _store_password(new_password)
        db.log("auth", "password_rotated", {"epoch": epoch})
        token, csrf, expires = issue_session(app.state.secret, epoch)
        response = JSONResponse({"ok": True, "csrf": csrf, "expires_at": expires})
        response.set_cookie(
            config.SESSION_COOKIE, token, max_age=config.SESSION_TTL_S,
            httponly=True, samesite="lax", secure=secure_cookie(), path="/")
        return response

    # --- authenticated reads ----------------------------------------------
    @app.get("/api/session")
    def session_info(session=Depends(require_owner)):
        return {"ok": True, "csrf": session["csrf"], "expires_at": session["exp"]}

    @app.get("/api/overview")
    def overview(session=Depends(require_owner)):
        account = broker.account()
        health, health_detail = data.market_health(broker, symbols)
        quotes = {}
        for symbol in symbols[:8]:
            quote = broker.quote(symbol)
            if quote:
                quotes[symbol] = quote.model_dump(mode="json")
        proposal_rows = db.query(
            "SELECT * FROM proposals ORDER BY created_at DESC LIMIT 10")
        report_rows = db.query(
            "SELECT * FROM reports ORDER BY as_of_utc DESC LIMIT 40")
        latest_reports: dict[str, dict] = {}
        for row in report_rows:
            latest_reports.setdefault(row["department"], json.loads(row["payload"]))
        audit_rows = db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT 40")
        return {
            "app": {"name": config.APP_NAME, "version": config.APP_VERSION,
                    "demo_only": config.DEMO_ONLY, "broker": broker.name,
                    "broker_class": broker.kind,
                    "provider": getattr(provider, "name", "unknown")},
            "broker_status": broker_status(broker),
            "system_state": _system_state(broker),
            "trade_armed": _trade_armed(broker),
            "arm_blocker": can_arm(broker, account)[1],
            "kill_switch": db.kill_switch_state(),
            "account": account.model_dump(mode="json") if account else None,
            "data_health": health,
            "data_health_detail": health_detail,
            # base -> broker symbol, as resolved from the terminal rather than assumed,
            # so a broker suffix or a look-alike contract is visible on the dashboard.
            "symbol_resolution": resolution,
            "transport_security": transport_security(),
            "calendar_status": providers.calendar_status(),
            "news_status": providers.news_status(),
            "news": providers.stored_news(15),
            "unresolved_orders": unresolved_orders(),
            # Settled as absent when the lookup could not rule out that they executed.
            # They do not block trading, so without this key a questionable row and a
            # clean desk look identical to anything reading the overview.
            "unverified_orders": unverified_orders(),
            "quotes": quotes,
            "positions": [p.model_dump(mode="json") for p in broker.positions()],
            "proposals": [_json(row["payload"]) for row in proposal_rows],
            "reports": list(latest_reports.values()),
            "calendar": data.upcoming_events(15),
            "risk": {"limits": _jsonable(risk.load_limits()),
                     "state": db.kill_switch_state()},
            "performance": learning.performance(),
            "reviews": learning.recent_reviews(10),
            "experiments": [_json(row["metrics"]) | {"name": row["name"],
                                                     "status": row["status"]}
                            for row in db.query(
                                "SELECT * FROM experiments ORDER BY id DESC LIMIT 10")],
            "audit": [{"ts": row["ts"], "actor": row["actor"], "action": row["action"],
                       "detail": _json(row["detail"])}
                      for row in audit_rows],
            "server_time_utc": db.now_iso(),
        }

    # --- owner actions -----------------------------------------------------
    @app.post("/api/kill-switch")
    def kill_switch(body: dict = Body(...), session=Depends(require_owner)):
        action = str(body.get("action", ""))
        if action == "pause":
            state = risk.pause("owner", str(body.get("reason", "")))
        elif action == "estop":
            state = risk.emergency_stop("owner", str(body.get("reason", "operator request")))
        elif action == "arm":
            if str(body.get("confirm", "")) != "ARM DEMO":
                raise HTTPException(status_code=400, detail='confirmation text must be "ARM DEMO"')
            account = broker.account()
            ok, why = can_arm(broker, account)
            if not ok:
                db.log("owner", "arm_refused", {"reason": why, "broker": broker.name})
                raise HTTPException(status_code=403, detail=f"cannot arm: {why}")
            if not db.ping():
                raise HTTPException(status_code=503, detail="database unavailable")
            health, detail = data.market_health(broker, symbols)
            if health != "HEALTHY":
                raise HTTPException(status_code=409,
                                    detail=f"cannot arm: market data health is {health}")
            pending = unresolved_orders()
            if pending:
                raise HTTPException(
                    status_code=409,
                    detail=f"cannot arm: {len(pending)} unresolved order(s) must be reconciled")
            state = risk.arm("owner", account, broker)
        else:
            raise HTTPException(status_code=400, detail="action must be pause, estop or arm")
        db.log("owner", "kill_switch_action", {"action": action, "state": state})
        return {"ok": True, "state": state, "system_state": _system_state(broker)}

    @app.post("/api/settings/risk")
    def update_risk(body: dict = Body(...), session=Depends(require_owner)):
        if str(body.get("confirm", "")) != "I UNDERSTAND":
            raise HTTPException(status_code=400,
                                detail='confirmation text must be "I UNDERSTAND"')
        incoming = body.get("limits")
        if not isinstance(incoming, dict) or not incoming:
            raise HTTPException(status_code=400, detail="limits object required")
        limits = risk.load_limits()
        clean: dict = {}
        for key, value in incoming.items():
            try:
                clean[key] = risk.clean_limit(key, value)
            except (TypeError, ValueError, ArithmeticError):
                raise HTTPException(status_code=400, detail=f"invalid value for {key}")
        limits.update(clean)
        db.set_setting("risk_limits", json.dumps(_jsonable(limits)), actor="owner")
        app.state.execution.engine = RiskEngine(risk.load_limits())
        return {"ok": True, "limits": _jsonable(risk.load_limits())}

    @app.post("/api/research/run")
    async def run_research(session=Depends(require_owner)):
        result = await asyncio.to_thread(departments.run, provider, broker, symbols)
        return {"ok": True, "proposal": result["proposal"],
                "reports": len(result["reports"]), "data_health": result["data_health"]}

    @app.post("/api/reconcile")
    def reconcile(session=Depends(require_owner)):
        return app.state.execution.reconcile()

    @app.post("/api/calendar/refresh")
    def refresh_calendar(session=Depends(require_owner)):
        try:
            return {"ok": True, **providers.ingest_calendar(14)}
        except Exception as exc:  # noqa: BLE001 - reported, never faked
            raise HTTPException(status_code=503,
                                detail=f"calendar feed unavailable ({type(exc).__name__})")

    @app.post("/api/news/refresh")
    def refresh_news(session=Depends(require_owner)):
        try:
            return {"ok": True, **providers.ingest_news(20)}
        except Exception as exc:  # noqa: BLE001 - reported, never faked
            raise HTTPException(status_code=503,
                                detail=f"news feed unavailable ({type(exc).__name__})")

    @app.post("/api/learning/audit")
    async def audit_trades(session=Depends(require_owner)):
        reviews = await asyncio.to_thread(learning.audit_all_closed, broker, provider)
        return {"ok": True, "reviews": len(reviews)}

    @app.post("/api/experiments/{experiment_id}/promote")
    def promote(experiment_id: int, body: dict = Body(...),
                session=Depends(require_owner)):
        human_approved = bool(body.get("human_approved")) and (
            str(body.get("confirm", "")) == "PROMOTE")
        return learning.promote_experiment(experiment_id, human_approved, "owner")

    # --- dashboard ---------------------------------------------------------
    @app.get("/")
    def index():
        path = config.STATIC_DIR / "index.html"
        if not path.exists():
            return JSONResponse({"detail": "dashboard not installed"}, status_code=500)
        return FileResponse(path)

    if config.STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")

    return app


def _json(raw: str) -> dict:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"value": value}
    except (TypeError, ValueError):
        return {}


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "as_tuple"):
        return str(value)
    return value


def _trade_armed(broker) -> bool:
    """True only when the kill switch is ARMED *and* a live demo account verifies.

    A research-only broker therefore reports False no matter what the kill switch says.
    """
    try:
        if db.kill_switch_state() != "ARMED":
            return False
        return can_arm(broker, broker.account())[0]
    except Exception:
        return False


def _system_state(broker) -> str:
    state = db.kill_switch_state()
    if state == "EMERGENCY_STOP":
        return "EMERGENCY_STOP"
    if state != "ARMED":
        return "PAUSED"
    try:
        account = broker.account() if broker else None
    except Exception:
        account = None
    return "DEMO_ARMED" if can_arm(broker, account)[0] else "PAUSED"