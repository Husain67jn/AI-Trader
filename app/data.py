"""Market data, freshness gate, economic calendar. No observed value is ever invented."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal

from . import config, db
from .broker import REAL_MT5
from .broker import eligible_symbols as _eligible_symbols
from .models import Quote

# Order of severity for the desk verdict. CLOSED sits between a healthy desk and a
# degraded one: it is not a data fault, but nothing may be traded on it either.
QUALITY_ORDER = ["HEALTHY", "CLOSED", "DEGRADED", "STALE", "UNKNOWN"]


def eligible_symbols(broker) -> list[str]:
    """One broker symbol per whitelisted instrument (see broker.resolve_symbol)."""
    return _eligible_symbols(broker)


def quote_status(quote: Quote | None, max_age_s: int, now: datetime | None = None,
                 market_open: bool | None = None) -> str:
    """HEALTHY / STALE / CLOSED / UNKNOWN for one quote.

    An old print has two very different causes, and calling both STALE is what made a
    closed weekend look like a broken feed. When the desk's own session is closed, the
    quote is the last tick of the previous session: that is CLOSED - no data fault,
    and still nothing tradable. `market_open=None` means "assume open", so an old print
    with no session information is reported as STALE rather than excused.
    """
    if quote is None:
        return "UNKNOWN"
    age = ((now or datetime.now(timezone.utc)) - quote.ts_utc).total_seconds()
    if age < -5:
        return "UNKNOWN"
    if age > max_age_s:
        return "STALE" if market_open is None or market_open else "CLOSED"
    return "HEALTHY"


def market_health(broker, symbols: list[str] | None = None) -> tuple[str, dict]:
    """Only HEALTHY may reach execution. One stale quote degrades the whole desk.

    Every resolved instrument is probed, not a sample of them: a quote nobody checked
    cannot degrade anything, and the instruments the desk may actually trade are the
    ones that have to be healthy.

    CLOSED is returned only when the desk's trading session is closed *and* the quotes
    are older than the freshness limit; the last-tick timestamps are reported next to
    it so the verdict can be checked rather than taken on trust.
    """
    now = datetime.now(timezone.utc)
    try:
        account = broker.account()
        details: dict = {"account": None, "quotes": {}, "checked_at_utc": now.isoformat()}
        if account is None:
            return "UNKNOWN", {**details, "reason": "no account snapshot"}
        details["account"] = {"login": account.login, "mode": account.trade_mode}
        details["session_open"] = config.session_open(now)
        info = getattr(broker, "mt5", None) if getattr(broker, "kind", None) == REAL_MT5 else None
        if info is not None:
            terminal = info.terminal_info()
            details["terminal"] = bool(terminal and terminal.connected)
            if not details["terminal"]:
                return "UNKNOWN", {**details, "reason": "terminal disconnected"}
        elif getattr(broker, "connected", True) is False:
            return "UNKNOWN", {**details, "reason": "broker disconnected"}
        probes = symbols or eligible_symbols(broker)
        if not probes:
            return "UNKNOWN", {**details, "reason": "no eligible symbols"}
        max_age = config.DEFAULT_RISK_LIMITS["max_quote_age_s"]
        worst = "HEALTHY"
        last_tick = None
        for symbol in probes:
            quote = broker.quote(symbol)
            status = quote_status(quote, max_age, now=now,
                                  market_open=details["session_open"])
            details["quotes"][symbol] = status
            if quote is not None and (last_tick is None or quote.ts_utc < last_tick):
                last_tick = quote.ts_utc
            if status == "UNKNOWN":
                worst = "DEGRADED" if worst == "HEALTHY" else worst
            elif status == "CLOSED":
                worst = "CLOSED" if worst in ("HEALTHY", "DEGRADED") else worst
            elif max_age and status == "STALE":
                worst = "STALE" if worst in ("HEALTHY", "DEGRADED") else worst
        if last_tick is not None:
            details["oldest_tick_utc"] = last_tick.isoformat()
            details["oldest_tick_age_s"] = round((now - last_tick).total_seconds(), 1)
        if worst == "CLOSED":
            details["reason"] = ("the trading session is closed; the quotes are the last "
                                 "ticks of the previous session, not a feed fault")
        return worst, details
    except Exception as exc:
        return "UNKNOWN", {"error": type(exc).__name__}


def store_snapshot(broker, symbols: list[str] | None = None) -> dict:
    now = db.now_iso()
    snap: dict = {"ts_utc": now, "quotes": {}}
    with db.tx() as con:
        for symbol in symbols or eligible_symbols(broker):
            quote = broker.quote(symbol)
            if quote is None:
                continue
            payload = quote.model_dump(mode="json")
            snap["quotes"][symbol] = payload
            con.execute(
                "INSERT INTO market_snapshots(ts, symbol, payload) VALUES(?,?,?)",
                (now, symbol, json.dumps(payload)),
            )
    return snap


def _missing_candles_are_outside_the_session(previous: int, current: int,
                                             step: int) -> bool:
    """True when every candle missing between two bars falls outside the trading session.

    A weekend, or the daily break metals take around the session close, removes candles
    from the feed without anything being wrong with it. The broker's own session hours
    are not exposed by the MetaTrader5 package, so the desk's configured session is the
    calendar used here: a gap whose missing candles are all outside it is a closure, and
    a gap with even one missing candle inside it is a real hole.
    ponytail: session-calendar heuristic; swap in the broker's session table if MT5 ever
    exposes one.
    """
    stamp = previous + step
    while stamp < current:
        if config.session_open(datetime.fromtimestamp(stamp, tz=timezone.utc)):
            return False
        stamp += step
    return True


def bars_quality(bars: list[dict], timeframe: str = "H1") -> dict:
    """Detect missing, duplicated, out-of-order or impossible candles.

    Reported, never repaired: a gap is evidence about the data, and silently filling it
    would invent prices the broker never printed. Expected closures - a weekend, a daily
    break - are separated from real holes so that "the market was shut" is not presented
    as "the feed lost candles", and vice versa.
    """
    issues: list[str] = []
    closures: list[dict] = []
    if not bars:
        return {"ok": False, "issues": ["no bars returned"], "closures": [], "count": 0}
    steps = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600,
             "H4": 14400, "D1": 86400}
    step = steps.get(timeframe, 3600)
    seen: set[int] = set()
    for index, bar in enumerate(bars):
        stamp = int(bar["time"])
        if stamp in seen:
            issues.append(f"duplicate candle at {stamp}")
        seen.add(stamp)
        if bar["high"] < bar["low"] or not (bar["low"] <= bar["open"] <= bar["high"]) \
                or not (bar["low"] <= bar["close"] <= bar["high"]):
            issues.append(f"impossible OHLC at {stamp}")
        if index and stamp <= int(bars[index - 1]["time"]):
            issues.append(f"out-of-order candle at {stamp}")
        elif index and stamp - int(bars[index - 1]["time"]) > step * 1.5:
            previous = int(bars[index - 1]["time"])
            if _missing_candles_are_outside_the_session(previous, stamp, step):
                closures.append({"after": previous, "before": stamp,
                                 "seconds": stamp - previous})
            else:
                issues.append(f"gap before {stamp}")
    return {"ok": not issues, "issues": issues, "closures": closures, "count": len(bars)}


def _baseline(key: str, equity: Decimal, bucket: str) -> Decimal:
    raw = db.get_setting(key)
    if raw:
        try:
            stored = json.loads(raw)
            if stored.get("bucket") == bucket:
                return Decimal(str(stored["equity"]))
        except (ValueError, KeyError, ArithmeticError):
            pass
    db.set_setting(key, json.dumps({"bucket": bucket, "equity": str(equity)}), actor="system")
    return equity


def equity_baselines(equity: Decimal, account_key: str = "default") -> tuple[Decimal, Decimal]:
    """Day and week start equity for one account.

    Keyed per account: two different accounts must never share a loss baseline, or a
    fresh account would inherit another account's drawdown.
    """
    today = date.today()
    year, week, _ = today.isocalendar()
    day = _baseline(f"day_start_equity:{account_key}", equity, today.isoformat())
    week_start = _baseline(f"week_start_equity:{account_key}", equity, f"{year}-W{week:02d}")
    return day, week_start


def peak_equity(equity: Decimal, account_key: str = "default") -> Decimal:
    """Highest equity ever recorded for this account. Feeds the drawdown check."""
    key = f"peak_equity:{account_key}"
    raw = db.get_setting(key)
    stored: Decimal | None = None
    if raw:
        try:
            stored = Decimal(str(json.loads(raw)["equity"]))
        except (ValueError, KeyError, ArithmeticError, TypeError):
            stored = None
    peak = max(stored, equity) if stored is not None else equity
    if stored is None or peak != stored:
        db.set_setting(key, json.dumps({"equity": str(peak)}), actor="system")
    return peak


def blackout_active(currencies: list[str], minutes: int | None = None,
                    impacts: tuple[str, ...] | None = None) -> tuple[bool, list[dict]]:
    """Conservative window around scheduled high-impact releases.

    Only events whose impact reaches the configured threshold black out trading; an
    event whose impact is *unknown* always does, because an unlabelled release is not
    evidence of a quiet market. Every hit carries its impact and its `quality` (the
    provenance of the row), so a caller can tell a licensed feed's High-impact release
    from a free public calendar's note.
    """
    minutes = minutes or config.DEFAULT_RISK_LIMITS["event_blackout_minutes"]
    allowed = impacts if impacts is not None else config.event_blackout_impacts()
    now = datetime.now(timezone.utc)
    hits: list[dict] = []
    try:
        rows = db.query("SELECT * FROM events ORDER BY ts_utc")
    except Exception:
        return False, []
    for row in rows:
        try:
            when = datetime.fromisoformat(row["ts_utc"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if row["currency"] not in currencies:
            continue
        impact = (row["impact"] or "UNKNOWN").upper()
        if impact != "UNKNOWN" and impact not in allowed:
            continue
        if abs((when - now).total_seconds()) <= minutes * 60:
            hits.append({"event_id": row["event_id"], "title": row["title"],
                         "currency": row["currency"], "ts_utc": row["ts_utc"],
                         "impact": impact, "quality": row["quality"]})
    return bool(hits), hits


def upcoming_events(limit: int = 20) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    rows = db.query("SELECT * FROM events WHERE ts_utc >= ? ORDER BY ts_utc LIMIT ?",
                    (now, limit))
    return [dict(row) for row in rows]


def fetch_fred_series(series_id: str, limit: int = 12) -> list[dict]:
    """Official FRED API adapter. Requires FRED_API_KEY in the local .env."""
    key = os.getenv("FRED_API_KEY")
    if not key:
        raise MissingCredential("FRED_API_KEY is not configured")
    import httpx
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series_id, "api_key": key, "file_type": "json",
              "sort_order": "desc", "limit": limit}
    response = httpx.get(url, params=params, timeout=20)
    response.raise_for_status()
    retrieved = db.now_iso()
    return [{
        "series_id": series_id, "date": obs["date"], "value": obs["value"],
        "source_url": f"https://fred.stlouisfed.org/series/{series_id}",
        "retrieved_at": retrieved,
    } for obs in response.json().get("observations", [])]


class MissingCredential(RuntimeError):
    pass