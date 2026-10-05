"""
WhatsApp Cloud API channel.

Meta POSTs every incoming customer message to /webhook/whatsapp. We answer
with the same chat pipeline as the web page (src/services/chat.py) and send
the reply back through the Graph API.

Each phone number is one conversation: session_id "wa:+91XXXXXXXXXX",
general enquiry (no property pre-selected) - EXCEPT a tenant account's own
phone (tenant_account_for_phone): their WhatsApp messages go into the same
conversation as their web Chat, and maintenance problems they report are
filed against the home they rent, so they show on their Maintenance tab. WhatsApp doesn't send earlier
messages, so history is loaded from Supabase.

DRY RUN (default until real credentials are set): replies are kept in an
in-memory outbox instead of being sent to Meta, so the whole flow can be
tested with fake webhook messages from the "WhatsApp test" tab.

.env settings:
    WHATSAPP_DRY_RUN=true            # false to really send
    WHATSAPP_ACCESS_TOKEN=...        # Meta app -> WhatsApp -> API Setup
    WHATSAPP_PHONE_NUMBER_ID=...     # same page
    WHATSAPP_VERIFY_TOKEN=...        # any secret you choose; paste the same in Meta's webhook setup
    WHATSAPP_APP_SECRET=...          # Meta app -> Settings -> Basic; checks webhooks really come from Meta
    WHATSAPP_API_VERSION=v23.0       # Graph API version shown in Meta's API Setup page
"""

import hashlib
import hmac
import os
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from src.services import cache, chat, db
from src.services.viewings import normalize_phone


load_dotenv()

ACCESS_TOKEN = (os.getenv("WHATSAPP_ACCESS_TOKEN") or "").strip()
PHONE_NUMBER_ID = (os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
VERIFY_TOKEN = (os.getenv("WHATSAPP_VERIFY_TOKEN") or "").strip()
APP_SECRET = (os.getenv("WHATSAPP_APP_SECRET") or "").strip()
API_VERSION = (os.getenv("WHATSAPP_API_VERSION") or "v23.0").strip()

# Stay in dry run unless explicitly turned off AND credentials exist.
DRY_RUN = (
    os.getenv("WHATSAPP_DRY_RUN", "true").strip().lower() != "false"
    or not (ACCESS_TOKEN and PHONE_NUMBER_ID)
)

MAX_TEXT_LENGTH = 4096     # WhatsApp's limit for one text message
HISTORY_MESSAGES = 20      # earlier messages given to the AI
OUTBOX_SIZE = 500

FALLBACK_REPLY = (
    "Sorry, I'm having trouble replying right now. "
    "Please send your message again in a minute."
)

UNSUPPORTED_REPLY = (
    "Thanks! I can only read text messages for now. "
    + ("Please type what you're looking for, e.g. \"2BHK in OMR under 25k\"."
       if (os.getenv("BUSINESS_CURRENCY") or "USD").strip().upper() == "INR"
       else "Please type what you're looking for, e.g. \"3 bedrooms in Garner under $2,000\".")
)


# ---------------------------------------------------------------------
# State kept in memory (fine for one server; see README before scaling)
# ---------------------------------------------------------------------

_seen_ids = OrderedDict()          # message id -> time, to ignore Meta's retries
_seen_lock = threading.Lock()
SEEN_TTL_SECONDS = 24 * 3600

_phone_locks = {}                  # one message at a time per customer, in order
_phone_locks_guard = threading.Lock()

_memory_history = {}               # used only when Supabase isn't configured

OUTBOX = deque(maxlen=OUTBOX_SIZE) # dry-run "sent" messages


def status() -> dict:
    return {
        "dry_run": DRY_RUN,
        "access_token_set": bool(ACCESS_TOKEN),
        "phone_number_id_set": bool(PHONE_NUMBER_ID),
        "verify_token_set": bool(VERIFY_TOKEN),
        "app_secret_set": bool(APP_SECRET),
        "api_version": API_VERSION,
    }


def session_for(phone: str) -> str:
    return f"wa:{phone}"


def first_time_seen(message_id: str) -> bool:
    """True the first time a message id arrives; Meta may deliver twice.

    Uses Redis (shared across instances/restarts) when REDIS_URL is set;
    falls back to the old in-memory OrderedDict otherwise, so this still
    works fine for local dev or a single server with no Redis configured.
    """

    if cache.available():
        first = cache.set_nx(f"wa_seen:{message_id}", "1", ex_seconds=SEEN_TTL_SECONDS)
        if first is not None:  # None = Redis unreachable -> in-memory check below
            return first

    now = time.time()

    with _seen_lock:

        while _seen_ids and now - next(iter(_seen_ids.values())) > SEEN_TTL_SECONDS:
            _seen_ids.popitem(last=False)

        if message_id in _seen_ids:
            return False

        _seen_ids[message_id] = now
        return True


def lock_for(phone: str) -> threading.Lock:
    with _phone_locks_guard:
        return _phone_locks.setdefault(phone, threading.Lock())


# ---------------------------------------------------------------------
# Webhook security and parsing
# ---------------------------------------------------------------------

def verify_subscription(mode: str, token: str, challenge: str):
    """Meta's one-time GET check when you save the webhook URL."""

    if mode == "subscribe" and VERIFY_TOKEN and hmac.compare_digest(token or "", VERIFY_TOKEN):
        return challenge

    return None


def signature_ok(raw_body: bytes, signature_header: str) -> bool:
    """Check X-Hub-Signature-256 so only Meta can post to the webhook.
    Without an app secret this is only allowed in dry run; live webhooks
    (including delivery statuses) must be verifiable."""

    if not APP_SECRET:
        return DRY_RUN

    expected = "sha256=" + hmac.new(APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()

    return hmac.compare_digest(expected, signature_header or "")


def parse_incoming(payload: dict) -> list[dict]:
    """
    Pull customer messages out of a Meta webhook payload:
    entry[].changes[].value.messages[] (+ contacts[] for the profile name).
    Delivery/read status updates are ignored.
    """

    incoming = []

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:

            value = change.get("value") or {}
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in value.get("contacts") or []
            }

            for m in value.get("messages") or []:

                kind = m.get("type")
                text = None

                if kind == "text":
                    text = (m.get("text") or {}).get("body")
                elif kind == "button":
                    text = (m.get("button") or {}).get("text")
                elif kind == "interactive":
                    reply = (m.get("interactive") or {})
                    text = ((reply.get("button_reply") or reply.get("list_reply")) or {}).get("title")

                incoming.append({
                    "id": m.get("id"),
                    "from": m.get("from"),
                    "name": names.get(m.get("from")),
                    "type": kind,
                    "text": (text or "").strip() or None,
                    "timestamp": m.get("timestamp"),
                })

    return incoming


def fake_webhook_payload(phone: str, text: str, name: str = None, message_id: str = None) -> dict:
    """A payload shaped exactly like Meta's, for testing without Meta."""

    wa_id = "".join(ch for ch in str(phone) if ch.isdigit())

    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "TEST_WABA_ID",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "910000000000", "phone_number_id": "TEST_PHONE_ID"},
                    "contacts": [{"profile": {"name": name} if name else {}, "wa_id": wa_id}],
                    "messages": [{
                        "from": wa_id,
                        "id": message_id or f"wamid.TEST{int(time.time() * 1000)}",
                        "timestamp": str(int(time.time())),
                        "type": "text",
                        "text": {"body": text},
                    }],
                },
            }],
        }],
    }


# ---------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------

def split_text(text: str, limit: int = MAX_TEXT_LENGTH) -> list[str]:
    """Split long replies at paragraph or line breaks under WhatsApp's limit."""

    text = (text or "").strip()
    parts = []

    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()

    if text:
        parts.append(text)

    return parts


def send_template(to_wa_id: str, template_name: str, language: str = "en_US", components: list[dict] | None = None) -> list[dict]:
    """Send one approved WhatsApp template. In dry run it goes to the outbox."""

    template = {"name": template_name, "language": {"code": language}}
    if components:
        template["components"] = components

    record = {
        "to": to_wa_id,
        "template": template_name,
        "template_language": language,
        "template_components": components or [],
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dry_run": DRY_RUN,
    }

    if DRY_RUN:
        OUTBOX.append(record)
        print(f"[WhatsApp dry run] -> {to_wa_id}: [template {template_name}]")
        return [record]

    try:
        response = requests.post(
            f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_wa_id,
                "type": "template",
                "template": template,
            },
            timeout=20,
        )
        record["status_code"] = response.status_code
        if not response.ok:
            record["error"] = response.text[:500]
            print(f"WhatsApp template send failed ({response.status_code}): {response.text[:500]}")
        else:
            try:
                record["provider_response"] = response.json()
            except ValueError:
                record["provider_response"] = None
    except Exception as exc:
        record["error"] = str(exc)
        print(f"WhatsApp template send failed: {exc}")

    return [record]


def send_text(to_wa_id: str, body: str) -> list[dict]:
    """Send a reply (split if long). In dry run it goes to the outbox."""

    sent = []

    for part in split_text(body):

        record = {
            "to": to_wa_id,
            "text": part,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dry_run": DRY_RUN,
        }

        if DRY_RUN:
            OUTBOX.append(record)
            print(f"[WhatsApp dry run] -> {to_wa_id}: {part[:120]!r}")
            sent.append(record)
            continue

        try:
            response = requests.post(
                f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
                json={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": to_wa_id,
                    "type": "text",
                    "text": {"preview_url": False, "body": part},
                },
                timeout=20,
            )
            record["status_code"] = response.status_code
            if not response.ok:
                record["error"] = response.text[:300]
                print(f"WhatsApp send failed ({response.status_code}): {response.text[:300]}")
        except Exception as e:
            record["error"] = str(e)
            print(f"WhatsApp send failed: {e}")

        sent.append(record)

    return sent


# ---------------------------------------------------------------------
# Handling a message
# ---------------------------------------------------------------------

def send_image(to_wa_id: str, url: str, caption: str = "") -> dict:
    """Send one picture. In dry run it goes to the outbox so the WhatsApp test
    tab can show it; live it is sent as an image message by link."""

    record = {
        "to": to_wa_id,
        "image": url,
        "text": caption,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dry_run": DRY_RUN,
    }

    if DRY_RUN:
        OUTBOX.append(record)
        print(f"[WhatsApp dry run] -> {to_wa_id}: [photo] {caption[:80]!r}")
        return record

    try:
        response = requests.post(
            f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_wa_id,
                "type": "image",
                "image": {"link": url, "caption": caption[:1024]},
            },
            timeout=20,
        )
        record["status_code"] = response.status_code
        if not response.ok:
            record["error"] = response.text[:300]
            print(f"WhatsApp photo failed ({response.status_code}): {response.text[:200]}")
    except Exception as e:
        record["error"] = str(e)
        print(f"WhatsApp photo failed: {e}")

    return record


def load_history(session_id: str) -> list[dict]:

    if not db.ENABLED:
        return list(_memory_history.get(session_id, []))[-HISTORY_MESSAGES:]

    try:
        conversation = db.get_conversation(session_id, None)
        if not conversation:
            return []
        messages = db.list_messages(conversation["id"])
        return [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ["user", "assistant"]
        ][-HISTORY_MESSAGES:]
    except Exception as e:
        print(f"Loading WhatsApp history failed: {e}")
        return []


def onboarding_reply_for(message: dict, phone: str):
    """Invited tenants/owners get step-by-step onboarding replies instead of
    property search. The exchange is saved to their WhatsApp conversation so
    the 24-hour customer service window can be checked later."""

    if not db.ENABLED:
        return None
    try:
        from src.services import onboarding_whatsapp
        reply = onboarding_whatsapp.handle_incoming(message, os.getenv("PUBLIC_BASE_URL") or "http://127.0.0.1:8000")
    except Exception as e:
        print(f"Onboarding WhatsApp handling failed: {e}")
        return None
    if reply:
        try:
            conversation = db.get_or_create_conversation(session_for(phone), None, f"WhatsApp · {message.get('name') or phone}", "tenant", {})
            db.add_message(conversation["id"], "user", message.get("text") or f"[{message.get('type')}]")
            db.add_message(conversation["id"], "assistant", reply)
        except Exception as e:
            print(f"Saving onboarding WhatsApp exchange failed: {e}")
    return reply


def _digits(phone) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def same_phone(a, b) -> bool:
    """Two phone numbers typed differently ("+1 919-555-0111" / "9195550111")."""
    da, db_ = _digits(normalize_phone(a) or a), _digits(normalize_phone(b) or b)
    if not da or not db_:
        return False
    return da == db_ or (min(len(da), len(db_)) >= 10 and (da.endswith(db_) or db_.endswith(da)))


def tenant_account_for_phone(phone: str) -> dict | None:
    """The tenant account this WhatsApp number belongs to, or None.

    Looked up from the phone the tenant saved (Portfolio / application
    checklist: account_portfolios.details.phone) and the phone on their
    rental applications. Never raises - a failed lookup just means the
    message is handled as an anonymous WhatsApp customer, as before."""
    if not db.ENABLED:
        return None
    tail = _digits(phone)[-7:]
    if len(tail) < 7:
        return None
    pattern = "*" + "*".join(tail[i:i + 3] for i in range(0, 7, 3)) + "*"  # matches "555-0111", "5550111", ...
    candidates: list[str] = []
    try:
        for row in db._get("account_portfolios", {"details->>phone": f"ilike.{pattern}",
                                                  "select": "account_id,details", "limit": "20"}) or []:
            if same_phone(phone, (row.get("details") or {}).get("phone")):
                candidates.append(row["account_id"])
    except Exception as e:
        print(f"WhatsApp tenant lookup (portfolio) failed (non-fatal): {e}")
    if not candidates:
        try:
            for row in db._get("rental_applications", {"tenant_phone": f"ilike.{pattern}",
                                                       "select": "tenant_account_id,tenant_phone",
                                                       "order": "updated_at.desc", "limit": "20"}) or []:
                if same_phone(phone, row.get("tenant_phone")):
                    candidates.append(row["tenant_account_id"])
        except Exception as e:
            print(f"WhatsApp tenant lookup (applications) failed (non-fatal): {e}")
    for account_id in dict.fromkeys(candidates):
        try:
            rows = db._get("accounts", {"id": f"eq.{account_id}", "select": "*", "limit": "1"}) or []
        except Exception as e:
            print(f"WhatsApp tenant lookup (account) failed (non-fatal): {e}")
            continue
        if rows and rows[0].get("role") == "tenant":
            return rows[0]
    return None


def handle_incoming(message: dict, account: dict | None = None) -> dict:
    """Reply to one parsed customer message. Returns what happened.

    account: the tenant account this message is from, when already known
    (the logged-in tenant on the WhatsApp test tab). Otherwise it's looked
    up from the sender's phone (tenant_account_for_phone)."""

    if not message.get("id") or not message.get("from"):
        return {"handled": False, "reason": "missing id or sender"}

    if not first_time_seen(message["id"]):
        return {"handled": False, "reason": "duplicate", "id": message["id"]}

    wa_id = message["from"]                      # digits with country code, e.g. 919876543210
    phone = normalize_phone(wa_id)
    if not str(phone).startswith("+"):
        phone = "+" + "".join(ch for ch in str(wa_id) if ch.isdigit())
    session_id = session_for(phone)

    # A tenant messaging from their own phone: same conversation as their web
    # Chat, and maintenance goes to their rented home / Maintenance tab.
    if account is None:
        account = tenant_account_for_phone(phone)
    if account and account.get("role") != "tenant":
        account = None
    if account:
        session_id = account["session_id"]

    with lock_for(wa_id):

        from src.services import owner_leads
        opt_out_reply = owner_leads.handle_opt_out(phone, message.get("text") or "")
        if opt_out_reply:
            return {"handled": True, "session_id": session_id, "reply": opt_out_reply,
                    "sent": send_text(wa_id, opt_out_reply), "opted_out": True}

        onboarding_reply = onboarding_reply_for(message, phone)
        if onboarding_reply:
            return {"handled": True, "session_id": session_id, "reply": onboarding_reply,
                    "sent": send_text(wa_id, onboarding_reply), "onboarding": True}

        if not message.get("text"):
            return {"handled": True, "reply": UNSUPPORTED_REPLY, "sent": send_text(wa_id, UNSUPPORTED_REPLY)}

        history = load_history(session_id)
        title = f"WhatsApp · {message.get('name') or phone}"
        if account:
            title = account.get("name") or title

        try:
            result = chat.process_message(
                message=message["text"],
                conversation_history=history,
                property_context={},
                session_id=session_id,
                listing_id=None,
                listing_title=title,
                persona="tenant",
                customer_phone=phone,
                customer_name=(account or {}).get("name") or message.get("name"),
                channel="whatsapp",
                account_role="tenant" if account else None,
                account_id=account["id"] if account else None,
            )
            reply = result.get("response") or result.get("next_question") or FALLBACK_REPLY
        except chat.AIUnavailable as e:
            print(f"WhatsApp AI error for {phone}: {e}")
            result, reply = None, FALLBACK_REPLY

        if not db.ENABLED:
            _memory_history.setdefault(session_id, []).extend([
                {"role": "user", "content": message["text"]},
                {"role": "assistant", "content": reply},
            ])

        sent = send_text(wa_id, reply)

        # Investors: send each home as its own picture, then one closing line -
        # a few short messages, the way a person would send them.
        photos = (result or {}).get("investor_photos") or []
        for photo in photos[:3]:
            sent.append(send_image(wa_id, photo["url"], photo["caption"]))
        if photos and (result or {}).get("investor_followup"):
            sent.extend(send_text(wa_id, result["investor_followup"]))

    return {
        "handled": True,
        "session_id": session_id,
        "reply": reply,
        "sent": sent,
        "analysis": result,
        "linked_account": bool(account),
    }
