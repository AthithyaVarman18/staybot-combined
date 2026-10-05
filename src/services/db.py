"""
Thin Supabase (PostgREST) client used for persistence.

Only the backend talks to Supabase - it uses the service_role key, which
bypasses Row Level Security, so that key must never be sent to the browser.
The frontend only ever talks to *our* FastAPI endpoints.

If SUPABASE_URL / SUPABASE_SERVICE_KEY aren't set, every function here
becomes a no-op (returns None / [] ) so the rest of the app keeps working
without a database configured, same as the AI keys.
"""

import os
import requests

from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()

ENABLED = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

REST_URL = f"{SUPABASE_URL}/rest/v1"

HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

TIMEOUT = 15

# A dropped connection ("Connection aborted / RemoteDisconnected", a reset
# from Supabase's edge) is usually gone a moment later - try again rather
# than failing the user's action.
RETRY_DELAYS = (0.4, 1.2)


def _send(method: str, url: str, retry: bool = True, **kwargs):
    import time
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            response = requests.request(method, url, timeout=TIMEOUT, **kwargs)
            response.retried = attempt > 0
            return response
        except requests.exceptions.ConnectionError as error:
            if not retry or attempt == len(RETRY_DELAYS):
                raise
            print(f"Supabase {method} {url.rsplit('/', 1)[-1]} dropped ({error.__class__.__name__}), retrying")
            time.sleep(RETRY_DELAYS[attempt])


def _get(path: str, params: dict = None):
    r = _send("GET", f"{REST_URL}/{path}", headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict, params: dict = None):
    headers = {**HEADERS, "Prefer": "return=representation"}
    # Only retried when the row brings its own id: if the first attempt did
    # reach the database before the connection dropped, the retry is then
    # refused as a duplicate (409) instead of saving the row twice, and the
    # row that was saved is returned. Rows with a database-made id aren't
    # retried - a retry there could create a duplicate.
    own_id = isinstance(body, dict) and body.get("id") is not None
    r = _send("POST", f"{REST_URL}/{path}", retry=own_id, headers=headers, params=params, json=body)
    if own_id and r.status_code == 409 and getattr(r, "retried", False):
        found = _get(path, {"id": f"eq.{body['id']}", "select": "*", "limit": "1"})
        if found:
            return found[0]
    r.raise_for_status()
    data = r.json()
    return data[0] if isinstance(data, list) and data else data


def _patch(path: str, body: dict, params: dict):
    headers = {**HEADERS, "Prefer": "return=representation"}
    r = _send("PATCH", f"{REST_URL}/{path}", headers=headers, params=params, json=body)
    r.raise_for_status()
    data = r.json()
    return data[0] if isinstance(data, list) and data else data


def _delete(path: str, params: dict):
    r = _send("DELETE", f"{REST_URL}/{path}", headers=HEADERS, params=params)
    r.raise_for_status()


# ---------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------

def get_conversation(session_id: str, listing_id: str | None):
    """Return the most recent conversation for this (session_id, listing_id)
    pair, or None if there isn't one yet."""

    if not ENABLED:
        return None

    params = {
        "session_id": f"eq.{session_id}",
        "order": "updated_at.desc",
        "limit": "1",
        "select": "*",
    }

    if listing_id:
        params["listing_id"] = f"eq.{listing_id}"
    else:
        params["listing_id"] = "is.null"

    rows = _get("conversations", params)
    return rows[0] if rows else None


def latest_conversation(session_id: str):
    """The most recently active conversation for this session, whichever
    listing it is about (or none)."""

    if not ENABLED or not session_id:
        return None

    rows = _get("conversations", {"session_id": f"eq.{session_id}", "order": "updated_at.desc",
                                  "limit": "1", "select": "*"})
    return rows[0] if rows else None


def create_conversation(session_id: str, listing_id, listing_title, persona, property_context):
    if not ENABLED:
        return None

    body = {
        "session_id": session_id,
        "listing_id": listing_id,
        "listing_title": listing_title,
        "persona": persona,
        "property_context": property_context or {},
    }
    return _post("conversations", body)


def get_or_create_conversation(session_id: str, listing_id, listing_title, persona, property_context):
    if not ENABLED:
        return None

    existing = get_conversation(session_id, listing_id)
    if existing:
        return existing

    return create_conversation(session_id, listing_id, listing_title, persona, property_context)


def update_conversation(conversation_id: str, fields: dict):
    if not ENABLED or not conversation_id:
        return None

    return _patch("conversations", fields, {"id": f"eq.{conversation_id}"})


def delete_conversation(session_id: str, listing_id: str | None):
    if not ENABLED:
        return

    params = {"session_id": f"eq.{session_id}"}
    if listing_id:
        params["listing_id"] = f"eq.{listing_id}"
    else:
        params["listing_id"] = "is.null"

    _delete("conversations", params)


def archive_conversations(session_id: str):
    """"New chat": move every thread of this session out of the way so the
    next message (and the next page load) starts fresh. The rows are kept -
    only their session_id changes - so nothing that points at them breaks
    (onboarding cases, viewings, enquiries, maintenance tickets, the
    investor profile) and the team's Leads view still has the conversation.
    Deleting could fail on those references and leave the old chat in place."""
    if not ENABLED or not session_id:
        return
    import time
    _patch("conversations", {"session_id": f"{session_id}~archived~{int(time.time())}"},
           {"session_id": f"eq.{session_id}"})


def list_leads(limit: int = 200):
    if not ENABLED:
        return []

    params = {
        "order": "updated_at.desc",
        "limit": str(limit),
        "select": "*",
    }
    return _get("conversations", params)


def get_lead(conversation_id: str):
    if not ENABLED:
        return None

    rows = _get("conversations", {"id": f"eq.{conversation_id}", "select": "*"})
    return rows[0] if rows else None


# ---------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------

def list_properties():
    """Active listings. Raises if the properties table doesn't exist yet
    (supabase_properties.sql not run), so the caller can fall back."""

    if not ENABLED:
        return None

    params = {
        "status": "eq.active",
        "order": "created_at.asc",
        "select": "*",
    }
    return _get("properties", params)


def list_properties_by_status(status: str, limit: int = 200):
    if not ENABLED:
        return []

    params = {
        "status": f"eq.{status}",
        "order": "updated_at.desc",
        "limit": str(limit),
        "select": "*",
    }
    return _get("properties", params)


def find_owner_draft(conversation_id: str = None, session_id: str = None, statuses: tuple = ("pending",)):
    """The listing this owner is still filling in through the chat.
    Normally only a 'pending' draft; investor accounts' listings go live
    straight away (owner_listings.py), so for them an 'active' one counts
    too - otherwise every later detail they add would start a new listing."""

    if not ENABLED or not (conversation_id or session_id):
        return None

    params = {
        "source": "eq.owner_chat",
        "status": f"in.({','.join(statuses)})",
        "order": "created_at.desc",
        "limit": "1",
        "select": "*",
    }

    if conversation_id:
        params["conversation_id"] = f"eq.{conversation_id}"
    else:
        params["session_id"] = f"eq.{session_id}"

    rows = _get("properties", params)
    return rows[0] if rows else None


def create_property(fields: dict):
    if not ENABLED:
        return None

    return _post("properties", fields)


def update_property(property_id: str, fields: dict):
    if not ENABLED or not property_id:
        return None

    return _patch("properties", fields, {"id": f"eq.{property_id}"})


# ---------------------------------------------------------------------
# Viewings
# ---------------------------------------------------------------------

OPEN_VIEWING_STATUSES = "(requested,confirmed)"


def find_open_viewing(property_id: str, conversation_id: str = None, session_id: str = None):
    """The customer's current (requested or confirmed) viewing for this
    property, so a new day/time reschedules it instead of adding another."""

    if not ENABLED or not (conversation_id or session_id):
        return None

    params = {
        "property_id": f"eq.{property_id}",
        "status": f"in.{OPEN_VIEWING_STATUSES}",
        "order": "created_at.desc",
        "limit": "1",
        "select": "*",
    }

    if conversation_id:
        params["conversation_id"] = f"eq.{conversation_id}"
    else:
        params["session_id"] = f"eq.{session_id}"

    rows = _get("viewings", params)
    return rows[0] if rows else None


def create_viewing(fields: dict):
    if not ENABLED:
        return None

    return _post("viewings", fields)


def update_viewing(viewing_id: str, fields: dict):
    if not ENABLED or not viewing_id:
        return None

    return _patch("viewings", fields, {"id": f"eq.{viewing_id}"})


def list_viewings(status: str = None, limit: int = 200):
    if not ENABLED:
        return []

    params = {
        "order": "viewing_date.asc,viewing_time.asc",
        "limit": str(limit),
        "select": "*",
    }

    if status:
        params["status"] = f"eq.{status}"

    return _get("viewings", params)


# ---------------------------------------------------------------------
# MLS listings (read-only lookup - see supabase_mls_listings.sql)
# ---------------------------------------------------------------------

def get_mls_listing(list_number: str):
    """An MLS listing - or, when list_number is a home an investor listed
    for sale through Staybot (see owner_sale_listings()), that home in the
    same shape, so selecting / analysing it works like any MLS home."""
    if not ENABLED or not list_number:
        return None

    rows = _get("mls_listings", {"list_number": f"eq.{list_number}", "select": "*"})
    if rows:
        return rows[0]
    return get_owner_sale_listing(list_number)


# ---------------------------------------------------------------------
# Homes investors list themselves (owner_listings.py / my_listings.py),
# shown to other investors alongside the MLS feed
# ---------------------------------------------------------------------

OWNER_LISTING_SOURCES = ("owner_chat", "investor_form")


def owner_listing_as_mls_row(p: dict) -> dict:
    """A properties-table row, reshaped with the mls_listings column names
    that investor matching / deal analysis read (list_number, list_price,
    street_address, ...). Values the owner never gave stay None."""
    photos = p.get("photos") or []
    city = p.get("area") or p.get("city")
    # Owners often give just the area ("Garner") as the location; then
    # "Garner, Garner" says nothing, so use the listing's own title instead
    # ("1 bedroom house for sale in Garner" -> "1 bedroom house for sale").
    location = str(p.get("location") or "").strip()
    title = str(p.get("title") or "").strip()
    if city and title.lower().endswith(f" in {str(city).lower()}"):
        title = title[: -len(f" in {city}")]
    street = location if location and location.lower() != str(city or "").lower() else (title or location or None)
    return {
        "list_number": p.get("id"),
        "status_label": "active",
        "property_type": p.get("property_type"),
        "street_address": street,
        "city": city,
        "neighborhood": p.get("area"),
        "subdivision": None,
        "postal_code": None,
        "bedrooms": p.get("bedrooms"),
        "bathrooms_full": p.get("bathrooms"),
        "living_area": None,
        "year_built": None,
        "list_price": p.get("sale_price"),
        "tax_annual": None,
        "hoa_fee": None,
        "hoa_frequency": None,
        "photo_url": photos[0] if photos else None,
        "days_on_market": None,
        "price_per_sqft": None,
        "remarks": p.get("description"),
        "county": None,
        "listing_date": str(p.get("created_at") or "")[:10] or None,
        "owner_listed": True,
        "owner_session_id": p.get("session_id"),
        "title": p.get("title"),
    }


def owner_sale_listings(max_price: float = None, exclude_session_id: str = None, limit: int = 300):
    """Active for-sale homes investors listed through Staybot, as MLS-shaped
    rows. exclude_session_id leaves out the asking investor's own homes.
    Returns [] (never raises) if the properties table isn't set up."""
    if not ENABLED:
        return []

    params = {
        "status": "eq.active",
        "listing_type": "eq.sale",
        "source": f"in.({','.join(OWNER_LISTING_SOURCES)})",
        "order": "sale_price.asc",
        "limit": str(limit),
        "select": "*",
    }
    if max_price:
        params["sale_price"] = f"lte.{int(max_price)}"

    try:
        rows = _get("properties", params) or []
    except Exception as e:
        print(f"Owner-listed sale homes lookup failed (non-fatal): {e}")
        return []

    return [
        owner_listing_as_mls_row(p) for p in rows
        if p.get("sale_price") and not (exclude_session_id and p.get("session_id") == exclude_session_id)
    ]


def get_owner_sale_listing(property_id: str):
    if not ENABLED or not property_id:
        return None
    try:
        rows = _get("properties", {"id": f"eq.{property_id}", "select": "*", "limit": "1"})
    except Exception:
        return None
    p = rows[0] if rows else None
    if not p or p.get("status") != "active" or p.get("listing_type") != "sale" \
            or p.get("source") not in OWNER_LISTING_SOURCES or not p.get("sale_price"):
        return None
    return owner_listing_as_mls_row(p)


# ---------------------------------------------------------------------
# Property inquiries
# ---------------------------------------------------------------------

def create_inquiry(fields: dict):
    if not ENABLED:
        return None

    try:
        return _post("property_inquiries", fields)
    except requests.exceptions.HTTPError as error:
        # lead_summary/lead_score/lead_status are a newer addition
        # (supabase_property_inquiries.sql) - if that migration hasn't been
        # run yet, PostgREST rejects the whole insert over the unknown
        # columns. Retry once without them rather than losing the inquiry
        # itself; the snapshot just won't show until the migration runs.
        text = getattr(getattr(error, "response", None), "text", "") or str(error)
        snapshot_keys = {"lead_summary", "lead_score", "lead_status"}
        if snapshot_keys & fields.keys() and "PGRST204" in text:
            print("property_inquiries is missing lead_summary/lead_score/lead_status - "
                  "run supabase_property_inquiries.sql to enable the snapshot.")
            return _post("property_inquiries", {k: v for k, v in fields.items() if k not in snapshot_keys})
        raise


def update_inquiry(inquiry_id: str, fields: dict):
    if not ENABLED or not inquiry_id:
        return None

    return _patch("property_inquiries", fields, {"id": f"eq.{inquiry_id}"})


def list_inquiries(status: str = None, limit: int = 200):
    if not ENABLED:
        return []

    params = {
        "order": "created_at.desc",
        "limit": str(limit),
        # lead_summary/lead_score/lead_status are a snapshot taken when the
        # inquiry was created (see inquiries.create_inquiry()), not a live
        # read of conversation_id - "General enquiry" is one long-running
        # conversation reused for whatever the customer asks about next, so
        # a live join would drift: an old inquiry about one property would
        # end up showing whatever they're chatting about right now.
        "select": "*",
    }

    if status:
        params["status"] = f"eq.{status}"

    return _get("property_inquiries", params)


# ---------------------------------------------------------------------
# Maintenance tickets
# ---------------------------------------------------------------------

def create_maintenance_ticket(fields: dict):
    if not ENABLED:
        return None

    return _post("maintenance_tickets", fields)


def update_maintenance_ticket(ticket_id: str, fields: dict):
    if not ENABLED or not ticket_id:
        return None

    return _patch("maintenance_tickets", fields, {"id": f"eq.{ticket_id}"})


def get_maintenance_ticket(ticket_id: str):
    if not ENABLED:
        return None

    rows = _get("maintenance_tickets", {"id": f"eq.{ticket_id}", "select": "*"})
    return rows[0] if rows else None


def find_open_maintenance_ticket(conversation_id: str):
    """The still-open ticket (needs_review/open/in_progress) for this
    conversation, if any, so a follow-up message or photo updates it
    instead of creating a duplicate."""

    if not ENABLED or not conversation_id:
        return None

    params = {
        "conversation_id": f"eq.{conversation_id}",
        "ticket_status": "in.(needs_review,open,in_progress)",
        "order": "created_at.desc",
        "limit": "1",
        "select": "*",
    }

    rows = _get("maintenance_tickets", params)
    return rows[0] if rows else None


def list_maintenance_tickets(status: str = None, limit: int = 200):
    if not ENABLED:
        return []

    params = {
        "order": "created_at.desc",
        "limit": str(limit),
        "select": "*",
    }

    if status:
        params["ticket_status"] = f"eq.{status}"

    return _get("maintenance_tickets", params)


# ---------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------

def add_message(conversation_id: str, role: str, content: str, analysis: dict = None):
    if not ENABLED or not conversation_id:
        return None

    body = {
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "analysis": analysis,
    }
    return _post("messages", body)


def list_messages(conversation_id: str):
    if not ENABLED or not conversation_id:
        return []

    params = {
        "conversation_id": f"eq.{conversation_id}",
        "order": "created_at.asc",
        "select": "*",
    }
    return _get("messages", params)


# ---------------------------------------------------------------------
# Performance log
# ---------------------------------------------------------------------

def create_ai_call(fields: dict):
    if not ENABLED:
        return None

    return _post("ai_calls", fields)


def list_ai_calls(since_iso: str, limit: int = 5000):
    if not ENABLED:
        return []

    params = {
        "created_at": f"gte.{since_iso}",
        "order": "created_at.desc",
        "limit": str(limit),
        "select": "*",
    }
    return _get("ai_calls", params)
