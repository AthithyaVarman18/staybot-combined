"""
Property inquiries.

Saved when a customer clicks "Enquire about this" on a suggested listing in
the chat and confirms in the enquiry panel. Unlike a viewing request, this
needs no date/time parsing from free text - it's a direct UI action - so
it's a thin save, not a chat-analysis handler like viewings.py.

Owner contact is copied onto the row at save time so staff can follow up
from the Inquiries tab without cross-referencing the properties table.
It's never sent back to the customer - main.py strips it from the create
response the same way /properties strips it from listings.

property_id can point at either source the chat shows listings from:
our own `properties` table (tenant/owner listings), or an MLS listing
(investor "Enquire about this" - see src/services/investors.py). MLS has no
homeowner contact, only the listing agency, so that's what's copied in
as the "owner" contact for those rows - still the right person for staff
to call about that specific home.
"""

from src.services import db, high_priority_inquiry_email, properties
from src.services.viewings import tidy_phone


def create_inquiry(
    property_id: str = None,
    property_title: str = None,
    price_label: str = None,
    area: str = None,
    session_id: str = None,
    listing_id: str = None,
    conversation_id: str = None,
    customer_name: str = None,
    customer_phone: str = None,
    notes: str = None,
):
    if not property_title:
        raise ValueError("property_title is required")

    p = properties.get_property(property_id) if property_id else None
    mls = None if (p or not property_id) else db.get_mls_listing(property_id)

    owner_name = (p or {}).get("owner_name") or ((mls or {}).get("agency_name"))
    owner_phone = tidy_phone((p or {}).get("owner_phone") or ((mls or {}).get("agency_phone")))

    # The button doesn't know the conversation's id (that's server state),
    # only session_id - look it up the same way the chat itself resumes a
    # conversation. Its lead score/status/summary are copied onto this row
    # as a SNAPSHOT of this moment, not read live off conversation_id later
    # - "General enquiry" is one long-running conversation reused for
    # whatever the customer asks about next, so without a snapshot, an old
    # inquiry about one property would silently start showing whatever
    # they're chatting about right now instead of what this enquiry was
    # actually about.
    existing = None
    if not conversation_id and session_id:
        try:
            existing = db.get_conversation(session_id, listing_id)
            conversation_id = existing["id"] if existing else None
        except Exception as error:
            print(f"Resolving conversation for inquiry failed (non-fatal): {error}")

    fields = {
        "property_id": property_id,
        "property_title": property_title,
        "price_label": price_label or (properties.price_label(p) if p else None),
        "area": area or (p or {}).get("area") or (mls or {}).get("city"),
        "owner_name": owner_name,
        "owner_phone": owner_phone,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "status": "new",
    }

    if customer_name:
        fields["customer_name"] = customer_name.strip() or None
    if customer_phone:
        fields["customer_phone"] = tidy_phone(customer_phone)
    if notes:
        fields["notes"] = notes.strip() or None

    if existing:
        fields["lead_summary"] = existing.get("summary")
        fields["lead_score"] = existing.get("intent_score")
        fields["lead_status"] = existing.get("lead_status")

    saved = db.create_inquiry(fields)
    # If the enquiry is created after the linked conversation has already
    # reached Hot / >90, treat the saved snapshot as the threshold event
    # (src/services/high_priority_inquiry_email.py). Email failures are
    # non-fatal to the enquiry save.
    try:
        high_priority_inquiry_email.notify_from_saved_inquiry(saved or {})
    except Exception as error:
        print(f"High-priority inquiry email on create failed (non-fatal): {error}")
    return saved


# ---------------------------------------------------------------------
# The home's owner (the investor who listed it) hears about each enquiry:
# an email now, and the Enquiries list + badge on their Tenants tab.
# Same rule as rental applications: the enquirer's phone stays with our
# team, who follow up - the owner sees who asked, about which home, when.
# ---------------------------------------------------------------------

OWNER_SOURCES = ("owner_chat", "investor_form")
OWNER_FIELDS = ("id", "property_id", "property_title", "price_label", "area", "customer_name",
                "status", "created_at", "owner_seen_at")


def owner_for_property(property_id: str):
    """The investor account that listed this home, or None (team-managed
    homes and MLS listings have no owner account)."""
    if not property_id:
        return None
    from src.services import rentals  # local import - rentals imports a lot
    prop = rentals.one("properties", {"id": f"eq.{property_id}"})
    return rentals.owner_account_for(prop) if prop else None


def notify_owner(inquiry: dict):
    """Email the listing owner about a new enquiry. Never raises; with no
    SMTP configured, email_sender keeps it in its test outbox instead."""
    try:
        owner = owner_for_property(inquiry.get("property_id"))
        if not owner or owner.get("session_id") == inquiry.get("session_id"):
            return  # no owner account, or the owner enquired about their own home
        if not owner.get("email"):
            return
        from src.services import email_sender  # local import
        who = inquiry.get("customer_name") or "Someone"
        home = inquiry.get("property_title") or "your home"
        subject = f"New enquiry about {home}"
        body = (f"Hi {(owner.get('name') or '').split(' ')[0] or 'there'},\n\n"
                f"{who} has sent an enquiry about {home}"
                f"{' (' + inquiry['price_label'] + ')' if inquiry.get('price_label') else ''}.\n\n"
                "Our team will get in touch with them. You can see every enquiry about your homes "
                "on the Tenants tab in Staybot, under Enquiries.")
        email_sender.send(owner["email"], subject, body, "<pre style='font-family:inherit;white-space:pre-wrap'>"
                          + body.replace("&", "&amp;").replace("<", "&lt;") + "</pre>")
    except Exception as error:
        print(f"Notifying the owner about an enquiry failed (non-fatal): {error}")


def _owner_property_ids(account: dict) -> list:
    rows = db._get("properties", {"session_id": f"eq.{account['session_id']}",
                                  "source": f"in.({','.join(OWNER_SOURCES)})", "select": "id"}) or []
    return [str(r["id"]) for r in rows if r.get("id")]


def for_owner(account: dict, limit: int = 100) -> list:
    """Enquiries about the homes this investor listed, newest first - minus
    any they sent themselves, and without anyone's contact details."""
    ids = _owner_property_ids(account)
    if not ids:
        return []
    quoted = ",".join('"' + i.replace('"', '') + '"' for i in ids)
    rows = db._get("property_inquiries", {"property_id": f"in.({quoted})", "order": "created_at.desc",
                                          "limit": str(limit), "select": "*"}) or []
    out = []
    for r in rows:
        if r.get("session_id") == account["session_id"]:
            continue
        view = {k: r.get(k) for k in OWNER_FIELDS}
        view["is_new"] = not r.get("owner_seen_at")
        out.append(view)
    return out


def mark_seen_by_owner(account: dict) -> int:
    """Clear the owner's unseen badge. Returns how many were marked."""
    from datetime import datetime, timezone
    unseen = [r["id"] for r in for_owner(account) if r["is_new"]]
    if unseen:
        db._patch("property_inquiries", {"owner_seen_at": datetime.now(timezone.utc).isoformat()},
                  {"id": f"in.({','.join(unseen)})"})
    return len(unseen)


def with_kind(rows: list) -> list:
    """Tag each inquiry "tenant" or "sales" for the Inquiries tab's two lists.

    Worked out from the listing it points at rather than stored, so older
    rows are sorted too: one of our own rental listings is a tenant
    enquiry; a sale listing, or an MLS home (investor "Enquire about this" -
    those ids aren't in `properties` at all), is a sales enquiry.
    """
    ids = sorted({str(r["property_id"]) for r in rows if r.get("property_id")})
    types = {}
    if ids:
        try:
            quoted = ",".join('"' + i.replace('"', '') + '"' for i in ids)
            found = db._get("properties", {"id": f"in.({quoted})", "select": "id,listing_type"})
            types = {str(p["id"]): p.get("listing_type") for p in found}
        except Exception as error:
            print(f"Looking up inquiry listing types failed (non-fatal): {error}")
    for r in rows:
        r["kind"] = "tenant" if types.get(str(r.get("property_id"))) == "rent" else "sales"
    return rows
