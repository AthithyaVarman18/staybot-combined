"""
Hot customer alert for the staff member.

When a customer's chat (web or WhatsApp) becomes Hot - or its lead score
goes over 90 - the team is told straight away, by email and/or WhatsApp,
so a serious buyer / renter / investor gets a call within the hour.
Whether or not they ever clicked "Enquire about this".

One alert per crossing: it fires when the score moves INTO Hot (or across
90), not on every message of an already-hot chat - so no table is needed to
remember what was sent.

.env (any or all; nothing set = the alert is only logged):
    STAFF_ALERT_EMAIL=staff@example.com,manager@example.com
    STAFF_ALERT_WHATSAPP=+19195550101
    WHATSAPP_TEMPLATE_HOT_LEAD=hot_lead_alert      # approved template (needed
    WHATSAPP_TEMPLATE_HOT_LEAD_LANGUAGE=en_US      # outside WhatsApp's 24h window)

The customer never receives this - it shows our internal score.
Called from chat.py in a background thread, so the reply is never slowed.
"""

import html
import os
import threading
from typing import Optional

from src.services import email_sender

TRIGGERS = ("hot", "score_gt_90")


def _list(name: str) -> list[str]:
    return [x.strip() for x in (os.getenv(name) or "").split(",") if x.strip()]


def crossed(previous_score, previous_status, score, status) -> Optional[str]:
    """Which trigger this scoring update crossed, if any (Hot wins over >90)."""
    prev_status = str(previous_status or "").strip().lower()
    status = str(status or "").strip().lower()
    if status == "hot" and prev_status != "hot":
        return "hot"
    try:
        prev = float(previous_score) if previous_score is not None else None
    except (TypeError, ValueError):
        prev = None
    if float(score or 0) > 90 and (prev is None or prev <= 90) and prev_status != "hot":
        return "score_gt_90"
    return None


def _wants(result: dict) -> list[str]:
    """What they want, from the requirements / investor brief the AI pulled out."""
    from src.services import inquiry_profile   # shared wording ("2 bed · Garner · $1,800/month")
    bits = inquiry_profile.wants_text(result.get("requirements") or {})
    brief = result.get("investor_profile") or {}
    if isinstance(brief, dict):
        if brief.get("cash_available"):
            bits.append(f"${float(brief['cash_available']):,.0f} cash")
        areas = brief.get("areas")
        if areas:
            bits.append(", ".join(areas) if isinstance(areas, list) else str(areas))
        if brief.get("timeline"):
            bits.append(f"buying {str(brief['timeline']).replace('_', ' ')}")
    return bits


def build_message(contact: dict, result: dict, score, status: str, reasons: list[str], trigger: str) -> dict:
    name = contact.get("name") or "Name not given yet"
    phone = contact.get("phone") or "no phone yet"
    role = {"tenant": "renter / buyer", "investor": "investor", "owner": "owner"}.get(str(result.get("role") or ""), "customer")
    wants = " · ".join(_wants(result)) or "-"
    why = " · ".join(reasons) or "-"
    summary = result.get("summary") or "-"
    link = (os.getenv("PUBLIC_BASE_URL") or "http://127.0.0.1:8000").rstrip("/") + "/ui#customers"
    head = "🔥 Hot customer" if trigger == "hot" else "🔥 High-score customer (over 90)"
    text = (f"{head}\n"
            f"{name} ({role}) · {phone}\n"
            f"Score: {score} ({status})\n"
            f"Wants: {wants}\n"
            f"Why: {why}\n"
            f"Summary: {summary}\n"
            f"Call them within the hour. Open in Staybot: {link}")
    esc = lambda v: html.escape(str(v))
    body = ('<div style="font-family:Arial,sans-serif;line-height:1.5">'
            f"<h2 style='margin:0 0 8px'>{esc(head)}</h2>"
            f"<p><b>{esc(name)}</b> ({esc(role)}) · {esc(phone)}<br>"
            f"<b>Score:</b> {esc(score)} ({esc(status)})<br>"
            f"<b>Wants:</b> {esc(wants)}<br><b>Why:</b> {esc(why)}</p>"
            f"<p><b>AI summary:</b> {esc(summary)}</p>"
            f"<p>Call them within the hour. <a href=\"{esc(link)}\">Open in Staybot</a></p></div>")
    return {"subject": f"{head}: {name}", "text": text, "html": body,
            "template_params": [name, phone, wants[:120], str(score)]}


def notify(session_id: str, conversation_id: Optional[str], result: dict, lead: dict,
           previous_score, previous_status) -> Optional[dict]:
    """Send the alert if this update crossed into Hot / over 90. Never raises.
    Returns what was sent (for tests and logs), or None."""
    try:
        score = (lead or {}).get("intent_score", 0)
        status = (lead or {}).get("lead_status", "")
        trigger = crossed(previous_score, previous_status, score, status)
        if not trigger:
            return None

        contact = {}
        try:
            from src.services import customers   # name / phone from wherever they gave them
            conv = [{"id": conversation_id, "session_id": session_id}] if conversation_id else []
            contact = customers.contacts([session_id], conv).get(session_id, {}) if session_id else {}
        except Exception as e:
            print(f"Hot alert: contact lookup failed (non-fatal): {e}")
        if not contact.get("phone") and str(session_id or "").startswith("wa:"):
            contact["phone"] = session_id[3:]

        from src.services import inquiry_profile
        reasons = inquiry_profile.score_reasons({"lead_components": lead})
        msg = build_message(contact, result, score, status, reasons, trigger)
        sent = {"trigger": trigger, "email": [], "whatsapp": []}

        for addr in _list("STAFF_ALERT_EMAIL"):
            try:
                out = email_sender.send(addr, msg["subject"], msg["text"], msg["html"])
                sent["email"].append({"to": addr, "ok": bool(out.get("ok")), "test_mode": out.get("test_mode")})
            except Exception as e:
                sent["email"].append({"to": addr, "ok": False, "error": str(e)[:200]})

        numbers = _list("STAFF_ALERT_WHATSAPP")
        if numbers:
            from src.services import whatsapp
            template = (os.getenv("WHATSAPP_TEMPLATE_HOT_LEAD") or "").strip()
            lang = (os.getenv("WHATSAPP_TEMPLATE_HOT_LEAD_LANGUAGE") or "en_US").strip()
            for number in numbers:
                to = "".join(ch for ch in number if ch.isdigit())
                try:
                    if template:   # works any time (outside WhatsApp's 24-hour window too)
                        whatsapp.send_template(to, template, lang, [{"type": "body", "parameters": [
                            {"type": "text", "text": p} for p in msg["template_params"]]}])
                    else:          # plain text: fine in test mode / within 24h of the staff member's last message
                        whatsapp.send_text(to, msg["text"])
                    sent["whatsapp"].append({"to": to, "ok": True})
                except Exception as e:
                    sent["whatsapp"].append({"to": to, "ok": False, "error": str(e)[:200]})

        if not sent["email"] and not sent["whatsapp"]:
            print("Hot alert: nobody to tell - set STAFF_ALERT_EMAIL and/or STAFF_ALERT_WHATSAPP in .env.\n" + msg["text"])
        return {**sent, "message": msg["text"]}
    except Exception as e:
        print(f"Hot alert failed (non-fatal): {e}")
        return None


def notify_in_background(*args, **kwargs):
    threading.Thread(target=notify, args=args, kwargs=kwargs, daemon=True).start()
