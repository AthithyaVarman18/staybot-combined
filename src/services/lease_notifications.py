"""In-app reminders for customer accounts (tenant / new & existing investor).

  lease_expiry       tenant + the owner who listed the home. From the lease
                     start: once every 30 days; from 15 days before the end:
                     daily through the due date; then a "lease date exceeded"
                     notice.
  maintenance_issue  tenant + owner, one per open ticket per day it's open.
  offer_expiry /     the investor on a purchase offer: 3 days, 2 days,
  offer_closing      tomorrow, then 12 / 6 / 3 / 1 hours, with the exact time.

Lease dates come from the customer rental workflow (move_in_date +
lease_months) or, when present, an explicit lease_end value. Every reminder
has a dedupe_key, so polling the dashboard never creates duplicates.

Served at /me/account-notifications (the "Notifications" tab in /ui). The
separate /me/notifications routes belong to owner_notifications.py - the
"someone asked about your house" pop-ups for owners. Table:
supabase_lease_notifications.sql.
"""

from calendar import monthrange
from datetime import date, datetime, time, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.services import accounts, db
from src.services.maintenance import ISSUE_LABELS
from src.services.real_estate_ai import APP_TIMEZONE

router = APIRouter(prefix="/me/account-notifications", tags=["Account notifications"])

# From this many days before the lease end, the reminder becomes daily.
LEASE_DAILY_FROM_DAYS = 15

CUSTOMER_ROLES = ("tenant", "new_investor", "existing_investor")


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_lease_notifications.sql to enable notifications.")


def require_customer(request: Request) -> dict:
    account = accounts.current_account(request)
    if not account:
        raise HTTPException(401, "Please log in.")
    if account.get("role") not in CUSTOMER_ROLES:
        raise HTTPException(403, "Notifications are only available to customer accounts.")
    configured()
    return account


def add_months(start: date, months: int) -> date:
    """Calendar-safe month addition (e.g. Jan 31 + 1 month -> Feb 28/29)."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, monthrange(year, month)[1])
    return date(year, month, day)


def parse_date(value) -> Optional[date]:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def lease_end_from_application(app: dict) -> Optional[date]:
    details = app.get("details") if isinstance(app.get("details"), dict) else {}
    explicit = parse_date(details.get("lease_end"))
    if explicit:
        return explicit
    start = parse_date(details.get("move_in_date") or app.get("move_in_date"))
    months = details.get("lease_months")
    try:
        months = int(months)
    except (TypeError, ValueError):
        return None
    if not start or months < 1 or months > 60:
        return None
    # A 12-month lease starting on Oct 1 runs through Sep 30 of the next year.
    return add_months(start, months) - timedelta(days=1)


def lease_start_from_application(app: dict) -> Optional[date]:
    details = app.get("details") if isinstance(app.get("details"), dict) else {}
    return parse_date(details.get("lease_start") or details.get("move_in_date") or app.get("move_in_date"))


def expiry_bucket(days_left: int, days_since_start: Optional[int] = None) -> Optional[tuple[str, str]]:
    """Return lease reminders using the requested schedule.

    - From the lease/buying start date, remind once every 30 days.
    - Once 15 days remain, switch to a daily reminder through the due date.
    - After the due date, send an exceeded notification.
    """
    if days_left < 0:
        return "expired", "Lease date exceeded"
    if days_left <= LEASE_DAILY_FROM_DAYS:
        if days_left == 0:
            return "day_0", "Lease expires today"
        return f"day_{days_left}", f"{days_left} day{'s' if days_left != 1 else ''} left"
    if days_since_start is not None:
        if days_since_start == 0:
            return "start_day", "Lease starts today"
        if days_since_start > 0 and days_since_start % 30 == 0:
            return f"every_30_days_{days_since_start}", f"{days_since_start} days since the lease started"
    return None


def _property_title(app: dict) -> str:
    title = (app.get("property_title") or "").strip()
    if title:
        return title
    try:
        rows = db._get("properties", {"id": f"eq.{app['property_id']}", "select": "title", "limit": "1"}) or []
        return (rows[0].get("title") or "your rented property") if rows else "your rented property"
    except Exception:
        return "your rented property"


def _lease_message(property_title: str, lease_end: date, bucket: str, today: date) -> str:
    end = lease_end.strftime('%B %d, %Y')
    if bucket == "expired":
        return f"The lease for {property_title} ended on {end}. The lease date has been exceeded."
    if bucket == "day_0":
        return f"The lease for {property_title} expires today ({end})."
    if bucket == "start_day":
        return f"The lease for {property_title} starts today ({today.strftime('%B %d, %Y')}). It runs until {end}."
    if bucket.startswith("every_30_days_"):
        days_since_start = int(bucket.rsplit("_", 1)[-1])
        return (f"The lease for {property_title} has been active for {days_since_start} days. "
                f"Lease expiry is {end}.")
    days = (lease_end - today).days
    return f"The lease for {property_title} expires on {end} ({days} day{'s' if days != 1 else ''} remaining)."


def _create_lease_notification(account_id: str, app: dict, lease_end: date, bucket: str, label: str,
                               role_label: str, today: date) -> bool:
    key = f"lease-expiry:{app['id']}:{bucket}:{account_id}"
    return _create_account_notification(
        account_id, "lease_expiry", f"Lease expiry reminder: {label}",
        _lease_message(_property_title(app), lease_end, bucket, today),
        app.get("property_id"), app.get("id"), lease_end.isoformat(), key, role_label,
    )


def sync_account_lease_notifications(account: dict) -> int:
    """Create the reminder that is due today for each of the account's leases."""
    role = account.get("role")
    if role == "tenant":
        params = {"tenant_account_id": f"eq.{account['id']}"}
    elif role in ("new_investor", "existing_investor") and account.get("session_id"):
        params = {"owner_session_id": f"eq.{account['session_id']}"}
    else:
        return 0
    apps = db._get("rental_applications", {
        **params, "status": "eq.approved", "select": "*", "limit": "200",
    }) or []

    created = 0
    today = datetime.now(APP_TIMEZONE).date()
    role_label = "tenant" if role == "tenant" else "owner"
    for app in apps:
        details = app.get("details") if isinstance(app.get("details"), dict) else {}
        if details.get("kind") == "purchase":
            continue
        lease_end = lease_end_from_application(app)
        lease_start = lease_start_from_application(app)
        if not lease_end or not lease_start:
            continue
        bucket_info = expiry_bucket((lease_end - today).days, (today - lease_start).days)
        if not bucket_info:
            continue
        bucket, label = bucket_info
        if _create_lease_notification(account["id"], app, lease_end, bucket, label, role_label, today):
            created += 1
    return created


def _ticket_created_date(ticket: dict) -> date:
    value = ticket.get("created_at")
    if isinstance(value, datetime):
        return value.astimezone(APP_TIMEZONE).date() if value.tzinfo else value.date()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo:
            return parsed.astimezone(APP_TIMEZONE).date()
        return parsed.date()
    except (TypeError, ValueError):
        return datetime.now(APP_TIMEZONE).date()


def _create_account_notification(account_id: str, notification_type: str, title: str,
                                  message: str, property_id: str | None,
                                  reference_id: str | None, reference_date: str | None,
                                  dedupe_key: str, recipient_role: str) -> bool:
    existing = db._get("account_notifications", {
        "dedupe_key": f"eq.{dedupe_key}",
        "account_id": f"eq.{account_id}",
        "select": "id",
        "limit": "1",
    }) or []
    if existing:
        return False
    reference_at = reference_date if reference_date and "T" in str(reference_date) else None
    reference_day = str(reference_date)[:10] if reference_date else None
    db._post("account_notifications", {
        "account_id": account_id,
        "notification_type": notification_type,
        "title": title,
        "message": message,
        "property_id": property_id,
        "reference_id": reference_id,
        "reference_date": reference_day,
        "reference_at": reference_at,
        "dedupe_key": dedupe_key,
        "recipient_role": recipient_role,
    })
    return True


ACTIVE_TICKET_STATUSES = "in.(needs_review,open,in_progress)"


def _account_tickets(account: dict) -> list[dict]:
    """Open maintenance tickets this account should hear about: a tenant's own
    tickets, or - for an owner - tickets from tenants they approved for their
    homes (the same rule as the owner's Maintenance list in rentals.py)."""
    role = account.get("role")
    if role == "tenant":
        if not account.get("session_id"):
            return []
        return db._get("maintenance_tickets", {
            "session_id": f"eq.{account['session_id']}", "ticket_status": ACTIVE_TICKET_STATUSES,
            "order": "created_at.desc", "limit": "50", "select": "*",
        }) or []
    if role in ("new_investor", "existing_investor") and account.get("session_id"):
        from src.services import rentals  # local import - rentals imports a lot
        pairs = rentals.owner_tenant_pairs(account)
        if not pairs:
            return []
        homes = sorted({home for _, home in pairs if home})
        rows = db._get("maintenance_tickets", {
            "property_id": f"in.({','.join(homes)})", "ticket_status": ACTIVE_TICKET_STATUSES,
            "order": "created_at.desc", "limit": "100", "select": "*",
        }) or []
        return [t for t in rows if (t.get("session_id"), t.get("property_id")) in pairs]
    return []


def sync_maintenance_notifications(account: dict) -> int:
    """One reminder per open ticket per day it stays open, for this account."""
    role_label = "tenant" if account.get("role") == "tenant" else "owner"
    today = datetime.now(APP_TIMEZONE).date()
    created = 0
    for ticket in _account_tickets(account):
        if not ticket.get("id"):
            continue
        age_days = max(0, (today - _ticket_created_date(ticket)).days)
        property_title = (ticket.get("property_title") or "your property").strip() or "your property"
        issue = ISSUE_LABELS.get(ticket.get("issue_type"), "maintenance")
        timing = "reported today" if age_days == 0 else f"open for {age_days} day{'s' if age_days != 1 else ''}"
        message = f"Maintenance issue ({issue}) for {property_title} was {timing}."
        key = f"maintenance:{ticket['id']}:day:{age_days}:{account['id']}"
        if _create_account_notification(
            account["id"], "maintenance_issue", "Maintenance issue reported",
            message, ticket.get("property_id"), ticket.get("id"),
            str(ticket.get("created_at") or "") or None, key, role_label,
        ):
            created += 1
    return created


def _offer_time_bucket(seconds_left: float):
    """Offer schedule: 3 days, 2 days, tomorrow, then hour reminders."""
    if seconds_left <= 0:
        return "expired", "Offer has expired"
    day = 86400
    if seconds_left <= 3 * day and seconds_left > 2 * day:
        return "d_3", "3 days left"
    if seconds_left <= 2 * day and seconds_left > 1 * day:
        return "d_2", "2 days left"
    if seconds_left <= 1 * day:
        # Keep the calendar-day/tomorrow message until the final 24-hour
        # window, then switch to the requested hour countdown.
        if seconds_left > 12 * 3600:
            return "d_1", "tomorrow"
        for threshold in (1, 3, 6, 12):
            if seconds_left <= threshold * 3600:
                return f"h_{threshold}", f"{threshold} hour{'s' if threshold != 1 else ''} left"
    return None


def _parse_expiry(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=APP_TIMEZONE)
    return parsed.astimezone(APP_TIMEZONE)


def _parse_closing(closing, closing_time) -> Optional[datetime]:
    if not (closing and closing_time):
        return None
    try:
        closing_date = date.fromisoformat(str(closing)[:10])
        hh, mm = [int(part) for part in str(closing_time)[:5].split(":")]
        return datetime.combine(closing_date, time(hh, mm), tzinfo=APP_TIMEZONE)
    except (TypeError, ValueError):
        return None


def sync_offer_notifications(account: dict) -> int:
    """Expiry / closing countdown for the purchase offers of this investor."""
    investor_id = account.get("investor_id")
    if account.get("role") not in ("new_investor", "existing_investor") or not investor_id:
        return 0
    offers = db._get("acquisition_offers", {
        "investor_id": f"eq.{investor_id}",
        "status": "not.in.(draft,accepted,rejected,withdrawn,expired)",
        "order": "updated_at.desc",
        "limit": "50",
        "select": "*",
    }) or []
    now = datetime.now(APP_TIMEZONE)
    created = 0
    for offer in offers:
        versions = db._get("acquisition_offer_versions", {
            "offer_id": f"eq.{offer['id']}",
            "version_number": f"eq.{offer.get('current_version') or 1}",
            "select": "terms",
            "limit": "1",
        }) or []
        terms = (versions[0].get("terms") or {}) if versions else {}
        property_title = offer.get("property_title") or "the offer property"

        expiry = _parse_expiry(terms.get("expires_at"))
        bucket_info = _offer_time_bucket((expiry - now).total_seconds()) if expiry else None
        if bucket_info:
            bucket, label = bucket_info
            when = expiry.strftime('%B %d, %Y %I:%M %p %Z')
            if bucket == "expired":
                title = f"Offer expired: {property_title}"
                message = f"The offer for {property_title} expired at {when}."
            else:
                title = f"Offer expiring soon: {property_title}"
                message = f"The offer for {property_title} expires at {when}. {label.capitalize()}."
            key = f"offer:{offer['id']}:expiry:{bucket}:{account['id']}"
            if _create_account_notification(account["id"], "offer_expiry", title, message,
                                            offer.get("property_id"), offer.get("id"),
                                            expiry.isoformat(), key, "investor"):
                created += 1

        closing_at = _parse_closing(terms.get("closing_date"), terms.get("closing_time"))
        bucket_info = _offer_time_bucket((closing_at - now).total_seconds()) if closing_at else None
        if bucket_info and bucket_info[0] != "expired":
            bucket, label = bucket_info
            message = (f"The offer for {property_title} closes on {closing_at.strftime('%B %d, %Y at %I:%M %p %Z')}. "
                       f"{label.capitalize()}.")
            key = f"offer:{offer['id']}:closing:{bucket}:{account['id']}"
            if _create_account_notification(account["id"], "offer_closing", f"Offer closing soon: {property_title}",
                                            message, offer.get("property_id"), offer.get("id"),
                                            closing_at.isoformat(), key, "investor"):
                created += 1
    return created


def _db_error_text(error: Exception) -> str:
    return getattr(getattr(error, "response", None), "text", "") or str(error)


def _missing_table(text: str) -> bool:
    return "account_notifications" in text or "PGRST205" in text or "42P01" in text


SETUP_MESSAGE = "Run supabase_lease_notifications.sql in Supabase to enable in-app notifications."


@router.get("")
def list_notifications(request: Request, unread_only: bool = False):
    account = require_customer(request)
    try:
        sync_account_lease_notifications(account)
    except Exception as e:
        text = _db_error_text(e)
        if _missing_table(text):
            raise HTTPException(503, SETUP_MESSAGE)
        print(f"Lease notifications sync failed (non-fatal): {text[:300]}")
    # Maintenance and offer reminders are best-effort: a missing optional
    # table (maintenance / acquisition workflows not set up yet) must never
    # hide the account's other notifications.
    for sync in (sync_maintenance_notifications, sync_offer_notifications):
        try:
            sync(account)
        except Exception as e:
            print(f"{sync.__name__} failed (non-fatal): {_db_error_text(e)[:300]}")
    try:
        params = {
            "account_id": f"eq.{account['id']}",
            "order": "created_at.desc",
            "limit": "50",
            "select": "*",
        }
        if unread_only:
            params["read_at"] = "is.null"
        rows = db._get("account_notifications", params) or []
    except Exception as e:
        text = _db_error_text(e)
        if _missing_table(text):
            raise HTTPException(503, SETUP_MESSAGE)
        print(f"Account notifications database error: {text[:300]}")
        raise HTTPException(500, "The notifications database request failed. Please try again.")
    return {"notifications": rows, "unread": sum(1 for row in rows if not row.get("read_at"))}


@router.post("/read-all")
def mark_all_read(request: Request):
    account = require_customer(request)
    try:
        rows = db._patch("account_notifications", {"read_at": accounts.now_iso()}, {
            "account_id": f"eq.{account['id']}",
            "read_at": "is.null",
        })
    except Exception as e:
        if _missing_table(_db_error_text(e)):
            raise HTTPException(503, SETUP_MESSAGE)
        raise HTTPException(500, "The notifications could not be updated.")
    return {"marked_read": len(rows or [])}


@router.post("/{notification_id}/read")
def mark_read(notification_id: str, request: Request):
    account = require_customer(request)
    try:
        rows = db._patch("account_notifications", {"read_at": accounts.now_iso()}, {
            "id": f"eq.{notification_id}",
            "account_id": f"eq.{account['id']}",
            "read_at": "is.null",
        })
    except Exception as e:
        if _missing_table(_db_error_text(e)):
            raise HTTPException(503, SETUP_MESSAGE)
        raise HTTPException(500, "The notification could not be updated.")
    if not rows:
        # Already read (or not this account's) - nothing left to do.
        existing = db._get("account_notifications", {
            "id": f"eq.{notification_id}", "account_id": f"eq.{account['id']}",
            "select": "*", "limit": "1",
        }) or []
        if not existing:
            raise HTTPException(404, "Notification not found.")
        return existing[0]
    return rows[0] if isinstance(rows, list) else rows
