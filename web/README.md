# Ahead: pre-order for cafes (MVP)

Venues register, get approved, build their menu and receive pickup orders live (website + optional Telegram).
Customers sign up (e-mail or Google), find a venue, order, choose an arrival time. No payments yet.

Stack: Starlette + uvicorn, Jinja2 templates, SQLite, WebSockets. Python 3.12.

## Run locally
```bash
cd web
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DEMO_SEED=1 DEMO_MODE=1 ADMIN_PASSWORD=change-me-please uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000. Admin: `/admin`. Run one process only (no `--workers`).

## Settings (environment variables)
| Variable | Purpose |
| --- | --- |
| `BASE_URL` | Public address (e-mail links, Google redirect). Required in production. |
| `SECRET_KEY` | Signs sessions. Required in production, otherwise sessions reset on restart. |
| `ADMIN_PASSWORD` | Turns on `/admin` (approve venues). `ADMIN_EMAIL` gets a mail for each new venue. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Outgoing mail (Gmail works with an app password). Without it mails are only logged. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google sign-in. Redirect URI: `BASE_URL/auth/google/callback`. |
| `TELEGRAM_BOT_TOKEN` | Create a bot with @BotFather. Venues connect chats in Settings → Telegram. |
| `TRUST_PROXY=1` | Behind Cloudflare or a proxy (real client IP for rate limits). |
| `DEMO_MODE=1` | Demo only: no e-mail confirmation needed to order. |
| `DATABASE_PATH` | SQLite file (default `web/data/app.db`). |

## GitHub Actions demo
Actions → **Run web demo**. The URL is in the run summary. Add secrets `ADMIN_PASSWORD` (required to approve venues), and optionally `DEMO_PASSWORD`, `SECRET_KEY`, `TELEGRAM_BOT_TOKEN`, `ADMIN_EMAIL`, `SMTP_*`.
The address changes every run, so Google sign-in does not work there (use e-mail sign-up); the database is temporary.

## Tests
`python tests/test_smoke.py` (needs `pip install httpx` for Starlette's test client).

See `SECURITY.md` for the security review.

## Stage 2 update
New: opening hours, preparation time, pause and capacity limits per venue; no-show grace period and per-venue blocking; order cancel by customer; automatic expiry of stale orders; venue password reset and change; navigation menu and category chips; protection against floods. Run tests: `cd web && python3 -I tests/test_smoke.py`. Run the server with ONE process (`--ws-max-size 1024`). See `docs/ROADMAP.md` for what is next.
