"""
"Someone asked about your house" - pop-up notifications for owners.

When a TENANT or a NEW PROPERTY INVESTOR asks about a home an investor
account listed, that owner gets a notification with the asker's details
(name, account type, email, phone and what we know about them from their
Portfolio - budget, move-in, income, financing ...) and their question.
notifications.js in /ui pops it up for the owner and keeps a bell list.

Where a question comes from (source):
  chat      the asker's Chat (web, or WhatsApp for a linked tenant) mentions a
            specific home: the listing they have open, "the second one" from
            the list they were just shown, the home's title, or a viewing
            request for it (chat.py -> from_chat())
  enquiry   "Enquire about this" on a listing card (POST /inquiries)
  message   a tenant's message to the owner on the Messages tab (rental_chat.py)

Only homes listed by an investor account have an owner to notify (team and
MLS listings don't). Owners are never notified about their own homes.
Several questions from the same person about the same home within
MERGE_MINUTES are folded into one notification (latest question, count),
so a back-and-forth chat doesn't flood the owner.

Table: supabase_owner_notifications.sql. Everything here is best-effort and
never raises into the caller - a notification failing must never break the
chat reply, enquiry or message that triggered it.
"""

import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.services import accounts, db

router = APIRouter(prefix="/me/notifications", tags=["Owner notifications"])

ASKER_ROLES = ("tenant", "new_investor")
ROLE_LABELS = {"tenant": "Tenant", "new_investor": "New Property Investor", "existing_investor": "Existing Property Investor"}
MERGE_MINUTES = 30
RUN_IN_BACKGROUND = True  # tests set False to run synchronously

# Portfolio facts worth showing an owner, in this order (account_portfolios.details).
DETAIL_FIELDS = [
    ("employment_status", "Employment"), ("monthly_income", "Monthly income"), ("move_in_date", "Move-in"),
    ("lease_months", "Lease (months)"), ("adults", "Adults"), ("children", "Children"), ("has_pets", "Pets"),
    ("pet_details", "Pet details"), ("budget", "Budget"), ("max_budget", "Budget"), ("bedrooms", "Bedrooms"),
    ("location", "Looking in"), ("cash_available", "Cash available"), ("financing", "Financing"),
    ("goal", "Goal"), ("areas", "Areas"), ("timeline", "Timeline"), ("experience", "Experience"),
]
MAX_DETAILS = 8


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _one(table: str, params: dict) -> Optional[dict]:
    rows = db._get(table, {**params, "select": "*", "limit": "1"}) or []
    return rows[0] if rows else None


def _pretty(v) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x not in (None, ""))
    return str(v).replace("_", " ") if isinstance(v, str) and "_" in v and " " not in v else str(v)


def owner_of(property_id: str) -> tuple[Optional[dict], Optional[dict]]:
    """(property row, the investor account that listed it) - (prop, None) for
    team / anonymous-owner listings, (None, None) when it isn't ours (MLS)."""
    from src.services import rentals  # local import: rentals imports a lot
    prop = _one("properties", {"id": f"eq.{property_id}"})
    if not prop:
        return None, None
    return prop, rentals.owner_account_for(prop)


def asker_details(account: dict, phone: Optional[str] = None) -> dict:
    """What the owner sees about the person asking (snapshot at ask time)."""
    details = {}
    try:
        rows = db._get("account_portfolios", {"account_id": f"eq.{account['id']}", "select": "details", "limit": "1"}) or []
        details = (rows[0].get("details") if rows else None) or {}
    except Exception as e:
        print(f"Owner notification: portfolio lookup failed (non-fatal): {e}")
    facts, seen = [], set()
    for key, label in DETAIL_FIELDS:
        v = details.get(key)
        if v in (None, "", [], {}) or label in seen:
            continue
        seen.add(label)
        facts.append({"label": label, "value": _pretty(v)[:120]})
        if len(facts) >= MAX_DETAILS:
            break
    return {
        "name": account.get("name") or "Someone",
        "role": account.get("role"),
        "role_label": ROLE_LABELS.get(account.get("role"), account.get("role") or ""),
        "email": account.get("email"),
        "phone": phone or details.get("phone"),
        "member_since": str(account.get("created_at") or "")[:10] or None,
        "facts": facts,
    }


def notify(property_id: str, asker: dict, question: str, source: str,
           property_title: Optional[str] = None, phone: Optional[str] = None) -> Optional[dict]:
    """Tell the owner of property_id that `asker` asked about it. Returns the
    saved notification, or None when there's no one to tell. Never raises."""
    try:
        if not (db.ENABLED and property_id and asker and asker.get("role") in ASKER_ROLES):
            return None
        prop, owner = owner_of(property_id)
        if not owner or owner.get("id") == asker.get("id") or owner.get("session_id") == asker.get("session_id"):
            return None
        question = re.sub(r"\s+", " ", str(question or "")).strip()[:600]
        title = (prop or {}).get("title") or property_title or "your home"

        since = (datetime.now(timezone.utc) - timedelta(minutes=MERGE_MINUTES)).isoformat()
        recent = db._get("owner_notifications", {
            "owner_account_id": f"eq.{owner['id']}", "asker_account_id": f"eq.{asker['id']}",
            "property_id": f"eq.{property_id}", "updated_at": f"gte.{since}",
            "order": "updated_at.desc", "limit": "1", "select": "*",
        }) or []
        if recent:
            n = recent[0]
            return db._patch("owner_notifications", {
                "question": question or n.get("question"), "source": source,
                "ask_count": int(n.get("ask_count") or 1) + 1, "updated_at": now_iso(),
            }, {"id": f"eq.{n['id']}"})

        return db._post("owner_notifications", {
            "owner_account_id": owner["id"],
            "property_id": property_id,
            "property_title": title,
            "asker_account_id": asker["id"],
            "asker_role": asker.get("role"),
            "asker": asker_details(asker, phone),
            "question": question,
            "source": source,
        })
    except Exception as e:
        print(f"Owner notification failed (non-fatal - is supabase_owner_notifications.sql run?): {e}")
        return None


def notify_later(*args, **kwargs):
    """notify() without making the caller (a chat reply) wait for it."""
    if not RUN_IN_BACKGROUND:
        return notify(*args, **kwargs)
    threading.Thread(target=notify, args=args, kwargs=kwargs, daemon=True).start()
    return None


# ---------------------------------------------------------------------
# Which home a chat message is about
# ---------------------------------------------------------------------

ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
            "fifth": 5, "5th": 5, "sixth": 6, "6th": 6, "last": -1}
HOME_WORDS = r"(?:one|home|house|property|listing|place|option|apartment|flat|unit|condo|townhouse)"
ORDINAL_RE = re.compile(r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|sixth|6th|last)\s+" + HOME_WORDS + r"\b", re.I)
NUMBER_RE = re.compile(r"(?:#\s*|\b(?:number|no\.?|option|home|house|listing|property)\s*#?\s*)([1-9])\b", re.I)


def position_in(message: str) -> Optional[int]:
    """1-based position of a home in the list just shown ("the second one",
    "#2", "option 3", "the last house"), -1 for "last", or None."""
    m = ORDINAL_RE.search(message or "")
    if m:
        return ORDINALS[m.group(1).lower()]
    m = NUMBER_RE.search(message or "")
    return int(m.group(1)) if m else None


def home_named_in(message: str) -> Optional[dict]:
    """A live listing whose title the message quotes (copied from a card)."""
    from src.services import properties
    try:
        rows, _ = properties.all_properties()
        t = properties._norm(message)
        named = [p for p in rows if p.get("id") and len(properties._norm(p.get("title"))) >= 8
                 and properties._norm(p.get("title")) in t]
        return max(named, key=lambda p: len(properties._norm(p.get("title")))) if named else None
    except Exception as e:
        print(f"Owner notification: title match failed (non-fatal): {e}")
        return None


def home_asked_about(message: str, listing_id: Optional[str] = None, listing_title: Optional[str] = None,
                     shown_listings: Optional[list] = None, visit_home: Optional[dict] = None) -> Optional[dict]:
    """{"id", "title"} of the home this chat message is about, or None."""
    if visit_home and visit_home.get("id"):
        return {"id": visit_home["id"], "title": visit_home.get("title")}
    if listing_id:
        return {"id": listing_id, "title": listing_title}
    shown = [s for s in (shown_listings or []) if s.get("id")]
    pos = position_in(message) if shown else None
    if pos is not None:
        index = len(shown) - 1 if pos == -1 else pos - 1
        if 0 <= index < len(shown):
            return {"id": shown[index]["id"], "title": shown[index].get("title")}
    p = home_named_in(message)
    return {"id": p["id"], "title": p.get("title")} if p else None


def from_chat(message: str, account_id: Optional[str], account_role: Optional[str], phone: Optional[str] = None,
              listing_id=None, listing_title=None, shown_listings=None, visit_home=None, channel: str = "web"):
    """chat.py hook, called once per chat turn. Never raises."""
    if account_role not in ASKER_ROLES or not account_id or not db.ENABLED:
        return
    try:
        home = home_asked_about(message, listing_id, listing_title, shown_listings, visit_home)
        if not home:
            return

        def run():
            asker = _one("accounts", {"id": f"eq.{account_id}"})
            if asker:
                notify(home["id"], asker, message, "whatsapp" if channel == "whatsapp" else "chat", home.get("title"), phone)

        if RUN_IN_BACKGROUND:
            threading.Thread(target=run, daemon=True).start()
        else:
            run()
    except Exception as e:
        print(f"Owner notification (chat) failed (non-fatal): {e}")


# ---------------------------------------------------------------------
# Owner's API (notifications.js)
# ---------------------------------------------------------------------

def view(n: dict) -> dict:
    return {k: n.get(k) for k in ("id", "property_id", "property_title", "asker_role", "asker", "question",
                                  "source", "ask_count", "read_at", "created_at", "updated_at")}


@router.get("")
def my_notifications(request: Request, limit: int = 30):
    """The owner's recent "asked about your house" notifications."""
    account = accounts.current_account(request)
    if not account or not db.ENABLED:
        return {"notifications": [], "unread": 0}
    try:
        rows = db._get("owner_notifications", {
            "owner_account_id": f"eq.{account['id']}", "order": "updated_at.desc",
            "limit": str(max(1, min(limit, 100))), "select": "*",
        }) or []
    except Exception as e:
        print(f"Loading owner notifications failed (non-fatal): {e}")
        return {"notifications": [], "unread": 0, "setup_needed": True}
    return {"notifications": [view(r) for r in rows], "unread": sum(1 for r in rows if not r.get("read_at"))}


def _mine(request: Request) -> dict:
    account = accounts.current_account(request)
    if not account:
        raise HTTPException(401, "Please log in.")
    if not db.ENABLED:
        raise HTTPException(503, "Supabase isn't configured.")
    return account


@router.post("/{notification_id}/read")
def mark_read(notification_id: str, request: Request):
    account = _mine(request)
    db._patch("owner_notifications", {"read_at": now_iso()},
              {"id": f"eq.{notification_id}", "owner_account_id": f"eq.{account['id']}"})
    return {"status": "ok"}


@router.post("/read-all")
def mark_all_read(request: Request):
    account = _mine(request)
    db._patch("owner_notifications", {"read_at": now_iso()},
              {"owner_account_id": f"eq.{account['id']}", "read_at": "is.null"})
    return {"status": "ok"}
