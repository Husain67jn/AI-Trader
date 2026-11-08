# Changelog

## 2026-11-07 (evening) — the mission round: MT5 verified live, symbol resolution, data health, M3–M6

157 → **209 tests passing**. No order sent, no paid API call, kill switch still `PAUSED`.

- **MT5 works again and is verified through the app's own adapter.** Terminal build 6306
  with package 5.0.6231 completes the handshake now (the `-10005` IPC failure investigated
   previously has cleared). Demo account verification and symbol resolution succeeded;
  forex + gold ticks retrieved, 200 H1 bars per symbol.
- **Broker-aware symbol resolution** (`app/config.py` `symbol_map`, `app/broker.py`
  `resolve_symbol` / `resolved_symbols` / `eligible_symbols`): explicit
  `EURUSD→EURUSDm`, `GBPUSD→GBPUSDm`, `XAUUSD→XAUUSDm`, overridable with
  `TRADER_SYMBOL_MAP`; discovery prefers an exact name then the shortest, so **`XAUUSD247m`
  can no longer be substituted for `XAUUSDm`** — and a configured mapping the broker does
  not offer is audited (`symbol_map_missing`) instead of being passed over quietly. One
  contract per instrument: the eligible list went from 9 names to 8.
- **`Data health: STALE` was a misdiagnosis, and is fixed by evidence.** On a closed
  market the verdict is now `CLOSED` — session closed, quotes are the last ticks of the
  previous session — with `session_open`, `oldest_tick_age_s` and a reason in the payload,
  and the dashboard prints them. `STALE` is reserved for an old quote *inside* a session,
  which is a real feed fault. `CLOSED` is never tradable: risk requires `HEALTHY` and
  arming requires `HEALTHY` (verified live: `409 cannot arm: market data health is CLOSED`).
- **Candle gaps now know the calendar.** A gap whose missing candles are all outside the
  desk's session is an expected closure (weekend, gold's daily two-hour break — both
  measured); only a hole with a missing candle inside session time is an issue. Every
  symbol previously reported `ok: False` over a weekend. `bars_quality` returns
  `closures` alongside `issues`.
- **Invented bar prices removed.** `bars()` attached a `bid_close`/`ask_close` derived from
  the bar's close while treating `spread` (points) as a price — off by 10^5 on a 5-digit
  symbol. A bar has one price; both adapters now return none. A failed
  `copy_rates_from_pos` is logged (`bars_unavailable`, with the package code) instead of
  being indistinguishable from "no history", and the timeframe constant is looked up for
  the requested timeframe only.
- **All instruments are probed for health**, not the first five: a quote nobody checked
  could not degrade the desk before (`app/data.py`). The terminal check is derived from the
  broker class rather than the string `"mt5"`.
- **M3 fixed — risk settings could be poisoned.** `Infinity` was accepted and stored,
  `NaN` produced HTTP 500, and spread maps were unvalidated. `risk.MAX_LIMIT` +
  `risk.clean_limit()` now require finite, positive values inside a ceiling, and
  `load_limits()` re-validates the stored overrides so a value cannot arrive by another
  route.
- **M4 fixed — the owner password can be rotated** (`POST /api/auth/password`), requiring
  the current password and 12 characters, bumping a session `epoch` so every other session
  is retired immediately. Settings tab has the form.
- **M5 fixed — LAN mode is no longer silently insecure.** `--lan` requires `TRADER_HTTPS=1`
  or an explicit `TRADER_ALLOW_INSECURE_LAN=1`; `api.transport_security()` reports
  `LOCAL_HTTP` / `INSECURE_LAN` / `HTTPS`, a startup alert is audited and the dashboard
  shows a red banner. Loopback development is unchanged.
- **M6 fixed — importing `app.api` does nothing.** No module-level app, no `init_db()`, no
  MT5 session; uvicorn runs `app.api:create_app --factory` (`scripts/serve.py`). Verified in
  a subprocess: an `ARMED` switch is untouched and no audit row is written.
- **Autonomous paid research is refused by default.** The hourly job called the billing
  provider every hour without approval, against the project's own rule 8. It now requires
  `TRADER_ALLOW_PAID_AUTORESEARCH=1` (the free mock provider is unaffected); the manual
  *Run research now* button is unchanged.
- **Smaller:** bounded login-throttle map; `symbol_resolution` and `transport_security` in
  `/api/overview` plus dashboard rows for them and the health reason; diagnostics reuses one
  MT5 session instead of opening a second, compares measured spread against the configured
  limit, and tolerates a database that does not exist yet; the test fixture now clears all
  per-test settings (a password rotation used to retire the credential for every later
  test); dead imports removed from `api`, `data`, `risk`, `providers`, `learning`.
- **Evidence:** the round's acceptance probe — 19/19 claims PASS against the live terminal
  on a throwaway database with the mock provider: `/healthz`, login and CSRF, the real MT5
  session and demo account, symbol resolution, ticks, the `CLOSED` health verdict, the
  refused arm (409) with the switch still `PAUSED`, a clean reconcile, the dashboard, and
  that no credential appears in the overview payload.

## 2026-11-07 — the ten PART 5 findings implemented, probed and verified
- **A failed history call is never read as "never executed".** `history_orders_get`
  returns `None` both for "no such order" and for "the call failed", and the code read
  both as absence — which is how an executed order gets erased from the ledger.
  `order_by_request` now raises `HistoryUnavailable` carrying `last_error()`
  (`app/broker.py:50`, `819`), and `_settle_unresolved` only treats absence as
  conclusive when the lookup actually answered (`app/execution.py:463-497`). Chosen
  variant: **alert-and-continue** — the row becomes `ABSENT_UNVERIFIED`, is reported,
  and does not wedge the queue.
- **The kill switch is read once more immediately before `order_send`**, after the
  `verify_demo` and `check_order` round trips (`app/broker.py:713-719`); a fill that
  beats a pause raises a `filled_after_pause` alert and never downgrades the operator's
  state (`app/broker.py:738-745`). The remaining window is the send call itself.
- **`ABSENT_UNVERIFIED` is visible everywhere**: a runtime `orders_absent_unverified`
  alert on every reconcile (`app/execution.py:414-420`), `unverified_orders` in
  `/api/overview` (`app/api.py:277`), and two new rows in the dashboard's command table
  (`app/static/index.html:285-289`) — which previously showed no order state at all.
- **The MT5 adapter enforces all of its own preconditions and audits every refusal**
  (`app/broker.py:619-693`): DEMO_ONLY, ARMED, whitelist, no other unresolved order,
  instrument, volume bounds/step, deviation cap, quote, price drift. Two traces changed
  the shape of the fix: the import is function-local to avoid a cycle with
  `execution`, and the guard excludes the request's **own** pending row, which the
  executor writes before calling the adapter — counting it refused every order.
- **A non-numeric volume no longer escapes the adapter.** `Decimal("lots")` raises
  `ConversionSyntax`, an `ArithmeticError` and not a `ValueError`, so the volume guard
  crashed instead of refusing with an audit row (`app/broker.py:664-670`). Found by the
  verification probes, not by the suite; a `("volume": "lots")` case was added.
- **The request id is registered in flight before its row is written**
  (`app/execution.py:215-217`) and `_IN_FLIGHT_LOCK` is released before any I/O
  (`app/execution.py:456-464`), so a settle cannot call a live submission clean and a
  contended database cannot stall registration.
- **A ticket belongs to one order row**: partial unique index
  `orders_ticket_unique ON orders(ticket) WHERE ticket IS NOT NULL` (`app/db.py:31-37`).
- **The request key widened from 8 to 16 characters**, derived in one place
  (`app/broker.py:60-68`); the audit now names the state a write replaced (`_finish`),
  and `TRANSITIONS` is enforced instead of being dead code
  (`transition_allowed`, `app/execution.py:56-73`).
- **Tests.** 137 → 157 passed. 19 tests and 1 parametrized case added. Independent
  probes: 55/55 claims pass, and the migration verified against an empty database, a copy
  of the project schema and a legacy schema, 11/11.

## 2026-11-07 — adversarial review of H1–H4: five defects fixed
- **The kill switch is now read immediately before the send.** It could move while
  `check_order` round-tripped to the terminal: a pause landing in that window was
  ignored and the order still went out (`app/execution.py:204-216`). The last read now
  sits between the check and `broker.submit`, and the pending row is settled as
  `REJECTED` so nothing is left blocking.
- **The MT5 adapter enforces the money boundary itself.** `MT5Broker.submit` refuses
  unless the kill switch is `ARMED` and `canonical_base(symbol)` is not `None`
  (`app/broker.py:600-615`). Previously only `ExecutionService` checked, so anything
  holding the broker object — a future department, script or plugin — could place an
  order past every control. `order_send` is now provably not called in either case.
- **Order lookup consults order history.** `order_by_request` searched the in-memory
  send map, live positions and live orders only, so an order that filled and whose
  position had since closed looked absent and was marked `RECONCILED` with no ticket —
  silently erasing an executed trade from the local ledger
  (`app/broker.py:657-700`). It now also scans `history_orders_get` from the row's own
  creation time.
- **`RECONCILED` no longer means "we don't know".** When the lookup could not have
  covered the row (older than the 30-day history window) the state is the new
  `ABSENT_UNVERIFIED`, audited as `order_absent_unverified` and reported in
  `reconcile()["unverified_orders"]`. Rows whose submission is still in flight are
  skipped outright (`settle_skipped_in_flight`) via a process-local in-flight set,
  because a hung MT5 call cannot be cancelled.
- **A settle can no longer clobber a newer state.** The settle `UPDATE` is guarded with
  `AND state IN (unresolved)`: if a submission finished while the settle was looking,
  its state wins and the settle audits `settle_skipped`
  (`app/execution.py:394-410`). Two interleaved reconciles now write one audit row
  instead of two.
- **Tests.** 130 → 137 passed. New: kill-switch flip during `check_order`, the
  adapter's own refusal (paused + off-whitelist), history lookup for a filled-and-closed
  order, the in-flight guard, the settle/clobber race, and the unverified-old-row case.

## 2026-11-07 — high-risk audit findings fixed
- **Symbol whitelist is enforced at execution.** `MAJOR_BASES` was only applied when
  building research context: `RiskEngine` and `ExecutionService.submit` accepted any
  symbol the terminal offered. Both now reject a proposal whose `canonical_base()` is
  `None`; the boundary check runs even against a forged `RiskDecision`
  (`app/risk.py:123`, `app/execution.py:132`).
- **`check_order` failures no longer wedge the order queue.** A broker exception used to
  leave the row in `SUBMISSION_PENDING` forever, blocking every later submission with no
  way back through the API. The row is now settled as `REJECTED` (the check is
  read-only, nothing was sent).
- **A failed read-back no longer loses the ticket.** `_verified_state` is wrapped: an
  exception while verifying a sent order pauses trading, records the ticket, writes an
  alert, and marks the row `UNKNOWN` instead of raising out of `submit`.
- **Restart/crash recovery actually resolves.** `reconcile()` settles unresolved rows
  from broker evidence (`order_by_request`) before reporting; rows newer than two
  minutes are skipped so an in-flight submission is not clobbered. This makes the
  documented `/api/reconcile` recovery real.
- **Tests.** 125 → 130 passed. New: off-whitelist rejection (risk and submit), check
  failure settlement, verification failure, stale-pending settlement, and the
  fresh-pending guard.

## 2026-11-07 — broker label cannot overclaim a connected session
- **Fix (audit item 1).** `broker_status()` labelled any `REAL_MT5`-kind broker
  "connected terminal, demo account" without checking `connected`, so the label
  could contradict `connected: false` in the same payload. It is now gated on
  that flag and reads "REAL MT5 — not connected" otherwise (`app/broker.py:79`).
  Unreachable over HTTP (the only producer verifies the session first); this
  closes the programmatic path. Test extended; 125 passed.

## 2026-11-07 — broker honesty: REAL MT5 unavailable is now explicit
- **Broker identity in the API.** `GET /api/overview` gains `broker_status` with
  `real_mt5` / `simulated` derived from the broker *class* (not from configuration),
  a human label, the connect error and the effective handshake timeout.
- **Actionable connection errors.** `BrokerUnavailable` now carries the MetaTrader5
  error `code` and an operator-facing `hint` (one per failure class: `-10005` IPC
  timeout, `-6` not authorized, missing package, non-demo account). The fallback
  broker receives `mt5_error`, so the reason survives into the dashboard instead of
  living only in the audit log.
- **Configurable handshake timeout.** `TRADER_MT5_INIT_TIMEOUT_MS` (default 60000,
  clamped 1000–300000, read per call) is passed to `mt5.initialize(timeout=...)`.
- **Dashboard.** A persistent banner reads **REAL MT5 UNAVAILABLE** with the error
  code, the fix and the timeout in force; a broker badge distinguishes
  `REAL MT5` from `SIMULATED`; quotes and positions carry a "simulated, not market
  data, not real trades" marker; the command center shows the connection error.
- **Unchanged safety.** `DEMO_ONLY` still hard-coded, kill switch starts and stays
  `PAUSED`, the mock remains `RESEARCH_ONLY` and cannot arm, no order was sent.
- **Tests.** 117 → 125 passed, 0 skipped. New coverage: timeout configuration and
  clamping, timeout actually reaching `initialize()`, actionable `-10005` / `-6`
  errors, fallback reporting, real-vs-simulated status, overview honesty, and a
  static check that the dashboard keeps the explicit unavailable banner.

## 2026-11-07 — final hardening and integration sprint
- **Calendar fail-closed.** `TRADE_WITHOUT_CALENDAR_FEED` deleted. Every risk
  assessment now requires a positively `AVAILABLE` calendar (licensed feed configured
  **and** ingested within 180 minutes); `UNAVAILABLE`, `STALE` and `UNKNOWN` all
  reject. No switch, flag or research-only bypass can authorize execution.
- **Configuration order.** `.env` is loaded before `DATA_DIR`/`DB_PATH` are computed,
  so `TRADER_DATA_DIR` / `TRADER_DB` inside `.env` now take effect; the duplicated
  loader was removed; importing config touches nothing. Covered by subprocess tests.
- **Anthropic.** Injectable client for deterministic offline tests, process-wide budget
  lock, actual-usage settlement including failed and refused calls, and an explicit
  credential gate. Credentials never appear in logs (tested). The three budget tests
  no longer skip on machines without a credential.
- **News pipeline.** New `news` table with SHA-256 deduplication, retrieval and
  publication freshness windows, scheduler polling every 15 minutes, news in the
  research context, `POST /api/news/refresh`, and a dashboard News panel. `AVAILABLE`
  now requires a recent successful ingestion, not merely a configured URL.
- **Ten departments.** Verified through one provider interface with per-role prompt
  versions, schema validation and CIO-only aggregation; `NO_TRADE` /
  `INSUFFICIENT_DATA` never reach execution.
- **Risk.** Added a hard rejection when the MT5 terminal reports AutoTrading disabled
  (previously captured but not enforced).
- **Ops.** Diagnostics prints why MT5 is not in use instead of silently showing the
  simulator; dashboard shows calendar and news status with refresh controls.
- **Tests.** 93 → 117 passed, 0 skipped. New coverage: calendar fail-closed (3 states),
  config initialisation, news ingestion/dedup/freshness, offline Anthropic (budget,
  refusal, malformed output, secret-free logs), FRED honesty, session expiry,
  login throttling, terminal AutoTrading, and the full ten-role workflow.
- Deliverables (since consolidated and removed, 2026-11-07): the hardening, test and
  blocker reports were merged into the sprint record and then deleted by the owner on
  2026-11-07, together with the accumulated round reports and the master build prompt.
  This changelog is the durable record from that point on.

## 2026-11-07 — repair, hardening and completion
- **Demo gate.** Brokers now declare `kind` (`REAL_MT5` / `RESEARCH_ONLY`).
  `can_arm()` requires a real demo session, `submit()` re-checks at the money
  boundary, and the dashboard exposes `broker_class`, `trade_armed`, `arm_blocker`,
  `system_state`. A simulator can no longer arm or submit.
- **MT5 adapter correctness.** Order-state map built from the package's own
  `ORDER_STATE_*` constants (the previous hand-written map inverted FILLED and
  CANCELED); `order_check` success restricted to retcode 0 / `TRADE_RETCODE_DONE`;
  `order_send` retcodes bucketed into done / refused / unknown; a fill is only
  reported on `TRADE_RETCODE_DONE`; closed trades keyed on `position_id` (the previous
  use of the closing deal's ticket made every closed position look "missing", pausing
  trading).
- **Risk.** All documented conditions implemented; approval is recalculated on fresh
  data immediately before submission and the engine's own volume is what gets sent;
  missing input rejects rather than defaulting; equity baselines keyed per account;
  `peak_equity()` now persists the high-water mark.
- **AI.** Daily token budget enforced with a lock-based reserve/settle, so concurrent
  research cannot collectively exceed it; a failed call releases its reservation.
- **Learning.** Reviews reconstruct and persist the original decision context (order,
  proposal, department reports, risk decision, pre-entry snapshot).
- **Market data.** Real HTTP-JSON calendar and news adapters with UTC conversion, row
  validation, staleness and explicit `UNAVAILABLE` when unconfigured; ingestion wired
  to the scheduler (6 h) and `POST /api/calendar/refresh`; FRED implemented and
  operational only with a key. No event or headline is ever invented.
- **Backtesting.** Entry candle evaluated; gap-through-stop fills at the open; open
  positions closed and counted at the end; ATR no longer reads bars off the end;
  provenance (`spec`, data source, bar-quality issues) in every result; walk-forward
  reports per-fold train metrics.
- **Execution/restart.** Startup reconciliation runs before serving; unresolved orders
  block new submissions; unknown outcomes pause and reconcile — never retried.
- **Operations.** `scripts/serve.py` (stdlib launcher) plus `start.cmd` / `stop.cmd` /
  `backup.cmd`; the `.ps1` files are wrappers because this machine's execution policy
  blocks them. Diagnostics now reports the real arm gate instead of "demo verified"
  for a mock.
- **Tests.** 34 → 93, including the sprint's 30 required items; every fix has a
  regression test. MT5 tests use a fake package and are labelled as such.

## 2026-11-06 — initial build
- Safety-first skeleton: hard-coded `DEMO_ONLY`, kill switch (ARMED/PAUSED/
  EMERGENCY_STOP, default PAUSED on restart), strict Pydantic contracts.
- Broker abstraction: deterministic `MockBroker` plus an MT5 adapter that refuses
  non-demo accounts.
- Deterministic risk engine: 0.25%/trade, 1% daily, 2.5% weekly, max 2 positions,
  spread and quote-freshness limits, event blackout, volume step, margin and
  correlated-exposure checks. Fails closed.
- Execution state machine: idempotent request ids, ack-is-not-fill, timeout
  reconciliation by request id, partial fills, mismatch pauses trading.
- Ten departments plus CIO with strict JSON validation, mock provider fallback,
  and `<untrusted_data>` isolation for external text.
- Learning: trade memory, post-trade reviews, experiments with promotion gates
  (no self-promotion, minimum sample), immutable strategy versions.
- Backtester: Donchian breakout with ATR stops, next-bar entries, spread/slippage/
  commission costs, walk-forward.
- Mobile dashboard with scrypt password auth, HMAC session cookie, CSRF, rate-limited
  login, security headers, and owner kill-switch/arm controls.
- Scheduler, backup, diagnostics, launch/stop scripts; documentation set; 34 tests
  passing.
