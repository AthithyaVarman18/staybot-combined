"""
Automated high-priority enquiry email notifications.

The existing lead_scoring.score_lead() remains the only scoring system.
This service only observes the already-calculated lead score/status and
notifies users that are actually associated with the same enquiry/property.
"""
from __future__ import annotations

import html
import os
from typing import Iterable

from src.services import db, email_sender


TRIGGERS = ("hot", "score_gt_90")


def _account_rows(params: dict) -> list[dict]:
    return db._get("accounts", {**params, "select": "id,name,email,role,session_id,investor_id", "limit": "50"}) or []


def _add_account(recipients: dict, account: dict, role_label: str) -> None:
    account_id = account.get("id")
    if not account_id:
        return
    if account.get("role") not in ("tenant", "new_investor", "existing_investor"):
        return
    recipients[str(account_id)] = (account, role_label)


def _recipients_for_inquiry(inquiry: dict) -> list[tuple[dict, str]]:
    """
    Resolve recipients only through stored relationships.

    Tenant:
      property_inquiries.session_id -> accounts.session_id

    Property owner:
      properties.session_id -> accounts.session_id

    Investor portfolio association:
      investor_portfolio_properties.property_id -> investors.id
      -> accounts.investor_id

    Rental application association is also checked because some existing
    property ownership relationships live there rather than on properties.
    No name/phone/email matching is used.
    """
    recipients: dict[str, tuple[dict, str]] = {}

    # The customer who enquired is NOT emailed: this alert shows our internal
    # lead score. The staff member is told separately (staff_alerts.py).
    session_id = str(inquiry.get("session_id") or "").strip()

    property_id = str(inquiry.get("property_id") or "").strip()
    if not property_id:
        return list(recipients.values())

    # Direct property owner/listing account.
    try:
        properties = db._get("properties", {
            "id": f"eq.{property_id}",
            "select": "id,session_id",
            "limit": "1",
        }) or []
        owner_session = str((properties[0] if properties else {}).get("session_id") or "").strip()
        if owner_session:
            for account in _account_rows({"session_id": f"eq.{owner_session}"}):
                if account.get("role") in ("existing_investor", "new_investor"):
                    _add_account(recipients, account, "investor")
    except Exception as error:
        print(f"High-priority inquiry property owner lookup failed (non-fatal): {error}")

    # Investor portfolio -> property association.
    try:
        portfolio_rows = db._get("investor_portfolio_properties", {
            "property_id": f"eq.{property_id}",
            "select": "investor_id",
            "limit": "50",
        }) or []
        investor_ids = {str(r.get("investor_id")) for r in portfolio_rows if r.get("investor_id")}
        for investor_id in investor_ids:
            for account in _account_rows({"investor_id": f"eq.{investor_id}"}):
                if account.get("role") in ("existing_investor", "new_investor"):
                    _add_account(recipients, account, "investor")
    except Exception as error:
        # This table is optional for older installs. Direct property ownership
        # and tenant association remain usable if the table is not installed.
        print(f"High-priority inquiry investor association lookup failed (non-fatal): {error}")

    # Approved rental applications provide a second stored property/owner link.
    if session_id:
        try:
            applications = db._get("rental_applications", {
                "tenant_session_id": f"eq.{session_id}",
                "property_id": f"eq.{property_id}",
                "status": "in.(approved,ended)",
                "select": "owner_session_id,tenant_account_id",
                "limit": "50",
            }) or []
            for app in applications:
                owner_session = str(app.get("owner_session_id") or "").strip()
                if owner_session:
                    for account in _account_rows({"session_id": f"eq.{owner_session}"}):
                        if account.get("role") in ("existing_investor", "new_investor"):
                            _add_account(recipients, account, "investor")
        except Exception as error:
            print(f"High-priority inquiry rental association lookup failed (non-fatal): {error}")

    return list(recipients.values())


def _trigger_crossed(previous_score, previous_status, current_score, current_status) -> list[str]:
    """
    Return only thresholds crossed by this scoring update.

    The >90 rule is deliberately strict: 90 does not trigger.
    """
    previous_score_num = None
    try:
        if previous_score is not None:
            previous_score_num = float(previous_score)
    except (TypeError, ValueError):
        pass

    current_score_num = float(current_score or 0)
    previous_status_norm = str(previous_status or "").strip().lower()
    current_status_norm = str(current_status or "").strip().lower()

    triggers = []
    if current_status_norm == "hot" and previous_status_norm != "hot":
        # The two conditions are OR conditions. If the same update makes both
        # true, one email is enough; Hot is the more specific trigger.
        return ["hot"]
    if current_score_num > 90 and (previous_score_num is None or previous_score_num <= 90):
        triggers.append("score_gt_90")
    return triggers


def _claim(inquiry: dict, account: dict, recipient_role: str, trigger: str,
           score: int, lead_status: str) -> dict | None:
    """Create the durable idempotency row before sending the email."""
    params = {
        "inquiry_id": f"eq.{inquiry['id']}",
        "recipient_account_id": f"eq.{account['id']}",
        "trigger_type": f"eq.{trigger}",
        "select": "id",
        "limit": "1",
    }
    try:
        if db._get("high_priority_inquiry_email_notifications", params):
            return None
    except Exception as error:
        # Do not let notification bookkeeping break lead scoring.
        print(f"High-priority email idempotency lookup failed (non-fatal): {error}")
        return None

    row = {
        "inquiry_id": inquiry["id"],
        "property_id": inquiry.get("property_id"),
        "recipient_account_id": account["id"],
        "recipient_role": recipient_role,
        "recipient_email": account.get("email"),
        "trigger_type": trigger,
        "lead_score": int(score),
        "lead_status": lead_status,
        "delivery_status": "pending",
    }
    try:
        return db._post("high_priority_inquiry_email_notifications", row)
    except Exception as error:
        # The unique constraint is the final race-safe duplicate guard.
        response_text = getattr(getattr(error, "response", None), "text", "") or str(error)
        if "duplicate" in response_text.lower() or "23505" in response_text:
            return None
        print(f"High-priority email claim failed (non-fatal): {error}")
        return None


def _property_link() -> str:
    base = (os.getenv("PUBLIC_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
    return f"{base}/ui#inquiries"


def _email_content(inquiry: dict, score: int, lead_status: str, trigger: str) -> tuple[str, str, str]:
    title = inquiry.get("property_title") or inquiry.get("property_id") or "Property"
    area = inquiry.get("area") or "Location not provided"
    price = inquiry.get("price_label") or "Price not provided"
    status_label = lead_status.title() if lead_status else "Hot"
    trigger_text = "The enquiry has entered the Hot lead status." if trigger == "hot" else \
        "The enquiry score has crossed 90% and is now high priority."
    link = _property_link()

    subject = f"Staybot high-priority enquiry — {title}"
    text = (
        "Staybot\n\n"
        f"High-priority enquiry for: {title}\n"
        f"Location: {area}\n"
        f"Price: {price}\n"
        f"Current enquiry score: {score}%\n"
        f"Current status: {status_label}\n\n"
        f"{trigger_text}\n"
        "Please review this enquiry in Staybot.\n\n"
        f"Open Staybot: {link}\n"
    )
    safe_title = html.escape(str(title))
    safe_area = html.escape(str(area))
    safe_price = html.escape(str(price))
    safe_status = html.escape(str(status_label))
    safe_link = html.escape(link, quote=True)
    body = (
        '<div style="font-family:Arial,sans-serif;line-height:1.5">'
        '<h2>Staybot</h2>'
        '<p><strong>High-priority enquiry</strong> has reached the configured threshold.</p>'
        f"<p><strong>Property:</strong> {safe_title}<br>"
        f"<strong>Location:</strong> {safe_area}<br>"
        f"<strong>Price:</strong> {safe_price}<br>"
        f"<strong>Enquiry score:</strong> {score}%<br>"
        f"<strong>Status:</strong> {safe_status}</p>"
        f"<p>{html.escape(trigger_text)}</p>"
        f'<p><a href="{safe_link}">Open Staybot</a></p>'
        "</div>"
    )
    return subject, text, body


def notify_for_update(inquiry: dict, previous_score, previous_status, current_score, current_status) -> int:
    """Send one durable notification per crossed trigger and recipient."""
    if not db.ENABLED or not inquiry or not inquiry.get("id"):
        return 0

    try:
        score = int(round(float(current_score or 0)))
    except (TypeError, ValueError):
        score = 0
    status = str(current_status or "").strip().lower()
    triggers = _trigger_crossed(previous_score, previous_status, score, status)
    if not triggers:
        return 0

    try:
        recipients = _recipients_for_inquiry(inquiry)
    except Exception as error:
        print(f"High-priority inquiry recipient lookup failed (non-fatal): {error}")
        return 0

    created = 0
    for trigger in triggers:
        for account, role in recipients:
            row = _claim(inquiry, account, role, trigger, score, status)
            if not row:
                continue
            try:
                recipient_email = (account.get("email") or "").strip()
                if not recipient_email:
                    result = {"ok": False, "error": "No registered email address is available for this account."}
                else:
                    subject, text, html_body = _email_content(inquiry, score, status, trigger)
                    result = email_sender.send(recipient_email, subject, text, html_body)
                update = {
                    "delivery_status": "sent" if result.get("ok") else "failed",
                    "sent_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                    "error": result.get("error"),
                }
                db._patch("high_priority_inquiry_email_notifications", update, {"id": f"eq.{row['id']}"})
                created += 1
            except Exception as error:
                try:
                    db._patch(
                        "high_priority_inquiry_email_notifications",
                        {
                            "delivery_status": "failed",
                            "sent_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                            "error": str(error)[:500],
                        },
                        {"id": f"eq.{row['id']}"},
                    )
                except Exception as patch_error:
                    print(f"High-priority inquiry email failure-record update failed (non-fatal): {patch_error}")
                print(f"High-priority inquiry email failed (non-fatal): {error}")
    return created


def notify_from_saved_inquiry(inquiry: dict) -> int:
    """Evaluate an enquiry snapshot at creation time.

    There is no prior scoring update available on the inquiry row, so a
    snapshot already above 90 or Hot is treated as the moment the enquiry
    reached the threshold.
    """
    if not inquiry:
        return 0
    score = inquiry.get("lead_score")
    status = inquiry.get("lead_status")
    try:
        score_num = float(score) if score is not None else 0
    except (TypeError, ValueError):
        score_num = 0
    if str(status or "").lower() == "hot" or score_num > 90:
        return notify_for_update(inquiry, None, None, score_num, status)
    return 0
