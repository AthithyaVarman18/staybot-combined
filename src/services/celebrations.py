"""
Congratulation pop-ups (celebrations.js in /ui).

GET /me/celebrations returns the happy moments for the logged-in account:

  tenant_assigned  a tenant's rental application is approved - the home is theirs to rent
  home_bought      an investor's purchase request was agreed (buying another investor's
                   listing), or a tenant finished buying the home they rent
  home_sold        the owner's home was sold (to an investor buyer or to their own tenant)

Nothing is stored here: everything is read from rental_applications and
home_buy_requests. Each event has a stable id; the browser remembers which
ids it has already shown (localStorage, per account), so each pop-up appears
once. Only events from the last RECENT_DAYS are returned, so people who were
approved long ago don't get a surprise pop-up the first time this ships.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Request

from src.services import accounts, db

router = APIRouter(prefix="/me/celebrations", tags=["Celebrations"])

RECENT_DAYS = 14


def _when(row: dict, *keys: str) -> Optional[str]:
    for k in keys:
        if row.get(k):
            return row[k]
    return None


def _recent(ts: Optional[str]) -> bool:
    if not ts:
        return True  # no timestamp at all - better to celebrate than to miss it
    try:
        at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at >= datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)


def _is_sale(row: dict) -> bool:
    d = row.get("details") if isinstance(row.get("details"), dict) else {}
    return d.get("kind") == "purchase" or bool(d.get("sale_agreed"))


def _title(row: dict) -> str:
    return (row.get("property_title") or "").strip() or "your new home"


def _get(table: str, params: dict) -> list[dict]:
    try:
        return db._get(table, {**params, "select": "*"}) or []
    except Exception as e:  # table not created yet, Supabase down - no pop-ups, never an error
        print(f"Celebrations: reading {table} failed (non-fatal): {e}")
        return []


def _event(kind: str, key: str, row: dict, at: Optional[str]) -> dict:
    title = _title(row)
    text = {
        "tenant_assigned": ("Congratulations!", f"You've been assigned {title}. Welcome to your new home!"),
        "home_bought": ("Congratulations on your new home!", f"You've bought {title}. It's now part of your portfolio."),
        "home_sold": ("Congratulations!", f"Your house {title} has been sold."),
    }[kind]
    return {"id": f"{kind}:{key}", "kind": kind, "title": text[0], "message": text[1],
            "property_id": row.get("property_id"), "property_title": row.get("property_title"), "at": at}


def events_for(account: dict) -> list[dict]:
    out: dict[str, dict] = {}

    def add(ev: dict):
        out.setdefault(ev["id"], ev)

    # As the tenant / buyer on an application.
    for r in _get("rental_applications", {"tenant_account_id": f"eq.{account['id']}", "status": "eq.approved"}):
        at = _when(r, "owner_decided_at", "team_reviewed_at", "updated_at")
        if not _recent(at):
            continue
        if _is_sale(r):
            add(_event("home_bought", r["property_id"], r, at))
        else:
            add(_event("tenant_assigned", r["id"], r, at))

    # A tenant who finished buying the home they rent.
    for r in _get("home_buy_requests", {"tenant_account_id": f"eq.{account['id']}", "status": "eq.completed"}):
        at = _when(r, "completed_at", "updated_at")
        if _recent(at):
            add(_event("home_bought", r["property_id"], r, at))

    # As the owner who listed the home.
    if account.get("session_id"):
        owner = f"eq.{account['session_id']}"
        for r in _get("rental_applications", {"owner_session_id": owner, "status": "eq.approved"}):
            at = _when(r, "owner_decided_at", "updated_at")
            if _is_sale(r) and _recent(at):
                add(_event("home_sold", r["property_id"], r, at))
        for r in _get("home_buy_requests", {"owner_session_id": owner, "status": "eq.completed"}):
            at = _when(r, "completed_at", "updated_at")
            if _recent(at):
                add(_event("home_sold", r["property_id"], r, at))

    return sorted(out.values(), key=lambda e: e.get("at") or "")


@router.get("")
def my_celebrations(request: Request):
    """Congratulation pop-ups for this account (empty when logged out)."""
    account = accounts.current_account(request)
    if not account or not db.ENABLED:
        return {"events": []}
    return {"events": events_for(account), "account_key": account["id"]}
