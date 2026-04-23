"""
Momentus — Client Onboarding
Standalone Typeform-style form. Saves submissions to SQLite and emails them
to the configured recipient. Zero dependencies on the GEO tool.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import socket
import sqlite3
import ssl
import urllib.request
import urllib.error
import uuid
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict

import anyio
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ─────────────────────────────────────────────────────────────
# CONFIG (env-driven)
# ─────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "submissions.db"))

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "noreply@momentus.hu")
SMTP_TO = os.environ.get("SMTP_TO", "v.mozsa@gmail.com")

# Resend HTTP API (preferred — Railway blocks outbound SMTP).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "Momentus Onboarding <onboarding@resend.dev>")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("onboarding")

# ─────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────
def db_init() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            company_name TEXT,
            contact_email TEXT,
            answers_json TEXT NOT NULL,
            emailed INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def db_insert(row_id: str, answers: Dict[str, Any]) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO submissions (id, created_at, company_name, contact_email, answers_json, emailed) VALUES (?, ?, ?, ?, ?, ?)",
        (
            row_id,
            datetime.utcnow().isoformat(),
            str(answers.get("company_name", ""))[:200],
            str(answers.get("contact_email", ""))[:200],
            json.dumps(answers, ensure_ascii=False),
            0,
        ),
    )
    conn.commit()
    conn.close()


def db_mark_emailed(row_id: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE submissions SET emailed = 1 WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────
def format_answers_html(answers: Dict[str, Any]) -> str:
    rows = []
    for k, v in answers.items():
        if isinstance(v, list):
            val = ", ".join(str(x) for x in v)
        elif isinstance(v, dict):
            val = json.dumps(v, ensure_ascii=False)
        else:
            val = str(v)
        val = (val or "").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
        rows.append(
            f'<tr><td style="padding:8px 14px;border-bottom:1px solid #eee;font-family:monospace;color:#666;vertical-align:top;white-space:nowrap">{k}</td>'
            f'<td style="padding:8px 14px;border-bottom:1px solid #eee;color:#111">{val}</td></tr>'
        )
    return (
        '<table style="border-collapse:collapse;width:100%;max-width:720px;font-family:system-ui,-apple-system,sans-serif;font-size:14px">'
        + "".join(rows)
        + "</table>"
    )


def format_answers_text(answers: Dict[str, Any]) -> str:
    lines = []
    for k, v in answers.items():
        if isinstance(v, list):
            val = ", ".join(str(x) for x in v)
        elif isinstance(v, dict):
            val = json.dumps(v, ensure_ascii=False)
        else:
            val = str(v)
        lines.append(f"{k}:\n{val}\n")
    return "\n".join(lines)


def send_via_resend(subject: str, text: str, html: str, reply_to: str, row_id: str) -> bool:
    """Send via Resend HTTPS API. Works on Railway (SMTP outbound is blocked)."""
    if not RESEND_API_KEY:
        return False
    body = {
        "from": RESEND_FROM,
        "to": [SMTP_TO],
        "subject": subject,
        "text": text,
        "html": html,
    }
    if reply_to:
        body["reply_to"] = reply_to
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
            log.info("Resend OK for %s → %s: %s", row_id, SMTP_TO, raw[:200])
            return True
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
        log.error("Resend HTTP %s for %s: %s", e.code, row_id, body_txt[:300])
        return False
    except Exception as e:
        log.error("Resend failed for %s: %s: %s", row_id, type(e).__name__, e)
        return False


def send_email_sync(row_id: str, answers: Dict[str, Any]) -> bool:
    """Send the onboarding email. Prefers Resend HTTPS; falls back to SMTP."""
    company = str(answers.get("company_name", "")).strip() or "Ismeretlen cég"
    subject = f"[Momentus Onboarding] {company}"

    reply_to = str(answers.get("contact_email", "")) or ""
    text = (
        f"Új onboarding beérkezett.\n\n"
        f"ID: {row_id}\n"
        f"Időpont (UTC): {datetime.utcnow().isoformat()}\n\n"
        f"{format_answers_text(answers)}"
    )
    html = (
        f'<div style="font-family:system-ui,-apple-system,sans-serif;max-width:720px">'
        f'<h2 style="font-family:Georgia,serif;color:#111">Új onboarding — {company}</h2>'
        f'<p style="color:#666;font-size:13px">ID: <code>{row_id}</code> · {datetime.utcnow().isoformat()} UTC</p>'
        f'{format_answers_html(answers)}'
        f'</div>'
    )

    if RESEND_API_KEY:
        if send_via_resend(subject, text, html, reply_to, row_id):
            return True
        log.warning("Resend failed — falling back to SMTP for %s", row_id)

    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        log.warning("No email transport configured — skipping email for %s", row_id)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = SMTP_TO
    msg["Reply-To"] = reply_to or SMTP_FROM
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    # Resolve to IPv4 explicitly — Railway containers often lack IPv6 outbound,
    # and Python's default getaddrinfo prefers AAAA → ENETUNREACH.
    try:
        infos = socket.getaddrinfo(SMTP_HOST, SMTP_PORT, socket.AF_INET, socket.SOCK_STREAM)
        ipv4_host = infos[0][4][0]
    except Exception as e:
        log.error("DNS A-record lookup failed for %s: %s: %s", SMTP_HOST, type(e).__name__, e)
        ipv4_host = SMTP_HOST

    try:
        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(ipv4_host, SMTP_PORT, context=ctx, timeout=20) as s:
                s.ehlo(SMTP_HOST)
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(ipv4_host, SMTP_PORT, timeout=20) as s:
                s.ehlo(SMTP_HOST)
                s.starttls(context=ctx)
                s.ehlo(SMTP_HOST)
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        log.info("Email sent for %s → %s (via %s)", row_id, SMTP_TO, ipv4_host)
        return True
    except Exception as e:
        log.error("Email failed for %s: %s: %s", row_id, type(e).__name__, e)
        return False


def send_email_and_mark(row_id: str, answers: Dict[str, Any]) -> None:
    """Background task: send email, update DB emailed flag on success."""
    ok = send_email_sync(row_id, answers)
    if ok:
        try:
            db_mark_emailed(row_id)
        except Exception as e:
            log.error("db_mark_emailed failed for %s: %s", row_id, e)


# ─────────────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="Momentus Onboarding", version="1.0.0")

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
async def _startup():
    db_init()
    log.info(
        "Onboarding ready. DB=%s  RESEND=%s  SMTP=%s  TO=%s",
        DB_PATH, bool(RESEND_API_KEY), bool(SMTP_HOST), SMTP_TO,
    )


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "templates" / "index.html", media_type="text/html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "resend_configured": bool(RESEND_API_KEY),
        "resend_from": RESEND_FROM if RESEND_API_KEY else None,
        "smtp_configured": bool(SMTP_HOST and SMTP_USER and SMTP_PASS),
        "recipient": SMTP_TO,
    }


@app.post("/submit")
async def submit(request: Request, background: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    answers = payload.get("answers") or {}
    if not isinstance(answers, dict) or not answers:
        raise HTTPException(400, "No answers provided")

    row_id = "sub-" + uuid.uuid4().hex[:12]
    # DB write is fast (local sqlite) — run in threadpool anyway to keep loop clear.
    await anyio.to_thread.run_sync(db_insert, row_id, answers)

    # Email send is slow + can hang. Queue as background task so we respond
    # immediately and the user never sees a 503 from Railway's edge timing out.
    background.add_task(send_email_and_mark, row_id, answers)

    return JSONResponse({"success": True, "data": {"submission_id": row_id, "queued": True}})


@app.get("/api/test-email")
async def test_email():
    """Synchronous send so we can see the error inline."""
    ok = await anyio.to_thread.run_sync(
        send_email_sync,
        "test-" + uuid.uuid4().hex[:6],
        {"company_name": "TEST", "contact_email": SMTP_TO, "note": "Test from /api/test-email"},
    )
    return {"sent": ok, "resend_configured": bool(RESEND_API_KEY), "smtp_configured": bool(SMTP_HOST and SMTP_USER and SMTP_PASS)}


@app.post("/api/resend/{row_id}")
async def resend(row_id: str, background: BackgroundTasks):
    """Manual retry hook — re-fetches answers from DB and re-queues email."""
    def _fetch():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("SELECT answers_json FROM submissions WHERE id = ?", (row_id,))
        row = cur.fetchone()
        conn.close()
        return row
    row = await anyio.to_thread.run_sync(_fetch)
    if not row:
        raise HTTPException(404, "Submission not found")
    answers = json.loads(row[0])
    background.add_task(send_email_and_mark, row_id, answers)
    return {"success": True, "data": {"submission_id": row_id, "queued": True}}
