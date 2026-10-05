"""
HubSpot CRM sync.

Every WhatsApp lead is mirrored into HubSpot so the team's CRM stays live
without manual entry: one Contact per person, one Inquiry per analyzed
chat turn, and Property records for listings owners submit through chat.

Portal objects (discovered via GET /crm/v3/schemas on this portal):

  Contacts (0-1)                              - the person (tenant/owner/buyer)
  p247376832_inquiries  ("Inquiry", custom)   - one record per analyzed chat turn
  p247376832_properties ("Property", custom)  - mirrors a Supabase listing

Deals exist in this portal but its custom fields (implementation_services,
number_of_users_licenses, ...) look like an unrelated SaaS template, not a
real-estate pipeline, so nothing here creates Deals. Add that once the
Deals pipeline/stage values are defined.

Association typeIds are portal-specific and were looked up once via
GET /crm/v4/associations/{from}/{to}/labels - they are not guessable,
so if you ever recreate these associations in HubSpot, re-run that
lookup and update the constants below.

Best-effort throughout, matching db.py/viewings.py: a HubSpot hiccup
must never break the chat reply the customer is waiting on.
"""

import os
from datetime import datetime, timezone

import requests

from src.services.lead_scoring import parse_move_date

TOKEN = (os.getenv("HUBSPOT_ACCESS_TOKEN") or "").strip()
ENABLED = bool(TOKEN)

BASE = "https://api.hubapi.com"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
TIMEOUT = 8

CONTACTS = "contacts"
INQUIRIES = "p247376832_inquiries"
LISTINGS = "p247376832_properties"

# typeId, from GET /crm/v4/associations/{from}/{to}/labels - all USER_DEFINED.
ASSOC_CONTACT_TO_INQUIRY = 52                 # default contact<->inquiry pairing
ASSOC_CONTACT_TO_LISTING_INTERESTED = 28      # "Interested Customer"
ASSOC_CONTACT_TO_LISTING_OWNED = 30           # "Owned By"
ASSOC_LISTING_TO_INQUIRY_ABOUT = 58           # "About"


def status() -> dict:
    """Live check, not just "is the env var set" - actually calls the API
    so a revoked/mistyped token shows up as broken, not connected."""

    if not ENABLED:
        return {"status": "not_connected", "detail": "Set HUBSPOT_ACCESS_TOKEN in .env to enable HubSpot sync."}

    try:
        r = requests.get(f"{BASE}/crm/v3/objects/contacts", headers=HEADERS, params={"limit": 1}, timeout=TIMEOUT)
    except Exception as e:
        return {"status": "error", "detail": f"Could not reach HubSpot: {type(e).__name__}: {e}"}

    if r.status_code == 200:
        return {"status": "connected", "detail": "Token is valid and can read/write this portal."}
    if r.status_code == 401:
        return {"status": "invalid_token", "detail": "HUBSPOT_ACCESS_TOKEN is set but rejected (expired, revoked, or wrong portal)."}
    return {"status": "error", "detail": f"HubSpot returned {r.status_code}: {r.text[:200]}"}


# ---------------------------------------------------------------------
# Low-level API helpers
# ---------------------------------------------------------------------

def _request(method: str, path: str, **kwargs):
    if not ENABLED:
        return None
    try:
        r = requests.request(method, f"{BASE}{path}", headers=HEADERS, timeout=TIMEOUT, **kwargs)
        if r.status_code >= 400:
            print(f"HubSpot {method} {path} -> {r.status_code}: {r.text[:300]}")
            return None
        return r.json() if r.content else {}
    except Exception as e:
        print(f"HubSpot request failed (non-fatal): {type(e).__name__}: {e}")
        return None


def _find_by_property(object_type: str, prop_name: str, value) -> str | None:
    if not value:
        return None
    body = {
        "filterGroups": [{"filters": [{"propertyName": prop_name, "operator": "EQ", "value": str(value)}]}],
        "properties": ["hs_object_id"],
        "limit": 1,
    }
    data = _request("POST", f"/crm/v3/objects/{object_type}/search", json=body)
    results = (data or {}).get("results") or []
    return results[0]["id"] if results else None


def _upsert(object_type: str, find_prop: str, find_value, properties: dict) -> str | None:
    """Search by find_prop, PATCH if found, else POST a new record."""
    properties = {k: v for k, v in (properties or {}).items() if v not in (None, "", [])}
    if not properties:
        return None
    existing_id = _find_by_property(object_type, find_prop, find_value)
    if existing_id:
        _request("PATCH", f"/crm/v3/objects/{object_type}/{existing_id}", json={"properties": properties})
        return existing_id
    data = _request("POST", f"/crm/v3/objects/{object_type}", json={"properties": properties})
    return (data or {}).get("id")


def associate(from_type: str, from_id: str, to_type: str, to_id: str, type_id: int):
    if not (from_id and to_id):
        return
    _request(
        "PUT",
        f"/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}/{to_id}",
        json=[{"associationCategory": "USER_DEFINED", "associationTypeId": type_id}],
    )


# ---------------------------------------------------------------------
# Enum-safe conversions - these four properties are strict single-select
# fields in this portal (a value outside the allowed list is rejected
# with a 400), and each uses its own taxonomy/casing, not this app's.
# Looked up via GET /crm/v3/properties/{object}/{name}.
# ---------------------------------------------------------------------

def _yes_no(value):
    if value is None:
        return None
    return "Yes" if value else "No"


ROLE_MAP = {  # -> contacts.customer_role, inquiries.role
    "tenant": "Tenant", "owner": "Owner", "buyer": "Buyer",
    "seller": "Seller", "property_manager": "Property Manager", "investor": "Investor",
}

TEMPERATURE_MAP = {  # -> contacts.lead_temperature (exact portal casing)
    "hot": "Hot", "warm": "WARM", "nurture": "NURTURE", "unqualified": "UNQUALIFIED",
}

SENTIMENT_MAP = {  # -> inquiries.sentiment (no "mixed" option on this portal)
    "positive": "Positive", "neutral": "Neutral", "negative": "Negative", "mixed": "Neutral",
}

INTENT_MAP = {  # this app's granular intents -> inquiries.intent's 6 buckets
    "property_search": "Property Search", "property_requirements": "Property Search",
    "list_property": "Property Listing", "advertise_property": "Property Listing", "update_property": "Property Listing",
    "property_inquiry": "Property Information", "rent_question": "Property Information",
    "buy_question": "Property Information", "property_question": "Property Information",
    "general_question": "Property Information",
    "maintenance_issue": "Maintenance",
    "schedule_viewing": "Viewing Request",
    # Investor intents mapped onto this portal's fixed 6-value enum (no
    # "Investment" bucket exists there yet - closest match used instead).
    "investor_inquiry": "Property Information", "portfolio_update": "Property Information",
    "investment_analysis_request": "Property Search", "property_offer": "Property Listing",
    "financing_question": "Property Information", "tenant_placement": "Property Listing",
}

CHANNEL_MAP = {"whatsapp": "WhatsApp", "web": "Website"}  # -> inquiries.channel

INQUIRY_QUALIFICATION_MAP = {  # lead_status -> inquiries.qualification_status
    "hot": "Qualified", "warm": "Qualified", "nurture": "In Progress", "unqualified": "Unqualified",
}

AVAILABILITY_MAP = {"pending": "Pending", "active": "Available"}  # Supabase properties.status -> listings.availability_status

PROPERTY_TYPE_KEYWORDS = [  # substring match, first hit wins -> listings.property_type
    ("villa", "Villa"), ("studio", "Studio"), ("condo", "Condo"), ("townhouse", "Townhouse"),
    ("land", "Land"), ("plot", "Land"), ("commercial", "Commercial"), ("shop", "Commercial"),
    ("office", "Commercial"), ("house", "House"), ("flat", "Apartment"), ("apartment", "Apartment"),
]


def _date_millis(text):
    """contacts.movein_date is a HubSpot date property (epoch ms at
    midnight UTC), but the AI gives free text like "next month" - reuse
    lead_scoring's fuzzy date parser rather than guessing again here."""
    day = parse_move_date(text)
    if not day:
        return None
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def _property_type(value):
    text = str(value or "").strip().lower()
    for keyword, label in PROPERTY_TYPE_KEYWORDS:
        if keyword in text:
            return label
    return "Other" if text else None


def _contact_qualification_status(lead: dict) -> str:
    """contacts.qualification_status is a workflow stage (Not Started ->
    Qualified -> Human Verified / Rejected), not the hot/warm/nurture
    temperature - there's no field for that mapping, so it's derived."""
    verification = lead.get("human_verification")
    if verification == "verified":
        return "Human Verified"
    if verification == "not_genuine":
        return "Rejected"
    score = lead.get("lead_score") or 0
    if score >= 50:
        return "Qualified"
    if score >= 25:
        return "Partial"
    return "Not Started"


# ---------------------------------------------------------------------
# Field mapping - app data -> this portal's property internal names
# ---------------------------------------------------------------------

def _split_name(name):
    parts = str(name or "").strip().split(None, 1)
    return (parts[0] if parts else None, parts[1] if len(parts) > 1 else None)


def contact_fields(result: dict, lead: dict, customer_name: str = None) -> dict:
    req = result.get("requirements") or {}
    role = str(result.get("role") or "").lower()
    is_owner = role == "owner"
    comp = lead.get("components") or {}
    firstname, lastname = _split_name(customer_name)

    def score(key):
        return (comp.get(key) or {}).get("score")

    fields = {
        "firstname": firstname,
        "lastname": lastname,
        "customer_role": ROLE_MAP.get(role),
        "message": result.get("summary"),
        "ai_lead_score": lead.get("lead_score"),
        "lead_temperature": TEMPERATURE_MAP.get(lead.get("lead_status")),
        "qualification_status": _contact_qualification_status(lead),
        "human_verified": lead.get("human_verification") == "verified",
        "intent_score": score("intent"),
        "engagement_score": score("engagement"),
        "property_fit_score": score("property_fit"),
        "readiness_score": score("readiness"),
        "sentiment_score": score("sentiment"),
        "response_behavior_score": score("response_behavior"),
        "qualification_score": score("completeness"),
        "financial_fit_score": score("financial_fit"),
    }

    if not is_owner:
        fields.update({
            "budget": req.get("budget"),
            "bedrooms": req.get("bedrooms"),
            "bathrooms": req.get("bathrooms"),
            "preferred_location": req.get("location"),
            "property_type": _property_type(req.get("property_type")),
            "parking_required": _yes_no(req.get("parking")),
            "pets": _yes_no(req.get("pets")),
            "movein_date": _date_millis(req.get("move_in_date")),
        })

    return fields


def inquiry_fields(result: dict, lead: dict, message: str, phone: str, channel: str, property_id: str = None) -> dict:
    qualification = result.get("qualification") or {}
    flags = qualification.get("signal_flags") or {}
    reasons = lead.get("reasons") or []
    escalation = bool(
        flags.get("not_interested")
        or "Not interested" in reasons
        or lead.get("human_verification") == "not_genuine"
    )
    recommended_action = {
        "hot": "Call within the hour",
        "warm": "Send more matching listings",
        "nurture": "Follow up in a few days",
        "unqualified": "Low priority / archive",
    }.get(lead.get("lead_status"), "Review manually")
    role = str(result.get("role") or "").lower()

    return {
        "inquiry_name": f"{role or 'lead'} - {result.get('intent') or 'inquiry'} - {phone or 'unknown'}"[:255],
        "channel": CHANNEL_MAP.get(channel, "Other"),
        "contact_id": phone,
        "intent": INTENT_MAP.get(result.get("intent"), "Other"),
        "role": ROLE_MAP.get(role),
        "sentiment": SENTIMENT_MAP.get(str(result.get("sentiment") or "").lower()),
        "message": message,
        "ai_summary": result.get("summary"),
        "budget": (result.get("requirements") or {}).get("budget"),
        "property_id": property_id,
        "qualification_status": INQUIRY_QUALIFICATION_MAP.get(lead.get("lead_status")),
        "recommended_action": recommended_action,
        "escalation_required": escalation,
    }


def listing_fields(prop: dict) -> dict:
    return {
        "property_id": prop.get("id"),
        "address": prop.get("location"),
        "city": prop.get("city"),
        "bedrooms": prop.get("bedrooms"),
        "bathrooms": prop.get("bathrooms"),
        "property_type": _property_type(prop.get("property_type")),
        "price": prop.get("rent") or prop.get("sale_price"),
        "rent": prop.get("rent"),
        "availability_status": AVAILABILITY_MAP.get(prop.get("status")),
        "parking_available": _yes_no(prop.get("parking")),
        "pets_allowed": _yes_no(prop.get("pets_allowed")),
        "pm_property_id": prop.get("id"),
        "owner_id": prop.get("owner_phone"),
    }


# ---------------------------------------------------------------------
# Object-level upserts
# ---------------------------------------------------------------------

def upsert_contact(phone: str, fields: dict) -> str | None:
    return _upsert(CONTACTS, "phone", phone, {"phone": phone, **fields})


def create_inquiry(fields: dict) -> str | None:
    if not fields.get("inquiry_name"):
        return None
    fields = {k: v for k, v in fields.items() if v not in (None, "", [])}
    data = _request("POST", f"/crm/v3/objects/{INQUIRIES}", json={"properties": fields})
    return (data or {}).get("id")


def upsert_listing(property_id: str, fields: dict) -> str | None:
    return _upsert(LISTINGS, "property_id", property_id, fields)


# ---------------------------------------------------------------------
# Investors
# ---------------------------------------------------------------------

INVESTOR_JOURNEY_LABELS = {"new_investor": "New Property Investor", "existing_investor": "Existing Property Investor"}
INVESTOR_TYPE_MAP = {"new": "First-time", "existing": "Existing"}


# investor_* custom Contact properties this sync writes. Unlike CONTACTS/
# INQUIRIES/LISTINGS above (discovered from this live portal), these were
# not - create them as Contact properties (Settings -> Properties -> Contact)
# before enabling, or PATCH will 400 and the sync will just log and skip
# (see _request), same as any other unconfigured field in this file.
def investor_fields(investor: dict, stage_label: str = None) -> dict:
    return {
        "investor_type": INVESTOR_TYPE_MAP.get(investor.get("investor_type")),
        "investor_journey": INVESTOR_JOURNEY_LABELS.get(investor.get("journey")),
        "investor_stage": stage_label,
        "investor_status": investor.get("status"),
        "investment_goals": investor.get("investment_goals"),
        "investment_budget": investor.get("budget"),
        "investment_location": investor.get("location"),
        "investment_strategy": investor.get("investment_strategy"),
        "risk_tolerance": investor.get("risk_tolerance"),
        "investment_timeline": investor.get("timeline"),
        "existing_property_count": investor.get("existing_property_count"),
        "financing_requirements": investor.get("financing_requirements"),
    }


def sync_investor(investor: dict, stage_label: str = None) -> str | None:
    """Upsert the investor's Contact with their profile and pipeline stage.
    Best-effort and non-fatal, like the rest of this module - called from
    investors.py after any save or stage move, independently of sync_turn()
    (which only fires on a chat turn with a phone number)."""

    if not ENABLED or not investor.get("phone"):
        return None

    fields = {"customer_role": "Investor", "firstname": investor.get("name"),
              **investor_fields(investor, stage_label)}
    return upsert_contact(investor["phone"], fields)


# ---------------------------------------------------------------------
# Main entry point - call once per analyzed chat turn
# ---------------------------------------------------------------------

def sync_turn(
    result: dict,
    lead: dict,
    message: str,
    customer_phone: str,
    customer_name: str = None,
    channel: str = "whatsapp",
    property_id: str = None,
):
    """Mirror one chat turn into HubSpot: upsert the Contact, upsert the
    Property if the owner just (re)submitted a listing this turn, log an
    Inquiry record, and link them all up. Silently does nothing if
    HUBSPOT_ACCESS_TOKEN isn't set or there's no phone number to key on."""

    if not ENABLED or not customer_phone:
        return

    contact_id = upsert_contact(customer_phone, contact_fields(result, lead, customer_name))

    listing_hs_id = None
    owner_listing = result.get("owner_listing") or {}

    if owner_listing.get("saved") and owner_listing.get("fields"):
        listing_hs_id = upsert_listing(owner_listing["fields"].get("id"), listing_fields(owner_listing["fields"]))
        if contact_id and listing_hs_id:
            associate(CONTACTS, contact_id, LISTINGS, listing_hs_id, ASSOC_CONTACT_TO_LISTING_OWNED)

    elif property_id:
        listing_hs_id = _find_by_property(LISTINGS, "property_id", property_id)
        if contact_id and listing_hs_id:
            associate(CONTACTS, contact_id, LISTINGS, listing_hs_id, ASSOC_CONTACT_TO_LISTING_INTERESTED)

    inquiry_id = create_inquiry(inquiry_fields(result, lead, message, customer_phone, channel, property_id))

    if contact_id and inquiry_id:
        associate(CONTACTS, contact_id, INQUIRIES, inquiry_id, ASSOC_CONTACT_TO_INQUIRY)
    if listing_hs_id and inquiry_id:
        associate(LISTINGS, listing_hs_id, INQUIRIES, inquiry_id, ASSOC_LISTING_TO_INQUIRY_ABOUT)
