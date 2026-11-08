# Learning System

Learning is a controlled research process, not self-modifying code or prompts.

## Memory layers
1. **Trade memory** (`proposals`, `orders`, `trades`, `reviews`, `audit_log`): market
   snapshot at decision time, department reports, proposal, risk checks, broker request
   and response, fill/slippage/fees, stop/target changes, exit reason, P&L, post-trade review.
2. **Market knowledge** (`market_snapshots`, `events`, reports): regime, volatility, spread,
   event context, source, timestamp, quality.
3. **Strategy intelligence** (`experiments`, `strategy_versions`): immutable version ids,
   hypotheses, backtest/OOS/walk-forward/demo-forward results, promotion decisions.

## Post-trade review
Every closed trade gets a structured review. Process quality is scored separately from
P&L: a profitable trade can be a poor decision. The reviewer may propose a hypothesis and
the next test; it may not change strategy parameters.

## Promotion gates
`MIN_PROMOTION_SAMPLE = 100`. A version must evidence backtest, out-of-sample,
walk-forward, robustness, demo-forward results, the minimum sample, **and** human approval.
An experiment cannot promote itself; `promote_experiment` records the gates and reasons.

## Immutability
Strategy versions are immutable. Re-registering the same version with different content
raises an error; create a new version id. Experiments and audit entries are append-only in
normal operation.