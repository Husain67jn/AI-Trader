"""Timestamp-correct bar backtester with realistic costs and walk-forward validation."""
from __future__ import annotations

import statistics
from datetime import datetime

from . import data

DEFAULT_SPEC = {
    "contract_size": 100.0, "tick_size": 0.01, "tick_value": 1.0,
    "point": 0.01, "volume_min": 0.01, "volume_step": 0.01, "volume_max": 50.0,
    "commission_per_lot": 7.0, "slippage_points": 2,
}


def _atr(bars: list[dict], index: int, period: int) -> float:
    # Clamped at 0: before there are `period` bars, Python's negative indexing would
    # silently read bars off the end of the list instead of using what exists.
    ranges = []
    for i in range(max(0, index - period + 1), index + 1):
        high, low = bars[i]["high"], bars[i]["low"]
        prev_close = bars[i - 1]["close"] if i else bars[i]["open"]
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(ranges) / len(ranges) if ranges else 0.0


def run_backtest(bars: list[dict], params: dict | None = None) -> dict:
    """Donchian breakout, stop/target from ATR, next-bar entries (no look-ahead)."""
    params = {**{"window": 20, "atr_period": 14, "stop_atr": 1.5, "target_atr": 3.0,
                 "risk_pct": 0.25, "starting_equity": 100000.0, "max_hold": 200,
                 "timeframe": "H1", "data_source": "UNSPECIFIED"},
              **(params or {})}
    spec = {**DEFAULT_SPEC, **(params.get("spec") or {})}
    window = int(params["window"])
    # Data quality is reported with every result: an edge measured over gappy or
    # duplicated candles is not the same claim as one measured over clean bars.
    quality = data.bars_quality(bars, str(params["timeframe"])) if bars else {
        "ok": False, "issues": ["no bars"], "count": 0}
    common = {"bars_issues": quality["issues"], "bars_count": quality["count"],
              "bars_ok": quality["ok"], "data_source": params["data_source"]}
    if len(bars) <= window + 2:
        return {"trades": [], "metrics": {"note": "insufficient bars"}, **common}

    equity = float(params["starting_equity"])
    peak = equity
    max_dd = 0.0
    position = None
    trades: list[dict] = []

    def close_position(price: float, reason: str, when: int) -> None:
        nonlocal equity, peak, max_dd
        direction = 1 if position["side"] == "BUY" else -1
        gross = direction * (price - position["entry"]) * spec["contract_size"] * position["lots"]
        commission = spec["commission_per_lot"] * position["lots"] * 2
        pnl = gross - commission
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100 if peak else 0)
        trades.append({
            "side": position["side"], "lots": round(position["lots"], 4),
            "signal_time": position["signal_time"], "entry_time": position["entry_time"],
            "exit_time": when, "entry": round(position["entry"], 5),
            "exit": round(price, 5), "pnl": round(pnl, 2),
            "commission": round(commission, 2), "reason": reason,
        })

    for i in range(window, len(bars) - 1):
        bar, nxt = bars[i], bars[i + 1]
        if position is None:
            recent = bars[i - window:i]
            upper = max(b["high"] for b in recent)
            lower = min(b["low"] for b in recent)
            signal = "BUY" if bar["close"] > upper else ("SELL" if bar["close"] < lower else None)
            if signal is None:
                continue
            stop_dist = max(_atr(bars, i, int(params["atr_period"])) * float(params["stop_atr"]),
                            spec["point"] * 10)
            risk_money = equity * float(params["risk_pct"]) / 100
            loss_per_lot = (stop_dist / spec["tick_size"]) * spec["tick_value"]
            raw = risk_money / loss_per_lot if loss_per_lot else 0
            lots = (raw // spec["volume_step"]) * spec["volume_step"]
            lots = min(lots, spec["volume_max"])
            if lots < spec["volume_min"]:
                continue
            spread = bar.get("spread", 0) * spec["point"]
            slip = spec["slippage_points"] * spec["point"]
            raw_entry = nxt["open"]
            entry = raw_entry + (spread / 2 + slip) if signal == "BUY" else raw_entry - (spread / 2 + slip)
            stop = entry - stop_dist if signal == "BUY" else entry + stop_dist
            target = (entry + stop_dist * float(params["target_atr"]) / float(params["stop_atr"])
                      if signal == "BUY" else
                      entry - stop_dist * float(params["target_atr"]) / float(params["stop_atr"]))
            position = {
                "side": signal, "entry": entry, "stop": stop, "target": target,
                "lots": lots, "signal_time": bar["time"], "entry_time": nxt["time"],
                "entry_index": i + 1,
            }
            # Deliberately no `continue`: the bar the position was opened on is evaluated
            # below, so a stop touched inside the entry candle is a real loss.

        side = position["side"]
        exit_price = None
        exit_reason = None
        hit_stop = nxt["low"] <= position["stop"] if side == "BUY" else nxt["high"] >= position["stop"]
        hit_target = nxt["high"] >= position["target"] if side == "BUY" else nxt["low"] <= position["target"]
        if hit_stop:  # conservative: stop wins when both are touched in one bar
            # A bar that gaps through the stop fills at its open, which is worse than the
            # stop price. Filling at the stop would invent a price the market never printed.
            exit_price = (min(position["stop"], nxt["open"]) if side == "BUY"
                          else max(position["stop"], nxt["open"]))
            exit_reason = "STOP"
        elif hit_target:
            exit_price = (max(position["target"], nxt["open"]) if side == "BUY"
                          else min(position["target"], nxt["open"]))
            exit_reason = "TARGET"
        elif i + 1 - position["entry_index"] >= int(params["max_hold"]):
            exit_price, exit_reason = nxt["close"], "TIME"
        if exit_price is not None:
            close_position(exit_price, exit_reason, nxt["time"])
            position = None

    if position is not None:
        # Still open when the data ends: closed at the final close and labelled, so an
        # unfinished trade is never quietly dropped from the results.
        close_position(float(bars[-1]["close"]), "OPEN_AT_END", bars[-1]["time"])

    metrics = _metrics(trades, equity, float(params["starting_equity"]), max_dd)
    metrics["open_at_end"] = sum(1 for t in trades if t["reason"] == "OPEN_AT_END")
    return {"trades": trades, "metrics": metrics,
            "params": {k: v for k, v in params.items() if k != "spec"}, "spec": spec,
            **common}


def _metrics(trades: list[dict], equity: float, start: float, max_dd: float) -> dict:
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    returns = [p / start for p in pnls]
    sharpe = (statistics.mean(returns) / statistics.pstdev(returns) * (len(returns) ** 0.5)
              if len(returns) > 1 and statistics.pstdev(returns) > 0 else None)
    downside = statistics.pstdev([r for r in returns if r < 0]) if len([r for r in returns if r < 0]) > 1 else None
    sortino = (statistics.mean(returns) / downside * (len(returns) ** 0.5)
               if downside and downside > 0 else None)
    net = equity - start
    return {
        "net_return_pct": round(net / start * 100, 3),
        "trades": len(pnls),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "average_win": round(sum(wins) / len(wins), 2) if wins else None,
        "average_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "expectancy": round(sum(pnls) / len(pnls), 4) if pnls else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else None,
        "max_drawdown_pct": round(max_dd, 3),
        "recovery_factor": round(net / (max_dd / 100 * start), 3) if max_dd else None,
        "sharpe_approx": round(sharpe, 3) if sharpe is not None else None,
        "sortino_approx": round(sortino, 3) if sortino is not None else None,
        "total_costs": round(sum(t["commission"] for t in trades), 2),
        "note": "Backtests are conditional on data and assumptions, not evidence of future profit.",
    }


def walk_forward(bars: list[dict], folds: int = 3, param_grid: list[dict] | None = None) -> dict:
    """Optimize on an earlier slice, test only on the following unseen slice."""
    grid = param_grid or [{"window": w} for w in (10, 20, 40)]
    if len(bars) < 200:
        return {"folds": [], "note": "need at least 200 bars"}
    quality = data.bars_quality(bars)
    size = len(bars) // (folds + 1)
    out = []
    for fold in range(folds):
        train = bars[fold * size:(fold + 1) * size]
        test = bars[(fold + 1) * size:(fold + 2) * size]
        scored = []
        for params in grid:
            result = run_backtest(train, params)
            expectancy = result["metrics"].get("expectancy") or 0
            scored.append((expectancy, params, result["metrics"]))
        _, best, train_metrics = max(scored, key=lambda item: item[0])
        result = run_backtest(test, best)
        out.append({"fold": fold + 1, "best_params": best,
                    "train_metrics": train_metrics,
                    "test_metrics": result["metrics"],
                    "test_bars_issues": result["bars_issues"]})
    tested = [f["test_metrics"].get("expectancy") for f in out]
    tested = [value for value in tested if value is not None]
    return {"folds": out,
            "train_test_gap": (round(sum(tested) / len(tested), 4) if tested else None),
            "bars_issues": quality["issues"], "bars_ok": quality["ok"],
            "note": "Walk-forward results use unseen slices but remain historical estimates."}


def bars_from_rows(rows) -> list[dict]:
    return [{"time": int(datetime.fromisoformat(row["time"]).timestamp()),
             "open": float(row["open"]), "high": float(row["high"]),
             "low": float(row["low"]), "close": float(row["close"]),
             "spread": int(row["spread"])} for row in rows]