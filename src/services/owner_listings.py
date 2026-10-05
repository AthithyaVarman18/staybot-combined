"""
Owner listings added through the chat.

When an owner describes their property, the LLM fills
result["property_details"]. Once there is enough to describe the home
(type or bedrooms, location, and a rent or sale price), this module saves
it to the `properties` table as a 'pending' draft and keeps it updated as
the owner adds details. Drafts are never shown to tenants: a person
approves them (status -> 'active') from the New listings tab.
"""

import os
import re
import uuid

from src.prompts.system_prompt import CURRENCY, MARKET
from src.services import db, listing_checks, property_photos
from src.services.properties import parse_int, parse_money, price_label
from src.services.viewings import append_line, normalize_phone

# Where a listing is assumed to be when the owner never says the area.
DEFAULT_AREA = MARKET.split("(")[0].split(",")[0].strip() or "your area"

# Homes listed by a logged-in New / Existing Property Investor account go
# live for tenants straight away (they're registered, identifiable owners),
# instead of waiting in the team's "New listings" review queue like an
# anonymous owner's chat draft. Set INVESTOR_LISTINGS_NEED_REVIEW=true to
# send investor listings through that review queue as well.
INVESTOR_ROLES = {"new_investor", "existing_investor"}
INVESTOR_LISTINGS_NEED_REVIEW = (os.getenv("INVESTOR_LISTINGS_NEED_REVIEW") or "").strip().lower() in ("1", "true", "yes")


def publishes_immediately(account_role: str = None) -> bool:
    return account_role in INVESTOR_ROLES and not INVESTOR_LISTINGS_NEED_REVIEW


def to_bool(value):
    if isinstance(value, bool):
        return value

    text = str(value or "").strip().lower()

    if text in ["yes", "true", "allowed", "available", "y"]:
        return True

    if text in ["no", "false", "not allowed", "not available", "n", "none"]:
        return False

    return None


def clean_text(value):
    text = str(value or "").strip()
    return text if text and text.lower() not in ["null", "none", "unknown", "n/a"] else None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:30] or "home"


def area_from(location: str) -> str:
    """'Downtown Garner, NC' -> 'Downtown Garner'."""
    return clean_text(str(location or "").split(",")[0]) or DEFAULT_AREA


def normalized_details(details: dict) -> dict:
    """The owner's details in the properties table's column format.
    Only values the owner actually gave are included."""

    d = details or {}

    rent = parse_money(d.get("rent"))
    sale_price = parse_money(d.get("sale_price"))
    location = clean_text(d.get("location"))

    amenities = d.get("amenities")
    if isinstance(amenities, str):
        amenities = [a.strip() for a in amenities.split(",")]
    amenities = [str(a).strip() for a in (amenities or []) if clean_text(a)]

    fields = {
        "property_type": clean_text(d.get("property_type")),
        "bedrooms": parse_int(d.get("bedrooms")),
        "bathrooms": parse_int(d.get("bathrooms")),
        "location": location,
        "area": area_from(location) if location else None,
        "listing_type": "sale" if sale_price and not rent else ("rent" if rent else None),
        "rent": int(rent) if rent else None,
        "sale_price": int(sale_price) if sale_price else None,
        "deposit": int(parse_money(d.get("deposit"))) if parse_money(d.get("deposit")) else None,
        "pets_allowed": to_bool(d.get("pets_allowed")),
        "parking": to_bool(d.get("parking")),
        "furnished": clean_text(d.get("furnished")),
        "amenities": amenities or None,
        "available_from": clean_text(d.get("available_from")),
        "description": clean_text(d.get("property_description")),
        "owner_name": clean_text(d.get("owner_name")),
        "owner_phone": normalize_phone(d.get("owner_phone")),
    }

    return {key: value for key, value in fields.items() if value is not None}


def missing_for_draft(fields: dict) -> list[str]:
    missing = []

    if not (fields.get("property_type") or fields.get("bedrooms")):
        missing.append("property type or bedrooms")
    if not fields.get("location"):
        missing.append("location")
    if not (fields.get("rent") or fields.get("sale_price")):
        missing.append("rent or sale price")

    return missing


def make_title(fields: dict) -> str:
    # "3 bedroom house for rent in Garner" in the US, "3BHK flat for rent in OMR" in India.
    beds = fields.get("bedrooms")
    size = (f"{beds} bedroom " if CURRENCY == "USD" else f"{beds}BHK ") if beds else ""
    kind = fields.get("property_type") or "home"
    if size and kind.lower() in ["flat", "apartment"]:
        kind = "apartment" if CURRENCY == "USD" else "flat"
    verb = "for sale" if fields.get("listing_type") == "sale" else "for rent"
    title = f"{size}{kind} {verb} in {fields.get('area') or DEFAULT_AREA}".strip()
    return title[0].upper() + title[1:]


FIELD_LABELS = {
    "property_type": "type", "bedrooms": "bedrooms", "bathrooms": "bathrooms",
    "location": "location", "rent": "rent", "sale_price": "price", "deposit": "deposit",
    "pets_allowed": "pets", "parking": "parking", "furnished": "furnishing",
    "amenities": "amenities", "available_from": "availability", "description": "description",
    "owner_name": "your name", "owner_phone": "phone", "photos": "photos",
}


def handle_owner_listing(
    result: dict,
    conversation_id: str = None,
    session_id: str = None,
    known_phone: str = None,
    known_name: str = None,
    image_bytes: bytes = None,
    image_content_type: str = None,
    account_role: str = None,
):
    """
    Save or update the owner's draft listing from this turn's analysis.
    account_role: the logged-in account's role, if any - investor accounts'
    listings are published for tenants immediately (publishes_immediately()).
    Adds result["owner_listing"] and appends one line to the reply when
    the draft is created, changed, or a photo is attached.

    image_bytes/image_content_type: an optional photo the owner attached
    to this message. Only used while result["role"] is "owner" (the AI's
    own call, from src/prompts/system_prompt.py) - a tenant's photo is
    handled separately, by src/services/maintenance.py.
    """

    if str(result.get("role") or "").lower() != "owner":
        return

    fields = normalized_details(result.get("property_details"))

    if not fields.get("owner_phone") and normalize_phone(known_phone):
        fields["owner_phone"] = normalize_phone(known_phone)

    if not fields.get("owner_name") and clean_text(known_name):
        fields["owner_name"] = clean_text(known_name)

    listing = {"saved": False, "missing": missing_for_draft(fields)}
    result["owner_listing"] = listing

    if not (conversation_id or session_id):
        return

    publish = publishes_immediately(account_role)

    try:
        draft = db.find_owner_draft(
            conversation_id, session_id,
            statuses=("pending", "active") if publish else ("pending",),
        )
    except Exception as e:
        print(f"Owner draft lookup failed: {e}")
        listing["error"] = "Owner listings can't be saved yet. Run supabase_owner_listings.sql in Supabase."
        return

    if not draft and listing["missing"]:
        # Not enough to describe the home yet; the AI keeps asking. A
        # photo sent this early would have nothing to attach to, so it's
        # dropped - the AI is told (system_prompt.py) to ask for the
        # core details first and a photo shortly after, which is when
        # this normally arrives.
        return

    photo_added = False

    if image_bytes:
        try:
            existing_photos = list((draft or {}).get("photos") or [])
            url = property_photos.save(conversation_id or session_id, image_bytes, image_content_type)
            fields["photos"] = existing_photos + [url]
            photo_added = True
        except Exception as e:
            print(f"Owner photo upload failed: {e}")
            listing["photo_error"] = "The photo couldn't be saved. Run supabase_owner_listing_photos.sql in Supabase."

    fields["title"] = make_title({**(draft or {}), **fields})

    # Listing checks (listing_checks.py): a home with no bedrooms, or a price
    # that looks like a typo ($18 rent), is kept as a draft until it's fixed,
    # even for accounts that normally go live straight away.
    checks = listing_checks.check({"listing_type": "rent", **(draft or {}), **fields})
    go_live = publish and checks["ok"]

    try:

        if draft:

            changes = {
                key: value for key, value in fields.items()
                if draft.get(key) != value
            }

            # A rent listing can't also keep an old sale price, and vice versa.
            if changes.get("listing_type") == "rent":
                changes["sale_price"] = None
            elif changes.get("listing_type") == "sale":
                changes["rent"] = None

            # A draft saved before this account could publish directly
            # (or one whose problems are now fixed).
            if go_live and draft.get("status") == "pending":
                changes["status"] = "active"
            # A live listing changed to something that looks wrong: off the
            # tenant side until it's fixed.
            elif publish and not checks["ok"] and draft.get("status") == "active":
                changes["status"] = "pending"

            saved = db.update_property(draft["id"], changes) if changes else draft
            action = "updated" if changes else "unchanged"

        else:

            changes = fields
            saved = db.create_property({
                **fields,
                "id": f"owner-{slug(fields.get('area'))}-{uuid.uuid4().hex[:6]}",
                "listing_type": fields.get("listing_type") or "rent",
                "city": DEFAULT_AREA,
                "status": "active" if go_live else "pending",
                "source": "owner_chat",
                "conversation_id": conversation_id,
                "session_id": session_id,
            })
            action = "created"

    except Exception as e:
        print(f"Saving owner listing failed: {e}")
        listing["error"] = "The listing couldn't be saved. Run supabase_owner_listings.sql in Supabase."
        return

    saved = saved or {}

    # Tenants' listing cards / search read a 30-second cache - clear it so a
    # live listing (or a change to one) shows up for them right away.
    if saved.get("status") == "active" or changes.get("status") == "pending":
        from src.services import properties  # local import, same package
        properties.clear_cache()

    listing.update(
        saved=True,
        action=action,
        id=saved.get("id"),
        status=saved.get("status", "pending"),
        title=saved.get("title"),
        price_label=price_label(saved) if saved else None,
        missing=missing_for_draft(saved),
        fields=saved,  # full row, for callers (e.g. hubspot.py) that need more than the summary above
        problems=checks["problems"],
        warnings=checks["warnings"],
    )

    live = saved.get("status") == "active"
    listing["live"] = live

    if action == "created" and live:
        line = (
            f"🏠 Your listing is live: {listing['title']} - {listing['price_label']}. "
            + ("Tenants and other investors can see it now (investors also find it in Investing > Deals). "
               if saved.get("listing_type") == "sale" else "Tenants and other investors can see it now. ")
            + "You can manage it from the My listings tab."
        )
        if photo_added:
            line += " Photo added - feel free to attach more."
        append_line(result, line)
    elif action == "created" and publish:
        # Would have gone live, but a check failed - say what to fix.
        line = (
            f"📝 Listing draft saved: {listing['title']} - {listing['price_label']}. "
            f"Before it goes live: {listing_checks.problems_text(checks)}"
        )
        if photo_added:
            line += " Photo added - feel free to attach more."
        append_line(result, line)
    elif action == "created":
        line = (
            f"📝 Listing draft saved: {listing['title']} - {listing['price_label']}. "
            "Our team will review it before tenants can see it."
        )
        if photo_added:
            line += " Photo added - feel free to attach more."
        append_line(result, line)
    elif action == "updated":
        added = [
            FIELD_LABELS[key] for key in changes
            if key in FIELD_LABELS and changes[key] is not None and key != "photos"
        ]
        if photo_added:
            added.append("photo")
        if added:
            noun = "Listing" if live else "Listing draft"
            line = f"📝 {noun} updated: {', '.join(dict.fromkeys(added))}."
            if publish and changes.get("status") == "active":
                line += " All checks passed - it's live now and tenants can see it."
            elif publish and not checks["ok"]:
                line += f" Before it goes live: {listing_checks.problems_text(checks)}"
            append_line(result, line)
    elif photo_added:
        # Nothing else changed this turn, but the photo still needs
        # confirming - the AI's own "response" never claims this itself.
        append_line(result, "📷 Photo added to your listing." if live else "📷 Photo added to your listing draft.")
