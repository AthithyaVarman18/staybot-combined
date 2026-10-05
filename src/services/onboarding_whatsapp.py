"""
WhatsApp for onboarding cases: invitations, final lease PDF delivery,
delivery-status webhooks and step-by-step self-service replies.

Honest delivery states
  queued/sending  we are about to call WhatsApp
  accepted        WhatsApp accepted the request (NOT proof of delivery)
  sent/delivered/read  only from WhatsApp status webhooks
  failed          the request or a status webhook reported a failure
In test mode nothing is sent: rows are marked test_mode and the UI labels
them "Simulated". Test cases (is_test) never message real numbers unless the
number is listed in WHATSAPP_TEST_RECIPIENTS.

Business-initiated messages outside WhatsApp's 24-hour customer service
window must use a pre-approved template (configure the names below); inside
the window a normal text/document message is used.

.env:
  WHATSAPP_TEMPLATE_INVITE=onboarding_invite            # body: {{1}} name, {{2}} home, {{3}} secure link
  WHATSAPP_TEMPLATE_LEASE_READY=lease_ready             # document header; body: {{1}} name, {{2}} agreement reference
  WHATSAPP_TEMPLATE_LANGUAGE=en_US
  WHATSAPP_TEST_RECIPIENTS=+17045550101,+17045550102    # numbers allowed to receive messages from test cases
"""

import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import requests

from src.services import db, onboarding_cases as oc, onboarding_handoff, whatsapp

TEMPLATE_INVITE = (os.getenv("WHATSAPP_TEMPLATE_INVITE") or "").strip()
TEMPLATE_LEASE_READY = (os.getenv("WHATSAPP_TEMPLATE_LEASE_READY") or "").strip()
TEMPLATE_LANGUAGE = (os.getenv("WHATSAPP_TEMPLATE_LANGUAGE") or "en_US").strip()
TEST_RECIPIENTS = {p.strip() for p in (os.getenv("WHATSAPP_TEST_RECIPIENTS") or "").split(",") if p.strip()}
GRAPH = "https://graph.facebook.com"


def setup_status():
    live = not whatsapp.DRY_RUN
    return {
        "mode": "live" if live else "test",
        "label": "Live: messages are really sent" if live else "Test mode: nothing is sent to WhatsApp; deliveries are simulated and labelled",
        "credentials": {"access_token": bool(whatsapp.ACCESS_TOKEN), "phone_number_id": bool(whatsapp.PHONE_NUMBER_ID),
                        "app_secret": bool(whatsapp.APP_SECRET), "verify_token": bool(whatsapp.VERIFY_TOKEN)},
        "templates": {"invite": TEMPLATE_INVITE or None, "lease_ready": TEMPLATE_LEASE_READY or None, "language": TEMPLATE_LANGUAGE},
        "needs": [item for item, missing in (
            ("WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID, then WHATSAPP_DRY_RUN=false", not live),
            ("WHATSAPP_APP_SECRET so delivery webhooks can be verified", live and not whatsapp.APP_SECRET),
            ("An approved invitation template (WHATSAPP_TEMPLATE_INVITE)", not TEMPLATE_INVITE),
            ("An approved lease-ready template with a document header (WHATSAPP_TEMPLATE_LEASE_READY)", not TEMPLATE_LEASE_READY),
        ) if missing],
    }


def test_mode_for(case, phone):
    return whatsapp.DRY_RUN or (case.get("is_test") and phone not in TEST_RECIPIENTS)


def digits(phone):
    return re.sub(r"\D", "", phone or "")


ASSUME_WINDOW_FOR_TEST_RECIPIENTS = (os.getenv("WHATSAPP_ASSUME_WINDOW_FOR_TEST_RECIPIENTS") or "").strip().lower() == "true"


def window_open(phone):
    """True if this person messaged us in the last 24 hours (free-form replies allowed).

    Local testing: with no public webhook, inbound messages never reach this
    server, so a tester who really did message the business number looks silent.
    WHATSAPP_ASSUME_WINDOW_FOR_TEST_RECIPIENTS=true assumes the window is open
    for the numbers in WHATSAPP_TEST_RECIPIENTS only. WhatsApp still refuses the
    message if the window is actually closed, and that failure is recorded."""

    if ASSUME_WINDOW_FOR_TEST_RECIPIENTS and phone in TEST_RECIPIENTS:
        return True
    try:
        conversation = db.get_conversation(whatsapp.session_for(phone), None)
        if not conversation:
            return False
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        rows = db._get("messages", {"conversation_id": f"eq.{conversation['id']}", "role": "eq.user",
                                    "created_at": f"gte.{since}", "select": "id", "limit": "1"})
        return bool(rows)
    except Exception:
        return False


def claim(case, kind, party, phone, key, final_document_id=None):
    """Create or fetch the message row for an idempotency key, and claim it for
    sending. Returns (row, claimed). A row that is already on its way or
    delivered is never sent again."""
    existing = oc.get("onboarding_whatsapp_messages", {"idempotency_key": f"eq.{key}"})
    if not existing:
        try:
            existing = [oc.insert("onboarding_whatsapp_messages", {
                "case_id": case["id"], "kind": kind, "party": party, "to_phone": phone, "idempotency_key": key,
                "final_document_id": final_document_id, "status": "queued", "test_mode": bool(test_mode_for(case, phone))})]
        except Exception:
            existing = oc.get("onboarding_whatsapp_messages", {"idempotency_key": f"eq.{key}"})
    row = existing[0]
    if row["status"] not in ("queued", "failed"):
        return row, False
    claimed = oc.patch("onboarding_whatsapp_messages",
                       {"status": "sending", "attempts": row["attempts"] + 1, "error": None, "updated_at": oc.now_iso(),
                        "test_mode": bool(test_mode_for(case, phone))},
                       {"id": f"eq.{row['id']}", "status": f"eq.{row['status']}", "attempts": f"eq.{row['attempts']}"})
    if not claimed:
        return oc.one("onboarding_whatsapp_messages", {"id": f"eq.{row['id']}"}), False
    return claimed, True


def finish(row, *, provider_id=None, error=None, test_mode=False, actor="system"):
    fields = {"status": "failed" if error else "accepted", "error": error, "updated_at": oc.now_iso(), "status_updated_at": oc.now_iso()}
    if provider_id:
        fields["provider_message_id"] = provider_id
    updated = oc.patch("onboarding_whatsapp_messages", fields, {"id": f"eq.{row['id']}", "status": "eq.sending"}) or row
    oc.audit(row["case_id"], actor, f"whatsapp_{row['kind']}_{'failed' if error else 'accepted'}",
             {"message_id": row["id"], "party": row["party"], "test_mode": test_mode, "error": error, "attempt": row["attempts"]})
    return updated


def graph_post(path, **kwargs):
    response = requests.post(f"{GRAPH}/{whatsapp.API_VERSION}/{path}", headers={"Authorization": f"Bearer {whatsapp.ACCESS_TOKEN}"},
                             timeout=60, **kwargs)
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if not response.ok:
        detail = (data.get("error") or {}).get("message") or response.text[:200]
        raise RuntimeError(f"WhatsApp API {response.status_code}: {detail}")
    return data


def send_payload(to_phone, payload):
    data = graph_post(f"{whatsapp.PHONE_NUMBER_ID}/messages",
                      json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": digits(to_phone), **payload})
    return (data.get("messages") or [{}])[0].get("id")


def first_name(person):
    return (person.get("full_name") or "there").split()[0]


def home_label(view):
    unit = f", unit {view['unit']}" if view.get("unit") else ""
    return f"{view['property']['title']}{unit}"


def send_invite(case, view, party, person, url, link, actor):
    phone = person["whatsapp"]
    row, claimed = claim(case, "invite", party, phone, f"invite:{link['id']}")
    if not claimed:
        return row
    text = (f"Hi {first_name(person)}, this is Staybot. Your onboarding as the {party} for {home_label(view)} is ready.\n\n"
            f"Confirm your details, upload documents and review the agreement here (private link, expires in {oc.LINK_DAYS} days):\n{url}\n\n"
            "Reply HELP any time to reach our team.")
    if row["test_mode"]:
        whatsapp.OUTBOX.append({"to": digits(phone), "text": text, "at": oc.now_iso(), "dry_run": True, "kind": "onboarding_invite"})
        return finish(row, provider_id=f"test.{uuid.uuid4()}", test_mode=True, actor=actor)
    try:
        if window_open(phone):
            provider_id = send_payload(phone, {"type": "text", "text": {"preview_url": True, "body": text}})
        elif TEMPLATE_INVITE:
            provider_id = send_payload(phone, {"type": "template", "template": {
                "name": TEMPLATE_INVITE, "language": {"code": TEMPLATE_LANGUAGE},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": v} for v in (first_name(person), home_label(view), url)]}]}})
        else:
            return finish(row, error="No approved invitation template is configured and the person has not messaged in the last 24 hours.", actor=actor)
        return finish(row, provider_id=provider_id, actor=actor)
    except Exception as error:
        return finish(row, error=str(error)[:300], actor=actor)


def send_final_pdf(case, party, person, final, data, actor):
    phone = person["whatsapp"]
    row, claimed = claim(case, "final_pdf", party, phone, f"final:{final['id']}:{party}", final["id"])
    if not claimed:
        return row
    filename = f"Lease-{case['reference']}.pdf"
    caption = (f"Hi {first_name(person)}, here is your final lease agreement {case['reference']}"
               f"{' (DEMO - testing only)' if final['is_demo'] else ''}. Both parties approved this exact version. "
               "Reply HELP if anything looks wrong.")
    if row["test_mode"]:
        whatsapp.OUTBOX.append({"to": digits(phone), "text": f"[document {filename}, sha256 {final['sha256'][:12]}] {caption}",
                                "at": oc.now_iso(), "dry_run": True, "kind": "final_lease_pdf", "final_document_id": final["id"]})
        return finish(row, provider_id=f"test.{uuid.uuid4()}", test_mode=True, actor=actor)
    try:
        media = graph_post(f"{whatsapp.PHONE_NUMBER_ID}/media", data={"messaging_product": "whatsapp", "type": "application/pdf"},
                           files={"file": (filename, data, "application/pdf")})
        document = {"id": media["id"], "filename": filename}
        if window_open(phone):
            provider_id = send_payload(phone, {"type": "document", "document": {**document, "caption": caption[:1024]}})
        elif TEMPLATE_LEASE_READY:
            provider_id = send_payload(phone, {"type": "template", "template": {
                "name": TEMPLATE_LEASE_READY, "language": {"code": TEMPLATE_LANGUAGE},
                "components": [{"type": "header", "parameters": [{"type": "document", "document": document}]},
                               {"type": "body", "parameters": [{"type": "text", "text": first_name(person)}, {"type": "text", "text": case["reference"]}]}]}})
        else:
            return finish(row, error="No approved lease-ready template is configured and the person has not messaged in the last 24 hours.", actor=actor)
        return finish(row, provider_id=provider_id, actor=actor)
    except Exception as error:
        return finish(row, error=str(error)[:300], actor=actor)


# ---------------------------------------------------------------------
# Delivery status webhooks
# ---------------------------------------------------------------------

def parse_statuses(payload):
    out = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for s in (change.get("value") or {}).get("statuses") or []:
                if s.get("id") and s.get("status") in ("sent", "delivered", "read", "failed"):
                    out.append(s)
    return out


def apply_status(status):
    """Idempotent: each (message, status, timestamp) event is applied once, and
    statuses only move forward (a late 'sent' never overrides 'read')."""
    key = f"{status['id']}:{status['status']}:{status.get('timestamp', '')}"
    ts = None
    if str(status.get("timestamp", "")).isdigit():
        ts = datetime.fromtimestamp(int(status["timestamp"]), timezone.utc).isoformat()
    try:
        db._post("onboarding_whatsapp_status_events", {"event_key": key, "provider_message_id": status["id"], "status": status["status"],
                                                        "provider_timestamp": ts, "payload": status})
    except requests.HTTPError as error:
        if getattr(error.response, "status_code", None) == 409:
            return {"event": key, "applied": False, "reason": "duplicate"}
        raise
    rows = db._get("onboarding_whatsapp_messages", {"provider_message_id": f"eq.{status['id']}", "select": "*"})
    if not rows:
        return {"event": key, "applied": False, "reason": "not an onboarding message"}
    row = rows[0]
    new = status["status"]
    if new == "failed":
        allowed = ["accepted", "sent", "sending"]
        errors = "; ".join(f"{e.get('code')}: {e.get('title') or e.get('message')}" for e in status.get("errors") or []) or "WhatsApp reported a failure."
        fields = {"status": "failed", "error": errors[:300]}
    else:
        allowed = [s for s, rank in oc.STATUS_RANK.items() if rank < oc.STATUS_RANK[new] and s not in ("queued", "sending")]
        fields = {"status": new}
    updated = db._patch("onboarding_whatsapp_messages", {**fields, "status_updated_at": ts or oc.now_iso(), "updated_at": oc.now_iso()},
                        {"id": f"eq.{row['id']}", "status": f"in.({','.join(allowed)})"})
    applied = bool(updated)
    db._post("onboarding_audit_log", {"case_id": row["case_id"], "actor": "whatsapp", "action": f"whatsapp_status_{new}",
                                      "details": {"message_id": row["id"], "party": row["party"], "applied": applied,
                                                  "test_mode": row["test_mode"]}})
    return {"event": key, "applied": applied}


def simulated_status_payload(provider_message_id, status, recipient, error_title=None):
    s = {"id": provider_message_id, "status": status, "timestamp": str(int(datetime.now(timezone.utc).timestamp())), "recipient_id": digits(recipient)}
    if status == "failed":
        s["errors"] = [{"code": 131026, "title": error_title or "Simulated failure (test mode)"}]
    return {"object": "whatsapp_business_account", "entry": [{"id": "TEST", "changes": [{"field": "messages", "value": {"statuses": [s]}}]}]}


# ---------------------------------------------------------------------
# Self-service replies for invited tenants/owners
# ---------------------------------------------------------------------

YES = re.compile(r"^\s*(1|yes|y|yeah|yep|correct|right|ok|okay|confirm)\b", re.I)
NO = re.compile(r"^\s*(2|no|n|nope|wrong)\b", re.I)
QUESTIONS = ("role", "name", "email", "language", "communication")


def find_invited_case(phone):
    people = db._get("onboarding_people", {"whatsapp": f"eq.{phone}", "select": "id"})
    if not people:
        return None
    pid = people[0]["id"]
    for party in oc.PARTIES:
        cases = db._get("onboarding_cases", {f"{party}_id": f"eq.{pid}", "status": "in.(active,finalized)", "order": "updated_at.desc", "select": "*"})
        for case in cases:
            progress = db._get("onboarding_party_progress", {"case_id": f"eq.{case['id']}", "party": f"eq.{party}", "select": "*"})
            if progress and progress[0].get("invited_at"):
                return case, party, progress[0]
    return None


def question(item, view, party, person):
    if item == "role":
        return (f"Hi {first_name(person)}! Staybot here. I have you as the {party.upper()} for {home_label(view)} "
                f"(case {view['reference']}). Is that right?\n1 Yes\n2 No, that's not me / wrong role\n\nReply HELP any time for staff.")
    if item == "name":
        return f"Is your full legal name {person['full_name']}?\n1 Yes\nOr type your correct full name."
    if item == "email":
        current = f" We have {person['email']}." if person.get("email") else ""
        return f"What email should go on the lease?{current} Type it, or reply SKIP."
    if item == "language":
        return "Which language do you prefer?\n1 English\n2 Español (staff will follow up)"
    return "How should we keep in touch about this lease?\n1 WhatsApp only\n2 WhatsApp and email"


def status_summary(view, party):
    lines = []
    docs = [d for d in view["documents"] if d["party"] == party and d["required"] and d["status"] in ("not_uploaded", "changes_required")]
    if docs:
        lines.append("Documents still needed: " + ", ".join(d["label"] for d in docs) + ". Please upload them with your secure link.")
    agreement = view["agreement"]
    if view["status"] == "finalized":
        lines.append("Your final lease PDF is ready and will be sent here.")
    elif agreement["latest_version"] and agreement["is_current"]:
        decision = agreement["approvals"][party]["status"]
        if decision == "approved":
            lines.append(f"You approved agreement version {agreement['latest_version']['version']}. Thank you!")
        else:
            lines.append(f"Agreement version {agreement['latest_version']['version']} is ready for you to review and approve with your secure link.")
    else:
        lines.append("We'll message you when the agreement is ready to review.")
    lines.append("Reply LINK for a new secure link, or HELP for staff.")
    return "\n".join(lines)


def update_completion(case_id, party):
    view = oc.case_view(case_id)
    progress = view["party_progress"][party]
    confirmed = progress.get("confirmed") or {}
    docs_done = not any(d["party"] == party and d["required"] and d["status"] in ("not_uploaded", "changes_required") for d in view["documents"])
    approved = view["agreement"]["is_current"] and view["agreement"]["approvals"][party]["status"] == "approved"
    if all(q in confirmed for q in QUESTIONS) and docs_done and approved and not progress.get("self_service_done_at"):
        oc.progress_upsert(case_id, party, {"self_service_done_at": oc.now_iso()})


def handle_incoming(message, base_url):
    """Returns the reply text if this sender is an invited tenant/owner, else None."""
    phone = "+" + digits(message.get("from"))
    found = find_invited_case(phone)
    if not found:
        return None
    case, party, progress = found
    view = oc.case_view(case["id"])
    person = db._get("onboarding_people", {"id": f"eq.{case[f'{party}_id']}", "select": "*"})[0]
    text = (message.get("text") or "").strip()
    confirmed = dict(progress.get("confirmed") or {})
    fields = {"first_response_at": progress.get("first_response_at") or oc.now_iso()}
    actor = f"{party}:whatsapp:{phone}"

    category = onboarding_handoff.classify(text)
    if category:
        oc.raise_escalation(case, view, party, category, text or "Asked for help")
        oc.progress_upsert(case["id"], party, fields)
        return "Thanks, I've passed this to our team. A staff member will reply here soon."

    if text.lower() == "link":
        if case["status"] != "active":
            return "This onboarding is already finalized. Reply HELP if you need anything."
        link, url = oc.issue_link(oc.load_case(case["id"]), party, actor, base_url)
        oc.progress_upsert(case["id"], party, fields)
        return f"Here is your new private link (expires in {oc.LINK_DAYS} days):\n{url}"

    awaiting = progress.get("awaiting")
    if awaiting and case["status"] == "active":
        if awaiting == "role":
            if YES.match(text):
                confirmed["role"] = True
            elif NO.match(text):
                oc.raise_escalation(case, view, party, "conflict", f"Says they are not the {party} on this case: {text}")
                oc.progress_upsert(case["id"], party, {**fields, "awaiting": None})
                return "Thanks for telling us. I've asked a staff member to check the case and contact you."
        elif awaiting == "name":
            if YES.match(text):
                confirmed["name"] = True
            elif 2 <= len(text) <= 200 and not text.isdigit():
                new_name = " ".join(text.split())
                db._patch("onboarding_people", {"full_name": new_name, "updated_at": oc.now_iso()}, {"id": f"eq.{person['id']}"})
                oc.audit(case["id"], actor, f"{party}_self_corrected_name", {"person_id": person["id"]})
                person["full_name"] = new_name
                confirmed["name"] = True
                oc.refresh_agreement_if_started(case["id"], actor)
        elif awaiting == "email":
            if text.lower() == "skip":
                confirmed["email"] = "skipped"
            elif re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
                db._patch("onboarding_people", {"email": text, "updated_at": oc.now_iso()}, {"id": f"eq.{person['id']}"})
                oc.audit(case["id"], actor, f"{party}_self_updated_email", {"person_id": person["id"]})
                person["email"] = text
                confirmed["email"] = True
                oc.refresh_agreement_if_started(case["id"], actor)
        elif awaiting == "language" and text[:1] in ("1", "2"):
            db._patch("onboarding_people", {"preferred_language": "en" if text[:1] == "1" else "es"}, {"id": f"eq.{person['id']}"})
            confirmed["language"] = True
        elif awaiting == "communication" and text[:1] in ("1", "2"):
            comm = {**(person.get("communication") or {}), "channel": "whatsapp" if text[:1] == "1" else "whatsapp_and_email"}
            db._patch("onboarding_people", {"communication": comm}, {"id": f"eq.{person['id']}"})
            confirmed["communication"] = True

    # Next unanswered question; confirmed details are never asked again.
    next_item = next((item for item in QUESTIONS if item not in confirmed), None) if case["status"] == "active" else None
    oc.progress_upsert(case["id"], party, {**fields, "confirmed": confirmed, "awaiting": next_item})
    if next_item:
        reply = question(next_item, view, party, person)
        if awaiting == next_item and text:
            reply = "Sorry, I didn't catch that. " + reply
        return reply
    update_completion(case["id"], party)
    return "All your details are confirmed. " + status_summary(oc.case_view(case["id"]), party)
