# Security notes

Checked by `tests/test_smoke.py` (CSRF, login required, venue isolation, WebSocket auth) and by reading the code.

## Done
- **CSRF**: every POST needs a session token (and a matching `Origin` header when sent). WebSockets require a same-site `Origin` and a logged-in session.
- **Passwords**: scrypt, 10+ characters, max 128 (no hash-DoS), same response time for unknown accounts, per-IP and per-account rate limits. Changing or resetting a password logs out all other devices.
- **Sessions**: signed, `HttpOnly`, `SameSite=Lax`, `Secure` when `BASE_URL` is https, a fresh session on every login.
- **Access control**: every venue query is scoped to the logged-in venue, every customer order to its owner; foreign ids return 404.
- **Prices**: never read from the browser. Order totals are computed from the database.
- **Injection / XSS**: all SQL parameterised; Jinja autoescape on; no inline scripts, strict Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff`, no-store on pages.
- **Open redirects**: `next` accepts same-site paths only.
- **Flash messages** live in the session, not in URLs (no text spoofing via links).
- **Fake venues**: new venues are `pending` until approved in `/admin`; registration is rate limited.
- **Google sign-in**: state and nonce checked; an unconfirmed password account that shares the Google address loses its password (pre-registration takeover).
- **Telegram**: no inbound port (long polling), one-time 15-minute link codes, a chat can act only on the venue it is linked to, all user text HTML-escaped.
- **Data**: customers can delete their account; venues keep anonymous order totals. Container runs as a non-root user.

## Known limits (fine for an MVP, fix before real customers)
- Sign-up reveals whether an e-mail is registered (rate limited).
- Rate limits and live updates are in memory: run exactly **one** server process.
- The request size cap uses the `Content-Length` header only.
- Sessions cannot be revoked individually; they end after 14 days or a password change.
- The admin area is a single shared password: use a long one, and prefer HTTPS-only hosting.
- Telegram group chats: everyone in a linked group can press the buttons.
- No audit log, no CAPTCHA, no dependency scanning in CI, SQLite backups are manual.
- Not yet tested against the real Telegram and Google servers (no internet access in the build sandbox).
- GDPR: add a privacy policy and terms page before collecting real customer data.
