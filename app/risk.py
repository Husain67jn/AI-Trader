"""Deterministic risk engine. No LLM, prompt, skill or strategy can alter these checks."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from . import config, db
from .models import (
    AccountSnapshot, Instrument, Position, Quote, RiskDecision, TradeProposal,
)

HEALTHY = "HEALTHY"

# Absolute ceilings, in the unit of each limit. A limit may be tightened freely;
# these bound how far it can be loosened, and they are what makes a non-finite,
# negative or absurd value impossible rather than merely unlikely.
MAX_LIMIT: dict = {
    "risk_per_trade_pct": Decimal("10"),
    "daily_loss_pct": Decimal("50"),
    "weekly_loss_pct": Decimal("50"),
    "max_drawdown_pct": Decimal("100"),
    "max_total_exposure_pct": Decimal("1000"),
    "max_positions": 10,
    "max_spread_points": 10000,
    "max_slippage_points": 500,
    "event_blackout_minutes": 1440,
    "max_quote_age_s": 3600,
}


def clean_limit(key: str, value):
    """Validate one incoming owner limit, or raise ValueError.

    Decimals must be finite (NaN and Infinity are rejected, not stored), every number
    must be positive and inside `MAX_LIMIT`, and spread limits must be a non-empty map
    of symbol -> points. The stored form is JSON-safe: decimals as strings.
    """
    if key not in config.DEFAULT_RISK_LIMITS or key not in MAX_LIMIT:
        raise ValueError(f"unknown limit: {key}")
    ceiling = MAX_LIMIT[key]
    if isinstance(config.DEFAULT_RISK_LIMITS[key], dict):
        if not isinstance(value, dict) or not value:
            raise ValueError(f"{key} must be a non-empty object of symbol -> points")
        cleaned = {}
        for name, raw in value.items():
            points = int(raw)
            if not 0 < points <= ceiling:
                raise ValueError(f"{key}.{name} must be between 1 and {ceiling}")
            cleaned[str(name)] = points
        return cleaned
    if isinstance(config.DEFAULT_RISK_LIMITS[key], Decimal):
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError(f"{key} must be a finite number")
        if not 0 < number <= ceiling:
            raise ValueError(f"{key} must be greater than 0 and at most {ceiling}")
        return str(number)
    number = int(value)
    if not 0 < number <= ceiling:
        raise ValueError(f"{key} must be between 1 and {ceiling}")
    return number


def load_limits() -> dict:
    """Code defaults, optionally overridden by the authenticated owner (logged).

    Every override is validated again here, not only at the API that wrote it: this is
    the single place every caller reads limits from, so a value that got into settings
    by another route (a hand-edited row, an older build) still cannot become a limit.
    A non-finite or non-positive number is refused outright - `NaN` in particular
    compares False against every bound, so accepting it would silently disable the
    check it was meant to tighten.
    """
    limits = dict(config.DEFAULT_RISK_LIMITS)
    limits["max_spread_points"] = dict(config.DEFAULT_RISK_LIMITS["max_spread_points"])
    raw = db.get_setting("risk_limits")
    if not raw:
        return limits
    try:
        overrides = json.loads(raw)
    except (TypeError, ValueError):
        return limits
    for key, value in overrides.items():
        if key not in limits:
            continue
        try:
            current = limits[key]
            if isinstance(current, dict):          # e.g. per-symbol spread limits
                if not isinstance(value, dict):
                    continue
                sane = {str(k): int(v) for k, v in value.items() if 0 < int(v) <= MAX_LIMIT[key]}
                limits[key] = {**current, **sane}
            elif isinstance(current, Decimal):
                cleaned = Decimal(str(value))
                if cleaned.is_finite() and 0 < cleaned <= MAX_LIMIT[key]:
                    limits[key] = cleaned
            elif isinstance(current, int) and not isinstance(current, bool):
                cleaned = int(value)
                if 0 < cleaned <= MAX_LIMIT[key]:
                    limits[key] = cleaned
        except (TypeError, ValueError, ArithmeticError):
            continue
    return limits


def arm(actor: str, account: AccountSnapshot, broker=None) -> str:
    """Arm only a real MT5 session holding a positively verified demo account."""
    from .broker import can_arm
    ok, why = can_arm(broker, account)
    if not ok or not config.DEMO_ONLY:
        raise PermissionError(f"cannot arm: {why}")
    db.log(actor, "armed", {"reason": why})
    return db.set_kill_switch("ARMED", actor)


def pause(actor: str, reason: str = "") -> str:
    db.log(actor, "pause", {"reason": reason})
    return db.set_kill_switch("PAUSED", actor)


def emergency_stop(actor: str, reason: str = "") -> str:
    db.log(actor, "emergency_stop", {"reason": reason})
    return db.set_kill_switch("EMERGENCY_STOP", actor)


class RiskEngine:
    """Pure assessment over explicit inputs; fails closed on any exception."""

    def __init__(self, limits: dict | None = None):
        self.limits = limits or load_limits()

    def assess(
        self,
        *,
        proposal: TradeProposal,
        account: AccountSnapshot,
        instrument: Instrument,
        quote: Quote,
        positions: list[Position],
        health: str,
        start_of_day_equity: Decimal,
        start_of_week_equity: Decimal,
        margin_required: Decimal | None = None,
        margin_for_volume=None,
        blackout: bool = False,
        cost_per_lot: Decimal = Decimal("0"),
        kill_state: str | None = None,
        now: datetime | None = None,
        peak_equity: Decimal | None = None,
        unresolved_orders: int = 0,
        session_open: bool | None = None,
        slippage_points: int | None = None,
        exposure_fn=None,
        calendar_state: str = "UNKNOWN",
    ) -> RiskDecision:
        try:
            return self._assess(
                proposal=proposal, account=account, instrument=instrument,
                quote=quote, positions=positions, health=health,
                start_of_day_equity=start_of_day_equity,
                start_of_week_equity=start_of_week_equity,
                margin_required=margin_required, margin_for_volume=margin_for_volume,
                blackout=blackout, cost_per_lot=cost_per_lot, kill_state=kill_state,
                now=now or datetime.now(timezone.utc),
                peak_equity=peak_equity, unresolved_orders=unresolved_orders,
                session_open=session_open, slippage_points=slippage_points,
                exposure_fn=exposure_fn, calendar_state=calendar_state,
            )
        except Exception as exc:  # fail closed, never guess
            return RiskDecision(
                approved=False,
                reasons=[f"risk engine error, fail closed: {type(exc).__name__}"],
                checks={"error": type(exc).__name__},
            )

    def _assess(self, *, proposal, account, instrument, quote, positions, health,
                start_of_day_equity, start_of_week_equity, margin_required,
                margin_for_volume, blackout, cost_per_lot, kill_state, now,
                peak_equity, unresolved_orders, session_open, slippage_points,
                exposure_fn, calendar_state) -> RiskDecision:
        reasons: list[str] = []
        checks: dict = {}
        limits = self.limits

        base = config.canonical_base(proposal.symbol)
        # The whitelist is a safety boundary, not guidance: an untrusted proposal must
        # not be able to reach an instrument the desk was never configured for.
        checks["symbol_whitelisted"] = base is not None
        if base is None:
            reasons.append(f"symbol {proposal.symbol} is not on the tradable whitelist")
        checks["demo_verified"] = account.trade_mode == "DEMO"
        if account.trade_mode != "DEMO":
            reasons.append("account is not a positively verified demo account")

        state = kill_state or db.kill_switch_state()
        checks["kill_switch"] = state
        if state != "ARMED":
            reasons.append(f"kill switch is {state}")

        checks["data_health"] = health
        if health != HEALTHY:
            reasons.append(f"market data health is {health}")

        # Account status: a real broker can disable trading on the account or terminal
        # even while the account mode still reads DEMO.
        checks["account_trade_allowed"] = account.trade_allowed
        checks["terminal_trade_allowed"] = account.terminal_trade_allowed
        if not account.trade_allowed:
            reasons.append("broker reports trading disabled on this account")
        if not account.terminal_trade_allowed:
            reasons.append("broker terminal reports AutoTrading disabled")

        checks["session_open"] = (config.session_open(now) if session_open is None
                                  else bool(session_open))
        if not checks["session_open"]:
            reasons.append("outside the configured trading session")

        checks["unresolved_orders"] = unresolved_orders
        if unresolved_orders:
            reasons.append(f"{unresolved_orders} order(s) in an unresolved state")

        if proposal.decision not in ("TRADE", "PROPOSE"):
            reasons.append(f"proposal decision is {proposal.decision}")

        age = (now - quote.ts_utc).total_seconds()
        checks["quote_age_s"] = round(age, 3)
        if age > limits["max_quote_age_s"] or age < -5:
            reasons.append("quote is stale or has an impossible timestamp")

        point = instrument.point
        spread_points = (quote.ask - quote.bid) / point
        spread_limit = limits["max_spread_points"].get(
            base or "", limits["max_spread_points"].get("default", 30)
        )
        checks["spread_points"] = str(spread_points)
        checks["spread_limit"] = spread_limit
        if spread_points < 0 or spread_points > spread_limit:
            reasons.append("spread exceeds the configured limit")

        daily_loss = Decimal("0")
        if start_of_day_equity > 0:
            daily_loss = ((start_of_day_equity - account.equity) / start_of_day_equity) * 100
        weekly_loss = Decimal("0")
        if start_of_week_equity > 0:
            weekly_loss = ((start_of_week_equity - account.equity) / start_of_week_equity) * 100
        checks["daily_loss_pct"] = str(daily_loss.quantize(Decimal("0.01")))
        checks["weekly_loss_pct"] = str(weekly_loss.quantize(Decimal("0.01")))
        if daily_loss >= limits["daily_loss_pct"]:
            reasons.append("daily loss stop reached")
        if weekly_loss >= limits["weekly_loss_pct"]:
            reasons.append("weekly loss stop reached")

        # Drawdown from the highest equity seen, not just from today's baseline.
        reference = max(peak_equity or account.equity, account.equity)
        drawdown = Decimal("0")
        if reference > 0:
            drawdown = ((reference - account.equity) / reference) * 100
        checks["drawdown_pct"] = str(drawdown.quantize(Decimal("0.01")))
        if drawdown >= limits["max_drawdown_pct"]:
            reasons.append("maximum drawdown reached")

        if account.margin_level is not None and account.margin_level > 0:
            checks["margin_level_pct"] = str(account.margin_level)  # broker-reported

        # Slippage cannot be predicted, so the bound is the deviation the order is
        # allowed to suffer. Anything above the limit is refused before submission.
        if slippage_points is not None:
            checks["slippage_points"] = slippage_points
            checks["slippage_limit"] = limits["max_slippage_points"]
            if slippage_points > limits["max_slippage_points"]:
                reasons.append("requested slippage tolerance exceeds the limit")

        checks["open_positions"] = len(positions)
        if len(positions) >= limits["max_positions"]:
            reasons.append("maximum concurrent positions reached")

        group = config.CORRELATION_GROUPS.get(base or "")
        same_symbol = [p for p in positions if p.symbol == proposal.symbol]
        correlated = [p for p in positions if group and config.CORRELATION_GROUPS.get(
            config.canonical_base(p.symbol) or "", "") == group]
        checks["duplicate_symbol"] = bool(same_symbol)
        checks["correlated_positions"] = [p.ticket for p in correlated]
        if same_symbol:
            reasons.append("an existing position already covers this symbol")
        elif correlated:
            reasons.append("correlated exposure already open")

        if blackout:
            checks["event_blackout"] = True
            reasons.append("high-impact economic event blackout active")

        # An unverified calendar means the blackout above proved nothing, so it is a
        # hard rejection. AVAILABLE requires a configured feed AND a recent successful
        # ingestion; an empty result is not evidence that no events exist.
        checks["calendar"] = calendar_state
        if calendar_state != "AVAILABLE":
            reasons.append(f"economic calendar is {calendar_state}")

        entry = proposal.entry_price or (quote.ask if proposal.direction == "BUY" else quote.bid)
        entry = Decimal(entry)
        dist = abs(entry - proposal.stop_loss)
        min_stop = instrument.stops_level_points * instrument.point
        checks["entry_reference"] = str(entry)
        checks["stop_distance"] = str(dist)
        checks["min_stop_distance"] = str(min_stop)
        if proposal.direction == "BUY":
            if proposal.stop_loss >= entry:
                reasons.append("BUY stop-loss is not below entry")
            if proposal.take_profit <= entry:
                reasons.append("BUY take-profit is not above entry")
        else:
            if proposal.stop_loss <= entry:
                reasons.append("SELL stop-loss is not above entry")
            if proposal.take_profit >= entry:
                reasons.append("SELL take-profit is not below entry")
        if dist < min_stop:
            reasons.append("stop distance is below the broker minimum")

        volume: Decimal | None = None
        if dist > 0 and instrument.tick_size > 0 and instrument.tick_value > 0:
            loss_per_lot = (dist / instrument.tick_size) * instrument.tick_value + cost_per_lot
            risk_money = account.equity * limits["risk_per_trade_pct"] / 100
            step = instrument.volume_step
            raw_lots = risk_money / loss_per_lot if loss_per_lot > 0 else Decimal("0")
            volume = (raw_lots // step) * step
            volume = min(volume, instrument.volume_max).quantize(step)
            checks["risk_money"] = str(risk_money.quantize(Decimal("0.01")))
            checks["raw_lots"] = str(raw_lots)
            checks["volume"] = str(volume)
            if volume < instrument.volume_min:
                reasons.append("risk budget is below the broker minimum volume")
            if margin_for_volume is not None and volume >= instrument.volume_min:
                margin_required = margin_for_volume(volume)
            if margin_required is not None:
                checks["margin_required"] = str(margin_required)
                checks["margin_free"] = str(account.margin_free)
                if margin_required > account.margin_free:
                    reasons.append("insufficient free margin")
            # Total portfolio exposure: existing notional plus the new position.
            if exposure_fn is not None and volume >= instrument.volume_min:
                try:
                    exposure = exposure_fn(volume)
                    checks["exposure_pct"] = str(exposure)
                    if exposure > limits["max_total_exposure_pct"]:
                        reasons.append("total portfolio exposure would exceed the limit")
                except Exception as exc:  # unknown exposure is not a safe default
                    reasons.append(f"exposure could not be computed ({type(exc).__name__})")
        else:
            reasons.append("cannot size position from contract specification")

        approved = not reasons
        return RiskDecision(
            approved=approved, reasons=reasons, checks=checks,
            volume=volume if approved else None,
        )


def record_decision(proposal_id: str, decision: RiskDecision) -> None:
    with db.tx() as con:
        con.execute(
            "INSERT INTO risk_decisions(proposal_id, approved, reasons, checks, created_at) "
            "VALUES(?,?,?,?,?)",
            (
                proposal_id, int(decision.approved), json.dumps(decision.reasons),
                json.dumps(decision.checks, default=str), db.now_iso(),
            ),
        )
        db.audit(con, "risk_engine", "risk_assessed", {
            "proposal_id": proposal_id, "approved": decision.approved,
            "reasons": decision.reasons, "volume": str(decision.volume),
        })