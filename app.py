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
import sqlite3
import ssl
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


def send_email_sync(row_id: str, answers: Dict[str, Any]) -> bool:
    """Blocking SMTP send. Call via threadpool or background task."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        log.warning("SMTP not configured — skipping email for %s", row_id)
        return False

    company = str(answers.get("company_name", "")).strip() or "Ismeretlen cég"
    subject = f"[Momentus Onboarding] {company}"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = SMTP_TO
    msg["Reply-To"] = str(answers.get("contact_email", "")) or SMTP_FROM

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

    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    try:
        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=15) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        log.info("Email sent for %s → %s", row_id, SMTP_TO)
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
    log.info("Onboarding ready. DB=%s  SMTP=%s  TO=%s", DB_PATH, bool(SMTP_HOST), SMTP_TO)


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "templates" / "index.html", media_type="text/html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
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
