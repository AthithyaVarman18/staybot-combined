"""
A tenant buying the home they rent ("Own this house").

  Tenant  /me/rentals/home/buy-request        "Own this house" on their home card (Homes tab)
  Owner   /me/owner/buy-requests/{id}/...     accept / decline on the current tenant's card (Tenants tab)
  Team    /applications/buy-requests/{id}/... buyer + property checks (Applications tab, staff-only)
  Buyer   /me/home-purchase/...               after the purchase: live there or rent it out? (Portfolio tab)

Steps (home_buy_requests.status, supabase_home_buy_requests.sql):
  requested     -> the owner is asked (Tenants tab badge + a message in their Messages thread)
  owner_declined-> the tenant sees "Offer declined" with the owner's note; the button stays
                   disabled for this tenancy (one request per rental application)
  negotiating   -> the owner agreed. The tenant gets an investor profile on the new-investor
                   journey (at "Offer preparation" - the property is already known) and the
                   price is negotiated with the existing owner <-> tenant purchase offers
                   (tenant_offers on their rental application, rentals.py)
  sale_agreed   -> either side accepted the other's offer. The buyer check is already done - they
                   were screened when they became a tenant. The property check is the same
                   inspection flow as an investor buying a home: the purchase gets an
                   acquisition_offers row (PO-...), so the team books the inspector, uploads the
                   report and records findings / repairs in the Inspections tab, then marks the
                   property check passed (only once a report is in) or failed
  completed     -> the property check passed: the home is bought. The tenancy ends, the home leaves
                   the seller's portfolio and is added to the buyer's as "owned", the listing
                   moves to the buyer, and their account becomes an Existing Property Investor.
                   They then answer: live there (home stays unlisted) or rent it out (may we
                   list it? would you like rental recommendations for yourself?)
  cancelled     -> the property check failed, or the tenancy ended first - nothing changes hands

An owner sending a purchase offer on their own also counts as agreeing to sell, so offers
made before (or without) a request still end in a completed purchase.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.services import accounts, db, owner_listings, properties, rentals

tenant_router = APIRouter(prefix="/me/rentals/home", tags=["Home purchase - tenant"])
owner_router = APIRouter(prefix="/me/owner/buy-requests", tags=["Home purchase - owner"])
team_router = APIRouter(prefix="/applications/buy-requests", tags=["Home purchase - team"])
buyer_router = APIRouter(prefix="/me/home-purchase", tags=["Home purchase - new owner"])

OPEN = ("requested", "negotiating", "sale_agreed")
STATUS_TEXT = {
    "requested": "Request sent - waiting for the owner",
    "owner_declined": "Offer declined by the owner",
    "negotiating": "The owner agreed to sell - agree the price and terms",
    "sale_agreed": "Price agreed - the home is being inspected",
    "completed": "Bought - this home is yours",
    "cancelled": "Purchase cancelled",
}
PRIVATE = ("owner_session_id",)


def one(table: str, params: dict) -> Optional[dict]:
    return rentals.one(table, params)


def db_error(e: Exception):
    text = getattr(getattr(e, "response", None), "text", "") or str(e)
    if "home_buy_requests" in text or "PGRST205" in text:
        raise HTTPException(503, "Buying a rented home needs supabase_home_buy_requests.sql run in Supabase.")
    raise e


def view(req: Optional[dict]) -> Optional[dict]:
    if not req:
        return None
    out = {k: v for k, v in req.items() if k not in PRIVATE}
    out["status_text"] = STATUS_TEXT.get(req.get("status"), req.get("status"))
    out["needs_plan"] = req.get("status") == "completed" and not req.get("plan")
    if req.get("status") in ("sale_agreed", "completed") and not req.get("buyer_check"):
        out["buyer_check"], out["buyer_check_note"] = "passed", BUYER_CHECK_NOTE  # screened when they became a tenant
    if req.get("acquisition_offer_id") and req.get("status") in ("sale_agreed", "completed", "cancelled"):
        out["inspection"] = inspection_status(req["acquisition_offer_id"])
    return out


BUYER_CHECK_NOTE = "Screened when they became a tenant (tenant screening and onboarding)."


def inspection_status(acquisition_offer_id: str) -> dict:
    """Where the property inspection is (Inspections tab, inspections.py):
    the latest booking, how many reports are in, and their findings.
    Never raises."""
    out = {"offer_reference": None, "booking": None, "reports": 0, "major": 0, "minor": 0, "findings": []}
    try:
        offer = one("acquisition_offers", {"id": f"eq.{acquisition_offer_id}"})
        out["offer_reference"] = (offer or {}).get("reference")
        bookings = db._get("inspection_bookings", {"offer_id": f"eq.{acquisition_offer_id}", "order": "created_at.desc", "select": "*"}) or []
        live = [b for b in bookings if b.get("status") != "cancelled"]
        if live:
            b = sorted(live, key=lambda r: str(r.get("created_at") or ""), reverse=True)[0]
            inspector = one("inspectors", {"id": f"eq.{b['inspector_id']}"}) if b.get("inspector_id") else None
            chosen = None
            if b.get("confirmed_date"):
                chosen = next((x.get("label") for x in b.get("proposed_slots") or []
                               if x.get("date") == str(b["confirmed_date"]) and x.get("time") == str(b.get("confirmed_time") or "")[:5]), None)
                chosen = chosen or f"{b['confirmed_date']} {str(b.get('confirmed_time') or '')[:5]}".strip()
            out["booking"] = {"status": b.get("status"), "when": chosen, "inspector": (inspector or {}).get("name")}
        reports = db._get("inspection_reports", {"offer_id": f"eq.{acquisition_offer_id}", "select": "id"}) or []
        out["reports"] = len(reports)
        if reports:
            findings = db._get("inspection_findings", {"report_id": f"in.({','.join(r['id'] for r in reports)})", "select": "severity,summary"}) or []
            out["major"] = sum(1 for f in findings if f.get("severity") == "major")
            out["minor"] = sum(1 for f in findings if f.get("severity") == "minor")
            out["findings"] = [{"severity": f.get("severity"), "summary": f.get("summary")}
                               for f in sorted(findings, key=lambda f: f.get("severity") != "major")]
    except Exception as e:
        print(f"Inspection status lookup failed (non-fatal): {e}")
    return out


def open_property_check(req: dict, app: dict, offer: dict, investor_id: Optional[str], actor: str) -> Optional[str]:
    """The agreed purchase as a purchase-offer record (acquisition_offers),
    so the property check runs through the same Inspections workspace as an
    investor buying a home. Never raises."""
    if req.get("acquisition_offer_id"):
        return req["acquisition_offer_id"]
    try:
        # Once per purchase: the record is tagged with the request id, so a
        # retry (or a save of acquisition_offer_id that failed) reuses it.
        marker = f"home_purchase:{req['id']}"
        existing = db._get("acquisition_offers", {"created_by": f"eq.{marker}", "select": "id", "limit": "1"}) or []
        if not existing:  # made before the tag existed
            existing = db._get("acquisition_offers", {"created_by": "eq.system", "property_id": f"eq.{app['property_id']}",
                                                      "investor_name": f"eq.{app.get('tenant_name') or ''}",
                                                      "order": "created_at.asc", "select": "id", "limit": "1"}) or []
        if existing:
            return existing[0]["id"]
        import secrets
        from datetime import datetime, timezone
        from src.services import purchase_offers  # local import
        prop = one("properties", {"id": f"eq.{app['property_id']}"}) or {}
        tenant = one("accounts", {"id": f"eq.{app['tenant_account_id']}"}) or {}
        terms = offer.get("terms") or {}
        row = db._post("acquisition_offers", {
            "reference": f"PO-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3).upper()}",
            "property_id": app["property_id"], "property_ref": prop.get("ref"),
            "property_title": prop.get("title") or app.get("property_title"),
            "investor_id": investor_id, "investor_name": app.get("tenant_name") or tenant.get("name") or "Tenant buyer",
            "investor_email": app.get("tenant_email") or tenant.get("email"), "investor_phone": app.get("tenant_phone"),
            # Price already agreed between tenant and owner (tenant_offers) - no
            # approval link or broker negotiation to run, only the inspection.
            "status": "accepted", "current_version": 1, "state_version": 1, "created_by": marker,
        })
        db._post("acquisition_offer_versions", {"offer_id": row["id"], "version_number": 1, "terms": terms,
                                                "content_hash": purchase_offers.content_hash(terms), "created_by": actor})
        return row["id"]
    except Exception as e:
        print(f"Opening the property inspection for the tenant purchase failed (non-fatal, is "
              f"supabase_acquisition_workflows.sql run?): {e}")
        return None


def for_application(app_id: str) -> Optional[dict]:
    """The buy request on this rental application, or None. Never raises -
    the Homes / Tenants tabs call it before the table may exist."""
    try:
        req = one("home_buy_requests", {"application_id": f"eq.{app_id}"})
        return with_inspection_file(req) if req else None
    except Exception as e:
        print(f"Buy request lookup failed (non-fatal, is supabase_home_buy_requests.sql run?): {e}")
        return None


def for_applications(app_ids: list[str]) -> dict[str, dict]:
    if not app_ids:
        return {}
    try:
        rows = db._get("home_buy_requests", {"application_id": f"in.({','.join(app_ids)})", "select": "*"}) or []
    except Exception as e:
        print(f"Buy request lookup failed (non-fatal, is supabase_home_buy_requests.sql run?): {e}")
        return {}
    return {r["application_id"]: view(with_inspection_file(r)) for r in rows}


def with_inspection_file(req: dict) -> dict:
    """A purchase agreed before the property check used the Inspections
    workspace has no inspection file yet - open it now. Never raises."""
    if req.get("status") != "sale_agreed" or req.get("acquisition_offer_id") or not req.get("agreed_offer_id"):
        return req
    try:
        app = one("rental_applications", {"id": f"eq.{req['application_id']}"})
        offer = one("tenant_offers", {"id": f"eq.{req['agreed_offer_id']}"})
        if not app or not offer:
            return req
        acquisition_offer_id = open_property_check(req, app, offer, req.get("investor_id"), "system")
        if not acquisition_offer_id:
            return req
        fields = {"acquisition_offer_id": acquisition_offer_id,
                  "buyer_check": req.get("buyer_check") or "passed", "buyer_check_note": req.get("buyer_check_note") or BUYER_CHECK_NOTE}
        return update(req, fields)
    except Exception as e:
        print(f"Opening the inspection file failed (non-fatal): {e}")
        return req


def get_request(req_id: str) -> dict:
    try:
        req = one("home_buy_requests", {"id": f"eq.{req_id}"})
    except Exception as e:
        db_error(e)
    if not req:
        raise HTTPException(404, "Request not found.")
    return req


def update(req: dict, fields: dict, expect_status: Optional[str] = None) -> dict:
    params = {"id": f"eq.{req['id']}"}
    if expect_status:
        params["status"] = f"eq.{expect_status}"
    saved = db._patch("home_buy_requests", fields, params)
    if not saved:
        raise HTTPException(409, "This request changed in the meantime. Refresh.")
    return saved


def post_chat_message(app: dict, side: str, text: str):
    """Drop a note into the tenant <-> owner Messages thread for this home,
    so the other side gets it with the usual unread badge. Never raises."""
    try:
        from src.services import rental_chat  # local import - rental_chat imports rentals
        prop = one("properties", {"id": f"eq.{app['property_id']}"}) or {"id": app["property_id"], "title": app.get("property_title")}
        tenant = one("accounts", {"id": f"eq.{app['tenant_account_id']}"})
        owner = one("accounts", {"session_id": f"eq.{app['owner_session_id']}"})
        if not tenant or not owner:
            return
        thread = rental_chat.open_or_create(prop, tenant, owner)
        sender = tenant if side == "tenant" else owner
        msg = db._post("rental_messages", {"thread_id": thread["id"], "sender": side,
                                           "sender_account_id": sender["id"], "body": text})
        now = (msg or {}).get("created_at") or rental_chat.now_precise()
        db._patch("rental_threads", {"last_message_at": now, "last_message_preview": text[:140],
                                     "last_sender": side, f"{side}_last_read_at": now}, {"id": f"eq.{thread['id']}"})
    except Exception as e:
        print(f"Posting the buy request to Messages failed (non-fatal): {e}")


# ---------------------------------------------------------------------
# Investor profile for the buying tenant
# ---------------------------------------------------------------------

def ensure_investor(req: dict, actor: str) -> Optional[dict]:
    """The tenant is buying now: give them an investor profile on the
    new-investor journey, straight at "Offer preparation" (the property is
    already chosen), linked to their account. Their account stays a tenant
    account until the purchase completes - they still live in the home.
    Never raises."""
    from src.services import investor_journey  # local import - investor_journey imports a lot
    try:
        account = one("accounts", {"id": f"eq.{req['tenant_account_id']}"})
        if not account:
            return None
        investor = rentals.buyer_investor(account["id"])
        note = f"Tenant buying the home they rent: {req.get('property_title') or req['property_id']}."
        if not investor:
            keys = [k for k, _, _ in investor_journey.NEW_INVESTOR_STAGES]
            investor = db._post("investors", {
                "name": account.get("name") or "Tenant buyer", "session_id": account["session_id"],
                "investor_type": "new", "journey": investor_journey.JOURNEYS["new"]["key"],
                "stage": "offer_preparation", "stage_index": keys.index("offer_preparation"),
                "existing_property_count": 0,
            })
            db._post("investor_stage_history", {"investor_id": investor["id"], "from_stage": None,
                                                "to_stage": "offer_preparation", "actor": actor, "note": note})
            db._post("investor_activity", {"investor_id": investor["id"], "actor": actor, "action": "profile_started",
                                           "details": {"via": "tenant home purchase", "property_id": req["property_id"]}})
        elif not investor.get("journey"):
            # A profile the chat started but never classified: without a
            # journey no stage move (or the graduation at completion) applies.
            keys = [k for k, _, _ in investor_journey.NEW_INVESTOR_STAGES]
            investor = db._patch("investors", {
                "investor_type": "new", "journey": investor_journey.JOURNEYS["new"]["key"],
                "stage": "offer_preparation", "stage_index": keys.index("offer_preparation"),
                "existing_property_count": investor.get("existing_property_count") or 0,
            }, {"id": f"eq.{investor['id']}"}) or investor
            db._post("investor_stage_history", {"investor_id": investor["id"], "from_stage": None,
                                                "to_stage": "offer_preparation", "actor": actor, "note": note})
        elif investor.get("journey") == investor_journey.JOURNEYS["new"]["key"]:
            # Skip the search stages - but never move someone backwards.
            keys = [k for k, _, _ in investor_journey.NEW_INVESTOR_STAGES]
            current = keys.index(investor["stage"]) if investor.get("stage") in keys else -1
            if current < keys.index("offer_preparation"):
                investor = investor_journey.advance(investor, actor, note, target_stage="offer_preparation")
        if not account.get("investor_id"):
            db._patch("accounts", {"investor_id": investor["id"]}, {"id": f"eq.{account['id']}"})
        return investor
    except Exception as e:
        print(f"Creating the buying tenant's investor profile failed (non-fatal): {e}")
        return None


def move_investor(investor_id: Optional[str], stage: str, actor: str, note: str):
    """Move the buyer's investor profile to a stage of whichever journey it's on. Never raises."""
    if not investor_id:
        return
    from src.services import investor_journey  # local import
    try:
        investor = one("investors", {"id": f"eq.{investor_id}"})
        if investor and stage in [k for k, _, _ in investor_journey.stages_for(investor.get("journey")) or []]:
            investor_journey.advance(investor, actor, note, target_stage=stage)
    except Exception as e:
        print(f"Moving the buyer's investor stage failed (non-fatal): {e}")


# ---------------------------------------------------------------------
# Tenant: "Own this house"
# ---------------------------------------------------------------------

class BuyRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(default="", max_length=1000)


@tenant_router.post("/buy-request")
def request_to_buy(body: BuyRequestIn, request: Request):
    account = rentals.require_role(request, "tenant")
    home = rentals.tenant_home(account["id"])
    if not home:
        raise HTTPException(403, "You can ask to buy a home once you rent it through Staybot.")
    app = rentals.get_application(home["application_id"])
    d = app.get("details") if isinstance(app.get("details"), dict) else {}
    if d.get("sale_approved_at") or d.get("sale_agreed"):
        raise HTTPException(409, "The sale of this home to you is already agreed.")
    if not app.get("owner_session_id"):
        raise HTTPException(409, "This home is managed by our team, not an owner on Staybot - ask us about buying it in the chat.")
    if for_application(app["id"]):
        raise HTTPException(409, "You've already asked the owner about buying this home.")
    message = body.message.strip()
    try:
        req = db._post("home_buy_requests", {
            "application_id": app["id"], "property_id": app["property_id"], "property_title": app.get("property_title"),
            "tenant_account_id": account["id"], "owner_session_id": app["owner_session_id"],
            "tenant_message": message or None, "status": "requested",
        })
    except Exception as e:
        text = getattr(getattr(e, "response", None), "text", "") or str(e)
        if "duplicate" in text or "23505" in text:
            raise HTTPException(409, "You've already asked the owner about buying this home.")
        db_error(e)
    post_chat_message(app, "tenant", "🏠 I'd like to buy this home from you. You can accept or decline my request "
                                     "on your Tenants tab." + (f"\n\n{message}" if message else ""))
    return view(req)


# ---------------------------------------------------------------------
# Owner: accept or decline
# ---------------------------------------------------------------------

class OwnerDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accept", "decline"]
    note: str = Field(default="", max_length=1000)


@owner_router.post("/{req_id}/decision")
def owner_decision(req_id: str, body: OwnerDecisionIn, request: Request):
    account = rentals.require_role(request, *owner_listings.INVESTOR_ROLES)
    req = get_request(req_id)
    if req["owner_session_id"] != account["session_id"]:
        raise HTTPException(404, "Request not found.")
    if req["status"] != "requested":
        raise HTTPException(409, "This request isn't waiting for your answer. Refresh.")
    app = rentals.get_application(req["application_id"])
    note = body.note.strip()
    fields = {"owner_note": note or None, "owner_decided_at": rentals.now_iso()}
    if body.decision == "decline":
        saved = update(req, {**fields, "status": "owner_declined"}, expect_status="requested")
        post_chat_message(app, "owner", "I've declined your request to buy the home." + (f"\n\n{note}" if note else ""))
        return view(saved)
    saved = accept(req, fields, actor=f"owner:{account.get('email') or account['id']}")
    post_chat_message(app, "owner", "I'm happy to sell you the home - I'll send you a purchase offer, which you can "
                                    "accept or modify on your Homes tab." + (f"\n\n{note}" if note else ""))
    return view(saved)


def accept(req: dict, fields: dict, actor: str) -> dict:
    saved = update(req, {**fields, "status": "negotiating"}, expect_status=req["status"])
    investor = ensure_investor(req, actor)
    if investor:
        saved = update(saved, {"investor_id": investor["id"]})
    return saved


def on_owner_offer(app: dict, account: dict):
    """rentals.owner_send_offer: an owner who sends their tenant a purchase
    offer has agreed to sell - a request still waiting is accepted. Never raises."""
    if rentals.is_purchase(app):
        return
    try:
        req = for_application(app["id"])
        if req and req["status"] == "requested":
            accept(req, {"owner_decided_at": rentals.now_iso()}, actor=f"owner:{account.get('email') or account['id']}")
    except Exception as e:
        print(f"Accepting the buy request with the offer failed (non-fatal): {e}")


def on_offer_accepted(app: dict, offer: dict, actor: str):
    """rentals.respond_to_offer / owner_respond_to_counter: one side accepted
    the other's purchase offer on a rental application - the sale is agreed
    and goes to our team for the checks. Offers the owner made without a
    request still count (the owner offering is their agreement). Never raises."""
    if rentals.is_purchase(app) or app.get("status") != "approved" or not app.get("owner_session_id"):
        return
    try:
        price = rentals._price((offer.get("terms") or {}).get("price"))
        fields = {"status": "sale_agreed", "agreed_offer_id": offer["id"], "agreed_price": price,
                  "buyer_check": "passed", "buyer_check_note": BUYER_CHECK_NOTE,
                  "property_check": None, "property_check_note": None}
        req = for_application(app["id"])
        if not req:
            req = db._post("home_buy_requests", {
                "application_id": app["id"], "property_id": app["property_id"], "property_title": app.get("property_title"),
                "tenant_account_id": app["tenant_account_id"], "owner_session_id": app["owner_session_id"],
                "status": "negotiating", "owner_decided_at": rentals.now_iso(),
            })
        if req["status"] not in ("requested", "negotiating", "owner_declined"):
            return
        if not req.get("investor_id"):
            investor = ensure_investor(req, actor)
            if investor:
                fields["investor_id"] = investor["id"]
        acquisition_offer_id = open_property_check(req, app, offer, fields.get("investor_id") or req.get("investor_id"), actor)
        if acquisition_offer_id:
            fields["acquisition_offer_id"] = acquisition_offer_id
        saved = update(req, fields)
        move_investor(saved.get("investor_id"), "property_inspection", actor,
                      f"Price agreed with the owner{f' at ${price:,.0f}' if price else ''}; property inspection next.")
    except Exception as e:
        print(f"Recording the agreed home sale failed (non-fatal): {e}")


def on_tenancy_ended(app: dict):
    """rentals.owner_end_tenancy: an open request can't go on. Never raises."""
    try:
        req = for_application(app["id"])
        if req and req["status"] in OPEN:
            update(req, {"status": "cancelled", "owner_note": req.get("owner_note") or "The tenancy ended before the purchase completed."})
    except Exception as e:
        print(f"Cancelling the buy request failed (non-fatal): {e}")


# ---------------------------------------------------------------------
# Team: buyer checks + property checks, then the purchase completes
# ---------------------------------------------------------------------

class CheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The buyer check is done at tenant onboarding - only the property check is left.
    check: Literal["property"] = "property"
    result: Literal["passed", "failed"]
    note: str = Field(default="", max_length=1000)


@team_router.post("/{req_id}/checks")
def record_check(req_id: str, body: CheckIn, request: Request, x_staybot_staff: Optional[str] = Header(default=None)):
    rentals.configured()
    actor = rentals.team_actor(request, x_staybot_staff)
    req = with_inspection_file(get_request(req_id))
    if req["status"] != "sale_agreed":
        raise HTTPException(409, "The property check runs once the price is agreed. Refresh.")
    if body.result == "passed":
        if not req.get("acquisition_offer_id"):
            raise HTTPException(409, "This purchase has no inspection file yet - run supabase_acquisition_workflows.sql "
                                     "and supabase_home_buy_requests.sql, then have the tenant and owner agree the offer again.")
        inspection = inspection_status(req["acquisition_offer_id"])
        if not inspection["reports"]:
            raise HTTPException(409, "Upload the inspection report first: Inspections tab → Upload inspection report → "
                                     f"offer {inspection['offer_reference'] or 'for this home'}.")
    fields = {"property_check": body.result, "property_check_note": body.note.strip() or None, "checked_by": actor,
              "buyer_check": req.get("buyer_check") or "passed", "buyer_check_note": req.get("buyer_check_note") or BUYER_CHECK_NOTE}
    if body.result == "failed":
        fields["status"] = "cancelled"
    saved = update(req, fields, expect_status="sale_agreed")
    if body.result == "failed":
        move_investor(saved.get("investor_id"), "offer_preparation", actor, "Purchase cancelled: the property inspection didn't pass.")
        mark_offer(saved, "rejected")
        return view(saved)
    saved = complete_purchase(saved, actor)
    return view(saved)


def mark_offer(req: dict, status: str):
    """Keep the purchase-offer record in step (it's listed on the Purchase
    offers / Inspections pages). Never raises."""
    if not req.get("acquisition_offer_id"):
        return
    try:
        db._patch("acquisition_offers", {"status": status}, {"id": f"eq.{req['acquisition_offer_id']}"})
    except Exception as e:
        print(f"Updating the purchase-offer record failed (non-fatal): {e}")


def complete_purchase(req: dict, actor: str) -> dict:
    """Both checks passed - the tenant owns the home now."""
    app = rentals.get_application(req["application_id"])
    prop = one("properties", {"id": f"eq.{req['property_id']}"}) or {}
    buyer = one("accounts", {"id": f"eq.{req['tenant_account_id']}"})
    if not buyer:
        raise HTTPException(409, "The buyer's account no longer exists.")

    # 1. They stop being the tenant of this home.
    db._patch("rental_applications", {"status": "ended", "ended_at": rentals.now_iso(),
                                      "team_note": "The tenant bought this home."}, {"id": f"eq.{app['id']}"})

    # 2. The listing moves to the buyer, off the market until they say what they want.
    try:
        db.update_property(req["property_id"], {"status": "sold", "session_id": buyer["session_id"]})
        properties.clear_cache()
    except Exception as e:
        print(f"Moving the listing to the buyer failed (non-fatal): {e}")

    # 3. Out of the seller's portfolio.
    remove_from_seller_portfolio(req, actor)

    # 4. Into the buyer's, and they're an existing investor from here on.
    investor = rentals.buyer_investor(buyer["id"]) or ensure_investor(req, actor)
    if investor:
        add_to_portfolio(investor, req, app, prop)
        move_investor(investor["id"], "closing", actor, "Property inspection passed - purchase completed.")
        try:
            from src.services import investor_journey  # local import
            investor_journey.graduate_to_existing_investor(investor["id"], actor)
        except Exception as e:
            print(f"Graduating the buyer to existing investor failed (non-fatal): {e}")
    db._patch("accounts", {"role": "existing_investor", **({"investor_id": investor["id"]} if investor else {})},
              {"id": f"eq.{buyer['id']}"})
    update_buyer_profile(buyer, investor, req, app, prop)

    fields = {"status": "completed", "completed_at": rentals.now_iso()}
    if investor:
        fields["investor_id"] = investor["id"]
    return update(req, fields)


def update_buyer_profile(buyer: dict, investor: Optional[dict], req: dict, app: dict, prop: dict):
    """Their Portfolio tab profile and investor record now describe an
    Existing Property Investor who owns this home. Never raises."""
    from src.services import portfolio  # local import
    owned = 1
    try:
        if investor:
            rows = db._get("investor_portfolio_properties", {"investor_id": f"eq.{investor['id']}",
                                                             "relationship": "eq.owned", "select": "id"}) or []
            owned = max(len(rows), 1)
            db._patch("investors", {"existing_property_count": owned,
                                    **({"email": buyer["email"]} if buyer.get("email") and not investor.get("email") else {})},
                      {"id": f"eq.{investor['id']}"})
    except Exception as e:
        print(f"Updating the buyer's investor record failed (non-fatal): {e}")
    portfolio.became_existing_investor(buyer["id"], owned_count=owned,
                                       home=rentals._portfolio_address(prop, app) or req.get("property_title"))


def remove_from_seller_portfolio(req: dict, actor: str):
    """The seller doesn't own it any more. Never raises."""
    try:
        seller = one("accounts", {"session_id": f"eq.{req['owner_session_id']}"})
        investor = rentals.buyer_investor(seller["id"]) if seller else None
        if not investor:
            return
        rows = db._get("investor_portfolio_properties", {"investor_id": f"eq.{investor['id']}",
                                                         "property_id": f"eq.{req['property_id']}", "select": "id,address"}) or []
        for r in rows:
            db._delete("investor_portfolio_properties", {"id": f"eq.{r['id']}"})
        db._post("investor_activity", {"investor_id": investor["id"], "actor": actor, "action": "portfolio_property_sold",
                                       "details": {"property_id": req["property_id"], "title": req.get("property_title"),
                                                   "price": req.get("agreed_price"), "sold_to": "their tenant"}})
    except Exception as e:
        print(f"Removing the home from the seller's portfolio failed (non-fatal): {e}")


def add_to_portfolio(investor: dict, req: dict, app: dict, prop: dict) -> Optional[dict]:
    """The bought home in the buyer's portfolio as "owned". Only facts we
    have - anything unknown stays blank. Once per buyer + home. Never raises."""
    try:
        existing = db._get("investor_portfolio_properties", {"investor_id": f"eq.{investor['id']}",
                                                             "property_id": f"eq.{req['property_id']}", "select": "id", "limit": "1"})
        if existing:
            return db._patch("investor_portfolio_properties", {"relationship": "owned"}, {"id": f"eq.{existing[0]['id']}"})
        price = req.get("agreed_price")
        row = {
            "investor_id": investor["id"], "relationship": "owned",
            "address": rentals._portfolio_address(prop, app),
            "property_type": prop.get("property_type"), "bedrooms": prop.get("bedrooms"), "bathrooms": prop.get("bathrooms"),
            "purchase_price": price, "estimated_value": price, "property_id": req["property_id"],
            "condition_notes": f"Bought through Staybot - you rented this home before buying it.",
        }
        saved = db._post("investor_portfolio_properties", {k: v for k, v in row.items() if v is not None})
        db._post("investor_activity", {"investor_id": investor["id"], "actor": "system", "action": "portfolio_property_added",
                                       "details": {"address": row["address"], "source": "tenant_bought_home"}})
        return saved
    except Exception as e:
        print(f"Adding the bought home to the portfolio failed (non-fatal): {e}")
        return None


# ---------------------------------------------------------------------
# New owner: live there, or rent it out?
# ---------------------------------------------------------------------

def own_requests(account: dict) -> list[dict]:
    try:
        return db._get("home_buy_requests", {"tenant_account_id": f"eq.{account['id']}", "status": "eq.completed",
                                             "order": "completed_at.desc", "select": "*"}) or []
    except Exception as e:
        print(f"Home purchase lookup failed (non-fatal): {e}")
        return []


def own_completed(request: Request, req_id: str) -> tuple[dict, dict]:
    account = rentals.require_role(request, *owner_listings.INVESTOR_ROLES)
    req = get_request(req_id)
    if req["tenant_account_id"] != account["id"] or req["status"] != "completed":
        raise HTTPException(404, "Purchase not found.")
    return account, req


@buyer_router.get("")
def my_home_purchases(request: Request):
    account = rentals.require_role(request, *owner_listings.INVESTOR_ROLES)
    return {"purchases": [view(r) for r in own_requests(account)]}


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["live_in", "rent_out"]
    list_home: Optional[bool] = None
    want_rentals: Optional[bool] = None

    @model_validator(mode="after")
    def check(self):
        if self.plan == "rent_out" and (self.list_home is None or self.want_rentals is None):
            raise ValueError("Tell us whether we can list the home, and whether you'd like rental recommendations.")
        if self.plan == "live_in":
            self.list_home, self.want_rentals = False, False
        return self


@buyer_router.post("/{req_id}/plan")
def set_plan(req_id: str, body: PlanIn, request: Request):
    account, req = own_completed(request, req_id)
    actor = f"account:{account.get('email') or account['id']}"
    investor = rentals.buyer_investor(account["id"])
    listed = None

    if body.plan == "live_in":
        # Their own home: never on the market.
        db.update_property(req["property_id"], {"status": "sold"})
        note_portfolio(investor, req, "You live in this home (owner-occupied).")
        move_investor(investor and investor["id"], "property_management", actor, "Lives in the home they bought.")
    else:
        if body.list_home:
            prop = one("properties", {"id": f"eq.{req['property_id']}"}) or {}
            status = "active" if owner_listings.publishes_immediately(account.get("role")) else "pending"
            db.update_property(req["property_id"], {"status": status, "listing_type": "rent",
                                                    "session_id": account["session_id"],
                                                    "source": prop.get("source") if prop.get("source") in ("owner_chat", "investor_form") else "investor_form"})
            listed = status
        else:
            db.update_property(req["property_id"], {"status": "sold"})
        note_portfolio(investor, req, "Rented out - you don't live here." + (" Listed for tenants on Staybot." if body.list_home else ""))
        move_investor(investor and investor["id"], "tenant_lead_generation", actor,
                      "Renting out the home they bought" + (" - listed on Staybot." if body.list_home else " - not listed on Staybot."))
    properties.clear_cache()

    saved = update(req, {"plan": body.plan, "list_home": body.list_home, "want_rentals": body.want_rentals,
                         "plan_answered_at": rentals.now_iso()})
    return {"purchase": view(saved), "listed": listed,
            "rentals": rental_recommendations(saved) if body.want_rentals else []}


def note_portfolio(investor: Optional[dict], req: dict, note: str):
    if not investor:
        return
    try:
        db._patch("investor_portfolio_properties", {"condition_notes": f"Bought through Staybot - you rented this home before buying it. {note}"},
                  {"investor_id": f"eq.{investor['id']}", "property_id": f"eq.{req['property_id']}"})
    except Exception as e:
        print(f"Updating the portfolio note failed (non-fatal): {e}")


@buyer_router.get("/{req_id}/rentals")
def recommended_rentals(req_id: str, request: Request):
    _, req = own_completed(request, req_id)
    return {"rentals": rental_recommendations(req)}


def rental_recommendations(req: dict, limit: int = 6) -> list[dict]:
    """Homes for rent the new owner could move into: same area first, then
    closest in size and rent to the home they're leaving."""
    home = one("properties", {"id": f"eq.{req['property_id']}"}) or {}
    try:
        rows = db._get("properties", {"status": "eq.active", "listing_type": "eq.rent", "select": "*", "limit": "300"}) or []
    except Exception as e:
        print(f"Rental recommendations failed (non-fatal): {e}")
        return []
    area = str(home.get("area") or "").strip().lower()
    beds = home.get("bedrooms") or 0
    rent = rentals._price(home.get("rent")) or 0

    def score(p):
        same_area = area and str(p.get("area") or "").strip().lower() == area
        rent_gap = abs((rentals._price(p.get("rent")) or rent) - rent) / (rent or 1)
        return (0 if same_area else 1, abs((p.get("bedrooms") or 0) - beds), rent_gap)

    picks = sorted((p for p in rows if p["id"] != req["property_id"]), key=score)[:limit]
    return [{k: p.get(k) for k in ("id", "title", "area", "city", "location", "bedrooms", "bathrooms", "rent", "property_type")}
            | {"price_label": properties.price_label(p)} for p in picks]
