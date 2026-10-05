"""
Tenant maintenance reports, detected inside the normal chat/WhatsApp
conversation (src/services/chat.py) - not a separate flow. When the AI
sets intent="maintenance_issue" (see src/prompts/system_prompt.py), this
module classifies the tenant's message (+ optional photo) with
src/services/maintenance_ai.py and saves it as a ticket in the Supabase
`maintenance_tickets` table, the same way viewings.py saves a viewing
request from result["viewing_request"].

Two status fields on a ticket:
  classification_status  - what the AI decided: classified / rejected / needs_review
  ticket_status           - what the team is doing about it: needs_review / open /
                             in_progress / resolved / dismissed (team-managed)

A tenant can describe an issue, then send a photo a message or two later
(or vice versa) - handle_maintenance_report finds the still-open ticket
for this conversation and updates it in place instead of creating a
duplicate for every turn.

The AI always asks for the tenant's unit/apartment number first, before
discussing the problem (see MAINTENANCE REPORTS in system_prompt.py) -
result["tenant_id"] is what it captured. Nothing is classified or saved
until that's known.
"""

from typing import Any, Dict, Optional

from src.services import db
from src.services.maintenance_ai import classify_maintenance_photo


MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

TICKET_STATUSES = ["needs_review", "open", "in_progress", "resolved", "dismissed"]

OPEN_TICKET_STATUSES = ("needs_review", "open", "in_progress")


def _default_ticket_status(classification_status: str) -> str:
    if classification_status == "rejected":
        return "dismissed"
    if classification_status == "needs_review":
        return "needs_review"
    return "open"


def classify_maintenance_ticket(
    message: str = "",
    image_bytes: Optional[bytes] = None,
    image_content_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the vision classifier and apply the photo/text safety rule:
    a mismatch, or an unverifiable photo on an otherwise-classified
    report, always drops the ticket into needs_review."""

    message = (message or "").strip()
    has_image = bool(image_bytes)

    if not message and not has_image:
        raise ValueError("Please provide a description or attach a photo.")

    result = classify_maintenance_photo(
        message=message,
        image_bytes=image_bytes,
        image_content_type=image_content_type,
    )

    if not isinstance(result, dict):
        raise ValueError("The classifier returned an invalid result.")

    if not has_image:
        result["photoTextMatch"] = None

    elif result.get("photoTextMatch") is False:
        result["status"] = "needs_review"
        result["summary"] = (
            (result.get("summary") or "").strip()
            + " The uploaded photo and the written description appear to describe "
              "different issues. Manual review is required."
        ).strip()

    elif result.get("photoTextMatch") is None and result.get("status") == "classified":
        result["status"] = "needs_review"
        result["summary"] = (
            (result.get("summary") or "").strip()
            + " The uploaded photo could not be confidently verified against the "
              "written description."
        ).strip()

    if result.get("isPropertyIssue") is True and result.get("status") != "needs_review":
        result["status"] = "classified"
    elif result.get("isPropertyIssue") is False:
        result["status"] = "rejected"
    elif result.get("status") not in ("needs_review", "classified", "rejected"):
        result["status"] = "needs_review"

    return result


ISSUE_LABELS = {
    "plumbing": "plumbing", "electrical": "electrical", "hvac": "HVAC",
    "appliance": "appliance", "structural": "structural", "water_damage": "water damage",
    "gas": "gas", "pest": "pest", "security": "security", "door_window": "door/window",
    "heating": "heating", "cooling": "cooling", "other": "maintenance",
}


def append_line(result: dict, line: str):
    reply = str(result.get("response") or "").rstrip()
    result["response"] = f"{reply}\n\n{line}" if reply else line


def handle_maintenance_report(
    result: dict,
    message: str = "",
    property_id: Optional[str] = None,
    property_title: Optional[str] = None,
    conversation_id: Optional[str] = None,
    session_id: Optional[str] = None,
    known_phone: Optional[str] = None,
    known_name: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    image_content_type: Optional[str] = None,
    image_filename: Optional[str] = None,
):
    """
    Classify this turn's maintenance report and save/update a ticket -
    but only once result["tenant_id"] (their unit/apartment number, set
    by the AI per src/prompts/system_prompt.py) is known. Until then the
    AI's own reply is just asking for it, and this is a no-op.

    Adds result["maintenance_ticket"] describing what happened, and
    appends one line to result["response"] - mirrors
    viewings.handle_viewing_request's shape and calling convention.
    """

    if result.get("intent") != "maintenance_issue":
        return

    tenant_id = str(result.get("tenant_id") or "").strip()

    if not tenant_id:
        # The AI's own reply is just asking for the unit/apartment
        # number this turn (see MAINTENANCE REPORTS, step 1 in the
        # system prompt) - nothing to classify or save yet.
        return

    # The AI builds this from the whole conversation, not just the
    # current message, since the problem may have been described a
    # turn or two before the tenant gave their unit number.
    description = str(result.get("maintenance_description") or "").strip() or (message or "").strip()
    has_image = bool(image_bytes)

    if not description and not has_image:
        return

    try:
        classification = classify_maintenance_ticket(
            message=description,
            image_bytes=image_bytes,
            image_content_type=image_content_type,
        )
    except Exception as e:
        print(f"Maintenance classification failed: {type(e).__name__}: {e}")
        ticket = {"saved": False, "error": str(e)}
        result["maintenance_ticket"] = ticket
        append_line(result, "⚠️ I couldn't process that report automatically - the team will follow up with you directly.")
        return

    existing = None
    if db.ENABLED and conversation_id:
        try:
            existing = db.find_open_maintenance_ticket(conversation_id)
        except Exception as e:
            print(f"Looking up existing maintenance ticket failed (non-fatal): {e}")

    fields = {
        "property_id": property_id,
        "property_title": property_title,
        "tenant_id": tenant_id,
        "issue_type": classification.get("issueType"),
        "urgency": classification.get("urgency"),
        "confidence": classification.get("confidence"),
        "photo_text_match": classification.get("photoTextMatch"),
        "summary": classification.get("summary"),
        "recommended_action": classification.get("recommendedAction"),
        "classification_status": classification.get("status"),
        "ticket_status": _default_ticket_status(classification.get("status")),
    }

    if known_name:
        fields["tenant_name"] = known_name
    if known_phone:
        fields["tenant_phone"] = known_phone

    ticket = {**fields, "message": description, "has_image": has_image, "image_filename": image_filename}

    if not db.ENABLED:
        ticket["saved"] = False
        ticket["save_error"] = "Supabase isn't configured, so this report wasn't saved."
        action = "not_saved"
    else:
        try:
            if existing:
                merged_message = str(existing.get("message") or "").strip()
                if description and description not in merged_message:
                    merged_message = f"{merged_message}\n{description}".strip() if merged_message else description
                update_fields = {
                    **fields,
                    "message": merged_message,
                    "has_image": bool(existing.get("has_image")) or has_image,
                    "image_filename": image_filename or existing.get("image_filename"),
                }
                saved = db.update_maintenance_ticket(existing["id"], update_fields)
                action = "updated"
            else:
                saved = db.create_maintenance_ticket({
                    **ticket,
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                })
                action = "created"

            if not isinstance(saved, dict) or not saved.get("id"):
                raise RuntimeError("Database did not acknowledge the saved ticket")

            ticket = saved
            ticket["saved"] = True

        except Exception as e:
            print(f"Saving maintenance ticket failed (non-fatal): {e}")
            ticket["saved"] = False
            ticket["save_error"] = str(e)
            action = "not_saved"

    ticket["action"] = action
    result["maintenance_ticket"] = ticket

    label = ISSUE_LABELS.get(ticket.get("issue_type") or classification.get("issueType"), "maintenance")
    urgency = ticket.get("urgency") or classification.get("urgency")
    status = ticket.get("classification_status") or classification.get("status")

    if status == "rejected":
        line = "That doesn't sound like a property maintenance issue, so I haven't logged a ticket for it."
    elif action == "updated":
        line = f"🔧 Updated your {label} report" + (f" ({urgency} priority)" if urgency else "") + " with this new information."
    elif status == "needs_review":
        line = "🔧 Thanks - I've flagged this for the team to review directly."
    else:
        line = f"🔧 Logged as a {label} issue" + (f" ({urgency} priority)" if urgency else "") + " for the team."

    # Whether it actually saved is a team/ops detail (visible in the
    # Maintenance dashboard tab and the chat's own analysis panel via
    # ticket["saved"]/["save_error"]) - not something to put in front of
    # the tenant, who can't do anything about it.
    append_line(result, line)


def list_tickets(status: Optional[str] = None, limit: int = 200):
    return db.list_maintenance_tickets(status=status, limit=limit)


def update_ticket(ticket_id: str, fields: dict):
    return db.update_maintenance_ticket(ticket_id, fields)
