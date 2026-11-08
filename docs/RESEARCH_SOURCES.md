# Research Sources

Access date: 2026-11-07 (UTC). Status is the HTTP code observed from the laptop.

| Source | URL | Status | Used for |
|---|---|---|---|
| MetaTrader 5 Python integration docs | https://www.mql5.com/en/docs/python_metatrader5 | 200 | Official API surface and package compatibility |
| MetaTrader 5 terminal docs | https://www.mql5.com/en/docs | 200 (same host) | Order/position/account semantics |
| FRED API docs | https://fred.stlouisfed.org/docs/api/fred/ | 200 | Official macro series API, key requirement |
| BLS release schedule | https://www.bls.gov/schedule/news_release/ | 403 (bot block; source remains official) | CPI/payrolls/unemployment schedule |
| Federal Reserve | https://www.federalreserve.gov/ | not fetched | FOMC statements, minutes, rates |
| BEA | https://www.bea.gov/data | not fetched | GDP and PCE releases |
| Claude Code docs | https://docs.claude.com/en/docs/claude-code/overview | 301 (redirect) | Harness, skills, permissions |
| Claude API docs | https://docs.claude.com/en/api/overview | 301 (redirect) | Messages API, usage, keys |
| Tailscale install docs | https://tailscale.com/kb/1017/install | 308 (redirect) | Private remote access recommendation |

## Decisions and caveats
- Use the official `MetaTrader5` Python package; if a broker/terminal combination is
  incompatible, an MQL5 Expert Advisor bridge is the fallback. No undocumented API is invented.
- Use the official Anthropic SDK for the application; no desktop-UI automation or scraping.
- Economic data is stored with source URL, release time, retrieval time, and quality.
  Revisions are preserved; a revised value is never treated as known historically.
- No news provider is enabled by default. Adding one requires checking licensing and terms.
- URLs above were reachability-checked, not deeply read during this build. Confirm current
  details before changing broker or data integrations.