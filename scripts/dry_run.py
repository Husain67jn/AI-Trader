"""Full execution dry run: the whole chain, stopping before any broker order.

Market data -> analysts -> CIO -> risk engine -> execution state machine -> broker
adapter (guard, order_check) -> order tracking -> reconciliation -> trade review.

It is a dry run in the strict sense:

  * the MetaTrader5 package is a stand-in whose ``order_send`` **raises** if it is ever
    called, so the last step of the real adapter path cannot reach a wire - and the run
    asserts afterwards that it was never invoked;
  * the state machine, tracking, reconciliation and review stages run against a fake
    broker, never the terminal;
  * everything lives in a temporary database under the temp folder. The project database
    is not opened.

    python scripts/dry_run.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

UTC = timezone.utc
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The isolated database must be chosen BEFORE app.config is imported: config computes
# DATA_DIR and DB_PATH once, at import time. A harness that set these afterwards ran
# against the project database and armed its kill switch - the mistake this ordering and
# `_refuse_project_database()` below exist to make impossible.
_TMP = Path(tempfile.mkdtemp(prefix="ati-dryrun-"))
_PROJECT_DATA = (Path(__file__).resolve().parent.parent / "data").resolve()
os.environ["TRADER_DATA_DIR"] = str(_TMP)
os.environ["TRADER_DB"] = str(_TMP / "dryrun.db")
os.environ["TRADER_LLM_PROVIDER"] = "mock"
os.environ["TRADER_SCHEDULER"] = "0"
# A calendar source must be *named* for the desk's own gate to see a configured
# provider; a row is written into the isolated database below, labelled DRY_RUN, so no
# fetch happens and nothing is presented as live.
os.environ["TRADER_CALENDAR_SOURCE"] = "forexfactory"

from app import config  # noqa: E402 - after the environment, deliberately
from app.broker import REAL_MT5  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []
BLOCKING: list[str] = []


def _refuse_project_database() -> None:
    """Hard stop if anything about the configured database is the real one."""
    resolved = Path(config.DB_PATH).resolve()
    if str(resolved).startswith(str(_PROJECT_DATA)):
        raise SystemExit(
            f"refusing to run: the configured database is the project database ({resolved})")
    if not str(resolved).startswith(str(_TMP.resolve())):
        raise SystemExit(f"refusing to run: {resolved} is not inside the temp directory")


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    if not ok:
        BLOCKING.append(f"{name}: {detail}")
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


# --- a stand-in for the MetaTrader5 package ---------------------------------
class FakeMt5:
    """The package's API with its real constant values, and a poisoned send.

    Read paths answer with plausible demo data so the adapter's own logic runs; the one
    write path raises, which is what makes this a dry run rather than a test order.
    """

    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_TIME_GTC, ORDER_FILLING_IOC = 0, 1
    ORDER_STATE_STARTED, ORDER_STATE_PLACED, ORDER_STATE_CANCELED = 0, 1, 2
    ORDER_STATE_PARTIAL, ORDER_STATE_FILLED, ORDER_STATE_REJECTED = 3, 4, 5
    ORDER_STATE_EXPIRED, ORDER_STATE_REQUEST_ADD = 6, 7
    ORDER_STATE_REQUEST_MODIFY, ORDER_STATE_REQUEST_CANCEL = 8, 9
    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED = 10009, 10008
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1, 0, 1
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_H1 = 1, 5, 16385
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1

    def __init__(self) -> None:
        self.order_send_calls: list[dict] = []
        self.check_calls: list[dict] = []
        self.shutdowns = 0
        self.selected: set[str] = set()
        self._position: dict | None = None

    # connection / account
    def initialize(self, *args, **kwargs) -> bool:
        return True

    def shutdown(self) -> None:
        self.shutdowns += 1

    def last_error(self):
        return (0, "ok")

    def account_info(self):
        return types.SimpleNamespace(
            # A balance that can size a minimum-lot trade, so the chain can be
            # exercised at all. A separate arbitrary synthetic small account tests
            # minimum-volume position sizing in stage 3b.
            login=99999999, trade_mode=self.ACCOUNT_TRADE_MODE_DEMO, equity=10000.0,
            balance=10000.0, margin_free=10000.0, currency="USD", leverage=100,
            margin_level=None, trade_allowed=True)

    def terminal_info(self):
        return types.SimpleNamespace(connected=True, trade_allowed=True)

    # symbols
    def symbols_get(self):
        return [types.SimpleNamespace(name=name) for name in
                ("EURUSDm", "GBPUSDm", "XAUUSDm", "XAUUSD247m", "USDCNHm", "US500m")]

    def symbol_select(self, name, enable=True) -> bool:
        self.selected.add(name)
        return True

    def symbol_info(self, symbol):
        spec = {
            "EURUSDm": (5, 0.00001, 100000.0, 0.01, 100.0, 0.01),
            "GBPUSDm": (5, 0.00001, 100000.0, 0.01, 100.0, 0.01),
            "XAUUSDm": (3, 0.001, 100.0, 0.01, 50.0, 0.01),
        }.get(symbol)
        if spec is None:
            return None
        digits, point, contract, vmin, vmax, vstep = spec
        return types.SimpleNamespace(
            name=symbol, trade_contract_size=contract, volume_min=vmin, volume_max=vmax,
            volume_step=vstep, point=point, digits=digits, trade_tick_size=point,
            trade_tick_value=1.0, trade_stops_level=0)

    def symbol_info_tick(self, symbol):
        mid = {"EURUSDm": 1.0850, "GBPUSDm": 1.2650, "XAUUSDm": 2650.0}.get(symbol)
        if mid is None:
            return None
        half, digits = {"EURUSDm": (0.00010, 5), "GBPUSDm": (0.00010, 5),
                        "XAUUSDm": (0.12, 3)}[symbol]
        stamp = int(datetime.now(UTC).timestamp() * 1000)
        return types.SimpleNamespace(bid=round(mid - half, digits),
                                     ask=round(mid + half, digits), time_msc=stamp)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        step = {self.TIMEFRAME_M1: 60, self.TIMEFRAME_H1: 3600}.get(timeframe, 3600)
        mid = {"EURUSDm": 1.0850, "GBPUSDm": 1.2650, "XAUUSDm": 2650.0}.get(symbol, 1.0)
        end = int(datetime.now(UTC).timestamp())
        out = []
        for i in range(count):
            stamp = end - step * (count - 1 - i)
            close = mid * (1 + 0.0002 * ((i % 7) - 3) / 3)
            out.append({"time": stamp, "open": close, "high": close * 1.0004,
                        "low": close * 0.9996, "close": close, "tick_volume": 100,
                        "spread": 10})
        return out

    # trading
    def order_calc_margin(self, order_type, symbol, volume, price):
        contract = {"EURUSDm": 100000.0, "GBPUSDm": 100000.0, "XAUUSDm": 100.0}[symbol]
        return volume * contract * price / 100

    def order_check(self, request):
        self.check_calls.append(request)
        return types.SimpleNamespace(retcode=0, comment="Done")

    def order_send(self, request):
        """The dry-run boundary: recorded, then refused loudly."""
        self.order_send_calls.append(request)
        raise AssertionError("dry run: order_send must never be called")

    def positions_get(self, ticket=None, **kwargs):
        if self._position is None:
            return []
        if ticket is not None and ticket != self._position["ticket"]:
            return []
        return [types.SimpleNamespace(**self._position)]

    def orders_get(self, ticket=None, **kwargs):
        return []

    def history_orders_get(self, *args, **kwargs):
        return []

    def history_deals_get(self, *args, **kwargs):
        return []


class DryRunBroker:
    """A fake with the class of a real session, for the state-machine stages.

    Only `kind` makes it usable by the executor; every method is deterministic and
    in-memory, and it records the request it was asked to send so the dry run can show
    exactly what an armed desk would have submitted.
    """

    kind = REAL_MT5                                   # the executor's money gate
    name = "dry-run"
    magic = 20261106
    history_window_days = 30
    connected = True

    def __init__(self, equity: str = "100000") -> None:
        from app.models import AccountSnapshot, Instrument, Position, Quote
        self._models = (AccountSnapshot, Instrument, Position, Quote)
        self.equity = Decimal(equity)
        self._positions: list = []
        self._sent: list[dict] = []
        self._approved_requests: dict[str, object] = {}

    # --- connection / account
    def connect(self):
        return self

    def disconnect(self) -> None:
        self.connected = False

    def account(self):
        return self._models[0](login=99999999, trade_mode="DEMO", equity=self.equity,
                               balance=self.equity, margin_free=self.equity,
                               currency="USD", leverage=100, trade_allowed=True,
                               terminal_trade_allowed=True)

    # --- market
    def symbols(self):
        return ["EURUSDm", "GBPUSDm", "XAUUSDm"]

    def instrument(self, symbol):
        spec = {"EURUSDm": ("EURUSD", 100000, 5, Decimal("0.00001")),
                "GBPUSDm": ("GBPUSD", 100000, 5, Decimal("0.00001")),
                "XAUUSDm": ("XAUUSD", 100, 3, Decimal("0.001")),
                # Offered by the terminal and not on the desk's whitelist: the risk
                # engine, not an unknown-instrument guard, must be what refuses it.
                "USDCNHm": ("USDCNH", 100000, 4, Decimal("0.0001"))}.get(symbol)
        if spec is None:
            return None
        base, contract, digits, point = spec
        return self._models[1](
            symbol=symbol, base=base, contract_size=Decimal(contract),
            volume_min=Decimal("0.01"), volume_max=Decimal("100"),
            volume_step=Decimal("0.01"), point=point, digits=digits,
            tick_size=point, tick_value=Decimal("1"), stops_level_points=0)

    def quote(self, symbol):
        mid = {"EURUSDm": Decimal("1.0850"), "GBPUSDm": Decimal("1.2650"),
               "XAUUSDm": Decimal("2650.00"), "USDCNHm": Decimal("7.2000")}.get(symbol)
        if mid is None:
            return None
        half = {"XAUUSDm": Decimal("0.05"), "USDCNHm": Decimal("0.0004")}.get(
            symbol, Decimal("0.00005"))
        return self._models[3](symbol=symbol, bid=mid - half, ask=mid + half,
                               ts_utc=datetime.now(UTC))

    def bars(self, symbol, timeframe="H1", count=500):
        from app import data
        base = self.quote(symbol)
        if base is None:
            return []
        end = datetime.now(UTC).replace(second=0, microsecond=0)
        step = {"M1": 60, "H1": 3600}.get(timeframe, 3600)
        price = base.ask
        out = []
        for i in range(count):
            stamp = end - timedelta(seconds=step * (count - 1 - i))
            out.append({"time": int(stamp.timestamp()), "open": float(price),
                        "high": float(price) * 1.0005, "low": float(price) * 0.9995,
                        "close": float(price), "tick_volume": 100, "spread": 10})
        del data
        return out

    def calc_margin(self, symbol, direction, volume, price):
        instrument = self.instrument(symbol)
        return (Decimal(volume) * instrument.contract_size * Decimal(price) / 100
                ).quantize(Decimal("0.01"))

    # --- execution
    def check_order(self, request) -> tuple[bool, str]:
        return True, "ok"

    def submit(self, request):
        from app.models import OrderResult
        self._sent.append(request)
        ticket = 5000 + len(self._sent)
        self._models[2]  # Position
        self._positions.append(self._models[2](
            ticket=ticket, symbol=request["symbol"], direction=request["direction"],
            volume=Decimal(request["volume"]), price_open=Decimal(request["price"]),
            sl=Decimal(request.get("sl") or 0), tp=Decimal(request.get("tp") or 0),
            magic=self.magic, comment=request.get("comment", "")))
        result = OrderResult(ok=True, request_id=request["request_id"], ticket=ticket,
                             retcode="DONE", filled_volume=Decimal(request["volume"]),
                             state="FILLED")
        self._approved_requests[request["request_id"]] = result
        return result

    def order_status(self, ticket: int) -> str:
        return "FILLED" if any(p.ticket == ticket for p in self._positions) else "UNKNOWN"

    def order_by_request(self, request_id: str, since=None):
        return self._approved_requests.get(request_id)

    def positions(self):
        return list(self._positions)

    def close_position(self, ticket: int, pnl: str = "20") -> None:
        for position in list(self._positions):
            if position.ticket == ticket:
                self._positions.remove(position)
                self._closed.append(position) if hasattr(self, "_closed") else None

    def closed_trades(self, days: int = 30):
        return []

    def sent_requests(self) -> list[dict]:
        return list(self._sent)


def stage_market_data(broker) -> tuple[str, dict]:
    from app import data
    print("\n1. Market data (through the adapter's MT5 path)")
    symbols = data.eligible_symbols(broker)
    check(symbols == ["EURUSDm", "GBPUSDm", "XAUUSDm"],
          "the whitelist resolves to the broker's own contracts", str(symbols))
    quote = broker.quote("EURUSDm")
    check(quote is not None and quote.ask > quote.bid > 0,
          "a live bid/ask is readable",
          f"bid={quote.bid} ask={quote.ask}" if quote else "no quote")
    bars = broker.bars("EURUSDm", "H1", 200)
    quality = data.bars_quality(bars, "H1")
    check(quality["count"] == 200 and quality["ok"],
          "200 H1 candles are complete and ordered", str(quality["issues"][:2]))
    health, detail = data.market_health(broker, symbols)
    check(health == "HEALTHY", "the health gate reports HEALTHY on fresh data", health)
    return health, detail


def stage_research(broker, provider, symbols: list[str]) -> dict:
    from app import departments, providers
    print("\n2. Analysts and CIO")
    result = departments.run(provider, broker, symbols)
    proposal = result["proposal"]
    check(len(result["reports"]) == 9, "nine department reports were produced",
          f"{len(result['reports'])} reports")
    check(all(report["data_quality"] for report in result["reports"]),
          "every report carries an explicit data quality")
    check(proposal["decision"] in ("TRADE", "PROPOSE", "NO_TRADE", "INSUFFICIENT_DATA"),
          "the CIO returned a validated proposal", f"decision={proposal['decision']}")
    check(providers.calendar_status()["state"] == providers.AVAILABLE,
          "the calendar is verified before any risk assessment")
    return proposal


def stage_risk(service, proposal, health: str):
    from app.models import TradeProposal
    print("\n3. Risk engine")
    parsed = TradeProposal.model_validate(proposal)
    decision = service.assess(parsed, health=health)
    if not decision.approved and any("stale" in reason for reason in decision.reasons):
        # A harness artefact rather than desk behaviour: the fake tick is stamped when
        # the adapter asks for it, so a machine stall between the quote and the freshness
        # check can age it past the 15-second window. Retried once, and said out loud.
        print(f"    note: the first assessment raced a clock stall (quote_age_s="
              f"{decision.checks.get('quote_age_s')}); retrying once")
        decision = service.assess(parsed, health=health)
    check(decision.approved, "the proposal is approved on healthy data",
          "; ".join(decision.reasons[:2])
          + (f" [quote_age_s={decision.checks.get('quote_age_s')} "
             f"now={datetime.now(UTC).isoformat()}]" if not decision.approved else ""))
    check(decision.volume is not None and decision.volume > 0,
          "a position size was computed", f"volume={decision.volume}")
    checks = decision.checks
    check(Decimal(checks["stop_distance"]) > 0, "stop distance is positive",
          f"{checks['stop_distance']}")
    check(checks["risk_money"] and Decimal(checks["risk_money"]) > 0,
          "the risk budget is explicit", f"money at risk = {checks['risk_money']}")
    check(float(checks["spread_points"]) <= float(checks["spread_limit"]),
          "spread is inside the configured limit",
          f"{checks['spread_points']} <= {checks['spread_limit']}")
    check(checks["symbol_whitelisted"] is True, "the symbol is whitelisted")
    check(checks["calendar"] == "AVAILABLE", "the calendar is verified")
    return parsed, decision


def stage_adapter(broker, fake: FakeMt5, proposal, decision) -> None:
    """The adapter's own last checks, with the send poisoned: nothing reaches a wire."""
    print("\n4. Broker adapter (guard and order_check, send poisoned)")
    quote = broker.quote(proposal.symbol)
    price = quote.ask if proposal.direction == "BUY" else quote.bid
    request = {
        "request_id": f"dry-run-{proposal.proposal_id[:8]}", "symbol": proposal.symbol,
        "direction": proposal.direction, "volume": decision.volume, "price": price,
        "sl": proposal.stop_loss, "tp": proposal.take_profit,
        "deviation": 30, "comment": "ATI-DEMO-dryrun",
    }
    refusal = broker.guard(request)
    check(refusal is None, "the adapter's own preconditions pass (guard)",
          "" if refusal is None else f"{refusal.retcode}: {refusal.message}")
    ok, message = broker.check_order(request)
    check(ok, "order_check accepts the request", message)
    check(fake.order_send_calls == [], "order_send was NOT called",
          f"{len(fake.order_send_calls)} call(s)")
    check(len(fake.check_calls) == 1, "exactly one order_check reached the package")
    print("\n  the order an armed desk would have submitted:")
    for key in ("symbol", "direction", "volume", "price", "sl", "tp", "deviation",
                "comment"):
        print(f"    {key:<10} {request[key]}")
    checks = decision.checks
    reward = abs(Decimal(proposal.take_profit) - Decimal(price))
    risk = abs(Decimal(price) - Decimal(proposal.stop_loss))
    print(f"    {'risk':<10} {checks['risk_money']} at risk, stop "
          f"{checks['stop_distance']} away, reward {reward}, reward:risk "
          f"{(reward / risk).quantize(Decimal('0.01')) if risk else 'n/a'}")


def stage_account_size(adapter, proposal, health: str) -> None:
    """Exercise minimum-lot sizing with an arbitrary synthetic small account.

    The fake terminal uses a larger synthetic account so the execution chain can be
    tested. This separate scenario demonstrates refusal when a small account cannot
    support the minimum lot; it contains no measurement from a user's account.
    """
    from app.risk import RiskEngine
    from decimal import Decimal
    print("\n3b. Position sizing on an illustrative synthetic small account")
    account = adapter.account().model_copy(
        update={"equity": Decimal("50"), "balance": Decimal("50"),
                "margin_free": Decimal("50")})
    decision = RiskEngine().assess(
        proposal=proposal, account=account, instrument=adapter.instrument(proposal.symbol),
        quote=adapter.quote(proposal.symbol), positions=[], health=health,
        start_of_day_equity=account.equity, start_of_week_equity=account.equity,
        kill_state="ARMED", session_open=True, calendar_state="AVAILABLE")
    check(not decision.approved, "the synthetic small account cannot size this trade",
          "; ".join(decision.reasons[:2]))
    detail = f"risk budget {decision.checks.get('risk_money')} on synthetic equity {account.equity}, stop "
    detail += f"{decision.checks.get('stop_distance')}, a minimum lot would risk "
    detail += f"{_minimum_lot_risk(adapter, proposal)}"
    print(f"    {detail}")
    print("    this synthetic scenario needs roughly "
          f"{_equity_needed(adapter, proposal)} USD of equity for a minimum-lot trade "
          "at the configured 0.25% risk per trade; raising that percentage is a risk "
          "limit change and is left to the owner")


def _minimum_lot_risk(adapter, proposal) -> str:
    instrument = adapter.instrument(proposal.symbol)
    quote = adapter.quote(proposal.symbol)
    entry = quote.ask if proposal.direction == "BUY" else quote.bid
    distance = abs(entry - proposal.stop_loss)
    ticks = distance / instrument.tick_size
    return f"{ticks * instrument.tick_value * instrument.volume_min} " \
           f"{adapter.account().currency}"


def _equity_needed(adapter, proposal) -> str:
    instrument = adapter.instrument(proposal.symbol)
    quote = adapter.quote(proposal.symbol)
    entry = quote.ask if proposal.direction == "BUY" else quote.bid
    distance = abs(entry - proposal.stop_loss)
    risk_per_lot = (distance / instrument.tick_size) * instrument.tick_value
    minimum_risk = risk_per_lot * instrument.volume_min
    pct = Decimal(str(config.DEFAULT_RISK_LIMITS["risk_per_trade_pct"])) / 100
    return str((minimum_risk / pct).quantize(Decimal("1")))


def stage_state_machine(broker, proposal, decision, tmp: Path) -> None:
    """Tracking, idempotency, kill switch, restart recovery - all in the temp DB."""
    from app import db, departments
    from app.execution import ExecutionService, unresolved_orders
    from app.risk import RiskEngine
    print("\n5. Execution state machine (fake broker, isolated database)")
    service = ExecutionService(broker, RiskEngine())
    # Recorded the way the department workflow records it, so the outcome the desk would
    # be reading can propagate from the order back to the proposal.
    departments.store_proposal(proposal, "PROPOSED")
    check(bool(db.query("SELECT outcome FROM proposals WHERE proposal_id=?",
                        (proposal.proposal_id,))),
          "the proposal is on the ledger before anything is submitted")

    db.set_kill_switch("PAUSED", "dry-run")
    blocked = service.submit(proposal, decision, request_id="dry-paused")
    check(blocked["state"] == "BLOCKED" and not broker.sent_requests(),
          "a PAUSED kill switch blocks the submission", blocked["message"])

    db.set_kill_switch("ARMED", "dry-run")
    result = service.submit(proposal, decision, request_id="dry-1")
    check(result["state"] == "FILLED" and result["ticket"],
          "an armed submission fills and is tracked",
          f"state={result['state']} ticket={result['ticket']}")
    row = db.query("SELECT * FROM orders WHERE request_id=?", ("dry-1",))[0]
    check(row["state"] == "FILLED" and row["ticket"] == result["ticket"],
          "the ledger row matches the broker's ticket")
    proposal_row = db.query("SELECT outcome FROM proposals WHERE proposal_id=?",
                            (proposal.proposal_id,))
    check(bool(proposal_row) and proposal_row[0]["outcome"] == "FILLED",
          "the proposal's outcome follows the order")

    replay = service.submit(proposal, decision, request_id="dry-1")
    check(replay.get("duplicate") and len(broker.sent_requests()) == 1,
          "a duplicate request id is refused, not resubmitted")

    # A second position on the same symbol is refused by the risk engine.
    second = service.assess(proposal)
    check(not second.approved and any("already covers" in reason
                                      for reason in second.reasons),
          "a second position on the same symbol is refused")

    print("\n6. Reconciliation and restart recovery")
    report = service.reconcile()
    check(report["ok"] and report["unresolved_orders"] == [],
          "reconcile reports a clean ledger", str(report["missing_at_broker"]))
    # A restart is a new process reading the same database: the order must still be
    # known, and the kill switch must come back PAUSED.
    db.init_db()
    check(db.kill_switch_state() == "PAUSED", "a restart leaves the switch PAUSED")
    check(unresolved_orders() == [], "no order is left unresolved after the restart")
    restarted = ExecutionService(broker, RiskEngine())
    check(restarted.reconcile()["ok"] is True, "the restarted desk reconciles cleanly")

    with db.tx() as con:
        con.execute("UPDATE orders SET state='SUBMISSION_PENDING', ticket=NULL, "
                    "updated_at=? WHERE request_id='dry-1'",
                    ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(),))
    settled = restarted.reconcile()
    check(settled["settled_orders"] == ["dry-1"],
          "an order left pending by a crash is settled from broker evidence",
          str(settled["settled_orders"]))


def stage_negatives(broker, proposal, decision) -> None:
    """Every gate that must still say no."""
    from app import db
    from app.execution import ExecutionService
    from app.risk import RiskEngine
    from app.models import TradeProposal
    print("\n7. Negative controls (each must block)")
    service = ExecutionService(broker, RiskEngine())

    off_whitelist = TradeProposal.model_validate(
        {**proposal.model_dump(mode="json"), "symbol": "USDCNHm", "proposal_id": "d-off"})
    refused = service.assess(off_whitelist)
    check(not refused.approved and any("whitelist" in r for r in refused.reasons),
          "an off-whitelist symbol is refused", "; ".join(refused.reasons[:1]))

    for health in ("STALE", "CLOSED", "UNKNOWN", "DEGRADED"):
        blocked = service.assess(proposal, health=health)
        check(not blocked.approved, f"data health {health} blocks trading")

    stale_quote = broker.quote(proposal.symbol)
    old = stale_quote.model_copy(update={"ts_utc": datetime.now(UTC) - timedelta(minutes=5)})
    stale = RiskEngine().assess(
        proposal=proposal, account=broker.account(), instrument=broker.instrument(proposal.symbol),
        quote=old, positions=[], health="HEALTHY",
        start_of_day_equity=broker.account().equity,
        start_of_week_equity=broker.account().equity, kill_state="ARMED",
        session_open=True, calendar_state="AVAILABLE")
    check(not stale.approved and any("stale" in r for r in stale.reasons),
          "a stale quote is refused even at HEALTHY desk level")

    wide = broker.quote(proposal.symbol).model_copy(
        update={"ask": broker.quote(proposal.symbol).ask + Decimal("0.01")})
    wide_decision = RiskEngine().assess(
        proposal=proposal, account=broker.account(), instrument=broker.instrument(proposal.symbol),
        quote=wide, positions=[], health="HEALTHY",
        start_of_day_equity=broker.account().equity,
        start_of_week_equity=broker.account().equity, kill_state="ARMED",
        session_open=True, calendar_state="AVAILABLE")
    check(not wide_decision.approved and any("spread" in r for r in wide_decision.reasons),
          "a spread above the configured maximum is refused")

    for state in ("PAUSED", "EMERGENCY_STOP"):
        db.set_kill_switch(state, "dry-run")
        blocked = service.assess(proposal)
        check(not blocked.approved, f"the kill switch {state} blocks trading")
    db.set_kill_switch("ARMED", "dry-run")

    closed = RiskEngine().assess(
        proposal=proposal, account=broker.account(), instrument=broker.instrument(proposal.symbol),
        quote=broker.quote(proposal.symbol), positions=[], health="HEALTHY",
        start_of_day_equity=broker.account().equity,
        start_of_week_equity=broker.account().equity, kill_state="ARMED",
        session_open=config.session_open is not None and False, calendar_state="AVAILABLE")
    check(not closed.approved and any("session" in r for r in closed.reasons),
          "outside the configured trading session, trading is refused",
          "; ".join(closed.reasons[:1]))

    over = TradeProposal.model_validate(
        {**proposal.model_dump(mode="json"), "proposal_id": "d-far",
         "entry_price": None,
         "stop_loss": Decimal("1.0849"), "take_profit": Decimal("1.0851")})
    tight = service.assess(over)
    check(not tight.approved, "a stop that is not beyond entry is refused",
          "; ".join(tight.reasons[:1]))


def main() -> int:
    _refuse_project_database()
    tmp = _TMP
    from app import db, providers
    from app.broker import MT5Broker
    from app.claude import MockProvider
    from app.execution import ExecutionService
    from app.risk import RiskEngine

    print(f"EXECUTION DRY RUN — {datetime.now(UTC).isoformat()}")
    print(f"isolated database: {config.DB_PATH}")
    print("no terminal is contacted; order_send raises if it is ever reached\n")

    db.init_db()
    fake = FakeMt5()
    # The market is shut, so the desk's session window is opened for this run only, and
    # the switch is armed - both in the isolated database. The project database is never
    # opened by this script and keeps its PAUSED switch, which the negative controls
    # below still exercise through the real gates.
    real_session_open = config.session_open
    config.session_open = lambda now: True
    db.set_kill_switch("ARMED", "dry-run set-up")
    with db.tx() as con:
        soon = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
        con.execute(
            "INSERT INTO events(event_id,title,currency,ts_utc,actual,forecast,previous,"
            "impact,source_url,retrieved_at,quality) VALUES('dry-e1','FOMC Statement',"
            "'USD',?,NULL,NULL,NULL,'HIGH','https://example.invalid',?,?)",
            (soon, db.now_iso(), "DRY_RUN"))
    calendar = providers.calendar_status()
    print(f"calendar: {calendar['state']} "
          f"({calendar.get('reason') or 'verified, ' + str(calendar.get('events')) + ' rows'})")

    adapter = MT5Broker()
    adapter.mt5 = fake
    adapter.connected = True

    health, _ = stage_market_data(adapter)
    symbols = ["EURUSDm", "GBPUSDm", "XAUUSDm"]
    proposal_row = stage_research(adapter, MockProvider(), symbols)
    if proposal_row["decision"] not in ("TRADE", "PROPOSE"):
        print(f"\nthe mock CIO returned {proposal_row['decision']}; a dry run needs a "
              "trade proposal, so a fixed one is used for the remaining stages")
        proposal_row = _fixed_proposal()
    service = ExecutionService(adapter, RiskEngine())
    proposal, decision = stage_risk(service, proposal_row, health)
    stage_account_size(adapter, proposal, health)
    stage_adapter(adapter, fake, proposal, decision)

    state_broker = DryRunBroker()
    state_proposal = _fixed_proposal()
    state_decision = ExecutionService(state_broker, RiskEngine()).assess(state_proposal,
                                                                        health="HEALTHY")
    if not state_decision.approved:
        print("the fixed proposal was not approved: " + "; ".join(state_decision.reasons))
    stage_state_machine(state_broker, state_proposal, state_decision, tmp)
    stage_negatives(state_broker, state_proposal, state_decision)

    print("\n8. Trade review")
    _stage_review(state_broker, tmp)

    print(f"\nfinal checks")
    config.session_open = real_session_open
    check(fake.order_send_calls == [], "no order_send reached the package in any stage")
    check(str(config.DB_PATH).startswith(str(tmp)),
          "every row was written to the isolated database",
          str(config.DB_PATH))
    check(real_session_open(datetime.now(UTC)) is False,
          "the real session gate still reports the market closed at this hour")
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} dry-run checks passed")
    for failure in BLOCKING:
        print(f"  FAILED: {failure}")
    if not BLOCKING:
        print("The execution path is verified end to end up to the broker boundary. "
              "No order was sent, and none can be sent by this script.")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if BLOCKING else 0


def _fixed_proposal():
    """A deterministic proposal, for the stages the mock CIO's NO_TRADE cannot reach."""
    from app.models import TradeProposal
    return TradeProposal(
        created_at_utc=datetime.now(UTC), symbol="EURUSDm", direction="BUY",
        order_type="MARKET", timeframe="H1", stop_loss=Decimal("1.0800"),
        take_profit=Decimal("1.0950"), confidence=Decimal("0.6"),
        supporting_evidence=["dry run: H1 close above the prior range, timestamped"],
        department_report_ids=["dry-1"], strategy_version="dry-run-1", decision="TRADE")


def _stage_review(broker, tmp: Path) -> None:
    from app import db, learning
    from app.claude import MockProvider
    from app.models import TradeRecord
    trade = TradeRecord(
        ticket=9001, symbol="EURUSDm", direction="BUY", volume=Decimal("0.04"),
        open_price=Decimal("1.0850"), close_price=Decimal("1.0900"),
        open_time=datetime.now(UTC) - timedelta(hours=2), close_time=datetime.now(UTC),
        pnl=Decimal("20.00"))
    with db.tx() as con:
        con.execute("INSERT OR IGNORE INTO trades(ticket,symbol,direction,volume,"
                    "open_price,close_price,open_time,close_time,pnl,payload) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (trade.ticket, trade.symbol, trade.direction, str(trade.volume),
                     str(trade.open_price), str(trade.close_price),
                     trade.open_time.isoformat(), trade.close_time.isoformat(),
                     str(trade.pnl), trade.model_dump_json()))
    review = learning.audit_closed_trade(trade, MockProvider(), broker)
    check(review["process_quality"] in ("GOOD", "ACCEPTABLE", "POOR", "UNCLEAR"),
          "a closed trade is reviewed on process, not on profit", review["process_quality"])
    check("decision_context" in review and review["decision_context"] is not None,
          "the review carries the evidence that existed at entry")
    check(learning.performance()["trades"] == 1, "performance accounting sees the trade")


if __name__ == "__main__":
    raise SystemExit(main())
