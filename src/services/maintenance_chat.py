"""
Maintenance chat assistant - the tenant's Maintenance tab (src/services/rentals.py,
POST /me/rentals/maintenance/chat).

The tenant talks to the AI about a problem in the home they rent. The AI:
  - asks one short question at a time to understand it,
  - suggests only simple, safe things a tenant can try (reset a breaker,
    plunge a toilet, check thermostat batteries...),
  - for an emergency (gas, fire/smoke, sparking, flooding, CO alarm, break-in)
    gives safety steps and hands it on straight away,
  - hands it on when it can't be fixed by the tenant, a self-help step didn't
    work, or the tenant asks for someone.

"Hands it on" = escalate(): a maintenance ticket for the home's owner and the
team (same ticket as every other report, so the owner sees it on their Tenants
tab and staff on the Maintenance tab), carrying an AI summary of the whole
chat and the transcript itself. Later messages in the same chat update that
ticket's summary and transcript.

The same assistant also answers repair problems raised in the main Chat tab
and on WhatsApp (handle_chat_turn(), called from chat.py), so a leak gets the
same help, safety steps, summary and owner/team hand-off wherever it's
reported. A WhatsApp number that isn't a known tenant's gets the assistant
too, and its ticket goes to the team marked "home not identified".
"""

import json
from typing import Optional

from src.services import db, email_sender, maintenance
from src.services import real_estate_ai as ai
from src.services.maintenance_ai import make_image_data_url

ISSUE_TYPES = list(maintenance.ISSUE_LABELS)

SYSTEM_PROMPT = f"""You are Staybot's maintenance assistant, chatting with a tenant about a problem in the home they rent: {{home}}.

Your job: understand the problem, help with simple safe fixes, and hand it to the owner and the property team when it needs them.

Rules:
- Be warm and brief (2-4 short sentences). Ask at most ONE question per reply.
- Never ask for a unit or apartment number or which property - you already know the home.
- First understand: what is wrong, where in the home, since when, how bad (e.g. is water still running).
- Only suggest things any tenant can do safely: reset a tripped breaker or GFCI outlet, use a plunger, turn off the shut-off valve under a sink/toilet, check thermostat settings or batteries, replace a bulb, check the appliance is plugged in. Never suggest opening walls, electrical panels, gas lines, or climbing on the roof.
- EMERGENCY (smell of gas, fire or smoke, sparking/burning smell from outlets, carbon monoxide alarm, flooding or water near electrics, sewage backup, no heat in freezing weather, break-in or broken door lock): give the key safety steps first (e.g. gas: leave the home now, don't use switches or flames, call the gas company / 911 from outside), set emergency true, urgency "urgent", action "escalate".
- Set action "escalate" when: it's an emergency; it needs a professional or a repair (leaks that don't stop, broken appliance, no hot water, pests, damage, mould, anything structural); a suggested self-help step didn't work; or the tenant asks for someone to come. When you escalate, tell the tenant you've sent it to their owner and the team, who will follow up. Never promise a time or a cost.
- Set action "resolved" only when the tenant confirms a self-help step fixed it.
- Otherwise action "continue".
- If a photo is attached, describe what you can actually see and use it.
- If the home is "not identified yet": the rule about never asking for the unit does NOT apply. Until the
  tenant has told you, also ask (once, in the same reply as your question about the problem) for the street
  address and unit/apartment number, and put what they gave in "home_address".

Return ONLY a JSON object:
{{
  "reply": "your message to the tenant",
  "action": "continue" | "escalate" | "resolved",
  "emergency": true | false,
  "issue_type": one of {json.dumps(ISSUE_TYPES)} or null,
  "urgency": "urgent" | "normal" | "low" | null,
  "escalation_reason": "short reason, if escalating" or null,
  "home_address": "street address / unit the tenant gave, only when the home is not identified yet" or null,
  "summary": "2-4 sentence summary of the WHOLE conversation for the owner and property team: the problem, where in the home, since when, what the tenant already tried, current state, and anything they asked for. Write it even when action is continue."
}}"""

MAX_TURNS = 30


def clean_history(history) -> list[dict]:
    """[{role: tenant|assistant, content}] from the browser, trimmed."""
    out = []
    for m in (history if isinstance(history, list) else [])[-MAX_TURNS:]:
        if not isinstance(m, dict):
            continue
        role = "assistant" if m.get("role") == "assistant" else "tenant"
        text = str(m.get("content") or "").strip()[:2000]
        if text:
            out.append({"role": role, "content": text, **({"photo": True} if m.get("photo") else {})})
    return out


def ask_ai(home: dict, history: list[dict], message: str,
           image_bytes: Optional[bytes] = None, image_content_type: Optional[str] = None) -> dict:
    where = home.get("title") or "their home"
    if home.get("location"):
        where += f", {home['location']}"
    if home.get("unidentified"):
        where = "a home that is not identified yet"

    messages = [{"role": "system", "content": SYSTEM_PROMPT.replace("{home}", where)}]
    for m in history:
        messages.append({"role": "assistant" if m["role"] == "assistant" else "user", "content": m["content"]})

    text = message or "[The tenant sent a photo without text]"
    if image_bytes:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": make_image_data_url(image_bytes, image_content_type)}},
        ]})
    else:
        messages.append({"role": "user", "content": text})

    ready, _ = ai.models_in_order()
    errors = []
    for model in ready:
        try:
            out = ai.parse_model_json(ai.call_model(model, messages))
            if str(out.get("reply") or "").strip():
                return normalize(out)
            errors.append(f"{model}: empty reply")
        except Exception as e:
            errors.append(f"{model}: {e}")
    raise RuntimeError("Maintenance assistant unavailable: " + "; ".join(errors))


def normalize(out: dict) -> dict:
    action = out.get("action") if out.get("action") in ("continue", "escalate", "resolved") else "continue"
    urgency = out.get("urgency") if out.get("urgency") in ("urgent", "normal", "low") else None
    emergency = bool(out.get("emergency"))
    if emergency:
        action, urgency = "escalate", "urgent"
    issue = out.get("issue_type") if out.get("issue_type") in ISSUE_TYPES else None
    return {
        "reply": str(out.get("reply") or "").strip(),
        "action": action,
        "emergency": emergency,
        "issue_type": issue,
        "urgency": urgency,
        "escalation_reason": (str(out.get("escalation_reason") or "").strip() or None),
        "summary": str(out.get("summary") or "").strip(),
        "home_address": (str(out.get("home_address") or "").strip() or None),
    }


def transcript_text(transcript: list[dict]) -> str:
    return "\n".join(
        f"{'Assistant' if m['role'] == 'assistant' else 'Tenant'}: {m['content']}" + (" [photo]" if m.get("photo") else "")
        for m in transcript
    )


def save_chat_fields(ticket_id: str, turn: dict, transcript: list[dict], fallback_message: str) -> dict:
    """Store the summary + transcript on the ticket. Needs
    supabase_maintenance_chat.sql; without it they're folded into the
    ticket's summary/message columns so the owner and team still see them."""
    fields = {"chat_summary": turn["summary"] or None, "chat_transcript": transcript,
              "escalation_reason": turn.get("escalation_reason")}
    if turn.get("emergency") or turn.get("urgency") == "urgent":
        fields["urgency"] = "urgent"
    try:
        return db.update_maintenance_ticket(ticket_id, fields) or {}
    except Exception as e:
        print(f"Chat columns missing (run supabase_maintenance_chat.sql) - folding into message: {e}")
        legacy = {"summary": turn["summary"] or None,
                  "message": f"{fallback_message}\n\n--- Chat ---\n{transcript_text(transcript)}".strip()[:20000]}
        if "urgency" in fields:
            legacy["urgency"] = "urgent"
        try:
            return db.update_maintenance_ticket(ticket_id, legacy) or {}
        except Exception as e2:
            print(f"Saving chat to ticket failed (non-fatal): {e2}")
            return {}


def escalate(turn: dict, transcript: list[dict], home: dict, account: dict, phone: Optional[str],
             image_bytes=None, image_content_type=None, image_filename=None,
             conversation_id: Optional[str] = None) -> dict:
    """Create the ticket for the owner + team from this chat. conversation_id:
    the Chat / WhatsApp conversation it came from (None for the Maintenance tab)."""
    description = turn["summary"] or transcript_text([m for m in transcript if m["role"] == "tenant"])
    unit = home.get("location") or home.get("title")
    if home.get("unidentified"):
        unit = turn.get("home_address") or home.get("unit") or "Not identified"
        turn["escalation_reason"] = "⚠️ Home not identified - please check which property this is. " + (turn.get("escalation_reason") or "")
    result = {"intent": "maintenance_issue", "tenant_id": unit,
              "maintenance_description": description, "response": ""}
    maintenance.handle_maintenance_report(
        result, message=description, property_id=home.get("property_id"),
        property_title=home.get("title") if not home.get("unidentified") else "Home not identified",
        conversation_id=conversation_id, session_id=account["session_id"], known_phone=phone,
        known_name=account.get("name"), image_bytes=image_bytes, image_content_type=image_content_type,
        image_filename=image_filename,
    )
    ticket = result.get("maintenance_ticket") or {}
    if not ticket.get("saved"):
        raise RuntimeError(ticket.get("save_error") or ticket.get("error") or "The request couldn't be saved.")
    # The classifier can reject a chat summary as "not a property issue";
    # a tenant who chose to send it to their owner still gets it seen.
    if ticket.get("ticket_status") == "dismissed":
        ticket = db.update_maintenance_ticket(ticket["id"], {"ticket_status": "needs_review",
                                                             "classification_status": "needs_review"}) or ticket
    if turn.get("issue_type") and not ticket.get("issue_type"):
        ticket = db.update_maintenance_ticket(ticket["id"], {"issue_type": turn["issue_type"]}) or ticket
    saved = save_chat_fields(ticket["id"], turn, transcript, description)
    ticket = {**ticket, **saved}
    notify(ticket, turn, home)
    return ticket


def update_escalated(ticket_id: str, turn: dict, transcript: list[dict]) -> dict:
    ticket = db.get_maintenance_ticket(ticket_id) or {}
    saved = save_chat_fields(ticket_id, turn, transcript, str(ticket.get("message") or ""))
    return {**ticket, **saved}


def notify(ticket: dict, turn: dict, home: dict):
    """Email the home's owner (and MAINTENANCE_ALERT_EMAIL for the team, if
    set). Test mode (no SMTP configured) only records it in the outbox."""
    import os
    from src.services import rentals  # local import: rentals imports this module
    to = []
    try:
        app = rentals.one("rental_applications", {"id": f"eq.{home['application_id']}"}) if home.get("application_id") else None
        if app and app.get("owner_session_id"):
            owner = rentals.one("accounts", {"session_id": f"eq.{app['owner_session_id']}"})
            if owner and owner.get("email"):
                to.append(owner["email"])
    except Exception as e:
        print(f"Owner lookup for maintenance email failed (non-fatal): {e}")
    team = (os.getenv("MAINTENANCE_ALERT_EMAIL") or "").strip()
    if team:
        to.append(team)
    urgent = (ticket.get("urgency") or turn.get("urgency")) == "urgent"
    label = maintenance.ISSUE_LABELS.get(ticket.get("issue_type") or turn.get("issue_type"), "maintenance")
    subject = f"{'URGENT: ' if urgent else ''}{label} request - {home.get('title')}"
    body = (f"{ticket.get('tenant_name') or 'Your tenant'} reported a {label} problem at {home.get('title')}.\n\n"
            f"Summary: {turn.get('summary') or ticket.get('summary') or ''}\n\n"
            f"Suggested next step: {ticket.get('recommended_action') or '-'}\n\n"
            "See the full conversation in Staybot (Tenants tab for owners, Maintenance tab for the team).")
    for addr in to:
        try:
            email_sender.send(addr, subject, body, "<pre style='font-family:inherit;white-space:pre-wrap'>"
                              + body.replace("&", "&amp;").replace("<", "&lt;") + "</pre>")
        except Exception as e:
            print(f"Maintenance email to {addr} failed (non-fatal): {e}")


# ---------------------------------------------------------------------
# The same assistant inside the main Chat tab and WhatsApp (chat.py)
# ---------------------------------------------------------------------

# A repair conversation carries on until the tenant clearly moves to
# something else - these intents mean they have.
LEAVE_INTENTS = {"property_search", "property_inquiry", "schedule_viewing", "application", "rent_question",
                 "buy_question", "negotiate", "list_property", "update_property", "investment_enquiry",
                 "deal_analysis"}

THREAD_MESSAGES = 30


def _repair_thread(conversation_id: Optional[str]) -> tuple[list[dict], Optional[dict]]:
    """(transcript of the repair conversation so far, its latest state) from
    the saved Chat messages - each assistant reply the repair assistant gave
    carries analysis.repair_assistant. ([], None) when the last reply wasn't
    the repair assistant's, or there's no database."""
    if not (db.ENABLED and conversation_id):
        return [], None
    try:
        rows = db.list_messages(conversation_id)[-THREAD_MESSAGES:]
    except Exception as e:
        print(f"Loading the repair conversation failed (non-fatal): {e}")
        return [], None
    thread, state = [], None
    for m in reversed(rows):
        if m.get("role") == "assistant":
            repair = (m.get("analysis") or {}).get("repair_assistant") if isinstance(m.get("analysis"), dict) else None
            if not repair:
                break
            state = state or repair
            thread.append({"role": "assistant", "content": str(m.get("content") or "")[:2000]})
        elif m.get("role") == "user":
            thread.append({"role": "tenant", "content": str(m.get("content") or "")[:2000]})
    thread.reverse()   # starts with the message(s) that opened the repair conversation
    return thread, state


def _open_ticket_id(state: Optional[dict]) -> Optional[str]:
    """The ticket this repair conversation already created, if it's still
    open - a resolved or dismissed one isn't updated by new messages."""
    ticket_id = (state or {}).get("ticket_id")
    if not ticket_id:
        return None
    try:
        ticket = db.get_maintenance_ticket(ticket_id)
    except Exception as e:
        print(f"Looking up the repair ticket failed (non-fatal): {e}")
        return None
    return ticket_id if ticket and ticket.get("ticket_status") in maintenance.OPEN_TICKET_STATUSES else None


def handle_chat_turn(result: dict, message: str, conversation_id: Optional[str], session_id: Optional[str],
                     home: Optional[dict], customer_name: Optional[str] = None, phone: Optional[str] = None,
                     image_bytes=None, image_content_type=None, image_filename=None) -> bool:
    """Answer this Chat / WhatsApp turn with the repair assistant when it's a
    repair problem (the chat AI said intent "maintenance_issue") or the
    tenant is still in a repair conversation. home: the tenant's rented home
    (rentals.tenant_home), a selected listing, or {"unidentified": True}.

    Replaces result["response"] with the assistant's reply, creates the
    owner/team ticket when the assistant hands it on (or keeps the one
    already created up to date), and records the state in
    result["repair_assistant"] so the next turn continues. Returns False -
    leaving the old ticket flow to run - when this isn't a repair turn or the
    assistant couldn't answer."""

    thread, state = _repair_thread(conversation_id)
    leaving = result.get("intent") in LEAVE_INTENTS
    continuing = bool(state) and state.get("action") == "continue" and not leaving
    ticket_id = _open_ticket_id(state) if not leaving else None
    already_sent = bool(ticket_id)
    if result.get("intent") != "maintenance_issue" and not continuing and not already_sent:
        return False
    if not home:
        return False

    try:
        turn = ask_ai(home, clean_history(thread), message, image_bytes, image_content_type)
    except Exception as e:
        print(f"Repair assistant unavailable in chat, using the basic report instead: {e}")
        return False

    transcript = clean_history(thread) + [{"role": "tenant", "content": message or "[photo]",
                                            **({"photo": True} if image_bytes else {})}]
    if turn["reply"]:
        transcript.append({"role": "assistant", "content": turn["reply"]})

    ticket, action = None, None
    if ticket_id:
        try:
            ticket = update_escalated(ticket_id, turn, transcript)
            action = "updated"
        except Exception as e:
            print(f"Updating the repair ticket failed (non-fatal): {e}")
    elif turn["action"] == "escalate":
        try:
            ticket = escalate(turn, transcript, home, {"session_id": session_id, "name": customer_name}, phone,
                              image_bytes, image_content_type, image_filename, conversation_id=conversation_id)
            action = "created"
        except Exception as e:
            print(f"Sending the repair to the owner/team failed: {e}")
            turn["reply"] = (turn["reply"] + "\n\n" if turn["reply"] else "") + \
                "⚠️ I couldn't send this automatically - the team will follow up with you directly."

    result["intent"] = "maintenance_issue"
    result["response"] = turn["reply"] or "I've passed this to your owner and our team."
    if action == "created":
        who = "the team" if home.get("unidentified") else "your owner and our team"
        line = (f"🚨 Marked URGENT and sent to {who}." if (ticket or {}).get("urgency") == "urgent" or turn["emergency"]
                else f"🔧 Sent to {who} with a summary of our chat.")
        if not home.get("unidentified"):
            line += " You can follow it on your Maintenance tab."
        maintenance.append_line(result, line)
    if ticket:
        result["maintenance_ticket"] = {**{k: v for k, v in ticket.items() if k not in ("session_id",)},
                                        "saved": True, "action": action}
    result["repair_assistant"] = {
        "action": "escalate" if (ticket_id or action == "created") else turn["action"],
        "ticket_id": ticket_id or (ticket or {}).get("id"),
        "emergency": turn["emergency"], "urgency": (ticket or {}).get("urgency") or turn["urgency"],
        "summary": turn["summary"],
    }
    return True
