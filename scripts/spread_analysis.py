"""Read-only, instrument-specific spread analysis.

Answers, with measurements rather than assumptions:

  * do the desk's spread limits use the same units the broker quotes in?
  * what spread does each instrument actually show, per market session?
  * which configured limits are satisfiable, which are too tight, and what would be a
    defensible value?

It changes nothing: no limit is written, no order is sent, and the project database is
only read. Recommendations are printed for the owner to accept through the authenticated
settings endpoint.

    python scripts/spread_analysis.py [--bars 20000] [--json]

`--bars` is the M1 history depth per instrument (the terminal rejected 100000 in testing
and served 50000; 20000 is about two weeks of trading hours).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

# UTC session bands, used only to group observations. The desk's own tradable window is
# Mon-Fri 00:00-21:00 UTC (config.SESSION_WINDOWS_UTC).
SESSIONS = (
    ("sydney/tokyo", 0, 7),
    ("london", 7, 12),
    ("london/ny overlap", 12, 16),
    ("new york", 16, 21),
    ("off-session", 21, 24),
)


def _percentile(sorted_values: list[int], fraction: float) -> int:
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1, int(round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def _stats(spreads: list[int]) -> dict:
    ordered = sorted(spreads)
    return {
        "observations": len(ordered),
        "min": ordered[0] if ordered else 0,
        "p50": _percentile(ordered, 0.50),
        "p90": _percentile(ordered, 0.90),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1] if ordered else 0,
    }


def _limits() -> dict:
    try:
        from app import risk
        return risk.load_limits()["max_spread_points"]
    except Exception:  # noqa: BLE001 - the analysis must run before a database exists
        return config.DEFAULT_RISK_LIMITS["max_spread_points"]


def _money_per_lot(spread_points: int, instrument) -> Decimal:
    """What a spread costs on one lot, in the account currency."""
    if instrument.tick_size <= 0:
        return Decimal("0")
    ticks = (Decimal(spread_points) * instrument.point) / instrument.tick_size
    return (ticks * instrument.tick_value).quantize(Decimal("0.01"))


def analyse(broker, bars: int) -> dict:
    from app.broker import resolved_symbols, symbol_report
    limits = _limits()
    mapping = resolved_symbols(broker)
    out = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
           "bars_requested": bars, "limits": dict(limits), "instruments": {}}
    for base, symbol in mapping.items():
        instrument = broker.instrument(symbol)
        report = symbol_report(broker, symbol)
        limit = limits.get(base, limits.get("default"))
        entry: dict = {"symbol": symbol, "limit_points": limit,
                       "metadata": None, "live_tick": report["quote"],
                       "unit_check": None, "sessions": {}, "verdict": "",
                       "recommended_points": None}
        if instrument is not None:
            entry["metadata"] = {
                "digits": instrument.digits, "point": str(instrument.point),
                "tick_size": str(instrument.tick_size), "tick_value": str(instrument.tick_value),
                "contract_size": str(instrument.contract_size),
                "volume_min": str(instrument.volume_min), "volume_step": str(instrument.volume_step),
            }
        # The unit check: does (ask - bid) / point reproduce the broker's own spread
        # figure for this tick? If not, the limits are being compared in the wrong unit.
        if report["quote"] and instrument is not None and instrument.point > 0:
            price_diff = Decimal(report["quote"]["ask"]) - Decimal(report["quote"]["bid"])
            entry["unit_check"] = {
                "ask_minus_bid": str(price_diff),
                "in_points": str((price_diff / instrument.point).normalize()),
                "reported_spread_points": report["quote"]["spread_points"],
            }
        history = broker.bars(symbol, "M1", bars)
        by_session: dict[str, list[int]] = {name: [] for name, _, _ in SESSIONS}
        for bar in history:
            hour = datetime.fromtimestamp(int(bar["time"]), tz=timezone.utc).hour
            for name, start, end in SESSIONS:
                if start <= hour < end:
                    by_session[name].append(int(bar["spread"]))
                    break
        for name, _, _ in SESSIONS:
            entry["sessions"][name] = _stats(by_session[name])
        all_spreads = [int(bar["spread"]) for bar in history]
        entry["sessions"]["all"] = _stats(all_spreads)
        overall = entry["sessions"]["all"]
        if history and instrument is not None:
            entry["cost_per_lot_at_p50"] = str(_money_per_lot(overall["p50"], instrument))
            entry["cost_per_lot_at_limit"] = str(_money_per_lot(int(limit), instrument))
        if not history:
            entry["verdict"] = "NO DATA: the terminal returned no M1 history for this symbol"
        elif overall["p50"] > limit:
            entry["verdict"] = (f"TOO TIGHT: the median spread ({overall['p50']}) already "
                                f"exceeds the limit ({limit}); this instrument cannot trade")
        elif overall["p95"] > limit:
            entry["verdict"] = (f"BORDERLINE: {overall['p95']} points at the 95th percentile "
                                f"exceeds the limit ({limit}); most of the time it passes")
        else:
            entry["verdict"] = (f"SATISFIABLE: the 95th percentile ({overall['p95']}) is "
                                f"inside the limit ({limit})")
        # A recommendation only: p95 across the full sample with headroom for a quiet
        # session's occasional widening. Never applied by this script.
        if history:
            entry["recommended_points"] = int(round(overall["p95"] * 1.25) + 1)
        out["instruments"][base] = entry
    return out


def report(data: dict) -> None:
    print(f"Spread analysis (read-only) — generated {data['generated_at_utc']}")
    print(f"M1 bars requested per instrument: {data['bars_requested']}")
    print(f"Configured limits (points): {data['limits']}\n")
    for base, entry in data["instruments"].items():
        overall = entry["sessions"]["all"]
        print(f"=== {base} ({entry['symbol']}) — limit {entry['limit_points']} points")
        if entry["metadata"]:
            meta = entry["metadata"]
            print(f"    digits={meta['digits']} point={meta['point']} "
                  f"tick_size={meta['tick_size']} tick_value={meta['tick_value']} "
                  f"contract={meta['contract_size']}")
        if entry["unit_check"]:
            check = entry["unit_check"]
            print(f"    unit check: ask-bid={check['ask_minus_bid']} = "
                  f"{check['in_points']} points (broker reports "
                  f"{check['reported_spread_points']})")
        print(f"    {'session':<20} {'n':>6} {'min':>5} {'p50':>5} {'p90':>5} "
              f"{'p95':>5} {'p99':>5} {'max':>5}")
        for name, _, _ in SESSIONS:
            stats = entry["sessions"][name]
            if not stats["observations"]:
                continue
            print(f"    {name:<20} {stats['observations']:>6} {stats['min']:>5} "
                  f"{stats['p50']:>5} {stats['p90']:>5} {stats['p95']:>5} "
                  f"{stats['p99']:>5} {stats['max']:>5}")
        print(f"    {'ALL':<20} {overall['observations']:>6} {overall['min']:>5} "
              f"{overall['p50']:>5} {overall['p90']:>5} {overall['p95']:>5} "
              f"{overall['p99']:>5} {overall['max']:>5}")
        if entry.get("cost_per_lot_at_p50"):
            print(f"    cost on 1 lot: {entry['cost_per_lot_at_p50']} at p50, "
                  f"{entry['cost_per_lot_at_limit']} at the limit")
        print(f"    verdict: {entry['verdict']}")
        if entry["recommended_points"]:
            print(f"    recommendation (owner decision, not applied): "
                  f"{entry['recommended_points']} points")
        print("    live tick:", "no quote" if not entry["live_tick"]
              else f"bid={entry['live_tick']['bid']} ask={entry['live_tick']['ask']} "
                   f"spread={entry['live_tick']['spread_points']} points, "
                   f"age {entry['live_tick']['age_s']}s")
        print()
    print("No limit was changed by this analysis. The desk keeps refusing trades whose "
          "spread exceeds the configured maximum.")


def main(argv: list[str]) -> int:
    config.load_dotenv()
    bars = 20000
    if "--bars" in argv:
        bars = max(100, min(int(argv[argv.index("--bars") + 1]), 50000))
    from app.broker import connect_from_env
    broker = connect_from_env()
    try:
        data = analyse(broker, bars)
    finally:
        broker.disconnect()
    if "--json" in argv:
        print(json.dumps(data, indent=2, default=str))
    else:
        report(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
