# Known Limitations

1. **REAL MT5 was unavailable and now works.** History: the terminal auto-updated to
   build 6306 while the newest `MetaTrader5` package on PyPI was 5.0.6231, and
   `mt5.initialize()` returned `(-10005, 'IPC timeout')` even with the terminal running,
   responsive and logged into a demo account. Elevation, Windows session, duplicate
   instances, install path, firewall and Defender were each checked and ruled out.
   **Current state (2026-11-07 evening): the same package and terminal complete the
   handshake.** The adapter connects, verifies the demo account, resolves all eight
   whitelisted instruments, reads forex and gold ticks and returns 200 H1 candles per
   symbol — reproduce with `python scripts\diagnostics.py`, which prints the account
   mode, the resolved symbols, each instrument's measured spread and tick age, and the
   candle completeness per symbol. Resolve any future recurrence by installing the
   package matching the terminal build, or by pinning the terminal to a matching build;
   then complete a supervised smallest-volume demo test before arming autonomy.
2. **No live Claude call was made.** The Anthropic provider uses the official SDK and
   `claude-opus-5-5` by default, but only the mock provider was exercised in tests.
3. **No licensed calendar feed is configured.** The adapter exists and the scheduler
   refreshes every 6 hours once a URL is set, but with no feed the status is
   `UNAVAILABLE` and the risk engine rejects **every** trade (fail closed, by design —
   there is no switch to bypass it). FRED historical series need `FRED_API_KEY`.
4. **No news provider is configured.** The ingest/deduplicate/freshness pipeline is
   implemented and tested offline; licensing/terms must be reviewed before a real feed
   is enabled.
5. **Notifications are in-app only** (dashboard/audit log). No push/email/SMS provider.
6. **No built-in HTTPS.** Use a trusted reverse proxy or a private VPN.
7. **Only MARKET orders** are implemented; LIMIT proposals are rejected by the executor.
8. **Single process / single owner.** No multi-user roles, no high availability.
9. **MT5 closed-trade mapping is approximate** — the record is keyed on `position_id`
   (correct for reconciliation) but open/close prices and times are taken from the deal
   history, which can differ from the fill the strategy intended. Improve for precise
   execution analytics.
10. **Backtester is intentionally simple** (one Donchian model) and is not evidence of a
    trading edge. Costs model spread/slippage/commission but not every broker condition.
11. **Daily/weekly baselines** are stored per account in settings at first use and the
    peak equity persists across restarts, but neither is reconciled against broker
    statements after a deposit or withdrawal.
12. **No historical macro/news replay** for backtests; only price bars.
13. **No strategy auto-promotion** by design; human review is required.
14. **No profitability claim** is made anywhere. Small samples are not evidence.

15. **`.ps1` scripts cannot run on a default Windows 10 install** here: the PowerShell
    execution policy is `Undefined` on every scope. Use `scripts\start.cmd`,
    `stop.cmd` and `backup.cmd`, which call `scripts\serve.py`; the `.ps1` files are
    thin wrappers for machines where scripts are allowed.

16. **The launcher is single-instance.** One pid file, one port, no clustering.

17. **Unresolved orders block everything** until reconciled — deliberate, and it means a
    broker outage can stop trading. Reconciliation (startup, daily, or
    `POST /api/reconcile`) now settles such rows from broker evidence: live positions,
    live orders and, for MT5, **order history** — so an order that filled and whose
    position has since closed is found rather than declared absent. Rows younger than
    two minutes are left alone, and so is a submission this process is still waiting on
    (an MT5 call cannot be cancelled, so age alone cannot tell a hung terminal from a
    crash). An order the lookup could not have covered — older than the 30-day history
    window — is recorded as `ABSENT_UNVERIFIED` and reported in `unverified_orders`
    instead of being called clean. A settle that itself fails leaves the row blocking,
    loudly — including a history query that *raises* rather than returning `None`, which
    is left fail-closed on purpose (the package's documented convention is
    `None` + `last_error()`, so a raise means something unexpected). The MT5 adapter
    itself refuses to send unless DEMO_ONLY holds, the kill switch is ARMED, the symbol
    is on the whitelist and every volume/price/deviation check passes, and each refusal
    is audited — so a caller that skips `ExecutionService` still cannot reach the market.

18. **The token budget lock is process-wide**, not database-wide. A second uvicorn
    worker would need a DB-level reservation before the daily cap could be trusted
    across processes.

19. **Login throttling is in-memory and per-IP** (5 failures/minute); it resets when the
    server restarts.

20. **The dashboard CSP allows `'unsafe-inline'` scripts and styles** for the local
    single-page app. Acceptable on 127.0.0.1; tighten it before any wider exposure.

21. **News deduplication is keyed on headline + source + publication time.** A reworded
    headline is treated as a new article, and an edited headline from the same feed is
    not correlated with the original.

22. **The last window before the send cannot be closed.** The kill switch is read
    immediately before `order_send` and once after it, but the send itself cannot be
    made atomic with it: a pause landing inside that single call, or one that is set and
    cleared again before the post-send read, is not observable from this side and the
    order goes out. A fill found against a non-ARMED switch is alerted on
    (`filled_after_pause`) — that is detection, not prevention.

23. **`ABSENT_UNVERIFIED` does not block trading.** Deliberate: a terminal that cannot
    answer a history query must not wedge the submission queue forever. The cost is that
    an order that may have executed does not stop the next submission; it is reported in
    `/api/overview`, on the dashboard and in a runtime alert instead. An operator who
    wants a hard stop pauses the switch; nothing does it automatically.

24. **The request key still collides for ids sharing their first 16 characters.** Two
    such ids produce one order comment and `order_by_request` would report whichever the
    broker returns first. Proposal ids are 32-character uuids here, so a 16-character
    prefix collision needs roughly 2^32 ids on one desk; it cannot be widened much
    further anyway, because MT5 caps the comment at 31 characters.

25. **A database that already contains two rows with the same ticket will not start.**
    `CREATE UNIQUE INDEX orders_ticket_unique` fails with `IntegrityError` and `init_db`
    propagates it — fail-closed and loud rather than a silent repair, but it is an
    operator task: find and fix the duplicate. Regression coverage uses empty, current,
    and legacy schemas; inspect only your own local database when resolving a duplicate.

26. **The in-flight submission set is process-local.** It is correct for the single
    uvicorn process `scripts/serve.py` starts (no `--workers`, no `--reload`), and wrong
    the moment a second worker or a reloader is added: that process would not see the
    set and could settle a live submission from age alone.
27. **A closed market reports `CLOSED`, and `CLOSED` is not a data fault.** The verdict is
    derived from the desk's own session window (`config.SESSION_WINDOWS_UTC`) plus quote
    age: an old quote with the session closed is `CLOSED` (the last ticks of the previous
    session), and an old quote with the session open is `STALE` (a real feed fault).
    Neither is tradable — the risk engine requires `HEALTHY`, and arming requires it too.
    The detail payload carries `session_open`, `oldest_tick_utc`, `oldest_tick_age_s` and
    a reason, so the verdict can be checked rather than trusted. Caveat: the *desk's*
    session is not the *broker's* session. If a broker's hours differ from
    `SESSION_WINDOWS_UTC`, the verdict reflects the desk's configuration, which is also
    what gates trading — widen `SESSION_WINDOWS_UTC` if the broker trades longer.

28. **Candle gaps can only be judged against the desk's session calendar.** The
    MetaTrader5 package exposes no session hours, so `bars_quality` calls a gap an
    expected closure when every missing candle falls outside `config.session_open()`.
    A broker with unusual hours could therefore have a real hole classified as a closure
    (or vice versa) — reported either way, with the closure list and the issue list kept
    separate. `app/data.py::_missing_candles_are_outside_the_session` names the heuristic.

29. **The configured spread limits are stricter than this broker's measured spreads for
    GBPUSDm (38 points vs a limit of 30) and XAUUSDm (240 points vs 60).** Gold's point is
    0.001 on this broker, so the default was written for a different quote convention.
    Those instruments are refused by the spread check even with healthy data. Deliberate:
    loosening a risk limit is the owner's decision, made in Settings and audited.
    `scripts/diagnostics.py` prints `spread=<measured>/<limit>` per instrument and flags it.

30. **The hourly research job will not spend money unattended.** With a provider that
    bills per token it is skipped and audited unless `TRADER_ALLOW_PAID_AUTORESEARCH=1`;
    the free mock provider still runs. The dashboard's *Run research now* button is an
    explicit owner action and is unaffected. This enforces the project rule that no paid
    API call is made without the owner's approval.

31. **The symbol map's defaults name Exness suffixes.** `EURUSDm`, `GBPUSDm`, `XAUUSDm`
    are preferences, not assumptions: a mapping the broker does not offer is audited
    (`symbol_map_missing`) and discovery is used, reported as `discovered-after-map-miss`.
    On another broker, set `TRADER_SYMBOL_MAP`.

32. **Instruments are validated before use, not repaired.** `broker.instrument_issues()`
    reports a contract whose point, step, tick value or volume bounds are unusable; the
    proposal is refused rather than sized with a guess.

33. **A small demo account may be unable to size a minimum-lot trade.** The risk engine
    refuses a proposal when its budget falls below the broker minimum; it never rounds
    volume upwards to fit a small account. Dry-run stage 3b demonstrates this with an
    arbitrary synthetic account, independently of any local broker balance. Configure
    account funding and risk limits deliberately when validating your own demo setup.

34. **The free public calendar is not a licensed feed.** It is an undocumented public
    weekly endpoint: current week only, no `actual` values, not for redistribution. Rows
    are stamped `quality="UNOFFICIAL_PUBLIC"` and the provenance note travels with the
    status. Nothing is selected by default, so an unconfigured install refuses every trade
    rather than trading without a calendar. See [data sources](DATA_SOURCES.md).

35. **`copy_rates_from_pos(..., count=100000)` is rejected by the terminal**
    (`(-2, 'Terminal: Invalid params')`); 50,000 works (about 35 trading days of M1). The
    desk requests 200-500 candles and logs any rejected request as `bars_unavailable`
    rather than returning an empty series, so the ceiling is not reached in normal use.
    `scripts/verify_market_data.py --bars` is clamped to 50,000 for this reason.

36. **Session-band statistics are grouped by fixed UTC bands**, not by the broker's own
    session table, which the MetaTrader5 package does not expose. The same heuristic is
    used to tell a market closure from a data hole (item 28), so a broker with unusual
    hours could have a real hole classified as a closure - reported either way, with
    closures and issues kept separate.
