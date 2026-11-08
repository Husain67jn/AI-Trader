"""Execution state machine. Submit once, verify broker state, never blind-retry."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import config, data, db, providers
from .broker import HistoryUnavailable
from .models import OrderResult, RiskDecision, TradeProposal
from .risk import RiskEngine, record_decision

STATES = ["PROPOSED", "RISK_REJECTED", "RISK_APPROVED", "SUBMISSION_PENDING",
          "ACKNOWLEDGED", "FILLED", "PARTIALLY_FILLED", "REJECTED", "CANCELED",
          "EXPIRED", "UNKNOWN", "RECONCILED", "ABSENT_UNVERIFIED"]
TRANSITIONS = {
    "SUBMISSION_PENDING": {"ACKNOWLEDGED", "FILLED", "PARTIALLY_FILLED", "REJECTED", "UNKNOWN"},
    "ACKNOWLEDGED": {"FILLED", "PARTIALLY_FILLED", "REJECTED", "CANCELED", "EXPIRED", "UNKNOWN"},
    "UNKNOWN": {"ACKNOWLEDGED", "FILLED", "PARTIALLY_FILLED", "RECONCILED", "REJECTED",
                "CANCELED", "EXPIRED"},
    "FILLED": {"RECONCILED"},
    "PARTIALLY_FILLED": {"RECONCILED", "FILLED"},
}
# States that must be resolved against the broker before anything new is submitted.
UNRESOLVED_STATES = ("SUBMISSION_PENDING", "ACKNOWLEDGED", "UNKNOWN")
# Request ids currently between "row written" and "broker answered". Reconcile must not
# settle one of these: the MT5 adapter has no cancellable call, so an age test alone
# cannot tell "crashed" from "still waiting on a hung terminal".
_IN_FLIGHT: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()
# Broker states that mean the order will never fill.
BROKER_DEAD_STATES = ("CANCELED", "REJECTED", "EXPIRED")


def unresolved_orders() -> list[str]:
    """Request ids whose outcome is not yet known, oldest first."""
    rows = db.query(
        "SELECT request_id FROM orders WHERE state IN ({}) ORDER BY created_at".format(
            ",".join("?" * len(UNRESOLVED_STATES))), UNRESOLVED_STATES)
    return [row["request_id"] for row in rows]


def unverified_orders() -> list[str]:
    """Orders settled as absent when the lookup could not rule out that they executed.

    Reported, never blocking: an order nobody can verify would otherwise wedge the
    submission queue for good. Every surface that shows unresolved orders must show
    these too, or a questionable row is indistinguishable from a clean one.
    """
    rows = db.query("SELECT request_id FROM orders WHERE state='ABSENT_UNVERIFIED' "
                    "ORDER BY created_at")
    return [row["request_id"] for row in rows]


def transition_allowed(previous: str | None, state: str, verified: bool) -> bool:
    """Is this move legal? TRANSITIONS is a guard here, not documentation.

    A move the table does not list is refused and audited instead of written, so one
    mistaken write cannot silently re-wedge the queue that H2 unwedged. Three moves
    are always allowed: a row that does not exist yet, a rewrite of the same state,
    and anything carrying the broker's own verdict (a verified result outranks a
    locally concluded state).
    """
    if previous is None or previous == state or verified:
        return True
    if previous in UNRESOLVED_STATES or previous == "ABSENT_UNVERIFIED":
        return True
    return state in TRANSITIONS.get(previous, ())


class ExecutionService:
    def __init__(self, broker, engine: RiskEngine | None = None):
        self.broker = broker
        self.engine = engine or RiskEngine()

    # --- risk ---------------------------------------------------------------
    def assess(self, proposal: TradeProposal, health: str | None = None) -> RiskDecision:
        try:
            account = self.broker.account()
            instrument = self.broker.instrument(proposal.symbol)
            quote = self.broker.quote(proposal.symbol)
            if account is None or instrument is None or quote is None:
                decision = RiskDecision(approved=False,
                                        reasons=["account, instrument or quote unavailable"])
                record_decision(proposal.proposal_id, decision)
                return decision
            if health is None:
                health = data.market_health(self.broker, [proposal.symbol])[0]
            day_start, week_start = data.equity_baselines(account.equity, str(account.login))
            base = config.canonical_base(proposal.symbol) or ""
            blackout, _ = data.blackout_active([base[:3], base[3:6]])
            try:
                calendar_state = providers.calendar_status()["state"]
            except Exception:  # noqa: BLE001 - unknown status is not a safe default
                calendar_state = "UNKNOWN"
            price = proposal.entry_price or (
                quote.ask if proposal.direction == "BUY" else quote.bid)
            decision = self.engine.assess(
                proposal=proposal, account=account, instrument=instrument,
                quote=quote, positions=self.broker.positions(), health=health,
                start_of_day_equity=day_start, start_of_week_equity=week_start,
                margin_for_volume=lambda volume: self.broker.calc_margin(
                    proposal.symbol, proposal.direction, volume, price),
                blackout=blackout,
                peak_equity=data.peak_equity(account.equity, str(account.login)),
                unresolved_orders=len(unresolved_orders()),
                slippage_points=config.DEFAULT_RISK_LIMITS["max_slippage_points"],
                exposure_fn=lambda volume: self._exposure_pct(
                    account, proposal.symbol, quote.ask, volume),
                calendar_state=calendar_state,
            )
            record_decision(proposal.proposal_id, decision)
            return decision
        except Exception as exc:  # fail closed
            decision = RiskDecision(
                approved=False, reasons=[f"execution pre-check failed: {type(exc).__name__}"])
            try:
                record_decision(proposal.proposal_id, decision)
            except Exception:
                pass
            return decision

    def _exposure_pct(self, account, symbol: str, price: Decimal, volume: Decimal) -> Decimal:
        """Notional of everything open plus this order, as a percentage of equity."""
        notional = Decimal("0")
        for position in self.broker.positions():
            instrument = self.broker.instrument(position.symbol)
            if instrument is None:
                raise ValueError(f"unknown instrument {position.symbol}")
            notional += position.volume * instrument.contract_size * position.price_open
        instrument = self.broker.instrument(symbol)
        if instrument is None:
            raise ValueError(f"unknown instrument {symbol}")
        notional += volume * instrument.contract_size * Decimal(price)
        if account.equity <= 0:
            raise ValueError("non-positive equity")
        return ((notional / account.equity) * 100).quantize(Decimal("0.01"))

    # --- execution ----------------------------------------------------------
    def submit(self, proposal: TradeProposal, decision: RiskDecision,
               request_id: str | None = None, actor: str = "system") -> dict:
        request_id = request_id or proposal.proposal_id
        try:
            existing = db.query("SELECT * FROM orders WHERE request_id=?", (request_id,))
            if existing:
                row = existing[0]
                return {"ok": row["state"] in ("FILLED", "PARTIALLY_FILLED", "ACKNOWLEDGED"),
                        "state": row["state"], "request_id": request_id,
                        "ticket": row["ticket"], "duplicate": True,
                        "message": "request already submitted; no retry"}
            if not decision.approved:
                return self._blocked(request_id, "risk decision was not approved")
            if not config.DEMO_ONLY:
                return self._blocked(request_id, "live trading is not implemented")
            from .broker import REAL_MT5
            if getattr(self.broker, "kind", None) != REAL_MT5:
                # Belt and braces: nothing can arm a research-only broker, and this is the
                # money boundary, so it is checked again here rather than trusted.
                return self._blocked(
                    request_id, "broker is research-only; only a live MT5 demo session may submit")
            account = self.broker.account()
            if account is None or account.trade_mode != "DEMO":
                return self._blocked(request_id, "demo account not positively verified")
            state = db.kill_switch_state()
            if state != "ARMED":
                return self._blocked(request_id, f"kill switch is {state}")
            if config.canonical_base(proposal.symbol) is None:
                return self._blocked(
                    request_id, f"symbol {proposal.symbol} is not on the tradable whitelist")
            instrument = self.broker.instrument(proposal.symbol)
            quote = self.broker.quote(proposal.symbol)
            if instrument is None or quote is None:
                return self._blocked(request_id, "instrument or quote unavailable")
            age = (datetime.now(timezone.utc) - quote.ts_utc).total_seconds()
            if age > config.DEFAULT_RISK_LIMITS["max_quote_age_s"] or age < -5:
                return self._blocked(request_id, "quote is stale")
            if proposal.order_type != "MARKET":
                return self._blocked(request_id, "only MARKET orders are implemented")
            if not db.ping():
                return self._blocked(request_id, "database unavailable")
            pending = unresolved_orders()
            if pending:
                return self._blocked(
                    request_id,
                    f"{len(pending)} unresolved order(s) must be reconciled first")
            # Recalculate on fresh data immediately before submission: the caller's
            # decision may have been produced minutes ago, and it may have been
            # constructed by hand. Only this result is allowed to size the order.
            fresh = self.assess(proposal)
            if not fresh.approved or not fresh.volume:
                return self._blocked(
                    request_id,
                    "risk revalidation failed: " + "; ".join(fresh.reasons or ["no volume"]))
            if decision.approved and decision.volume and fresh.volume != decision.volume:
                db.log("risk_engine", "volume_revised_at_submission", {
                    "request_id": request_id, "was": str(decision.volume),
                    "now": str(fresh.volume)})
            decision = fresh
        except Exception as exc:
            return {"ok": False, "state": "BLOCKED", "request_id": request_id,
                    "ticket": None, "message": f"pre-submit failure: {type(exc).__name__}"}

        price = quote.ask if proposal.direction == "BUY" else quote.bid
        request = {
            "request_id": request_id, "symbol": proposal.symbol,
            "direction": proposal.direction, "volume": decision.volume,
            "price": price, "sl": proposal.stop_loss, "tp": proposal.take_profit,
            "deviation": config.DEFAULT_RISK_LIMITS["max_slippage_points"],
            "magic": config.MAGIC,
            "comment": f"{config.ORDER_COMMENT}-{proposal.proposal_id[:16]}",
        }
        # Registered before the row exists, not after it: reconcile must never see a
        # request id that is between "row written" and "broker answered" and decide it
        # from age alone.
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.add(request_id)
        try:
            inserted = False
            try:
                self._insert_pending(request_id, proposal, request)
                inserted = True
                ok, message = self.broker.check_order(request)
                if not ok:
                    self._finish(request_id, "REJECTED", message=message)
                    return {"ok": False, "state": "REJECTED", "request_id": request_id,
                            "ticket": None, "message": message}
            except Exception as exc:
                # check_order only validates: nothing reached the market. A row left in
                # SUBMISSION_PENDING would block every later submission for good.
                if inserted:
                    try:
                        self._finish(request_id, "REJECTED",
                                     message=f"pre-submit failure: {type(exc).__name__}")
                    except Exception:
                        pass
                return self._blocked(
                    request_id,
                    f"pre-submit database/broker failure: {type(exc).__name__}")

            # The switch can move while check_order round-trips to the terminal. It is
            # read again here, immediately before the send, so the operator's last word
            # wins. The adapter reads it once more right before order_send.
            kill_state = db.kill_switch_state()
            if kill_state != "ARMED":
                message = f"kill switch changed to {kill_state} during submission"
                try:
                    self._finish(request_id, "REJECTED", message=message)
                except Exception:
                    pass
                return self._blocked(request_id, message)

            try:
                result = self.broker.submit(request)
            except TimeoutError:
                return self._reconcile_timeout(request_id, proposal)
            except Exception as exc:
                return self._reconcile_timeout(request_id, proposal,
                                               error=f"{type(exc).__name__}: {exc}")
        finally:
            with _IN_FLIGHT_LOCK:
                _IN_FLIGHT.discard(request_id)

        try:
            state = self._verified_state(result)
        except Exception as exc:
            # The order may exist at the broker and we failed to read it back: pause,
            # keep the ticket, and leave the row unresolved for reconciliation.
            state = "UNKNOWN"
            db.set_kill_switch("PAUSED", actor)
            db.log(actor, "alert", {"kind": "order_verification_failed",
                                    "request_id": request_id, "ticket": result.ticket,
                                    "error": type(exc).__name__})
        if state == "UNKNOWN":
            db.set_kill_switch("PAUSED", actor)
            db.log(actor, "alert", {"kind": "unknown_order_state",
                                    "request_id": request_id})
        self._finish(request_id, state, result=result)
        db.log(actor, "order_submitted", {
            "request_id": request_id, "proposal_id": proposal.proposal_id,
            "state": state, "ticket": result.ticket, "volume": str(result.filled_volume),
        })
        return {"ok": state in ("FILLED", "PARTIALLY_FILLED", "ACKNOWLEDGED"),
                "state": state, "request_id": request_id, "ticket": result.ticket,
                "message": result.message}

    def _verified_state(self, result: OrderResult) -> str:
        """An acknowledgement is not a fill; ask the broker whenever a ticket exists."""
        if result.ticket:
            verified = self.broker.order_status(result.ticket)
            if verified in ("FILLED", "PARTIALLY_FILLED"):
                return verified
            if verified in ("PENDING", "STARTED"):
                return "ACKNOWLEDGED"
            if verified in BROKER_DEAD_STATES:
                # Cancelled, rejected or expired: definitively not a fill, and never
                # silently reported as one.
                return "REJECTED" if verified == "REJECTED" else verified
            if verified == "UNKNOWN":
                return "UNKNOWN"
        if not result.ok:
            return "REJECTED" if result.state == "REJECTED" else "UNKNOWN"
        if result.state in ("ACKNOWLEDGED", "PENDING"):
            return "ACKNOWLEDGED"
        if result.state in STATES:
            return result.state
        return "UNKNOWN"

    def _reconcile_timeout(self, request_id: str, proposal: TradeProposal,
                           error: str = "timeout") -> dict:
        found = self.broker.order_by_request(request_id)
        if found is not None:
            state = self._verified_state(found)
            self._finish(request_id, state, result=found)
            db.log("execution", "reconciled_after_timeout", {
                "request_id": request_id, "state": state, "ticket": found.ticket,
                "error": error})
            return {"ok": state in ("FILLED", "PARTIALLY_FILLED", "ACKNOWLEDGED"),
                    "state": state, "request_id": request_id, "ticket": found.ticket,
                    "message": "reconciled after timeout; no resubmission"}
        db.set_kill_switch("PAUSED", "execution")
        db.log("execution", "alert", {"kind": "order_status_unknown",
                                      "request_id": request_id, "error": error,
                                      "action": "trading paused; no blind retry"})
        self._finish(request_id, "UNKNOWN")
        return {"ok": False, "state": "UNKNOWN", "request_id": request_id,
                "ticket": None, "message": "status unknown; trading paused, not retried"}

    def _insert_pending(self, request_id: str, proposal: TradeProposal, request: dict) -> None:
        now = db.now_iso()
        with db.tx() as con:
            con.execute(
                "INSERT INTO orders(request_id, proposal_id, state, payload, ticket, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (request_id, proposal.proposal_id, "SUBMISSION_PENDING",
                 json.dumps(request, default=str), None, now, now),
            )
            con.execute("UPDATE proposals SET outcome='SUBMITTED' WHERE proposal_id=?",
                        (proposal.proposal_id,))
            db.audit(con, "execution", "submission_pending", {"request_id": request_id})

    def _finish(self, request_id: str, state: str, result: OrderResult | None = None,
                message: str = "") -> None:
        """Write a row's outcome, its proposal's outcome and the audit entry together.

        The move is checked against TRANSITIONS first: an illegal one is refused and
        audited rather than written, and the audit always names the state it replaced.
        """
        with db.tx() as con:
            row = con.execute("SELECT state FROM orders WHERE request_id=?",
                              (request_id,)).fetchone()
            previous = row["state"] if row else None
            if not transition_allowed(previous, state, verified=result is not None):
                db.audit(con, "execution", "order_transition_refused", {
                    "request_id": request_id, "from": previous, "to": state,
                    "message": message})
                return
            con.execute(
                "UPDATE orders SET state=?, ticket=?, updated_at=?, "
                "payload=json_set(payload, '$.result', ?) WHERE request_id=?",
                (state, result.ticket if result else None, db.now_iso(),
                 json.dumps(result.model_dump(mode="json") if result else {"message": message},
                            default=str), request_id),
            )
            con.execute("UPDATE proposals SET outcome=? WHERE proposal_id="
                        "(SELECT proposal_id FROM orders WHERE request_id=?)",
                        (state, request_id))
            db.audit(con, "execution", "order_state", {
                "request_id": request_id, "state": state, "replaced": previous,
                "message": message})

    def _blocked(self, request_id: str, reason: str) -> dict:
        db.log("execution", "order_blocked", {"request_id": request_id, "reason": reason})
        return {"ok": False, "state": "BLOCKED", "request_id": request_id,
                "ticket": None, "message": reason}

    # --- reconciliation -----------------------------------------------------
    def reconcile(self) -> dict:
        """Compare internal state with the broker, without confusing "closed" with "lost".

        A filled order whose position has since closed is normal; its ticket appears in
        the broker's deal history. Only a ticket that is neither open nor in history is a
        real discrepancy. Unresolved internal states are reported separately and block
        new submissions until they clear.
        """
        try:
            settled = self._settle_unresolved()
            filled = db.query("SELECT * FROM orders WHERE state IN "
                              "('FILLED','PARTIALLY_FILLED') AND ticket IS NOT NULL")
            internal = {int(row["ticket"]): row["request_id"] for row in filled}
            open_tickets = {p.ticket for p in self.broker.positions()}
            history_tickets = {t.ticket for t in self.broker.closed_trades(30)}
            missing = sorted(t for t in internal if t not in open_tickets
                             and t not in history_tickets)
            untracked = sorted(open_tickets - set(internal))
            pending = unresolved_orders()
            unverified = unverified_orders()

            closed_elsewhere = sorted(t for t in internal
                                      if t not in open_tickets and t in history_tickets)
            if closed_elsewhere:
                self._mark_reconciled(closed_elsewhere)

            discrepancies = {"missing_at_broker": missing, "untracked_at_broker": untracked,
                             "unresolved_orders": pending,
                             "settled_orders": settled,
                             "unverified_orders": unverified,
                             "closed_since_submission": closed_elsewhere}
            blocked = bool(missing or untracked or pending)
            if blocked:
                db.set_kill_switch("PAUSED", "reconciliation")
                db.log("reconciliation", "alert", {"kind": "position_mismatch",
                                                   **discrepancies})
            elif unverified:
                # Not blocked - an order nobody can verify must not wedge the queue - but
                # never quiet either: a runtime alert, not only the startup one.
                db.log("reconciliation", "alert", {"kind": "orders_absent_unverified",
                                                   "orders": unverified})
            else:
                db.log("reconciliation", "reconciled", {
                    "positions": len(open_tickets), "closed": len(closed_elsewhere)})
            return {"ok": not blocked, **discrepancies}
        except Exception as exc:
            db.set_kill_switch("PAUSED", "reconciliation")
            db.log("reconciliation", "alert", {"kind": "reconciliation_error",
                                               "error": type(exc).__name__})
            return {"ok": False, "error": type(exc).__name__}

    def _settle_unresolved(self, min_age_s: int = 120) -> list[str]:
        """Close the crash window: ask the broker about orders whose state we never wrote.

        Rows younger than `min_age_s` are skipped so a submission in flight is never
        clobbered, and so is any request this process is still waiting on (a hung MT5
        call cannot be cancelled, so age alone cannot tell it from a crash).

        Evidence decides the state, in this order:
          - found at the broker   -> its verified state, ticket preserved (a filled order
            whose position has since closed is found in order *history*);
          - absent, and the lookup covered everything the adapter can see
            -> RECONCILED (never executed);
          - absent, but the lookup could not cover everything - the row predates the
            history the adapter can search, or the terminal could not answer the history
            query at all -> ABSENT_UNVERIFIED: reported and alerted on, never silently
            called clean.
        A settle that itself fails leaves the row blocking, loudly.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=min_age_s)).isoformat()
        rows = db.query(
            "SELECT request_id, created_at FROM orders WHERE state IN ({}) AND updated_at < ?"
            .format(",".join("?" * len(UNRESOLVED_STATES))),
            (*UNRESOLVED_STATES, cutoff))
        settled = []
        for row in rows:
            request_id = row["request_id"]
            # The lock is released before any I/O: a contended database must not stall
            # every submission's registration just because a settle is talking.
            with _IN_FLIGHT_LOCK:
                in_flight = request_id in _IN_FLIGHT
            if in_flight:
                db.log("reconciliation", "settle_skipped_in_flight",
                       {"request_id": request_id})
                continue
            try:
                created = datetime.fromisoformat(row["created_at"])
                window = int(getattr(self.broker, "history_window_days", 0) or 0)
                try:
                    found = (self.broker.order_by_request(request_id, since=created)
                             if window else self.broker.order_by_request(request_id))
                    unavailable = ""
                except HistoryUnavailable as exc:
                    # The lookup could not look. Absence is not evidence, so the row is
                    # reported as unverified rather than called clean - and it does not
                    # block, because a broken terminal must not wedge the queue.
                    found, unavailable = None, str(exc)
                if found is not None:
                    self._finish(request_id, self._verified_state(found), result=found)
                    settled.append(request_id)
                    continue
                # Absence is conclusive only when the lookup could see every source.
                # A broker with no external history (the mock) records everything it
                # executes itself; MT5 must also have had the row inside its window and
                # must have answered the query.
                covered = (not unavailable
                           and (window == 0
                                or created >= datetime.now(timezone.utc)
                                - timedelta(days=window)))
                state = "RECONCILED" if covered else "ABSENT_UNVERIFIED"
                action = "order_settled_absent" if covered else "order_absent_unverified"
                detail = {"request_id": request_id, "history_window_days": window}
                if unavailable:
                    detail["history_unavailable"] = unavailable
                with db.tx() as con:
                    changed = con.execute(
                        "UPDATE orders SET state=?, updated_at=? WHERE request_id=? "
                        "AND state IN ({})".format(",".join("?" * len(UNRESOLVED_STATES))),
                        (state, db.now_iso(), request_id, *UNRESOLVED_STATES)).rowcount
                    if changed:
                        db.audit(con, "reconciliation", action, detail)
                    else:
                        # Another writer finished this row while we were looking; its
                        # state is newer than our evidence, so it wins.
                        db.audit(con, "reconciliation", "settle_skipped",
                                 {"request_id": request_id})
                if changed:
                    settled.append(request_id)
            except Exception as exc:  # left unresolved: it still blocks, loudly
                db.log("reconciliation", "settle_failed",
                       {"request_id": request_id, "error": type(exc).__name__})
        return settled

    def _mark_reconciled(self, tickets: list[int]) -> None:
        with db.tx() as con:
            for ticket in tickets:
                con.execute("UPDATE orders SET state='RECONCILED', updated_at=? "
                            "WHERE ticket=?", (db.now_iso(), ticket))
            db.audit(con, "reconciliation", "positions_closed", {"tickets": tickets})