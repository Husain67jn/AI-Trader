"""Validated contracts. Claude output is untrusted input and must pass these models."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# CLOSED is not a data fault: it means the market is closed outside the desk's session
# and the quotes are the last ticks of the previous one. It is still never tradable.
Quality = Literal["HEALTHY", "DEGRADED", "STALE", "CLOSED", "UNKNOWN"]
Decision = Literal["TRADE", "PROPOSE", "NO_TRADE", "INSUFFICIENT_DATA"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class DepartmentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(default_factory=new_id)
    department: str = Field(min_length=1)
    prompt_version: str = ""
    as_of_utc: AwareDatetime
    data_sources: list[str] = Field(default_factory=list)
    data_quality: Quality = "UNKNOWN"
    facts: list[str] = Field(default_factory=list)
    interpretations: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    bullish_case: str = ""
    bearish_case: str = ""
    neutral_case: str = ""
    recommendation: str = Field(min_length=1)
    invalidation_conditions: list[str] = Field(default_factory=list)


class TradeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(default_factory=new_id)
    created_at_utc: AwareDatetime
    symbol: str = Field(min_length=1)
    direction: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    timeframe: str = "H1"
    entry_price: Decimal | None = None
    stop_loss: Decimal
    take_profit: Decimal
    risk_reward: Decimal = Decimal("0")
    confidence: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    market_regime: str = "UNKNOWN"
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    economic_event_risk: Literal["LOW", "MEDIUM", "HIGH"] = "HIGH"
    invalidation_conditions: list[str] = Field(default_factory=list)
    department_report_ids: list[str] = Field(default_factory=list)
    strategy_version: str = "unversioned"
    decision: Decision = "NO_TRADE"

    @model_validator(mode="after")
    def _trade_needs_complete_parameters(self) -> "TradeProposal":
        if self.decision not in ("TRADE", "PROPOSE"):
            return self
        if self.stop_loss <= 0 or self.take_profit <= 0:
            raise ValueError("stop_loss and take_profit must be positive")
        if not self.supporting_evidence:
            raise ValueError("a trade proposal needs timestamped evidence")
        entry = self.entry_price
        if entry is None:
            if self.order_type == "LIMIT":
                raise ValueError("LIMIT orders require entry_price")
            return self
        if entry <= 0:
            raise ValueError("entry_price must be positive")
        if self.direction == "BUY":
            if not (self.stop_loss < entry < self.take_profit):
                raise ValueError("BUY requires stop < entry < target")
        else:
            if not (self.take_profit < entry < self.stop_loss):
                raise ValueError("SELL requires target < entry < stop")
        risk = abs(entry - self.stop_loss)
        reward = abs(self.take_profit - entry)
        self.risk_reward = (reward / risk).quantize(Decimal("0.01"))
        return self


class AccountSnapshot(BaseModel):
    login: int
    # CONTEST is Mt5's own mode (1); it is not demo and not real, so it never arms.
    trade_mode: Literal["DEMO", "CONTEST", "LIVE", "UNKNOWN"]
    equity: Decimal
    balance: Decimal
    margin_free: Decimal
    currency: str = "USD"
    leverage: int = 100
    trade_allowed: bool = False
    terminal_trade_allowed: bool = False
    margin_level: Decimal | None = None


class Instrument(BaseModel):
    symbol: str
    base: str
    contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    point: Decimal
    digits: int
    tick_size: Decimal
    tick_value: Decimal
    stops_level_points: int = 0
    margin_initial: Decimal | None = None


class Quote(BaseModel):
    symbol: str
    bid: Decimal
    ask: Decimal
    ts_utc: AwareDatetime

    @property
    def spread_points(self) -> Decimal:
        return self.ask - self.bid


class Position(BaseModel):
    ticket: int
    symbol: str
    direction: Literal["BUY", "SELL"]
    volume: Decimal
    price_open: Decimal
    sl: Decimal = Decimal("0")
    tp: Decimal = Decimal("0")
    profit: Decimal = Decimal("0")
    magic: int = 0
    comment: str = ""


class OrderResult(BaseModel):
    ok: bool
    request_id: str
    ticket: int | None = None
    retcode: int | str = ""
    message: str = ""
    filled_volume: Decimal = Decimal("0")
    state: str = "UNKNOWN"


class RiskDecision(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    checks: dict = Field(default_factory=dict)
    volume: Decimal | None = None


class TradeRecord(BaseModel):
    ticket: int
    symbol: str
    direction: Literal["BUY", "SELL"]
    volume: Decimal
    open_price: Decimal
    close_price: Decimal
    open_time: AwareDatetime
    close_time: AwareDatetime
    pnl: Decimal
    commission: Decimal = Decimal("0")
    swap: Decimal = Decimal("0")
    comment: str = ""