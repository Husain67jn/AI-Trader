# Backtesting Methodology

Implemented in `app/backtest.py`.

## Anti-look-ahead
- Signals use closed bars up to index `i` only.
- Entry occurs at the **next** bar's open. Tests assert `entry_time > signal_time`.
- Stops/targets are evaluated on the following bars; when both are touched in one bar,
  the stop is assumed first (conservative).
- No future macro/news value is available to historical decisions.

## Costs
- Half-spread on entry, slippage in points, commission per lot per side.
- Contract size, tick size/value, volume step, and minimum volume are explicit parameters.
- The synthetic sample uses mock data; it is labelled and not evidence about markets.

## Validation
- `run_backtest` reports net return, trades, win rate, average win/loss, expectancy,
  profit factor, max drawdown, recovery factor, approximate Sharpe/Sortino, and costs.
- `walk_forward` optimizes on an earlier slice and evaluates on the following unseen slice.
- Repeated optimization on the final test set is prohibited by process, not just tooling.
- Results never imply future profitability. Report sample size and uncertainty.

## Known ceiling
The built-in strategy is a simple Donchian breakout with ATR stops, included to prove the
framework and produce a labelled sample. It is not promoted as a trading edge.
`ponytail:` extend the engine only when a specific hypothesis needs it.