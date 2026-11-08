"""Configuration. Everything here is code-owned; no env switch can enable live trading."""
from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT / "prompts"
STATIC_DIR = ROOT / "app" / "static"


def load_dotenv(path: Path | None = None) -> None:
    """Tiny .env loader (KEY=VALUE, comments, no interpolation). Stdlib only.

    `TRADER_ENV_FILE` may point at another file (used by tests). An already-set
    environment variable always wins; this never overwrites the caller's shell.
    """
    path = path or Path(os.getenv("TRADER_ENV_FILE") or (ROOT / ".env"))
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# Loaded before the path constants below, which are read once at import time. Doing
# this later (as it used to be) meant TRADER_DATA_DIR / TRADER_DB in .env were silently
# ignored and the default database was used instead.
load_dotenv()

DATA_DIR = Path(os.getenv("TRADER_DATA_DIR") or (ROOT / "data"))
DB_PATH = Path(os.getenv("TRADER_DB") or (DATA_DIR / "trader.db"))

APP_NAME = "AI Trading Institution"
APP_VERSION = "0.1.0"
# Hard-coded. There is deliberately no environment variable, switch or API path
# that changes this. A separate reviewed project would be required for live money.
DEMO_ONLY = True
MAGIC = 20261106
ORDER_COMMENT = "ATI-DEMO"

# How long mt5.initialize() may wait for the terminal's IPC handshake, in ms. Read per
# call (not at import) so an operator or a test can change it without a code edit, and
# clamped so a typo cannot turn the wait into zero or an hour.
MT5_INIT_TIMEOUT_DEFAULT_MS = 60000
MT5_INIT_TIMEOUT_MIN_MS = 1000
MT5_INIT_TIMEOUT_MAX_MS = 300000


def mt5_init_timeout_ms() -> int:
    try:
        value = int(os.getenv("TRADER_MT5_INIT_TIMEOUT_MS") or MT5_INIT_TIMEOUT_DEFAULT_MS)
    except (TypeError, ValueError):
        return MT5_INIT_TIMEOUT_DEFAULT_MS
    return max(MT5_INIT_TIMEOUT_MIN_MS, min(value, MT5_INIT_TIMEOUT_MAX_MS))


DEFAULT_RISK_LIMITS: dict = {
    "risk_per_trade_pct": Decimal("0.25"),
    "daily_loss_pct": Decimal("1.0"),
    "weekly_loss_pct": Decimal("2.5"),
    "max_drawdown_pct": Decimal("6.0"),
    "max_positions": 2,
    "max_spread_points": {"default": 30, "XAUUSD": 60},
    "max_total_exposure_pct": Decimal("200.0"),
    "max_slippage_points": 30,
    "event_blackout_minutes": 30,
    "max_quote_age_s": 15,
}

# Trading sessions in UTC. The desk is flat outside these windows; Friday closes early
# and the weekend is excluded entirely.
SESSION_WINDOWS_UTC = ((0, 0, 21, 0),)   # Mon-Fri 00:00-21:00 UTC
SESSION_WEEKDAYS = (0, 1, 2, 3, 4)        # Monday=0 .. Friday=4

# There is deliberately no "trade without a calendar" switch. An unverified calendar
# means the event blackout proves nothing, so the risk engine rejects (fail closed).
# A licensed feed and a successful ingestion are requirements, not preferences.
#
# A calendar source must therefore be *named*, never assumed. The free public weekly
# calendar (Forex Factory's public JSON, no credential) is available as an explicit
# opt-in for research and demo work; a licensed feed URL always wins over it. See
# docs/DATA_SOURCES.md and ECONOMIC_CALENDAR_REPORT.md for the source research.
FREE_CALENDAR_SOURCES = {
    "forexfactory": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
}


# Currencies the desk actually reasons about: those of the tradable pairs, plus CNY,
# which the free weekly calendar reports and which matters to AUD and NZD. An event in
# any other currency can never match a pair's own two currencies, so a calendar row for
# it is dropped rather than stored as noise (the feed also uses "ALL" as a country).
CALENDAR_CURRENCIES = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "CNY")


def free_calendar_url(source: str) -> str:
    return FREE_CALENDAR_SOURCES.get((source or "").strip().lower(), "")


# Which event impacts may black out trading. HIGH and MEDIUM by default: a 30-minute
# window around a scheduled release is only meaningful for releases that move price,
# and a calendar that blocks on every Low-impact note is one an operator would disable.
# An event whose impact is unknown is always treated as blocking (fail closed).
EVENT_BLACKOUT_IMPACTS_DEFAULT = ("HIGH", "MEDIUM")
VALID_IMPACTS = ("HIGH", "MEDIUM", "LOW", "UNKNOWN")


def event_blackout_impacts() -> tuple[str, ...]:
    raw = os.getenv("TRADER_EVENT_BLACKOUT_IMPACTS") or ""
    chosen = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    chosen = tuple(part for part in chosen if part in VALID_IMPACTS)
    return chosen or EVENT_BLACKOUT_IMPACTS_DEFAULT


# How long a fetched calendar snapshot stays usable, per source. The scheduler refreshes
# on this cadence too (see providers.refresh_interval_s), so a healthy feed stays inside
# its window; a feed that stops answering goes STALE and trading is refused.
CALENDAR_MAX_AGE_MINUTES_DEFAULT = 180

# Only these instruments may be traded; broker suffixes are allowed.
MAJOR_BASES = (
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "AUDUSD", "NZDUSD", "USDCAD", "XAUUSD",
)

# Explicit base -> broker symbol. The verified broker (Exness) suffixes its
# instruments with "m", so the desk names the instrument and the map names the
# contract. A mapping is a *preference*, not a substitute: app/broker.py only uses an
# entry that the broker actually offers, and reports (and audits) a miss instead of
# quietly resolving to some other contract with a similar name.
DEFAULT_SYMBOL_MAP = {"EURUSD": "EURUSDm", "GBPUSD": "GBPUSDm", "XAUUSD": "XAUUSDm"}


def symbol_map() -> dict[str, str]:
    """DEFAULT_SYMBOL_MAP plus TRADER_SYMBOL_MAP ("BASE=SYMBOL,BASE=SYMBOL").

    Read per call, like the timeout, so a test or an operator can change the mapping
    without a code edit. Only whitelisted bases are accepted; a malformed pair is
    ignored rather than guessed at.
    """
    mapping = {base: symbol for base, symbol in DEFAULT_SYMBOL_MAP.items()
               if base in MAJOR_BASES}
    raw = os.getenv("TRADER_SYMBOL_MAP") or ""
    for pair in raw.split(","):
        base, _, symbol = pair.partition("=")
        base, symbol = base.strip().upper(), symbol.strip()
        if base in MAJOR_BASES and symbol:
            mapping[base] = symbol
    return mapping
CORRELATION_GROUPS = {
    "EURUSD": "usd-major", "GBPUSD": "usd-major", "AUDUSD": "usd-major",
    "NZDUSD": "usd-major", "USDJPY": "usd-major", "USDCHF": "usd-major",
    "USDCAD": "usd-major", "XAUUSD": "gold",
}

SESSION_COOKIE = "ati_session"
SESSION_TTL_S = 8 * 3600
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_S = 60


def session_open(now: datetime) -> bool:
    """True inside a configured UTC trading session. Weekends are always closed."""
    if now.weekday() not in SESSION_WEEKDAYS:
        return False
    minutes = now.hour * 60 + now.minute
    for window in SESSION_WINDOWS_UTC:
        start_h, start_m, end_h, end_m = window
        if start_h * 60 + start_m <= minutes < end_h * 60 + end_m:
            return True
    return False


def canonical_base(symbol: str) -> str | None:
    """Map broker symbol (XAUUSD.a, EURUSDm) to a whitelisted base."""
    upper = symbol.upper()
    for base in MAJOR_BASES:
        if upper == base or (upper.startswith(base) and upper[len(base):].isalnum()):
            return base
    return None


def symbol_currency(base: str) -> str:
    return "XAU" if base == "XAUUSD" else base[:3]