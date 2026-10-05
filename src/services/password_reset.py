"""
Forgot password / email password reset for customer accounts.

Belongs to the normal Staybot account system (src/services/accounts.py:
tenants, New / Existing Property Investors) and is deliberately independent of
the staff Team login (src/services/team_auth.py). Nothing in this module logs
anyone in: it never creates an account session, never sets a cookie, never
looks at ADMIN_PASSWORD and never changes a role. After a reset the person
goes back to /login and signs in with the new password.

Flow
    /login -> "Forgot password?" -> /forgot-password -> POST /auth/forgot-password
    -> email with /reset-password?token=... -> POST /auth/reset-password
    -> "Password Reset Successful" -> /login

Security
  * The token is 256 bits from secrets.token_urlsafe. Only its SHA-256 is
    stored (password_reset_tokens.token_hash); the raw token exists only in the
    email link. Raw tokens and passwords are never logged.
  * Tokens expire (PASSWORD_RESET_TTL_MINUTES) and are single use. A token is
    claimed with one atomic UPDATE ... WHERE used_at IS NULL, so two requests
    racing with the same link can't both succeed.
  * POST /auth/forgot-password answers identically whether or not the email has
    an account, and the lookup/send runs in a background task so the response
    time doesn't reveal it either.
  * The link is built from APP_BASE_URL / PUBLIC_BASE_URL, never from the
    request's Host header (that would let an attacker aim a victim's reset link
    at their own server).
  * A new request replaces older unused tokens for that account, and requests
    for the same account are throttled (RESEND_COOLDOWN_SECONDS) so the form
    can't be used to flood someone's inbox.
  * The staff admin account is not resettable here (it has its own hidden
    login, /admin/login); asking for it behaves like an unknown email.

.env:
    APP_BASE_URL / PUBLIC_BASE_URL=https://your-staybot-server.example.com
    PASSWORD_RESET_TTL_MINUTES=60            # optional, 5-1440, default 60
    PASSWORD_RESET_REVOKE_SESSIONS=false     # optional: true = also log the account out everywhere
Email goes through src/services/email_sender.py (EMAIL_SMTP_* settings).
Run supabase_password_reset.sql once to create the table.
"""

import hashlib
import html
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict

from src.services import accounts, db, email_sender
from src.services.accounts import now, now_iso

router = APIRouter(prefix="/auth", tags=["Password reset"])

GENERIC_MESSAGE = "If an account exists for this email address, a password reset link has been sent."
INVALID_LINK = "This password reset link is invalid or has expired. Please request a new one."
EMAIL_SUBJECT = "Staybot Password Reset"
LINK_GONE = 410                # unknown / expired / already-used link: all one answer, so nothing is revealed

RESEND_COOLDOWN_SECONDS = 60   # one reset email per account per minute
MIN_PASSWORD = 8               # same limits as registration (accounts.RegisterIn)
MAX_PASSWORD = 200
MAX_TOKEN_LENGTH = 200         # real tokens are 43 characters; refuse absurd input before hashing
DEFAULT_TTL_MINUTES = 60
LOCAL_BASE_URL = "http://127.0.0.1:8000"


# ---------------------------------------------------------------------
# Settings, tokens, helpers
# ---------------------------------------------------------------------

def ttl_minutes() -> int:
    """How long a reset link works. Read on every call so a changed .env value
    takes effect after a restart without code changes; clamped to 5 min - 24 h."""
    try:
        minutes = int((os.getenv("PASSWORD_RESET_TTL_MINUTES") or DEFAULT_TTL_MINUTES))
    except ValueError:
        minutes = DEFAULT_TTL_MINUTES
    return min(max(minutes, 5), 1440)


def revoke_sessions_on_reset() -> bool:
    return (os.getenv("PASSWORD_RESET_REVOKE_SESSIONS") or "").strip().lower() in ("1", "true", "yes", "on")


def base_url() -> str:
    """Public address of this server for the link in the email. Deliberately
    configuration only - see 'Security' above for why not the Host header."""
    configured = (os.getenv("APP_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not configured:
        print("Password reset: APP_BASE_URL / PUBLIC_BASE_URL is not set, so the emailed link points at "
              f"{LOCAL_BASE_URL}. Set it to this server's public address.")
        return LOCAL_BASE_URL
    return configured


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def reset_link(token: str) -> str:
    return f"{base_url()}/reset-password?token={token}"


def parse_time(value) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_email(link: str, minutes: int) -> tuple[str, str]:
    """(plain text, html) bodies for the reset email."""
    if minutes % 60 == 0:
        hours = minutes // 60
        lifetime = f"{hours} hour" + ("" if hours == 1 else "s")
    else:
        lifetime = f"{minutes} minutes"

    text = (
        "Hello,\n\n"
        "We received a request to reset your Staybot password.\n\n"
        "Open this link to create a new password:\n"
        f"{link}\n\n"
        f"This link will expire in {lifetime} and can only be used once.\n\n"
        "If you did not request this password reset, you can safely ignore this email. "
        "Your password will not change.\n\n"
        "Staybot\n"
    )
    safe_link = html.escape(link, quote=True)
    body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Arial,sans-serif;'
        'font-size:15px;line-height:1.55;color:#15191E;max-width:520px">'
        "<p>Hello,</p>"
        "<p>We received a request to reset your Staybot password.</p>"
        "<p>Click the button below to create a new password.</p>"
        f'<p style="margin:26px 0"><a href="{safe_link}" style="background:#1B3A5C;color:#ffffff;'
        'text-decoration:none;font-weight:600;padding:13px 26px;border-radius:8px;display:inline-block">'
        "Reset Password</a></p>"
        f'<p style="color:#5A6069;font-size:13px">If the button doesn\'t work, copy and paste this link into your browser:<br>'
        f'<a href="{safe_link}" style="color:#1B3A5C;word-break:break-all">{safe_link}</a></p>'
        f"<p>This link will expire in {html.escape(lifetime)} and can only be used once.</p>"
        "<p>If you did not request this password reset, you can safely ignore this email. "
        "Your password will not change.</p>"
        "<p>Staybot</p></div>"
    )
    return text, body


def _db(fn, *args, **kwargs):
    """Run a database call for the reset endpoint, turning failures into clear
    HTTP errors (and never echoing database text, which could hold secrets)."""
    accounts.configured()
    try:
        return fn(*args, **kwargs)
    except requests.RequestException as error:
        text = getattr(getattr(error, "response", None), "text", "") or str(error)
        if "PGRST205" in text or "does not exist" in text:
            raise HTTPException(503, "Run supabase_password_reset.sql in Supabase to enable password reset.")
        print(f"Password reset database error: {text[:300]}")
        raise HTTPException(500, "The password reset request failed. Please try again.")


# ---------------------------------------------------------------------
# Keep raw tokens out of the server's access log
# ---------------------------------------------------------------------

TOKEN_IN_URL = re.compile(r"([?&]token=)[^&\s\"]+")


class RedactTokens(logging.Filter):
    """uvicorn's access log prints every request URL, which for the emailed
    page GET /reset-password?token=... would write the live secret to the log."""

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(TOKEN_IN_URL.sub(r"\1[redacted]", a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = TOKEN_IN_URL.sub(r"\1[redacted]", record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(RedactTokens())


# ---------------------------------------------------------------------
# Step 1: ask for a reset link
# ---------------------------------------------------------------------

class ForgotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str


def send_reset_email_if_account_exists(email: str) -> None:
    """Runs after the HTTP response has been sent (see forgot_password), so what
    happens here - found / not found, throttled, sent - can't be observed by the
    caller. Never raises: problems are logged (without tokens) for the operator."""
    try:
        rows = db._get("accounts", {"email": f"eq.{email}", "select": "id,email,role", "limit": "1"})
        if not rows:
            return
        account = rows[0]
        if accounts.is_admin(account):
            return   # the staff admin account has its own login and is not reset by email

        # Throttle: an unused, unexpired link was issued very recently - it is
        # still in their inbox, so don't send another.
        latest = db._get("password_reset_tokens", {
            "account_id": f"eq.{account['id']}", "used_at": "is.null",
            "select": "created_at,expires_at", "order": "created_at.desc", "limit": "1",
        })
        if latest:
            issued = parse_time(latest[0]["created_at"])
            still_valid = parse_time(latest[0]["expires_at"]) > now()
            if still_valid and (now() - issued).total_seconds() < RESEND_COOLDOWN_SECONDS:
                return

        minutes = ttl_minutes()
        token = new_token()
        row = db._post("password_reset_tokens", {
            "account_id": account["id"],
            "token_hash": hash_token(token),
            "expires_at": (now() + timedelta(minutes=minutes)).isoformat(),
        })

        # Only the newest link works: retire every other unused one.
        try:
            db._patch("password_reset_tokens", {"used_at": now_iso()}, {
                "account_id": f"eq.{account['id']}", "used_at": "is.null", "id": f"neq.{row['id']}",
            })
        except Exception as error:
            print(f"Retiring older password reset links failed (non-fatal): {error}")

        text, body = build_email(reset_link(token), minutes)
        result = email_sender.send(account["email"], EMAIL_SUBJECT, text, body)
        if not result.get("ok"):
            print(f"Password reset email for account {account['id']} was not sent: {result.get('error')}")
        elif result.get("test_mode"):
            print(f"Password reset for account {account['id']}: email is in TEST MODE, nothing was sent "
                  "(set EMAIL_SMTP_* and EMAIL_DRY_RUN=false to send real emails).")
    except Exception as error:
        print(f"Password reset request failed (has supabase_password_reset.sql been run?): {error}")


@router.post("/forgot-password")
def forgot_password(body: ForgotIn, background: BackgroundTasks):
    """Always the same answer for any well-formed email (no account enumeration).
    Never authenticates anyone."""
    accounts.configured()
    try:
        email = accounts.valid_email(body.email)
    except ValueError as error:
        raise HTTPException(400, str(error))
    background.add_task(send_reset_email_if_account_exists, email)
    return {"message": GENERIC_MESSAGE}


# ---------------------------------------------------------------------
# Step 2: choose a new password
# ---------------------------------------------------------------------

class ResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str
    password: str
    confirm_password: str


@router.post("/reset-password")
def reset_password(body: ResetIn):
    """Spends a valid reset token to set a new password. Does NOT log the person
    in: no session, no cookie - they sign in at /login with the new password.

    400 = the new password isn't acceptable (the link is untouched, try again);
    410 = the link is unknown, expired or already used - the person must ask for
    a new one. Both 4xx bodies are {"detail": "<message>"} like the rest of /auth."""

    # Cheap input checks first, so a typo never burns the one-time link.
    if len(body.password) < MIN_PASSWORD:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD} characters.")
    if len(body.password) > MAX_PASSWORD:
        raise HTTPException(400, f"Password must be at most {MAX_PASSWORD} characters.")
    if body.password != body.confirm_password:
        raise HTTPException(400, "Passwords do not match.")

    token = body.token.strip()
    if not token or len(token) > MAX_TOKEN_LENGTH:
        raise HTTPException(LINK_GONE, INVALID_LINK)

    rows = _db(db._get, "password_reset_tokens", {"token_hash": f"eq.{hash_token(token)}", "select": "*", "limit": "1"})
    record = rows[0] if rows else None
    if not record or record.get("used_at") or parse_time(record["expires_at"]) <= now():
        raise HTTPException(LINK_GONE, INVALID_LINK)

    # Spend the token atomically. If another request got there first this
    # matches no row and we stop - one link can only ever set one password.
    claimed = _db(db._patch, "password_reset_tokens", {"used_at": now_iso()},
                  {"id": f"eq.{record['id']}", "used_at": "is.null"})
    if not claimed:
        raise HTTPException(LINK_GONE, INVALID_LINK)

    account_id = record["account_id"]
    try:
        found = db._get("accounts", {"id": f"eq.{account_id}", "select": "id,role", "limit": "1"})
        if not found or accounts.is_admin(found[0]):
            raise HTTPException(LINK_GONE, INVALID_LINK)
        password_hash, salt = accounts.hash_password(body.password)
        # Only the credential columns: role, session_id, investor_id etc. are untouched.
        db._patch("accounts", {"password_hash": password_hash, "password_salt": salt}, {"id": f"eq.{account_id}"})
    except HTTPException:
        raise
    except Exception as error:
        print(f"Password reset could not update account {account_id}: {error.__class__.__name__}")
        try:   # give the link back so the person can try again
            db._patch("password_reset_tokens", {"used_at": None}, {"id": f"eq.{record['id']}"})
        except Exception as undo_error:
            print(f"Releasing the reset link failed: {undo_error.__class__.__name__}")
        raise HTTPException(500, "The password reset request failed. Please try again.")

    # Any other link still outstanding for this account dies with this reset.
    try:
        db._patch("password_reset_tokens", {"used_at": now_iso()}, {"account_id": f"eq.{account_id}", "used_at": "is.null"})
    except Exception as error:
        print(f"Retiring other password reset links failed (non-fatal): {error}")

    # Optional (PASSWORD_RESET_REVOKE_SESSIONS=true): also sign the account out of
    # every device. Off by default so existing sessions are left exactly as they were.
    if revoke_sessions_on_reset():
        try:
            db._delete("account_sessions", {"account_id": f"eq.{account_id}"})
        except Exception as error:
            print(f"Revoking sessions after password reset failed (non-fatal): {error}")

    return {"status": "ok", "message": "Your password has been updated successfully."}
