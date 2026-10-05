"""
Customers - one card per person for the staff member: the Inquiries page's
"All chats" tab and the customer pop-up that every Inquiries tab opens
(Interested, Site visits, All chats).

A customer is one chat session_id ("wa:+1919..." for a WhatsApp number, or a
logged-in account's session). Everything the app already knows about them is
pulled together here, read-only:

  conversations          their chats with the AI: score, tier, summary, role
  accounts / portfolios  name, email, phone of a logged-in account
  property_inquiries     homes they said they're interested in (+ name/phone they gave)
  viewings               site visits they asked for (+ name/phone they gave)
  rental_applications    rentals / purchases they applied for
  investor_profiles      an investor's brief (cash, areas, timeline ...)
  investors              their investor journey stage

plus the staff member's own tracking in customer_followups
(supabase_customer_followups.sql): status (new / contacted / follow-up /
won / lost), notes and a follow-up date.

Staff-only (/customers is in team_auth.STAFF_ONLY_PREFIXES).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Literal, Optional

import requests
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.services import db
from src.services import inquiry_profile as profile   # shared wording: score reasons, "what they want", status texts

router = APIRouter(prefix="/customers", tags=["Customers"])

FOLLOWUP_TABLE = "customer_followups"
SETUP_HINT = "Run supabase_customer_followups.sql in the Supabase SQL Editor to save status, notes and follow-up dates."
TIER_RANK = {"hot": 3, "warm": 2, "nurture": 1, "unqualified": 0}
CHUNK = 60

# Each Supabase read is a round trip; the card needs a dozen of them, so they
# run side by side instead of one after another (seconds -> well under one).
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="customers")


def parallel(**calls):
    """Run zero-argument callables at the same time; {name: result}."""
    futures = {name: _pool.submit(fn) for name, fn in calls.items()}
    return {name: f.result() for name, f in futures.items()}


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Supabase isn't configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY to .env.")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_in(table: str, column: str, values, select: str = "*", extra: Optional[dict] = None) -> list[dict]:
    """Rows where column is one of values (chunked, quoted - session ids
    contain ':' and '+'). A missing table just means nothing to show."""
    values = sorted({str(v) for v in values if v})
    rows: list[dict] = []
    for i in range(0, len(values), CHUNK):
        quoted = ",".join('"' + v.replace('"', "") + '"' for v in values[i:i + CHUNK])
        try:
            rows += db._get(table, {column: f"in.({quoted})", "select": select, **(extra or {})}) or []
        except requests.RequestException as e:
            print(f"Customers: reading {table} failed (non-fatal): {str(e)[:200]}")
            return rows
    return rows


def followups(session_ids) -> tuple[dict, bool]:
    """(session_id -> tracking row, table exists?)"""
    values = [s for s in session_ids if s]
    if not values:
        return {}, True
    try:
        db._get(FOLLOWUP_TABLE, {"select": "session_id", "limit": "1"})
    except requests.RequestException:
        return {}, False
    return {r["session_id"]: r for r in fetch_in(FOLLOWUP_TABLE, "session_id", values)}, True


def first(*values):
    return next((v for v in values if v not in (None, "")), None)


def contacts(sessions: list[str], conversations: list[dict]) -> dict[str, dict]:
    """session_id -> {name, phone, email, account_role} from everything the
    customer has told us anywhere."""
    out = {s: {"name": None, "phone": s[3:] if s.startswith("wa:") else None, "email": None, "account_role": None}
           for s in sessions}

    by_conv = {cv["id"]: cv.get("session_id") for cv in conversations}
    got = parallel(
        accounts=lambda: fetch_in("accounts", "session_id", sessions, "id,session_id,name,email,role"),
        inquiries=lambda: fetch_in("property_inquiries", "session_id", sessions,
                                   "session_id,customer_name,customer_phone,created_at", {"order": "created_at.desc"}),
        visits=lambda: fetch_in("viewings", "session_id", sessions,
                                "session_id,customer_name,customer_phone,created_at", {"order": "created_at.desc"}),
        profiles=lambda: fetch_in("investor_profiles", "conversation_id", list(by_conv),
                                  "conversation_id,full_name,whatsapp,email"),
    )
    accounts = got["accounts"]
    phones = {}
    if accounts:
        for p in fetch_in("account_portfolios", "account_id", [a["id"] for a in accounts], "account_id,details"):
            phones[p["account_id"]] = ((p.get("details") or {}).get("phone"))
    for a in accounts:
        c = out.get(a["session_id"])
        if c is not None:
            c.update(name=a.get("name"), email=a.get("email"), account_role=a.get("role"),
                     phone=first(c["phone"], phones.get(a["id"])))

    # Names / phones typed into an enquiry, a visit request or an investor brief.
    for g in got["inquiries"] + got["visits"]:
        c = out.get(g.get("session_id"))
        if c is not None:
            c["name"] = first(c["name"], g.get("customer_name"))
            c["phone"] = first(c["phone"], g.get("customer_phone"))

    for p in got["profiles"]:
        c = out.get(by_conv.get(p.get("conversation_id")))
        if c is not None:
            c["name"] = first(c["name"], p.get("full_name"))
            c["phone"] = first(c["phone"], p.get("whatsapp"))
            c["email"] = first(c["email"], p.get("email"))
    return out


def group_conversations(conversations: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for cv in conversations:
        if cv.get("session_id"):
            groups.setdefault(cv["session_id"], []).append(cv)
    return groups


def card(session_id: str, convs: list[dict], contact: dict, track: Optional[dict]) -> dict:
    """One row of the "All chats" list. convs: newest first."""
    latest = convs[0]
    best = max(convs, key=lambda c: (TIER_RANK.get(c.get("lead_status") or "", -1), c.get("intent_score") or 0))
    return {
        "session_id": session_id,
        "name": contact.get("name"),
        "phone": contact.get("phone"),
        "email": contact.get("email"),
        "account_role": contact.get("account_role"),
        "channel": "whatsapp" if session_id.startswith("wa:") else "web",
        "role": first(*(c.get("role") for c in convs)),
        "lead_status": best.get("lead_status"),
        "score": best.get("intent_score"),
        "summary": first(*(c.get("summary") for c in convs)),
        "last_active": latest.get("updated_at"),
        "latest_conversation_id": latest.get("id"),
        "conversation_ids": [c["id"] for c in convs],
        "verification": latest.get("human_verification") or "unverified",
        "outcome": latest.get("outcome") or "open",
        "status": (track or {}).get("status") or "new",
        "notes": (track or {}).get("notes"),
        "follow_up_at": (track or {}).get("follow_up_at"),
        "contacted_at": (track or {}).get("contacted_at"),
    }


@router.get("")
def list_customers(limit: int = 500):
    """Everyone who chatted with the AI, one row per person, hottest first
    is left to the page (it sorts and filters)."""
    configured()
    try:
        conversations = db.list_leads(limit=min(max(limit, 1), 1000)) or []
    except requests.RequestException as e:
        raise HTTPException(502, f"Could not load chats: {str(e)[:200]}")
    groups = group_conversations(conversations)
    sessions = list(groups)
    got = parallel(
        people=lambda: contacts(sessions, conversations),
        tracks=lambda: followups(sessions),
        asked=lambda: fetch_in("property_inquiries", "session_id", sessions, "session_id"),
        visits=lambda: fetch_in("viewings", "session_id", sessions, "session_id"),
    )
    people = got["people"]
    tracks, tracking_ready = got["tracks"]
    asked, visits = {}, {}
    for r in got["asked"]:
        asked[r["session_id"]] = asked.get(r["session_id"], 0) + 1
    for r in got["visits"]:
        visits[r["session_id"]] = visits.get(r["session_id"], 0) + 1

    rows = []
    for sid, convs in groups.items():
        row = card(sid, convs, people.get(sid, {}), tracks.get(sid))
        row["homes_asked"] = asked.get(sid, 0)
        row["visits"] = visits.get(sid, 0)
        rows.append(row)
    return {"customers": rows, "tracking_ready": tracking_ready, "setup_hint": None if tracking_ready else SETUP_HINT}


@router.get("/{session_id}")
def customer_detail(session_id: str):
    """Everything about one customer, for the pop-up."""
    configured()
    convs = db._get("conversations", {"session_id": f"eq.{session_id}", "order": "updated_at.desc",
                                      "select": "*", "limit": "50"}) or []
    convs.sort(key=lambda c: str(c.get("updated_at") or ""), reverse=True)   # newest first, whatever the DB returned
    conv_ids = [c["id"] for c in convs]

    def messages():
        """(the chat for the pop-up, what they want) - "what they want" is
        every requirement the AI pulled out of the chat (inquiry_profile)."""
        if not convs:
            return [], []
        try:
            raw = db.list_messages(convs[0]["id"]) or []
        except requests.RequestException as e:
            print(f"Customers: loading messages failed (non-fatal): {e}")
            return [], []
        chat = [{"role": m.get("role"), "content": m.get("content"), "created_at": m.get("created_at")}
                for m in raw if m.get("role") in ("user", "assistant")][-80:]
        return chat, profile.wants_text(profile.requirements_from(raw))

    def investor():
        """(journey stage, the team's purchase offers) - found through the
        account's investor_id first, then the chat session."""
        try:
            from src.services import investor_journey  # local import - investor_journey imports a lot
            acct = (db._get("accounts", {"session_id": f"eq.{session_id}", "select": "investor_id", "limit": "1"}) or [{}])[0]
            rows = []
            if acct.get("investor_id"):
                rows = db._get("investors", {"id": f"eq.{acct['investor_id']}", "select": "*", "limit": "1"}) or []
            if not rows:
                rows = db._get("investors", {"session_id": f"eq.{session_id}", "select": "*",
                                             "order": "updated_at.desc", "limit": "1"}) or []
            if not rows:
                return None, []
            offers = fetch_in("acquisition_offers", "investor_id", [rows[0]["id"]],
                              "id,property_title,reference,status,updated_at", {"order": "updated_at.desc"})
            return {"stage": investor_journey.stage_title(rows[0]), "type": rows[0].get("investor_type")}, offers
        except Exception as e:
            print(f"Customers: investor lookup failed (non-fatal): {e}")
            return None, []

    got = parallel(
        contact=lambda: contacts([session_id], convs).get(session_id, {}),
        tracks=lambda: followups([session_id]),
        messages=messages,
        inquiries=lambda: fetch_in("property_inquiries", "session_id", [session_id],
                                   "id,property_title,price_label,area,status,created_at,notes", {"order": "created_at.desc"}),
        visits=lambda: fetch_in("viewings", "session_id", [session_id],
                                "id,property_title,viewing_date,viewing_time,status,created_at", {"order": "viewing_date.desc"}),
        applications=lambda: fetch_in("rental_applications", "tenant_session_id", [session_id],
                                      "id,property_title,status,details,created_at", {"order": "created_at.desc"}),
        investor=investor,
        profiles=lambda: fetch_in("investor_profiles", "conversation_id", conv_ids,
                                  "cash_available,financing,goal,areas,timeline,tier,score") if conv_ids else [],
    )
    contact = got["contact"]
    tracks, tracking_ready = got["tracks"]
    if not convs and not any(contact.values()):
        raise HTTPException(404, "Customer not found.")
    (messages, wants), inquiries, visits = got["messages"], got["inquiries"], got["visits"]
    investor, offers = got["investor"]
    applications = got["applications"]
    for a in applications:
        details = a.pop("details", None)
        a["kind"] = "purchase" if isinstance(details, dict) and details.get("kind") == "purchase" else "rental"
        a["text"] = (profile.PURCHASE_TEXT if a["kind"] == "purchase" else profile.APPLICATION_TEXT).get(a.get("status"), a.get("status"))
    # Purchase offers the team prepared for an investor (Purchase offers tab).
    for o in offers:
        applications.append({"id": o.get("id"), "property_title": o.get("property_title") or o.get("reference") or "Home",
                             "kind": "offer", "status": o.get("status"),
                             "text": profile.OFFER_TEXT.get(o.get("status"), o.get("status"))})
    best = max(convs, key=lambda c: (TIER_RANK.get(c.get("lead_status") or "", -1), c.get("intent_score") or 0)) if convs else None
    brief = got["profiles"][0] if got["profiles"] else None

    return {
        "customer": card(session_id, convs, contact, tracks.get(session_id)) if convs else
                    {"session_id": session_id, **contact, "status": (tracks.get(session_id) or {}).get("status") or "new"},
        "conversations": [{k: c.get(k) for k in ("id", "listing_title", "summary", "lead_status", "intent_score",
                                                  "role", "intent", "updated_at", "human_verification", "outcome")}
                          for c in convs],
        "messages": messages,
        "inquiries": inquiries,
        "visits": visits,
        "applications": applications,
        "investor": investor,
        "investor_brief": brief,
        "score_reasons": profile.score_reasons(best) if best else [],
        "wants": wants,
        "tracking_ready": tracking_ready,
        "setup_hint": None if tracking_ready else SETUP_HINT,
    }


# ---------------------------------------------------------------------
# Best deals for this customer (the pop-up's "Best deals" section)
# ---------------------------------------------------------------------
# Investors: the same matching and deal maths as the Investing -> Investors
# page (investors.match_listings) - top MLS / investor-listed homes for their
# cash, areas and bedrooms, with cash needed and monthly cash flow.
# Families buying a home to live in: homes for sale within their budget, in
# their area, with enough bedrooms - no investment numbers.

DEAL_FIELDS = ("list_number", "street_address", "city", "postal_code", "bedrooms", "bathrooms_full",
               "living_area", "year_built", "list_price", "photo_url", "owner_listed")


def _home(row: dict) -> dict:
    return {k: row.get(k) for k in DEAL_FIELDS}


def _similar_to_asked(session_id: str, limit: int) -> Optional[dict]:
    """Homes like the for-sale home(s) this customer asked about - similar price
    (+/-15%), same town first, about as many bedrooms. Used when the AI doesn't
    know their budget or cash yet. None if they never asked about a sale home."""
    asked = fetch_in("property_inquiries", "session_id", [session_id], "property_id,property_title,created_at",
                     {"order": "created_at.desc"})
    ids = [a["property_id"] for a in asked if a.get("property_id")]
    if not ids:
        return None
    refs = {r["list_number"]: {"city": r.get("city"), "price": r.get("list_price"), "bedrooms": r.get("bedrooms")}
            for r in fetch_in("mls_listings", "list_number", ids, "list_number,city,list_price,bedrooms")}
    for p in fetch_in("properties", "id", ids, "id,area,location,sale_price,bedrooms,listing_type"):
        if p.get("listing_type") == "sale" and p.get("sale_price"):
            refs[p["id"]] = {"city": p.get("area") or p.get("location"), "price": p.get("sale_price"), "bedrooms": p.get("bedrooms")}
    ref_id = next((i for i in ids if i in refs and refs[i].get("price")), None)
    if not ref_id:
        return None
    ref = refs[ref_id]
    price = float(ref["price"])
    lo, hi = price * 0.85, price * 1.15
    try:
        rows = db._get("mls_listings", {"select": ",".join(DEAL_FIELDS[:-1]), "status_label": "eq.active",
                                        "list_price": f"lte.{int(hi)}", "order": "list_price.desc", "limit": "300"}) or []
    except requests.RequestException:
        rows = []
    rows = [r for r in list(rows) + db.owner_sale_listings(max_price=hi)
            if float(r.get("list_price") or 0) >= lo and str(r.get("list_number")) not in ids]
    if ref.get("bedrooms"):
        rows = [r for r in rows if (r.get("bedrooms") or 0) >= int(ref["bedrooms"]) - 1]
    town = str(ref.get("city") or "").lower()
    rows.sort(key=lambda r: (town and town in str(r.get("city") or "").lower(), -abs(float(r.get("list_price") or 0) - price)),
              reverse=True)
    title = next((a.get("property_title") for a in asked if a.get("property_id") == ref_id), None)
    return {"kind": "similar", "reference": {"title": title, "price": price, "city": ref.get("city"), "bedrooms": ref.get("bedrooms")},
            "area": ref.get("city"), "budget": hi, "bedrooms": ref.get("bedrooms"),
            "matches": [_home(r) for r in rows[:limit]]}


@router.get("/{session_id}/deals")
def customer_deals(session_id: str, limit: int = 5):
    """Top homes for a buyer / investor, from what they told the AI."""
    configured()
    from src.services import investors, properties   # local imports - both pull in a lot
    limit = max(1, min(limit, 10))
    convs = db._get("conversations", {"session_id": f"eq.{session_id}", "select": "id,role,updated_at",
                                      "limit": "50"}) or []
    convs.sort(key=lambda c: str(c.get("updated_at") or ""), reverse=True)
    conv_ids = [c["id"] for c in convs]
    role = first(*(c.get("role") for c in convs))

    # Investor: their brief from the chat (investor_profiles), newest with cash first.
    briefs = fetch_in("investor_profiles", "conversation_id", conv_ids, "*") if conv_ids else []
    briefs.sort(key=lambda b: (bool(b.get("cash_available")), str(b.get("updated_at") or "")), reverse=True)
    brief = briefs[0] if briefs else None
    if brief and brief.get("cash_available"):
        try:
            found = investors.match_listings(brief, limit=limit, exclude_session_id=None)
        except Exception as e:
            raise HTTPException(502, f"Couldn't work out the deals: {str(e)[:200]}")
        return {
            "kind": "investor",
            "profile_id": brief["id"],
            "can_send": bool(brief.get("whatsapp")),
            "note": found.get("alternative_note"),
            "matches": [{**_home(m["listing"]), "cash_needed": m.get("cash_needed"),
                         "cash_flow_monthly": m.get("cash_flow_monthly"), "cap_rate_percent": m.get("cap_rate_percent"),
                         "rent_source": m.get("rent_source")} for m in found.get("matches") or []],
        }
    if role == "investor" or brief:
        return _similar_to_asked(session_id, limit) or {"kind": "investor", "needs": "cash", "matches": []}

    # Family buyer: what they want, from the requirements the AI pulled out of the chat.
    raw = []
    if conv_ids:
        try:
            raw = db.list_messages(conv_ids[0]) or []
        except requests.RequestException:
            raw = []
    req = profile.requirements_from(raw)
    looking = properties.listing_type_from(req.get("rent_or_buy"))
    budget = properties.parse_money(req.get("budget") or req.get("price"))
    if looking != "sale" or not budget:
        # No buying budget from the chat: if they asked about a home for sale,
        # show homes like that one instead.
        similar = _similar_to_asked(session_id, limit)
        if similar:
            return similar
    if looking == "rent":
        return {"kind": "renter", "matches": []}
    if looking != "sale":
        return {"kind": "unknown", "matches": []}
    if not budget:
        return {"kind": "buyer", "needs": "budget", "matches": []}
    beds = properties.parse_int(req.get("bedrooms"))
    area = str(req.get("location") or "").strip()

    params = {"select": ",".join(DEAL_FIELDS[:-1]), "status_label": "eq.active",
              "list_price": f"lte.{int(budget)}", "order": "list_price.desc", "limit": "200"}
    try:
        rows = db._get("mls_listings", params) or []
    except requests.RequestException as e:
        print(f"Customers: MLS search failed (non-fatal): {e}")
        rows = []
    rows = list(rows) + db.owner_sale_listings(max_price=budget)
    if beds:
        rows = [r for r in rows if (r.get("bedrooms") or 0) >= beds]
    in_area = [r for r in rows if area and area.lower() in str(r.get("city") or "").lower()]
    picked = (in_area or rows)
    # Closest to their budget first: that's usually the most home for the money.
    picked.sort(key=lambda r: float(r.get("list_price") or 0), reverse=True)
    return {"kind": "buyer", "budget": budget, "area": area or None, "bedrooms": beds,
            "other_areas": bool(area) and not in_area and bool(rows),
            "matches": [_home(r) for r in picked[:limit]]}


class FollowUpIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["new", "contacted", "follow_up", "won", "lost"]] = None
    notes: Optional[str] = Field(default=None, max_length=4000)
    follow_up_at: Optional[datetime] = None
    clear_follow_up: bool = False


def staff_actor(request: Request, x_staybot_staff: Optional[str]) -> str:
    if (x_staybot_staff or "").strip():
        return f"staff:{x_staybot_staff.strip()[:120]}"
    from src.services import accounts  # local import
    acct = accounts.current_account(request)
    return f"admin:{acct.get('email')}" if acct else "team"


@router.patch("/{session_id}")
def update_customer(session_id: str, body: FollowUpIn, request: Request,
                    x_staybot_staff: Optional[str] = Header(default=None)):
    """Save the staff member's status / notes / follow-up date for this customer."""
    configured()
    fields: dict = {"updated_by": staff_actor(request, x_staybot_staff)}
    if body.status is not None:
        fields["status"] = body.status
    if body.notes is not None:
        fields["notes"] = body.notes.strip() or None
    if body.clear_follow_up:
        fields["follow_up_at"] = None
    elif body.follow_up_at is not None:
        when = body.follow_up_at if body.follow_up_at.tzinfo else body.follow_up_at.replace(tzinfo=timezone.utc)
        fields["follow_up_at"] = when.isoformat()
    try:
        existing = (db._get(FOLLOWUP_TABLE, {"session_id": f"eq.{session_id}", "select": "*", "limit": "1"}) or [None])[0]
        if fields.get("status") in ("contacted", "follow_up", "won", "lost") and not (existing or {}).get("contacted_at"):
            fields["contacted_at"] = now_iso()
        if existing:
            saved = db._patch(FOLLOWUP_TABLE, fields, {"session_id": f"eq.{session_id}"})
        else:
            saved = db._post(FOLLOWUP_TABLE, {"session_id": session_id, **fields})
    except requests.RequestException as e:
        text = getattr(getattr(e, "response", None), "text", "") or str(e)
        if "PGRST205" in text or "does not exist" in text or "Could not find the table" in text:
            raise HTTPException(503, SETUP_HINT)
        raise HTTPException(502, f"Could not save: {text[:200]}")
    return saved or {"session_id": session_id, **fields}
