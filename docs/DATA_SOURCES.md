# Data Sources

## Market data
- Primary: MT5 terminal via the official Python package (ticks, bars M1–D1).
- Broker time is normalized to UTC; bar timestamps come from the terminal.
- Health gate: `HEALTHY`, `CLOSED`, `DEGRADED`, `STALE`, `UNKNOWN`. Only `HEALTHY`
  can reach execution. `CLOSED` means the markets are shut (outside the desk's
  session) and the quotes are the last ticks of the previous session - not a feed
  fault, and never tradable. `STALE` is an old quote *inside* a session, which is a
  real feed problem. Both are refusals. The API reports `session_open`,
  `oldest_tick_age_s` and the reason next to the verdict.
- No missing price is ever fabricated. Missing bars reduce health rather than being
  filled, and no bid/ask pair is invented for a candle (a candle has one price).
- Completeness: a gap whose missing candles are all outside the desk's session is
  reported as an expected market closure (a weekend, or gold's daily break); a gap
  with a missing candle inside session time is reported as a hole. Neither is ever
  silently filled.
- Symbols: one broker contract per whitelisted instrument, resolved explicitly
  (`TRADER_SYMBOL_MAP`) and then by discovery, preferring an exact name and then the
  shortest - so `XAUUSDm` is never replaced by `XAUUSD247m`. Contract metadata
  (contract size, point, digits, volume min/step/max, tick size/value, bid/ask) is
  validated before use; nothing is defaulted from the simulator.
- Quote freshness threshold: 15 seconds (configurable by the owner, not by Claude).

## Economic data
- Official sources only: BLS (CPI, payrolls, unemployment), Federal Reserve (FOMC,
  statements, minutes), BEA (GDP, PCE), FRED (historical series).
- Stored fields: event id, title, currency, scheduled UTC time, original timezone when
  available, actual/forecast/previous/revised, units, source URL, retrieval time, quality.
- Release time and retrieval time are distinct. Revisions are preserved.
- `app/data.py::fetch_fred_series` requires `FRED_API_KEY` in `.env`; without it the
  adapter raises `MissingCredential` and the app continues.
- No HTML calendar scraping is enabled. A public weekly JSON calendar is available by
  setting `TRADER_CALENDAR_SOURCE=forexfactory`; a configured licensed feed URL takes
  precedence. Review source terms before use. Successful, fresh ingestion is required
  before the risk engine accepts the calendar.

## News
- Disabled by default. No article is treated as verified fact; injected instructions in
  article text cannot change system policy.

## Synthetic data
- The mock broker generates deterministic synthetic bars for tests and the sample report.
  Every such artifact is labelled **SYNTHETIC** and is not evidence about any market.
