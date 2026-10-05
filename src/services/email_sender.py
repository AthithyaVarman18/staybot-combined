"""
Outgoing email over SMTP (works with Gmail app passwords, Zoho, Amazon SES,
Resend SMTP, SendGrid SMTP...).

TEST MODE (default until EMAIL_SMTP_HOST is set and EMAIL_DRY_RUN=false):
nothing is sent; emails go to an in-memory outbox shown in the Owner leads tab.

.env:
    EMAIL_DRY_RUN=true
    EMAIL_SMTP_HOST=smtp.gmail.com
    EMAIL_SMTP_PORT=587                 # 587 = STARTTLS, 465 = SSL
    EMAIL_SMTP_USER=you@yourdomain.com
    EMAIL_SMTP_PASSWORD=app-password    # never commit this
    EMAIL_FROM=Staybot <you@yourdomain.com>
    BUSINESS_POSTAL_ADDRESS=...         # shown in every outreach email footer
"""

import os
import smtplib
import ssl
from collections import deque
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid

from dotenv import load_dotenv

load_dotenv()

HOST = (os.getenv("EMAIL_SMTP_HOST") or "").strip()
PORT = int((os.getenv("EMAIL_SMTP_PORT") or "587").strip() or 587)
USER = (os.getenv("EMAIL_SMTP_USER") or "").strip()
PASSWORD = (os.getenv("EMAIL_SMTP_PASSWORD") or "").strip()
FROM = (os.getenv("EMAIL_FROM") or USER or "Staybot <no-reply@example.com>").strip()
DRY_RUN = (os.getenv("EMAIL_DRY_RUN", "true").strip().lower() != "false") or not (HOST and USER and PASSWORD)
OUTBOX = deque(maxlen=300)


def status():
    return {"mode": "test" if DRY_RUN else "live", "from": FROM, "host": HOST or None,
            "label": "Test mode: emails are not sent, they appear in the test outbox" if DRY_RUN else f"Live: sending from {FROM}"}


def send(to: str, subject: str, text: str, html: str, unsubscribe_url: str | None = None) -> dict:
    """Returns {"ok": bool, "test_mode": bool, "error": str|None}."""
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = FROM, to, subject
    msg["Message-ID"] = make_msgid(domain=(FROM.split("@")[-1].strip("> ") or "localhost"))
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    if DRY_RUN:
        OUTBOX.append({"to": to, "subject": subject, "text": text, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return {"ok": True, "test_mode": True, "error": None}
    try:
        if PORT == 465:
            with smtplib.SMTP_SSL(HOST, PORT, context=ssl.create_default_context(), timeout=30) as smtp:
                smtp.login(USER, PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(HOST, PORT, timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(USER, PASSWORD)
                smtp.send_message(msg)
        return {"ok": True, "test_mode": False, "error": None}
    except Exception as error:
        return {"ok": False, "test_mode": False, "error": str(error)[:300]}
