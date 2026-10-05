"""
End-user accounts: real login/register for tenants and investors.

Separate from src/services/team_auth.py, which gates the staff dashboard
behind one shared password. This module is customer-facing: anyone can
register, pick a workflow (Tenant / New Property Investor / Existing
Property Investor), and log back in.

Each account owns a fixed `session_id` - the same kind of id the anonymous
web chat has always generated client-side and keyed conversations/investors
on (see getSessionId() in index.html, investor_journey.find_open()). Logging in
just makes that session_id durable and tied to a real identity instead of
"whatever's in this browser's localStorage", so returning on another device
resumes the same conversation and investor profile. Registering as either
investor role creates the investors row immediately, already classified and
on the right journey (see src/services/investor_journey.py) - the AI doesn't have
to infer it from the conversation first.

Passwords: PBKDF2-HMAC-SHA256 (stdlib hashlib, no extra dependency), a
random salt per account, a constant-time comparison on login.
Sessions: a random token is handed to the browser as an httponly cookie;
only its SHA-256 is stored, so a leaked database dump doesn't hand out
working session tokens.
"""

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

import requests
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.services import db

router = APIRouter(prefix="/auth", tags=["Accounts"])

COOKIE_NAME = "staybot_account_session"
# Per-tab login (see src/static/tab-session.js). A cookie is shared by every
# tab/window of the browser, so logging in as an investor in a second window
# used to silently turn an already-open tenant window into that investor
# (its Portfolio, chat, etc. all followed the newest cookie). Now each tab
# keeps its own session token in sessionStorage and sends it on every API
# call in this header; when present it wins over the cookie. The cookie is
# still set - it's what full-page navigations (/ui, /portal, ...) use - and
# the focused tab re-points it at its own session via POST /auth/activate.
SESSION_HEADER = "X-Staybot-Session"
ISSUED_HEADER = "X-Staybot-Session-Issued"    # login/register/admin login: new token for this tab
CLEARED_HEADER = "X-Staybot-Session-Cleared"  # logout: this tab should forget its token
POST_AUTH_REDIRECT = "/ui"  # where register/login send the browser
EDUCATION_REDIRECT = "/investor-education"  # New Property Investor: shown once before the dashboard
SCREENING_REDIRECT = "/tenant-screening"  # Tenant: shown once before the dashboard
# New Property Investor who got a property and was moved to existing_investor:
# shown once before the dashboard (accounts.existing_welcome_pending).
EXISTING_WELCOME_REDIRECT = "/existing-investor-welcome"
REQUIREMENTS_REDIRECT = "/requirements"
SESSION_DAYS = 30
PBKDF2_ITERATIONS = 260_000
MAX_UPLOAD = 10 * 1024 * 1024
FILE_TYPES = {"application/pdf": b"%PDF-", "image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}

ROLE_LABELS = {
    "tenant": "Tenant",
    "new_investor": "New Property Investor",
    "existing_investor": "Existing Property Investor",
    "admin": "Admin",
}

# The single staff admin account. It is never created through /auth/register
# (RegisterIn only accepts the three customer roles) - it's inserted straight
# into the database with supabase_admin.sql. It logs in only through the
# hidden /admin/login page (POST /auth/admin/login below); the public
# /auth/login refuses it, so the customer login page never reveals it exists.
ADMIN_ROLE = "admin"
ADMIN_REDIRECT = "/ui"


def is_admin(account: dict | None) -> bool:
    return bool(account) and account.get("role") == ADMIN_ROLE


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_accounts.sql (and supabase_investors.sql) to enable accounts.")


def q(fn, *args, **kwargs):
    configured()
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except requests.RequestException as error:
        text = getattr(getattr(error, "response", None), "text", "") or str(error)
        if "PGRST205" in text or "does not exist" in text:
            raise HTTPException(503, "Run supabase_accounts.sql in Supabase to enable accounts.")
        if "accounts_email_key" in text or "duplicate key" in text.lower():
            raise HTTPException(409, "An account with this email already exists.")
        print(f"Accounts database error: {text[:300]}")
        raise HTTPException(500, "The accounts database request failed. Please try again.")


def now():
    return datetime.now(timezone.utc)


def now_iso():
    return now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, password_hash)


# ---------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------

def set_session_cookie(response: Response, token: str):
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_DAYS * 86400, httponly=True,
        samesite="lax", path="/",
    )


def issue_session(account_id: str, response: Response):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db._post("account_sessions", {
        "account_id": account_id, "token_hash": token_hash,
        "expires_at": (now() + timedelta(days=SESSION_DAYS)).isoformat(),
    })
    set_session_cookie(response, token)
    # Handed to this tab's tab-session.js, which keeps it in sessionStorage
    # so other tabs logging in as someone else can't take this tab over.
    response.headers[ISSUED_HEADER] = token


def record_login(account: dict):
    """Journey tracker (src/services/tenant_journeys.py): when this account last
    logged in, and how often. Registering counts as the first login - that's
    when a tenant's journey starts. Best-effort: needs supabase_tenant_journeys.sql,
    and until that's run (or if it fails) logging in works exactly as before."""
    if not db.ENABLED or not account or not account.get("id"):
        return
    try:
        db._patch("accounts", {"last_login_at": now_iso(), "login_count": int(account.get("login_count") or 0) + 1},
                  {"id": f"eq.{account['id']}"})
    except Exception as e:
        print(f"Recording login failed (non-fatal - has supabase_tenant_journeys.sql been run?): {e}")


def request_token(request: Request) -> str | None:
    """This tab's session token (header) if it sent one, else the shared
    browser cookie. No fallback from a header to the cookie: a tab whose own
    session is gone must be logged out, not quietly switched to whichever
    account last logged in from another window."""

    header = (request.headers.get(SESSION_HEADER) or "").strip()
    if header:
        return header
    return request.cookies.get(COOKIE_NAME) or None


def current_account(request: Request) -> dict | None:
    """The logged-in account for this request, or None - never raises, so
    every public page/endpoint can call this freely without a try/except."""

    if not db.ENABLED:
        return None

    token = request_token(request)
    if not token:
        return None

    try:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        sessions = db._get("account_sessions", {"token_hash": f"eq.{token_hash}", "select": "*", "limit": "1"})
        if not sessions:
            return None
        session = sessions[0]
        if datetime.fromisoformat(session["expires_at"].replace("Z", "+00:00")) < now():
            return None
        accounts = db._get("accounts", {"id": f"eq.{session['account_id']}", "select": "*", "limit": "1"})
        return accounts[0] if accounts else None
    except Exception as e:
        print(f"Account session lookup failed (non-fatal): {e}")
        return None


def account_view(account: dict) -> dict:
    return {
        "id": account["id"], "name": account["name"], "email": account["email"],
        "role": account["role"], "role_label": ROLE_LABELS.get(account["role"], account["role"]),
        "session_id": account["session_id"], "investor_id": account.get("investor_id"),
        "education_seen": bool(account.get("education_seen")),
        "screening_seen": bool(account.get("screening_seen")),
        "existing_welcome_pending": existing_welcome_pending(account),
    }


def existing_welcome_pending(account: dict | None) -> bool:
    """Was this account just moved from New to Existing Property Investor,
    and hasn't seen the one-time welcome page yet?"""
    return bool(account and account.get("role") == "existing_investor" and account.get("existing_welcome_pending"))


def post_auth_redirect(account: dict) -> str:
    """Where register/login send the browser. New Property Investor accounts
    get routed through the one-time real-estate-basics page first (unless
    they've already been through it); tenant accounts get routed through the
    one-time screening intake first (unless already done); everyone else goes
    straight to the dashboard, unchanged from before."""

    if account.get("role") == "new_investor" and not account.get("education_seen"):
        return EDUCATION_REDIRECT
    if account.get("role") == "tenant" and not account.get("screening_seen"):
        return SCREENING_REDIRECT
    if existing_welcome_pending(account):
        return EXISTING_WELCOME_REDIRECT
    if account.get("role") in ("tenant", "new_investor", "existing_investor"):
        try:
            from src.services import requirements
            if not requirements.has_active(account["id"], account["role"]):
                return REQUIREMENTS_REDIRECT
        except Exception as e:
            print(f"Requirements redirect check failed (non-fatal): {e}")
    return POST_AUTH_REDIRECT


# ---------------------------------------------------------------------
# Investor pre-classification at signup
# ---------------------------------------------------------------------

def create_investor_for_signup(name: str, role: str, session_id: str, existing_property_count: int | None) -> str | None:
    """New Property Investor / Existing Property Investor accounts get their
    investors row created immediately, already on the right journey -
    investor_journey.find_open() picks it up by session_id as soon as they chat,
    same as any AI-classified investor (see src/services/investor_journey.py)."""

    from src.services.investor_journey import JOURNEYS  # local import: investors.py doesn't import accounts.py

    investor_type = "existing" if role == "existing_investor" else "new"
    count = existing_property_count if existing_property_count is not None else (1 if investor_type == "existing" else 0)

    row = db._post("investors", {
        "name": name, "session_id": session_id, "investor_type": investor_type,
        "journey": JOURNEYS[investor_type]["key"], "stage": "lead_generation", "stage_index": 0,
        "existing_property_count": count,
    })
    db._post("investor_activity", {"investor_id": row["id"], "actor": "signup", "action": "profile_started", "details": {"via": "registration form"}})
    return row["id"] if row else None


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(value: str) -> str:
    value = value.strip().lower()
    if not EMAIL_RE.match(value) or len(value) > 200:
        raise ValueError("Enter a valid email address.")
    return value


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    role: Literal["tenant", "new_investor", "existing_investor"]
    existing_property_count: int | None = Field(default=None, ge=1, le=1000)

    _clean_email = field_validator("email")(valid_email)


@router.post("/register")
def register(body: RegisterIn, response: Response):
    configured()

    existing = q(db._get, "accounts", {"email": f"eq.{body.email.lower()}", "select": "id", "limit": "1"})
    if existing:
        raise HTTPException(409, "An account with this email already exists.")

    if body.role == "existing_investor" and not body.existing_property_count:
        raise HTTPException(400, "Tell us how many properties you already own (1 or more) for the Existing Property Investor workflow.")

    session_id = f"acct-{secrets.token_hex(12)}"
    password_hash, salt = hash_password(body.password)

    investor_id = None
    if body.role in ("new_investor", "existing_investor"):
        try:
            investor_id = create_investor_for_signup(
                body.name, body.role, session_id,
                body.existing_property_count if body.role == "existing_investor" else 0,
            )
        except Exception as e:
            print(f"Creating investor at signup failed (non-fatal, chat will create it instead): {e}")

    account = q(db._post, "accounts", {
        "name": body.name.strip(), "email": body.email.lower(), "password_hash": password_hash,
        "password_salt": salt, "role": body.role, "session_id": session_id, "investor_id": investor_id,
    })

    try:
        from src.services import portfolio  # local import: portfolio.py doesn't import accounts.py
        portfolio.seed_from_registration(
            account["id"], name=account["name"], email=account["email"],
            existing_property_count=body.existing_property_count if body.role == "existing_investor" else None,
        )
    except Exception as e:
        print(f"Portfolio seed at registration failed (non-fatal): {e}")

    issue_session(account["id"], response)
    record_login(account)
    return {"account": account_view(account), "redirect_url": post_auth_redirect(account)}


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)
    # Which pill the person had selected on the login page (login.html).
    # Optional so any older/other client that doesn't send it still works
    # exactly as before; when present, it must match the account's real
    # role - stops someone logging into a tenant account while "Existing
    # Property Investor" is selected (or vice versa).
    role: Literal["tenant", "new_investor", "existing_investor"] | None = None

    _clean_email = field_validator("email")(valid_email)


@router.post("/login")
def login(body: LoginIn, response: Response):
    configured()
    rows = q(db._get, "accounts", {"email": f"eq.{body.email.lower()}", "select": "*", "limit": "1"})
    if not rows or not verify_password(body.password, rows[0]["password_hash"], rows[0]["password_salt"]):
        raise HTTPException(401, "Incorrect email or password.")

    account = rows[0]

    # The admin account only logs in via the hidden /admin/login page. Same
    # message as a wrong password, so this form doesn't confirm the email exists.
    if is_admin(account):
        raise HTTPException(401, "Incorrect email or password.")

    if body.role and body.role != account["role"]:
        actual_label = ROLE_LABELS.get(account["role"], account["role"])
        raise HTTPException(
            401,
            f"This account is registered as {actual_label}. Switch to \"{actual_label}\" above and try again.",
        )

    issue_session(account["id"], response)
    record_login(account)
    return {"account": account_view(account), "redirect_url": post_auth_redirect(account)}


class AdminLoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)

    _clean_email = field_validator("email")(valid_email)


@router.post("/admin/login", include_in_schema=False)
def admin_login(body: AdminLoginIn, response: Response):
    """Login for the hidden /admin/login page. Only accepts the admin account;
    a customer's credentials get the same generic error as a wrong password."""

    configured()
    rows = q(db._get, "accounts", {"email": f"eq.{body.email.lower()}", "select": "*", "limit": "1"})
    if (
        not rows
        or not is_admin(rows[0])
        or not verify_password(body.password, rows[0]["password_hash"], rows[0]["password_salt"])
    ):
        raise HTTPException(401, "Incorrect email or password.")

    issue_session(rows[0]["id"], response)
    return {"account": account_view(rows[0]), "redirect_url": ADMIN_REDIRECT}


@router.post("/logout")
def logout(request: Request, response: Response):
    # Log out only this tab's session. The browser cookie is cleared only if
    # it points at that same session - if another window is logged in as a
    # different account, logging out here must not log that window out too.
    token = request_token(request)
    cookie_token = request.cookies.get(COOKIE_NAME)
    if token and db.ENABLED:
        try:
            db._delete("account_sessions", {"token_hash": f"eq.{hashlib.sha256(token.encode()).hexdigest()}"})
        except Exception as e:
            print(f"Session delete failed (non-fatal): {e}")
    if not cookie_token or cookie_token == token:
        response.delete_cookie(COOKIE_NAME, path="/")
    response.headers[CLEARED_HEADER] = "1"
    return {"status": "ok"}


@router.post("/activate")
def activate(request: Request, response: Response):
    """Called by tab-session.js when a tab gets focus: points the browser
    cookie back at this tab's own session, so full-page navigations and
    plain links from this tab (/ui, /portal, document links, ...) act as the
    account this tab is logged in as - not whoever logged in most recently
    in another window."""

    token = (request.headers.get(SESSION_HEADER) or "").strip()
    if not token:
        raise HTTPException(400, "No tab session.")
    account = current_account(request)
    if not account:
        raise HTTPException(401, "Not logged in.")
    if request.cookies.get(COOKIE_NAME) != token:
        set_session_cookie(response, token)
    return {"status": "ok", "role": account["role"]}


@router.get("/me")
def me(request: Request, response: Response):
    account = current_account(request)
    if not account:
        raise HTTPException(401, "Not logged in.")
    # A tab with no token of its own (opened by typing /ui, a bookmark, a
    # link...) is riding on the shared cookie, so a login in another window
    # would silently turn it into that account. Give it its own session for
    # the account it opened as, so it stays that account.
    if not (request.headers.get(SESSION_HEADER) or "").strip():
        try:
            token = secrets.token_urlsafe(32)
            db._post("account_sessions", {
                "account_id": account["id"], "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "expires_at": (now() + timedelta(days=SESSION_DAYS)).isoformat(),
            })
            response.headers[ISSUED_HEADER] = token
        except Exception as e:
            print(f"Pinning tab session failed (non-fatal): {e}")
    return account_view(account)


@router.get("/portfolio")
def my_portfolio(request: Request):
    """The details the AI has picked up about this account across every
    chat turn (src/services/portfolio.py), for the account's own dashboard."""

    account = current_account(request)
    if not account:
        raise HTTPException(401, "Not logged in.")

    from src.services import portfolio  # local import: portfolio.py doesn't import accounts.py
    return portfolio.get_portfolio(account["id"])


@router.post("/education-complete")
def education_complete(request: Request):
    """The New Property Investor real-estate-basics page (see /investor-education)
    calls this once the person clicks through, so it isn't shown again on their
    next login. A no-op for other roles, so the page can call it unconditionally."""

    account = current_account(request)
    if not account:
        raise HTTPException(401, "Not logged in.")
    if not account.get("education_seen"):
        q(db._patch, "accounts", {"education_seen": True}, {"id": f"eq.{account['id']}"})
        account = {**account, "education_seen": True}
    return {"account": account_view(account), "redirect_url": post_auth_redirect(account)}


# ---------------------------------------------------------------------
# New -> Existing Property Investor: one-time welcome (see /existing-investor-welcome)
# ---------------------------------------------------------------------

@router.get("/existing-welcome")
def existing_welcome(request: Request):
    """What the welcome page shows: the account, and the home(s) that made
    them an Existing Property Investor (newest first)."""

    account = current_account(request)
    if not account:
        raise HTTPException(401, "Not logged in.")
    homes, owned = [], 0
    if db.ENABLED and account.get("investor_id"):
        try:
            rows = db._get("investor_portfolio_properties", {
                "investor_id": f"eq.{account['investor_id']}", "relationship": "in.(owned,acquired)",
                "select": "address,property_type,bedrooms,monthly_rent,estimated_value,purchase_price,property_id",
                "order": "created_at.desc"}) or []
            owned = len(rows)
            homes = rows[:3]
        except Exception as e:
            print(f"Loading the welcome page homes failed (non-fatal): {e}")
    return {"account": account_view(account), "homes": homes, "owned_count": owned}


@router.post("/existing-welcome-complete")
def existing_welcome_complete(request: Request):
    """The welcome page calls this when the person moves on, so it's never
    shown again. A no-op for anyone without a pending welcome."""

    account = current_account(request)
    if not account:
        raise HTTPException(401, "Not logged in.")
    if account.get("existing_welcome_pending"):
        q(db._patch, "accounts", {"existing_welcome_pending": False}, {"id": f"eq.{account['id']}"})
        account = {**account, "existing_welcome_pending": False}
    return {"account": account_view(account), "redirect_url": post_auth_redirect(account)}


# ---------------------------------------------------------------------
# Tenant screening: one-time mandatory intake (see /tenant-screening)
# ---------------------------------------------------------------------

class ScreeningDetailsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    phone: str = Field(min_length=3, max_length=40)
    employment_status: Literal["employed", "self_employed", "unemployed", "student", "retired"]
    monthly_income: float = Field(ge=0, le=100_000_000)
    annual_income: float = Field(ge=0, le=1_000_000_000)
    has_emi: bool
    emi_details: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=1000)

    _clean_email = field_validator("email")(valid_email)


@router.post("/screening/details")
def screening_details(body: ScreeningDetailsIn, request: Request):
    """Step 1 of the tenant screening intake (src/static/tenant_screening.html):
    the structured answers. Saved into the account's existing portfolio
    (src/services/portfolio.py) rather than a new table, so they show up in
    "Your portfolio" on the dashboard for free, same as chat-extracted fields."""

    account = current_account(request)
    if not account:
        raise HTTPException(401, "Not logged in.")
    if account["role"] != "tenant":
        raise HTTPException(403, "Tenant screening only applies to tenant accounts.")

    from src.services import portfolio  # local import: portfolio.py doesn't import accounts.py
    from src.services.viewings import tidy_phone   # +1 form, same as WhatsApp numbers
    portfolio.merge_fields(account["id"], {
        "phone": tidy_phone(body.phone),
        "employment_status": body.employment_status,
        "monthly_income": body.monthly_income,
        "annual_income": body.annual_income,
        "has_emi": body.has_emi,
        "emi_details": body.emi_details.strip() if body.has_emi else "",
        "screening_notes": body.notes.strip(),
    })
    # The name/email on the account itself stay as registered; the form only
    # confirms/collects them for the portfolio snapshot, so they aren't
    # written back onto accounts here.
    return {"saved": True}


@router.post("/screening/document")
async def screening_document(request: Request):
    """Step 2: the proof-of-income upload. Same private-storage pattern as
    onboarding_cases.store_upload() (hashed, private Supabase Storage bucket,
    staff-reviewable) - see src/services/tenant_screening.py for the staff
    side (review/download)."""

    account = current_account(request)
    if not account:
        raise HTTPException(401, "Not logged in.")
    if account["role"] != "tenant":
        raise HTTPException(403, "Tenant screening only applies to tenant accounts.")
    configured()

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type not in FILE_TYPES:
        raise HTTPException(415, "Upload a PDF, JPEG or PNG file.")
    data = await request.body()
    if not data or len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Files must be between 1 byte and 10 MB.")
    if not data.startswith(FILE_TYPES[content_type]):
        raise HTTPException(415, "The file content does not match its type.")

    from src.services import file_store  # local import: file_store.py doesn't import accounts.py
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", (request.headers.get("x-file-name") or "proof_of_income").strip())[:120] or "proof_of_income"
    digest = file_store.sha256(data)
    path = f"accounts/{account['id']}/documents/proof_of_income/{secrets.token_hex(8)}-{digest[:12]}"
    try:
        file_store.put(path, data, content_type)
    except file_store.StorageError as error:
        raise HTTPException(502, str(error))

    row = q(db._post, "account_documents", {
        "account_id": account["id"], "doc_key": "proof_of_income", "file_name": name, "content_type": content_type,
        "size_bytes": len(data), "sha256": digest, "storage_path": path, "status": "awaiting_review",
    })
    from src.services import portfolio
    portfolio.merge_fields(account["id"], {
        "proof_of_income": f"Uploaded ({name})",
        "proof_of_income_document_id": row["id"],
        "proof_of_income_content_type": content_type,
    })
    return {"document": {k: row[k] for k in ("id", "status", "file_name")}}


@router.get("/screening/document/{document_id}/file")
def screening_document_file(document_id: str, request: Request):
    """Lets a tenant view their own uploaded proof-of-income (dashboard.html's
    portfolio panel embeds this as an <img>/link) - same private storage as
    the staff download in tenant_screening.py, but scoped to the caller's
    own account instead of team auth."""

    account = current_account(request)
    if not account:
        raise HTTPException(401, "Not logged in.")

    rows = q(db._get, "account_documents", {"id": f"eq.{document_id}", "account_id": f"eq.{account['id']}", "select": "*"})
    if not rows:
        raise HTTPException(404, "Document not found.")
    doc = rows[0]

    from src.services import file_store
    data = file_store.get(doc["storage_path"])
    return Response(data, media_type=doc["content_type"] or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{doc["file_name"] or "proof_of_income"}"'})


@router.post("/screening/complete")
def screening_complete(request: Request):
    """Step 3: marks the intake done so it isn't shown again. Requires the
    details and at least one proof-of-income upload to already be saved -
    the intake page calls this last, after both prior calls succeed."""

    account = current_account(request)
    if not account:
        raise HTTPException(401, "Not logged in.")
    if account["role"] != "tenant":
        raise HTTPException(403, "Tenant screening only applies to tenant accounts.")

    from src.services import portfolio
    details = portfolio.get_portfolio(account["id"]).get("details") or {}
    if not details.get("monthly_income") and details.get("monthly_income") != 0:
        raise HTTPException(400, "Save your details before finishing.")
    if not details.get("proof_of_income"):
        raise HTTPException(400, "Upload proof of income before finishing.")

    if not account.get("screening_seen"):
        q(db._patch, "accounts", {"screening_seen": True}, {"id": f"eq.{account['id']}"})
        account = {**account, "screening_seen": True}
        try:  # journey tracker milestone (supabase_tenant_journeys.sql) - best-effort
            db._patch("accounts", {"screening_completed_at": now_iso()}, {"id": f"eq.{account['id']}"})
        except Exception as e:
            print(f"Recording screening time failed (non-fatal): {e}")
    return {"account": account_view(account), "redirect_url": post_auth_redirect(account)}
