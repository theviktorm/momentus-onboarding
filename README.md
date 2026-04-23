# Momentus — Client Onboarding

Standalone Hungarian Typeform-style onboarding form. Submissions are saved to SQLite and emailed to the configured recipient.

## Run locally

```bash
pip install -r requirements.txt
python start.py
# → http://localhost:8100
```

## Environment variables

| Var | Required | Default | Purpose |
|-----|----------|---------|---------|
| `PORT` | no | `8100` | HTTP port |
| `SMTP_HOST` | for email | — | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | no | `587` | `465` for SSL, `587` for STARTTLS |
| `SMTP_USER` | for email | — | SMTP username |
| `SMTP_PASS` | for email | — | SMTP password / app password |
| `SMTP_FROM` | no | `SMTP_USER` | From address |
| `SMTP_TO` | no | `v.mozsa@gmail.com` | Recipient |
| `DB_PATH` | no | `submissions.db` | SQLite path |

If SMTP is not configured, submissions still save to the DB — the email step is skipped and logged.

## Deploy (Railway)

1. Connect repo, Railway auto-detects `Dockerfile`.
2. Set the env vars above (at minimum `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`).
3. Leave **Custom Start Command** empty.

Gmail: create an [App Password](https://myaccount.google.com/apppasswords), use `smtp.gmail.com` / `587`.

## Endpoints

- `GET /` — onboarding form
- `POST /submit` — `{ "answers": {...} }` → saves + emails
- `GET /api/health` — health check
