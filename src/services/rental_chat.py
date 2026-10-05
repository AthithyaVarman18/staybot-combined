"""
Direct chat between a tenant and the investor who listed a home.

  Tenant   starts a thread from a home on the Homes tab ("Message owner"),
           or from one of their applications.
  Owner    starts one from an application on their Tenants tab ("Message tenant").
  Both     read and reply on the "Messages" tab in /ui.

One thread per (home, tenant) - tables in supabase_rental_chat.sql. Only the
two people in a thread can read it. Messages are polled (the Messages tab
refreshes every few seconds while open) - no websockets needed.

TENANT_OWNER_CHAT_NEEDS_APPROVAL=true: a tenant can only start a chat once
our team has approved their application for that home (same point at which
the owner starts seeing it - see src/services/rentals.py). Off by default, so
tenants can ask an owner questions before applying.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.services import accounts, db, owner_listings, rentals

router = APIRouter(prefix="/me/messages", tags=["Tenant-owner chat"])

NEEDS_APPROVAL = (os.getenv("TENANT_OWNER_CHAT_NEEDS_APPROVAL") or "").strip().lower() in ("1", "true", "yes")
OWNER_SOURCES = ("owner_chat", "investor_form")
MAX_MESSAGES = 500


def now_precise() -> str:
    """Microsecond timestamp for read markers. accounts.now_iso() rounds to
    the second, which would leave a message sent in the same second as the
    other person reads the thread counted as unread forever."""
    return datetime.now(timezone.utc).isoformat()


def me(request: Request) -> tuple[dict, str]:
    """(account, side) where side is 'tenant' or 'owner'."""
    account = accounts.current_account(request)
    if not account:
        raise HTTPException(401, "Please log in.")
    rentals.configured()
    if account.get("role") == "tenant":
        return account, "tenant"
    if account.get("role") in owner_listings.INVESTOR_ROLES:
        return account, "owner"
    raise HTTPException(403, "Messages are between tenants and home owners.")


def in_thread(thread: dict, account: dict, side: str) -> bool:
    if side == "tenant":
        return thread.get("tenant_account_id") == account["id"]
    return thread.get("owner_session_id") == account["session_id"]


def get_thread(thread_id: str, account: dict, side: str) -> dict:
    thread = rentals.one("rental_threads", {"id": f"eq.{thread_id}"})
    if not thread or not in_thread(thread, account, side):
        raise HTTPException(404, "Conversation not found.")
    return thread


def other(side: str) -> str:
    return "owner" if side == "tenant" else "tenant"


def unread_count(thread: dict, side: str) -> int:
    params = {"thread_id": f"eq.{thread['id']}", "sender": f"eq.{other(side)}", "select": "id", "limit": "100"}
    last_read = thread.get(f"{side}_last_read_at")
    if last_read:
        params["created_at"] = f"gt.{last_read}"
    return len(db._get("rental_messages", params) or [])


def thread_view(thread: dict, side: str, with_unread: bool = True) -> dict:
    out = {
        "id": thread["id"],
        "property_id": thread.get("property_id"),
        "property_title": thread.get("property_title"),
        # Each side sees the other person's name - never their session ids.
        "with_name": thread.get("owner_name") if side == "tenant" else thread.get("tenant_name"),
        "with_role": other(side),
        "last_message_at": thread.get("last_message_at"),
        "last_message_preview": thread.get("last_message_preview"),
        "last_sender": thread.get("last_sender"),
        "created_at": thread.get("created_at"),
    }
    if with_unread:
        out["unread"] = unread_count(thread, side)
    return out


def message_view(m: dict, side: str) -> dict:
    return {"id": m["id"], "body": m["body"], "created_at": m["created_at"],
            "sender": m["sender"], "mine": m["sender"] == side}


def open_or_create(prop: dict, tenant: dict, owner: dict) -> dict:
    existing = rentals.one("rental_threads", {"property_id": f"eq.{prop['id']}", "tenant_account_id": f"eq.{tenant['id']}"})
    if existing:
        return existing
    return db._post("rental_threads", {
        "property_id": prop["id"],
        "property_title": prop.get("title"),
        "tenant_account_id": tenant["id"],
        "tenant_name": tenant.get("name"),
        "owner_session_id": owner["session_id"],
        "owner_name": owner.get("name"),
    })


@router.get("/threads")
def threads(request: Request):
    account, side = me(request)
    key = {"tenant_account_id": f"eq.{account['id']}"} if side == "tenant" else {"owner_session_id": f"eq.{account['session_id']}"}
    rows = db._get("rental_threads", {**key, "order": "last_message_at.desc.nullslast,created_at.desc", "select": "*"}) or []
    items = [thread_view(t, side) for t in rows]
    return {"threads": items, "unread": sum(t["unread"] for t in items), "side": side}


@router.get("/unread")
def unread(request: Request):
    """Just the total, for the Messages tab badge (polled)."""
    account, side = me(request)
    key = {"tenant_account_id": f"eq.{account['id']}"} if side == "tenant" else {"owner_session_id": f"eq.{account['session_id']}"}
    rows = db._get("rental_threads", {**key, "select": "*"}) or []
    return {"unread": sum(unread_count(t, side) for t in rows)}


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_id: Optional[str] = Field(default=None, max_length=120)      # tenant: the home
    application_id: Optional[str] = Field(default=None, max_length=60)    # owner (or tenant): an application


@router.post("/threads")
def start(body: StartIn, request: Request):
    """Open (or re-open) the conversation about one home between this tenant
    and its owner."""

    account, side = me(request)

    if side == "owner":
        if not body.application_id:
            raise HTTPException(400, "Pick an application to message the tenant about.")
        app = rentals.own_app(account, body.application_id)   # 404 unless it's theirs and visible to them
        prop = rentals.one("properties", {"id": f"eq.{app['property_id']}"}) or {"id": app["property_id"], "title": app.get("property_title")}
        tenant = rentals.one("accounts", {"id": f"eq.{app['tenant_account_id']}"})
        if not tenant:
            raise HTTPException(404, "That tenant's account no longer exists.")
        return thread_view(open_or_create(prop, tenant, account), side)

    # Tenant
    app = None
    if body.application_id:
        app = rentals.get_application(body.application_id)
        if app["tenant_account_id"] != account["id"]:
            raise HTTPException(404, "Application not found.")
    property_id = body.property_id or (app or {}).get("property_id")
    if not property_id:
        raise HTTPException(400, "Pick a home to message the owner about.")

    prop = rentals.one("properties", {"id": f"eq.{property_id}"})
    if not prop:
        raise HTTPException(404, "This home isn't listed any more.")
    owner = rentals.owner_account_for(prop)
    if not owner:
        raise HTTPException(400, "This home is managed by our team - ask about it in Chat instead.")

    existing = rentals.one("rental_threads", {"property_id": f"eq.{prop['id']}", "tenant_account_id": f"eq.{account['id']}"})
    if existing:
        return thread_view(existing, side)

    # A new conversation needs the home to still be on the market - unless
    # the tenant already has an application for it (e.g. it's their home now).
    apps = db._get("rental_applications", {
        "tenant_account_id": f"eq.{account['id']}", "property_id": f"eq.{prop['id']}", "select": "status",
    }) or []
    statuses = {a["status"] for a in apps}
    if NEEDS_APPROVAL and not statuses & {"team_approved", "approved"}:
        raise HTTPException(403, "You can message the owner once our team has approved your application for this home.")
    if prop.get("status") != "active" and not statuses:
        raise HTTPException(404, "This home isn't available any more.")

    return thread_view(open_or_create(prop, account, owner), side)


@router.get("/threads/{thread_id}")
def read_thread(thread_id: str, request: Request, after: Optional[str] = None):
    """Messages in this conversation (all, or only those after `after` when
    polling), and marks everything up to now as read for this person."""

    account, side = me(request)
    thread = get_thread(thread_id, account, side)
    params = {"thread_id": f"eq.{thread_id}", "order": "created_at.asc", "limit": str(MAX_MESSAGES), "select": "*"}
    if after:
        params["created_at"] = f"gt.{after}"
    rows = db._get("rental_messages", params) or []

    # Mark read (only when there's something new from the other person).
    if any(m["sender"] != side for m in rows) or not thread.get(f"{side}_last_read_at"):
        try:
            db._patch("rental_threads", {f"{side}_last_read_at": now_precise()}, {"id": f"eq.{thread_id}"})
        except Exception as e:
            print(f"Marking chat read failed (non-fatal): {e}")

    return {"thread": thread_view(thread, side, with_unread=False), "messages": [message_view(m, side) for m in rows]}


class SendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=2000)


@router.post("/threads/{thread_id}")
def send(thread_id: str, body: SendIn, request: Request):
    account, side = me(request)
    thread = get_thread(thread_id, account, side)
    text = body.body.strip()
    if not text:
        raise HTTPException(400, "Write a message first.")

    msg = db._post("rental_messages", {
        "thread_id": thread_id, "sender": side, "sender_account_id": account["id"], "body": text,
    })
    now = msg.get("created_at") or now_precise()
    db._patch("rental_threads", {
        "last_message_at": now,
        "last_message_preview": text[:140],
        "last_sender": side,
        f"{side}_last_read_at": now,   # your own message is read by you
    }, {"id": f"eq.{thread_id}"})
    if side == "tenant":
        # Pop-up for the owner with this tenant's details (owner_notifications.py).
        from src.services import owner_notifications
        owner_notifications.notify_later(thread["property_id"], account, text, "message", thread.get("property_title"))
    return message_view(msg, side)
