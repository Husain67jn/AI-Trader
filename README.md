# AI Trader

AI Trader is a local Windows application for AI-assisted market research and
**MetaTrader 5 demo-account trading**. The dashboard is called AI Trading Institution.
Structured analyst reports feed a CIO proposal, while deterministic Python checks
control risk and execution. This is an actively developed prototype, version `0.1.0`;
no profitability is claimed.

## Features

- Nine research roles and a CIO produce validated JSON reports and trade proposals.
- An authenticated, responsive dashboard shows research, market health, proposals,
  economic events, risk limits, performance, trade reviews, and audit records.
- MT5 and mock broker adapters provide quotes, candles, contract metadata, and symbol
  resolution for supported forex pairs and gold.
- Risk checks cover position sizing, exposure, drawdown, loss limits, spread, quote
  freshness, trading sessions, and economic-event blackouts.
- An execution state machine tracks submissions, prevents duplicate processing,
  reconciles broker evidence, and records unresolved orders.
- SQLite stores research and operational state; consistent backup scripts are included.
- Trade reviews, strategy experiments, a simple Donchian backtester, and walk-forward
  analysis support research. Strategy promotion requires human review.
- Anthropic and mock AI providers support JSON validation and a daily token budget.
  Scheduled paid research requires an explicit opt-in.

## Safety and development status

`DEMO_ONLY = True` is hard-coded. No environment setting enables real-money trading.
Each application start resets the kill switch to `PAUSED`. Demo execution requires a
positively verified MT5 demo account, healthy data, a configured and fresh economic
calendar, risk approval, and the owner's typed `ARM DEMO` confirmation. The mock broker
is for research and tests and cannot arm or send orders.

The offline suite exercises risk, authentication, data validation, model responses,
execution, reconciliation, and learning with mocked services. These checks do not prove
real broker execution or a profitable strategy. Validate your own MT5 demo integration
with a supervised smallest-volume test before enabling automated operation.

Current limits include single-owner/single-process operation, market orders only,
in-app notifications, a simple price-based backtester, and no built-in TLS server.
External model and data integrations need your own configuration and verification.
See [known limitations](docs/KNOWN_LIMITATIONS.md) for the detailed development record.

## Architecture

```text
Browser dashboard (app/static/index.html)
  -> FastAPI factory, authentication, owner controls (app/api.py)
  -> UTC scheduler (app/scheduler.py)
     -> Analyst workflow and CIO (app/departments.py, prompts/)
        -> Anthropic or mock provider (app/claude.py)
     -> Deterministic risk checks (app/risk.py)
     -> Execution and reconciliation (app/execution.py)
        -> MT5 or research-only mock broker (app/broker.py)
     -> Market data and calendar/news (app/data.py, app/providers.py)
     -> Reviews, experiments, backtesting (app/learning.py, app/backtest.py)
  -> SQLite repository and audit state (app/db.py)
```

`app/models.py` defines validated data contracts; `app/config.py` supplies defaults and
environment configuration. Model output cannot change risk limits, the kill switch,
or application code. See [architecture](docs/ARCHITECTURE.md) and
[security model](docs/SECURITY_MODEL.md).

## Setup

Use **64-bit Python 3.11 on Windows**. Dependencies are pinned in `requirements.txt`;
the MetaTrader5 package and terminal integration require Windows.

From the repository directory in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item -LiteralPath .env.example -Destination .env
```

Edit `.env` locally. The example selects the mock broker and mock AI provider, disables
scheduled jobs, and contains no credentials. No API key or MT5 terminal is required for
this offline mode. The application creates its `data/` directory and database on demand.

```powershell
.\.venv\Scripts\python.exe scripts\serve.py start
# Open http://127.0.0.1:8000
.\.venv\Scripts\python.exe scripts\serve.py status
# When finished:
.\.venv\Scripts\python.exe scripts\serve.py stop
```

Set a strong `TRADER_OWNER_PASSWORD` locally, or retrieve the generated first-run
password from `data/owner_password.txt`, store it safely, and delete that plaintext file.
Keep the generated signing key and database private.

For MT5 demo data, install the terminal, log into a demo account, and set
`TRADER_BROKER=mt5`. Broker credentials stay in the local terminal. For Anthropic
research, configure your own `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` and change
`TRADER_LLM_PROVIDER` to `auto`. Verify model availability for your account.
Set `TRADER_SCHEDULER=1` when scheduled jobs are wanted; paid scheduled calls also
require `TRADER_ALLOW_PAID_AUTORESEARCH=1`.

Calendar/news endpoints and the optional public weekly calendar source are described
in [environment variables](docs/ENV_VARS.md) and [data sources](docs/DATA_SOURCES.md).
Review provider terms before using a feed. Missing or stale calendar data blocks trades.
Keep the dashboard on loopback by default. Phone/LAN use requires an owner password
and explicit transport configuration; see [mobile access](docs/MOBILE_DASHBOARD.md).

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe scripts\dry_run.py
```

Tests select temporary databases, mock broker/model services, and disable the scheduler.
The dry-run uses a fake MT5 package and a temporary database; its `order_send` raises if
reached. Its final session check verifies the risk engine against the configured session
gate whether the session is open or closed.

`diagnostics.py`, `verify_market_data.py`, `verify_app.py`, and `spread_analysis.py` in
`scripts/` can contact your configured terminal and feeds. `verify_app.py` starts a
temporary application and attempts to arm it; use these tools deliberately for supervised
demo integration checks. `scripts/hygiene_check.py` performs a local hygiene inspection;
its output includes local metadata and should remain private. `scripts/sample_report.py`
generates an explicitly synthetic report; the generated document is excluded from Git.

## Repository contents and privacy

- `app/`: application code and dashboard.
- `prompts/`: system, analyst, and CIO prompt templates.
- `tests/`: offline regression suite.
- `scripts/`: launchers, backup tools, diagnostics, and research checks.
- `docs/`: architecture, policies, setup, methodology, and limitations.
- `requirements.txt`, `.env.example`, `.gitignore`, and `CHANGELOG.md`: dependency pins,
  credential-free defaults, exclusions, and development history.

`.gitignore` excludes real environment files, all `data/` contents, databases, broker
exports, trading history, passwords/keys, logs, caches, virtual environments, editor and
assistant settings, backups, and generated reports. Only the sanitized root
`.env.example` is included. Review new files before every commit; ignore rules alone
cannot detect credentials embedded in source or remove previously committed data.
