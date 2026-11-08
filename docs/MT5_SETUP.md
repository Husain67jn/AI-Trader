# MetaTrader 5 Setup

1. Install and open a 64-bit MetaTrader 5 terminal on Windows.
2. Log in to a **demo** account only. Never enter live credentials into this project.
3. In MT5: Tools → Options → Expert Advisors → allow algorithmic trading only if your
   broker/demo setup requires it for order submission. The Python package uses the terminal.
4. Confirm the exact symbol names on the demo account. Brokers often use suffixes such as
   `EURUSD.a` or `XAUUSDm`; the app discovers and whitelists supported bases automatically.
5. Set `TRADER_BROKER=mt5` locally and start with your Python environment:
   `python scripts/serve.py start`. If MT5 cannot be initialized, the app logs the
   failure and falls back to the deterministic mock broker in research mode.
6. Open the dashboard, verify: account mode `DEMO`, broker `mt5`, data health `HEALTHY`.
7. Only after checking the demo account is correct, use **Arm demo trading** (type
   `ARM DEMO`). Arming is deliberate; launching the app never starts trading.
8. To stop: **Pause** or **Emergency stop** on the dashboard, or run
   `python scripts/serve.py stop`.

## Important
- Offline tests use a fake terminal. Complete a supervised, smallest-volume test against
  your own verified MT5 demo account before enabling autonomous operation.
- The app refuses to submit if the account is not `DEMO` or cannot be verified.
- Symbol contract details, stops level, filling mode, and volume step come from the broker;
  never assume them.
- MT5 closed-trade history mapping in `MT5Broker.closed_trades` is approximate (open and
  close prices come from the out-deal). Improve it before relying on detailed post-trade
  execution analytics.
