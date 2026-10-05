"""
One customer message in, one reply out.

Shared by the web chat (POST /analyze) and WhatsApp, so both channels get
exactly the same AI, search, viewing and owner-listing behaviour.
"""

import re
import time

from datetime import datetime, timezone

from src.services import db, fact_check, fair_housing, hubspot, investor_journey, investors, lead_scoring, maintenance, metrics, owner_listings, portfolio, properties, quick_replies, viewings
from src.services.real_estate_ai import analyze_message


# Investors also own homes they want to rent out or sell. Without this, the
# notes below made the AI file "I want to list my 3 bed house in Garner for
# $1,800" as investing criteria, so the listing was never saved and tenants
# never saw it (src/services/owner_listings.py only acts on role "owner").
LISTING_EXCEPTION = (
    " Exception - listing a home: if they want to list, rent out, advertise or sell a property they "
    "own (e.g. \"I want to list my house\", \"rent out my 3 bed in Garner for $1,800\"), use role "
    "\"owner\" with intent \"list_property\" (or \"update_property\" while they add details) and "
    "fill property_details from what they said, exactly as for any owner (see OWNER DETAILS). Their "
    "listing is shown to tenants once it has a type or bedrooms, a location and a rent or sale price, "
    "so ask for whichever of those is still missing, one at a time, then offer to add a photo."
)

# ---------------------------------------------------------------------
# Investors listing a home across several messages
# ---------------------------------------------------------------------
# An investor typically says "I want to list my house" once and then gives
# the details over the next few messages ("Garner, 3 beds", "$1,800 a
# month, pets are fine"). On their own those follow-ups read exactly like
# the investor's own search criteria, so the AI filed them as role
# "investor" and the listing was never saved - tenants never saw it. So
# for investor accounts we remember that a listing is in progress for this
# conversation and tell the AI (listing_mode_note()); and if it still
# answers as an investor on a turn that plainly describes the home, we ask
# it once more with that made explicit (see process_message()).

# "list my house", "rent it out", "looking for tenants", "sell my condo"...
LISTING_INTENT_RE = re.compile(
    r"\b("
    r"list(ing)?\s+(my|our|a|the|this|it)\b"
    r"|(rent|lease|let)(ing)?\s+(it|them|this|that|my|our)?\s*out\b"
    r"|rent(ing)?\s+(my|our)\b"
    r"|advertis(e|ing)\s+(my|our|it)\b"
    r"|put(ting)?\s+(it|my|our|this)\b.{0,30}\b(for|on\s+the\s+market)\b"
    r"|sell(ing)?\s+(my|our)\b"
    r"|(find|need|want|looking\s+for)\s+(a\s+|some\s+|new\s+)?tenants?\b"
    r"|tenants?\s+for\s+(my|our)\b"
    r"|my\s+(\w+\s+){0,3}(house|home|property|place|unit|apartment|condo|townhouse|duplex|rental)\s+(is\s+)?(vacant|empty|available)\b"
    r")",
    re.IGNORECASE,
)

# Lines owner_listings.py adds to the reply once a listing exists.
LISTING_REPLY_MARKERS = ("your listing is live", "listing draft saved", "listing draft updated",
                         "listing updated", "photo added to your listing")

# Does this message describe a home (so it's worth re-asking the AI)?
HOME_DETAILS_RE = re.compile(
    r"\$\s?\d|\d\s*(k\b|bed|br\b|bd\b|bath|ba\b|bhk|sq)"
    r"|\b(rent|price|deposit|pets?|parking|garage|driveway|furnished|available|bed(room)?s?|bath(room)?s?"
    r"|house|townhouse|condo|apartment|duplex|yard|street|st\.?|rd|road|ave|avenue|drive|dr)\b",
    re.IGNORECASE,
)


def listing_mode(message: str, conversation_history: list, account_role: str) -> tuple[bool, bool]:
    """(said_now, in_progress) for an investor account listing a home.
    said_now: this message itself says they're listing. in_progress: that,
    or they said so / a listing was saved in the last few turns."""

    if account_role not in owner_listings.INVESTOR_ROLES:
        return False, False

    said_now = bool(LISTING_INTENT_RE.search(message or ""))
    recent = [m for m in (conversation_history or [])[-10:] if isinstance(m, dict)]
    earlier = any(
        (m.get("role") == "user" and LISTING_INTENT_RE.search(str(m.get("content") or "")))
        or (m.get("role") == "assistant" and any(k in str(m.get("content") or "").lower() for k in LISTING_REPLY_MARKERS))
        for m in recent
    )
    return said_now, said_now or earlier


LISTING_MODE_NOTE = (
    " LISTING IN PROGRESS: in this conversation the customer has said they want to list, rent out or "
    "sell a home THEY OWN, so tenants/buyers can find it. Messages describing that home - type, bedrooms, "
    "bathrooms, address/location, rent or sale price, deposit, pets, parking, furnishing, amenities, "
    "availability, description, photos - are details of THEIR listing, not what they want to buy: use "
    "role \"owner\" with intent \"list_property\" (or \"update_property\"), and put everything they have "
    "said about the home anywhere in this conversation into property_details (never investor_profile or "
    "requirements). Don't suggest homes for them to buy or rent. Ask only for whichever of type-or-bedrooms, "
    "location, and rent-or-sale-price is still missing, one at a time, then offer to add a photo. Only stop "
    "treating messages this way if they clearly change the subject (e.g. start asking about buying an "
    "investment property)."
)

LISTING_RETRY_NOTE = (
    " This message is the customer describing the home they are listing for tenants/buyers. You MUST "
    "answer with role \"owner\" and fill property_details with every detail of that home from the whole "
    "conversation."
)


ACCOUNT_ROLE_NOTES = {
    "tenant": "This customer registered for the Tenant workflow (looking to rent/buy a home).",
    "new_investor": (
        "This customer registered for the New Property Investor workflow (first-time real-estate "
        "investor) - they are shopping for an investment property, not somewhere to live. A message "
        "that sounds like an ordinary rental search (a budget, an area, a bedroom count, pet/parking "
        "preferences) is still their investing criteria, not a tenant's - treat it as role \"investor\" "
        "and put those details in investor_profile (areas, property_type, min_bedrooms, ...), never "
        "role \"tenant\" or requirements. Only use role \"tenant\" if they say outright this home is for "
        "them to live in. Only leave investing entirely if they clearly want to report a maintenance "
        "issue on a property they already own, or ask something with nothing to do with real estate."
        + LISTING_EXCEPTION
    ),
    "existing_investor": (
        "This customer registered for the Existing Property Investor workflow (already owns investment "
        "property) - they are shopping for their next investment, not somewhere to live. A message that "
        "sounds like an ordinary rental search (a budget, an area, a bedroom count, pet/parking "
        "preferences) is still their investing criteria, not a tenant's - treat it as role \"investor\" "
        "and put those details in investor_profile (areas, property_type, min_bedrooms, ...), never "
        "role \"tenant\" or requirements. Only use role \"tenant\" if they say outright this home is for "
        "them to live in. Only leave investing entirely if they clearly want to report a maintenance "
        "issue on a property they already own, or ask something with nothing to do with real estate."
        + LISTING_EXCEPTION
    ),
}


class AIUnavailable(RuntimeError):
    """The AI couldn't produce a reply (quota, network, invalid output)."""


def process_message(
    message: str,
    conversation_history: list = None,
    property_context: dict = None,
    session_id: str = None,
    listing_id: str = None,
    listing_title: str = None,
    persona: str = "tenant",
    customer_phone: str = None,
    customer_name: str = None,
    channel: str = "web",
    image_bytes: bytes = None,
    image_content_type: str = None,
    image_filename: str = None,
    account_role: str = None,
    account_id: str = None,
    saved_requirements: dict = None,
) -> dict:
    """
    Run the AI on one message, then search listings, save viewing
    requests and owner drafts, and store both messages in Supabase
    (when session_id is given). Returns the full analysis; the text to
    send the customer is result["response"].

    image_bytes/image_content_type/image_filename: an optional photo
    attached to this message. Used when the AI decides this turn is a
    maintenance report (see src/services/maintenance.py) or when it's an
    owner attaching a photo of their property (see
    src/services/owner_listings.py); ignored otherwise.
    """

    conversation_history = conversation_history or []
    started = time.time()

    # Small talk ("hi", "thanks", "ok") is answered instantly, without the AI.
    quick = quick_replies.quick_reply(message, conversation_history)

    if quick:
        return _instant_reply(
            quick, message, started, session_id, listing_id,
            listing_title, persona, property_context, channel
        )

    shown_listings = (
        None if property_context
        else properties.shown_in_history(conversation_history)
    )

    # Looked up here (rather than in the "Persist to Supabase" block further
    # down, where this used to happen) so known_brief_note below can use
    # conversation["id"] - it needs this BEFORE the AI call, not after.
    conversation = None

    if db.ENABLED and session_id:

        try:

            conversation = db.get_or_create_conversation(
                session_id=session_id,
                listing_id=listing_id,
                listing_title=listing_title,
                persona=persona,
                property_context=property_context
            )

        except Exception as e:

            print(f"Supabase conversation lookup failed (non-fatal): {e}")

    # Investor account listing a home over several messages? (see listing_mode())
    listing_said_now, listing_in_progress = listing_mode(message, conversation_history, account_role)

    channel_note = (
        f"The customer is on WhatsApp. Their phone number is already known "
        f"({customer_phone}), so never ask for a phone number. "
        "Keep replies short and plain (no markdown tables)."
        if customer_phone else None
    )

    # Returning investor: tell the AI what they already told us, so it picks the
    # conversation up instead of starting the questions again. Web chat has no
    # phone to key off, so this also needs to look up by conversation_id -
    # without it, a browser session (no WhatsApp number) was re-asked
    # cash/financing/goal/areas/timeline every turn even after answering all
    # five, because known_brief_note() had nothing to look them up by.
    investor_profile = investors.find_known_profile(
        conversation_id=conversation["id"] if conversation else None,
        known_phone=customer_phone,
    )
    brief_note = investors.known_brief_note(investor_profile)
    # Asking about a home we already showed them? Give the AI the real
    # computed numbers for it, so it states them directly instead of
    # deferring to "a team member" for numbers the system already has -
    # this takes priority over the brief note, since it's more specific.
    listing_note = investors.mentioned_listing_note(message, conversation_history, investor_profile)
    # About to be suppressed as a repeat of the exact same search? Say so
    # before the AI replies, not after - otherwise it promises "pulling the
    # best fits" (per the system prompt) on a turn where matches_for_chat()
    # is about to add nothing at all.
    # New Property Investor accounts see homes by their second message (see
    # investors.quick_ready()) instead of after the whole five-field brief.
    fast_listing = account_role == "new_investor"
    turn = investors.user_turn_number(conversation_history)
    repeat_note = (investors.repeat_match_note(investor_profile, message, fast=fast_listing, turn=turn)
                   if not listing_note else None)
    fast_note = (investors.fast_listing_note(investor_profile, conversation_history)
                 if fast_listing and not listing_note and not repeat_note else None)
    if fast_note:
        brief_note = f"{brief_note} {fast_note}" if brief_note else fast_note
    if listing_note:
        channel_note = f"{channel_note} {listing_note}" if channel_note else listing_note
    elif repeat_note:
        channel_note = f"{channel_note} {repeat_note}" if channel_note else repeat_note
    elif brief_note:
        channel_note = f"{channel_note} {brief_note}" if channel_note else brief_note

    # Mid-listing, the investing notes above (their brief, "pulling the best
    # fits"...) would steer the AI back to asking about budget and financing.
    if listing_in_progress:
        channel_note = (
            f"The customer is on WhatsApp. Their phone number is already known ({customer_phone}), so never "
            "ask for a phone number. Keep replies short and plain (no markdown tables)."
            if customer_phone else None
        )

    # Where this person is in their guided investor journey, if they are on one
    # (src/services/investor_journey.py), so the AI picks up mid-journey.
    try:
        open_journey = investor_journey.find_open(session_id=session_id) if session_id else None
    except Exception as error:
        print(f"Investor journey lookup failed (non-fatal): {error}")
        open_journey = None
    # The journey's "still being qualified" briefing would have the AI keep
    # asking the qualifying questions instead of showing homes.
    investor_stage_note = (None if listing_in_progress or fast_note
                           else investor_journey.stage_note(open_journey))

    # A logged-in account already chose its workflow at signup
    # (src/services/accounts.py) - nudge the AI towards it without forcing it.
    account_note = ACCOUNT_ROLE_NOTES.get(account_role)
    if saved_requirements:
        from src.services.requirements import context_note
        saved_note = context_note(saved_requirements)
        if saved_note:
            account_note = f"{account_note or ''}\n\n{saved_note}".strip()
    requirement_update_proposal = None
    if saved_requirements and account_id and channel == "web":
        try:
            from src.services.requirements import detect_update_request
            requirement_update_proposal = detect_update_request(message, saved_requirements)
        except Exception as e:
            print(f"Requirement update detection failed (non-fatal): {e}")

    # Tenant accounts: maintenance is for the home they rent through Staybot
    # (team + owner approved - src/services/rentals.py). Tell the AI which
    # home that is, so it files the report without asking for a unit number.
    tenant_home = None
    if account_role == "tenant":
        from src.services import rentals  # local import: rentals.py imports maintenance/properties
        tenant_home = rentals.tenant_home(account_id)
        if tenant_home:
            account_note = f"{account_note or ''} {rentals.home_note(tenant_home)}".strip()
        # Requests made on any channel (Chat, WhatsApp, Maintenance tab).
        tickets_note = rentals.open_tickets_note(session_id)
        if tickets_note:
            account_note = f"{account_note or ''}{tickets_note}".strip()

    if listing_in_progress:
        account_note = (account_note or "") + LISTING_MODE_NOTE

    try:

        result = analyze_message(
            message=message,
            conversation_history=conversation_history,
            property_context=property_context,
            shown_listings=shown_listings,
            channel_note=channel_note,
            investor_stage_note=investor_stage_note,
            account_note=account_note,
        )

    except Exception as e:

        metrics.record(
            channel=channel,
            outcome="error",
            total_ms=(time.time() - started) * 1000,
            perf=getattr(e, "perf", None),
            session_id=session_id,
            error=str(e),
        )

        raise AIUnavailable(str(e)) from e

    # Still answered as a buyer on a turn that plainly describes the home
    # they're listing? Ask once more with that made explicit. Best-effort:
    # if the retry fails, the first answer stands.
    if (
        listing_in_progress
        and str(result.get("role") or "").lower() != "owner"
        and (listing_said_now or HOME_DETAILS_RE.search(message or ""))
    ):
        try:
            retry = analyze_message(
                message=message,
                conversation_history=conversation_history,
                property_context=property_context,
                shown_listings=shown_listings,
                channel_note=channel_note,
                investor_stage_note=None,
                account_note=account_note + LISTING_RETRY_NOTE,
            )
            if str(retry.get("role") or "").lower() == "owner":
                result = retry
                account_note = account_note + LISTING_RETRY_NOTE  # keep the fact-check consistent
        except Exception as e:
            print(f"Listing re-ask failed (non-fatal, keeping first answer): {e}")

    # Fact-check the AI's own words before anything is added or sent.
    _fact_check(
        result, message, conversation_history, property_context, shown_listings,
        channel_note=channel_note, investor_stage_note=investor_stage_note, account_note=account_note,
    )

    # A visit asked for in a general enquiry ("I want to visit the 3 bedroom
    # house for rent in Wilmington"): work out WHICH home from the customer's
    # own words, so the chat asks for a day and time and books it, instead of
    # running a fresh search and listing "closest options".
    visit_home = None
    last_reply = next((str(i.get("content") or "") for i in reversed(conversation_history)
                       if isinstance(i, dict) and i.get("role") == "assistant"), "")
    answering_visit_time = bool(re.search(r"\bvisit|\bviewing", last_reply, re.I)) and \
        bool(re.search(r"\bday\b|\btime\b", last_reply, re.I))
    if not property_context and (viewings.wants_viewing(result, message) or answering_visit_time):
        visit_home = viewings.resolve_home(result, shown_listings, message, conversation_history)
        if visit_home:
            result["_visit_asked"] = True  # book it even if the AI didn't flag the intent

    # General enquiry: find listings that fit what the customer asked for.
    if not visit_home:
        properties.attach_matches(
            result,
            property_context=property_context,
            conversation_history=conversation_history,
            message=message,
        )
        if not property_context and viewings.wants_viewing(result, message) and result.get("matches"):
            viewings.append_line(result, "Which of these would you like to visit? Reply with its number "
                                         "(or its name) and a day and time that suits you.")

    if requirement_update_proposal:
        result["requirement_update_proposal"] = requirement_update_proposal
        result["response"] = (
            str(result.get("response") or "").rstrip()
            + "\n\nI can update your saved requirements, but I won't change them automatically. "
              "Please confirm the proposed change below."
        ).strip()

    # -----------------------------------------------------------------
    # Persist to Supabase (best-effort - never let a DB hiccup break the
    # chat reply the user is waiting on). `conversation` was already
    # looked up/created above, before the AI call.
    # -----------------------------------------------------------------

    # Save a viewing request if the customer asked for one. Runs before the
    # messages are stored so the saved reply includes the viewing line.
    if property_context:
        viewing_property_id = listing_id or property_context.get("property_id")
        viewing_property_title = listing_title
    elif visit_home:
        viewing_property_id = visit_home.get("id")
        viewing_property_title = visit_home.get("title")
    else:
        viewing_property_id = None
        viewing_property_title = None

    viewings.handle_viewing_request(
        result,
        property_id=viewing_property_id,
        property_title=viewing_property_title,
        shown_listings=shown_listings,
        conversation_id=conversation["id"] if conversation else None,
        session_id=session_id,
        known_phone=customer_phone,
        known_name=customer_name,
        message=message if result.get("_visit_asked") else None,
    )

    # A tenant / new investor asking about a specific home an investor listed:
    # pop up a notification for that owner with the asker's details
    # (src/services/owner_notifications.py). Runs in the background.
    if result.get("intent") != "maintenance_issue":
        from src.services import owner_notifications  # local import
        owner_notifications.from_chat(
            message, account_id, account_role, phone=customer_phone,
            listing_id=listing_id if property_context else None, listing_title=listing_title,
            shown_listings=shown_listings, visit_home=visit_home, channel=channel,
        )

    # Tenant maintenance report: classify (text + optional photo) and
    # save/update a ticket, tied to this conversation. A tenant account's
    # report is always about the home they rent - and before they rent one
    # (application approved by the team and the owner), there's nothing to
    # file it against, so they're told how to get there instead.
    #
    # Repair problems raised here (web Chat or WhatsApp) get the same repair
    # assistant as the Maintenance tab (src/services/maintenance_chat.py):
    # simple safe fixes first, safety steps for emergencies, and a ticket for
    # the owner + team with a summary and the chat when it can't be fixed. It
    # also carries on a repair conversation across messages. A tenant account
    # with a rented home: that home. No account (an unknown WhatsApp number,
    # or the team testing the web chat): the selected listing, otherwise
    # "home not identified" - the ticket goes to the team to check. If the
    # assistant can't answer, the basic report below runs as before.
    from src.services import maintenance_chat  # local import
    repair_home = None
    if account_role == "tenant":
        repair_home = tenant_home          # None -> not renting yet, told how below
    elif not account_role:
        repair_home = ({"property_id": viewing_property_id, "title": viewing_property_title or "the selected home",
                        "location": ""} if viewing_property_id else {"unidentified": True})
    repair_handled = bool(repair_home) and maintenance_chat.handle_chat_turn(
        result, message,
        conversation_id=conversation["id"] if conversation else None,
        session_id=session_id, home=repair_home,
        customer_name=customer_name, phone=customer_phone,
        image_bytes=image_bytes, image_content_type=image_content_type, image_filename=image_filename,
    )

    maintenance_property_id, maintenance_property_title = viewing_property_id, viewing_property_title
    if not repair_handled and account_role == "tenant" and result.get("intent") == "maintenance_issue":
        from src.services import rentals  # local import
        if tenant_home:
            maintenance_property_id, maintenance_property_title = tenant_home["property_id"], tenant_home["title"]
            if not str(result.get("tenant_id") or "").strip():
                result["tenant_id"] = tenant_home["location"] or tenant_home["title"]
        else:
            result["intent"] = "maintenance_locked"  # handle_maintenance_report() skips it
            result["response"] = rentals.MAINTENANCE_LOCKED
            result["maintenance_ticket"] = {"saved": False, "locked": True}

    if not repair_handled:
        maintenance.handle_maintenance_report(
            result,
            message=message,
            property_id=maintenance_property_id,
            property_title=maintenance_property_title,
            conversation_id=conversation["id"] if conversation else None,
            session_id=session_id,
            known_phone=customer_phone,
            known_name=customer_name,
            image_bytes=image_bytes,
            image_content_type=image_content_type,
            image_filename=image_filename,
        )
        # Tenant accounts: the same request is on their Maintenance tab, whichever
        # channel (web Chat or WhatsApp) they reported it on.
        ticket = result.get("maintenance_ticket") or {}
        if account_role == "tenant" and ticket.get("saved") and ticket.get("action") == "created":
            maintenance.append_line(result, "You can follow it on your Maintenance tab.")

    # Owners: save or update their listing draft for review, including
    # any photo of the property they attached to this message.
    owner_listings.handle_owner_listing(
        result,
        conversation_id=conversation["id"] if conversation else None,
        session_id=session_id,
        account_role=account_role,
        known_phone=customer_phone,
        known_name=customer_name,
        image_bytes=image_bytes,
        image_content_type=image_content_type,
    )

    # Investors: save/update what we learned about their budget, financing and goal,
    # then add the real MLS homes that fit, so the AI never invents listings.
    investor_id = investors.handle_investor(
        result,
        conversation_id=conversation["id"] if conversation else None,
        known_phone=customer_phone,
        known_name=customer_name,
    )
    if investor_id:
        found = investors.matches_for_chat(investor_id, message=message, conversation_history=conversation_history,
                                           account_id=account_id, fast=fast_listing,
                                           session_id=session_id)
        if found:
            result["investor_matches"] = found["data"]
            result["investor_photos"] = found["photos"]
            result["investor_followup"] = found["followup"]
            result["response"] = (result.get("response") or "").rstrip() + "\n\n" + found["message"]

    # The guided journey (stage, portfolio, CRM) is fed from the same brief the
    # chat just collected, so the investor is never asked the same things twice.
    try:
        investor_journey.handle_investor_turn(
            result,
            conversation_id=conversation["id"] if conversation else None,
            session_id=session_id,
            known_phone=customer_phone,
            known_name=customer_name,
            account_role=account_role,
        )

        # Logged-in accounts: merge what we learned into their own portfolio page.
        portfolio.handle_portfolio_update(result, account_id=account_id)
    except Exception as error:      # tracking must never break the reply they are waiting on
        print(f"Investor journey/portfolio update failed (non-fatal): {error}")

    # AI Lead Score: nine components -> one score -> hot/warm/nurture/unqualified.
    lead = lead_scoring.score_lead(
        result,
        message=message,
        history=conversation_history,
        property_context=property_context,
        viewing=result.get("viewing"),
        message_times=_message_times(conversation),
        verification=(conversation or {}).get("human_verification") or "unverified",
    )
    result.setdefault("qualification", {}).update(lead)

    # Mirror this turn into HubSpot (best-effort, never blocks the reply).
    # Only WhatsApp messages carry a phone number to key the Contact on.
    if hubspot.ENABLED and customer_phone:
        try:
            hubspot.sync_turn(
                result=result,
                lead=lead,
                message=message,
                customer_phone=customer_phone,
                customer_name=customer_name,
                channel=channel,
                property_id=viewing_property_id,
            )
        except Exception as e:
            print(f"HubSpot sync failed (non-fatal): {e}")

    if conversation:

        try:

            conversation_id = conversation["id"]

            db.add_message(
                conversation_id=conversation_id,
                role="user",
                content=message
            )

            reply_text = result.get("response") or result.get("next_question") or ""

            db.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=reply_text,
                analysis=result
            )

            qualification = result.get("qualification") or {}

            fields = {
                "role": result.get("role"),
                "intent": result.get("intent"),
                "intent_score": qualification.get("intent_score", 0),
                "lead_status": qualification.get("lead_status", "nurture"),
                "summary": result.get("summary"),
                "listing_title": listing_title,
            }

            previous_score = conversation.get("intent_score")
            previous_status = conversation.get("lead_status")

            try:
                db.update_conversation(conversation_id, {**fields, "lead_components": lead})
            except Exception:
                # supabase_lead_scoring.sql not run yet: save without the breakdown.
                db.update_conversation(conversation_id, fields)

            # Automatic high-priority enquiry email (src/services/high_priority_inquiry_email.py):
            # when this update makes an enquiry Hot or pushes it over 90, email the
            # people tied to it. Best-effort - scoring and saving never fail because
            # of SMTP or the notification bookkeeping.
            try:
                from src.services import high_priority_inquiry_email  # local import
                for inquiry_row in db._get("property_inquiries", {
                        "conversation_id": f"eq.{conversation_id}", "select": "*", "limit": "100"}) or []:
                    high_priority_inquiry_email.notify_for_update(
                        inquiry_row, previous_score, previous_status,
                        qualification.get("intent_score", 0), qualification.get("lead_status", "nurture"),
                    )
            except Exception as error:
                print(f"High-priority enquiry email failed (non-fatal): {error}")

            # Hot alert for the staff member (src/services/staff_alerts.py): email /
            # WhatsApp the team when this chat turns Hot or goes over 90 - even if
            # the customer never sent an enquiry. Runs in the background.
            try:
                from src.services import staff_alerts  # local import
                staff_alerts.notify_in_background(
                    session_id, conversation_id, result, lead, previous_score, previous_status,
                )
            except Exception as error:
                print(f"Staff hot alert failed (non-fatal): {error}")

            result["_conversation_id"] = conversation_id

        except Exception as e:

            print(f"Supabase persistence failed (non-fatal): {e}")

    perf = result.get("_perf") or {}
    perf["total_ms"] = int((time.time() - started) * 1000)
    result["_perf"] = perf

    metrics.record(
        channel=channel,
        outcome="ai",
        total_ms=perf["total_ms"],
        perf=perf,
        session_id=session_id,
    )

    return result


def _fact_check(result, message, history, property_context, shown_listings, channel_note=None,
                investor_stage_note=None, account_note=None):
    """Check the reply against the listing data. If it states something
    false, ask the AI once to rewrite it with the problems spelled out; if
    the rewrite is still wrong (or fails), send a safe holding reply."""

    # This checks tenant-listing facts (pets/parking/furnishing/amenities)
    # and dollar amounts against shown_listings/requirements.budget - both
    # keyed to the sample tenant `properties` table, which has nothing to
    # do with an investor's MLS deal numbers (cash needed, cash flow, cap
    # rate, ...). Those numbers are already grounded in real MLS/deal-
    # analysis data (see investors.py: mentioned_listing_note(),
    # match_listings()) - running this on them only produced false
    # "hallucination" positives that overwrote a correct, data-backed
    # reply with the generic "let me check with the team" holding line.
    # Fair Housing (src/services/fair_housing.py) is checked for EVERY role,
    # investors included - steering is never OK, whoever is asking.
    investor = str(result.get("role") or "").lower() == "investor"
    if investor and not fair_housing.check(result.get("response")):
        result["fact_check"] = {"status": "skipped_investor"}
        return

    said = [str(m.get("content") or "") for m in history if m.get("role") == "user"] + [message]

    def problems(reply):
        found = fair_housing.check(reply)
        if not investor:
            found += fact_check.check_reply(
                reply, property_context, shown_listings, said, result.get("requirements")
            )
        return found

    def correction(found):
        fh = [i for i in found if fair_housing.is_fair_housing(i)]
        facts = [i for i in found if not fair_housing.is_fair_housing(i)]
        return "\n\n".join(([fair_housing.correction_note(fh)] if fh else [])
                            + ([fact_check.correction_note(facts)] if facts else []))

    issues = problems(result.get("response"))

    if not issues:
        result["fact_check"] = {"status": "passed"}
        return

    print(f"Fact check failed: {[i['type'] for i in issues]}")
    original = result.get("response")

    try:
        retry = analyze_message(
            message=message,
            conversation_history=history,
            property_context=property_context,
            shown_listings=shown_listings,
            channel_note=channel_note,
            investor_stage_note=investor_stage_note,
            account_note=account_note,
            correction_note=correction(issues),
        )
        still_wrong = problems(retry.get("response"))
    except Exception as e:
        print(f"Fact-check retry failed: {e}")
        retry, still_wrong = None, issues

    if retry and not still_wrong:
        result["response"] = retry.get("response")
        result["fact_check"] = {"status": "fixed", "issues": issues, "original": original}
    else:
        remaining = still_wrong or issues
        result["response"] = (fair_housing.SAFE_REPLY if any(fair_housing.is_fair_housing(i) for i in remaining)
                              else fact_check.SAFE_REPLY)
        result["fact_check"] = {"status": "blocked", "issues": still_wrong or issues, "original": original}


def _message_times(conversation) -> list:
    """(role, time) for the saved messages plus the current one, used to
    measure how quickly the customer replies."""

    times = []

    if conversation and db.ENABLED:
        try:
            for m in db.list_messages(conversation["id"])[-40:]:
                try:
                    times.append((m["role"], datetime.fromisoformat(str(m["created_at"]).replace("Z", "+00:00"))))
                except (KeyError, ValueError):
                    continue
        except Exception as e:
            print(f"Loading message times failed (non-fatal): {e}")

    times.append(("user", datetime.now(timezone.utc)))
    return times


def _instant_reply(quick, message, started, session_id, listing_id, listing_title, persona, property_context, channel):
    """Reply to small talk without the AI. The messages are saved, but the
    lead's score and summary are left as they were."""

    kind, reply = quick

    result = {
        "response": reply,
        "next_question": None,
        "quick_reply": kind,
        "_model_used": None,
    }

    if db.ENABLED and session_id:
        try:
            conversation = db.get_or_create_conversation(
                session_id=session_id,
                listing_id=listing_id,
                listing_title=listing_title,
                persona=persona,
                property_context=property_context
            )
            if conversation:
                db.add_message(conversation_id=conversation["id"], role="user", content=message)
                db.add_message(conversation_id=conversation["id"], role="assistant", content=reply, analysis=result)
                result["_conversation_id"] = conversation["id"]
        except Exception as e:
            print(f"Supabase persistence failed (non-fatal): {e}")

    total_ms = int((time.time() - started) * 1000)
    result["_perf"] = {"total_ms": total_ms, "model": None}

    metrics.record(
        channel=channel,
        outcome="quick_reply",
        total_ms=total_ms,
        quick_kind=kind,
        session_id=session_id,
    )

    return result
