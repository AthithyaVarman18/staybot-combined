"""
Viewing requests.

The LLM fills result["viewing_request"] (day, time, name, phone). This
module checks those values with plain rules - a real future date inside
opening hours - and saves the request in the Supabase `viewings` table.

Requests are saved as 'requested'. Only a person marks them 'confirmed'
(from the Viewings tab), so the chat never promises a confirmed slot.
"""

import re
from datetime import date, datetime, time, timedelta

from src.prompts.system_prompt import CURRENCY
from src.services import db
from src.services.real_estate_ai import APP_TIMEZONE


OPENING_HOUR = 8     # earliest viewing, 08:00
CLOSING_HOUR = 20    # latest viewing, 20:00
MAX_DAYS_AHEAD = 90

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def now() -> datetime:
    return datetime.now(APP_TIMEZONE)


# ---------------------------------------------------------------------
# Date and time
# ---------------------------------------------------------------------

def resolve_date(date_text, iso_date):
    """
    Work out the viewing day. Words like "tomorrow" or "Saturday" are
    resolved here rather than trusting the LLM's arithmetic; an ISO date
    from the LLM is used when the words don't name a day.
    """

    today = now().date()
    text = str(date_text or "").lower()

    if "day after tomorrow" in text:
        return today + timedelta(days=2)

    if "tomorrow" in text:
        return today + timedelta(days=1)

    if re.search(r"\b(today|tonight)\b", text):
        return today

    for index, name in enumerate(WEEKDAYS):
        if re.search(rf"\b({name}|{name[:3]})\b", text):
            days = (index - today.weekday()) % 7
            if days == 0 and "next" in text:
                days = 7
            return today + timedelta(days=days)

    try:
        return date.fromisoformat(str(iso_date)[:10])
    except (TypeError, ValueError):
        return None


def resolve_time(time_text, hhmm):
    """'10:00' from the LLM, or parse '10 AM', '5.30 pm', 'noon'."""

    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*", str(hhmm or ""))

    if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
        return time(int(match.group(1)), int(match.group(2)))

    text = str(time_text or "").lower()

    if "noon" in text:
        return time(12, 0)

    match = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", text)

    if not match:
        return None

    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    suffix = (match.group(3) or "").replace(".", "")

    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    elif not suffix and 1 <= hour <= 7:
        # "come at 5" means 5 PM for a property viewing
        hour += 12

    if hour > 23 or minute > 59:
        return None

    return time(hour, minute)


def check_slot(day, at):
    """Return a problem sentence, or None if the slot is fine."""

    current = now()

    if day < current.date():
        return "That date has already passed."

    if day > current.date() + timedelta(days=MAX_DAYS_AHEAD):
        return f"Viewings can be booked up to {MAX_DAYS_AHEAD} days ahead."

    if not (OPENING_HOUR <= at.hour < CLOSING_HOUR or (at.hour == CLOSING_HOUR and at.minute == 0)):
        return f"Viewings are between {OPENING_HOUR} AM and {CLOSING_HOUR - 12} PM."

    if day == current.date() and at <= (current + timedelta(hours=1)).time():
        return "That time is too soon for today."

    return None


def normalize_phone(phone):
    """A phone number the customer typed, in international form.

    US market (BUSINESS_CURRENCY=USD, the default): 10 digits, or 11 starting
    with 1, -> +1XXXXXXXXXX. This must come first - US area codes such as
    Raleigh's 919 start with 6-9 too, and the India rule below used to turn
    "919-555-0123" into an Indian number (+91 9195550123).
    India market (INR): 10-digit mobiles -> +91XXXXXXXXXX."""

    if not phone:
        return None

    raw = str(phone).strip()
    digits = re.sub(r"\D", "", raw)

    if CURRENCY == "USD" and not raw.startswith("+"):
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10 and digits[0] in "23456789":
            return "+1" + digits

    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]

    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits

    # Other countries: keep it only if it looks like a real number,
    # so words like "this one" are never saved as a phone.
    if 8 <= len(digits) <= 15:
        return "+" + digits

    return None


def tidy_phone(phone):
    """For saving a phone typed into a form: the +1 form when it is a number
    (so "919-555-0199" and "(919) 555 0199" are saved the same way and
    WhatsApp / alerts reach it); otherwise what they typed, never lost."""
    if phone is None or not str(phone).strip():
        return None
    return normalize_phone(phone) or str(phone).strip()


def slot_label(day, at) -> str:
    moment = datetime.combine(day, at)
    return f"{moment:%a %d %b} at {moment:%I:%M %p}".replace(" 0", " ")


# ---------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------

def handle_viewing_request(
    result: dict,
    property_id: str = None,
    property_title: str = None,
    shown_listings: list = None,
    conversation_id: str = None,
    session_id: str = None,
    known_phone: str = None,
    known_name: str = None,
    message: str = None,
):
    """
    Save or update a viewing request from this turn's analysis.

    Adds result["viewing"] describing what happened, and appends one
    line to result["response"] when a request is saved or changed, or
    when the requested slot can't be used.
    """

    request = result.get("viewing_request") or {}

    wants = request.get("wants_viewing") is True or result.get("intent") == "schedule_viewing" \
        or result.get("_visit_asked") is True

    if not wants:
        return

    # Which property?
    if not property_id:
        position = request.get("listing_position")
        try:
            listings = shown_listings or []
            # Only accept a positive, whole, one-based position.
            if isinstance(position, bool) or not re.fullmatch(r"[1-9][0-9]*", str(position)):
                raise ValueError("Invalid listing position")
            index = int(position) - 1
            if index >= len(listings):
                raise IndexError("Listing position out of range")
            listing = listings[index]
            property_id = listing.get("id")
            property_title = listing.get("title")
        except (TypeError, ValueError, IndexError):
            pass

    day = resolve_date(request.get("date_text"), request.get("date"))
    at = resolve_time(request.get("time_text"), request.get("time"))
    # The AI didn't pick out the day/time (e.g. "Saturday 10am" as a reply to
    # "which day and time?"): read them from the message itself - the time
    # only when it's clearly a time ("10am", "4:30"), never a bare number
    # like the 3 in "3 bedroom".
    if message and day is None:
        day = resolve_date(message, None)
    if message and at is None:
        clock = CLOCK_RE.search(message)
        if clock:
            at = resolve_time(clock.group(0), None)
    name = (str(request.get("name")).strip() or None) if request.get("name") else None
    name = name or ((str(known_name).strip() or None) if known_name else None)
    # A number the customer typed wins; otherwise use the one we already
    # know from the channel (the WhatsApp sender).
    phone = normalize_phone(request.get("phone")) or normalize_phone(known_phone)

    viewing = {
        "saved": False,
        "property_id": property_id,
        "property_title": property_title,
        "date": day.isoformat() if day else None,
        "time": at.strftime("%H:%M") if at else None,
        "name": name,
        "phone": phone,
        "missing": [
            label for label, value in [("property", property_id), ("day", day), ("time", at)]
            if not value
        ],
    }

    result["viewing"] = viewing

    if viewing["missing"]:
        # The AI asks for the missing piece; nothing to save yet. When we
        # know the home but not the day/time and the AI's reply didn't ask
        # anything, ask here so the visit can be booked on the next message.
        if property_id and "property" not in viewing["missing"]:
            reply = str(result.get("response") or "")
            if "?" not in reply:
                where = f" {property_title}" if property_title else " this home"
                append_line(result, f"📅 I can book a visit to{where}. Which day and time suit you? "
                                    f"(Visits run {OPENING_HOUR}:00-{CLOSING_HOUR}:00.)")
            elif property_title and property_title.lower() not in reply.lower():
                append_line(result, f"🏠 Visit to: {property_title}")
        return

    problem = check_slot(day, at)

    if problem:
        viewing["problem"] = problem
        # Only ask again if the AI's own reply didn't already ask a question.
        follow_up = "" if "?" in str(result.get("response") or "") else " Which day and time would suit you?"
        append_line(result, f"⚠️ {problem}{follow_up}")
        return

    if not (conversation_id or session_id):
        # No chat to attach it to (e.g. the AI exam or a bare API call):
        # never save an anonymous viewing nobody can follow up on.
        viewing["not_saved_reason"] = "no conversation"
        return

    if not db.ENABLED:
        report_save_failure(result, viewing)
        return

    fields = {
        "property_id": property_id,
        "property_title": property_title,
        "viewing_date": day.isoformat(),
        "viewing_time": at.strftime("%H:%M"),
    }

    if name:
        fields["customer_name"] = name
    if phone:
        fields["customer_phone"] = phone

    try:

        existing = db.find_open_viewing(property_id, conversation_id, session_id)

        if existing:

            same_slot = (
                existing.get("viewing_date") == fields["viewing_date"]
                and str(existing.get("viewing_time") or "")[:5] == fields["viewing_time"]
            )

            changes = {
                key: value for key, value in fields.items()
                if str(existing.get(key) or "")[: len(str(value))] != str(value)
            }

            if not same_slot:
                changes["status"] = "requested"   # a new slot needs confirming again

            saved = db.update_viewing(existing["id"], changes) if changes else existing
            action = "unchanged" if not changes else ("updated_contact" if same_slot else "rescheduled")

        else:

            saved = db.create_viewing({
                **fields,
                "conversation_id": conversation_id,
                "session_id": session_id,
                "status": "requested",
            })
            action = "created"

        # PostgREST must return the saved row before we claim success.
        if not isinstance(saved, dict) or not saved.get("id"):
            raise RuntimeError("Database did not acknowledge the saved viewing")

    except Exception as e:

        print(f"Saving viewing failed: {type(e).__name__}")
        report_save_failure(result, viewing)
        return

    viewing.update(
        saved=True,
        action=action,
        id=saved.get("id"),
        status=saved.get("status", "requested"),
        name=saved.get("customer_name") or name,
        phone=saved.get("customer_phone") or phone,
        label=slot_label(day, at),
    )

    where = f" for {property_title}" if property_title else ""
    ai_asked_phone = re.search(r"\b(phone|number|contact)\b", str(result.get("response") or ""), re.IGNORECASE)
    ask_phone = "" if viewing["phone"] or ai_asked_phone else " Please share a phone number so they can reach you."

    # A home an investor listed: they see this visit on their Viewings tab
    # (GET /me/viewings in main.py), so say so.
    owner_note = " The owner can see it on their Viewings tab too." if action in ("created", "rescheduled") \
        and has_owner_account(property_id) else ""

    if action == "created":
        append_line(result, f"📅 Viewing request saved: {viewing['label']}{where}. The team will confirm it with you.{owner_note}{ask_phone}")
    elif action == "rescheduled":
        append_line(result, f"📅 Viewing request changed to {viewing['label']}{where}. The team will confirm the new time.{owner_note}{ask_phone}")
    elif action == "updated_contact":
        append_line(result, "📞 Contact details added to your viewing request.")


CLOCK_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\b\d{1,2}:\d{2}\b|\bnoon\b", re.IGNORECASE)
VISIT_RE = re.compile(r"\b(visit|viewing|view (?:it|the|this|that)|tour|come (?:and|to) see|see (?:the|this|that) (?:house|home|place|flat|apartment))\b", re.IGNORECASE)


def wants_viewing(result: dict, message: str = None) -> bool:
    """The AI flagged a visit, or the customer plainly asked for one."""
    request = result.get("viewing_request") or {}
    return (request.get("wants_viewing") is True or result.get("intent") == "schedule_viewing"
            or bool(message and VISIT_RE.search(message)))


def resolve_home(result: dict, shown_listings: list = None, message: str = None, conversation_history: list = None):
    """Which live home a visit is for, in a general enquiry: the number of
    a home just shown ("the second one"), else the home the customer
    described or named (this message first, then their earlier ones).
    None when it isn't clear - the AI / the list then asks."""
    from src.services import properties  # local import - properties is heavy-ish
    position = (result.get("viewing_request") or {}).get("listing_position")
    listings = shown_listings or []
    if position is not None and not isinstance(position, bool) and re.fullmatch(r"[1-9][0-9]*", str(position)):
        index = int(position) - 1
        if index < len(listings) and listings[index].get("id"):
            return {"id": listings[index]["id"], "title": listings[index].get("title")}
    said = [message or ""] + [str(i.get("content") or "") for i in reversed(conversation_history or [])
                              if isinstance(i, dict) and i.get("role") == "user"][:6]
    try:
        p = properties.find_mentioned(said)
    except Exception as e:
        print(f"Finding the home to visit failed (non-fatal): {e}")
        return None
    return {"id": p["id"], "title": p.get("title")} if p else None


def has_owner_account(property_id) -> bool:
    """Was this home listed by an investor account (so it has an owner who
    sees its viewings)? Never raises."""
    if not property_id or not db.ENABLED:
        return False
    try:
        rows = db._get("properties", {"id": f"eq.{property_id}", "select": "session_id,source", "limit": "1"}) or []
        if not rows or rows[0].get("source") not in db.OWNER_LISTING_SOURCES or not rows[0].get("session_id"):
            return False
        return bool(db._get("accounts", {"session_id": f"eq.{rows[0]['session_id']}", "select": "id", "limit": "1"}))
    except Exception:
        return False


def append_line(result: dict, line: str):
    reply = str(result.get("response") or "").rstrip()
    result["response"] = f"{reply}\n\n{line}" if reply else line


def report_save_failure(result: dict, viewing: dict):
    """Make the actual save outcome authoritative over the AI's draft reply."""
    message = (
        "I couldn't verify that your viewing request was saved. "
        "Please try again or contact the team before making travel plans."
    )
    viewing["saved"] = False
    viewing["error"] = message
    result["response"] = message
    result["next_question"] = None


def notify_customer(viewing: dict):
    """The team confirmed or cancelled a visit: tell the customer who asked.
    A message in their current chat (so they see it in Chat, and the web
    page pops it up from /me/viewings) and, when we have their number, a
    WhatsApp message. Never raises - the status change already saved."""
    status = viewing.get("status")
    try:
        day = date.fromisoformat(str(viewing.get("viewing_date"))[:10])
        hh, mm = str(viewing.get("viewing_time") or "")[:5].split(":")
        when = slot_label(day, time(int(hh), int(mm)))
    except Exception:
        when = "the requested time"
    home = viewing.get("property_title") or "the home"
    if status == "confirmed":
        text = f"✅ Your visit to {home} on {when} is confirmed. See you there!"
    elif status == "cancelled":
        text = (f"❌ Your visit to {home} on {when} has been cancelled. "
                "Reply here if you'd like to pick another day and time.")
    else:
        return

    # Their current chat thread (after "New chat" the old one is archived),
    # else the thread the visit was booked in.
    conversation_id = None
    try:
        if viewing.get("session_id"):
            latest = db.latest_conversation(viewing["session_id"])
            conversation_id = (latest or {}).get("id")
        conversation_id = conversation_id or viewing.get("conversation_id")
        if conversation_id:
            db.add_message(conversation_id, "assistant", text)
    except Exception as e:
        print(f"Posting the visit update to the chat failed (non-fatal): {e}")

    if viewing.get("customer_phone"):
        try:
            from src.services import whatsapp  # local import - whatsapp imports chat
            whatsapp.send_text(str(viewing["customer_phone"]).lstrip("+"), text)
        except Exception as e:
            print(f"WhatsApp visit update failed (non-fatal): {e}")

