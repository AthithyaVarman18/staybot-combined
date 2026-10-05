"""
Homes that New / Existing Property Investor accounts list for tenants.

Investors can list a home two ways, and both land in the same `properties`
table the tenant side reads (GET /properties -> the chat's listing cards,
the tenant "Homes" tab and the AI's property search):

  1. In the chat - src/services/owner_listings.py (source 'owner_chat').
  2. The "My listings" tab in /ui - this module (source 'investor_form').

A listing belongs to the account through its fixed session_id (the same id
every chat message from the account is saved under - see accounts.py), so
listings made in the chat and in the form show up together here.

Listings go live for tenants immediately unless INVESTOR_LISTINGS_NEED_REVIEW
is set (see owner_listings.publishes_immediately()), in which case they wait
in the team's "New listings" review queue like any owner's chat draft.
"""

import uuid
from typing import Literal, Optional

import requests

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from src.services import accounts, db, listing_checks, listing_portfolio, owner_listings, properties, property_photos
from src.services.viewings import tidy_phone

router = APIRouter(prefix="/me/listings", tags=["Investor listings"])

SOURCES = ("owner_chat", "investor_form")
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_PHOTOS = 12
# What an investor may set on their own listing. 'active' is only allowed
# when listings don't need review (otherwise only the team can approve).
OWNER_STATUSES = ("active", "hidden", "let", "sold")


def require_investor(request: Request) -> dict:
    account = accounts.current_account(request)
    if not account:
        raise HTTPException(401, "Please log in.")
    if account.get("role") not in owner_listings.INVESTOR_ROLES:
        raise HTTPException(403, "Only property investor accounts can list homes.")
    if not db.ENABLED:
        raise HTTPException(503, "Supabase isn't configured, so listings can't be saved.")
    return account


def current_occupants(property_ids: list[str]) -> dict[str, dict]:
    """property_id -> its approved rental_applications row: the tenant who
    lives there now, or the buyer whose purchase offer was accepted
    (src/services/rentals.py). Never raises - no table yet means nobody."""
    ids = [i for i in property_ids if i]
    if not ids:
        return {}
    try:
        rows = db._get("rental_applications", {
            "property_id": f"in.({','.join(ids)})", "status": "eq.approved",
            "order": "updated_at.desc", "select": "property_id,details",
        }) or []
    except Exception as e:
        print(f"Occupant lookup failed (non-fatal): {e}")
        return {}
    out = {}
    for r in rows:
        out.setdefault(r["property_id"], r)
    return out


def _is_sale(app: dict) -> bool:
    d = app.get("details") if isinstance(app.get("details"), dict) else {}
    return d.get("kind") == "purchase" or bool(d.get("sale_agreed"))


def relist_blocked(p: dict, occupant: Optional[dict], bought: Optional[dict] = None) -> Optional[str]:
    """Why this home can't go back on the Homes tab, or None if it can.
    A sold home is sold for good; a let home is blocked while a tenant
    lives there (after "End tenancy" on the Tenants tab it can be relisted).
    A home this investor BOUGHT from their landlord (home_purchase.py) is
    theirs: they first say on the Portfolio tab whether they live there or
    rent it out."""
    if bought is not None and not occupant:
        if not bought.get("plan"):
            return ("You own this home now - first tell us on your Portfolio tab whether you'll "
                    "live there or rent it out.")
        return None
    if p.get("status") == "sold" or (occupant and _is_sale(occupant)):
        return "This home is already sold, so it can't be relisted."
    if occupant:
        return ("A tenant is already living in this home, so it can't be relisted. "
                "End their tenancy on the Tenants tab first.")
    return None


def view(p: dict, occupant: Optional[dict] = None, bought: Optional[dict] = None) -> dict:
    blocked = relist_blocked(p, occupant, bought)
    return {
        **{k: v for k, v in p.items() if k not in ("conversation_id",)},
        "price_label": properties.price_label(p),
        "visible_to_tenants": p.get("status") == "active",
        "relist_blocked": blocked,  # None, or the reason shown when Relist is clicked
        "checks": listing_checks.check(p),  # problems stop it going live; warnings are tips
    }


def own_listing(account: dict, property_id: str) -> dict:
    rows = db._get("properties", {"id": f"eq.{property_id}", "select": "*", "limit": "1"})
    if not rows or rows[0].get("session_id") != account["session_id"] or rows[0].get("source") not in SOURCES:
        raise HTTPException(404, "Listing not found.")
    return rows[0]


@router.get("")
def my_listings(request: Request):
    account = require_investor(request)
    try:
        rows = db._get("properties", {
            "session_id": f"eq.{account['session_id']}",
            "source": f"in.({','.join(SOURCES)})",
            "order": "created_at.desc",
            "select": "*",
        })
    except Exception as e:
        raise HTTPException(500, f"Could not load your listings (has supabase_owner_listings.sql been run?): {e}")
    rows = rows or []
    listing_portfolio.sync(account, rows)  # rent / price / sold -> Portfolio
    occupants = current_occupants([p["id"] for p in rows if p.get("status") in ("let", "sold", "hidden")])
    # A home sold through Staybot (an investor's purchase request, or a
    # tenant's approved purchase offer) belongs to the buyer now: it leaves
    # the seller's My listings and shows in the buyer's Portfolio instead.
    # The seller can still undo it with "Cancel sale" on the Tenants tab,
    # which brings it back here.
    rows = [p for p in rows if not (occupants.get(p["id"]) and _is_sale(occupants[p["id"]]))]
    bought = listing_portfolio.bought_homes(account) if any(p.get("status") == "sold" for p in rows) else {}
    return {
        "listings": [view(p, occupants.get(p["id"]), bought.get(p["id"])) for p in rows],
        "needs_review": owner_listings.INVESTOR_LISTINGS_NEED_REVIEW,
    }


class ListingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    listing_type: Literal["rent", "sale"] = "rent"
    property_type: Optional[str] = Field(default=None, max_length=60)
    bedrooms: Optional[int] = Field(default=None, ge=0, le=50)
    bathrooms: Optional[int] = Field(default=None, ge=0, le=50)
    location: str = Field(min_length=2, max_length=200)
    rent: Optional[int] = Field(default=None, ge=1, le=10_000_000)
    sale_price: Optional[int] = Field(default=None, ge=1, le=10_000_000_000)
    deposit: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    pets_allowed: Optional[bool] = None
    parking: Optional[bool] = None
    furnished: Optional[str] = Field(default=None, max_length=40)
    available_from: Optional[str] = Field(default=None, max_length=60)
    amenities: Optional[str] = Field(default=None, max_length=500)
    description: Optional[str] = Field(default=None, max_length=2000)
    owner_phone: Optional[str] = Field(default=None, max_length=30)


@router.post("")
def create_listing(body: ListingIn, request: Request):
    account = require_investor(request)

    # Same cleaning/format rules as a listing made in the chat.
    fields = owner_listings.normalized_details({
        "property_type": body.property_type,
        "bedrooms": body.bedrooms,
        "bathrooms": body.bathrooms,
        "location": body.location,
        "rent": body.rent if body.listing_type == "rent" else None,
        "sale_price": body.sale_price if body.listing_type == "sale" else None,
        "deposit": body.deposit,
        "pets_allowed": body.pets_allowed,
        "parking": body.parking,
        "furnished": body.furnished,
        "available_from": body.available_from,
        "amenities": body.amenities,
        "property_description": body.description,
        "owner_name": account.get("name"),
        "owner_phone": tidy_phone(body.owner_phone),
    })
    fields["listing_type"] = body.listing_type

    missing = owner_listings.missing_for_draft(fields)
    if missing:
        raise HTTPException(400, f"Please add: {', '.join(missing)}.")
    # Listing checks: no bedrooms, or a price that looks like a typo ($18 rent).
    problems = listing_checks.check(fields)["problems"]
    if problems:
        raise HTTPException(400, " ".join(problems))

    publish = owner_listings.publishes_immediately(account["role"])
    fields["title"] = owner_listings.make_title(fields)

    try:
        saved = db.create_property({
            **fields,
            "id": f"owner-{owner_listings.slug(fields.get('area'))}-{uuid.uuid4().hex[:6]}",
            "city": owner_listings.DEFAULT_AREA,
            "status": "active" if publish else "pending",
            "source": "investor_form",
            "session_id": account["session_id"],
        })
    except requests.exceptions.ConnectionError as e:
        # Still dropped after db._send()'s retries - a network problem, not the schema.
        print(f"Saving a listing failed - database unreachable: {e}")
        raise HTTPException(503, "We couldn't reach the database just now, so the home wasn't listed. Please press “List this home” again.")
    except Exception as e:
        raise HTTPException(500, f"Could not save the listing (has supabase_owner_listings.sql been run?): {e}")

    properties.clear_cache()
    listing_portfolio.sync(account)  # the new home shows in their Portfolio
    return view(saved or fields)


class ListingStatusIn(BaseModel):
    """Change the status, and/or the rent (rent listing) or sale price
    (sale listing). A new rent/price shows on Homes and in the Portfolio."""
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["active", "hidden", "let", "sold"]] = None
    rent: Optional[int] = Field(default=None, ge=1, le=10_000_000)
    sale_price: Optional[int] = Field(default=None, ge=1, le=10_000_000_000)


@router.patch("/{property_id}")
def update_listing(property_id: str, body: ListingStatusIn, request: Request):
    """Show / hide a listing, or mark it let or sold (which also takes it
    off the tenant side). An investor can't approve their own listing if
    listings need review - only the team can move 'pending' to 'active'."""

    account = require_investor(request)
    listing = own_listing(account, property_id)

    changes = {}
    if body.rent is not None:
        if listing.get("listing_type") == "sale":
            raise HTTPException(400, "This home is for sale - change its sale price instead.")
        changes["rent"] = body.rent
    if body.sale_price is not None:
        if listing.get("listing_type") != "sale":
            raise HTTPException(400, "This home is for rent - change its monthly rent instead.")
        changes["sale_price"] = body.sale_price
    if body.status is None and not changes:
        raise HTTPException(400, "Nothing to change.")
    # A new price that looks like a typo is refused; so is going live with a problem.
    after = listing_checks.check({**listing, **changes})
    price_problems = [p for p in after["problems"] if "looks too" in p]
    if changes and price_problems:
        raise HTTPException(400, " ".join(price_problems))
    if body.status == "active" and after["problems"]:
        raise HTTPException(409, "Fix this before it goes live: " + " ".join(after["problems"]))
    if body.status is None:
        saved = db.update_property(property_id, changes)
        properties.clear_cache()
        listing_portfolio.sync(account)
        return view(saved or {**listing, **changes})

    if body.status == "active" and (
        listing.get("status") == "pending" or owner_listings.INVESTOR_LISTINGS_NEED_REVIEW
    ):
        raise HTTPException(403, "This listing is waiting for our team's review before tenants can see it.")

    # Can't put a sold home, or one a tenant lives in, back on the Homes tab.
    if body.status == "active":
        bought = listing_portfolio.bought_homes(account).get(property_id) if listing.get("status") == "sold" else None
        blocked = relist_blocked(listing, current_occupants([property_id]).get(property_id), bought)
        if blocked:
            raise HTTPException(409, blocked)

    saved = db.update_property(property_id, {**changes, "status": body.status})
    properties.clear_cache()
    listing_portfolio.sync(account)
    return view(saved or {**listing, **changes, "status": body.status})


@router.post("/{property_id}/photos")
async def add_photo(property_id: str, request: Request, photo: UploadFile = File(...)):
    account = require_investor(request)
    listing = own_listing(account, property_id)

    content_type = (photo.content_type or "").lower()
    if content_type not in property_photos.EXTENSION_BY_TYPE:
        raise HTTPException(400, "Upload a JPG, PNG, WEBP or GIF image.")
    data = await photo.read()
    if not data:
        raise HTTPException(400, "The photo is empty.")
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(400, "Photos can be up to 8 MB.")

    photos = list(listing.get("photos") or [])
    if len(photos) >= MAX_PHOTOS:
        raise HTTPException(400, f"A listing can have up to {MAX_PHOTOS} photos.")

    try:
        url = property_photos.save(account["session_id"], data, content_type)
        saved = db.update_property(property_id, {"photos": photos + [url]})
    except Exception as e:
        raise HTTPException(500, f"The photo couldn't be saved (has supabase_owner_listing_photos.sql been run?): {e}")

    properties.clear_cache()
    return view(saved or {**listing, "photos": photos + [url]})
