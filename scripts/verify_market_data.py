"""One-command market-data verification for every configured instrument.

Read-only. Run it during a live session to complete the check that a closed market
cannot give you; run it any time to confirm the offline half.

    python scripts/verify_market_data.py [--bars 500]

It verifies, per instrument and for the terminal as a whole:

  * the symbol resolves to the broker's own contract (and a look-alike is not used)
  * instrument metadata is complete and usable
  * the live tick exists, has a sane bid/ask, and is fresh *for the current session*
  * the broker's spread in points matches (ask - bid) / point
  * candle history arrives, is ordered, has no duplicates or impossible OHLC, and its
    gaps are explained by market closures rather than holes
  * the newest candle is the still-forming one, not a missing one
  * symbols the desk is not configured for are still resolvable-checked (refused)
  * the session can be dropped and re-established

Exit code 0 when every check passes or is legitimately inapplicable; 1 when something
is actually wrong. Old quotes outside desk hours may report CLOSED. Fresh quotes can
report HEALTHY outside those hours, while execution still requires an open desk session.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, data  # noqa: E402

FRESH_SECONDS = config.DEFAULT_RISK_LIMITS["max_quote_age_s"]


class Checks:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []
        self.blocking: list[str] = []

    def check(self, ok: bool, name: str, detail: str = "", blocking: bool = True) -> bool:
        self.results.append((name, bool(ok), detail))
        if not ok and blocking:
            self.blocking.append(f"{name}: {detail}")
        return bool(ok)

    def skip(self, name: str, detail: str) -> None:
        self.results.append((name, True, f"SKIP — {detail}"))


def verify_terminal(broker, checks: Checks) -> bool:
    checks.check(getattr(broker, "kind", None) == "REAL_MT5",
                 "broker is a real MT5 session",
                 f"kind={getattr(broker, 'kind', None)} "
                 f"({type(broker).__name__}; the simulator cannot answer live questions)")
    account = broker.account()
    checks.check(account is not None, "account snapshot is readable")
    if account is None:
        return False
    checks.check(account.trade_mode == "DEMO",
                 "account is a DEMO account", f"mode={account.trade_mode}")
    from app.broker import can_arm
    armable, why = can_arm(broker, account)
    print(f"  arm gate: {'can arm' if armable else 'cannot arm'} — {why}")
    return True


def verify_symbol(broker, base: str, symbol: str, bars: int, checks: Checks,
                  session_open: bool) -> None:
    from app.broker import resolve_symbol, symbol_report
    resolved, via = resolve_symbol(broker, base)
    checks.check(resolved == symbol, f"{base}: resolves to {symbol}", f"got {resolved} ({via})")
    report = symbol_report(broker, symbol)
    checks.check(not report["issues"], f"{base}: metadata and quote are usable",
                 "; ".join(report["issues"]))
    quote = report["quote"]
    if quote is None:
        checks.skip(f"{base}: tick freshness", "no quote to age")
    else:
        age = float(quote["age_s"])
        if age <= FRESH_SECONDS or session_open:
            # A fresh tick is a fresh tick whenever it happens; an old tick is only a
            # fault when the market is supposed to be trading.
            checks.check(age <= FRESH_SECONDS, f"{base}: tick is fresh ({age:.1f}s)",
                         f"older than {FRESH_SECONDS}s while the session is open")
        else:
            # An old tick outside the session is the market being closed, which the
            # health gate reports as CLOSED. It is not a failure and not "healthy".
            checks.skip(f"{base}: tick freshness ({age / 3600:.1f}h old)",
                        "market closed, so freshness cannot be judged")
    if quote is not None:
        instrument = broker.instrument(symbol)
        expected = (Decimal(quote["ask"]) - Decimal(quote["bid"])) / instrument.point
        checks.check(abs(expected - Decimal(quote["spread_points"])) < Decimal("0.01"),
                     f"{base}: spread in points matches ask-bid",
                     f"computed {expected}, broker reports {quote['spread_points']}")
        checks.check(Decimal(quote["ask"]) >= Decimal(quote["bid"]),
                     f"{base}: ask is not below bid")
    history = broker.bars(symbol, "M1", bars)
    if not checks.check(bool(history), f"{base}: candle history is returned"):
        return
    quality = data.bars_quality(history, "M1")
    checks.check(not quality["issues"], f"{base}: candles are complete and ordered",
                 "; ".join(quality["issues"][:3]))
    stamps = [int(bar["time"]) for bar in history]
    checks.check(stamps == sorted(stamps), f"{base}: candle timestamps are ordered")
    last = datetime.fromtimestamp(stamps[-1], tz=timezone.utc)
    newest_allowed = datetime.now(timezone.utc).timestamp() - last.timestamp()
    checks.check(newest_allowed < 3 * 86400,
                 f"{base}: history reaches the present",
                 f"newest candle is {newest_allowed / 3600:.1f}h old")
    if session_open:
        forming = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        checks.check(last >= forming.replace(minute=forming.minute),
                     f"{base}: newest candle is the forming one",
                     f"newest {last.isoformat()} looks like a closed candle while the "
                     "session is open (a gap, not a forming bar)")
    else:
        checks.skip(f"{base}: forming-candle check",
                    f"market closed; newest candle {last.isoformat()}")
    if quality["closures"]:
        print(f"    {base}: {len(quality['closures'])} expected market closure(s) in "
              f"{quality['count']} candles")


def verify_unconfigured_symbols(broker, checks: Checks) -> None:
    from app.broker import resolve_symbol
    for name in ("USDCNH", "NOTASYMBOL"):
        resolved, via = resolve_symbol(broker, name)
        checks.check(resolved is None, f"{name}: refused (not tradable here)",
                     f"resolved to {resolved} ({via})")


def verify_reconnect(checks: Checks) -> None:
    """Drop the session and establish it again; a quote must still be readable."""
    from app.broker import MT5Broker
    session = None
    try:
        session = MT5Broker().connect()
        first = session.quote("EURUSDm")
        session.disconnect()
        session = MT5Broker().connect()
        second = session.quote("EURUSDm")
        checks.check(first is not None and second is not None,
                     "session can be dropped and re-established",
                     "a quote was not readable after reconnecting")
    except Exception as exc:  # noqa: BLE001 - reported as a failure, not a crash
        checks.check(False, "session can be dropped and re-established",
                     f"{type(exc).__name__}: {exc}")
    finally:
        if session is not None:
            session.disconnect()


def main(argv: list[str]) -> int:
    config.load_dotenv()
    bars = 500
    if "--bars" in argv:
        bars = max(10, min(int(argv[argv.index("--bars") + 1]), 50000))
    checks = Checks()
    now = datetime.now(timezone.utc)
    session_open = config.session_open(now)
    print(f"Market-data verification — {now.isoformat()}")
    print(f"Desk session open now: {session_open} "
          f"(Mon-Fri 00:00-21:00 UTC); {bars} M1 candles per instrument\n")

    from app.broker import connect_from_env, resolved_symbols
    broker = connect_from_env()
    try:
        print("Terminal")
        if not verify_terminal(broker, checks):
            print("\nNo account snapshot: the rest of the checks cannot run.")
            return 1
        mapping = resolved_symbols(broker)
        checks.check(bool(mapping), "the whitelist resolves to broker instruments",
                     "no instrument resolved")
        print(f"\nInstruments ({len(mapping)} resolved)")
        for base, symbol in mapping.items():
            verify_symbol(broker, base, symbol, bars, checks, session_open)
        print("\nRefusals")
        verify_unconfigured_symbols(broker, checks)
        print("\nData health gate")
        health, detail = data.market_health(broker)
        print(f"  {health} — {detail.get('reason', 'session open, quotes fresh')}")
        if getattr(broker, "kind", None) == "REAL_MT5":
            expected = "HEALTHY" if session_open else "HEALTHY, CLOSED, or UNKNOWN"
            checks.check(health == "HEALTHY"
                         or (not session_open and health in ("CLOSED", "UNKNOWN")),
                         f"health gate reports {expected} at this hour",
                         f"reported {health}")
        else:
            # The simulator always quotes "now", so its verdict says nothing about the
            # terminal. Reporting one failure with its cause beats three derived ones.
            checks.skip("health gate verdict",
                        "no real MT5 session to judge it against")
    finally:
        broker.disconnect()

    print("\nReconnection")
    verify_reconnect(checks)

    passed = sum(1 for _, ok, _ in checks.results if ok)
    print(f"\n{passed}/{len(checks.results)} checks passed")
    for name, ok, detail in checks.results:
        if not ok:
            print(f"  FAIL {name}: {detail}")
    if not session_open:
        print("\nThe desk session is closed. Some tick or candle checks may be inapplicable; "
              "re-run during an open session for full live validation. "
              "Nothing here waits for the market to open.")
    for name, ok, detail in checks.results:
        if ok and detail.startswith("SKIP"):
            print(f"  skip {name}: {detail[7:]}")
    return 1 if checks.blocking else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
