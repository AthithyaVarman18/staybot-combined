"""
Customer profile for one enquiry - the pop-up the team sees when they click a
card on the Inquiries tab.

Everything about the person behind the enquiry on one screen: who they are
and how to reach them, their lead score and why, the AI summary, what they
want, every home they asked about, their viewings, applications / offers,
their investor journey stage and a link to the full chat.

"The same person" means the same chat session_id (a logged-in account always
chats under its own fixed session_id) or the same phone number. Every
section is loaded on its own and fails quietly, so a table that hasn't been
set up yet (e.g. supabase_rental_applications.sql not run) just leaves that
section empty instead of breaking the pop-up.

Staff-only: served under /inquiries/<id>/profile, which team_auth.py keeps
for the team (only POST /inquiries is open to customers).
"""

import re
from datetime import date, datetime, time

from src.services import db, properties


ROLE_LABELS = {
    "tenant": "Tenant",
    "new_investor": "New investor",
    "existing_investor": "Existing investor",
    "owner": "Owner",
    "buyer": "Buyer",
}

# Staff wording (rentals.STATUS_TEXT talks to the tenant: "you rent this home").
APPLICATION_TEXT = {
    "submitted": "Applied - waiting for our team's approval",
    "team_approved": "Approved by our team - waiting for the owner",
    "approved": "Approved - renting this home",
    "team_declined": "Not approved by our team",
    "owner_declined": "Not approved by the owner",
    "withdrawn": "Withdrawn",
    "closed": "Closed - let to someone else",
    "ended": "Tenancy ended",
}
PURCHASE_TEXT = {
    "submitted": "Purchase request - waiting for our team",
    "team_approved": "Purchase request - waiting for the owner's offer",
    "approved": "Offer accepted - agreed sale",
    "owner_declined": "Not accepted by the owner",
    "withdrawn": "Withdrawn",
    "closed": "Closed - sold to another buyer",
}
OFFER_TEXT = {
    "draft": "Purchase offer being drafted",
    "awaiting_investor_approval": "Purchase offer - waiting for the investor to approve",
    "investor_approved": "Purchase offer approved by the investor",
    "handed_off": "Purchase offer sent to the seller",
    "negotiating": "Purchase offer - negotiating",
    "accepted": "Purchase offer accepted",
    "rejected": "Purchase offer rejected",
    "withdrawn": "Purchase offer withdrawn",
    "expired": "Purchase offer expired",
}


def digits(phone) -> str:
    return re.sub(r"\D", "", str(phone or ""))


def same_phone(a, b) -> bool:
    """Same number, ignoring formatting and a country code on one side."""
    a, b = digits(a), digits(b)
    if len(a) < 7 or len(b) < 7:
        return False
    return a[-10:] == b[-10:]


def _safe(fn, default=None):
    try:
        result = fn()
        return default if result is None else result
    except Exception as error:
        print(f"Inquiry profile: a section failed to load (non-fatal): {error}")
        return default


def _get(table: str, params: dict) -> list:
    return db._get(table, {"select": "*", **params}) or []


def money(value) -> str:
    """'$1,800' for a number, the text as-is for anything already formatted."""
    if value in (None, ""):
        return ""
    try:
        amount = float(str(value).replace(",", "").strip())
    except ValueError:
        return str(value).strip()
    symbol = "$" if getattr(properties, "CURRENCY", "USD") == "USD" else "₹"
    return f"{symbol}{amount:,.0f}"


def when_text(day, at) -> str:
    """'Sat 3 Oct, 11:00am' from a viewing's date and time columns."""
    try:
        d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
        label = f"{d:%a} {d.day} {d:%b}"
    except (TypeError, ValueError):
        label = str(day or "")
    if at:
        try:
            t = at if isinstance(at, time) else time.fromisoformat(str(at)[:8])
            label += f", {t.strftime('%I:%M%p').lstrip('0').lower()}"
        except ValueError:
            label += f", {at}"
    return label


# ---------------------------------------------------------------------
# Lead score: the snapshot on the enquiry, explained by the chat's parts
# ---------------------------------------------------------------------

def score_reasons(conversation: dict, limit: int = 3) -> list:
    """The strongest parts of the lead score in plain words, biggest first
    (each part's reason comes from lead_scoring.py)."""
    parts = ((conversation or {}).get("lead_components") or {}).get("components") or {}
    ranked = []
    for key, part in parts.items():
        if key == "human_verification" or not isinstance(part, dict):
            continue
        score, reason = part.get("score") or 0, (part.get("reason") or "").strip()
        if score >= 60 and reason:
            ranked.append((score * (part.get("weight") or 1), reason))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [reason for _, reason in ranked[:limit]]


def caps(conversation: dict) -> list:
    return list(((conversation or {}).get("lead_components") or {}).get("caps_applied") or [])


# ---------------------------------------------------------------------
# What they want: requirements the AI pulled out of the chat
# ---------------------------------------------------------------------

def requirements_from(messages: list) -> dict:
    """Merge every assistant reply's extracted requirements, newest winning,
    so a detail mentioned early in the chat isn't lost later."""
    merged = {}
    for m in messages or []:
        if m.get("role") != "assistant" or not isinstance(m.get("analysis"), dict):
            continue
        req = m["analysis"].get("requirements") or {}
        if isinstance(req, dict):
            merged.update({k: v for k, v in req.items() if v not in (None, "", [], {})})
    return merged


def wants_text(req: dict) -> list:
    """['2 bed', 'Garner', '$1,800/month', 'move in next month']"""
    if not req:
        return []
    bits = []
    if req.get("bedrooms") not in (None, ""):
        beds = str(req["bedrooms"]).strip()
        bits.append(f"{beds} bed" if beds.replace(".", "").isdigit() else beds)
    if req.get("property_type"):
        bits.append(str(req["property_type"]))
    if req.get("location"):
        bits.append(str(req["location"]))
    renting = str(req.get("rent_or_buy") or "").lower().startswith("rent")
    for key in ("budget", "price"):
        if req.get(key) not in (None, ""):
            amount = money(req[key])
            if renting and amount.startswith(("$", "₹")) and "/" not in amount:
                amount += "/month"
            bits.append(amount)
            break
    if req.get("rent_or_buy") and not renting:
        bits.append(f"to {req['rent_or_buy']}")
    for key, prefix in (("move_in_date", "move in "), ("available_from", "available ")):
        if req.get(key):
            bits.append(prefix + str(req[key]))
    for key, label in (("pets", "pets"), ("parking", "parking")):
        value = req.get(key)
        if value not in (None, "", False):
            bits.append(label if value is True else f"{label}: {value}")
    return bits


# ---------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------

def _mine(row: dict, session_id: str, phone: str, session_key="session_id", phone_key="customer_phone") -> bool:
    return bool((session_id and row.get(session_key) == session_id)
                or (phone and same_phone(row.get(phone_key), phone)))


def build(inquiry: dict) -> dict:
    session_id = inquiry.get("session_id")
    phone = inquiry.get("customer_phone")

    account = _safe(lambda: (_get("accounts", {"session_id": f"eq.{session_id}", "limit": "1"}) or [None])[0]) \
        if session_id else None
    account = {k: v for k, v in (account or {}).items() if k not in ("password_hash", "password_salt")} or None

    if not phone and account:
        from src.services import portfolio  # local import: portfolio imports a lot
        phone = _safe(lambda: ((portfolio.get_portfolio(account["id"]) or {}).get("details") or {}).get("phone"))

    # Every chat this person has had, newest first; the enquiry's own chat leads.
    conversations = _safe(lambda: _get("conversations", {
        "session_id": f"eq.{session_id}", "order": "updated_at.desc", "limit": "20"}), []) if session_id else []
    linked = next((c for c in conversations if c.get("id") == inquiry.get("conversation_id")), None)
    if not linked and inquiry.get("conversation_id"):
        linked = _safe(lambda: db.get_lead(inquiry["conversation_id"]))
    conversation = linked or (conversations[0] if conversations else None)

    messages = _safe(lambda: db.list_messages(conversation["id"]), []) if conversation else []
    requirements = requirements_from(messages)

    # Score: the snapshot taken when they enquired (what the card shows),
    # falling back to the chat's live score for older rows without one.
    score = inquiry.get("lead_score")
    status = inquiry.get("lead_status")
    if score is None and conversation:
        score, status = conversation.get("intent_score"), conversation.get("lead_status")

    # Every enquiry from this person - the same home can't be "asked about" twice.
    all_inquiries = _safe(lambda: db.list_inquiries(limit=500), [])
    homes = []
    for row in all_inquiries:
        if row.get("id") == inquiry.get("id") or _mine(row, session_id, phone):
            homes.append({
                "id": row.get("id"),
                "title": row.get("property_title") or row.get("property_id") or "Home",
                "price_label": row.get("price_label"),
                "area": row.get("area"),
                "status": row.get("status") or "new",
                "created_at": row.get("created_at"),
                "current": row.get("id") == inquiry.get("id"),
            })

    viewings = [
        {
            "id": v.get("id"),
            "title": v.get("property_title") or "Home",
            "when": when_text(v.get("viewing_date"), v.get("viewing_time")),
            "status": v.get("status") or "requested",
        }
        for v in _safe(lambda: db.list_viewings(limit=500), [])
        if _mine(v, session_id, phone)
    ]

    applications = []
    if account or session_id:
        rows = _safe(lambda: _get("rental_applications", {
            "tenant_session_id": f"eq.{session_id}", "order": "updated_at.desc", "limit": "20"}), [])
        for a in rows:
            purchase = isinstance(a.get("details"), dict) and a["details"].get("kind") == "purchase"
            text = (PURCHASE_TEXT if purchase else APPLICATION_TEXT).get(a.get("status"), a.get("status"))
            applications.append({"id": a.get("id"), "title": a.get("property_title") or "Home",
                                  "kind": "purchase" if purchase else "rental",
                                  "status": a.get("status"), "text": text})

    investor = None
    if account and account.get("investor_id"):
        investor = _safe(lambda: (_get("investors", {"id": f"eq.{account['investor_id']}", "limit": "1"}) or [None])[0])
    if investor:
        offers = _safe(lambda: _get("acquisition_offers", {
            "investor_id": f"eq.{investor['id']}", "order": "updated_at.desc", "limit": "20"}), [])
        for o in offers:
            applications.append({"id": o.get("id"), "title": o.get("property_title") or o.get("reference") or "Home",
                                 "kind": "offer", "status": o.get("status"),
                                 "text": OFFER_TEXT.get(o.get("status"), o.get("status"))})

    progress = None
    if investor:
        from src.services import investor_journey  # local import: avoids a cycle at startup
        progress = {"stage": investor.get("stage"),
                    "title": _safe(lambda: investor_journey.stage_title(investor), investor.get("stage")),
                    "journey": investor.get("journey")}

    role_key = (account or {}).get("role") or (conversation or {}).get("role") \
        or ("buyer" if inquiry.get("kind") == "sales" else None)

    return {
        "inquiry": inquiry,
        "customer": {
            "name": inquiry.get("customer_name") or (account or {}).get("name"),
            "email": (account or {}).get("email"),
            "phone": phone,
            "phone_digits": digits(phone),
            "role": role_key,
            "role_label": ROLE_LABELS.get(role_key, (role_key or "").replace("_", " ").capitalize() or None),
            "has_account": bool(account),
        },
        "lead": {
            "score": score,
            "status": status,
            "reasons": score_reasons(conversation),
            "caps": caps(conversation),
            "verification": (conversation or {}).get("human_verification"),
        },
        "summary": inquiry.get("lead_summary") or (conversation or {}).get("summary"),
        "wants": wants_text(requirements),
        "requirements": requirements,
        "homes": homes,
        "viewings": viewings,
        "applications": applications,
        "progress": progress,
        "conversations": [
            {"id": c.get("id"), "listing_title": c.get("listing_title") or "General enquiry",
             "updated_at": c.get("updated_at"), "linked": c is linked}
            for c in ([linked] if linked and linked not in conversations else []) + conversations
        ],
        "conversation_id": (conversation or {}).get("id"),
    }
