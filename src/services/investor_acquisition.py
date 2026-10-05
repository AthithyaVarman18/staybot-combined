"""Investor self-service view of purchase offers and inspection assistance.

The staff workflow (src/services/purchase_offers.py and inspections.py) stays
staff-only: creating offers, changing terms, broker handoff, inspectors,
bookings, uploads, findings and repairs are all under /purchase-offers and
/inspections, which team_auth.py refuses to customer accounts.

This module is what the "Purchase offers" and "Inspections" tabs in /ui call
for New Property Investor / Existing Property Investor accounts. Every route
is gated by the account's own login (same as /me/investor/alerts) and scoped
to offers whose investor_id is that account's investor row, so an investor
never sees another investor's offer, price, inspection or report.

What an investor can do here:
  - read their own offers: current terms, status, broker handoff state,
    negotiation timeline and active deadlines;
  - open the plain review summary PDF (headed "not a contract");
  - approve the current summary version, or request changes, when staff have
    sent it for approval - the same rule set and the same database function
    as the private approval link, bound to the version they reviewed;
  - read their own inspection bookings, reports (download the PDF), the
    staff-recorded findings (major/minor with report quotes) and repair
    requests.

Nothing here creates or edits offer terms, infers defects or costs, or makes
price recommendations.
"""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from src.services import db, file_store, purchase_offers
from src.services.investor_journey import _account_investor

router = APIRouter(prefix="/me/investor", tags=["Investor self-service"])

# Staff-internal columns never sent to an investor account.
OFFER_PRIVATE_FIELDS = {"created_by"}
BOOKING_PRIVATE_FIELDS = {"created_by", "listing_agent_contact"}
REPORT_PRIVATE_FIELDS = {"storage_path", "uploaded_by", "sha256"}
FINDING_PRIVATE_FIELDS = {"created_by"}
REPAIR_PRIVATE_FIELDS = {"requested_by", "responded_by"}
EVENT_PRIVATE_FIELDS = {"actor_id"}


def q(fn, *args, **kwargs):
    # Same error translation as the staff module (missing migration -> 503
    # with the SQL file name, RPC business errors -> 409/404).
    return purchase_offers.q(fn, *args, **kwargs)


def strip(row: dict | None, private: set) -> dict | None:
    if row is None:
        return None
    return {k: v for k, v in row.items() if k not in private}


def in_filter(ids) -> str:
    return "in.(" + ",".join(str(x) for x in ids) + ")"


def my_offers(investor: dict) -> list[dict]:
    return q(db._get, "acquisition_offers", {
        "investor_id": f"eq.{investor['id']}", "order": "updated_at.desc", "limit": "100", "select": "*",
    })


def my_offer(request: Request, offer_id: UUID) -> tuple[dict, dict]:
    """The offer, only if it belongs to the logged-in investor. A 404 (not
    403) for someone else's offer, so ids can't be probed."""
    investor = _account_investor(request)
    rows = q(db._get, "acquisition_offers", {
        "id": f"eq.{offer_id}", "investor_id": f"eq.{investor['id']}", "select": "*", "limit": "1",
    })
    if not rows:
        raise HTTPException(404, "Purchase offer not found.")
    return investor, rows[0]


def offer_view(offer: dict) -> dict:
    offer_id = offer["id"]
    version = q(db._get, "acquisition_offer_versions", {
        "offer_id": f"eq.{offer_id}", "version_number": f"eq.{offer['current_version']}", "select": "*", "limit": "1",
    })
    handoff = q(db._get, "acquisition_offer_handoffs", {
        "offer_id": f"eq.{offer_id}", "order": "created_at.desc", "limit": "1",
        "select": "platform,status,platform_link,note,created_at",
    })
    events = q(db._get, "acquisition_offer_events", {"offer_id": f"eq.{offer_id}", "order": "sequence.asc", "select": "*"})
    deadlines = q(db._get, "acquisition_offer_deadlines", {
        "offer_id": f"eq.{offer_id}", "order": "due_at.asc", "select": "kind,label,due_at,status",
    })
    approvals = q(db._get, "acquisition_offer_approvals", {
        "offer_id": f"eq.{offer_id}", "order": "created_at.desc", "limit": "1", "select": "decision,note,created_at,version_id",
    })
    current = version[0] if version else None
    terms = (current or {}).get("terms") or {}
    expired = bool(terms.get("expires_at")) and purchase_offers.parse_dt(terms["expires_at"]) <= datetime.now(timezone.utc)
    return {
        "offer": strip(offer, OFFER_PRIVATE_FIELDS),
        "terms": terms,
        "version_number": (current or {}).get("version_number"),
        "handoff": handoff[0] if handoff else None,
        "events": [strip(e, EVENT_PRIVATE_FIELDS) for e in events],
        "deadlines": deadlines,
        "latest_approval": approvals[0] if approvals else None,
        # Only when staff have actually sent the summary for approval, and
        # the offer hasn't expired - mirrors the private link's rules.
        "can_decide": offer["status"] in ("awaiting_investor_approval", "investor_approved") and not expired,
    }


@router.get("/purchase-offers")
def list_my_offers(request: Request):
    investor = _account_investor(request)
    return {"offers": [offer_view(o) for o in my_offers(investor)]}


@router.get("/purchase-offers/{offer_id}/summary.pdf")
def my_offer_summary_pdf(offer_id: UUID, request: Request):
    _, offer = my_offer(request, offer_id)
    version = purchase_offers.get_terms_version(offer["id"], offer["current_version"])
    data, _ = purchase_offers.render_version_pdf(offer, version)
    return Response(data, media_type="application/pdf", headers={
        "Cache-Control": "no-store",
        "Content-Disposition": f'inline; filename="Summary-{offer["reference"]}-v{version["version_number"]}.pdf"',
    })


class MyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The version the investor was looking at. If staff changed the terms
    # since, the decision is refused rather than applied to terms they
    # never saw.
    version_number: int = Field(ge=1)
    decision: Literal["approved", "changes_requested"]
    note: str = Field(default="", max_length=2000)
    reviewed_summary: bool


@router.post("/purchase-offers/{offer_id}/decision")
def my_offer_decision(offer_id: UUID, body: MyDecision, request: Request):
    investor, offer = my_offer(request, offer_id)
    if int(offer["current_version"]) != body.version_number:
        raise HTTPException(409, "These terms were updated since you opened them. Refresh and review the latest summary.")
    version = purchase_offers.get_terms_version(offer["id"], offer["current_version"])
    terms = version.get("terms") or {}
    if terms.get("expires_at") and purchase_offers.parse_dt(terms["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(409, "The offer has expired and cannot be approved.")
    if offer["status"] not in ("awaiting_investor_approval", "investor_approved"):
        raise HTTPException(409, "This offer is not waiting for your approval.")
    if body.decision == "approved" and not body.reviewed_summary:
        raise HTTPException(400, "Please confirm that you reviewed the summary before approving it.")
    if body.decision == "changes_requested" and not body.note.strip():
        raise HTTPException(400, "Tell the team what should change.")
    decision = purchase_offers._rpc("acquisition_offer_record_approval", {
        "p_offer_id": offer["id"], "p_version_id": version["id"], "p_actor_id": investor["id"],
        "p_decision": body.decision, "p_note": body.note.strip(),
    })
    return {"recorded": True, "decision": decision}


# ---------------------------------------------------------------------
# Inspections
# ---------------------------------------------------------------------

def my_scope(investor: dict) -> tuple[list[dict], set, set]:
    offers = my_offers(investor)
    offer_ids = {o["id"] for o in offers}
    deal_ids = {o["deal_id"] for o in offers if o.get("deal_id")}
    return offers, offer_ids, deal_ids


def scoped_rows(table: str, offer_ids: set, deal_ids: set, order: str) -> list[dict]:
    """Rows linked to one of the investor's offers, or to the deal one of
    their offers is on (inspections can be booked against the deal)."""
    rows, seen = [], set()
    for column, ids in (("offer_id", offer_ids), ("deal_id", deal_ids)):
        if not ids:
            continue
        for row in q(db._get, table, {column: in_filter(ids), "order": order, "select": "*", "limit": "300"}):
            if row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
    return rows


def my_report(request: Request, report_id: UUID) -> dict:
    investor = _account_investor(request)
    _, offer_ids, deal_ids = my_scope(investor)
    rows = q(db._get, "inspection_reports", {"id": f"eq.{report_id}", "select": "*", "limit": "1"})
    report = rows[0] if rows else None
    if not report or not (report.get("offer_id") in offer_ids or report.get("deal_id") in deal_ids):
        raise HTTPException(404, "Inspection report not found.")
    return report


@router.get("/inspections")
def my_inspections(request: Request):
    investor = _account_investor(request)
    offers, offer_ids, deal_ids = my_scope(investor)
    by_offer = {o["id"]: o for o in offers}
    by_deal = {o["deal_id"]: o for o in offers if o.get("deal_id")}

    def label(row):
        offer = by_offer.get(row.get("offer_id")) or by_deal.get(row.get("deal_id")) or {}
        return {"offer_reference": offer.get("reference"), "property_title": offer.get("property_title")}

    inspector_cache = {}

    def inspector(inspector_id):
        if not inspector_id:
            return None
        if inspector_id not in inspector_cache:
            rows = q(db._get, "inspectors", {"id": f"eq.{inspector_id}", "select": "name,license_number,turnaround", "limit": "1"})
            # Explicit pick (not just the select= projection) - the investor
            # sees who and their licence, never the internal contact field.
            inspector_cache[inspector_id] = (
                {k: rows[0].get(k) for k in ("name", "license_number", "turnaround")} if rows else None
            )
        return inspector_cache[inspector_id]

    bookings = []
    for row in scoped_rows("inspection_bookings", offer_ids, deal_ids, "created_at.desc"):
        bookings.append({**strip(row, BOOKING_PRIVATE_FIELDS), **label(row), "inspector": inspector(row.get("inspector_id"))})

    reports = []
    for row in scoped_rows("inspection_reports", offer_ids, deal_ids, "created_at.desc"):
        findings = q(db._get, "inspection_findings", {"report_id": f"eq.{row['id']}", "order": "severity.desc,id.asc", "select": "*"})
        reports.append({
            **strip(row, REPORT_PRIVATE_FIELDS), **label(row), "inspector": inspector(row.get("inspector_id")),
            "findings": [strip(f, FINDING_PRIVATE_FIELDS) for f in findings],
        })

    repairs = []
    if offer_ids:
        for row in q(db._get, "inspection_repairs", {"offer_id": in_filter(offer_ids), "order": "id.asc", "select": "*"}):
            repairs.append({**strip(row, REPAIR_PRIVATE_FIELDS), **label(row)})

    # The due-diligence end for each offer, so the investor can see when
    # repair negotiation closes (inspections.py refuses changes after it).
    due_diligence = []
    if offer_ids:
        for row in q(db._get, "acquisition_offer_deadlines", {
            "offer_id": in_filter(offer_ids), "kind": "eq.due_diligence_end", "status": "eq.active", "select": "offer_id,label,due_at",
        }):
            due_diligence.append({**row, **label(row)})

    return {"bookings": bookings, "reports": reports, "repairs": repairs, "due_diligence": due_diligence,
            "has_offers": bool(offers)}


@router.get("/inspections/reports/{report_id}/file")
def my_report_file(report_id: UUID, request: Request):
    report = my_report(request, report_id)
    data = file_store.get(report["storage_path"])
    return Response(data, media_type="application/pdf", headers={
        "Cache-Control": "no-store",
        "Content-Disposition": f'inline; filename="{report["file_name"].replace(chr(34), "_")}"',
    })


# ---------------------------------------------------------------------
# "Select this home" - an investor picking a suggested (MLS) home to buy
# ---------------------------------------------------------------------
# The investor counterpart of a tenant's "Select this home" in the chat
# (tenants send a rental application - src/services/rentals.py). Selecting
# here does NOT create a purchase offer: offers stay staff-created
# (purchase_offers.py). It records the pick on the investor's own activity
# log (so staff see it on the investor's journey) and raises a sales
# enquiry in the Inquiries tab with the investor's financing and price in
# mind, so the team can start offer preparation for that exact home.
#
# Stored as investor_activity rows (action home_selected / home_unselected)
# rather than a new table, so no extra migration is needed: the current
# selection set is the replay of those rows, newest action per home wins.
# Home details (address, price, city, photo) are read from mls_listings on
# the server, never trusted from the browser.

SELECT_ACTION = "home_selected"
UNSELECT_ACTION = "home_unselected"
FINANCING_LABELS = {"cash": "Cash", "mortgage": "Mortgage", "not_sure": "Not sure yet"}

# Buyer onboarding answers from the "Select this home" checklist
# (index.html: #buy-dialog). Every field is optional here so an older
# client still works; the dialog itself asks for the starred ones.
Short = lambda n=120: Field(default=None, max_length=n)  # noqa: E731


class BuyerDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 1. Your details
    full_name: str | None = Short()
    phone: str | None = Short(40)
    email: str | None = Short(200)
    contact_method: Literal["call", "whatsapp", "email"] | None = None
    # 2. Financing
    pre_approval: Literal["pre_approved", "applied", "not_yet"] | None = None
    lender: str | None = Short()
    pre_approved_amount: float | None = Field(default=None, ge=0, le=100_000_000)
    down_payment_percent: float | None = Field(default=None, ge=0, le=100)
    cash_available: float | None = Field(default=None, ge=0, le=100_000_000)
    proof_of_funds: bool | None = None
    # 3. Offer & timing
    earnest_money: float | None = Field(default=None, ge=0, le=10_000_000)
    closing_timeline: Literal["30_days", "30_60_days", "60_90_days", "flexible"] | None = None
    contingencies: list[Literal["inspection", "financing", "appraisal", "sale_of_other_home"]] = Field(default_factory=list, max_length=4)
    # 4. Plans for the home
    intended_use: Literal["long_term_rental", "short_term_rental", "live_in", "fix_and_flip"] | None = None
    management: Literal["self", "staybot", "not_sure"] | None = None
    expected_rent: float | None = Field(default=None, ge=0, le=1_000_000)
    repair_budget: float | None = Field(default=None, ge=0, le=10_000_000)
    # 5. Buying as
    buying_as: Literal["individual", "joint", "llc"] | None = None
    entity_name: str | None = Short(200)
    co_buyer_name: str | None = Short()
    has_agent: bool | None = None
    has_attorney: bool | None = None


class HomeSelectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    list_number: str = Field(min_length=1, max_length=80)
    financing: Literal["cash", "mortgage", "not_sure"] = "not_sure"
    offer_price: float | None = Field(default=None, gt=0, le=100_000_000)
    note: str = Field(default="", max_length=1000)
    details: BuyerDetails | None = None


LABELS = {
    "pre_approval": {"pre_approved": "Pre-approved", "applied": "Applied, waiting", "not_yet": "Not yet"},
    "closing_timeline": {"30_days": "Within 30 days", "30_60_days": "30-60 days", "60_90_days": "60-90 days", "flexible": "Flexible"},
    "intended_use": {"long_term_rental": "Long-term rental", "short_term_rental": "Short-term rental",
                     "live_in": "Live in it", "fix_and_flip": "Fix and flip"},
    "management": {"self": "Self-manage", "staybot": "Staybot team manages", "not_sure": "Not sure"},
    "buying_as": {"individual": "Individual", "joint": "Jointly with someone", "llc": "LLC / company"},
    "contact_method": {"call": "Phone call", "whatsapp": "WhatsApp", "email": "Email"},
}


def _selections(investor: dict) -> dict[str, dict]:
    rows = q(db._get, "investor_activity", {
        "investor_id": f"eq.{investor['id']}",
        "action": f"in.({SELECT_ACTION},{UNSELECT_ACTION})",
        "order": "id.asc", "select": "id,action,details,created_at", "limit": "1000",
    })
    current: dict[str, dict] = {}
    for row in rows:
        details = row.get("details") or {}
        key = str(details.get("list_number") or "")
        if not key:
            continue
        if row["action"] == SELECT_ACTION:
            # A later save for the same home (edited answers) keeps the
            # original selection time.
            first = (current.get(key) or {}).get("selected_at") or row.get("created_at")
            current[key] = {**details, "selected_at": first}
        else:
            current.pop(key, None)
    return current


def _money(v) -> str | None:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return None


def _yn(v) -> str | None:
    return None if v is None else ("Yes" if v else "No")


def summary_lines(investor: dict, body: HomeSelectionIn) -> list[str]:
    """The buyer's answers as short lines for the team (Inquiries tab notes)."""
    d = body.details or BuyerDetails()
    lab = lambda k, v: LABELS[k].get(v) if v else None  # noqa: E731
    pairs = [
        ("Buyer", d.full_name or investor.get("name")),
        ("Phone", d.phone or investor.get("phone")),
        ("Email", d.email or investor.get("email")),
        ("Contact by", lab("contact_method", d.contact_method)),
        ("Financing", FINANCING_LABELS[body.financing]),
        ("Pre-approval", lab("pre_approval", d.pre_approval)),
        ("Lender", d.lender),
        ("Pre-approved for", _money(d.pre_approved_amount) if d.pre_approved_amount else None),
        ("Down payment", f"{d.down_payment_percent:g}%" if d.down_payment_percent is not None else None),
        ("Cash available", _money(d.cash_available) if d.cash_available is not None else None),
        ("Proof of funds", _yn(d.proof_of_funds)),
        ("Price in mind", _money(body.offer_price) if body.offer_price else None),
        ("Earnest money", _money(d.earnest_money) if d.earnest_money else None),
        ("Closing", lab("closing_timeline", d.closing_timeline)),
        ("Contingencies", ", ".join(c.replace("_", " ") for c in d.contingencies) or None),
        ("Plan", lab("intended_use", d.intended_use)),
        ("Management", lab("management", d.management)),
        ("Expected rent", f"{_money(d.expected_rent)}/mo" if d.expected_rent else None),
        ("Repair budget", _money(d.repair_budget) if d.repair_budget else None),
        ("Buying as", lab("buying_as", d.buying_as)),
        ("Entity", d.entity_name),
        ("Co-buyer", d.co_buyer_name),
        ("Has agent", _yn(d.has_agent)),
        ("Has attorney", _yn(d.has_attorney)),
    ]
    lines = [f"{k}: {v}" for k, v in pairs if v not in (None, "")]
    if body.note.strip():
        lines.append(f"Note: {body.note.strip()}")
    return lines


@router.get("/selected-homes")
def my_selected_homes(request: Request):
    investor = _account_investor(request)
    homes = sorted(_selections(investor).values(), key=lambda h: h.get("selected_at") or "", reverse=True)
    # Prefill for the buyer checklist - what the chat already learned.
    profile = {k: investor.get(k) for k in ("name", "phone", "email", "financing_requirements", "budget")}
    return {"homes": homes, "profile": profile}


@router.post("/selected-homes")
def select_home(body: HomeSelectionIn, request: Request):
    """Select a home, or (for one already selected) save edited answers."""
    investor = _account_investor(request)
    existing = _selections(investor).get(body.list_number)
    if existing and body.details is None:
        return {"home": existing, "already_selected": True}

    listing = q(db.get_mls_listing, body.list_number)
    if not listing:
        raise HTTPException(404, "That home isn't in the listings any more.")

    title = ", ".join(x for x in (listing.get("street_address"), listing.get("city")) if x) or "This property"
    price_label = _money(listing.get("list_price"))
    note = body.note.strip()
    lines = summary_lines(investor, body)
    head = f"🏠 Selected to buy by investor {investor.get('name') or ''}".rstrip()
    notes = " · ".join([head, *lines, "Next step: prepare a purchase offer for this home (Purchase offers)."])

    # Best-effort: the selection itself must not be lost if the inquiries
    # table is missing (supabase_property_inquiries.sql not run yet).
    inquiry_id = (existing or {}).get("inquiry_id")
    try:
        if inquiry_id:
            db.update_inquiry(inquiry_id, {"notes": notes[:4000]})
        else:
            from src.services import accounts, inquiries  # deferred like _account_investor's accounts import
            account = accounts.current_account(request) or {}
            d = body.details or BuyerDetails()
            saved = inquiries.create_inquiry(
                property_id=body.list_number, property_title=title, price_label=price_label,
                area=listing.get("city"), session_id=account.get("session_id") or investor.get("session_id"),
                customer_name=d.full_name or investor.get("name") or account.get("name"),
                customer_phone=d.phone or investor.get("phone"),
                notes=notes[:4000],
            )
            inquiry_id = (saved or {}).get("id")
    except Exception as error:
        print(f"Saving the home-selection enquiry failed (non-fatal): {error}")

    details = {
        "list_number": body.list_number, "title": title, "price_label": price_label,
        "list_price": listing.get("list_price"), "city": listing.get("city"),
        "bedrooms": listing.get("bedrooms"), "photo_url": listing.get("photo_url"),
        "financing": body.financing, "offer_price": body.offer_price, "note": note or None,
        "buyer": body.details.model_dump(exclude_none=True) if body.details else {},
        "summary": lines,
        "inquiry_id": inquiry_id,
    }
    # A home another investor listed on Staybot: send the buyer's onboarding
    # answers to that investor (their Tenants tab -> "Buyers"), who replies
    # with a purchase offer - same flow as a tenant applying to rent.
    owner_request = None
    if listing.get("owner_listed"):
        from src.services import accounts, rentals  # deferred, like the imports above
        buyer = accounts.current_account(request)
        prop = db._get("properties", {"id": f"eq.{body.list_number}", "select": "*", "limit": "1"}) or []
        if buyer and prop:
            d = body.details or BuyerDetails()
            closing = LABELS["closing_timeline"].get(d.closing_timeline) if d.closing_timeline else None
            row = rentals.create_purchase_request(buyer, prop[0], {
                "buyer": d.model_dump(exclude_none=True),
                "financing": body.financing,
                "offer_price": body.offer_price,
                "closing": closing,
                "headline": rentals.purchase_headline(body.financing, body.offer_price, closing),
                "lines": lines,
                "listing": {"sale_price": prop[0].get("sale_price")},
            }, message=note)
            if row:
                owner_request = {"id": row.get("id"), "status": row.get("status")}
                details["owner_request_id"] = row.get("id")

    q(db._post, "investor_activity", {"investor_id": investor["id"], "actor": "investor",
                                      "action": SELECT_ACTION, "details": details})
    return {"home": {**details, "selected_at": (existing or {}).get("selected_at") or now_iso()},
            "already_selected": bool(existing), "updated": bool(existing), "owner_request": owner_request}


@router.delete("/selected-homes/{list_number}")
def unselect_home(list_number: str, request: Request):
    investor = _account_investor(request)
    existing = _selections(investor).get(list_number)
    if not existing:
        raise HTTPException(404, "You haven't selected that home.")
    q(db._post, "investor_activity", {"investor_id": investor["id"], "actor": "investor",
                                      "action": UNSELECT_ACTION,
                                      "details": {"list_number": list_number, "title": existing.get("title")}})
    if existing.get("owner_request_id"):
        from src.services import accounts, rentals  # deferred
        buyer = accounts.current_account(request)
        if buyer:
            rentals.withdraw_purchase_request(buyer, list_number)
    if existing.get("inquiry_id"):
        try:
            db.update_inquiry(existing["inquiry_id"], {"status": "closed", "notes": "Investor removed this home from their selection."})
        except Exception as error:
            print(f"Closing the home-selection enquiry failed (non-fatal): {error}")
    return {"removed": list_number}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
