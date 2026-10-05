"""
Renting a home: tenant applies -> team approves -> owner approves ->
the tenant rents it and can raise maintenance requests, which the owner sees.

  Tenant (tenant accounts)            /me/rentals/...      Homes + Maintenance tabs in /ui
  Owner  (new/existing investors)     /me/owner/...        "Tenants" tab in /ui
  Team   (shared password / admin)    /applications/...    "Applications" tab in /ui (staff-only,
                                                           see STAFF_ONLY_PREFIXES in team_auth.py)

Statuses (rental_applications.status, supabase_rental_applications.sql):
  submitted      -> the team reviews it first (tenant screening etc.)
  team_approved  -> now shows on the owner's Tenants tab, waiting for them
  approved       -> the tenant rents the home. The home is marked 'let' (off
                    the Homes tab), other open applications for it are
                    closed, and maintenance opens for this tenant + home.
  team_declined / owner_declined / withdrawn / closed / ended

A home no investor account owns (a listing the team added, or an anonymous
owner's chat listing) has no owner step: the team's approval is final.

Buying a home another investor listed (purchase requests) reuses the same
table, marked details.kind == "purchase":
  buyer (new/existing investor) picks "Select this home" and fills in the
  buyer checklist (investor_acquisition.select_home) -> a purchase request
  goes straight to the listing investor's Tenants tab ("Buyers"), status
  team_approved (no tenant screening step) -> the owner sends the buyer a
  purchase offer (tenant_offers) -> the buyer accepts / counters / declines
  on their Purchase offers tab (GET /me/buying) -> once either side accepts
  the other's terms the request is approved, the home is marked 'sold' and
  other buyers' open requests for it are closed.

Maintenance only works for a tenant who rents a home this way (tenant_home()).
Chat reports (src/services/chat.py) and the Maintenance tab's form both file
the ticket against that home; the owner sees tickets from their own tenants
on their homes, never anyone else's.
"""

from datetime import date, datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.services import accounts, db, maintenance, owner_listings, properties

tenant_router = APIRouter(prefix="/me/rentals", tags=["Rentals - tenant"])
owner_router = APIRouter(prefix="/me/owner", tags=["Rentals - owner"])
team_router = APIRouter(prefix="/applications", tags=["Rentals - team"])

OPEN = ("submitted", "team_approved", "approved")
WAITING = ("submitted", "team_approved")
# The owner sees every application for their homes from the moment the
# tenant applies - "submitted" ones marked as being reviewed by our team.
# They see the tenant's onboarding answers (household, move-in, work and
# income, rental history - rental_applications.details) straight away, but
# can only approve once the team has (status team_approved), and only see the
# tenant's contact details and screening documents from then on (TEAM_CLEARED).
OWNER_VISIBLE = ("submitted", "team_approved", "approved", "owner_declined", "ended", "closed",
                 "withdrawn", "team_declined")
TEAM_CLEARED = ("team_approved", "approved", "owner_declined", "ended")
OWNER_TICKET_STATUSES = ("open", "in_progress", "resolved")

STATUS_TEXT = {
    "submitted": "With our team for approval",
    "team_approved": "Approved by our team - waiting for the owner",
    "approved": "Approved - you rent this home",
    "team_declined": "Not approved by our team",
    "owner_declined": "Not approved by the owner",
    "withdrawn": "Withdrawn",
    "closed": "Closed - the home was let to someone else",
    "ended": "Tenancy ended",
}

PURCHASE_KIND = "purchase"
PURCHASE_STATUS_TEXT = {
    "team_approved": "With the owner - waiting for their purchase offer",
    "approved": "Offer accepted - agreed sale",
    "owner_declined": "Not accepted by the owner",
    "withdrawn": "Withdrawn",
    "closed": "Closed - the home was sold to another buyer",
}


SALE_CANCELLED_TEXT = "Sale cancelled by the owner"


def is_purchase(row: dict) -> bool:
    return isinstance(row.get("details"), dict) and row["details"].get("kind") == PURCHASE_KIND


MAINTENANCE_LOCKED = (
    "Maintenance requests open once you rent a home through Staybot - that's after our team and the "
    "home's owner approve your rental application. Tell me what you're looking for and pick 'Select this home' "
    "on one of the homes I suggest, or apply from the Homes tab."
)


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_rental_applications.sql to enable rental applications.")


def now_iso():
    return accounts.now_iso()


def require_role(request: Request, *roles: str) -> dict:
    account = accounts.current_account(request)
    if not account:
        raise HTTPException(401, "Please log in.")
    if account.get("role") not in roles:
        raise HTTPException(403, "This isn't available for your account type.")
    configured()
    return account


def one(table: str, params: dict) -> Optional[dict]:
    rows = db._get(table, {**params, "select": "*", "limit": "1"})
    return rows[0] if rows else None


def get_application(app_id: str) -> dict:
    row = one("rental_applications", {"id": f"eq.{app_id}"})
    if not row:
        raise HTTPException(404, "Application not found.")
    return row


def owner_account_for(prop: dict) -> Optional[dict]:
    """The investor account that listed this home, if any."""
    if not prop.get("session_id") or prop.get("source") not in ("owner_chat", "investor_form"):
        return None
    acct = one("accounts", {"session_id": f"eq.{prop['session_id']}"})
    return acct if acct and acct.get("role") in owner_listings.INVESTOR_ROLES else None


def screening_summary(tenant_account_id: str) -> dict:
    """What the owner / team see about the applicant from their screening intake."""
    from src.services import portfolio  # local import, portfolio.py doesn't import this module
    details = (portfolio.get_portfolio(tenant_account_id) or {}).get("details") or {}
    summary = {k: details.get(k) for k in ("employment_status", "monthly_income", "annual_income", "has_emi")
               if details.get(k) not in (None, "")}
    try:
        docs = db._get("account_documents", {
            "account_id": f"eq.{tenant_account_id}", "doc_key": "eq.proof_of_income",
            "order": "uploaded_at.desc", "limit": "1", "select": "status",
        })
        summary["proof_of_income"] = docs[0]["status"] if docs else "not_uploaded"
    except Exception:
        pass
    return summary


def app_view(row: dict, with_screening: bool = False, with_contact: bool = True) -> dict:
    if is_purchase(row):
        return purchase_view(row)
    out = {**row, "status_text": STATUS_TEXT.get(row.get("status"), row.get("status")), "kind": "rental"}
    if row.get("status") == "owner_declined" and (row.get("details") or {}).get("sale_cancelled_at"):
        out["status_text"] = SALE_CANCELLED_TEXT
    elif row.get("status") == "approved" and (row.get("details") or {}).get("sale_agreed"):
        out["status_text"] = PURCHASE_STATUS_TEXT["approved"]  # "Offer accepted - agreed sale"
    elif row.get("status") == "closed" and str(row.get("team_note") or "").startswith("The home was sold"):
        out["status_text"] = "Closed - " + row["team_note"][:1].lower() + row["team_note"][1:].rstrip(".")
    out.pop("tenant_session_id", None)
    out.pop("owner_session_id", None)
    out["has_owner_step"] = bool(row.get("owner_session_id"))
    out["has_details"] = bool(row.get("details"))
    details = row.get("details")
    if isinstance(details, dict) and details:
        shown = dict(details)
        if not with_contact:
            for k in DETAIL_CONTACT_KEYS:
                shown.pop(k, None)
            shown["contact_hidden"] = True
        out["details"] = shown
        out["summary"] = details_summary(details)
    else:
        out.pop("details", None)
    if not with_contact:
        for k in ("tenant_email", "tenant_phone"):
            out.pop(k, None)
    if with_screening:
        out["screening"] = screening_summary(row["tenant_account_id"])
    return out


# ---- Owner -> tenant purchase offers (tenant_offers) ----
# Same terms and validation as the staff Purchase offers page
# (purchase_offers.Terms). Staybot only records them; no contract is created.
OFFER_ACTIVE_APP_STATUSES = ("submitted", "team_approved", "approved")
OFFER_STATUS_TEXT = {  # {other} = the side that answers this version
    "sent": "Waiting for the {other}",
    "accepted": "Accepted by the {other}",
    "declined": "Declined by the {other}",
    "withdrawn": "Withdrawn by the {sender}",
    "replaced": "Replaced by a newer offer",
    "expired": "Expired",
}


class OfferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    terms: dict
    message: str = Field(default="", max_length=1000)


def validated_terms(raw: dict) -> dict:
    from pydantic import ValidationError
    from src.services import purchase_offers  # local import - same rules as the staff page
    try:
        return purchase_offers.Terms(**raw).snapshot()
    except ValidationError as e:
        raise HTTPException(422, "; ".join(str(err.get("msg", "")).removeprefix("Value error, ") for err in e.errors()))


def new_offer_version(app: dict, owner_session_id: str, terms: dict, sender: str, message: str) -> dict:
    """Send a new version: the one waiting for an answer (from either side) is replaced."""
    try:
        db._patch("tenant_offers", {"status": "replaced"},
                  {"application_id": f"eq.{app['id']}", "status": "eq.sent"})
        return db._post("tenant_offers", {
            "application_id": app["id"], "property_id": app["property_id"], "property_title": app.get("property_title"),
            "owner_session_id": owner_session_id, "tenant_account_id": app["tenant_account_id"],
            "terms": terms, "status": "sent", "from_party": sender, "message": message.strip() or None,
        })
    except HTTPException:
        raise
    except Exception as e:
        offer_db_error(e)


def current_offer(app_id: str) -> Optional[dict]:
    try:
        rows = db._get("tenant_offers", {"application_id": f"eq.{app_id}", "order": "created_at.desc"}) or []
    except Exception as e:
        offer_db_error(e)
    rows = sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows[0] if rows else None


def offer_view(o: dict) -> dict:
    from src.services import purchase_offers  # local import
    status = o.get("status")
    exp = purchase_offers.parse_dt((o.get("terms") or {}).get("expires_at"))
    if status == "sent" and exp and exp <= datetime.now(timezone.utc):
        status = "expired"
    sender = o.get("from_party") or "owner"
    other = "tenant" if sender == "owner" else "owner"
    return {k: o.get(k) for k in ("id", "application_id", "property_id", "property_title", "terms", "message",
                                   "tenant_note", "owner_note", "responded_at", "created_at", "updated_at")} | {
        "from_party": sender, "waiting_on": other if status == "sent" else None,
        "status": status, "status_text": OFFER_STATUS_TEXT.get(status, status).format(other=other, sender=sender)}


def latest_offers(app_ids: list[str]) -> dict[str, dict]:
    """The newest offer per application (not counting replaced ones). Never
    raises: before tenant_offers exists, applications simply have no offer."""
    if not app_ids:
        return {}
    try:
        rows = db._get("tenant_offers", {"application_id": f"in.({','.join(app_ids)})",
                                         "order": "created_at.desc", "select": "*"}) or []
    except Exception as e:
        print(f"Offer lookup failed (non-fatal, is supabase_rental_applications.sql run?): {e}")
        return {}
    by_app = {}
    for r in sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True):
        by_app.setdefault(r["application_id"], []).append(r)
    out = {}
    for app_id, versions in by_app.items():  # newest first
        view = offer_view(versions[0])
        view["version"] = len(versions)
        # What this version changed from the one before it (a counter-offer).
        if len(versions) > 1:
            before = versions[1].get("terms") or {}
            now = versions[0].get("terms") or {}
            view["previous_terms"] = before
            view["changed"] = sorted(k for k in set(before) | set(now) if same_term(k, before.get(k), now.get(k)) is False)
        out[app_id] = view
    return out


def same_term(key: str, a, b) -> bool:
    """Equal for display? The offer form edits expiry to the minute, so an
    untouched expiry comes back without its seconds - that isn't a change."""
    if key == "expires_at" and a and b:
        from src.services import purchase_offers  # local import
        try:
            fa, fb = purchase_offers.parse_dt(a), purchase_offers.parse_dt(b)
            return fa.replace(second=0, microsecond=0) == fb.replace(second=0, microsecond=0)
        except (TypeError, ValueError):
            pass
    return a == b


def with_offers(views: list[dict]) -> list[dict]:
    offers = latest_offers([v["id"] for v in views])
    # A current tenant buying the home they rent goes through the "Own this
    # house" flow (home_purchase.py: team checks + inspection), not the
    # owner's "Approve tenant" step below.
    tenancy_sales = home_purchase_apps([
        v["id"] for v in views
        if v.get("kind") != PURCHASE_KIND and v.get("status") == "approved"
        and (offers.get(v["id"]) or {}).get("status") == "accepted"])
    for v in views:
        v["offer"] = offers.get(v["id"])
        d = v.get("details") if isinstance(v.get("details"), dict) else {}
        accepted = bool(v["offer"]) and v["offer"].get("status") == "accepted"
        # Tenant accepted a purchase offer (or the owner accepted their
        # counter): the owner's "Approve tenant" is the last step.
        v["needs_owner_approval"] = (v.get("kind") != "purchase" and accepted and not d.get("sale_approved_at")
                                     and v.get("status") in ("team_approved", "approved")
                                     and v["id"] not in tenancy_sales)
        v["sale_approved"] = v.get("status") == "approved" and bool(d.get("sale_approved_at"))
        if accepted and v.get("kind") != "purchase":
            v["offer"]["owner_approval"] = "approved" if v["sale_approved"] else "pending"
    return views


def home_purchase_apps(app_ids: list[str]) -> set:
    """Which of these rental applications have a home_buy_requests row
    (the tenant is buying the home they rent - home_purchase.py). Empty
    when there are none or the table doesn't exist yet. Never raises."""
    ids = [i for i in app_ids if i]
    if not ids:
        return set()
    from src.services import home_purchase  # local import - home_purchase imports this module
    return set(home_purchase.for_applications(ids))


def with_buy_requests(views: list[dict]) -> list[dict]:
    """A tenant asking to buy the home they rent (home_purchase.py)."""
    from src.services import home_purchase  # local import - home_purchase imports this module
    reqs = home_purchase.for_applications([v["id"] for v in views if v.get("kind") != PURCHASE_KIND])
    for v in views:
        v["buy_request"] = reqs.get(v["id"])
    return views


def ensure_home_free(app: dict):
    taken = db._get("rental_applications", {"property_id": f"eq.{app['property_id']}", "status": "eq.approved",
                                             "id": f"neq.{app['id']}", "select": "id", "limit": "1"})
    if taken:
        raise HTTPException(409, "This home already has an approved tenant. End that tenancy first.")
    prop = one("properties", {"id": f"eq.{app['property_id']}"}) or {}
    d = app.get("details") if isinstance(app.get("details"), dict) else {}
    # (A home already marked sold to THIS tenant - from before the owner's
    # approval step existed - can still be approved to finish it.)
    if prop.get("status") == "sold" and not d.get("sale_agreed"):
        raise HTTPException(409, "This home has already been sold.")


def approve_final(app: dict) -> dict:
    """The last approval: the home is assigned to this tenant.

    The home is taken off the Homes tab (status 'let') FIRST: if that fails
    the approval stops with an error instead of leaving an approved tenant
    on a home everyone can still apply for. The approved application is what
    ties the home to this tenant - tenant_home() reads it for their "You
    rent ..." card, Maintenance and Messages, and My listings uses it to
    block Relist while they live there."""
    try:
        db.update_property(app["property_id"], {"status": "let"})
    except Exception as e:
        raise HTTPException(500, f"Couldn't take the home off the Homes tab, so the tenant wasn't approved. Try again. ({e})")
    properties.clear_cache()
    saved = db._patch("rental_applications", {"status": "approved"}, {"id": f"eq.{app['id']}"})
    try:
        db._patch("rental_applications",
                  {"status": "closed", "team_note": "The home was let to another applicant."},
                  {"property_id": f"eq.{app['property_id']}", "status": "in.(submitted,team_approved)"})
    except Exception as e:
        print(f"Closing other applications failed (non-fatal): {e}")
    return saved


def tenant_home(account_id: str) -> Optional[dict]:
    """The home this tenant account rents (approved application), or None.
    Never raises - chat.py calls it on every tenant message."""
    if not (db.ENABLED and account_id):
        return None
    try:
        rows = db._get("rental_applications", {
            "tenant_account_id": f"eq.{account_id}", "status": "eq.approved",
            "order": "updated_at.desc", "limit": "1", "select": "*",
        })
    except Exception as e:
        print(f"Tenant home lookup failed (non-fatal, is supabase_rental_applications.sql run?): {e}")
        return None
    if not rows:
        return None
    app = rows[0]
    prop = None
    try:
        prop = one("properties", {"id": f"eq.{app['property_id']}"})
    except Exception:
        pass
    prop = prop or {}
    return {
        "application_id": app["id"],
        "property_id": app["property_id"],
        "title": prop.get("title") or app.get("property_title") or "your home",
        "location": prop.get("location") or prop.get("area") or "",
        "since": app.get("owner_decided_at") or app.get("team_reviewed_at") or app.get("updated_at"),
        "has_owner": bool(app.get("owner_session_id")),  # an investor to message (rental_chat.py)
        # Bought through an approved purchase offer rather than rented.
        "bought": bool((app.get("details") or {}).get("sale_approved_at")) if isinstance(app.get("details"), dict) else False,
    }


def home_note(home: dict) -> str:
    """For the AI (chat.py): this tenant's home, so a maintenance report is
    filed against it without asking which property / unit it is."""
    where = f"{home['title']}" + (f", {home['location']}" if home.get("location") else "")
    return (
        f"This tenant rents {where} (approved through Staybot). Any maintenance problem they describe is "
        f"about that home: do NOT ask for a unit or apartment number - set tenant_id to \"{home['location'] or home['title']}\" "
        "straight away and go on with the report (step 2 of MAINTENANCE REPORTS)."
    )


TICKET_STATUS_WORDS = {"needs_review": "being reviewed by our team", "open": "open - waiting for a fix",
                       "in_progress": "in progress"}


def open_tickets_note(session_id: str) -> str:
    """For the AI (chat.py): the tenant's open maintenance requests, however
    they reported them - web Chat, WhatsApp or the Maintenance tab all file
    tickets under the account's session_id. Lets "any news on my leak?" be
    answered on any channel. Never raises; "" when there's nothing open."""
    if not (db.ENABLED and session_id):
        return ""
    try:
        rows = db._get("maintenance_tickets", {
            "session_id": f"eq.{session_id}", "ticket_status": "in.(needs_review,open,in_progress)",
            "order": "created_at.desc", "limit": "5",
            "select": "issue_type,urgency,ticket_status,summary,message,notes,created_at",
        }) or []
    except Exception as e:
        print(f"Open tickets lookup failed (non-fatal): {e}")
        return ""
    if not rows:
        return ""
    lines = []
    for t in rows:
        what = (t.get("summary") or t.get("message") or "maintenance request").strip().replace("\n", " ")[:160]
        kind = (t.get("issue_type") or "maintenance").replace("_", " ")
        bits = [f"{kind}: {what}", TICKET_STATUS_WORDS.get(t.get("ticket_status"), t.get("ticket_status") or "open")]
        if t.get("urgency"):
            bits.append(f"{t['urgency']} priority")
        if t.get("created_at"):
            bits.append(f"reported {str(t['created_at'])[:10]}")
        if t.get("notes"):
            bits.append(f"latest update from owner/team: {str(t['notes'])[:160]}")
        lines.append("- " + "; ".join(bits))
    return (
        " This tenant's open maintenance requests (reported in Chat, on WhatsApp or on their Maintenance tab - "
        "it's all one account):\n" + "\n".join(lines) + "\n"
        "If they ask about one of these (status, any news, when it'll be fixed), answer from this list - it is NOT a "
        "new report, so don't set intent to maintenance_issue for it. A different problem is a new report as usual. "
        "They can follow every request on their Maintenance tab."
    )


def ticket_view(t: dict) -> dict:
    out = {k: v for k, v in t.items() if k not in ("session_id", "conversation_id")}
    # Where the tenant reported it: a chat conversation (web Chat / WhatsApp,
    # one shared thread) or the Maintenance tab itself (no conversation).
    out["reported_in"] = "chat" if t.get("conversation_id") else "maintenance_tab"
    return out


# ---------------------------------------------------------------------
# Purchase requests (a buyer investor -> the investor who listed the home)
# ---------------------------------------------------------------------

def purchase_view(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k not in ("tenant_session_id", "owner_session_id")}
    d = row.get("details") or {}
    out.update(kind=PURCHASE_KIND, has_owner_step=True, has_details=True,
               status_text=SALE_CANCELLED_TEXT if d.get("sale_cancelled_at") and row.get("status") == "owner_declined"
               else PURCHASE_STATUS_TEXT.get(row.get("status"), row.get("status")),
               summary={"headline": d.get("headline") or "", "flags": [], "lines": d.get("lines") or []})
    return out


def purchase_headline(financing: str, offer_price, closing: Optional[str]) -> str:
    bits = [{"cash": "Cash buyer", "mortgage": "Buying with a mortgage"}.get(financing, "Financing not decided")]
    if offer_price:
        bits.append(f"price in mind ${float(offer_price):,.0f}")
    if closing:
        bits.append(f"closing {closing}")
    return " · ".join(bits)


def create_purchase_request(buyer: dict, prop: dict, details: dict, message: str = "") -> Optional[dict]:
    """File (or refresh) a buyer's purchase request with the investor who
    listed this home, so it shows on their Tenants tab like a rental
    application. None when nobody owns the home, the buyer owns it
    themselves, or the home isn't a live sale listing. Never raises."""
    try:
        if not prop or prop.get("status") != "active" or prop.get("listing_type") != "sale":
            return None
        owner = owner_account_for(prop)
        if not owner or owner.get("session_id") == buyer.get("session_id"):
            return None
        details = {**details, "kind": PURCHASE_KIND}
        existing = db._get("rental_applications", {
            "tenant_account_id": f"eq.{buyer['id']}", "property_id": f"eq.{prop['id']}",
            "status": f"in.({','.join(OPEN)})", "select": "*", "limit": "1",
        }) or []
        if existing:
            app = existing[0]
            if app["status"] == "approved":
                return app                      # already agreed - nothing to refresh
            return db._patch("rental_applications", {
                "details": details, "message": message.strip() or None,
                "tenant_phone": (details.get("buyer") or {}).get("phone") or app.get("tenant_phone"),
            }, {"id": f"eq.{app['id']}"}) or app
        buyer_d = details.get("buyer") or {}
        return db._post("rental_applications", {
            "property_id": prop["id"],
            "property_title": prop.get("title"),
            "tenant_account_id": buyer["id"],
            "tenant_session_id": buyer["session_id"],
            "tenant_name": buyer_d.get("full_name") or buyer.get("name"),
            "tenant_email": buyer_d.get("email") or buyer.get("email"),
            "tenant_phone": buyer_d.get("phone"),
            "owner_session_id": owner["session_id"],
            "message": message.strip() or None,
            # Straight to the owner: buyers don't go through tenant screening.
            "status": "team_approved",
            "details": details,
        })
    except Exception as e:
        print(f"Filing the purchase request with the home's owner failed (non-fatal): {e}")
        return None


def withdraw_purchase_request(buyer: dict, property_id: str):
    """The buyer took the home off their selection. Never raises."""
    try:
        db._patch("rental_applications", {"status": "withdrawn"}, {
            "tenant_account_id": f"eq.{buyer['id']}", "property_id": f"eq.{property_id}",
            "status": f"in.({','.join(WAITING)})"})
    except Exception as e:
        print(f"Withdrawing the purchase request failed (non-fatal): {e}")


def ensure_home_for_sale(app: dict):
    """Refuse to agree a sale on a home that has already been sold to
    someone else (their request / application is closed then)."""
    if app["status"] not in OFFER_ACTIVE_APP_STATUSES:
        raise HTTPException(409, "This home is no longer available.")
    prop = one("properties", {"id": f"eq.{app['property_id']}"}) or {}
    if prop.get("status") == "sold":
        raise HTTPException(409, "This home has already been sold.")


def agree_sale(app: dict, offer: Optional[dict] = None):
    """Both sides agreed on the same offer: the buyer's request is approved,
    the home comes off the listings as sold, other buyers' requests close,
    and the home is added to the buyer's own portfolio (Portfolio tab) at
    the agreed price. The owner can Relist it from My listings if the sale
    falls through."""
    fields = {"status": "approved", "owner_decided_at": now_iso()}
    if not is_purchase(app):
        # A tenant's application whose purchase offer the owner approved:
        # reads "agreed sale", not "you rent this home", and remembers the
        # offer so the tenant can download it from their Portfolio.
        fields["details"] = {**(app.get("details") or {}), "sale_agreed": True,
                             "sale_approved_at": now_iso(), "sale_offer_id": (offer or {}).get("id")}
    db._patch("rental_applications", fields, {"id": f"eq.{app['id']}"})
    try:
        db.update_property(app["property_id"], {"status": "sold"})
        properties.clear_cache()
    except Exception as e:
        print(f"Marking home sold failed (non-fatal): {e}")
    try:
        db._patch("rental_applications",
                  {"status": "closed", "team_note": "The home was sold to another buyer."},
                  {"property_id": f"eq.{app['property_id']}", "status": "in.(submitted,team_approved)"})
    except Exception as e:
        print(f"Closing other purchase requests failed (non-fatal): {e}")
    add_to_buyer_portfolio(app, offer)
    sync_seller_portfolio(app)


def sync_seller_portfolio(app: dict):
    """The home is sold: take it out of the seller's Portfolio now (the row
    listing_portfolio.sync() made from their listing), rather than waiting
    for their next Portfolio / My listings load. Never raises."""
    if not app.get("owner_session_id"):
        return
    try:
        seller = one("accounts", {"session_id": f"eq.{app['owner_session_id']}"})
        if seller:
            from src.services import listing_portfolio  # local import - it imports rentals
            listing_portfolio.sync(seller)
    except Exception as e:
        print(f"Removing the sold home from the seller's portfolio failed (non-fatal): {e}")


def accepted_offer(app_id: str) -> Optional[dict]:
    """The application's latest offer if both sides agreed on it, else None.
    Never raises (no tenant_offers table yet = no offers)."""
    try:
        offer = current_offer(app_id)
    except HTTPException:
        return None
    return offer if offer and offer.get("status") == "accepted" else None


def needs_sale_approval(app: dict) -> bool:
    """A tenant accepted a purchase offer (or the owner accepted their
    counter) but the owner hasn't pressed "Approve tenant" yet."""
    d = app.get("details") if isinstance(app.get("details"), dict) else {}
    if is_purchase(app) or d.get("sale_approved_at") or app.get("status") not in ("team_approved", "approved"):
        return False
    if accepted_offer(app["id"]) is None:
        return False
    # A current tenant buying their rented home: home_purchase.py runs it
    # (team checks + inspection) - no owner "Approve tenant" step.
    return not (app.get("status") == "approved" and home_purchase_apps([app["id"]]))


def offer_pdf_bytes(app: dict, offer: dict) -> bytes:
    from src.services import tenant_offer_pdf  # local import - reportlab is heavy
    prop = one("properties", {"id": f"eq.{app['property_id']}"}) or {}
    d = app.get("details") if isinstance(app.get("details"), dict) else {}
    owner = {}
    if app.get("owner_session_id"):
        try:
            owner = one("accounts", {"session_id": f"eq.{app['owner_session_id']}"}) or {}
        except Exception:
            owner = {}
    return tenant_offer_pdf.render(
        reference=f"OFFER-{str(offer['id'])[:8].upper()}",
        property_info={"id": prop.get("id") or app.get("property_id"),
                       "title": prop.get("title") or app.get("property_title"),
                       "address": prop.get("location") or prop.get("area")},
        buyer={"name": app.get("tenant_name"), "email": app.get("tenant_email"), "phone": app.get("tenant_phone")},
        seller={"name": owner.get("name") or prop.get("owner_name")},
        terms=offer.get("terms") or {},
        accepted_at=offer.get("responded_at"),
        approved_at=d.get("sale_approved_at"),
        generated_at=now_iso(),
    )


def pdf_response(data: bytes, name: str):
    from fastapi.responses import Response
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def approved_sale_offer(app: dict) -> Optional[dict]:
    """The offer the owner approved on this application, or None."""
    d = app.get("details") if isinstance(app.get("details"), dict) else {}
    if app.get("status") != "approved" or not d.get("sale_approved_at"):
        return None
    if d.get("sale_offer_id"):
        return one("tenant_offers", {"id": f"eq.{d['sale_offer_id']}"})
    return accepted_offer(app["id"])


def _price(v) -> Optional[float]:
    try:
        return float(str(v).replace("$", "").replace(",", "").strip()) if v not in (None, "") else None
    except ValueError:
        return None


def buyer_investor(account_id: str) -> Optional[dict]:
    """The buyer account's investor profile (the one /me/investor and the
    Portfolio tab read), or None."""
    acct = one("accounts", {"id": f"eq.{account_id}"})
    if not acct:
        return None
    if acct.get("investor_id"):
        row = one("investors", {"id": f"eq.{acct['investor_id']}"})
        if row:
            return row
    from src.services import investor_journey  # local import - investor_journey imports a lot
    return investor_journey.find_open(session_id=acct.get("session_id"))


def buyer_investor_or_create(account_id: str) -> Optional[dict]:
    """Like buyer_investor(), but an investor account with no profile yet
    (its signup-time profile failed to save) gets one - the same one signup
    makes - so the bought home still lands in their Portfolio."""
    investor = buyer_investor(account_id)
    if investor:
        return investor
    acct = one("accounts", {"id": f"eq.{account_id}"})
    if not acct or acct.get("role") not in owner_listings.INVESTOR_ROLES:
        return None  # tenants have no investor portfolio
    try:
        investor_id = accounts.create_investor_for_signup(
            acct.get("name") or "Investor", acct["role"], acct["session_id"],
            1 if acct["role"] == "existing_investor" else 0)
        if investor_id:
            db._patch("accounts", {"investor_id": investor_id}, {"id": f"eq.{acct['id']}"})
            return one("investors", {"id": f"eq.{investor_id}"})
    except Exception as e:
        print(f"Creating the buyer's investor profile failed (non-fatal): {e}")
    return None


def sync_bought_homes(account: dict):
    """Make sure every home this investor account bought through Staybot
    (agreed sale, not cancelled) is in their Portfolio - covers sales agreed
    before this was reliable. add_to_buyer_portfolio() adds each home once
    only, so this is safe to run on every Portfolio load. Never raises."""
    try:
        rows = db._get("rental_applications", {
            "tenant_account_id": f"eq.{account['id']}", "status": "eq.approved", "select": "*"}) or []
        for app in rows:
            if is_purchase(app):
                add_to_buyer_portfolio(app, accepted_offer(app["id"]))
    except Exception as e:
        print(f"Syncing bought homes into the portfolio failed (non-fatal): {e}")


def _portfolio_address(prop: dict, app: dict) -> Optional[str]:
    """The street address if the owner gave one; if they only gave the area
    ("Garner"), the listing title reads better: "5 bedroom house in Garner"."""
    location = str(prop.get("location") or "").strip()
    area = str(prop.get("area") or "").strip()
    title = str(prop.get("title") or app.get("property_title") or "").replace(" for sale", "").strip()
    if location and location.lower() != area.lower():
        return location
    return title or location or None


def add_to_buyer_portfolio(app: dict, offer: Optional[dict] = None) -> Optional[dict]:
    """Record the bought home in the buyer's portfolio as "acquired" (counted
    in their Portfolio totals). Only facts we have: the listing, the agreed
    price, and what the buyer told us in their buyer checklist. Anything
    unknown is left blank, so the Portfolio tab shows it as missing instead
    of guessing. Once per buyer + home. Never raises."""
    try:
        investor = buyer_investor_or_create(app["tenant_account_id"])
        if not investor:
            print("Adding the bought home to the buyer's portfolio skipped: no investor profile for this account.")
            return None
        existing = db._get("investor_portfolio_properties", {
            "investor_id": f"eq.{investor['id']}", "property_id": f"eq.{app['property_id']}", "select": "id", "limit": "1"})
        if existing:
            return existing[0]
        prop = one("properties", {"id": f"eq.{app['property_id']}"}) or {}
        d = app.get("details") or {}
        buyer = d.get("buyer") or {}
        price = _price(((offer or {}).get("terms") or {}).get("price")) or _price(d.get("offer_price")) or _price(prop.get("sale_price"))

        mortgage, note = None, None
        if d.get("financing") == "cash":
            mortgage = 0
        elif price and d.get("financing") == "mortgage" and buyer.get("down_payment_percent") is not None:
            mortgage = round(price * (1 - float(buyer["down_payment_percent"]) / 100), 2)
            note = f"Mortgage estimated from your {buyer['down_payment_percent']:g}% down payment."
        elif d.get("financing") == "mortgage":
            note = "Bought with a mortgage - add the loan balance to see accurate equity."

        row = {
            "investor_id": investor["id"],
            "relationship": "acquired",
            "address": _portfolio_address(prop, app),
            "property_type": prop.get("property_type"),
            "bedrooms": prop.get("bedrooms"),
            "bathrooms": prop.get("bathrooms"),
            "purchase_price": price,
            "estimated_value": price,
            "outstanding_mortgage": mortgage,
            "monthly_rent": _price(buyer.get("expected_rent")),
            "property_id": app["property_id"],
            "condition_notes": " ".join(x for x in (f"Bought through Staybot from the listing \"{prop.get('title') or app.get('property_title')}\".", note) if x),
        }
        saved = db._post("investor_portfolio_properties", {k: v for k, v in row.items() if v is not None})
        try:
            db._post("investor_activity", {"investor_id": investor["id"], "actor": "system",
                                           "action": "portfolio_property_added",
                                           "details": {"address": row["address"], "source": "purchase_agreed"}})
        except Exception:
            pass
        # A New Property Investor who just got their first home is an
        # Existing Property Investor now (role + one-time welcome page).
        try:
            from src.services import investor_journey  # local import - investor_journey imports a lot
            investor_journey.graduate_to_existing_investor(investor["id"], "system")
        except Exception as e:
            print(f"Graduating the buyer to existing investor failed (non-fatal): {e}")
        return saved
    except Exception as e:
        print(f"Adding the bought home to the buyer's portfolio failed (non-fatal): {e}")
        return None


buyer_router = APIRouter(prefix="/me/buying", tags=["Buying - buyer investor"])


@buyer_router.get("")
def my_purchases(request: Request):
    """Homes this investor asked to buy from other investors, with the
    owners' purchase offers (Purchase offers tab in /ui)."""
    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    rows = db._get("rental_applications", {
        "tenant_account_id": f"eq.{account['id']}", "order": "updated_at.desc", "select": "*",
    }) or []
    return {"requests": with_offers([purchase_view(r) for r in rows if is_purchase(r)])}


# ---------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------

@tenant_router.get("")
def my_rentals(request: Request):
    account = require_role(request, "tenant")
    rows = db._get("rental_applications", {
        "tenant_account_id": f"eq.{account['id']}", "order": "created_at.desc", "select": "*",
    })
    from src.services import home_purchase  # local import - home_purchase imports this module
    home = tenant_home(account["id"])
    if home:
        home["buy_request"] = home_purchase.view(home_purchase.for_application(home["application_id"]))
    return {"applications": with_offers([app_view(r) for r in rows or []]), "home": home}


# ---- Tenant onboarding checklist (the "Apply to rent" dialog) ----
# Stored as rental_applications.details (jsonb). Everything the owner and
# team need to decide, in one place. Contact details inside it are hidden
# from the owner until our team has cleared the tenant (DETAIL_CONTACT_KEYS),
# same rule as tenant_email / tenant_phone on the application itself.

EMPLOYMENT = Literal["employed", "self_employed", "unemployed", "student", "retired"]
DETAIL_CONTACT_KEYS = ("phone", "email", "current_address", "landlord_name", "landlord_phone",
                       "emergency_name", "emergency_relationship", "emergency_phone")


class ApplicationDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # 1. About you
    full_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=5, max_length=40)
    email: str = Field(default="", max_length=200)
    # 2. Move-in & lease
    move_in_date: date
    lease_months: int = Field(ge=1, le=60)
    # 3. Household
    adults: int = Field(ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    occupant_names: str = Field(default="", max_length=500)
    has_pets: bool = False
    pet_details: str = Field(default="", max_length=300)
    smoker: bool = False
    vehicles: int = Field(default=0, ge=0, le=10)
    # 4. Work & income
    employment_status: EMPLOYMENT
    employer: str = Field(default="", max_length=120)
    job_title: str = Field(default="", max_length=120)
    time_at_job: str = Field(default="", max_length=60)
    monthly_income: float = Field(ge=0, le=100_000_000)
    other_income: str = Field(default="", max_length=300)
    # 5. Rental history & emergency contact
    current_address: str = Field(default="", max_length=300)
    time_at_address: str = Field(default="", max_length=60)
    landlord_name: str = Field(default="", max_length=120)
    landlord_phone: str = Field(default="", max_length=40)
    reason_for_moving: str = Field(default="", max_length=500)
    evicted_before: bool = False
    emergency_name: str = Field(default="", max_length=120)
    emergency_relationship: str = Field(default="", max_length=60)
    emergency_phone: str = Field(default="", max_length=40)
    # 6. Review
    consent: bool

    @model_validator(mode="after")
    def check(self):
        if not self.consent:
            raise ValueError("Please confirm your details are correct and that we can share them with the owner.")
        if self.move_in_date < date.today():
            raise ValueError("The move-in date can't be in the past.")
        if self.has_pets and not self.pet_details:
            raise ValueError("Tell the owner about your pets (type and how many).")
        return self


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_id: str = Field(min_length=1, max_length=120)
    move_in_date: Optional[str] = Field(default=None, max_length=40)
    message: Optional[str] = Field(default=None, max_length=1000)
    details: Optional[ApplicationDetails] = None


def details_summary(details: dict) -> dict:
    """A one-line headline and things worth the owner's attention, from the
    tenant's onboarding answers plus the listing as it was when they applied.
    Rule-based only - it restates what the tenant entered, nothing inferred."""

    listing = details.get("listing") or {}
    adults, children = details.get("adults") or 0, details.get("children") or 0
    who = f"{adults} adult{'s' if adults != 1 else ''}" + (f", {children} child{'ren' if children != 1 else ''}" if children else "")
    bits = [who]
    if details.get("has_pets"):
        bits.append(f"pets: {details.get('pet_details') or 'yes'}")
    if details.get("move_in_date"):
        try:
            d = date.fromisoformat(details["move_in_date"])
            bits.append(f"move-in {d.day} {d:%b %Y}")
        except (TypeError, ValueError):
            bits.append(f"move-in {details['move_in_date']}")
    if details.get("lease_months"):
        bits.append(f"{details['lease_months']}-month lease")

    ratio = None
    rent, income = listing.get("rent"), details.get("monthly_income")
    if rent and income is not None:
        ratio = round(float(income) / float(rent), 1)

    flags = []
    if details.get("has_pets") and listing.get("pets_allowed") is False:
        flags.append("Has pets - this home is listed as no pets")
    if ratio is not None and ratio < 3:
        flags.append(f"Income is {ratio}x the rent (under the usual 3x)")
    if details.get("evicted_before"):
        flags.append("Says they have been evicted before")
    if details.get("smoker"):
        flags.append("Smoker")
    if details.get("vehicles") and listing.get("parking") is False:
        flags.append(f"{details['vehicles']} vehicle(s) - this home is listed without parking")
    return {"headline": " · ".join(bits), "income_to_rent": ratio, "flags": flags}


@tenant_router.post("/applications")
def apply(body: ApplyIn, request: Request):
    account = require_role(request, "tenant")
    prop = one("properties", {"id": f"eq.{body.property_id}"})
    if not prop or prop.get("status") != "active":
        raise HTTPException(404, "This home isn't available any more.")
    if prop.get("listing_type") != "rent":
        raise HTTPException(400, "This home is for sale, not rent - ask about it in the chat instead.")

    existing = db._get("rental_applications", {
        "tenant_account_id": f"eq.{account['id']}", "property_id": f"eq.{prop['id']}",
        "status": f"in.({','.join(OPEN)})", "select": "id", "limit": "1",
    })
    if existing:
        raise HTTPException(409, "You've already applied for this home.")

    owner = owner_account_for(prop)
    from src.services import portfolio  # local import
    phone = ((portfolio.get_portfolio(account["id"]) or {}).get("details") or {}).get("phone")

    extra = {}
    if body.details:
        d = prepared_details(body.details, account, prop)
        extra["details"] = d
        phone = d["phone"] or phone
        body.move_in_date = d["move_in_date"]

    fields = {
        "property_id": prop["id"],
        "property_title": prop.get("title"),
        "tenant_account_id": account["id"],
        "tenant_session_id": account["session_id"],
        "tenant_name": account.get("name"),
        "tenant_email": account.get("email"),
        "tenant_phone": phone,
        "owner_session_id": owner["session_id"] if owner else None,
        "move_in_date": (body.move_in_date or "").strip() or None,
        "message": (body.message or "").strip() or None,
        "status": "submitted",
        **extra,
    }
    try:
        row = db._post("rental_applications", fields)
    except Exception as e:
        body_text = getattr(getattr(e, "response", None), "text", "") or str(e)
        if extra and "details" in body_text:
            raise HTTPException(503, "Applications can't save the onboarding details yet - run the latest "
                                     "supabase_rental_applications.sql in Supabase (it adds the details column).")
        raise
    return app_view(row)


@tenant_router.get("/apply-prefill")
def apply_prefill(property_id: str, request: Request):
    """What the "Apply to rent" checklist can fill in for the tenant: their
    account and screening answers, their last application's answers (so a
    second application isn't typed out again), and the home's rent and rules."""

    account = require_role(request, "tenant")
    from src.services import portfolio  # local import
    screening = (portfolio.get_portfolio(account["id"]) or {}).get("details") or {}
    last = {}
    try:
        rows = db._get("rental_applications", {
            "tenant_account_id": f"eq.{account['id']}", "order": "created_at.desc", "limit": "10", "select": "details"})
        last = next((r["details"] for r in rows or [] if r.get("details")), None) or {}
    except Exception:
        pass  # details column not added yet, or nothing to reuse
    keep = set(ApplicationDetails.model_fields) - {"move_in_date", "consent"}
    prefill = {k: v for k, v in last.items() if k in keep and v not in (None, "")}
    for k, v in (("full_name", account.get("name")), ("email", account.get("email")),
                 ("phone", screening.get("phone")), ("employment_status", screening.get("employment_status")),
                 ("monthly_income", screening.get("monthly_income"))):
        if v not in (None, "") and k not in prefill:
            prefill[k] = v
    prop = one("properties", {"id": f"eq.{property_id}"}) or {}
    return {"prefill": prefill,
            "listing": {k: prop.get(k) for k in ("rent", "deposit", "pets_allowed", "parking", "available_from")}}


def prepared_details(details: "ApplicationDetails", account: dict, prop: dict) -> dict:
    d = details.model_dump(mode="json")
    d["email"] = d.get("email") or account.get("email") or ""
    from src.services.viewings import tidy_phone   # +1 form, same as WhatsApp numbers
    for key in ("phone", "landlord_phone", "emergency_phone"):
        if d.get(key):
            d[key] = tidy_phone(d[key])
    # The listing as it was when they applied, for the owner's summary.
    d["listing"] = {k: prop.get(k) for k in ("rent", "deposit", "pets_allowed", "parking")}
    return d


class DetailsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    details: ApplicationDetails
    message: Optional[str] = Field(default=None, max_length=1000)


@tenant_router.post("/applications/{app_id}/details")
def complete_application(app_id: str, body: DetailsIn, request: Request):
    """Add (or update) the onboarding checklist on an application that's
    still being reviewed - e.g. one sent before the checklist existed, which
    the owner otherwise sees with no summary."""

    account = require_role(request, "tenant")
    app = get_application(app_id)
    if app["tenant_account_id"] != account["id"]:
        raise HTTPException(404, "Application not found.")
    if app["status"] not in WAITING:
        raise HTTPException(409, "This application has already been decided, so its details can't be changed.")
    prop = one("properties", {"id": f"eq.{app['property_id']}"}) or {}
    d = prepared_details(body.details, account, prop)
    fields = {"details": d, "move_in_date": d["move_in_date"], "tenant_phone": d["phone"] or app.get("tenant_phone")}
    if body.message is not None:
        fields["message"] = body.message.strip() or None
    try:
        saved = db._patch("rental_applications", fields, {"id": f"eq.{app_id}"})
    except Exception as e:
        body_text = getattr(getattr(e, "response", None), "text", "") or str(e)
        if "details" in body_text:
            raise HTTPException(503, "Applications can't save the onboarding details yet - run the latest "
                                     "supabase_rental_applications.sql in Supabase (it adds the details column).")
        raise
    return app_view(saved)


class OfferReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accept", "decline"]
    note: str = Field(default="", max_length=500)


@tenant_router.post("/offers/{offer_id}/respond")
@buyer_router.post("/offers/{offer_id}/respond")
def respond_to_offer(offer_id: str, body: OfferReplyIn, request: Request):
    account = require_role(request, "tenant", *owner_listings.INVESTOR_ROLES)
    offer = one("tenant_offers", {"id": f"eq.{offer_id}"})
    if not offer or offer["tenant_account_id"] != account["id"]:
        raise HTTPException(404, "Offer not found.")
    view = offer_view(offer)
    if view["status"] == "expired":
        raise HTTPException(409, "This offer has expired. Ask the owner to send a new one.")
    if view["waiting_on"] != "tenant":
        raise HTTPException(409, "This offer isn't waiting for your answer. Refresh.")
    app = get_application(offer["application_id"])
    if body.decision == "accept":
        ensure_home_for_sale(app)
    saved = db._patch("tenant_offers", {
        "status": "accepted" if body.decision == "accept" else "declined",
        "tenant_note": body.note.strip() or None, "responded_at": now_iso(),
    }, {"id": f"eq.{offer_id}", "status": "eq.sent"})
    if not saved:
        raise HTTPException(409, "This offer changed before your answer was saved. Refresh.")
    # Investor buyer (purchase request): accepting agrees the sale at once.
    # Current tenant buying the home they rent: home_purchase.py takes it
    # from here (sale agreed -> team checks + inspection -> bought).
    # Applicant not yet renting (team_approved): the acceptance is recorded
    # and the owner then gets an "Approve tenant" button (owner_decision) -
    # that final step takes the home off Homes, opens Maintenance and puts
    # the offer PDF in the tenant's Portfolio.
    if body.decision == "accept" and is_purchase(app):
        agree_sale(app, saved)
    elif body.decision == "accept":
        from src.services import home_purchase  # local import
        home_purchase.on_offer_accepted(app, saved, actor=f"account:{account.get('email') or account['id']}")
    return offer_view(saved)


@tenant_router.post("/offers/{offer_id}/counter")
@buyer_router.post("/offers/{offer_id}/counter")
def counter_offer(offer_id: str, body: OfferIn, request: Request):
    """Modify the owner's purchase offer (or your own counter that's still
    waiting) and send it back. The owner then accepts, declines or modifies."""

    account = require_role(request, "tenant", *owner_listings.INVESTOR_ROLES)
    offer = one("tenant_offers", {"id": f"eq.{offer_id}"})
    if not offer or offer["tenant_account_id"] != account["id"]:
        raise HTTPException(404, "Offer not found.")
    view = offer_view(offer)
    if view["status"] == "expired":
        raise HTTPException(409, "This offer has expired. Ask the owner to send a new one.")
    if view["status"] != "sent":
        raise HTTPException(409, "This offer can't be modified any more. Refresh.")
    app = get_application(offer["application_id"])
    if app["status"] not in OFFER_ACTIVE_APP_STATUSES:
        raise HTTPException(409, "This application is closed, so the offer can't be modified.")
    ensure_sale_not_agreed(app)
    terms = validated_terms(body.terms)
    return offer_view(new_offer_version(app, offer["owner_session_id"], terms, "tenant", body.message))


@tenant_router.post("/applications/{app_id}/withdraw")
@buyer_router.post("/requests/{app_id}/withdraw")
def withdraw(app_id: str, request: Request):
    account = require_role(request, "tenant", *owner_listings.INVESTOR_ROLES)
    app = get_application(app_id)
    if app["tenant_account_id"] != account["id"]:
        raise HTTPException(404, "Application not found.")
    if app["status"] not in WAITING:
        raise HTTPException(409, "Only an application that's still being reviewed can be withdrawn.")
    return app_view(db._patch("rental_applications", {"status": "withdrawn"}, {"id": f"eq.{app_id}"}))


@tenant_router.get("/maintenance")
def my_maintenance(request: Request):
    account = require_role(request, "tenant")
    rows = db._get("maintenance_tickets", {
        "session_id": f"eq.{account['session_id']}", "order": "created_at.desc", "select": "*",
    })
    return {"home": tenant_home(account["id"]), "tickets": [ticket_view(t) for t in rows or []],
            "locked_message": MAINTENANCE_LOCKED}


@tenant_router.post("/maintenance")
async def report_maintenance(
    request: Request,
    description: str = Form(default=""),
    photo: Optional[UploadFile] = File(default=None),
):
    """The Maintenance tab's form - same classification and ticket as a
    report made in the chat, filed against the tenant's rented home."""

    account = require_role(request, "tenant")
    home = tenant_home(account["id"])
    if not home:
        raise HTTPException(403, MAINTENANCE_LOCKED)

    description = (description or "").strip()[:2000]
    image_bytes = content_type = filename = None
    if photo is not None and photo.filename:
        content_type = (photo.content_type or "").lower()
        if content_type not in maintenance.ALLOWED_IMAGE_TYPES:
            raise HTTPException(400, "Upload a JPG, PNG, WEBP or GIF photo.")
        image_bytes = await photo.read()
        if len(image_bytes) > maintenance.MAX_IMAGE_SIZE:
            raise HTTPException(400, "Photos can be up to 10 MB.")
        filename = photo.filename
    if not description and not image_bytes:
        raise HTTPException(400, "Describe the problem (and add a photo if you can).")

    from src.services import portfolio  # local import
    phone = ((portfolio.get_portfolio(account["id"]) or {}).get("details") or {}).get("phone")

    result = {"intent": "maintenance_issue", "tenant_id": home["location"] or home["title"],
              "maintenance_description": description, "response": ""}
    maintenance.handle_maintenance_report(
        result, message=description, property_id=home["property_id"], property_title=home["title"],
        conversation_id=None, session_id=account["session_id"], known_phone=phone,
        known_name=account.get("name"), image_bytes=image_bytes, image_content_type=content_type,
        image_filename=filename,
    )
    ticket = result.get("maintenance_ticket") or {}
    if not ticket.get("saved"):
        raise HTTPException(500, ticket.get("save_error") or ticket.get("error") or "The request couldn't be saved.")
    return {"ticket": ticket_view(ticket), "message": result.get("response", "").strip()}


@tenant_router.post("/maintenance/chat")
async def maintenance_chat_turn(
    request: Request,
    message: str = Form(default=""),
    history: str = Form(default="[]"),
    ticket_id: str = Form(default=""),
    send_now: bool = Form(default=False),
    photo: Optional[UploadFile] = File(default=None),
):
    """One turn of the Maintenance tab's chat with the AI assistant
    (src/services/maintenance_chat.py). When the AI can't sort it out - or
    it's urgent, or the tenant presses "Send to owner & team" (send_now) -
    it becomes a ticket for the home's owner and the team, with an AI
    summary and the transcript. ticket_id: that ticket, once created, so
    later messages keep it up to date."""

    import json as _json
    from src.services import maintenance_chat, portfolio  # local imports

    account = require_role(request, "tenant")
    home = tenant_home(account["id"])
    if not home:
        raise HTTPException(403, MAINTENANCE_LOCKED)

    message = (message or "").strip()[:2000]
    image_bytes = content_type = filename = None
    if photo is not None and photo.filename:
        content_type = (photo.content_type or "").lower()
        if content_type not in maintenance.ALLOWED_IMAGE_TYPES:
            raise HTTPException(400, "Upload a JPG, PNG, WEBP or GIF photo.")
        image_bytes = await photo.read()
        if len(image_bytes) > maintenance.MAX_IMAGE_SIZE:
            raise HTTPException(400, "Photos can be up to 10 MB.")
        filename = photo.filename
    try:
        past = maintenance_chat.clean_history(_json.loads(history or "[]"))
    except ValueError:
        past = []
    if not message and not image_bytes and not (send_now and past):
        raise HTTPException(400, "Type a message (or add a photo).")

    ticket = None
    if ticket_id:
        ticket = one("maintenance_tickets", {"id": f"eq.{ticket_id}"})
        if not ticket or ticket.get("session_id") != account["session_id"]:
            ticket = None  # stale/foreign id from the browser: start a new ticket if needed

    try:
        if message or image_bytes:
            turn = maintenance_chat.ask_ai(home, past, message, image_bytes, content_type)
        else:
            # "Send to owner & team" with nothing new typed: just summarise.
            turn = maintenance_chat.ask_ai(home, past, "Please send this to my owner and the team now.")
    except Exception as e:
        print(f"Maintenance chat AI failed: {e}")
        if not send_now:
            raise HTTPException(503, "The assistant is busy right now - try again, or press \"Send to owner & team\".")
        turn = {"reply": "", "action": "escalate", "emergency": False, "issue_type": None, "urgency": None,
                "escalation_reason": "Tenant asked to send it", "summary": ""}

    if send_now:
        turn["action"] = "escalate"
        turn["escalation_reason"] = turn.get("escalation_reason") or "Tenant asked to send it to the owner and team"

    transcript = past + ([{"role": "tenant", "content": message or "[photo]", **({"photo": True} if image_bytes else {})}]
                         if (message or image_bytes) else [])
    if turn["reply"]:
        transcript.append({"role": "assistant", "content": turn["reply"]})

    escalated_now = False
    if ticket:
        ticket = maintenance_chat.update_escalated(ticket["id"], turn, transcript)
    elif turn["action"] == "escalate":
        phone = ((portfolio.get_portfolio(account["id"]) or {}).get("details") or {}).get("phone")
        try:
            ticket = maintenance_chat.escalate(turn, transcript, home, account, phone,
                                               image_bytes, content_type, filename)
            escalated_now = True
        except Exception as e:
            print(f"Maintenance escalation failed: {e}")
            raise HTTPException(500, f"Couldn't send it to your owner: {e}")
        if not turn["reply"]:
            turn["reply"] = "I've sent this to your owner and our team with a summary of our chat. They'll follow up with you."
            transcript.append({"role": "assistant", "content": turn["reply"]})

    return {
        "reply": turn["reply"],
        "action": turn["action"],
        "emergency": turn["emergency"],
        "urgency": (ticket or {}).get("urgency") or turn["urgency"],
        "summary": turn["summary"],
        "escalated_now": escalated_now,
        "ticket": ticket_view(ticket) if ticket else None,
    }


# ---------------------------------------------------------------------
# Owner (the investor who listed the home)
# ---------------------------------------------------------------------

def owner_apps(account: dict, statuses=OWNER_VISIBLE) -> list[dict]:
    return db._get("rental_applications", {
        "owner_session_id": f"eq.{account['session_id']}",
        "status": f"in.({','.join(statuses)})",
        "order": "updated_at.desc", "select": "*",
    }) or []


def own_app(account: dict, app_id: str) -> dict:
    app = get_application(app_id)
    if app.get("owner_session_id") != account["session_id"] or app["status"] not in OWNER_VISIBLE:
        raise HTTPException(404, "Application not found.")
    return app


@owner_router.get("/applications")
def owner_applications(request: Request):
    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    rows = owner_apps(account)
    return {"applications": with_buy_requests(with_offers([app_view(r, with_screening=r["status"] in ("team_approved", "approved"),
                                                  with_contact=r["status"] in TEAM_CLEARED) for r in rows])),
            "waiting": sum(1 for r in rows if r["status"] == "team_approved"),
            "in_review": sum(1 for r in rows if r["status"] == "submitted")}


@owner_router.get("/inquiries")
def owner_inquiries(request: Request):
    """"Enquire about this" enquiries about the homes this investor listed
    (src/services/inquiries.py) - for the Enquiries list on their Tenants tab."""
    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    from src.services import inquiries  # local import
    try:
        rows = inquiries.for_owner(account)
    except Exception as e:
        print(f"Loading the owner's enquiries failed (non-fatal): {e}")
        rows = []
    return {"inquiries": rows, "unseen": sum(1 for r in rows if r["is_new"])}


@owner_router.post("/inquiries/seen")
def owner_inquiries_seen(request: Request):
    """The owner opened their Enquiries list - clear the badge."""
    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    from src.services import inquiries  # local import
    try:
        return {"marked": inquiries.mark_seen_by_owner(account)}
    except Exception as e:
        # No owner_seen_at column yet (supabase_property_inquiries.sql not re-run).
        print(f"Marking enquiries seen failed (non-fatal): {e}")
        return {"marked": 0}


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "decline"]
    note: str = Field(default="", max_length=500)


@owner_router.post("/applications/{app_id}/decision")
def owner_decision(app_id: str, body: DecisionIn, request: Request):
    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    app = own_app(account, app_id)
    if is_purchase(app) and body.decision == "approve":
        raise HTTPException(409, "Send this buyer a purchase offer instead - the sale is agreed once they accept it.")
    if app["status"] == "submitted":
        raise HTTPException(409, "Our team is still reviewing this tenant - you can approve or decline once they have.")
    pending_sale = app["status"] == "approved" and body.decision == "approve" and needs_sale_approval(app)
    if app["status"] != "team_approved" and not pending_sale:
        raise HTTPException(409, "This application isn't waiting for your decision. Refresh.")
    if body.decision == "approve":
        ensure_home_free(app)

    db._patch("rental_applications", {"owner_note": body.note.strip() or None, "owner_decided_at": now_iso()},
              {"id": f"eq.{app_id}"})
    offer = accepted_offer(app_id) if body.decision == "approve" else None
    if offer:
        # The tenant and owner agreed a purchase offer: approving makes it
        # final. The home comes off Homes (sold) and is assigned to this
        # tenant (Maintenance opens), and the offer PDF appears in their
        # Portfolio (GET /me/rentals/documents).
        agree_sale(app, offer)
        saved = get_application(app_id)
    elif body.decision == "decline":
        saved = db._patch("rental_applications", {"status": "owner_declined"}, {"id": f"eq.{app_id}"})
    else:
        saved = approve_final(app)
    return app_view(saved, with_screening=True)


@owner_router.post("/applications/{app_id}/end")
def owner_end_tenancy(app_id: str, request: Request):
    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    app = own_app(account, app_id)
    if app["status"] != "approved":
        raise HTTPException(409, "Only a current tenancy can be ended.")
    saved = db._patch("rental_applications", {"status": "ended", "ended_at": now_iso()}, {"id": f"eq.{app_id}"})
    from src.services import home_purchase  # local import
    home_purchase.on_tenancy_ended(app)
    return app_view(saved)


def offer_db_error(e: Exception):
    text = getattr(getattr(e, "response", None), "text", "") or str(e)
    if "tenant_offers" in text or "PGRST205" in text:
        raise HTTPException(503, "Purchase offers to tenants need the latest supabase_rental_applications.sql "
                                 "run in Supabase (it adds the tenant_offers table).")
    raise e


@owner_router.post("/applications/{app_id}/offer")
def owner_send_offer(app_id: str, body: OfferIn, request: Request):
    """Send the tenant a purchase offer with the staff page's terms - or
    modify the tenant's counter-offer. Replaces whatever is waiting."""

    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    app = own_app(account, app_id)
    if app["status"] not in OFFER_ACTIVE_APP_STATUSES:
        raise HTTPException(409, "You can only send an offer on an open application.")
    ensure_sale_not_agreed(app)
    terms = validated_terms(body.terms)
    sent = new_offer_version(app, account["session_id"], terms, "owner", body.message)
    from src.services import home_purchase  # local import
    home_purchase.on_owner_offer(app, account)
    return offer_view(sent)


def ensure_sale_not_agreed(app: dict):
    """Once the tenant and owner agree a price for the rented home, the
    offers are settled - the team's checks decide the rest."""
    if is_purchase(app):
        return
    d = app.get("details") if isinstance(app.get("details"), dict) else {}
    if d.get("sale_approved_at"):
        raise HTTPException(409, "The sale of this home is already approved.")
    from src.services import home_purchase  # local import
    req = home_purchase.for_application(app["id"])
    if req and req["status"] in ("sale_agreed", "completed"):
        raise HTTPException(409, "The price for this home is already agreed - our team is finishing the purchase.")


class OwnerOfferReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accept", "decline"]
    note: str = Field(default="", max_length=500)


@owner_router.post("/applications/{app_id}/offer/respond")
def owner_respond_to_counter(app_id: str, body: OwnerOfferReplyIn, request: Request):
    """Accept or decline the tenant's counter-offer."""

    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    app = own_app(account, app_id)
    offer = current_offer(app_id)
    view = offer_view(offer) if offer else None
    if not view or view["waiting_on"] != "owner":
        if view and view["status"] == "expired":
            raise HTTPException(409, "This counter-offer has expired. Send the tenant a new offer instead.")
        raise HTTPException(409, "There's no counter-offer waiting for your answer. Refresh.")
    if body.decision == "accept":
        ensure_home_for_sale(app)
    saved = db._patch("tenant_offers", {
        "status": "accepted" if body.decision == "accept" else "declined",
        "owner_note": body.note.strip() or None, "responded_at": now_iso(),
    }, {"id": f"eq.{offer['id']}", "status": "eq.sent"})
    if not saved:
        raise HTTPException(409, "This offer changed before your answer was saved. Refresh.")
    # Investor buyer (purchase request): accepting agrees the sale at once.
    # Current tenant buying the home they rent: home_purchase.py takes it
    # from here (sale agreed -> team checks + inspection -> bought).
    # Applicant not yet renting (team_approved): the acceptance is recorded
    # and the owner then gets an "Approve tenant" button (owner_decision) -
    # that final step takes the home off Homes, opens Maintenance and puts
    # the offer PDF in the tenant's Portfolio.
    if body.decision == "accept" and is_purchase(app):
        agree_sale(app, saved)
    elif body.decision == "accept":
        from src.services import home_purchase  # local import
        home_purchase.on_offer_accepted(app, saved, actor=f"owner:{account.get('email') or account['id']}")
    return offer_view(saved)


@owner_router.post("/applications/{app_id}/offer/withdraw")
def owner_withdraw_offer(app_id: str, request: Request):
    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    own_app(account, app_id)
    try:
        saved = db._patch("tenant_offers", {"status": "withdrawn"},
                          {"application_id": f"eq.{app_id}", "status": "eq.sent", "from_party": "eq.owner"})
    except Exception as e:
        offer_db_error(e)
    if not saved:
        raise HTTPException(409, "There's no offer of yours waiting for the tenant. Refresh.")
    return offer_view(saved)


def owner_tenant_pairs(account: dict) -> set[tuple[str, str]]:
    """(tenant session, home) for every tenant this owner has approved -
    current or past. Maintenance tickets are shown to the owner only when
    they come from one of these, so an owner never sees anyone else's."""
    rows = db._get("rental_applications", {
        "owner_session_id": f"eq.{account['session_id']}", "status": "in.(approved,ended)",
        "select": "tenant_session_id,property_id,details",
    }) or []
    return {(r["tenant_session_id"], r["property_id"]) for r in rows if not is_purchase(r)}


@owner_router.get("/maintenance")
def owner_maintenance(request: Request):
    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    pairs = owner_tenant_pairs(account)
    if not pairs:
        return {"tickets": []}
    homes = sorted({p for _, p in pairs})
    rows = db._get("maintenance_tickets", {
        "property_id": f"in.({','.join(homes)})", "order": "created_at.desc", "select": "*",
    }) or []
    return {"tickets": [ticket_view(t) for t in rows if (t.get("session_id"), t.get("property_id")) in pairs]}


class OwnerTicketIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_status: Literal["open", "in_progress", "resolved"]
    notes: Optional[str] = Field(default=None, max_length=1000)


@owner_router.patch("/maintenance/{ticket_id}")
def owner_update_ticket(ticket_id: str, body: OwnerTicketIn, request: Request):
    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    ticket = one("maintenance_tickets", {"id": f"eq.{ticket_id}"})
    if not ticket or (ticket.get("session_id"), ticket.get("property_id")) not in owner_tenant_pairs(account):
        raise HTTPException(404, "Maintenance request not found.")
    fields = {"ticket_status": body.ticket_status}
    if body.notes is not None:
        fields["notes"] = body.notes.strip() or None
    return ticket_view(maintenance.update_ticket(ticket_id, fields) or {**ticket, **fields})


# ---------------------------------------------------------------------
# Team (staff-only: /applications is in team_auth.STAFF_ONLY_PREFIXES)
# ---------------------------------------------------------------------

def team_actor(request: Request, x_staybot_staff: Optional[str]) -> str:
    name = (x_staybot_staff or "").strip()
    if name:
        return f"staff:{name}"
    acct = accounts.current_account(request)
    return f"admin:{acct.get('email')}" if acct else "team"


@team_router.get("")
def team_applications(status: Optional[str] = None):
    configured()
    params = {"order": "created_at.desc", "limit": "300", "select": "*"}
    if status:
        params["status"] = f"in.({status})"
    rows = db._get("rental_applications", params) or []
    # Which investor account each home belongs to - so the team can see where
    # an application goes next (or that the home has no owner account).
    owner_sessions = sorted({r["owner_session_id"] for r in rows if r.get("owner_session_id")})
    names = {}
    if owner_sessions:
        try:
            names = {a["session_id"]: a.get("name") for a in db._get("accounts", {
                "session_id": f"in.({','.join(owner_sessions)})", "select": "session_id,name"}) or []}
        except Exception as e:
            print(f"Owner name lookup failed (non-fatal): {e}")
    return {"applications": with_buy_requests(with_offers([app_view(r, with_screening=r["status"] in WAITING)
                                         | {"owner_name": names.get(r.get("owner_session_id"))} for r in rows]))}


@team_router.post("/{app_id}/review")
def team_review(app_id: str, body: DecisionIn, request: Request, x_staybot_staff: Optional[str] = Header(default=None)):
    configured()
    app = get_application(app_id)
    if app["status"] != "submitted":
        raise HTTPException(409, "This application was already reviewed. Refresh.")
    if body.decision == "approve" and not app.get("owner_session_id"):
        ensure_home_free(app)

    db._patch("rental_applications", {
        "team_note": body.note.strip() or None,
        "team_reviewed_by": team_actor(request, x_staybot_staff),
        "team_reviewed_at": now_iso(),
    }, {"id": f"eq.{app_id}"})

    if body.decision == "decline":
        saved = db._patch("rental_applications", {"status": "team_declined"}, {"id": f"eq.{app_id}"})
    elif app.get("owner_session_id"):
        saved = db._patch("rental_applications", {"status": "team_approved"}, {"id": f"eq.{app_id}"})
    else:
        # No investor owns this home - the team's approval is the final one.
        saved = approve_final(app)
    return app_view(saved, with_screening=True)


# ---- Accepted purchase offer PDF (after the owner's "Approve tenant") ----

@tenant_router.get("/documents")
def my_documents(request: Request):
    """The tenant's Portfolio "Your documents": one PDF per purchase offer the
    owner approved. The PDF is built from the stored offer on download."""
    account = require_role(request, "tenant")
    rows = db._get("rental_applications", {
        "tenant_account_id": f"eq.{account['id']}", "status": "eq.approved",
        "order": "updated_at.desc", "select": "*",
    }) or []
    docs = []
    for app in rows:
        d = app.get("details") if isinstance(app.get("details"), dict) else {}
        if d.get("sale_approved_at"):
            docs.append({
                "application_id": app["id"],
                "title": f"Accepted purchase offer - {app.get('property_title') or 'your home'}",
                "property_title": app.get("property_title"),
                "approved_at": d["sale_approved_at"],
                "url": f"/me/rentals/applications/{app['id']}/offer.pdf",
            })
    return {"documents": docs}


@tenant_router.get("/applications/{app_id}/offer.pdf")
def my_offer_pdf(app_id: str, request: Request):
    account = require_role(request, "tenant")
    app = get_application(app_id)
    if app.get("tenant_account_id") != account["id"]:
        raise HTTPException(404, "Document not found.")
    offer = approved_sale_offer(app)
    if not offer:
        raise HTTPException(404, "This offer PDF is available once the owner approves the accepted offer.")
    return pdf_response(offer_pdf_bytes(app, offer), f"purchase-offer-{app_id[:8]}.pdf")


@owner_router.get("/applications/{app_id}/offer.pdf")
def owner_offer_pdf(app_id: str, request: Request):
    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    app = own_app(account, app_id)
    offer = approved_sale_offer(app)
    if not offer:
        raise HTTPException(404, "The offer PDF is available once you approve the accepted offer.")
    return pdf_response(offer_pdf_bytes(app, offer), f"purchase-offer-{app_id[:8]}.pdf")


# ---- Owner cancels an agreed sale ----
# The owner agreed a sale (investor buyer's purchase request, or a tenant's
# approved purchase offer) but it isn't going ahead. "Cancel sale" on the
# Tenants tab undoes it: the home goes back on Homes, the buyer's request
# shows "Sale cancelled by the owner" (with the owner's reason), the offer is
# marked withdrawn, and the home comes out of the buyer's Portfolio and
# selected homes - so they can select it and ask again if they want to.

class CancelSaleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=500)


def is_agreed_sale(app: dict) -> bool:
    d = app.get("details") if isinstance(app.get("details"), dict) else {}
    return app.get("status") == "approved" and (is_purchase(app) or bool(d.get("sale_agreed")))


def remove_from_buyer(app: dict):
    """Undo what agree_sale() gave the buyer. Never raises."""
    investor = None
    try:
        investor = buyer_investor(app["tenant_account_id"])
    except Exception as e:
        print(f"Buyer lookup failed (non-fatal): {e}")
    if not investor:
        return  # e.g. a tenant account - no investor portfolio to update
    try:
        db._delete("investor_portfolio_properties", {
            "investor_id": f"eq.{investor['id']}", "property_id": f"eq.{app['property_id']}",
            "relationship": "eq.acquired"})
    except Exception as e:
        print(f"Removing the cancelled home from the buyer's portfolio failed (non-fatal): {e}")
    try:
        from src.services import investor_acquisition  # local import - it imports rentals
        db._post("investor_activity", {"investor_id": investor["id"], "actor": "system",
                                       "action": investor_acquisition.UNSELECT_ACTION,
                                       "details": {"list_number": app["property_id"], "title": app.get("property_title"),
                                                   "reason": "sale_cancelled_by_owner"}})
    except Exception as e:
        print(f"Clearing the buyer's home selection failed (non-fatal): {e}")


@owner_router.post("/applications/{app_id}/cancel-sale")
def owner_cancel_sale(app_id: str, body: CancelSaleIn, request: Request):
    account = require_role(request, *owner_listings.INVESTOR_ROLES)
    app = own_app(account, app_id)
    if not is_agreed_sale(app):
        raise HTTPException(409, "There's no agreed sale to cancel here. Refresh.")

    # Back on the Homes tab first - if that fails, nothing else changes.
    try:
        db.update_property(app["property_id"], {"status": "active"})
    except Exception as e:
        raise HTTPException(500, f"Couldn't put the home back on the Homes tab, so the sale wasn't cancelled. Try again. ({e})")
    properties.clear_cache()

    reason = body.reason.strip()
    d = app.get("details") if isinstance(app.get("details"), dict) else {}
    saved = db._patch("rental_applications", {
        "status": "owner_declined",
        "owner_note": reason or "The owner cancelled the sale.",
        "owner_decided_at": now_iso(),
        "details": {**d, "sale_agreed": False, "sale_cancelled_at": now_iso()},
    }, {"id": f"eq.{app_id}"})
    try:
        db._patch("tenant_offers", {"status": "withdrawn", "owner_note": reason or "Sale cancelled by the owner."},
                  {"application_id": f"eq.{app_id}", "status": "eq.accepted"})
    except Exception as e:
        print(f"Marking the accepted offer withdrawn failed (non-fatal): {e}")
    remove_from_buyer(app)
    sync_seller_portfolio(app)  # listing is active again -> back in their Portfolio
    return app_view(saved or {**app, "status": "owner_declined"})
