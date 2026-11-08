"""UTC scheduler. Deterministic monitoring is frequent; Claude is called only for real work."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from . import data, db, departments, providers
from .execution import ExecutionService

CALENDAR_REFRESH_S = 6 * 3600
NEWS_REFRESH_S = 15 * 60

class Scheduler:
    def __init__(self, broker, provider, execution: ExecutionService, symbols: list[str]):
        self.broker = broker
        self.provider = provider
        self.execution = execution
        self.symbols = symbols
        self._last_snapshot: datetime | None = None
        self._last_research: datetime | None = None
        self._last_calendar: datetime | None = None
        self._last_news: datetime | None = None
        self._last_daily: str | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="ati-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                db.log("scheduler", "job_error", {"error": type(exc).__name__})
            await asyncio.sleep(60)

    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if self._last_snapshot is None or (now - self._last_snapshot).total_seconds() >= 300:
            data.store_snapshot(self.broker, self.symbols)
            self._last_snapshot = now
        # The refresh cadence comes from the source: a weekly public calendar is polled
        # hourly so a snapshot is never older than its own freshness window, which would
        # otherwise turn a healthy feed into a STALE one and block trading for hours.
        interval = providers.refresh_interval_s()
        if self._last_calendar is None or (now - self._last_calendar).total_seconds() >= interval:
            self._last_calendar = now
            self.refresh_calendar()
        if self._last_news is None or (now - self._last_news).total_seconds() >= NEWS_REFRESH_S:
            self._last_news = now
            self.refresh_news()
        if self._last_research is None or (now - self._last_research).total_seconds() >= 3600:
            self._last_research = now
            self.research()
        day = now.date().isoformat()
        if self._last_daily != day:
            self._last_daily = day
            self.daily()

    def refresh_calendar(self) -> None:
        """Pull the calendar when a source is configured. With none configured the
        blackout simply has no data, which is logged rather than papered over."""
        if not providers.calendar_provider().available():
            return
        try:
            result = providers.ingest_calendar(14)
            db.log("scheduler", "calendar_ingested", {
                "provider": result["provider"], "events": result["ingested"],
                "by_impact": result.get("by_impact"), "state": result["state"]})
        except Exception as exc:
            db.log("scheduler", "calendar_ingest_failed", {"error": type(exc).__name__})

    def refresh_news(self) -> None:
        """News is stale within minutes, so it is polled far more often than the
        calendar. Unconfigured or failing feeds are logged, never filled with guesses."""
        if not providers.news_provider().available():
            return
        try:
            providers.ingest_news(20)
        except Exception as exc:
            db.log("scheduler", "news_ingest_failed", {"error": type(exc).__name__})

    def research(self) -> dict | None:
        health, _ = data.market_health(self.broker, self.symbols)
        if health != "HEALTHY":
            db.log("scheduler", "research_skipped", {"health": health})
            return None
        if not self._may_spend_tokens():
            return None
        result = departments.run(self.provider, self.broker, self.symbols)
        proposal = result["proposal"]
        db.log("scheduler", "research_complete", {
            "decision": proposal["decision"], "symbol": proposal["symbol"]})
        if proposal["decision"] in ("TRADE", "PROPOSE"):
            from .models import TradeProposal
            parsed = TradeProposal.model_validate(proposal)
            decision = self.execution.assess(parsed, health=health)
            # Execution is reached only while the human has armed demo trading.
            if decision.approved and db.kill_switch_state() == "ARMED":
                self.execution.submit(parsed, decision, actor="scheduler")
        return result

    def _may_spend_tokens(self) -> bool:
        """An hourly job may not spend money on its own.

        The mock provider is free, so unattended research runs when it is in use. A
        provider that bills per token (Anthropic) is only called this way when the owner
        has said so explicitly with TRADER_ALLOW_PAID_AUTORESEARCH=1; otherwise the
        hourly job is skipped and logged. Pressing "Run research now" still works - that
        is an owner decision made in the moment.
        """
        if getattr(self.provider, "name", "") == "mock":
            return True
        if os.getenv("TRADER_ALLOW_PAID_AUTORESEARCH") == "1":
            return True
        db.log("scheduler", "research_skipped_paid_provider", {
            "provider": getattr(self.provider, "name", "unknown"),
            "reason": "autonomous research may not spend tokens without "
                      "TRADER_ALLOW_PAID_AUTORESEARCH=1"})
        return False

    def daily(self) -> None:
        from .learning import audit_all_closed
        try:
            audit_all_closed(self.broker, self.provider)
        except Exception as exc:
            db.log("scheduler", "learning_error", {"error": type(exc).__name__})
        self.execution.reconcile()