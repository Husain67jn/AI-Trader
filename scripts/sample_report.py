"""Generate a clearly-labelled SYNTHETIC demo report.

No market data is used and no profitability is claimed. This exercises the
backtester and writes docs/SAMPLE_DEMO_REPORT.md.

Run: python scripts/sample_report.py
"""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import backtest  # noqa: E402


def synthetic_bars(count: int = 1200, seed: int = 7) -> list[dict]:
    """Random-walk bars, H1. Deterministic seed so the report is reproducible."""
    rng = random.Random(seed)
    price = 2000.0                      # XAUUSD-like level
    bars = []
    for i in range(count):
        open_ = price
        close = open_ * (1 + rng.gauss(0, 0.0012))
        wick = rng.uniform(0, 0.0004)
        bars.append({"time": i, "open": open_,
                     "high": max(open_, close) * (1 + wick),
                     "low": min(open_, close) * (1 - wick),
                     "close": close,
                     "tick_volume": rng.randint(500, 5000),
                     "spread": rng.choice([2, 3, 4])})
        price = close
    return bars


def _row(label, value):
    return f"| {label} | {value} |"


def main() -> int:
    bars = synthetic_bars()
    single = backtest.run_backtest(bars)
    wf = backtest.walk_forward(bars, folds=3)
    m = single["metrics"]

    lines = [
        "# Sample Demo Report — SYNTHETIC DATA", "",
        "> **This report is generated from a random walk, not from market data.** It exists to",
        "> show the shape of the learning/backtest output and to prove the backtester runs end",
        "> to end. It is **not** evidence of an edge and **no profitability is claimed**.",
        "> Regenerate with `python scripts/sample_report.py`.", "",
        "## Deterministic run configuration", "",
        "| Item | Value |", "|---|---|",
        _row("Broker", "none (offline synthetic bars)"),
        _row("Instrument profile", "gold-like (`XAUUSD` spec)"),
        _row("Bars", f"{len(bars)} H1 bars, seeded random walk"),
        _row("Strategy", "Donchian breakout, ATR stop/target, next-bar entry"),
        _row("Costs", "spread + slippage + commission from the spec"),
        _row("Live trading", "DISABLED — DEMO_ONLY, kill switch PAUSED"), "",
        "## Single-pass metrics (synthetic)", "",
        "| Metric | Value |", "|---|---|",
    ]
    for k, v in m.items():
        lines.append(_row(k.replace("_", " "), v if v is not None else "—"))

    lines += ["", "## Walk-forward (synthetic)", ""]
    if wf.get("folds"):
        lines += ["| Fold | Train trades | Train net % | Test trades | Test net % |",
                  "|---|---|---|---|---|"]
        for f in wf["folds"]:
            tr, te = f.get("train_metrics", {}), f.get("test_metrics", {})
            lines.append(f"| {f.get('fold')} | {tr.get('trades')} | {tr.get('net_return_pct')} "
                         f"| {te.get('trades')} | {te.get('net_return_pct')} |")
        lines += ["", "Out-of-sample results are the ones that matter; folds with few trades",
                  "are noise. Parameter selection on the training window is a known bias."]
    else:
        lines.append(f"No folds: {wf.get('note')}")

    lines += ["", "## What this does not show", "",
              "- Real slippage, gaps, weekend risk, or broker-specific execution.",
              "- Any Claude analysis — the LLM is not in the backtest loop.",
              "- Statistical significance. One model, one synthetic series, one seed.",
              "",
              "Do not size positions from this report."]

    out = ROOT / "docs" / "SAMPLE_DEMO_REPORT.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out} (synthetic; {m.get('trades')} trades)")
    return 0


if __name__ == "__main__":
    assert len(synthetic_bars(count=50)) == 50
    raise SystemExit(main())