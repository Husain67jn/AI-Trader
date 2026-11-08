# Risk Policy

These are conservative demo guardrails, not claims of optimal risk.

| Limit | Default |
|---|---|
| Risk per trade | 0.25% of equity |
| Daily loss stop | 1.0% of start-of-day equity |
| Weekly loss stop | 2.5% of start-of-week equity |
| Max concurrent positions | 2 |
| Max spread | 30 points (FX), 60 (XAUUSD) |
| Quote freshness | 15 seconds |
| Event blackout | 30 minutes around stored high-impact events |

## Enforced in code (`app/risk.py`)
- Account must be positively verified as DEMO.
- Kill switch must be `ARMED`; default after restart is `PAUSED`.
- Data health must be `HEALTHY`.
- Quote must be fresh and spread within limit.
- Stop-loss must be on the correct side and at least the broker minimum distance.
- Position size uses broker contract size, tick size/value, volume min/max/step.
  Volume is floored to a valid step and rejected if below the minimum.
- Free margin must cover the broker-calculated requirement.
- Daily and weekly loss stops block new orders.
- Duplicate-symbol and correlated-group exposure are rejected.
- Stored economic-event blackout blocks new entries.
- Any missing/unknown input or engine exception rejects the trade (fail closed).

No martingale, grid escalation, loss-chasing, or averaging into losses is implemented.
Claude, prompts, skills, plugins, and strategies cannot change these limits.

## Owner changes
Only an authenticated owner can update limits, via `POST /api/settings/risk`, with the
confirmation phrase `I UNDERSTAND`; the change is logged. The kill switch is controlled
via `POST /api/kill-switch`; arming requires typing `ARM DEMO` and a verified demo account.