# Environment Variables

Copy the credential-free `.env.example` to `.env` in the project root, then edit the
local copy. Real environment files are excluded from Git. Values are loaded once at startup and
never override an already-set environment variable. `.env` is loaded **before** the
`DATA_DIR`/`DB_PATH` constants are computed, so `TRADER_DATA_DIR` and `TRADER_DB`
inside `.env` take effect; a shell variable of the same name still wins.

The example defaults to offline mocks with scheduled jobs disabled. The reference
below also shows options for external services; enabling them is a local choice.

```ini
# --- Owner access ---
# Required for LAN/phone mode. If unset, a random password is generated once into
# data/owner_password.txt (move it somewhere safe, then delete that file).
TRADER_OWNER_PASSWORD=
# Optional: pin the session-key secret so sessions survive restarts.
TRADER_SECRET=
# Set to 1 only when a trusted reverse proxy terminates HTTPS (sets Secure cookie).
TRADER_HTTPS=0

# --- Broker ---
# mt5  = real MetaTrader 5 terminal, demo account only (verified at startup)
# mock = deterministic offline simulator (default when MT5 is unavailable)
# auto = try mt5, fall back to mock
TRADER_BROKER=mock
# How long mt5.initialize() may wait for the terminal handshake, in milliseconds.
# Clamped to 1000..300000; a bad value falls back to 60000. A short value makes a
# dead IPC channel fail fast; it never shortens an actual connection.
TRADER_MT5_INIT_TIMEOUT_MS=60000
# Explicit base -> broker symbol, comma separated. The defaults name this broker's
# contracts (EURUSD=EURUSDm, GBPUSD=GBPUSDm, XAUUSD=XAUUSDm). A mapping the broker does
# not offer is audited (symbol_map_missing) and discovery is used instead; discovery
# prefers an exact name and then the shortest, so a look-alike contract (XAUUSD247m) is
# never silently substituted. Only whitelisted bases are accepted.
TRADER_SYMBOL_MAP=

# --- Claude (optional) ---
ANTHROPIC_API_KEY=
# ANTHROPIC_AUTH_TOKEN=
TRADER_LLM_PROVIDER=auto
ANTHROPIC_MODEL=claude-opus-5-5
CLAUDE_DAILY_TOKEN_BUDGET=200000
# The hourly research job may not spend money unattended. Set to 1 to let the scheduled
# job call a billed provider (an explicit owner decision, logged to the audit table).
# The free mock provider runs regardless, and the dashboard's "Run research now" button
# is always available.
TRADER_ALLOW_PAID_AUTORESEARCH=

# --- Data (optional; each status is UNAVAILABLE until configured) ---
# FRED historical series. Unset => fetch_fred_series raises MissingCredential rather
# than returning an empty "success".
FRED_API_KEY=
# Economic calendar: an HTTP endpoint returning {"events": [...]} with
# event_id/title/currency/ts_utc/impact. Times may carry an offset; they are
# converted to UTC on ingest. Unset, never ingested, or older than 180 minutes =>
# calendar UNAVAILABLE/STALE and every risk assessment is rejected (fail closed).
# There is no switch that permits trading without a verified calendar.
TRADER_CALENDAR_FEED_URL=
TRADER_CALENDAR_API_KEY=
# News: an HTTP endpoint returning {"articles": [...]} with
# headline/source/published_at (optional url). Headlines are stored, deduplicated,
# and only shown while younger than 24 h. Unset => news UNAVAILABLE.
TRADER_NEWS_FEED_URL=
TRADER_NEWS_API_KEY=

# --- Runtime ---
TRADER_DATA_DIR=data
TRADER_DB=data/trader.db
TRADER_SCHEDULER=1
# LAN mode binding. scripts/serve.py sets TRADER_LAN itself, and refuses --lan over plain
# HTTP unless TRADER_HTTPS=1 (a TLS proxy terminates) or TRADER_ALLOW_INSECURE_LAN=1 is
# set deliberately. With it, the exposure is reported by the API, logged at startup and
# shown on the dashboard. Local (loopback) development needs neither variable.
TRADER_ALLOW_INSECURE_LAN=
```

There is deliberately **no** variable that enables live trading, no variable that can
arm the system, no variable that disables a risk check, and no variable that bypasses the
economic-calendar requirement. `DEMO_ONLY` is a hard-coded constant in `app/config.py`
and no environment variable is consulted for it. Absent credentials are reported as
unverified in the dashboard (`calendar_status`, `news_status`) — the app never substitutes
a fake success.

`TRADER_BROKER=mt5` still falls back to the research-only mock when the terminal is not
authorised; that fallback can never arm or submit.

Variants used by tests only (set in `tests/conftest.py`, not needed for normal use):
`TRADER_BROKER=mock`, `TRADER_SCHEDULER=0`, `TRADER_LLM_PROVIDER=mock`,
`TRADER_DATA_DIR`, `TRADER_DB`, `TRADER_OWNER_PASSWORD`.
