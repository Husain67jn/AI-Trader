"""Claude provider layer. Claude output is data: validated, budgeted, never trusted."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
from typing import Protocol

from . import db

# Tokens a single call is allowed to reserve before it runs. The reservation is
# written to the database under a lock, so concurrent callers cannot each see the
# same remaining balance and collectively overshoot the daily budget.
CALL_RESERVATION_TOKENS = 12000


class ProviderUnavailable(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class ProviderRefusal(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("response contained no JSON object")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("response JSON was not an object")
    return value


class Provider(Protocol):
    def complete_json(self, *, system: str, user: str, role: str = "") -> dict: ...


class MockProvider:
    """Offline provider. Returns safe NO_TRADE by default; fixtures are clearly synthetic."""
    name = "mock"

    def __init__(self, responses: dict[str, dict] | None = None):
        self.responses = responses or {}
        self.calls: list[str] = []

    def complete_json(self, *, system: str, user: str, role: str = "") -> dict:
        self.calls.append(role)
        if role in self.responses:
            return self.responses[role]
        now = db.now_iso()
        if role == "learning":
            return {
                "process_quality": "UNCLEAR", "outcome_vs_process": "Not assessed (mock)",
                "thesis_coherent": True, "evidence_timestamped": False,
                "regime_changed": False, "entry_consistent": True,
                "stop_target_policy_followed": True, "execution_quality": "UNKNOWN",
                "variance_or_process": "UNCLEAR",
                "hypothesis": "Mock review: no hypothesis generated.",
                "next_test": "Connect Claude API and re-run the audit.",
            }
        if role == "cio":
            return {
                "created_at_utc": now, "symbol": "EURUSD", "direction": "BUY",
                "order_type": "MARKET", "timeframe": "H1", "entry_price": None,
                "stop_loss": 0, "take_profit": 0, "risk_reward": 0, "confidence": 0,
                "market_regime": "UNKNOWN", "supporting_evidence": [],
                "contradicting_evidence": ["No verified Claude API provider configured."],
                "economic_event_risk": "HIGH", "invalidation_conditions": [],
                "department_report_ids": [], "strategy_version": "mock-0",
                "decision": "NO_TRADE",
            }
        return {
            "department": role, "as_of_utc": now, "data_sources": ["mock"],
            "data_quality": "UNKNOWN", "facts": [], "interpretations": [],
            "uncertainties": ["Mock provider: no live analysis was performed."],
            "bullish_case": "", "bearish_case": "", "neutral_case": "",
            "recommendation": "INSUFFICIENT_DATA", "invalidation_conditions": [],
        }


# The budget is one per process, so the lock must be too: two provider instances in
# the same process share the same daily counter.
# ponytail: process-wide lock only. A second worker process would need a DB-level
# reservation (BEGIN IMMEDIATE + conditional UPDATE); single uvicorn worker today.
_BUDGET_LOCK = threading.Lock()


class AnthropicProvider:
    """Official Anthropic SDK. No UI scraping, no shared terminal session."""
    name = "anthropic"

    def __init__(self, model: str | None = None, client=None):
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ProviderUnavailable("anthropic SDK is not installed") from exc
            if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
                raise ProviderUnavailable("no Anthropic credential configured")
            client = anthropic.Anthropic(timeout=120.0, max_retries=2)
        # An injected client is a test seam for the offline integration tests. It runs
        # through this exact code path: budget, usage accounting, JSON extraction.
        self.client = client
        self.model = model or os.getenv("ANTHROPIC_MODEL") or "claude-opus-5-5"
        self.budget = int(os.getenv("CLAUDE_DAILY_TOKEN_BUDGET", "200000"))
        self._lock = _BUDGET_LOCK

    def _usage_today(self) -> tuple[str, int]:
        today = date.today().isoformat()
        raw = db.get_setting("claude_usage")
        if raw:
            try:
                data = json.loads(raw)
                if data.get("date") == today:
                    return today, int(data.get("tokens", 0))
            except (ValueError, TypeError):
                pass
        return today, 0

    def _reserve(self, tokens: int) -> int:
        """Atomically claim budget before the call. Raises rather than overrunning."""
        with self._lock:
            today, used = self._usage_today()
            if used >= self.budget:
                raise BudgetExceeded(f"daily Claude token budget reached ({self.budget})")
            reserved = min(tokens, self.budget - used)
            db.set_setting("claude_usage",
                           json.dumps({"date": today, "tokens": used + reserved}),
                           actor="claude")
            return reserved

    def _settle(self, reserved: int, actual: int) -> None:
        """Replace the reservation with what the call actually cost."""
        with self._lock:
            today, used = self._usage_today()
            db.set_setting("claude_usage",
                           json.dumps({"date": today,
                                       "tokens": max(0, used - reserved + actual)}),
                           actor="claude")

    def complete_json(self, *, system: str, user: str, role: str = "") -> dict:
        reserved = self._reserve(CALL_RESERVATION_TOKENS)
        started = time.monotonic()
        try:
            message = self.client.messages.create(
                model=self.model, max_tokens=8192, system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception:
            self._settle(reserved, 0)  # release the claim; nothing was returned
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(message, "usage", None)
        tokens = int(getattr(usage, "input_tokens", 0)) + int(getattr(usage, "output_tokens", 0))
        self._settle(reserved, tokens)
        if getattr(message, "stop_reason", None) == "refusal":
            raise ProviderRefusal("Claude refused the request")
        text = "".join(block.text for block in message.content if block.type == "text")
        db.log("claude", "completion", {
            "role": role, "model": self.model, "request_id": getattr(message, "id", None),
            "latency_ms": latency_ms, "reserved_tokens": reserved,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "stop_reason": getattr(message, "stop_reason", None),
        })
        return _extract_json(text)


def get_provider(prefer_mock: bool = False) -> Provider:
    if prefer_mock or os.getenv("TRADER_LLM_PROVIDER", "").lower() == "mock":
        return MockProvider()
    try:
        return AnthropicProvider()
    except ProviderUnavailable:
        return MockProvider()