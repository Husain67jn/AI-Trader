"""Three memory layers, post-trade review, and gated strategy promotion."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import db
from .claude import Provider
from .models import TradeRecord

MIN_PROMOTION_SAMPLE = 100


class TradeReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    process_quality: Literal["GOOD", "ACCEPTABLE", "POOR", "UNCLEAR"]
    outcome_vs_process: str
    thesis_coherent: bool
    evidence_timestamped: bool
    regime_changed: bool
    entry_consistent: bool
    stop_target_policy_followed: bool
    execution_quality: str
    variance_or_process: Literal["VARIANCE", "PROCESS_PROBLEM", "UNCLEAR"]
    hypothesis: str
    next_test: str


REVIEW_SYSTEM = (
    "You audit a closed demo Forex/Gold trade. Judge process quality separately "
    "from profit and loss: a profitable trade can be a poor decision. Do not invent "
    "facts. Return one JSON object only, matching the supplied schema."
)


def decision_context(trade: TradeRecord) -> dict:
    """The evidence the trade was actually taken on, read back from the database.

    A review that only sees the entry and exit prices judges the outcome, not the
    decision. This returns the proposal, the department reports it cited, the risk
    decision and the market snapshot from before the entry.
    """
    context: dict = {"order": None, "proposal": None, "risk_decision": None,
                     "department_reports": [], "market_before_entry": None}
    orders = db.query("SELECT * FROM orders WHERE ticket=?", (trade.ticket,))
    if not orders:
        return context
    order = orders[0]
    context["order"] = {"request_id": order["request_id"], "state": order["state"],
                        "created_at": order["created_at"]}
    proposal_id = order["proposal_id"]
    proposals = db.query("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,))
    if proposals:
        try:
            context["proposal"] = json.loads(proposals[0]["payload"])
        except ValueError:
            context["proposal"] = None
        report_ids = (context["proposal"] or {}).get("department_report_ids") or []
        if report_ids:
            marks = ",".join("?" * len(report_ids))
            rows = db.query(
                "SELECT department, payload FROM reports WHERE report_id IN (" + marks + ")",
                tuple(report_ids))
            context["department_reports"] = [
                {"department": row["department"], "report": json.loads(row["payload"])}
                for row in rows]
    decisions = db.query("SELECT * FROM risk_decisions WHERE proposal_id=? "
                         "ORDER BY id DESC LIMIT 1", (proposal_id,))
    if decisions:
        context["risk_decision"] = {
            "approved": bool(decisions[0]["approved"]),
            "reasons": json.loads(decisions[0]["reasons"]),
            "checks": json.loads(decisions[0]["checks"]),
            "created_at": decisions[0]["created_at"],
        }
    # The most recent snapshot that predates the entry: what the desk could have seen.
    snaps = db.query("SELECT * FROM market_snapshots WHERE symbol=? AND ts<=? "
                     "ORDER BY ts DESC LIMIT 1",
                     (trade.symbol, trade.open_time.isoformat()))
    if snaps:
        context["market_before_entry"] = {"ts": snaps[0]["ts"],
                                          "quote": json.loads(snaps[0]["payload"])}
    return context


def audit_closed_trade(trade: TradeRecord, provider: Provider,
                       broker=None) -> dict:
    existing = db.query("SELECT * FROM reviews WHERE ticket=?", (trade.ticket,))
    if existing:
        return json.loads(existing[0]["payload"])
    schema = json.dumps(TradeReview.model_json_schema(), separators=(",", ":"))
    context = decision_context(trade)
    user = (
        "Closed trade (untrusted data):\n<untrusted_data>\n"
        + trade.model_dump_json()
        + "\n\nEvidence available when the trade was taken:\n"
        + json.dumps(context, default=str)
        + "\n</untrusted_data>\nReturn the review JSON."
    )
    try:
        raw = provider.complete_json(system=REVIEW_SYSTEM + "\nSchema:\n" + schema,
                                     user=user, role="learning")
        review = TradeReview.model_validate(raw)
    except Exception as exc:
        review = TradeReview(
            process_quality="UNCLEAR", outcome_vs_process="Review unavailable",
            thesis_coherent=False, evidence_timestamped=False, regime_changed=False,
            entry_consistent=False, stop_target_policy_followed=False,
            execution_quality="UNKNOWN", variance_or_process="UNCLEAR",
            hypothesis=f"No valid review produced ({type(exc).__name__}).",
            next_test="Re-run review when the provider is available.",
        )
    payload = {"ticket": trade.ticket, "reviewed_at_utc": db.now_iso(),
               "pnl": str(trade.pnl), "decision_context": context,
               **review.model_dump(mode="json")}
    with db.tx() as con:
        con.execute("INSERT OR REPLACE INTO reviews(ticket, created_at, payload) VALUES(?,?,?)",
                    (trade.ticket, db.now_iso(), json.dumps(payload, default=str)))
        db.audit(con, "learning_officer", "trade_reviewed",
                 {"ticket": trade.ticket, "process_quality": review.process_quality,
                  "had_decision_context": bool(context["proposal"])})
    return payload


def audit_all_closed(broker, provider: Provider, days: int = 30) -> list[dict]:
    results = []
    for trade in broker.closed_trades(days):
        with db.tx() as con:
            con.execute(
                "INSERT OR IGNORE INTO trades(ticket,symbol,direction,volume,open_price,"
                "close_price,open_time,close_time,pnl,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (trade.ticket, trade.symbol, trade.direction, str(trade.volume),
                 str(trade.open_price), str(trade.close_price),
                 trade.open_time.isoformat(), trade.close_time.isoformat(),
                 str(trade.pnl), trade.model_dump_json()),
            )
        results.append(audit_closed_trade(trade, provider, broker))
    return results


def create_experiment(name: str, config: dict) -> int:
    with db.tx() as con:
        cur = con.execute(
            "INSERT INTO experiments(name, version, config, status, metrics, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (name, "1", json.dumps(config), "PROPOSED", "{}", db.now_iso()),
        )
        experiment_id = cur.lastrowid
        db.audit(con, "learning_officer", "experiment_created",
                 {"id": experiment_id, "name": name})
    return experiment_id


def record_experiment_results(experiment_id: int, metrics: dict) -> None:
    with db.tx() as con:
        con.execute("UPDATE experiments SET metrics=?, status='MEASURED' WHERE id=?",
                    (json.dumps(metrics), experiment_id))
        db.audit(con, "learning_officer", "experiment_measured",
                 {"id": experiment_id})


def promote_experiment(experiment_id: int, human_approved: bool, reviewer: str) -> dict:
    """An experiment can never promote itself; every gate must be evidenced."""
    rows = db.query("SELECT * FROM experiments WHERE id=?", (experiment_id,))
    if not rows:
        return {"promoted": False, "reasons": ["experiment not found"]}
    metrics = json.loads(rows[0]["metrics"] or "{}")
    gates = {
        "backtest": bool(metrics.get("backtest")),
        "out_of_sample": bool(metrics.get("out_of_sample")),
        "walk_forward": bool(metrics.get("walk_forward")),
        "robustness": bool(metrics.get("robustness")),
        "demo_forward": bool(metrics.get("demo_forward")),
        "sample_size": int(metrics.get("trades", 0)) >= MIN_PROMOTION_SAMPLE,
        "human_review": bool(human_approved),
    }
    reasons = [name for name, passed in gates.items() if not passed]
    promoted = not reasons
    with db.tx() as con:
        con.execute("UPDATE experiments SET status=? WHERE id=?",
                    ("PROMOTED" if promoted else "REJECTED", experiment_id))
        db.audit(con, reviewer, "experiment_promotion", {
            "experiment_id": experiment_id, "promoted": promoted,
            "reasons": reasons, "human_approved": human_approved})
    return {"promoted": promoted, "reasons": reasons, "gates": gates}


def register_strategy(version: str, spec: dict) -> dict:
    existing = db.query("SELECT * FROM strategy_versions WHERE version=?", (version,))
    if existing:
        if json.loads(existing[0]["spec"]) != spec:
            raise ValueError("strategy versions are immutable; use a new version id")
        return {"version": version, "created": False}
    with db.tx() as con:
        con.execute("INSERT INTO strategy_versions(version, spec, status, created_at) "
                    "VALUES(?,?,?,?)",
                    (version, json.dumps(spec), "EXPERIMENTAL", db.now_iso()))
        db.audit(con, "learning_officer", "strategy_registered", {"version": version})
    return {"version": version, "created": True}


def recent_reviews(limit: int = 20) -> list[dict]:
    rows = db.query("SELECT * FROM reviews ORDER BY id DESC LIMIT ?", (limit,))
    return [json.loads(row["payload"]) for row in rows]


def performance() -> dict:
    rows = db.query("SELECT * FROM trades")
    pnls = [float(row["pnl"]) for row in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    return {
        "trades": len(pnls),
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "average_win": round(sum(wins) / len(wins), 2) if wins else None,
        "average_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "expectancy": round(sum(pnls) / len(pnls), 4) if pnls else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else None,
        "note": "Small samples are not evidence of an edge.",
    }