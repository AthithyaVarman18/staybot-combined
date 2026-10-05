"""
Owner leads from a provider's CSV file -> staff outreach -> AI chat -> listing.

1. Staff upload the provider's CSV. Columns are matched by name (many common
   spellings), phones are cleaned to +country format, and rows are checked:
   no name, or no phone and no email -> skipped; same owner twice in the file
   or already imported -> duplicate. A preview is shown before anything is saved.
2. Staff work the outreach list: call, email or WhatsApp, and record the result.
   WhatsApp is only allowed when the provider/owner consent is "whatsapp" and the
   owner is not Do Not Call. Replying STOP opts the owner out automatically.
3. The WhatsApp intro is saved into the owner's WhatsApp conversation, so when
   they reply the normal AI chat continues with that context.
4. "Convert to listing" creates a pending listing (reviewed in New listings).
All routes are team-only (TeamAuthMiddleware).
"""

import csv
import hashlib
import html as html_lib
import io
import os
import secrets
import time
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

import requests
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from src.services import db, email_sender, properties, whatsapp

router = APIRouter(prefix="/owner-leads", tags=["Owner leads"])
public_router = APIRouter(prefix="/o", include_in_schema=False)

MAX_FILE = 2 * 1024 * 1024
MAX_ROWS = 5000
STATUSES = ("new", "contacted", "interested", "call_back", "not_interested", "do_not_contact", "converted")
BUSINESS_NAME = (os.getenv("BUSINESS_NAME") or "Staybot").strip()
TEMPLATE_OWNER_INTRO = (os.getenv("WHATSAPP_TEMPLATE_OWNER_INTRO") or "").strip()
OPT_OUT_WORDS = {"stop", "unsubscribe", "stop all", "opt out", "optout"}
BUSINESS_WHATSAPP_NUMBER = re.sub(r"\D", "", os.getenv("BUSINESS_WHATSAPP_NUMBER") or "")
BUSINESS_POSTAL_ADDRESS = (os.getenv("BUSINESS_POSTAL_ADDRESS") or "").strip()
EMAILABLE_STATUSES = ("new", "contacted", "call_back")

# Canonical field -> accepted CSV header spellings (lower-case, spaces as _).
ALIASES = {
    "owner_name": ["owner_name", "name", "owner", "full_name", "contact_name", "owner_full_name"],
    "phone": ["phone", "mobile", "phone_number", "mobile_number", "contact_number", "whatsapp", "whatsapp_number"],
    "email": ["email", "email_address", "owner_email"],
    "property_title": ["property_title", "title", "listing_title", "property", "property_name"],
    "address": ["address", "street", "street_address", "property_address"],
    "area": ["area", "locality", "neighbourhood", "neighborhood", "location"],
    "city": ["city", "town"],
    "pincode": ["pincode", "pin", "zip", "zipcode", "zip_code", "postal_code"],
    "listing_type": ["listing_type", "type", "purpose", "for"],
    "price": ["price", "price_inr", "price_usd", "rent", "asking_price", "expected_price", "monthly_rent"],
    "deposit": ["deposit", "deposit_inr", "deposit_usd", "security_deposit"],
    "bedrooms": ["bedrooms", "beds", "bhk", "bedroom"],
    "bathrooms": ["bathrooms", "baths", "bathroom"],
    "sqft": ["sqft", "area_sqft", "size", "size_sqft", "carpet_area", "built_up_area"],
    "furnished": ["furnished", "furnishing"],
    "listing_url": ["listing_url", "url", "link", "listing_link"],
    "listed_on": ["listed_on", "listing_date", "date_listed", "posted_on", "date"],
    "source": ["source", "provider"],
    "consent": ["consent_to_contact", "consent", "contact_consent", "opt_in"],
    "do_not_call": ["do_not_call", "dnc", "dnd"],
    "provider_notes": ["notes", "remarks", "comments"],
}
COUNTRIES = {"IN": ("+91", "INR"), "US": ("+1", "USD")}
# The market's own country (.env BUSINESS_CURRENCY) - used when an import doesn't say.
DEFAULT_COUNTRY = "IN" if (os.getenv("BUSINESS_CURRENCY") or "USD").strip().upper() == "INR" else "US"


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_owner_leads.sql to use owner leads.")


def q(fn, *args, **kwargs):
    configured()
    try:
        return fn(*args, **kwargs)
    except requests.RequestException as error:
        text = getattr(getattr(error, "response", None), "text", "") or str(error)
        if "PGRST205" in text or "does not exist" in text:
            raise HTTPException(503, "Run supabase_owner_leads.sql in Supabase to enable owner leads.")
        print(f"Owner leads database error: {text[:300]}")
        raise HTTPException(500, "The owner leads database request failed. Please try again.")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def actor_name(header_value):
    name = (header_value or "").strip()[:120]
    return f"staff:{name}" if name else "staff:unnamed"


def log(lead_id, actor, action, details=None):
    q(db._post, "owner_lead_activity", {"lead_id": str(lead_id), "actor": actor, "action": action, "details": details or {}})


# ---------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------

def header_key(value):
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")


def map_headers(headers):
    keys = {header_key(h): h for h in headers if h}
    mapping, unused = {}, []
    for field, names in ALIASES.items():
        for name in names:
            if name in keys and keys[name] not in mapping.values():
                mapping[field] = keys[name]
                break
    unused = [h for h in headers if h and h not in mapping.values()]
    return mapping, unused


def clean_phone(value, country):
    raw = (value or "").strip()
    if not raw:
        return None, None
    digits = re.sub(r"\D", "", raw)
    code = COUNTRIES[country][0]
    if raw.startswith("+"):
        candidate = "+" + digits
    elif country == "IN" and len(digits) == 10 and digits[0] in "6789":
        candidate = "+91" + digits
    elif country == "IN" and len(digits) == 12 and digits.startswith("91"):
        candidate = "+" + digits
    elif country == "IN" and len(digits) == 11 and digits.startswith("0"):
        candidate = "+91" + digits[1:]
    elif country == "US" and len(digits) == 10:
        candidate = "+1" + digits
    elif country == "US" and len(digits) == 11 and digits.startswith("1"):
        candidate = "+" + digits
    else:
        candidate = code + digits
    if re.fullmatch(r"\+[1-9]\d{7,14}", candidate) and (not candidate.startswith("+91") or len(candidate) == 13):
        return candidate, None
    return None, f"phone '{raw}' is not a valid number"


def clean_money(value):
    text = re.sub(r"[^\d.]", "", (value or "").replace(",", ""))
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return str(amount.quantize(Decimal("0.01"))) if 0 <= amount < Decimal("1e12") else None


def clean_int(value, upper=100000):
    match = re.search(r"\d+", value or "")
    if not match:
        return None
    number = int(match.group())
    return number if 0 <= number <= upper else None


def clean_date(value):
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def clean_listing_type(value):
    v = (value or "").strip().lower()
    if v in ("rent", "rental", "lease", "for rent", "to let", "let"):
        return "rent"
    if v in ("sale", "sell", "buy", "for sale", "resale"):
        return "sale"
    return None


def clean_consent(value):
    v = (value or "").strip().lower()
    if "whatsapp" in v or v in ("sms", "text", "yes_whatsapp"):
        return "whatsapp"
    if v in ("call", "phone", "yes_call"):
        return "call"
    if "mail" in v:
        return "email"
    return "none"


def clean_bool(value):
    return (value or "").strip().lower() in ("yes", "y", "true", "1", "dnc", "dnd")


def dedupe_key(row):
    if row["phone"]:
        return f"phone:{row['phone']}"
    if row["email"]:
        return f"email:{row['email'].lower()}"
    address = " ".join(filter(None, [row.get("address"), row.get("area"), row.get("city")]))
    return "address:" + re.sub(r"[^a-z0-9]+", " ", address.lower()).strip()


def parse_csv(data: bytes, country: str, source: str):
    if len(data) > MAX_FILE:
        raise HTTPException(413, "The file is larger than 2 MB. Split it into smaller files.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "The file has no header row.")
    mapping, unused = map_headers(reader.fieldnames)
    if "owner_name" not in mapping or not ({"phone", "email"} & set(mapping)):
        raise HTTPException(400, "The file needs an owner name column and a phone or email column.")
    currency = COUNTRIES[country][1]
    rows = []
    for number, raw in enumerate(reader, start=2):
        if len(rows) >= MAX_ROWS:
            raise HTTPException(413, f"More than {MAX_ROWS} rows. Split the file.")
        get = lambda field: (raw.get(mapping[field]) or "").strip() if field in mapping else ""
        issues = []
        phone, problem = clean_phone(get("phone"), country)
        if problem:
            issues.append(problem)
        email = get("email").lower() or None
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            issues.append(f"email '{email}' is not valid")
            email = None
        row = {
            "line": number,
            "owner_name": " ".join(get("owner_name").split())[:200],
            "phone": phone, "email": email,
            "property_title": get("property_title")[:200] or None,
            "address": get("address")[:300] or None,
            "area": get("area")[:120] or None,
            "city": get("city")[:120] or None,
            "pincode": get("pincode")[:20] or None,
            "listing_type": clean_listing_type(get("listing_type")),
            "price": clean_money(get("price")), "deposit": clean_money(get("deposit")), "currency": currency,
            "bedrooms": clean_int(get("bedrooms"), 50), "bathrooms": clean_int(get("bathrooms"), 50),
            "sqft": clean_int(get("sqft")),
            "furnished": get("furnished")[:60] or None,
            "listing_url": get("listing_url")[:500] or None,
            "listed_on": clean_date(get("listed_on")),
            "source": (get("source") or source)[:120],
            "consent": clean_consent(get("consent")),
            "do_not_call": clean_bool(get("do_not_call")),
            "provider_notes": get("provider_notes")[:500] or None,
        }
        if not row["owner_name"]:
            row["result"], row["reason"] = "skipped", "no owner name"
        elif not row["phone"] and not row["email"]:
            row["result"], row["reason"] = "skipped", "no valid phone or email"
        else:
            row["result"], row["reason"] = "new", None
        row["issues"] = issues
        row["dedupe_key"] = dedupe_key(row)
        rows.append(row)
    return rows, mapping, unused


def mark_duplicates(rows):
    seen = {}
    for row in rows:
        if row["result"] != "new":
            continue
        if row["dedupe_key"] in seen:
            row["result"], row["reason"] = "duplicate", f"same owner as line {seen[row['dedupe_key']]}"
        else:
            seen[row["dedupe_key"]] = row["line"]
    keys = [k for k in seen]
    existing = set()
    for start in range(0, len(keys), 80):
        chunk = keys[start:start + 80]
        quoted = ",".join('"' + k.replace('"', '') + '"' for k in chunk)
        existing |= {r["dedupe_key"] for r in q(db._get, "owner_leads", {"dedupe_key": f"in.({quoted})", "select": "dedupe_key"})}
    for row in rows:
        if row["result"] == "new" and row["dedupe_key"] in existing:
            row["result"], row["reason"] = "duplicate", "already imported earlier"


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------

@router.post("/import")
async def import_csv(request: Request, background: BackgroundTasks, commit: bool = False, country: Literal["IN", "US"] = DEFAULT_COUNTRY,
                     source: str = "CSV provider", email_owners: bool = False,
                     x_staybot_staff: str | None = Header(default=None), x_file_name: str | None = Header(default=None)):
    """Preview (commit=false) or import (commit=true) a provider CSV."""
    configured()
    source = " ".join(source.split())[:120] or "CSV provider"
    rows, mapping, unused = parse_csv(await request.body(), country, source)
    mark_duplicates(rows)
    summary = {k: sum(r["result"] == k for r in rows) for k in ("new", "duplicate", "skipped")}
    summary["do_not_call"] = sum(r["do_not_call"] for r in rows if r["result"] == "new")
    summary["whatsapp_consent"] = sum(r["consent"] == "whatsapp" and not r["do_not_call"] for r in rows if r["result"] == "new")
    summary["with_email"] = sum(bool(r["email"]) for r in rows if r["result"] == "new")
    preview = {"summary": summary, "total": len(rows), "columns": mapping, "ignored_columns": unused,
               "rows": [{k: r[k] for k in ("line", "owner_name", "phone", "email", "property_title", "area", "listing_type",
                                         "price", "currency", "consent", "do_not_call", "result", "reason", "issues")} for r in rows]}
    if not commit:
        return {**preview, "imported": False}
    actor = actor_name(x_staybot_staff)
    new_rows = [r for r in rows if r["result"] == "new"]
    record = q(db._post, "owner_lead_imports", {
        "file_name": (x_file_name or "upload.csv")[:200], "source": source, "row_count": len(rows), "imported": len(new_rows),
        "duplicates": summary["duplicate"], "skipped": summary["skipped"], "created_by": actor})
    fields = ("dedupe_key", "owner_name", "phone", "email", "property_title", "address", "area", "city", "pincode", "listing_type",
              "price", "deposit", "currency", "bedrooms", "bathrooms", "sqft", "furnished", "listing_url", "listed_on", "source",
              "consent", "do_not_call", "provider_notes")
    created, created_ids = 0, []
    for row in new_rows:
        body = {k: row[k] for k in fields}
        body["import_id"] = record["id"]
        if row["do_not_call"]:
            body["status"] = "new"
        try:
            lead = db._post("owner_leads", body)
        except requests.HTTPError as error:
            if getattr(error.response, "status_code", None) == 409:
                continue        # imported by a parallel upload a moment ago
            raise HTTPException(500, "Import stopped part-way. Run it again; already imported owners are skipped.")
        log(lead["id"], actor, "imported", {"import_id": record["id"], "line": row["line"], "source": source})
        created += 1
        created_ids.append(lead["id"])
    queued = 0
    if email_owners:
        queued = queue_emails(background, created_ids, actor, base_url(request))
    return {**preview, "imported": True, "import_id": record["id"], "created": created, "emails_queued": queued,
            "email_mode": email_sender.status()["mode"]}


@router.get("")
def list_leads(status: str | None = None, limit: int = 500):
    configured()
    params = {"select": "*", "order": "updated_at.desc", "limit": str(max(1, min(limit, 2000)))}
    if status:
        if status not in STATUSES:
            raise HTTPException(400, f"status must be one of {STATUSES}")
        params["status"] = f"eq.{status}"
    leads = q(db._get, "owner_leads", params)
    conv_ids = sorted({l["conversation_id"] for l in leads if l.get("conversation_id")})
    replied = {}
    for start in range(0, len(conv_ids), 80):
        chunk = conv_ids[start:start + 80]
        for m in q(db._get, "messages", {"conversation_id": f"in.({','.join(chunk)})", "role": "eq.user",
                                          "select": "conversation_id,created_at", "order": "created_at.desc"}):
            replied.setdefault(m["conversation_id"], m["created_at"])
    counts = {s: 0 for s in STATUSES}
    for lead in leads:
        counts[lead["status"]] += 1
        lead["replied_at"] = replied.get(lead.get("conversation_id"))
        lead["can_whatsapp"] = can_whatsapp(lead)[0]
    for lead in leads:
        lead.pop("confirm_token_hash", None)
        lead["can_email"] = can_email(lead)
    return {"leads": leads, "counts": counts, "whatsapp_mode": "test" if whatsapp.DRY_RUN else "live",
            "email": email_sender.status(), "business_whatsapp_set": bool(BUSINESS_WHATSAPP_NUMBER)}


@router.get("/imports")
def list_imports():
    configured()
    return q(db._get, "owner_lead_imports", {"select": "*", "order": "created_at.desc", "limit": "50"})


def load(lead_id):
    rows = q(db._get, "owner_leads", {"id": f"eq.{lead_id}", "select": "*"})
    if not rows:
        raise HTTPException(404, "Owner lead not found.")
    return rows[0]


@router.get("/{lead_id}/activity")
def activity(lead_id: UUID):
    configured()
    load(lead_id)
    return q(db._get, "owner_lead_activity", {"lead_id": f"eq.{lead_id}", "select": "*", "order": "id.desc", "limit": "100"})


class LeadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["new", "contacted", "interested", "call_back", "not_interested", "do_not_contact"] | None = None
    note: str = Field(default="", max_length=1000)
    consent: Literal["whatsapp", "call", "email", "none"] | None = None
    consent_how: str = Field(default="", max_length=200)


@router.patch("/{lead_id}")
def update_lead(lead_id: UUID, body: LeadUpdate, x_staybot_staff: str | None = Header(default=None)):
    configured()
    actor = actor_name(x_staybot_staff)
    lead = load(lead_id)
    if lead["status"] == "converted" and body.status:
        raise HTTPException(409, "This owner is already converted to a listing.")
    fields, details = {}, {}
    if body.status and body.status != lead["status"]:
        fields["status"] = body.status
        details["status"] = [lead["status"], body.status]
        if body.status in ("contacted", "interested", "call_back", "not_interested"):
            fields["last_contacted_at"] = now_iso()
        if body.status == "do_not_contact":
            fields.update(do_not_call=True, consent="none", consent_source="owner_opted_out")
    if body.consent and body.consent != lead["consent"] and body.status != "do_not_contact":
        if body.consent != "none" and not body.consent_how.strip():
            raise HTTPException(400, "Say how the owner agreed (for example 'agreed on phone call on 20 Sep').")
        if lead["do_not_call"] and body.consent != "none":
            raise HTTPException(409, "This owner is on the Do Not Call list. Do not record new contact consent.")
        fields.update(consent=body.consent, consent_source=f"staff: {body.consent_how.strip()}"[:200])
        details["consent"] = [lead["consent"], body.consent, body.consent_how.strip()]
    if body.note.strip():
        fields["notes"] = ((lead.get("notes") or "") + f"\n[{now_iso()[:16]} {actor[6:]}] {body.note.strip()}").strip()[-4000:]
        details["note"] = body.note.strip()
    if not fields:
        return load(lead_id)
    q(db._patch, "owner_leads", fields, {"id": f"eq.{lead_id}"})
    log(lead_id, actor, "updated", details)
    return load(lead_id)


def can_whatsapp(lead):
    if lead["status"] in ("do_not_contact", "converted"):
        return False, "This owner is marked Do not contact." if lead["status"] == "do_not_contact" else "Already converted."
    if lead["do_not_call"] and lead.get("response") != "whatsapp":
        # The owner asking us (via the email link) to contact them on WhatsApp overrides the provider's DNC flag.
        return False, "This owner is on the Do Not Call list."
    if not lead.get("phone"):
        return False, "No phone number."
    if lead["consent"] != "whatsapp":
        return False, "No WhatsApp consent. Call or email first, then record consent."
    return True, None


def intro_message(lead):
    first = (lead["owner_name"].replace("DEMO", "").split() or ["there"])[0]
    home = lead.get("property_title") or (f"{lead['bedrooms']}BHK home" if lead.get("bedrooms") else "property")
    where = f" in {lead['area']}" if lead.get("area") else ""
    price = f" listed at {money_text(lead)}" if lead.get("price") else ""
    goal = "rent it out" if lead.get("listing_type") == "rent" else "sell it" if lead.get("listing_type") == "sale" else "rent or sell it"
    return (f"Hi {first}, this is {BUSINESS_NAME}. We saw your {home}{where}{price}. "
            f"We help owners {goal} faster with verified tenants and buyers, and we handle viewings and paperwork. "
            f"Would you like us to take care of it for you? Reply YES to know more, or STOP and we won't message you again.")


class WhatsAppIntro(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str | None = Field(default=None, max_length=1000)


@router.post("/{lead_id}/whatsapp")
def send_intro(lead_id: UUID, body: WhatsAppIntro, x_staybot_staff: str | None = Header(default=None)):
    from src.services import onboarding_whatsapp
    configured()
    actor = actor_name(x_staybot_staff)
    lead = load(lead_id)
    allowed, reason = can_whatsapp(lead)
    if not allowed:
        raise HTTPException(409, reason)
    recent = q(db._get, "owner_lead_activity", {"lead_id": f"eq.{lead_id}", "action": "eq.whatsapp_sent",
                                                 "created_at": f"gte.{(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec='seconds')}",
                                                 "select": "id"})
    if recent:
        raise HTTPException(409, "A WhatsApp message was sent to this owner in the last 10 minutes. Wait for their reply.")
    text = (body.message or "").strip() or intro_message(lead)
    phone = lead["phone"]
    wa_id = re.sub(r"\D", "", phone)
    if whatsapp.DRY_RUN:
        sent = whatsapp.send_text(wa_id, text)
        mode = "test"
    elif onboarding_whatsapp.window_open(phone):
        sent = whatsapp.send_text(wa_id, text)
        mode = "live"
    elif TEMPLATE_OWNER_INTRO:
        try:
            onboarding_whatsapp.send_payload(phone, {"type": "template", "template": {
                "name": TEMPLATE_OWNER_INTRO, "language": {"code": onboarding_whatsapp.TEMPLATE_LANGUAGE},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": lead["owner_name"].split()[0]},
                                                                {"type": "text", "text": lead.get("area") or "your area"}]}]}})
            sent, mode = [{"status_code": 200}], "live"
        except Exception as error:
            sent, mode = [{"error": str(error)[:300]}], "live"
    else:
        raise HTTPException(409, "WhatsApp needs an approved owner-intro template (WHATSAPP_TEMPLATE_OWNER_INTRO) to message "
                                 "an owner who has not written to you in the last 24 hours.")
    error = next((s.get("error") for s in sent if s.get("error")), None)
    if error:
        log(lead_id, actor, "whatsapp_failed", {"error": error, "mode": mode})
        raise HTTPException(502, f"WhatsApp did not accept the message: {error}")
    conversation = q(db.get_or_create_conversation, whatsapp.session_for(phone), None,
                     f"Owner lead · {lead['owner_name']}", "owner", {})
    if conversation:
        q(db.add_message, conversation["id"], "assistant", text)
        try:
            db.update_conversation(conversation["id"], {"role": "owner"})
        except Exception:
            pass
    q(db._patch, "owner_leads", {"status": "contacted" if lead["status"] == "new" else lead["status"],
                                 "last_contacted_at": now_iso(), "conversation_id": conversation["id"] if conversation else None},
      {"id": f"eq.{lead_id}"})
    log(lead_id, actor, "whatsapp_sent", {"mode": mode, "text": text})
    return {"sent": True, "mode": mode, "message": text, "lead": load(lead_id)}


@router.post("/{lead_id}/convert")
def convert_to_listing(lead_id: UUID, x_staybot_staff: str | None = Header(default=None)):
    configured()
    actor = actor_name(x_staybot_staff)
    lead = load(lead_id)
    if lead.get("property_id"):
        return {"property_id": lead["property_id"], "created": False, "lead": lead}
    if lead["status"] != "interested":
        raise HTTPException(409, "Mark the owner as Interested before creating their listing.")
    if lead.get("listing_type") not in ("rent", "sale"):
        raise HTTPException(400, "Set whether the home is for rent or sale first (edit the lead or re-import).")
    slug = re.sub(r"[^a-z0-9]+", "-", (lead.get("area") or lead.get("city") or "home").lower()).strip("-")[:30] or "home"
    price = int(Decimal(str(lead["price"]))) if lead.get("price") else None
    title = lead.get("property_title") or (f"{lead['bedrooms']}BHK in {lead['area']}" if lead.get("bedrooms") and lead.get("area") else "Owner listing")
    listing = {
        "id": f"lead-{slug}-{uuid.uuid4().hex[:6]}",
        "title": title[:160], "area": lead.get("area"), "city": lead.get("city"),
        "location": ", ".join(filter(None, [lead.get("address"), lead.get("area"), lead.get("city")])) or None,
        "listing_type": lead["listing_type"], "bedrooms": lead.get("bedrooms"), "bathrooms": lead.get("bathrooms"),
        "rent": price if lead["listing_type"] == "rent" else None,
        "sale_price": price if lead["listing_type"] == "sale" else None,
        "deposit": int(Decimal(str(lead["deposit"]))) if lead.get("deposit") else None,
        "furnished": lead.get("furnished"), "status": "pending", "source": "owner_lead",
        "owner_name": lead["owner_name"], "owner_phone": lead.get("phone"),
        "conversation_id": lead.get("conversation_id"),
        "description": f"Imported from {lead['source']}. Check details with the owner before approving.",
    }
    created = q(db._post, "properties", {k: v for k, v in listing.items() if v is not None})
    q(db._patch, "owner_leads", {"status": "converted", "property_id": created["id"]}, {"id": f"eq.{lead_id}"})
    log(lead_id, actor, "converted_to_listing", {"property_id": created["id"]})
    properties.clear_cache()
    return {"property_id": created["id"], "created": True, "lead": load(lead_id)}


def handle_opt_out(phone: str, text: str) -> str | None:
    """Called for every incoming WhatsApp message. An owner lead replying STOP
    is opted out immediately and never messaged again."""
    if not db.ENABLED or (text or "").strip().lower() not in OPT_OUT_WORDS:
        return None
    try:
        leads = db._get("owner_leads", {"phone": f"eq.{phone}", "select": "id,status"})
    except Exception:
        return None
    if not leads:
        return None
    for lead in leads:
        db._patch("owner_leads", {"status": "do_not_contact", "consent": "none", "do_not_call": True,
                                  "consent_source": "owner_replied_stop"}, {"id": f"eq.{lead['id']}"})
        db._post("owner_lead_activity", {"lead_id": lead["id"], "actor": f"owner:whatsapp:{phone}", "action": "opted_out", "details": {"text": text}})
    return f"Done. {BUSINESS_NAME} won't message you again. Sorry for the trouble."



# ---------------------------------------------------------------------
# Email outreach: every owner with an email gets one intro email with a
# personal link to say yes (WhatsApp / email), no, or unsubscribe.
# ---------------------------------------------------------------------

def base_url(request: Request):
    return (os.getenv("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")


def can_email(lead):
    return bool(lead.get("email")) and lead["status"] in EMAILABLE_STATUSES and lead.get("response") is None \
        and lead.get("email_status") in ("not_sent", "failed")


def indian_grouping(number: int) -> str:
    """12345678 -> 1,23,45,678 (lakh/crore commas)."""
    text = str(number)
    if len(text) <= 3:
        return text
    head, tail = text[:-3], text[-3:]
    head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
    return f"{head},{tail}"


def money_text(lead):
    if not lead.get("price"):
        return ""
    amount = int(Decimal(str(lead["price"])))
    shown = f"₹{indian_grouping(amount)}" if lead["currency"] == "INR" else f"${amount:,}"
    return shown + ("/month" if lead.get("listing_type") == "rent" else "")


def email_content(lead, link):
    first = (lead["owner_name"].replace("DEMO", "").split() or ["there"])[0]
    home = lead.get("property_title") or "property"
    where = f" in {lead['area']}" if lead.get("area") else ""
    price = money_text(lead)
    verb = "rent it out" if lead.get("listing_type") == "rent" else "sell it" if lead.get("listing_type") == "sale" else "rent or sell it"
    people = "tenants" if lead.get("listing_type") == "rent" else "buyers" if lead.get("listing_type") == "sale" else "tenants and buyers"
    subject = f"Your {home}{where}: can we help you {verb}?"
    footer = (f"You're receiving this one-time email because your property was listed publicly and shared with us by {lead['source']}. "
              f"{BUSINESS_NAME}{' · ' + BUSINESS_POSTAL_ADDRESS if BUSINESS_POSTAL_ADDRESS else ''}")
    text = (f"Hi {first},\n\nWe noticed your {home}{where}{' listed at ' + price if price else ''}.\n\n"
            f"{BUSINESS_NAME} helps owners {verb} faster: verified {people}, viewings handled for you, and the paperwork done.\n\n"
            f"Interested? Tell us how you'd like to be contacted:\n{link}\n\n"
            f"Not interested? Use the same link, or unsubscribe here: {link}/unsubscribe\n\n{footer}\n")
    e = html_lib.escape
    button = ("display:inline-block;padding:12px 20px;border-radius:24px;text-decoration:none;font-weight:600;"
              "font-family:Arial,sans-serif;font-size:15px;margin:4px 6px 4px 0")
    html = f"""<div style="font-family:Arial,sans-serif;font-size:15px;line-height:1.5;color:#222;max-width:560px">
<p>Hi {e(first)},</p>
<p>We noticed your <b>{e(home)}</b>{e(where)}{' listed at <b>' + e(price) + '</b>' if price else ''}.</p>
<p>{e(BUSINESS_NAME)} helps owners {e(verb)} faster: verified {e(people)}, viewings handled for you, and the paperwork done.</p>
<p><b>Would you like our help?</b></p>
<p><a href="{e(link)}?choice=whatsapp" style="{button};background:#E31C5F;color:#fff">Yes, contact me on WhatsApp</a>
<a href="{e(link)}?choice=email" style="{button};background:#222;color:#fff">Yes, email me</a>
<a href="{e(link)}?choice=not_interested" style="{button};border:1px solid #ddd;color:#222">No thanks</a></p>
<p style="color:#6a6a6a;font-size:12px;margin-top:28px">{e(footer)}<br><a href="{e(link)}/unsubscribe" style="color:#6a6a6a">Unsubscribe</a></p>
</div>"""
    return subject, text, html


def queue_emails(background: BackgroundTasks, lead_ids, actor, url_base):
    ids = [str(i) for i in lead_ids]
    if not ids:
        return 0
    background.add_task(send_emails, ids, actor, url_base)
    return len(ids)


def send_emails(lead_ids, actor, url_base):
    """Background: one email per lead, claimed first so a lead is never emailed twice."""
    for lead_id in lead_ids:
        try:
            send_one(lead_id, actor, url_base)
        except Exception as error:
            print(f"Owner lead email failed for {lead_id}: {error}")
        if not email_sender.DRY_RUN:
            time.sleep(1.0)      # stay well under SMTP provider rate limits


def send_one(lead_id, actor, url_base, force=False):
    rows = db._get("owner_leads", {"id": f"eq.{lead_id}", "select": "*"})
    if not rows:
        return {"sent": False, "reason": "not found"}
    lead = rows[0]
    if not lead.get("email"):
        return {"sent": False, "reason": "no email address"}
    if lead["status"] not in EMAILABLE_STATUSES or lead.get("response"):
        return {"sent": False, "reason": "owner already responded or is not to be contacted"}
    allowed = ("not_sent", "failed", "sent", "test_sent") if force else ("not_sent", "failed")
    if lead["email_status"] not in allowed:
        return {"sent": False, "reason": "already emailed"}
    token = secrets.token_urlsafe(24)
    claimed = db._patch("owner_leads", {"email_status": "sending", "confirm_token_hash": hashlib.sha256(token.encode()).hexdigest()},
                        {"id": f"eq.{lead_id}", "email_status": f"eq.{lead['email_status']}"})
    if not claimed:
        return {"sent": False, "reason": "already being emailed"}
    link = f"{url_base}/o/{token}"
    subject, text, html = email_content(lead, link)
    result = email_sender.send(lead["email"], subject, text, html, unsubscribe_url=f"{link}/unsubscribe")
    if result["ok"]:
        db._patch("owner_leads", {"email_status": "test_sent" if result["test_mode"] else "sent", "email_sent_at": now_iso(),
                                  "email_error": None, "status": "contacted" if lead["status"] == "new" else lead["status"],
                                  "last_contacted_at": now_iso()}, {"id": f"eq.{lead_id}"})
        db._post("owner_lead_activity", {"lead_id": lead_id, "actor": actor, "action": "email_sent",
                                         "details": {"test_mode": result["test_mode"], "subject": subject}})
        if result["test_mode"]:
            email_sender.OUTBOX[-1]["link"] = link      # test mode only: lets staff open the owner's page
        return {"sent": True, "test_mode": result["test_mode"]}
    db._patch("owner_leads", {"email_status": "failed", "email_error": result["error"]}, {"id": f"eq.{lead_id}"})
    db._post("owner_lead_activity", {"lead_id": lead_id, "actor": actor, "action": "email_failed", "details": {"error": result["error"]}})
    return {"sent": False, "reason": result["error"]}


@router.post("/email-all")
def email_all(request: Request, background: BackgroundTasks, x_staybot_staff: str | None = Header(default=None)):
    """Email every owner who has an email and has not been emailed or responded yet."""
    configured()
    leads = q(db._get, "owner_leads", {"select": "id,email,status,response,email_status", "email_status": "in.(not_sent,failed)",
                                       "status": f"in.({','.join(EMAILABLE_STATUSES)})", "response": "is.null", "limit": "5000"})
    ids = [l["id"] for l in leads if l.get("email")]
    return {"queued": queue_emails(background, ids, actor_name(x_staybot_staff), base_url(request)), "email": email_sender.status()}


class EmailOne(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resend: bool = False


@router.post("/{lead_id}/email")
def email_one(lead_id: UUID, body: EmailOne, request: Request, x_staybot_staff: str | None = Header(default=None)):
    configured()
    load(lead_id)
    result = send_one(str(lead_id), actor_name(x_staybot_staff), base_url(request), force=body.resend)
    if not result["sent"]:
        raise HTTPException(409, f"Email not sent: {result['reason']}")
    return {**result, "lead": load(lead_id)}


@router.get("/email-outbox")
def email_outbox():
    """Test mode only: the emails that would have been sent."""
    return {"email": email_sender.status(), "outbox": list(email_sender.OUTBOX)[::-1] if email_sender.DRY_RUN else []}


# ---------------------------------------------------------------------
# Owner's personal page from the email (public, token-protected)
# ---------------------------------------------------------------------

def lead_for_token(token):
    if not token or len(token) > 80 or not db.ENABLED:
        raise HTTPException(404, "This link is not valid.")
    rows = db._get("owner_leads", {"confirm_token_hash": f"eq.{hashlib.sha256(token.encode()).hexdigest()}", "select": "*"})
    if not rows:
        raise HTTPException(404, "This link is not valid or has been replaced.")
    lead = rows[0]
    if lead.get("email_sent_at") and datetime.fromisoformat(lead["email_sent_at"].replace("Z", "+00:00")) < datetime.now(timezone.utc) - timedelta(days=60):
        raise HTTPException(410, "This link has expired.")
    return lead


def whatsapp_link(lead):
    if not BUSINESS_WHATSAPP_NUMBER:
        return None
    text = f"Hi {BUSINESS_NAME}, I'm {lead['owner_name'].replace('DEMO', '').strip()} and I'd like help with my {lead.get('property_title') or 'property'}" \
           f"{' in ' + lead['area'] if lead.get('area') else ''}."
    return f"https://wa.me/{BUSINESS_WHATSAPP_NUMBER}?text={requests.utils.quote(text)}"


def mask_phone(phone):
    return f"{phone[:3]} •••••• {phone[-3:]}" if phone and len(phone) > 7 else None


@public_router.get("/{token}")
def owner_page(token: str):
    lead_for_token(token)
    return FileResponse(os.path.join(os.path.dirname(__file__), "..", "static", "owner_confirm.html"),
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"})


@public_router.get("/{token}/api")
def owner_state(token: str):
    lead = lead_for_token(token)
    return {
        "first_name": (lead["owner_name"].replace("DEMO", "").split() or ["there"])[0],
        "home": lead.get("property_title") or "your property", "area": lead.get("area"), "price": money_text(lead),
        "listing_type": lead.get("listing_type"), "business": BUSINESS_NAME, "phone_hint": mask_phone(lead.get("phone")),
        "response": lead.get("response"), "whatsapp_link": whatsapp_link(lead) if lead.get("response") == "whatsapp" else None,
    }


class OwnerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: Literal["whatsapp", "email", "not_interested"]
    whatsapp_number: str = Field(default="", max_length=30)


@public_router.post("/{token}/api")
def owner_respond(token: str, body: OwnerResponse):
    lead = lead_for_token(token)
    if lead.get("response") == "unsubscribed" or lead["status"] == "do_not_contact":
        raise HTTPException(409, "You unsubscribed. We won't contact you.")
    fields = {"response": body.choice, "responded_at": now_iso()}
    details = {"choice": body.choice}
    if body.choice == "whatsapp":
        phone = lead.get("phone")
        if body.whatsapp_number.strip():
            phone, problem = clean_phone(body.whatsapp_number, "IN" if lead["currency"] == "INR" else "US")
            if problem:
                raise HTTPException(400, "Please enter a valid WhatsApp number with country code.")
            if phone != lead.get("phone"):
                taken = db._get("owner_leads", {"phone": f"eq.{phone}", "id": f"neq.{lead['id']}", "select": "id"})
                if not taken:
                    fields["phone"] = phone
                details["new_number"] = True
        if not phone:
            raise HTTPException(400, "Please enter your WhatsApp number.")
        fields.update(consent="whatsapp", consent_source=f"owner confirmed via email link {now_iso()[:16]}", status="interested")
    elif body.choice == "email":
        fields.update(consent="email", consent_source=f"owner confirmed via email link {now_iso()[:16]}", status="interested")
    else:
        fields.update(status="not_interested")
    db._patch("owner_leads", fields, {"id": f"eq.{lead['id']}"})
    db._post("owner_lead_activity", {"lead_id": lead["id"], "actor": "owner:email_link", "action": f"owner_said_{body.choice}", "details": details})
    return owner_state(token)


@public_router.api_route("/{token}/unsubscribe", methods=["GET", "POST"])
def owner_unsubscribe(token: str):
    lead = lead_for_token(token)
    if lead.get("response") != "unsubscribed":
        db._patch("owner_leads", {"response": "unsubscribed", "responded_at": now_iso(), "status": "do_not_contact",
                                  "consent": "none", "do_not_call": True, "consent_source": "owner unsubscribed by email"},
                  {"id": f"eq.{lead['id']}"})
        db._post("owner_lead_activity", {"lead_id": lead["id"], "actor": "owner:email_link", "action": "unsubscribed", "details": {}})
    return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><title>Unsubscribed</title>"
                        f"<body style='font-family:Arial,sans-serif;max-width:480px;margin:60px auto;padding:0 16px;color:#222'>"
                        f"<h1>You're unsubscribed</h1><p>{html_lib.escape(BUSINESS_NAME)} won't contact you again about this property.</p></body>")
