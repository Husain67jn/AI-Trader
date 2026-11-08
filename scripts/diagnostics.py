"""Read-only diagnostics. Never prints secrets; safe to paste in a bug report.

Everything here goes through the application's own broker adapter, so the output is
evidence about the adapter rather than about a loose MetaTrader5 session. One adapter
session is opened and reused; no order is ever sent.

Run: python scripts/diagnostics.py
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402


def _initialised() -> bool:
    try:
        return bool(db.query("SELECT name FROM sqlite_master WHERE type='table' "
                             "AND name='orders'"))
    except Exception:  # noqa: BLE001
        return False


def _report_symbols(broker) -> list[str]:
    from app.broker import resolve_symbol, resolved_symbols, symbol_report
    try:
        from app import risk
        limits = risk.load_limits()["max_spread_points"]
    except Exception:  # noqa: BLE001 - diagnostics may run before the database exists
        limits = config.DEFAULT_RISK_LIMITS["max_spread_points"]
    mapping = resolved_symbols(broker)
    print(f"Symbols                : {len(mapping)} whitelisted instrument(s) resolved")
    for base, symbol in mapping.items():
        _, via = resolve_symbol(broker, base)
        report = symbol_report(broker, symbol)
        quote, issues = report["quote"], report["issues"]
        age = f"{quote['age_s']}s" if quote else "-"
        spread = quote["spread_points"] if quote and quote["spread_points"] else "-"
        limit = limits.get(base, limits.get("default"))
        # The limit is the owner's policy, in the broker's own points. Reported here so
        # a limit that this broker can never satisfy is visible before it silently
        # rejects every proposal for that instrument.
        verdict = "OK" if not issues else "ISSUES: " + "; ".join(issues)
        try:
            if quote and quote["spread_points"] and float(spread) > float(limit):
                verdict += f" | spread {spread} exceeds the configured limit {limit}"
        except (TypeError, ValueError):
            pass
        print(f"  {base:<7} -> {symbol:<12} ({via})  spread={spread:<8}/{limit:<6} "
              f"tick age={age:<10} {verdict}")
    return list(mapping.values())


def _report_history(broker, symbols: list[str]) -> None:
    from app import data
    print("Historical candles     : H1, 200 requested per symbol")
    for symbol in symbols:
        bars = broker.bars(symbol, "H1", 200)
        quality = data.bars_quality(bars, "H1")
        if bars:
            age = round(datetime.now(timezone.utc).timestamp() - bars[-1]["time"], 1)
            last = f"last bar age {age}s"
        else:
            last = "no bars returned"
        verdict = "OK" if quality["ok"] else "ISSUES: " + "; ".join(quality["issues"][:3])
        if quality["closures"]:
            # Expected closures are named, so a weekend is not mistaken for a data fault.
            verdict += f" ({len(quality['closures'])} expected market closure(s))"
        print(f"  {symbol:<12} bars={quality['count']:<5} {last:<22} {verdict}")


def main() -> int:
    config.load_dotenv()
    print(f"DEMO_ONLY              : {config.DEMO_ONLY}")
    print(f"BROKER setting         : {os.getenv('TRADER_BROKER', 'auto')}")
    print(f"Claude credential      : {'set' if (os.getenv('ANTHROPIC_API_KEY') or os.getenv('ANTHROPIC_AUTH_TOKEN')) else 'absent -> mock provider'}")
    print(f"LLM provider override  : {os.getenv('TRADER_LLM_PROVIDER') or 'auto'}")
    print(f"Autonomous paid calls  : {'allowed' if os.getenv('TRADER_ALLOW_PAID_AUTORESEARCH') == '1' else 'refused (TRADER_ALLOW_PAID_AUTORESEARCH unset)'}")
    print(f"Owner password in env  : {'set' if os.getenv('TRADER_OWNER_PASSWORD') else 'absent (generated at first run)'}")
    print(f"Data dir / DB          : {config.DATA_DIR} / {config.DB_PATH}")

    if not db.ping():
        print("DATABASE               : UNREACHABLE — execution is blocked (fail closed)")
        return 1
    print("DATABASE               : OK")

    broker = None
    try:
        from app import data
        from app.broker import MT5Broker, can_arm, connect_from_env
        broker = connect_from_env()
        acct = broker.account()
        print(f"Broker                 : {type(broker).__name__} [{getattr(broker, 'kind', 'UNKNOWN')}]  "
              f"mode={acct.trade_mode if acct else None}  "
              f"balance={acct.balance if acct else None} {acct.currency if acct else ''}")
        # The account's DEMO flag alone is not the gate: a research-only broker can
        # report a demo account and still never be allowed to send an order.
        armable, why = can_arm(broker, acct)
        print(f"Can arm (real gate)    : {armable}{'' if armable else ': ' + why}")
        health, detail = data.market_health(broker)
        extra = ""
        if detail.get("quotes"):
            extra = (f"  (session open: {detail.get('session_open')}, oldest tick age "
                     f"{detail.get('oldest_tick_age_s')} s)")
        print(f"Data health            : {health}{extra}")
        if detail.get("reason"):
            print(f"                         reason: {detail['reason']}")
        # connect_from_env() may have fallen back to the simulator. Say which it was and
        # why, instead of opening a second MT5 session to ask the same question.
        if isinstance(broker, MT5Broker):
            print("MT5 session            : connected (the session used above)")
        else:
            err = getattr(broker, "mt5_error", None) or {}
            print(f"MT5 session            : UNAVAILABLE (code {err.get('code')}, "
                  f"{(err.get('message') or 'no session')[:120]})")
            if err.get("hint"):
                print(f"                         {err['hint']}")
        _report_history(broker, _report_symbols(broker))
    except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
        print(f"Broker                 : UNAVAILABLE ({type(exc).__name__}: {exc})")
    finally:
        if broker is not None:
            try:
                broker.disconnect()
            except Exception:  # noqa: BLE001
                pass

    if not _initialised():
        print("Kill switch            : PAUSED (database not initialised yet; start the server once)")
        return 0
    print(f"Kill switch            : {db.kill_switch_state()}")
    states = {r["state"]: r["n"] for r in
              db.query("SELECT state, COUNT(*) AS n FROM orders GROUP BY state")}
    closed = db.query("SELECT ticket FROM trades WHERE close_time <> ''")
    reviewed = {r["ticket"] for r in db.query("SELECT ticket FROM reviews")}
    print(f"Orders by state        : {states or 'none'}")
    print(f"Closed trades          : {len(closed)}")
    print(f"Awaiting review        : {len([r for r in closed if r['ticket'] not in reviewed])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
