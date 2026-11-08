"""Broker abstraction: official MetaTrader5 Python API plus a deterministic mock."""
from __future__ import annotations

import math
import os
import random
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from . import config, db
from .models import (
    AccountSnapshot, Instrument, OrderResult, Position, Quote, TradeRecord,
)


class BrokerUnavailable(RuntimeError):
    """A broker session could not be established.

    `code` is the MetaTrader5 error code when there is one (e.g. -10005) and `hint`
    is the operator-facing next step, so neither is lost when the message is logged.
    """

    def __init__(self, message: str, code: int | None = None, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


def mt5_error_hint(code: int | None) -> str:
    """One actionable sentence per known MetaTrader5 failure class."""
    if code == -10005:
        return ("IPC handshake timed out: the terminal did not answer the Python "
                "package. Check that the MetaTrader5 package version matches the "
                "terminal build, that the terminal is running, and that security "
                "software is not blocking it. Run scripts/diagnostics.py for the "
                "package and terminal builds this machine is actually using.")
    if code == -6:
        return ("The terminal answered but no account is authorized: log a DEMO "
                "account into the terminal, then retry.")
    if code == -10004:
        return "initialize() rejected a parameter; check the TRADER_MT5_* settings."
    if code is None:
        return ("The terminal could not be reached. Start it, log into a DEMO account, "
                "and confirm its build matches the installed MetaTrader5 package.")
    return (f"MetaTrader5 error {code}. Check the terminal journal, that it is running "
            "and logged into a DEMO account.")


class HistoryUnavailable(RuntimeError):
    """The terminal could not answer a history query, so absence proves nothing.

    The package returns None both for "no such order" and for "the call failed"
    (`last_error()` disambiguates, and calling it is the whole point of this class).
    Any caller that treats the two as the same concludes "never executed" from an
    error, which is how an executed order gets erased from the local ledger.
    """


def request_key(request_id: str) -> str:
    """The terminal-side key for a request id: the order comment, and the needle that
    finds it again.

    Derived in exactly one place, because the writer (_mt5_request) and the searcher
    (order_by_request) must never drift apart. Sixteen characters stay well inside
    MT5's 31-character comment limit and put a collision out of reach of a single
    demo desk, while eight made one reachable by two short ids that share a prefix.
    """
    return f"{config.ORDER_COMMENT}-{request_id[:16]}"


def _last_error_tuple(mt5) -> tuple[int | None, str]:
    try:
        error = mt5.last_error()
    except Exception:  # noqa: BLE001 - a broken shim must not mask the real failure
        return None, "unknown"
    if isinstance(error, (tuple, list)) and len(error) == 2:
        try:
            return int(error[0]), str(error[1])
        except (TypeError, ValueError):
            return None, str(error)
    return None, str(error)


def broker_status(broker) -> dict:
    """What the dashboard is allowed to claim about the connection.

    `simulated` is not a hint: it is derived from the broker's class, which no
    environment variable can change for the mock.
    """
    real = getattr(broker, "kind", None) == REAL_MT5
    status = {
        "name": getattr(broker, "name", "unknown"),
        "kind": getattr(broker, "kind", "UNKNOWN"),
        "real_mt5": real,
        "simulated": not real,
        "connected": bool(getattr(broker, "connected", False)),
        "timeout_ms": config.mt5_init_timeout_ms(),
        "error": None,
    }
    if real:
        status["label"] = (
            "REAL MT5 — connected terminal, demo account"
            if status["connected"]
            else "REAL MT5 — not connected"
        )
        return status
    status["label"] = "SIMULATED — mock broker, research only (no real orders)"
    status["error"] = getattr(broker, "mt5_error", None)
    if status["error"] is None:
        if (os.getenv("TRADER_BROKER") or "auto").lower() == "mock":
            status["error"] = {
                "code": None,
                "message": "MT5 is disabled: TRADER_BROKER=mock",
                "hint": "Set TRADER_BROKER=auto to attempt a real MT5 session.",
            }
        else:
            status["error"] = {
                "code": None,
                "message": "no MT5 session was established",
                "hint": "Run scripts/diagnostics.py to see the connection error.",
            }
    return status


def D(value) -> Decimal:
    return Decimal(str(value))


class Broker(Protocol):
    name: str
    kind: str
    def connect(self) -> "Broker": ...
    def disconnect(self) -> None: ...
    def account(self) -> AccountSnapshot | None: ...
    def symbols(self) -> list[str]: ...
    def instrument(self, symbol: str) -> Instrument | None: ...
    def quote(self, symbol: str) -> Quote | None: ...
    def bars(self, symbol: str, timeframe: str, count: int) -> list[dict]: ...
    def calc_margin(self, symbol: str, direction: str, volume: Decimal, price: Decimal) -> Decimal | None: ...
    def check_order(self, request: dict) -> tuple[bool, str]: ...
    def submit(self, request: dict) -> OrderResult: ...
    def order_status(self, ticket: int) -> str: ...
    def order_by_request(self, request_id: str) -> OrderResult | None: ...
    def positions(self) -> list[Position]: ...
    def closed_trades(self, days: int = 30) -> list[TradeRecord]: ...


# A broker is either a real MT5 terminal session or a simulator. Only the former
# can ever satisfy demo-account verification; the mock is research-only by class,
# not by configuration, so no env value can promote it.
REAL_MT5 = "REAL_MT5"
RESEARCH_ONLY = "RESEARCH_ONLY"

# Canonical order states. Every adapter must map its own vocabulary into these.
ORDER_STATES = ("FILLED", "PARTIALLY_FILLED", "PENDING", "STARTED", "CANCELED",
                "REJECTED", "EXPIRED", "UNKNOWN")
# Terminal states that mean the order will never fill.
DEAD_STATES = ("CANCELED", "REJECTED", "EXPIRED")


def verify_demo(account: AccountSnapshot | None) -> bool:
    """Account-level demo check only. It says nothing about which broker produced it."""
    return account is not None and account.trade_mode == "DEMO"


def resolve_symbol(broker, requested: str) -> tuple[str | None, str]:
    """The broker's own symbol for a requested instrument, and how it was chosen.

    Resolution never invents a contract. In order:
      1. `requested` itself, when the broker offers that exact name;
      2. the configured mapping for its canonical base (`config.symbol_map()`);
      3. discovery among the broker's symbols for the same canonical base, preferring
         an exact base match and then the shortest name.

    Step 3's ordering is what keeps look-alike instruments apart: for XAUUSD the
    candidates are XAUUSDm and XAUUSD247m, and the shorter, exactly-suffixed name wins.
    `via` names the step, so a caller can tell an explicit mapping from a discovered
    one and no substitution is ever silent; a configured mapping the broker does not
    offer is audited rather than passed over quietly.
    """
    base = config.canonical_base(requested)
    if base is None:
        return None, "not-whitelisted"
    try:
        available = list(broker.symbols() or [])
    except Exception as exc:  # noqa: BLE001 - an unreachable broker has no symbols
        db.log("broker", "symbol_discovery_failed", {"error": type(exc).__name__})
        return None, "broker-unavailable"
    if requested in available:
        return requested, "requested"
    mapped = config.symbol_map().get(base)
    if mapped and mapped in available:
        return mapped, "explicit"
    if mapped:
        db.log("broker", "symbol_map_missing",
               {"base": base, "configured": mapped, "reason": "not offered by this broker"})
    candidates = sorted((s for s in available if config.canonical_base(s) == base),
                        key=lambda s: (s.upper() != base, len(s), s))
    if candidates:
        return candidates[0], "discovered-after-map-miss" if mapped else "discovered"
    return None, "unavailable"


def resolved_symbols(broker) -> dict[str, str]:
    """base -> broker symbol for every whitelisted instrument this broker offers.

    One symbol per instrument, so a second contract on the same base (XAUUSD247m) can
    never enter the desk's symbol list alongside the intended one.
    """
    out: dict[str, str] = {}
    for base in config.MAJOR_BASES:
        symbol, _ = resolve_symbol(broker, base)
        if symbol:
            out[base] = symbol
    return out


def eligible_symbols(broker) -> list[str]:
    """The tradable instruments, in whitelist order, as the broker names them."""
    return list(resolved_symbols(broker).values())


def instrument_issues(instrument: Instrument | None, quote: Quote | None) -> list[str]:
    """Everything wrong with an instrument's metadata or its current quote.

    Reported, never repaired: a contract whose step, point or tick value is unknown
    cannot size a position, and pretending otherwise would size it with a guess.
    """
    if instrument is None:
        return ["instrument metadata is unavailable"]
    issues = []
    if instrument.contract_size <= 0:
        issues.append("contract size is not positive")
    if instrument.point <= 0:
        issues.append("point size is not positive")
    if instrument.digits <= 0:
        issues.append("digits is not positive")
    if instrument.volume_min <= 0:
        issues.append("volume minimum is not positive")
    if instrument.volume_step <= 0:
        issues.append("volume step is not positive")
    if instrument.volume_max < instrument.volume_min:
        issues.append("volume maximum is below the minimum")
    if instrument.tick_size <= 0:
        issues.append("tick size is not positive")
    if instrument.tick_value <= 0:
        issues.append("tick value is not positive")
    if quote is None:
        issues.append("no quote")
    elif quote.bid <= 0 or quote.ask <= 0:
        issues.append("bid/ask are not both positive")
    elif quote.ask < quote.bid:
        issues.append("ask is below bid")
    return issues


def symbol_report(broker, symbol: str) -> dict:
    """Read-only evidence about one symbol: what the broker says and what is wrong.

    Used by diagnostics and by the reports that have to state whether the integration
    works, so the answer comes from the adapter rather than from a screenshot.
    """
    instrument = broker.instrument(symbol)
    quote = broker.quote(symbol)
    report = {
        "symbol": symbol,
        "base": config.canonical_base(symbol),
        "quote": None,
        "instrument": instrument.model_dump(mode="json") if instrument else None,
        "issues": instrument_issues(instrument, quote),
    }
    if quote is not None:
        report["quote"] = {
            "bid": str(quote.bid), "ask": str(quote.ask),
            "spread_points": str((quote.ask - quote.bid) / instrument.point)
            if instrument and instrument.point else None,
            "ts_utc": quote.ts_utc.isoformat(),
            "age_s": round((datetime.now(timezone.utc) - quote.ts_utc).total_seconds(), 3),
        }
    return report


def can_arm(broker, account: AccountSnapshot | None) -> tuple[bool, str]:
    """The full arming gate: real MT5 session + verified demo account + broker trade enabled."""
    if getattr(broker, "kind", None) != REAL_MT5:
        return False, "broker is research-only; a simulated account can never be armed"
    if not verify_demo(account):
        return False, f"account trade mode is {getattr(account, 'trade_mode', None)}, not DEMO"
    if account is not None and not account.trade_allowed:
        return False, "the account has trading disabled"
    return True, "verified demo account on a live MT5 session"


def connect_from_env():
    """MT5 unless TRADER_BROKER=mock. Falls back to the mock broker, loudly.

    The fallback carries `mt5_error` so the dashboard can say *why* the real
    session is missing instead of presenting the simulator as if it were MT5.
    """
    from . import db
    if os.getenv("TRADER_BROKER", "auto").lower() == "mock":
        return MockBroker().connect()
    try:
        return MT5Broker().connect()
    except Exception as exc:
        code = getattr(exc, "code", None)
        hint = getattr(exc, "hint", "") or mt5_error_hint(code)
        db.log("system", "mt5_unavailable", {"error": type(exc).__name__,
                                             "code": code,
                                             "detail": str(exc)[:200]})
        fallback = MockBroker().connect()
        fallback.mt5_error = {"code": code, "message": str(exc)[:200], "hint": hint}
        return fallback


def _base_specs() -> dict[str, dict]:
    return {
        "EURUSD": dict(price=1.0850, spread=2, contract=100000, pmin=0.01, pmax=100,
                       pstep=0.01, point=0.00001, digits=5, tick=0.00001, tval=1.0),
        "GBPUSD": dict(price=1.2650, spread=2, contract=100000, pmin=0.01, pmax=100,
                       pstep=0.01, point=0.00001, digits=5, tick=0.00001, tval=1.0),
        "USDJPY": dict(price=154.20, spread=2, contract=100000, pmin=0.01, pmax=100,
                       pstep=0.01, point=0.001, digits=3, tick=0.001, tval=0.67),
        "USDCHF": dict(price=0.8950, spread=3, contract=100000, pmin=0.01, pmax=100,
                       pstep=0.01, point=0.00001, digits=5, tick=0.00001, tval=1.0),
        "AUDUSD": dict(price=0.6550, spread=2, contract=100000, pmin=0.01, pmax=100,
                       pstep=0.01, point=0.00001, digits=5, tick=0.00001, tval=1.0),
        "NZDUSD": dict(price=0.6100, spread=3, contract=100000, pmin=0.01, pmax=100,
                       pstep=0.01, point=0.00001, digits=5, tick=0.00001, tval=1.0),
        "USDCAD": dict(price=1.3700, spread=3, contract=100000, pmin=0.01, pmax=100,
                       pstep=0.01, point=0.00001, digits=5, tick=0.00001, tval=1.0),
        "XAUUSD": dict(price=2650.00, spread=20, contract=100, pmin=0.01, pmax=50,
                       pstep=0.01, point=0.01, digits=2, tick=0.01, tval=1.0),
    }


class MockBroker:
    """Deterministic, offline simulator. RESEARCH_ONLY: it can never arm trading."""

    name = "mock"
    kind = RESEARCH_ONLY
    magic = config.MAGIC

    def __init__(self, equity: str = "100000"):
        self.equity = D(equity)
        self.balance = D(equity)
        self._specs = _base_specs()
        self._tick_counter = 0
        self._lock = threading.Lock()
        self._positions: list[Position] = []
        self._orders: dict[int, dict] = {}
        self._by_request: dict[str, OrderResult] = {}
        self._closed: list[TradeRecord] = []
        self._opened_at: dict[int, datetime] = {}
        self.submit_mode = "normal"  # normal | timeout_then_fill | reject | partial
        self._quote_ts_override: datetime | None = None
        self.connected = False

    # --- connection / account -------------------------------------------------
    def connect(self) -> "MockBroker":
        self.connected = True
        return self

    def disconnect(self) -> None:
        self.connected = False

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(
            login=10000001, trade_mode=getattr(self, "_force_mode", "DEMO"), equity=self.equity,
            balance=self.balance, margin_free=self.equity,
            currency="USD", leverage=100,
            trade_allowed=getattr(self, "_trade_allowed", True),
            terminal_trade_allowed=getattr(self, "_trade_allowed", True),
        )

    def set_trade_mode(self, mode: str) -> None:
        self._force_mode = mode

    def set_trade_allowed(self, allowed: bool) -> None:
        self._trade_allowed = allowed

    def symbols(self) -> list[str]:
        return list(self._specs)

    def instrument(self, symbol: str) -> Instrument | None:
        spec = self._specs.get(symbol)
        if spec is None:
            return None
        base = config.canonical_base(symbol) or symbol
        return Instrument(
            symbol=symbol, base=base, contract_size=D(spec["contract"]),
            volume_min=D(spec["pmin"]), volume_max=D(spec["pmax"]),
            volume_step=D(spec["pstep"]), point=D(spec["point"]), digits=spec["digits"],
            tick_size=D(spec["tick"]), tick_value=D(spec["tval"]),
            stops_level_points=10,
        )

    def _price(self, symbol: str) -> float:
        spec = self._specs.get(symbol)
        if spec is None:
            raise KeyError(symbol)
        return spec["price"]

    def quote(self, symbol: str) -> Quote | None:
        spec = self._specs.get(symbol)
        if spec is None:
            return None
        mid = self._price(symbol)
        half = spec["spread"] * spec["point"] / 2
        self._tick_counter += 1
        ts = self._quote_ts_override or datetime.now(timezone.utc)
        return Quote(symbol=symbol, bid=D(f"{mid - half:.{spec['digits']}f}"),
                     ask=D(f"{mid + half:.{spec['digits']}f}"), ts_utc=ts)

    def set_quote_age(self, seconds: float) -> None:
        self._quote_ts_override = datetime.now(timezone.utc) - timedelta(seconds=seconds)

    def set_spread(self, symbol: str, points: int) -> None:
        self._specs[symbol]["spread"] = points

    def set_price(self, symbol: str, price: float) -> None:
        self._specs[symbol]["price"] = price

    def calc_margin(self, symbol, direction, volume, price) -> Decimal:
        inst = self.instrument(symbol)
        leverage = D(self.account().leverage)
        return (volume * inst.contract_size * D(price) / leverage).quantize(D("0.01"))

    def check_order(self, request: dict) -> tuple[bool, str]:
        inst = self.instrument(request["symbol"])
        if inst is None:
            return False, "unknown symbol"
        volume = D(request["volume"])
        if volume < inst.volume_min or volume > inst.volume_max:
            return False, "volume outside broker bounds"
        if volume % inst.volume_step != 0:
            return False, "volume is not a valid step"
        price, sl, tp = D(request["price"]), D(request.get("sl", 0)), D(request.get("tp", 0))
        if request["direction"] == "BUY":
            if sl and sl >= price:
                return False, "BUY stop-loss must be below the entry price"
            if tp and tp <= price:
                return False, "BUY take-profit must be above the entry price"
        else:
            if sl and sl <= price:
                return False, "SELL stop-loss must be above the entry price"
            if tp and tp >= price:
                return False, "SELL take-profit must be below the entry price"
        return True, "ok"

    def set_order_state(self, ticket: int, state: str) -> None:
        """Test hook: force a ticket into a canonical state (e.g. CANCELED)."""
        if ticket in self._orders:
            self._orders[ticket]["state"] = state

    # --- execution ------------------------------------------------------------
    def submit(self, request: dict) -> OrderResult:
        request_id = request["request_id"]
        with self._lock:
            if request_id in self._by_request:
                return self._by_request[request_id]  # idempotent replay
            if self.submit_mode == "reject":
                result = OrderResult(ok=False, request_id=request_id, retcode="REJECTED",
                                     message="mock rejection", state="REJECTED")
                self._by_request[request_id] = result
                return result
            ticket = 1000 + len(self._orders) + 1
            volume = D(request["volume"])
            state = "FILLED"
            filled = volume
            if self.submit_mode == "partial":
                state, filled = "PARTIALLY_FILLED", (volume / 2).quantize(D("0.01"))
            self._positions.append(Position(
                ticket=ticket, symbol=request["symbol"], direction=request["direction"],
                volume=filled, price_open=D(request["price"]), sl=D(request.get("sl", 0)),
                tp=D(request.get("tp", 0)), magic=self.magic, comment=request.get("comment", ""),
            ))
            self._orders[ticket] = {"request_id": request_id, "state": state,
                                    "ticket": ticket, "volume": str(filled)}
            self._opened_at[ticket] = datetime.now(timezone.utc)
            result = OrderResult(ok=True, request_id=request_id, ticket=ticket,
                                 retcode="DONE", filled_volume=filled, state=state)
            self._by_request[request_id] = result
            if self.submit_mode == "timeout_then_fill":
                raise TimeoutError("mock broker timeout after fill")
            return result

    def order_status(self, ticket: int) -> str:
        order = self._orders.get(ticket)
        return order["state"] if order else "UNKNOWN"

    def order_by_request(self, request_id: str) -> OrderResult | None:
        return self._by_request.get(request_id)

    def positions(self) -> list[Position]:
        return list(self._positions)

    def close_position(self, ticket: int, pnl: str) -> None:
        for pos in list(self._positions):
            if pos.ticket == ticket:
                self._positions.remove(pos)
                opened = self._opened_at.get(ticket)
                closed = datetime.now(timezone.utc)
                self._closed.append(TradeRecord(
                    ticket=ticket, symbol=pos.symbol, direction=pos.direction,
                    volume=pos.volume, open_price=pos.price_open,
                    close_price=pos.price_open,  # placeholder for tests
                    open_time=opened or closed - timedelta(hours=1),
                    close_time=closed, pnl=D(pnl),
                    comment=pos.comment,
                ))
                return

    def closed_trades(self, days: int = 30) -> list[TradeRecord]:
        return list(self._closed)

    def bars(self, symbol: str, timeframe: str = "H1", count: int = 500) -> list[dict]:
        spec = self._specs.get(symbol)
        if spec is None:
            return []
        step = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
                "H1": 3600, "H4": 14400, "D1": 86400}.get(timeframe, 3600)
        rng = random.Random(f"{symbol}:{timeframe}:{count}")
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        price = spec["price"] * rng.uniform(0.97, 1.03)
        out: list[dict] = []
        for i in range(count):
            ts = end - timedelta(seconds=step * (count - 1 - i))
            wave = math.sin(i / 21.0) * 0.004 + math.sin(i / 5.0) * 0.0007
            open_ = price
            close = open_ * (1 + wave + rng.uniform(-0.0004, 0.0004))
            high = max(open_, close) * (1 + abs(rng.uniform(0, 0.0006)))
            low = min(open_, close) * (1 - abs(rng.uniform(0, 0.0006)))
            out.append({
                "time": int(ts.timestamp()), "open": round(open_, spec["digits"]),
                "high": round(high, spec["digits"]), "low": round(low, spec["digits"]),
                "close": round(close, spec["digits"]),
                "tick_volume": rng.randint(100, 5000),
                "spread": spec["spread"],
            })
            price = close
        return out


class MT5Broker:
    """Official MetaTrader5 integration against a verified demo account."""

    name = "mt5"
    kind = REAL_MT5
    magic = config.MAGIC
    # How far back order history is consulted when looking a request up. A filled order
    # whose position has since closed lives only here, so "absent from live positions,
    # live orders *and* this window" is the only evidence that justifies calling an order
    # never-executed. The mock advertises 0: it *is* the market, so its own record is
    # complete and no external history can exist.
    history_window_days = 30

    def __init__(self) -> None:
        self.mt5 = None
        self.connected = False
        self._request_map: dict[str, OrderResult] = {}

    def connect(self) -> "MT5Broker":
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise BrokerUnavailable(
                "MetaTrader5 package is not installed",
                hint="Install the official package on 64-bit Python: pip install MetaTrader5",
            ) from exc
        # Bounded, configurable wait: a dead IPC channel must fail in a controlled time.
        if not mt5.initialize(timeout=config.mt5_init_timeout_ms()):
            code, message = _last_error_tuple(mt5)
            raise BrokerUnavailable(f"MT5 initialize failed: ({code}, {message!r})",
                                    code=code, hint=mt5_error_hint(code))
        self.mt5 = mt5
        self.connected = True
        account = self.account()
        if not verify_demo(account):
            mt5.shutdown()
            self.mt5 = None
            self.connected = False
            raise BrokerUnavailable(
                "connected account is not a verified demo account",
                hint="Log into a DEMO account in the terminal; live and contest "
                     "accounts are refused by design.")
        return self

    def disconnect(self) -> None:
        if self.mt5:
            self.mt5.shutdown()
        self.mt5 = None
        self.connected = False

    def account(self) -> AccountSnapshot | None:
        info = self.mt5.account_info() if self.mt5 else None
        if info is None:
            return None
        terminal = self.mt5.terminal_info() if self.mt5 else None
        # Values come from the package's own constants, never from guessed integers.
        modes = {self.mt5.ACCOUNT_TRADE_MODE_DEMO: "DEMO",
                 self.mt5.ACCOUNT_TRADE_MODE_CONTEST: "CONTEST",
                 self.mt5.ACCOUNT_TRADE_MODE_REAL: "LIVE"}
        return AccountSnapshot(
            login=int(info.login), trade_mode=modes.get(int(info.trade_mode), "UNKNOWN"),
            equity=D(info.equity), balance=D(info.balance),
            margin_free=D(info.margin_free), currency=info.currency,
            leverage=int(info.leverage),
            trade_allowed=bool(getattr(info, "trade_allowed", False)),
            terminal_trade_allowed=bool(getattr(terminal, "trade_allowed", False)),
            margin_level=D(info.margin_level) if getattr(info, "margin_level", None) else None,
        )

    def symbols(self) -> list[str]:
        if not self.mt5:
            return []
        found = []
        for info in self.mt5.symbols_get() or []:
            if config.canonical_base(info.name):
                self.mt5.symbol_select(info.name, True)
                found.append(info.name)
        return found

    def instrument(self, symbol: str) -> Instrument | None:
        info = self.mt5.symbol_info(symbol) if self.mt5 else None
        if info is None:
            return None
        return Instrument(
            symbol=symbol, base=config.canonical_base(symbol) or symbol,
            contract_size=D(info.trade_contract_size), volume_min=D(info.volume_min),
            volume_max=D(info.volume_max), volume_step=D(info.volume_step),
            point=D(info.point), digits=int(info.digits),
            tick_size=D(info.trade_tick_size or info.point),
            tick_value=D(info.trade_tick_value), stops_level_points=int(info.trade_stops_level),
        )

    def quote(self, symbol: str) -> Quote | None:
        tick = self.mt5.symbol_info_tick(symbol) if self.mt5 else None
        if tick is None or not tick.ask or not tick.bid:
            return None
        return Quote(symbol=symbol, bid=D(tick.bid), ask=D(tick.ask),
                     ts_utc=datetime.fromtimestamp(tick.time_msc / 1000, tz=timezone.utc))

    def bars(self, symbol: str, timeframe: str = "H1", count: int = 500) -> list[dict]:
        if not self.mt5:
            return []
        # Looked up for the requested timeframe only: building the whole map eagerly
        # crashed on a package build that lacked one of the constants.
        tf = getattr(self.mt5, f"TIMEFRAME_{timeframe}", None)
        if tf is None:
            return []
        rates = self.mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None:
            # Not the same as "this symbol has no history": the call failed, and the
            # reason is the only thing that can tell the two apart.
            code, message = _last_error_tuple(self.mt5)
            db.log("broker", "bars_unavailable", {
                "symbol": symbol, "timeframe": timeframe, "code": code,
                "detail": message[:200]})
            return []
        # `spread` is in points, and a bar's own bid/ask is not in the package at all:
        # a bar has one price. A fabricated bid/ask pair derived from it used to be
        # attached here and would have been invented market data.
        return [{
            "time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "tick_volume": int(r["tick_volume"]), "spread": int(r["spread"]),
        } for r in rates]

    def calc_margin(self, symbol, direction, volume, price) -> Decimal | None:
        if not self.mt5:
            return None
        order_type = self.mt5.ORDER_TYPE_BUY if direction == "BUY" else self.mt5.ORDER_TYPE_SELL
        margin = self.mt5.order_calc_margin(order_type, symbol, float(volume), float(price))
        return D(margin) if margin is not None else None

    def check_order(self, request: dict) -> tuple[bool, str]:
        """order_check asks the server whether the request would be accepted.

        Success is retcode 0 (the MqlTradeCheckResult convention). TRADE_RETCODE_DONE
        (10009) is the order_send success code; some server builds echo it back on a
        check, so both are accepted. Everything else is a failure, and a failed check
        is never evidence that an order was placed.
        """
        if not self.mt5:
            return False, "broker is not connected"
        result = self.mt5.order_check(self._mt5_request(request))
        if result is None:
            return False, f"order_check returned nothing: {self.mt5.last_error()}"
        if int(result.retcode) in (0, self.mt5.TRADE_RETCODE_DONE):
            return True, result.comment or "ok"
        return False, f"check retcode {result.retcode}: {result.comment}"

    def _mt5_request(self, request: dict) -> dict:
        mt5 = self.mt5
        return {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": request["symbol"],
            "volume": float(request["volume"]),
            "type": mt5.ORDER_TYPE_BUY if request["direction"] == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": float(request["price"]), "sl": float(request.get("sl", 0)),
            "tp": float(request.get("tp", 0)),
            "deviation": int(request.get("deviation", 20)),
            "magic": self.magic,
            "comment": request_key(request["request_id"]),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

    def _classify_retcode(self, retcode: int) -> tuple[bool, str]:
        """Map an order_send return code to (accepted, canonical state).

        Three buckets only: accepted with a known state, definitely refused, or
        genuinely ambiguous (timeout/connection/requote) which must be reconciled.
        """
        mt5 = self.mt5
        if retcode == mt5.TRADE_RETCODE_DONE:
            return True, "FILLED"
        if retcode == mt5.TRADE_RETCODE_DONE_PARTIAL:
            return True, "PARTIALLY_FILLED"
        if retcode == mt5.TRADE_RETCODE_PLACED:
            return True, "PENDING"
        if retcode == mt5.TRADE_RETCODE_TIMEOUT:
            return False, "UNKNOWN"
        if retcode in (mt5.TRADE_RETCODE_CONNECTION, mt5.TRADE_RETCODE_TOO_MANY_REQUESTS,
                       mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED,
                       mt5.TRADE_RETCODE_PRICE_OFF, mt5.TRADE_RETCODE_ORDER_CHANGED):
            return False, "UNKNOWN"  # the server may or may not have acted
        if retcode in (mt5.TRADE_RETCODE_REJECT, mt5.TRADE_RETCODE_REJECT_CANCEL,
                       mt5.TRADE_RETCODE_CANCEL, mt5.TRADE_RETCODE_INVALID,
                       mt5.TRADE_RETCODE_INVALID_VOLUME, mt5.TRADE_RETCODE_INVALID_PRICE,
                       mt5.TRADE_RETCODE_INVALID_STOPS, mt5.TRADE_RETCODE_TRADE_DISABLED,
                       mt5.TRADE_RETCODE_MARKET_CLOSED, mt5.TRADE_RETCODE_NO_MONEY,
                       mt5.TRADE_RETCODE_CLIENT_DISABLES_AT, mt5.TRADE_RETCODE_SERVER_DISABLES_AT,
                       mt5.TRADE_RETCODE_LOCKED, mt5.TRADE_RETCODE_FROZEN):
            return False, "REJECTED"
        return False, "UNKNOWN"

    def _refuse(self, request_id: str, retcode: str, message: str) -> OrderResult:
        """Refuse inside the adapter, and leave a trace: a blocked direct call is
        evidence, not a no-op."""
        try:
            db.log("broker", "adapter_refused", {
                "request_id": request_id, "retcode": retcode, "reason": message})
        except Exception:  # noqa: BLE001 - the refusal stands even if the log cannot be written
            pass
        return OrderResult(ok=False, request_id=request_id, retcode=retcode,
                           message=message, state="REJECTED")

    def guard(self, request: dict) -> OrderResult | None:
        """Every precondition the adapter can verify on its own, or None to proceed.

        ExecutionService checks all of these too, but only for callers that route
        through it. This is the money boundary: anything holding this adapter is held
        to the same rules rather than trusted to have remembered them.
        """
        request_id = str(request.get("request_id") or "")
        if not config.DEMO_ONLY:
            return self._refuse(request_id, "LIVE_TRADING_DISABLED",
                                "live trading is not implemented (DEMO_ONLY is not set)")
        state = db.kill_switch_state()
        if state != "ARMED":
            return self._refuse(request_id, "KILL_SWITCH", f"kill switch is {state}")
        symbol = request.get("symbol") or ""
        if config.canonical_base(symbol) is None:
            return self._refuse(request_id, "NOT_WHITELISTED",
                                f"symbol {symbol} is not on the tradable whitelist")
        # An order that cannot be resolved yet must not be joined by another one.
        # This request's own row is excluded: the executor writes it SUBMISSION_PENDING
        # before calling the adapter, so counting it would refuse every order.
        from .execution import unresolved_orders      # local: execution imports this module
        pending = [rid for rid in unresolved_orders() if rid != request_id]
        if pending:
            return self._refuse(request_id, "UNRESOLVED_ORDERS",
                                f"{len(pending)} unresolved order(s) must be reconciled first")
        instrument = self.instrument(symbol)
        if instrument is None:
            return self._refuse(request_id, "NO_INSTRUMENT",
                                f"instrument {symbol} is unavailable")
        try:
            volume = D(str(request["volume"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            # ArithmeticError included on purpose: Decimal("lots") raises
            # ConversionSyntax, which is an ArithmeticError and not a ValueError, so
            # omitting it lets a malformed volume escape as a crash instead of a refusal.
            return self._refuse(request_id, "INVALID_VOLUME", "volume is not a number")
        if (volume < instrument.volume_min or volume > instrument.volume_max
                or volume % instrument.volume_step != 0):
            return self._refuse(
                request_id, "VOLUME_OUT_OF_BOUNDS",
                f"volume {volume} is outside {instrument.volume_min}-{instrument.volume_max} "
                f"step {instrument.volume_step}")
        try:
            deviation = int(request.get("deviation", config.DEFAULT_RISK_LIMITS["max_slippage_points"]))
        except (TypeError, ValueError):
            return self._refuse(request_id, "INVALID_DEVIATION", "deviation is not a number")
        cap = int(config.DEFAULT_RISK_LIMITS["max_slippage_points"])
        if deviation < 0 or deviation > cap:
            return self._refuse(request_id, "SLIPPAGE_TOO_WIDE",
                                f"deviation {deviation} exceeds the {cap}-point cap")
        quote = self.quote(symbol)
        if quote is None:
            return self._refuse(request_id, "NO_QUOTE", f"no quote for {symbol}")
        reference = quote.ask if request.get("direction") == "BUY" else quote.bid
        try:
            drift = abs(D(str(request["price"])) - reference) / instrument.point
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return self._refuse(request_id, "INVALID_PRICE", "price is not a number")
        if drift > cap:
            return self._refuse(request_id, "PRICE_TOO_FAR",
                                f"price is {drift:.0f} points from the quote, cap is {cap}")
        return None

    def submit(self, request: dict) -> OrderResult:
        mt5 = self.mt5
        if not self.mt5:
            return OrderResult(ok=False, request_id=request["request_id"],
                               retcode="OFFLINE", message="broker is not connected",
                               state="UNKNOWN")
        refusal = self.guard(request)
        if refusal is not None:
            return refusal
        if not verify_demo(self.account()):
            return OrderResult(ok=False, request_id=request["request_id"],
                               retcode="NOT_DEMO", message="demo verification failed",
                               state="REJECTED")
        ok, message = self.check_order(request)
        if not ok:
            return OrderResult(ok=False, request_id=request["request_id"],
                               retcode="CHECK_FAILED", message=message, state="REJECTED")
        # The switch can move while verify_demo and check_order round-trip to the
        # terminal, so it is read once more as the last thing before the send. The
        # window is now the single order_send call itself, which cannot be closed from
        # this side: a terminal cannot be asked to check-and-send atomically.
        state = db.kill_switch_state()
        if state != "ARMED":
            return self._refuse(request["request_id"], "KILL_SWITCH",
                                f"kill switch changed to {state} during submission")
        sent = mt5.order_send(self._mt5_request(request))
        if sent is None:
            result = OrderResult(ok=False, request_id=request["request_id"],
                                 retcode="NO_RESPONSE", message=str(mt5.last_error()),
                                 state="UNKNOWN")
        else:
            accepted, state = self._classify_retcode(int(sent.retcode))
            # sent.volume is the volume the server acted on; it is only evidence of a
            # fill when the retcode says the order actually completed. For anything
            # else the executor must reconcile against positions/deals.
            filled = D(getattr(sent, "volume", 0)) if state == "FILLED" else D("0")
            result = OrderResult(
                ok=accepted, request_id=request["request_id"], ticket=int(sent.order or 0),
                retcode=int(sent.retcode), message=sent.comment or "",
                filled_volume=filled, state=state,
            )
        self._request_map[request["request_id"]] = result
        if result.state == "FILLED":
            # A fill that beat a pause the operator pressed during the send cannot be
            # undone here, but it must never be silent. The switch is not touched: it is
            # already PAUSED or stopped, and downgrading EMERGENCY_STOP would weaken it.
            after = db.kill_switch_state()
            if after != "ARMED":
                db.log("broker", "alert", {
                    "kind": "filled_after_pause", "request_id": request["request_id"],
                    "ticket": result.ticket, "kill_switch": after})
        return result

    def order_state(self, ticket: int) -> str:
        """Canonical state for a ticket, using the package's ORDER_STATE_* constants."""
        mt5 = self.mt5
        if not mt5 or not ticket:
            return "UNKNOWN"
        if mt5.positions_get(ticket=ticket):
            return "FILLED"
        live = mt5.orders_get(ticket=ticket)
        if live:
            return self._state_from_code(int(live[0].state))
        history = mt5.history_orders_get(ticket=ticket)
        if history:
            return self._state_from_code(int(history[0].state))
        return "UNKNOWN"

    def _state_from_code(self, code: int) -> str:
        mt5 = self.mt5
        return {
            mt5.ORDER_STATE_STARTED: "STARTED",
            mt5.ORDER_STATE_PLACED: "PENDING",
            mt5.ORDER_STATE_PARTIAL: "PARTIALLY_FILLED",
            mt5.ORDER_STATE_FILLED: "FILLED",
            mt5.ORDER_STATE_CANCELED: "CANCELED",
            mt5.ORDER_STATE_REJECTED: "REJECTED",
            mt5.ORDER_STATE_EXPIRED: "EXPIRED",
            mt5.ORDER_STATE_REQUEST_ADD: "STARTED",
            mt5.ORDER_STATE_REQUEST_MODIFY: "STARTED",
            mt5.ORDER_STATE_REQUEST_CANCEL: "STARTED",
        }.get(code, "UNKNOWN")

    def order_status(self, ticket: int) -> str:
        return self.order_state(ticket)

    def order_by_request(self, request_id: str, since: datetime | None = None) -> OrderResult | None:
        """Find the order a request id produced, or None if the broker has no trace.

        Four sources, in order: this process's own send map, live positions, live
        pending orders, and finally order *history* — an order that filled and whose
        position has since closed appears nowhere else, so omitting history makes a
        genuinely executed order look like one that was never sent.

        None means "the terminal answered and has no such order". A terminal that
        could not answer raises HistoryUnavailable instead: the caller must never be
        able to read an API failure as an absence.
        """
        if request_id in self._request_map:
            return self._request_map[request_id]
        needle = request_key(request_id)
        if not self.mt5:
            return None
        for pos in self.mt5.positions_get() or []:
            if pos.comment == needle:
                return OrderResult(ok=True, request_id=request_id, ticket=int(pos.ticket),
                                   retcode="RECONCILED", state="FILLED",
                                   filled_volume=D(pos.volume))
        for order in self.mt5.orders_get() or []:
            if order.comment == needle:
                return OrderResult(ok=True, request_id=request_id, ticket=int(order.ticket),
                                   retcode="RECONCILED", state="PENDING")
        start = since or (datetime.now(timezone.utc)
                          - timedelta(days=self.history_window_days))
        try:
            history = self.mt5.history_orders_get(start, datetime.now(timezone.utc))
        except TypeError:
            # A package/fake whose history_orders_get takes no window. The call is still
            # made, so an empty answer is still "the terminal has no such order".
            history = self.mt5.history_orders_get()
        if history is None:
            # The package's error convention: None is both "no orders" and "the call
            # failed", and only last_error() tells them apart. Treating this as absence
            # is how an order that did execute gets called "never executed".
            code, message = _last_error_tuple(self.mt5)
            raise HistoryUnavailable(
                f"order history for {request_id} is unavailable: ({code}, {message!r})")
        for order in history or []:
            if (getattr(order, "comment", "") or "") == needle:
                state = self._state_from_code(int(order.state))
                # A history order that is not FILLED never became a position: report the
                # terminal's own verdict rather than a fill.
                return OrderResult(
                    ok=state == "FILLED", request_id=request_id, ticket=int(order.ticket),
                    retcode="RECONCILED", state=state,
                    filled_volume=D(getattr(order, "volume_initial", 0) or 0)
                    if state == "FILLED" else D("0"))
        return None

    def positions(self) -> list[Position]:
        if not self.mt5:
            return []
        out = []
        for p in self.mt5.positions_get() or []:
            if int(p.magic) != self.magic:
                continue
            out.append(Position(
                ticket=int(p.ticket), symbol=p.symbol,
                direction="BUY" if p.type == self.mt5.POSITION_TYPE_BUY else "SELL",
                volume=D(p.volume), price_open=D(p.price_open), sl=D(p.sl), tp=D(p.tp),
                profit=D(p.profit), magic=int(p.magic), comment=p.comment or "",
            ))
        return out

    def closed_trades(self, days: int = 30) -> list[TradeRecord]:
        """Closed positions, rebuilt from deal history grouped by position id.

        A closing (OUT) deal carries only the exit price, so the matching entry (IN)
        deal of the same position_id supplies direction, open price and open time.
        """
        if not self.mt5:
            return []
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        deals = self.mt5.history_deals_get(start, end) or []
        entries: dict[int, list] = {}
        for deal in deals:
            if int(deal.magic) != self.magic:
                continue
            entries.setdefault(int(deal.position_id), []).append(deal)
        out = []
        for position_id, group in entries.items():
            ins = [d for d in group if int(d.entry) == self.mt5.DEAL_ENTRY_IN]
            outs = [d for d in group if int(d.entry) == self.mt5.DEAL_ENTRY_OUT]
            if not outs:
                continue  # still open, or closed outside this window
            first_in, last_out = (ins[0] if ins else None), outs[-1]
            # The closing deal's type is the direction of the closing order, which is
            # the opposite of the position; prefer the entry deal when it is present.
            source = first_in or last_out
            direction = "BUY" if source.type == self.mt5.DEAL_TYPE_BUY else "SELL"
            out.append(TradeRecord(
                # The position's ticket is its position_id; the OUT deal has a ticket of
                # its own, and reconciliation compares against position tickets.
                ticket=int(position_id), symbol=last_out.symbol, direction=direction,
                volume=D(last_out.volume),
                open_price=D(first_in.price if first_in else last_out.price),
                close_price=D(last_out.price),
                open_time=datetime.fromtimestamp((first_in or last_out).time, tz=timezone.utc),
                close_time=datetime.fromtimestamp(last_out.time, tz=timezone.utc),
                pnl=D(sum(d.profit for d in outs)),
                commission=D(sum(d.commission for d in group)),
                swap=D(sum(d.swap for d in group)),
                comment=last_out.comment or "",
            ))
        return out