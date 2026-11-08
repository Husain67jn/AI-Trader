# Security Model

## Scope
Local, demo-only research and execution. The dashboard is not exposed to the public
internet. There is no code path that places live-money orders and no switch to enable one.

## Trust boundaries
1. **Claude** is untrusted. Responses are JSON-validated against strict Pydantic models
   with `extra="forbid"`. Prompt text inside `<untrusted_data>` cannot change policy.
2. **Market/news/calendar text** is untrusted evidence. It is wrapped and never used as
   instructions.
3. **Broker responses** are data; account state must be re-queried before acting.
4. **Owner actions** require an authenticated session, a CSRF token, and for risky actions
   an explicit confirmation phrase.

## Authentication and sessions
- Owner password hashed with `scrypt` (n=16384, r=8, p=1, 32-byte digest, random salt).
- Session cookie is HMAC-SHA256 signed, HttpOnly, SameSite=Lax, 8-hour expiry, `Secure`
  when `TRADER_HTTPS=1`.
- CSRF synchronizer token required on all state-changing requests.
- Login attempts limited to 5 per minute per client address.
- Password and signing secret may live in `.env`; generated values are stored under
  `data/` with restrictive permissions. Never commit `.env` or `data/`.

## Secrets
- Broker credentials are not stored. MT5 login happens in the official terminal.
- `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are read from the environment and never
  logged, returned to the browser, or written into reports.
- No mobile-browser storage contains credentials. Only the CSRF token is kept in
  `sessionStorage`; the session cookie is HttpOnly.

## Network
- Default bind: `127.0.0.1:8000`.
- LAN mode (`scripts/start.ps1 -Lan`) binds `0.0.0.0` only when a password is present.
- Do not port-forward. For remote phone access use a private VPN such as Tailscale,
  after verifying its current official setup. HTTPS is not terminated by this app;
  terminate it at a trusted reverse proxy or use the VPN.

## Execution safety
- Hard-coded `DEMO_ONLY`.
- Demo account positively verified before arming and again before submitting.
- Kill switch defaults to `PAUSED` after every restart; `EMERGENCY_STOP` is sticky.
- Fail closed on stale data, DB failure, unknown order state, or reconciliation mismatch.
- No blind retry: a timeout is reconciled by request id before any further action.