# Architecture

Local Windows host. No cloud deployment, no Docker requirement, no live-trading path.

```
Phone / laptop browser
        |  HTTP (LAN only) + authenticated session + CSRF
        v
app/api.py  FastAPI, auth, dashboard, owner controls, kill switch
        |
        v
app/scheduler.py  UTC jobs: snapshots, research, daily review/reconcile
        |
        +--> app/departments.py  ten role prompts -> validated reports -> CIO proposal
        |           |
        |           +--> app/claude.py  Anthropic SDK | mock, budgeted, validated
        |
        +--> app/risk.py  deterministic checks (no LLM authority)
        |
        +--> app/execution.py  state machine, idempotency, reconciliation
        |
        +--> app/broker.py  MT5Broker | MockBroker, symbol resolution, metadata checks
        |
        +--> app/data.py  quotes, health gate, calendar, FRED adapter
        |
        +--> app/learning.py  trade memory, reviews, experiments, promotion gates
        |
        +--> SQLite (WAL): app/db.py
```

The application object is built by the factory `app.api:create_app`, which is what
`scripts/serve.py` hands to uvicorn (`--factory`). Importing `app.api` therefore opens no
database, connects no terminal and changes no state; tests call the factory with the
broker and provider they want.

## Boundaries
- Claude returns structured JSON only. `app/models.py` validates every field; unknown
  fields are rejected. No model output can edit risk limits, kill switch state, or code.
- Risk limits live in `app/config.py` defaults and can only be changed through the
  authenticated owner endpoint, which writes an audit record.
- The execution adapter is the only path to `order_send`. It refuses non-demo accounts,
  non-ARMED states, unhealthy data, stale quotes, off-whitelist symbols, out-of-bounds or
  off-step volumes, over-wide deviation, an unmatched quote, and a price too far from that
  quote - and each refusal is audited.
- One broker contract per instrument: `app/broker.py::resolve_symbol` maps a whitelisted
  base to the terminal's own name (`XAUUSD` -> `XAUUSDm`), explicitly configured and then
  discovered, and reports how it resolved. A look-alike contract is never silently
  substituted.
- `DEMO_ONLY = True` is a code constant with no environment switch.
- Broker, LLM, and data sources are interfaces so they can be tested with mocks.

## Database
SQLite in WAL mode with a small repository layer (`app/db.py`). Tables cover proposals,
reports, risk decisions, orders, trades, reviews, experiments, strategy versions,
events, snapshots, settings, audit log, and the kill switch. The schema is deliberately
plain so a later PostgreSQL migration is mechanical.