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

## Update: stage 2 hardening
- Per-IP rate limits (all pages / POST / search), request body capped at 100 KB (counted while streaming), WebSocket frame size capped, open sockets per IP limited.
- Password hashing runs in a bounded pool, so a login flood cannot exhaust memory or CPU.
- Slow database queries are aborted after 3 s.
- Honeypot fields on public forms; idempotent order submission; per-user and per-venue order limits.
- Venue password change/reset (reset links are never written to the log unless MAIL_LOG_BODY=1).
- Static files are served with content hashes and long caching; strict CSP, no third-party fonts or scripts.
- Known gaps: no CAPTCHA yet, in-memory limiter works for a single process only, Telegram and Google login have not been tested against the real services.

## Update: add-ons and photos
- Add-on choices are validated on the server against the database: options must belong to groups attached to that item, required groups need a choice, single-choice groups allow one. The price is always recomputed on the server.
- Photos: type decided by decoding (JPG/PNG/WebP only), at most 6 MB and 25 megapixels, always re-encoded to JPEG (removes GPS/metadata and hidden payloads), random file names, strict name check when serving, upload CSRF check, 30 uploads per venue per hour, only two images processed at once. Only photo upload routes may exceed the normal 100 KB request limit.
- Uploaded files live next to the database (`data/uploads`): back them up and keep them on the persistent disk.

## Update: e-mail sign-in
- Sign-in code: 6 digits, valid 10 minutes, stored only as a keyed hash (HMAC with SECRET_KEY), single use, burned after 5 wrong guesses, rate-limited per IP and per address; the answer is identical for unknown addresses. A valid code also confirms the address.
- With working e-mail (SMTP_HOST set), customers must confirm their address before ordering.
- Contact form: honeypot, 3 messages per IP per hour, messages stored for the admin. Admin "test e-mail" shows the real SMTP error.
