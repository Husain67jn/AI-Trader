# Mobile Dashboard

## Local use
- Start with your Python environment: `python scripts/serve.py start`.
- Laptop URL: http://127.0.0.1:8000
- Sign in with the owner password. If one was generated, it is in
  `data/owner_password.txt` (move it somewhere safe and delete the file).

## Phone on the same Wi-Fi
1. Set `TRADER_OWNER_PASSWORD` in `.env` (strong, unique).
2. For a trusted TLS reverse proxy, set `TRADER_HTTPS=1` in the launching shell and use
   the proxy's HTTPS URL. For deliberate plain HTTP on a trusted local network, set
   `TRADER_ALLOW_INSECURE_LAN=1` in the launching shell; passwords and cookies then cross
   that network unencrypted. The launcher reads these flags from the shell environment.
3. Run `python scripts/serve.py start --lan`; this binds `0.0.0.0:8000` only with an owner
   password in `.env` and the required transport opt-in.
4. Allow Python through Windows Firewall for the private network.
5. On the phone open the TLS proxy URL, or `http://<laptop-LAN-IP>:8000` for deliberate
   plain HTTP use.

## Away from home
Use a private VPN such as Tailscale. Do not open router ports and do not expose an
unauthenticated dashboard. HTTPS is not terminated by this app; use a trusted reverse
proxy or the VPN's encryption. Verify Tailscale's current official setup before use.

## Features
Command Center, Trading Desk, AI departments/proposals, Economic Calendar, Risk Center,
Learning Lab, Performance, Audit Log, and protected Settings. The phone can Pause or
Emergency Stop; re-arming requires the authenticated confirmation phrase.

## Security notes
- Session cookie is HttpOnly; the CSRF token lives only in sessionStorage.
- No broker or Claude credentials are ever sent to the browser.
- The dashboard reflects actual service state (broker mode, data health, kill switch),
  not merely a green indicator.
