
import json
import os
from pathlib import Path
from typing import Optional

import requests

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from src.services import accounts, chat, db, hubspot, inquiries, maintenance, properties, requirements, team_auth, viewings, whatsapp


STATIC_DIR = Path(__file__).parent / "static"


app = FastAPI(
    title="Real Estate AI",
    version="2.0.0"
)

# Everything except the WhatsApp webhook and /health needs the team password.
app.add_middleware(team_auth.TeamAuthMiddleware)


# Which SQL file creates each table - so a missing table (a setup file not
# run yet in Supabase) says exactly what to run, instead of a bare 500.
SETUP_FILES = {
    "rental_applications": "supabase_rental_applications.sql",
    "rental_threads": "supabase_rental_chat.sql",
    "rental_messages": "supabase_rental_chat.sql",
    "properties": "supabase_properties.sql",
    "maintenance_tickets": "supabase_maintenance_tickets.sql",
    "high_priority_inquiry_email_notifications": "supabase_high_priority_inquiry_email.sql",
    "accounts": "supabase_accounts.sql",
    "last_login_at": "supabase_tenant_journeys.sql",
    "account_sessions": "supabase_accounts.sql",
    "account_notifications": "supabase_lease_notifications.sql",
    "owner_notifications": "supabase_owner_notifications.sql",
    "user_requirements": "supabase_requirements.sql",
    "requirement_versions": "supabase_requirements.sql",
}


@app.exception_handler(requests.exceptions.HTTPError)
async def supabase_error(request: Request, exc: requests.exceptions.HTTPError):
    text = (exc.response.text if exc.response is not None else "") or str(exc)
    missing = "PGRST205" in text or "42P01" in text or "Could not find the table" in text or "does not exist" in text
    if missing:
        table = next((t for t in SETUP_FILES if t in text), None)
        fix = f"Run {SETUP_FILES[table]} in the Supabase SQL Editor" if table else "Run the latest supabase_*.sql setup files in the Supabase SQL Editor"
        print(f"Missing Supabase table for {request.url.path}: {text[:300]}")
        return JSONResponse(status_code=503, content={"detail": f"This feature isn't set up in the database yet. {fix}, then refresh."})
    print(f"Supabase error for {request.url.path}: {text[:300]}")
    return JSONResponse(status_code=502, content={"detail": f"Database error: {text[:200]}"})


class ConversationMessage(BaseModel):
    message: str

    conversation_history: list[dict] = Field(
        default_factory=list
    )

    property_context: Optional[dict] = None

    # Persistence fields (all optional so /analyze still works with no DB configured)
    session_id: Optional[str] = None
    listing_id: Optional[str] = None
    listing_title: Optional[str] = None
    persona: Optional[str] = "tenant"


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/login")


@app.get("/ui", include_in_schema=False)
def ui(request: Request):

    # Tenant accounts that haven't finished the one-time screening intake
    # are sent there instead - covers not just the post-login redirect but
    # anyone revisiting /ui directly on an existing session.
    account = accounts.current_account(request)
    if account and account.get("role") == "tenant" and not account.get("screening_seen"):
        return RedirectResponse("/tenant-screening")

    # A New Property Investor who just got a property (now existing_investor)
    # sees the one-time welcome page before their new dashboard.
    if accounts.existing_welcome_pending(account):
        return RedirectResponse(accounts.EXISTING_WELCOME_REDIRECT)

    # no-store: always send the latest index.html, so a fix to the role
    # checks can't be hidden behind an old copy in the browser cache.
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/admin/login", include_in_schema=False)
def admin_login_page(request: Request):
    # Hidden staff admin login - deliberately not linked from any page.
    # Already logged in as admin? Straight to the console.
    if accounts.is_admin(accounts.current_account(request)):
        return RedirectResponse("/ui")
    return FileResponse(
        STATIC_DIR / "admin_login.html",
        headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"},
    )


# Public script for the login/register 3D property viewer: one file with
# Three.js (r149, MIT) bundled in, plus the viewer and its styles.
@app.get("/assets/property-scene.js", include_in_schema=False)
def property_scene_script():
    return FileResponse(STATIC_DIR / "property-scene.js", media_type="text/javascript",
                        headers={"Cache-Control": "public, max-age=3600"})


# Per-tab login script (keeps each tab on the account it logged in as - see
# the file's header comment). Loaded by every account page, including the
# login page itself, so it's public.
@app.get("/assets/tab-session.js", include_in_schema=False)
def tab_session_script():
    return FileResponse(STATIC_DIR / "tab-session.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/register", include_in_schema=False)
def register_page():
    return FileResponse(STATIC_DIR / "register.html")


# Forgot password / email password reset (src/services/password_reset.py). These
# belong to the normal customer account system - NOT the staff Team login - so
# both pages are public (listed in team_auth.PUBLIC_PATHS) and neither one reads
# or creates any session. The reset page carries a secret token in its URL, so
# it must never be cached, indexed or leaked through a Referer header.
RESET_PAGE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                      "X-Robots-Tag": "noindex, nofollow"}


@app.get("/forgot-password", include_in_schema=False)
def forgot_password_page():
    return FileResponse(STATIC_DIR / "forgot_password.html", headers=RESET_PAGE_HEADERS)


@app.get("/reset-password", include_in_schema=False)
def reset_password_page():
    return FileResponse(STATIC_DIR / "reset_password.html", headers=RESET_PAGE_HEADERS)


@app.get("/portal", include_in_schema=False)
def portal_page():
    return FileResponse(STATIC_DIR / "portal.html")


@app.get("/requirements", include_in_schema=False)
def requirements_page(request: Request):
    account = accounts.current_account(request)
    if not account:
        return RedirectResponse("/login")
    if account.get("role") == "admin":
        return RedirectResponse("/ui")
    return FileResponse(STATIC_DIR / "requirements.html", headers={"Cache-Control": "no-store"})


@app.get("/team/client-requirements", include_in_schema=False)
def client_requirements_page():
    return FileResponse(STATIC_DIR / "client_requirements.html", headers={"Cache-Control": "no-store"})


@app.get("/dashboard.html", include_in_schema=False)
def dashboard_page(request: Request):

    # Where register/login send people (accounts.POST_AUTH_REDIRECT). Gated
    # by the account session cookie, like /portal - no session, back to login.
    account = accounts.current_account(request)
    if not account:
        return RedirectResponse("/login")

    # Legacy leads-only page: customer accounts (tenant / investor) only get
    # the tabs of their own dashboard at /ui, so send them there.
    if not accounts.is_admin(account):
        return RedirectResponse("/ui")

    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/investor-education", include_in_schema=False)
def investor_education_page(request: Request):

    # New Property Investor accounts land here first: a one-time "real estate
    # basics" primer before the dashboard. Account session required.
    account = accounts.current_account(request)
    if not account:
        return RedirectResponse("/login")

    # Only for New Property Investors - anyone else goes to their own dashboard.
    if account.get("role") != "new_investor" and not accounts.is_admin(account):
        return RedirectResponse("/ui")

    return FileResponse(STATIC_DIR / "investor_education.html")


@app.get("/existing-investor-welcome", include_in_schema=False)
def existing_investor_welcome_page(request: Request):

    # Shown once, right after a New Property Investor got a property and was
    # moved to Existing Property Investor. Account session required.
    account = accounts.current_account(request)
    if not account:
        return RedirectResponse("/login")
    if not accounts.existing_welcome_pending(account):
        return RedirectResponse("/ui")
    return FileResponse(STATIC_DIR / "existing_investor_welcome.html", headers={"Cache-Control": "no-store"})


@app.get("/tenant-screening", include_in_schema=False)
def tenant_screening_page(request: Request):

    # Tenant accounts land here first: a one-time screening intake before
    # the dashboard. Account session required.
    account = accounts.current_account(request)
    if not account:
        return RedirectResponse("/login")

    # Only for tenants - anyone else goes to their own dashboard.
    if account.get("role") != "tenant" and not accounts.is_admin(account):
        return RedirectResponse("/ui")

    return FileResponse(STATIC_DIR / "tenant_screening.html")


# Purchase offers (Item 3) and inspection assistance (Item 4) - the staff
# workspaces. Staff-only: team_auth.py refuses them to customer accounts;
# investors get their own scoped view of the same records through the
# "Purchase offers" / "Inspections" tabs in /ui (/me/investor/... routes in
# src/services/investor_acquisition.py). The public, token-gated approval
# page is /purchase-approval/<token> (purchase_offers.public_router).
@app.get("/team/purchase-offers", include_in_schema=False)
def purchase_offers_page():
    return FileResponse(STATIC_DIR / "purchase_offers.html")


@app.get("/inspections", include_in_schema=False)
def inspections_page():
    return FileResponse(STATIC_DIR / "inspections.html")


@app.get("/team/inspections", include_in_schema=False)
def team_inspections_page():
    return FileResponse(STATIC_DIR / "inspections.html")


@app.get("/dashboard", include_in_schema=False)
def dashboard(request: Request):

    # The leads dashboard is now the "Leads" tab of the main page - a staff
    # tab, so customer accounts just go to their own dashboard.
    account = accounts.current_account(request)
    if account and not accounts.is_admin(account):
        return RedirectResponse("/ui")
    return RedirectResponse("/ui#leads")


@app.get("/auth/requirements")
def get_my_requirements(http_request: Request, profile_type: Optional[str] = None):
    account = requirements.require_account(http_request)
    profile_type = profile_type or account["role"]
    if profile_type == "owner" and account["role"] not in ("existing_investor", "new_investor"):
        raise HTTPException(403, "Owner property requirements are available to investor/owner accounts.")
    if profile_type not in requirements.SUPPORTED_PROFILES:
        raise HTTPException(400, "Unsupported requirement profile.")
    row = requirements.get_active(account["id"], profile_type)
    return {"account": accounts.account_view(account), "profile": requirements.public_row(row) if row else None, "profile_type": profile_type}


@app.post("/auth/requirements/confirm-update")
def confirm_requirement_update(payload: dict, http_request: Request):
    account = requirements.require_account(http_request)
    profile_type = str(payload.get("profile_type") or account["role"]).strip()
    if profile_type == "owner" and account["role"] not in ("existing_investor", "new_investor"):
        raise HTTPException(403, "Owner property requirements are available to investor/owner accounts.")
    if profile_type not in requirements.SUPPORTED_PROFILES or profile_type != account["role"] and profile_type != "owner":
        raise HTTPException(400, "Unsupported requirement profile.")
    row = requirements.get_active(account["id"], profile_type)
    if not row:
        raise HTTPException(404, "Saved requirement profile not found.")
    patch = payload.get("patch") or {}
    if not isinstance(patch, dict):
        raise HTTPException(400, "Invalid requirement update.")
    # Only fields understood by the structured model may be changed.
    try:
        updated = requirements.apply_update(account["id"], row, patch, created_by="user")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    matches = requirements.match_properties(account["role"], updated["requirements"]) if profile_type != "owner" else {"matches": [], "total": 0, "role": "owner"}
    return {"profile": updated, "matches": matches}


@app.post("/auth/requirements")
def save_my_requirements(payload: dict, http_request: Request):
    account = requirements.require_account(http_request)
    profile_type = str(payload.pop("profile_type", "") or account["role"]).strip()
    if profile_type == "owner" and account["role"] not in ("existing_investor", "new_investor"):
        raise HTTPException(403, "Owner property requirements are available to investor/owner accounts.")
    if profile_type not in requirements.SUPPORTED_PROFILES or profile_type != account["role"] and profile_type != "owner":
        raise HTTPException(400, "Unsupported requirement profile.")
    try:
        saved = requirements.save(account["id"], "owner" if profile_type == "owner" else account["role"], payload, created_by="user", profile_type=profile_type)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    matches = requirements.match_properties(account["role"], saved["requirements"]) if profile_type != "owner" else {"matches": [], "total": 0, "role": "owner"}
    return {"profile": saved, "matches": matches, "redirect_url": "/ui"}


@app.delete("/auth/requirements")
def deactivate_my_requirements(http_request: Request, profile_type: Optional[str] = None):
    account = requirements.require_account(http_request)
    profile_type = profile_type or account["role"]
    if profile_type == "owner" and account["role"] not in ("existing_investor", "new_investor"):
        raise HTTPException(403, "Owner property requirements are available to investor/owner accounts.")
    requirements.deactivate(account["id"], profile_type)
    return {"status": "ok"}


@app.get("/auth/requirements/matches")
def my_requirement_matches(http_request: Request, profile_type: Optional[str] = None):
    account = requirements.require_account(http_request)
    profile_type = profile_type or account["role"]
    row = requirements.get_active(account["id"], profile_type)
    if not row:
        return {"profile": None, "matches": {"matches": [], "total": 0}}
    if profile_type == "owner":
        return {"profile": requirements.public_row(row), "matches": {"matches": [], "total": 0, "role": "owner"}}
    return {"profile": requirements.public_row(row), "matches": requirements.match_properties(account["role"], row["requirements_json"])}


@app.get("/staff/requirements/clients")
def staff_requirement_clients():
    if not db.ENABLED:
        raise HTTPException(503, "Supabase isn't configured.")
    try:
        rows = db._get("accounts", {"role": "in.(tenant,new_investor,existing_investor)", "select": "id,name,email,role", "order": "name.asc", "limit": "500"}) or []
        return {"clients": rows}
    except Exception as exc:
        raise HTTPException(500, "Could not load clients.") from exc


@app.get("/staff/requirements")
def staff_requirements():
    if not db.ENABLED:
        raise HTTPException(503, "Supabase isn't configured.")
    try:
        return {"profiles": requirements.staff_rows()}
    except Exception as exc:
        raise HTTPException(500, "Could not load client requirements.") from exc


@app.get("/staff/requirements/{user_id}")
def staff_requirement_detail(user_id: str):
    if not db.ENABLED:
        raise HTTPException(503, "Supabase isn't configured.")
    try:
        rows = db._get("user_requirements", {"user_id": f"eq.{user_id}", "is_active": "eq.true", "order": "updated_at.desc", "select": "*"}) or []
        return {"profiles": [requirements.public_row(r) for r in rows]}
    except Exception as exc:
        raise HTTPException(500, "Could not load client requirements.") from exc


@app.put("/staff/requirements/{user_id}")
def staff_save_requirement(user_id: str, payload: dict):
    if not db.ENABLED:
        raise HTTPException(503, "Supabase isn't configured.")
    role = str(payload.get("role") or "").strip()
    profile_type = str(payload.get("profile_type") or role).strip()
    if role not in requirements.SUPPORTED_PROFILES or profile_type not in requirements.SUPPORTED_PROFILES:
        raise HTTPException(400, "Unsupported requirement profile.")
    # Verify the client exists before staff writes on their behalf.
    found = db._get("accounts", {"id": f"eq.{user_id}", "select": "id,role", "limit": "1"}) or []
    if not found:
        raise HTTPException(404, "Client account not found.")
    body = dict(payload.get("requirements") or payload)
    body.pop("role", None); body.pop("profile_type", None)
    try:
        return requirements.save(user_id, role, body, created_by="staff", profile_type=profile_type)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/health")
def health():

    return {
        "status": "healthy",
        "database": "connected" if db.ENABLED else "not configured",
        "hubspot": hubspot.status(),
    }


@app.post("/analyze")
def analyze(request: ConversationMessage, http_request: Request):

    if not request.message.strip():

        raise HTTPException(
            status_code=400,
            detail="Message cannot be empty."
        )

    # A logged-in account (src/services/accounts.py) owns a fixed session_id and
    # a chosen workflow - both override whatever the client sent, so the
    # conversation always lands on that account whichever device sent it.
    account = accounts.current_account(http_request)
    saved_requirements = requirements.get_active(account["id"], account["role"]) if account else None

    try:

        return chat.process_message(
            message=request.message,
            conversation_history=request.conversation_history,
            property_context=request.property_context,
            session_id=account["session_id"] if account else request.session_id,
            listing_id=request.listing_id,
            listing_title=request.listing_title,
            persona=request.persona,
            customer_name=account["name"] if account else None,
            account_role=account.get("role") if account else None,
            account_id=account["id"] if account else None,
            saved_requirements=saved_requirements,
        )

    except chat.AIUnavailable as e:

        # The real cause (model name, HTTP status, parse error, ...) is
        # already printed server-side by real_estate_ai.py as each model
        # in the fallback chain is tried - never put that in front of
        # whoever's chatting, staff testing on /ui included.
        print(f"AI error on /analyze: {e}")

        raise HTTPException(
            status_code=503,
            detail=whatsapp.FALLBACK_REPLY
        )


@app.get("/properties")
def list_properties():
    """Listings shown on the chat page. `source` says whether they came
    from the Supabase properties table or the built-in sample file."""

    rows, source = properties.all_properties()

    # Owner contact details and internal ids never go to the public page.
    private = {"owner_name", "owner_phone", "session_id", "conversation_id"}

    return {
        "source": source,
        "properties": [
            {
                **{key: value for key, value in p.items() if key not in private},
                "price_label": properties.price_label(p),
                "context": properties.property_context(p),
            }
            for p in rows
        ]
    }


@app.get("/conversations/resume")
def resume_conversation(http_request: Request, session_id: str, listing_id: Optional[str] = None,
                        latest: bool = False):
    """Load the saved history for this browser + listing, so the chat can
    pick up where it left off after a refresh. latest=true (the page's first
    load) ignores listing_id and returns whichever thread was active most
    recently, so a returning user lands back where they left off."""

    if not db.ENABLED:
        return {"conversation": None, "history": [], "results": []}

    # A logged-in account's messages are saved under its own fixed
    # session_id (see /analyze), not the browser's - look there too, or
    # every login shows an empty chat even though the history is saved.
    account = accounts.current_account(http_request)
    if account:
        session_id = account["session_id"]

    try:

        conversation = (db.latest_conversation(session_id) if latest
                        else db.get_conversation(session_id, listing_id))

        if not conversation:
            return {"conversation": None, "history": [], "results": []}

        messages = db.list_messages(conversation["id"])

        history = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
        ]

        results = [
            m["analysis"] for m in messages
            if m["role"] == "assistant" and m.get("analysis")
        ]

        return {
            "conversation": conversation,
            "history": history,
            "results": results
        }

    except Exception as e:

        raise HTTPException(status_code=500, detail=f"Could not load conversation: {e}")


@app.delete("/conversations/resume")
def reset_conversation(http_request: Request, session_id: str, listing_id: Optional[str] = None):
    """Delete the saved thread for this browser + listing (the "Reset chat"
    button) so the next message starts a brand new conversation row."""

    if not db.ENABLED:
        return {"status": "ok"}

    account = accounts.current_account(http_request)     # same id /analyze saved it under
    if account:
        session_id = account["session_id"]

    try:

        # All of this session's threads, not just the current listing's:
        # the page reloads the most recent thread of ANY listing, so
        # clearing only one brought an older chat back after a refresh.
        db.archive_conversations(session_id)
        return {"status": "ok"}

    except Exception as e:

        raise HTTPException(status_code=500, detail=f"Could not reset conversation: {e}")


@app.get("/leads")
def leads(limit: int = 200):
    """All saved conversations, most-recently-active first - the data
    behind the leads dashboard."""

    if not db.ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Supabase isn't configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY to .env."
        )

    try:
        return db.list_leads(limit=limit)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load leads: {e}")


@app.get("/leads/{conversation_id}")
def lead_detail(conversation_id: str):
    """A single lead's full message thread, for the dashboard's detail view."""

    if not db.ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Supabase isn't configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY to .env."
        )

    try:

        conversation = db.get_lead(conversation_id)

        if not conversation:
            raise HTTPException(status_code=404, detail="Lead not found.")

        messages = db.list_messages(conversation_id)

        return {"conversation": conversation, "messages": messages}

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load lead: {e}")


class ViewingUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None


VIEWING_STATUSES = ["requested", "confirmed", "cancelled", "completed"]


@app.get("/viewings")
def list_viewings(status: Optional[str] = None):
    """Every viewing request, soonest first - the data behind the Viewings tab."""

    if not db.ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Supabase isn't configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY to .env."
        )

    if status and status not in VIEWING_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {VIEWING_STATUSES}")

    try:
        return db.list_viewings(status=status)

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load viewings (has supabase_viewings.sql been run?): {e}"
        )


@app.get("/me/viewings")
def my_home_viewings(http_request: Request):
    """An investor's Viewings tab: the visits tenants / buyers asked for (in
    the chat) at the homes this investor listed, soonest first. The team
    still confirms each one from the admin Viewings tab; the owner sees the
    status change here. The visitor's phone is only shown once the team has
    confirmed the visit."""

    account = accounts.current_account(http_request)
    if not account or account.get("role") not in ("tenant", "new_investor", "existing_investor"):
        raise HTTPException(status_code=403, detail="Only tenant and investor accounts have a Viewings tab.")
    if not db.ENABLED:
        return []

    if account.get("role") == "tenant":
        # A tenant: the visits THEY asked for (saved under their account's
        # session - chat.py / viewings.py), with the status the team set.
        try:
            rows = db._get("viewings", {
                "session_id": f"eq.{account['session_id']}",
                "order": "viewing_date.asc,viewing_time.asc", "limit": "100", "select": "*",
            }) or []
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not load your visits: {e}")
        return [{k: v.get(k) for k in ("id", "property_id", "property_title", "viewing_date", "viewing_time",
                                       "status", "created_at", "updated_at")} for v in rows]

    try:
        homes = db._get("properties", {
            "session_id": f"eq.{account['session_id']}",
            "source": f"in.({','.join(db.OWNER_LISTING_SOURCES)})",
            "select": "id,title",
        }) or []
        if not homes:
            return []
        titles = {h["id"]: h.get("title") for h in homes}
        rows = db._get("viewings", {
            "property_id": f"in.({','.join(titles)})",
            "order": "viewing_date.asc,viewing_time.asc", "limit": "300", "select": "*",
        }) or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load viewings (has supabase_viewings.sql been run?): {e}")

    confirmed = ("confirmed", "completed")
    return [{
        "id": v.get("id"),
        "property_id": v.get("property_id"),
        "property_title": v.get("property_title") or titles.get(v.get("property_id")),
        "viewing_date": v.get("viewing_date"),
        "viewing_time": v.get("viewing_time"),
        "status": v.get("status"),
        "customer_name": v.get("customer_name"),
        "customer_phone": v.get("customer_phone") if v.get("status") in confirmed else None,
        "created_at": v.get("created_at"),
        "updated_at": v.get("updated_at"),
    } for v in rows]


@app.patch("/viewings/{viewing_id}")
def update_viewing(viewing_id: str, update: ViewingUpdate):
    """Confirm, cancel or complete a viewing, or add a note."""

    if not db.ENABLED:
        raise HTTPException(status_code=503, detail="Supabase isn't configured.")

    fields = {}

    if update.status is not None:
        if update.status not in VIEWING_STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of {VIEWING_STATUSES}")
        fields["status"] = update.status

    if update.notes is not None:
        fields["notes"] = update.notes.strip() or None

    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update.")

    try:
        before = (db._get("viewings", {"id": f"eq.{viewing_id}", "select": "status", "limit": "1"}) or [{}])[0]
    except Exception:
        before = {}

    try:
        saved = db.update_viewing(viewing_id, fields)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not update viewing: {e}")

    if not saved:
        raise HTTPException(status_code=404, detail="Viewing not found.")

    # Confirmed / cancelled by the team: tell the tenant (their chat, and
    # WhatsApp when we have their number). The owner's Viewings tab reads the
    # same row, so it shows the new status on its next refresh.
    if fields.get("status") in ("confirmed", "cancelled") and before.get("status") != fields["status"]:
        viewings.notify_customer(saved)

    return saved


# ---------------------------------------------------------------------
# Property inquiries
# ---------------------------------------------------------------------

# Owner contact never goes back to whoever sent the enquiry - same rule
# as /properties (main.py:160). Staff read it from the Inquiries tab.
INQUIRY_PRIVATE_FIELDS = {"owner_name", "owner_phone"}

INQUIRY_STATUSES = ["new", "contacted", "closed"]


class InquiryCreate(BaseModel):
    property_id: Optional[str] = None
    property_title: str
    price_label: Optional[str] = None
    area: Optional[str] = None
    session_id: Optional[str] = None
    listing_id: Optional[str] = None
    conversation_id: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None


class InquiryUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None


@app.post("/inquiries")
def create_inquiry(inquiry: InquiryCreate, http_request: Request):
    """Save an "Enquire about this" enquiry confirmed from a listing card."""

    if not db.ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Supabase isn't configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY to .env."
        )

    # A logged-in account owns a fixed session_id, same override /analyze
    # does - without it, an account's enquiry was saved under whatever raw
    # session_id the browser happened to have in localStorage, while every
    # actual chat message from that same account was saved under the
    # account's own session_id instead. Two different session_ids for the
    # same person meant conversation_id resolution (inquiries.py:
    # create_inquiry(), matching by session_id) could never find the real
    # conversation - it was always looking under the wrong id.
    account = accounts.current_account(http_request)
    fields = inquiry.model_dump()
    if account:
        fields["session_id"] = account["session_id"]
        if not fields.get("customer_name"):
            fields["customer_name"] = account["name"]

    try:
        saved = inquiries.create_inquiry(**fields)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save inquiry: {e}")

    if not isinstance(saved, dict) or not saved.get("id"):
        raise HTTPException(status_code=500, detail="Database did not acknowledge the saved inquiry.")

    # The investor who listed this home hears about it too (email now; the
    # Enquiries list + badge on their Tenants tab reads the same row).
    inquiries.notify_owner(saved)
    # Tenant / new investor: pop up a notification for the home's owner.
    if account and fields.get("property_id"):
        from src.services import owner_notifications
        owner_notifications.notify_later(
            fields["property_id"], account, fields.get("notes") or "Enquired about this home.",
            "enquiry", fields.get("property_title"), fields.get("customer_phone"),
        )

    return {key: value for key, value in saved.items() if key not in INQUIRY_PRIVATE_FIELDS}


@app.get("/inquiries")
def list_inquiries(status: Optional[str] = None):
    """Every property enquiry, most recent first - the data behind the
    Inquiries tab, owner contact included for staff follow-up."""

    if not db.ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Supabase isn't configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY to .env."
        )

    if status and status not in INQUIRY_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {INQUIRY_STATUSES}")

    try:
        return inquiries.with_kind(db.list_inquiries(status=status))

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load inquiries (has supabase_property_inquiries.sql been run?): {e}"
        )


@app.get("/inquiries/{inquiry_id}/profile")
def inquiry_profile(inquiry_id: str):
    """Everything about the customer behind one enquiry, for the pop-up on
    the Inquiries tab: contact, score and why, AI summary, what they want,
    every home they asked about, viewings, applications / offers, investor
    progress and their chats. Staff only (team_auth.py)."""

    from src.services import inquiry_profile as profile

    if not db.ENABLED:
        raise HTTPException(status_code=503, detail="Supabase isn't configured.")

    try:
        rows = db._get("property_inquiries", {"id": f"eq.{inquiry_id}", "select": "*", "limit": "1"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load inquiry: {e}")

    if not rows:
        raise HTTPException(status_code=404, detail="Inquiry not found.")

    return profile.build(inquiries.with_kind(rows)[0])


@app.patch("/inquiries/{inquiry_id}")
def update_inquiry(inquiry_id: str, update: InquiryUpdate):
    """Mark an inquiry contacted/closed, or add a note - staff only."""

    if not db.ENABLED:
        raise HTTPException(status_code=503, detail="Supabase isn't configured.")

    fields = {}

    if update.status is not None:
        if update.status not in INQUIRY_STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of {INQUIRY_STATUSES}")
        fields["status"] = update.status

    if update.notes is not None:
        fields["notes"] = update.notes.strip() or None

    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update.")

    try:
        saved = db.update_inquiry(inquiry_id, fields)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not update inquiry: {e}")

    if not saved:
        raise HTTPException(status_code=404, detail="Inquiry not found.")

    return saved


# ---------------------------------------------------------------------
# Maintenance reports
# ---------------------------------------------------------------------

MAINTENANCE_MAX_IMAGE_SIZE = int(os.getenv("MAINTENANCE_MAX_IMAGE_MB", "10")) * 1024 * 1024

MAINTENANCE_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


async def _read_maintenance_image(image: UploadFile) -> bytes:
    if image.content_type not in MAINTENANCE_ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported photo type. Allowed: JPG, PNG, WEBP, GIF.")

    chunks, total_size = [], 0

    while True:
        chunk = await image.read(1024 * 1024)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > MAINTENANCE_MAX_IMAGE_SIZE:
            raise HTTPException(status_code=413, detail="Photo is too large.")
        chunks.append(chunk)

    image_bytes = b"".join(chunks)

    if not image_bytes:
        raise HTTPException(status_code=400, detail="Uploaded photo is empty.")

    return image_bytes


@app.post("/analyze/photo")
async def analyze_photo(
    http_request: Request,
    message: str = Form(""),
    conversation_history: str = Form("[]"),
    property_context: str = Form("{}"),
    session_id: Optional[str] = Form(None),
    listing_id: Optional[str] = Form(None),
    listing_title: Optional[str] = Form(None),
    persona: str = Form("tenant"),
    image: Optional[UploadFile] = File(None),
):
    """Same as POST /analyze, but for a message with a photo attached
    (e.g. a tenant reporting a maintenance issue in the chat). JSON
    can't carry a file, so the history/context travel as form fields
    instead of a JSON body. The AI itself decides whether this turn is
    a maintenance report (src/prompts/system_prompt.py); when it is,
    src/services/maintenance.py classifies the photo and saves a ticket."""

    message = (message or "").strip()

    try:
        history = json.loads(conversation_history) if conversation_history else []
        ctx = json.loads(property_context) if property_context else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="conversation_history and property_context must be valid JSON.")

    if not isinstance(history, list):
        raise HTTPException(status_code=400, detail="conversation_history must be a JSON array.")
    if not isinstance(ctx, dict):
        raise HTTPException(status_code=400, detail="property_context must be a JSON object.")

    image_bytes = image_content_type = image_filename = None

    if image is not None:
        image_bytes = await _read_maintenance_image(image)
        image_content_type = image.content_type
        image_filename = image.filename

    if not message and not image_bytes:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    account = accounts.current_account(http_request)
    saved_requirements = requirements.get_active(account["id"], account["role"]) if account else None

    # analyze_message() needs some text even when the photo is the
    # point, and the AI needs to see that a photo came with this turn
    # (see PHOTOS in src/prompts/system_prompt.py) whether the sender is
    # a tenant reporting an issue or an owner adding one to their
    # listing - this marker also stays in conversation_history, so on
    # later turns the AI can tell a photo was already attached.
    if image_bytes:
        ai_message = f"{message}\n[Photo attached]" if message else "[Photo attached, no caption text]"
    else:
        ai_message = message

    try:
        return await run_in_threadpool(
            chat.process_message,
            message=ai_message,
            conversation_history=history,
            property_context=ctx or None,
            session_id=account["session_id"] if account else session_id,
            listing_id=listing_id,
            listing_title=listing_title,
            persona=persona,
            customer_name=account["name"] if account else None,
            account_role=account.get("role") if account else None,
            account_id=account["id"] if account else None,
            image_bytes=image_bytes,
            image_content_type=image_content_type,
            image_filename=image_filename,
            saved_requirements=saved_requirements,
        )
    except chat.AIUnavailable as e:
        print(f"AI error on /analyze/photo: {e}")
        raise HTTPException(
            status_code=503,
            detail=whatsapp.FALLBACK_REPLY
        )


@app.get("/maintenance/tickets")
def maintenance_tickets(status: Optional[str] = None, limit: int = 200):
    """Every saved ticket, newest first - the data behind the Maintenance tab."""

    if not db.ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Supabase isn't configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY to .env.",
        )

    if status and status not in maintenance.TICKET_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {maintenance.TICKET_STATUSES}")

    try:
        return maintenance.list_tickets(status=status, limit=limit)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load tickets (has supabase_maintenance_tickets.sql been run?): {e}",
        )


class MaintenanceTicketUpdate(BaseModel):
    ticket_status: Optional[str] = None
    notes: Optional[str] = None


@app.patch("/maintenance/tickets/{ticket_id}")
def update_maintenance_ticket(ticket_id: str, update: MaintenanceTicketUpdate):
    """Move a ticket through the team's workflow, or add a note."""

    if not db.ENABLED:
        raise HTTPException(status_code=503, detail="Supabase isn't configured.")

    fields = {}

    if update.ticket_status is not None:
        if update.ticket_status not in maintenance.TICKET_STATUSES:
            raise HTTPException(status_code=400, detail=f"ticket_status must be one of {maintenance.TICKET_STATUSES}")
        fields["ticket_status"] = update.ticket_status

    if update.notes is not None:
        fields["notes"] = update.notes.strip() or None

    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update.")

    try:
        saved = maintenance.update_ticket(ticket_id, fields)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not update ticket: {e}")

    if not saved:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    return saved


class PropertyUpdate(BaseModel):
    status: Optional[str] = None


PROPERTY_STATUSES = ["pending", "active", "let", "sold", "hidden"]


@app.get("/listings/pending")
def pending_listings():
    """Listings owners added through the chat, waiting for review."""

    if not db.ENABLED:
        raise HTTPException(status_code=503, detail="Supabase isn't configured.")

    try:
        rows = db.list_properties_by_status("pending")

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load new listings (has supabase_owner_listings.sql been run?): {e}"
        )

    from src.services import listing_checks  # local import
    return [{**p, "price_label": properties.price_label(p), "checks": listing_checks.check(p)} for p in rows]


@app.patch("/properties/{property_id}")
def update_property(property_id: str, update: PropertyUpdate):
    """Approve (active), reject (hidden) or mark a listing let/sold."""

    if not db.ENABLED:
        raise HTTPException(status_code=503, detail="Supabase isn't configured.")

    if update.status not in PROPERTY_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {PROPERTY_STATUSES}")

    # Listing checks (src/services/listing_checks.py): don't publish a home with
    # no price / address / bedrooms, or a price that looks like a typo.
    if update.status == "active":
        from src.services import listing_checks  # local import
        current = (db._get("properties", {"id": f"eq.{property_id}", "select": "*", "limit": "1"}) or [None])[0]
        if current is None:
            raise HTTPException(status_code=404, detail="Listing not found.")
        problems = listing_checks.check(current)["problems"]
        if problems:
            raise HTTPException(status_code=409, detail="Fix this before it goes live: " + " ".join(problems))

    try:
        saved = db.update_property(property_id, {"status": update.status})

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not update listing: {e}")

    if not saved:
        raise HTTPException(status_code=404, detail="Listing not found.")

    # Show the change on the chat page straight away instead of after the cache expires.
    properties.clear_cache()

    return {**saved, "price_label": properties.price_label(saved)}


# ---------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------

@app.get("/webhook/whatsapp", include_in_schema=False)
def whatsapp_verify(request: Request):
    """Meta calls this once when you save the webhook URL in the app dashboard."""

    challenge = whatsapp.verify_subscription(
        request.query_params.get("hub.mode"),
        request.query_params.get("hub.verify_token"),
        request.query_params.get("hub.challenge"),
    )

    if challenge is None:
        raise HTTPException(status_code=403, detail="Verification failed.")

    return PlainTextResponse(challenge)


@app.post("/webhook/whatsapp", include_in_schema=False)
async def whatsapp_webhook(request: Request, background: BackgroundTasks):
    """Incoming WhatsApp messages from Meta. Replies are worked out after
    returning 200, because Meta retries webhooks that answer slowly."""

    raw = await request.body()

    if not whatsapp.signature_ok(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403, detail="Bad signature.")

    try:
        payload = await request.json()
    except Exception:
        return {"status": "ignored"}

    messages = whatsapp.parse_incoming(payload)

    for message in messages:
        background.add_task(whatsapp.handle_incoming, message)

    # Delivery/read/failed statuses for onboarding messages (idempotent).
    from src.services import onboarding_whatsapp
    statuses = onboarding_whatsapp.parse_statuses(payload)
    for status in statuses:
        background.add_task(apply_whatsapp_status, status)

    return {"status": "ok", "messages": len(messages), "statuses": len(statuses)}


def apply_whatsapp_status(status):
    from src.services import onboarding_whatsapp
    try:
        return onboarding_whatsapp.apply_status(status)
    except Exception as e:
        print(f"Applying WhatsApp status failed: {e}")


class SimulatedStatus(BaseModel):
    provider_message_id: str
    status: str
    recipient: str = ""


@app.post("/whatsapp/simulate-status")
def whatsapp_simulate_status(fake: SimulatedStatus):
    """Test mode only: pretend WhatsApp reported sent/delivered/read/failed for a
    simulated message. Results stay labelled as test mode."""

    from src.services import onboarding_whatsapp

    if not whatsapp.DRY_RUN:
        raise HTTPException(status_code=403, detail="Status simulation is only available in test mode.")
    if fake.status not in ("sent", "delivered", "read", "failed"):
        raise HTTPException(status_code=400, detail="status must be sent, delivered, read or failed")
    payload = onboarding_whatsapp.simulated_status_payload(fake.provider_message_id, fake.status, fake.recipient)
    return [onboarding_whatsapp.apply_status(s) for s in onboarding_whatsapp.parse_statuses(payload)]


class SimulatedWhatsApp(BaseModel):
    phone: str
    text: str
    name: Optional[str] = None
    message_id: Optional[str] = None


@app.get("/whatsapp/status")
def whatsapp_status():

    return whatsapp.status()


@app.get("/whatsapp/me")
def whatsapp_me(http_request: Request):
    """For the WhatsApp test tab: a logged-in tenant chats as themselves -
    their saved phone, and their messages land in their own Chat and
    Maintenance tabs (whatsapp.handle_incoming links them)."""

    account = accounts.current_account(http_request)
    if not account or account.get("role") != "tenant":
        return {"linked": False}
    from src.services import portfolio
    phone = ((portfolio.get_portfolio(account["id"]) or {}).get("details") or {}).get("phone")
    return {"linked": True, "name": account.get("name"), "phone": phone or None}


@app.post("/whatsapp/simulate")
def whatsapp_simulate(fake: SimulatedWhatsApp, http_request: Request):
    """Pretend to be Meta delivering a customer message (dry run only).
    Runs the exact webhook path: Meta-shaped payload -> parse -> reply.
    A logged-in tenant is always linked to their own account, so the
    message shows in their Chat and maintenance reports in Maintenance."""

    if not whatsapp.DRY_RUN:
        raise HTTPException(status_code=403, detail="Simulation is only available while WHATSAPP_DRY_RUN is on.")

    if not fake.text.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    payload = whatsapp.fake_webhook_payload(fake.phone, fake.text, fake.name, fake.message_id)

    account = accounts.current_account(http_request)
    tenant = account if account and account.get("role") == "tenant" else None

    outcomes = [whatsapp.handle_incoming(m, account=tenant) for m in whatsapp.parse_incoming(payload)]

    return outcomes[0] if outcomes else {"handled": False, "reason": "no message parsed"}


@app.get("/whatsapp/outbox")
def whatsapp_outbox(phone: Optional[str] = None):
    """Replies 'sent' in dry run, newest last."""

    digits = "".join(ch for ch in (phone or "") if ch.isdigit())

    return [m for m in whatsapp.OUTBOX if not digits or m["to"] == digits]


# ---------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------

@app.get("/metrics/summary")
def metrics_summary(hours: float = 24):
    """Reply speed, tokens, models and instant-reply share for the Stats tab."""

    from src.services import metrics
    from src.services.real_estate_ai import cooldown_status

    return {
        **metrics.summary(hours=max(0.1, min(hours, 24 * 30))),
        "models_cooling_down": cooldown_status(),
    }


# ---------------------------------------------------------------------
# Human verification of leads
# ---------------------------------------------------------------------

class LeadVerification(BaseModel):
    status: str


@app.patch("/leads/{conversation_id}/verification")
def verify_lead(conversation_id: str, update: LeadVerification):
    """Team marks a lead verified / not genuine / unverified. The stored
    9-part score is recombined straight away with the new verification."""

    from src.services import lead_scoring

    if update.status not in lead_scoring.HUMAN_VERIFICATION:
        raise HTTPException(status_code=400, detail=f"status must be one of {list(lead_scoring.HUMAN_VERIFICATION)}")

    if not db.ENABLED:
        raise HTTPException(status_code=503, detail="Supabase isn't configured.")

    lead = db.get_lead(conversation_id)

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")

    if "human_verification" not in lead:
        raise HTTPException(status_code=409, detail="Run supabase_lead_scoring.sql in Supabase first.")

    fields = {"human_verification": update.status}
    stored = lead.get("lead_components") or {}

    if stored.get("components"):
        components = stored["components"]
        components["human_verification"] = {
            **components.get("human_verification", {}),
            "score": lead_scoring.HUMAN_VERIFICATION[update.status],
            "reason": update.status.replace("_", " "),
        }
        rescored = lead_scoring.combine(components, stored.get("reasons") or [], update.status)
        fields.update(
            intent_score=rescored["lead_score"],
            lead_status=rescored["lead_status"],
            lead_components=rescored,
        )

    try:
        saved = db.update_conversation(conversation_id, fields)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not update lead: {e}")

    return saved


# ---------------------------------------------------------------------
# Lead outcomes and score accuracy
# ---------------------------------------------------------------------

class LeadOutcome(BaseModel):
    outcome: str
    note: Optional[str] = None


@app.patch("/leads/{conversation_id}/outcome")
def set_lead_outcome(conversation_id: str, update: LeadOutcome):
    """Record what really happened: rented, bought, listed, lost, no_response
    (or open to undo). The current score and breakdown are frozen with it."""

    from src.services import outcomes

    if update.outcome not in outcomes.OUTCOMES:
        raise HTTPException(status_code=400, detail=f"outcome must be one of {outcomes.OUTCOMES}")

    if not db.ENABLED:
        raise HTTPException(status_code=503, detail="Supabase isn't configured.")

    lead = db.get_lead(conversation_id)

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")

    if "outcome" not in lead:
        raise HTTPException(status_code=409, detail="Run supabase_outcomes.sql in Supabase first.")

    try:
        return db.update_conversation(conversation_id, outcomes.mark(lead, update.outcome, update.note))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save outcome: {e}")


@app.get("/reports/outcomes")
def outcomes_report():
    """Win rate per tier and which score parts predict a win."""

    from src.services import outcomes

    if not db.ENABLED:
        raise HTTPException(status_code=503, detail="Supabase isn't configured.")

    try:
        leads = db.list_leads(limit=1000)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load leads: {e}")

    if leads and "outcome" not in leads[0]:
        raise HTTPException(status_code=409, detail="Run supabase_outcomes.sql in Supabase first.")

    return outcomes.report(leads)


# Team-auth middleware protects all onboarding routes except /p/ (secure links).
# The cases router is included first so /onboarding/cases is not taken by /onboarding/{record_id}.
from src.services.onboarding_cases import router as onboarding_cases_router
from src.services.onboarding_party import router as onboarding_party_router
from src.services.onboarding import router as onboarding_router
app.include_router(onboarding_cases_router)
app.include_router(onboarding_party_router)
app.include_router(onboarding_router)

# Tenant screening (staff review side) - team-only, same as onboarding above.
# The tenant-facing intake itself is under /auth/screening/* (accounts.py,
# public + account-gated) and /tenant-screening (the page, just above).
from src.services.tenant_screening import router as tenant_screening_router
app.include_router(tenant_screening_router)

from src.services.deals import router as deals_router
from src.services.investors import router as investors_router
from src.services.mls_listings import router as mls_router
from src.services.owner_leads import public_router as owner_reply_router, router as owner_leads_router
app.include_router(owner_leads_router)
app.include_router(owner_reply_router)
app.include_router(mls_router)
app.include_router(deals_router)
app.include_router(investors_router)

# The guided investor journey, the investor's own self-service routes and the
# customer account system (register/login/portal/dashboard).
from src.services.investor_journey import customer_router as investor_self_service_router, router as journeys_router
app.include_router(journeys_router)
app.include_router(investor_self_service_router)

# Portfolio alerts (lease-ending, stale maintenance) and their daily WhatsApp
# digest - staff-only, same TeamAuthMiddleware as everything above.
from src.services.investor_alerts import router as investor_alerts_router
app.include_router(investor_alerts_router)

# Purchase offers + inspection assistance: staff routers, the public
# token-gated approval page, and the investor's own scoped view.
from src.services.purchase_offers import router as purchase_offers_router, public_router as purchase_approval_router
from src.services.inspections import router as inspections_router
from src.services.investor_acquisition import router as investor_acquisition_router
app.include_router(purchase_offers_router)
app.include_router(purchase_approval_router)
app.include_router(inspections_router)
app.include_router(investor_acquisition_router)

from src.services.accounts import router as accounts_router
app.include_router(accounts_router)

# POST /auth/forgot-password and /auth/reset-password (own router, same /auth prefix).
from src.services.password_reset import router as password_reset_router
app.include_router(password_reset_router)

# Customer reminders (lease expiry, open maintenance, purchase-offer expiry /
# closing) for tenants and investors - src/services/lease_notifications.py,
# shown on the "Notifications" tab in /ui. Served under
# /me/account-notifications so it never clashes with the owners'
# "someone asked about your house" pop-ups at /me/notifications
# (owner_notifications.py + static/notifications.js).
from src.services.lease_notifications import router as lease_notifications_router
app.include_router(lease_notifications_router)

# Investors' own listings for tenants ("My listings" tab in /ui). Account-
# gated in the module itself (investor accounts only).
from src.services.my_listings import router as my_listings_router
app.include_router(my_listings_router)

# Renting a home: tenant applies -> team approves -> owner approves ->
# maintenance opens for that tenant + home (src/services/rentals.py).
from src.services.rentals import owner_router as rentals_owner_router, team_router as rentals_team_router, tenant_router as rentals_tenant_router
app.include_router(rentals_tenant_router)
app.include_router(rentals_owner_router)
app.include_router(rentals_team_router)
# Buying a home another investor listed: the buyer's side (Purchase offers tab).
from src.services.rentals import buyer_router as rentals_buyer_router
app.include_router(rentals_buyer_router)
# A tenant buying the home they rent ("Own this house"): owner consent, the
# price, the team's checks, then "live there or rent it out?" (home_purchase.py).
from src.services import home_purchase
for home_purchase_router in (home_purchase.tenant_router, home_purchase.owner_router,
                             home_purchase.team_router, home_purchase.buyer_router):
    app.include_router(home_purchase_router)

# Congratulation pop-ups: tenant assigned a home, investor bought one, owner sold one
# (src/services/celebrations.py + static/celebrations.js).
from src.services.celebrations import router as celebrations_router
app.include_router(celebrations_router)
# "Someone asked about your house" pop-ups for owners (owner_notifications.py + static/notifications.js).
from src.services.owner_notifications import router as owner_notifications_router
app.include_router(owner_notifications_router)

# Direct chat between a tenant and the investor who listed a home ("Messages" tab).
from src.services.rental_chat import router as rental_chat_router
app.include_router(rental_chat_router)

# Tenant journey tracker for the admin dashboard ("Tenant journeys" tab). Staff-only.
from src.services.tenant_journeys import (
    router as tenant_journeys_router,
    customer_router as tenant_journeys_customer_router,
)
app.include_router(tenant_journeys_router)
# A logged-in tenant's own journey (GET /me/tenant-journey).
app.include_router(tenant_journeys_customer_router)

# One card per customer for the staff member: Inquiries -> "All chats" + the
# customer pop-up (src/services/customers.py). Staff-only (team_auth).
from src.services.customers import router as customers_router
app.include_router(customers_router)

# Public privacy policy (Meta needs it for WhatsApp): src/services/privacy_page.py
from src.services.privacy_page import router as privacy_router
app.include_router(privacy_router)


@app.get("/ui/investors.js", include_in_schema=False)
def investors_script():
    return FileResponse(STATIC_DIR / "investors.js", media_type="text/javascript")


@app.get("/ui/deals.js", include_in_schema=False)
def deals_script():
    return FileResponse(STATIC_DIR / "deals.js", media_type="text/javascript")


@app.get("/ui/owner_leads.js", include_in_schema=False)
def owner_leads_script():
    return FileResponse(STATIC_DIR / "owner_leads.js", media_type="text/javascript")


@app.get("/ui/onboarding.js", include_in_schema=False)
def onboarding_script():
    return FileResponse(STATIC_DIR / "onboarding.js", media_type="text/javascript")


@app.get("/ui/notifications.js", include_in_schema=False)
def notifications_script():
    return FileResponse(STATIC_DIR / "notifications.js", media_type="text/javascript")


@app.get("/ui/celebrations.js", include_in_schema=False)
def celebrations_script():
    return FileResponse(STATIC_DIR / "celebrations.js", media_type="text/javascript")


@app.get("/ui/journeys.js", include_in_schema=False)
def journeys_script():
    return FileResponse(STATIC_DIR / "journeys.js", media_type="text/javascript")
