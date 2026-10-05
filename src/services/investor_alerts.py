"""
Investor portfolio alerts: three kinds only, on purpose (rent alerts come
later, once rent collection exists - see the module docstring warning below).

  lease_60        a portfolio property's lease_end_date is 31-60 days away
  lease_30        a portfolio property's lease_end_date is 0-30 days away
  maintenance_7   a maintenance ticket linked to an investor has been open
                   (needs_review/open/in_progress) for more than 7 days

refresh_alerts() recomputes these from investor_portfolio_properties and
maintenance_tickets and keeps investor_alerts in sync: new conditions open
a new alert, conditions that stop being true resolve the alert automatically
(a lease_60 alert resolves itself once the same property crosses into the
lease_30 window and a fresh lease_30 alert opens), and a status a staff
member has hand-set to "dismissed" is left alone rather than reopened.

Maintenance tickets aren't linked to an investor anywhere else in this
codebase - they're filed by a tenant against a marketplace `properties`
listing, while an investor's holdings live in investor_portfolio_properties.
_backfill_ticket_investors() bridges the two only when a portfolio property
has a linked listing (its property_id column); with no such link, a ticket
is simply never attributed to an investor, and never alerted on - nothing is
guessed.

run_daily_digest() is the one entry point an external daily cron should
call (POST /alerts/run-digest, staff-only like every other route here - see
main.py's TeamAuthMiddleware). It:
  1. Refuses to send anything outside local daytime hours (APP_TIMEZONE) -
     "do not send at night" - unless force=True (the dashboard's "send test
     digest now" button, for testing without waiting for the clock).
  2. Refreshes alerts, then groups every open alert by investor and sends
     ONE WhatsApp message per investor covering all of them, never one
     message per alert.
  3. Requires whatsapp_alerts_opt_in on the investor - false by default -
     and a phone number on file. No opt-in, no message, full stop.
  4. Inside WhatsApp's 24h customer-service window, sends the digest as a
     normal text (src/services/whatsapp.py); outside it, uses an approved
     template (WHATSAPP_TEMPLATE_INVESTOR_DIGEST) with a short parameter
     instead of the full text, same rule onboarding_whatsapp.py already
     follows for onboarding invites. No template configured and the window
     is closed -> skipped, not sent, and the reason is logged.
  5. Sends at most one digest per investor per local calendar day - the
     investor_alert_digests table's unique(investor_id, sent_date) is the
     real guarantee here, not just the time-of-day check.

.env:
  WHATSAPP_TEMPLATE_INVESTOR_DIGEST=investor_alert_digest   # body: {{1}} first name, {{2}} short summary
  ALERT_DIGEST_START_HOUR=8    # local hour (APP_TIMEZONE) sending may start
  ALERT_DIGEST_END_HOUR=20     # local hour sending must stop by
"""

import os
import re
from datetime import date, datetime

import requests
from fastapi import APIRouter, Header, HTTPException, Query
from uuid import UUID

from src.services import db, whatsapp
from src.services.real_estate_ai import APP_TIMEZONE

router = APIRouter(prefix="/alerts", tags=["Investor alerts"])

ALERT_TYPES = ("lease_60", "lease_30", "maintenance_7")
OPEN_TICKET_STATUSES = ("needs_review", "open", "in_progress")

ALERT_LABELS = {
    "lease_60": "Lease ending in 60 days",
    "lease_30": "Lease ending in 30 days",
    "maintenance_7": "Maintenance ticket open 7+ days",
}

TEMPLATE_DIGEST = (os.getenv("WHATSAPP_TEMPLATE_INVESTOR_DIGEST") or "").strip()
DIGEST_START_HOUR = int(os.getenv("ALERT_DIGEST_START_HOUR", "8"))
DIGEST_END_HOUR = int(os.getenv("ALERT_DIGEST_END_HOUR", "20"))


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_investor_alerts.sql to use investor alerts.")


def q(fn, *args, **kwargs):
    configured()
    try:
        return fn(*args, **kwargs)
    except requests.RequestException as error:
        text = getattr(getattr(error, "response", None), "text", "") or str(error)
        if "PGRST205" in text or "does not exist" in text:
            raise HTTPException(503, "Run supabase_investor_alerts.sql in Supabase to enable investor alerts.")
        print(f"Investor alerts database error: {text[:300]}")
        raise HTTPException(500, "The investor alerts database request failed. Please try again.")


def staff_actor(header_value: str | None) -> str:
    name = (header_value or "").strip()[:120]
    return f"staff:{name}" if name else "staff:unnamed"


# ---------------------------------------------------------------------
# Computing alerts
# ---------------------------------------------------------------------

def _upsert_alert(*, investor_id, alert_type, dedupe_key, title, detail, reference_date=None,
                   portfolio_property_id=None, maintenance_ticket_id=None):
    """Create the alert if it doesn't exist; refresh its facts if it does.
    A staff "dismissed" status is left alone - refreshed facts, not status -
    so re-dismissing every run is never needed. An alert coming back after
    being auto-"resolved" (the condition returned) reopens it."""

    fields = {
        "alert_type": alert_type, "title": title, "detail": detail, "reference_date": reference_date,
        "portfolio_property_id": portfolio_property_id, "maintenance_ticket_id": maintenance_ticket_id,
    }
    try:
        existing = db._get("investor_alerts", {"dedupe_key": f"eq.{dedupe_key}", "select": "id,status", "limit": "1"})
    except Exception as e:
        print(f"Looking up alert {dedupe_key} failed: {e}")
        return

    try:
        if existing:
            row = existing[0]
            if row["status"] != "dismissed":
                fields["status"] = "open"
            db._patch("investor_alerts", fields, {"id": f"eq.{row['id']}"})
        else:
            db._post("investor_alerts", {"investor_id": investor_id, "dedupe_key": dedupe_key, "status": "open", **fields})
    except Exception as e:
        print(f"Saving alert {dedupe_key} failed: {e}")


def _resolve_stale_alerts(active_keys):
    try:
        open_alerts = db._get("investor_alerts", {"status": "eq.open", "select": "id,dedupe_key", "limit": "5000"})
    except Exception as e:
        print(f"Loading open alerts to resolve failed: {e}")
        return 0
    resolved = 0
    for a in open_alerts:
        if a["dedupe_key"] in active_keys:
            continue
        try:
            db._patch("investor_alerts", {"status": "resolved"}, {"id": f"eq.{a['id']}"})
            resolved += 1
        except Exception as e:
            print(f"Resolving alert {a['id']} failed: {e}")
    return resolved


def _backfill_ticket_investors():
    """A ticket only becomes "this investor's" once its property_id matches
    a portfolio property's linked marketplace listing - see the module
    docstring. Returns how many tickets got linked."""

    try:
        unlinked = db._get("maintenance_tickets", {
            "investor_id": "is.null", "property_id": "not.is.null", "select": "id,property_id", "limit": "2000"})
    except Exception as e:
        print(f"Loading unlinked maintenance tickets failed: {e}")
        return 0
    if not unlinked:
        return 0

    property_ids = sorted({t["property_id"] for t in unlinked if t.get("property_id")})
    try:
        links = db._get("investor_portfolio_properties", {
            "property_id": f"in.({','.join(property_ids)})", "select": "id,investor_id,property_id"})
    except Exception as e:
        print(f"Loading portfolio property links failed: {e}")
        return 0

    by_property = {}
    for link in links:
        by_property.setdefault(link["property_id"], link)  # first match wins if more than one

    linked = 0
    for t in unlinked:
        link = by_property.get(t["property_id"])
        if not link:
            continue
        try:
            db._patch("maintenance_tickets", {"investor_id": link["investor_id"], "portfolio_property_id": link["id"]},
                     {"id": f"eq.{t['id']}"})
            linked += 1
        except Exception as e:
            print(f"Linking ticket {t['id']} to an investor failed: {e}")
    return linked


def refresh_alerts():
    """Recompute all three alert types from current data. Cheap and
    idempotent - call it as often as you like; run_daily_digest() always
    calls it first so the digest reflects the day's data."""

    if not db.ENABLED:
        return {"lease_60": 0, "lease_30": 0, "maintenance_7": 0, "resolved": 0}

    today = datetime.now(APP_TIMEZONE).date()
    active_keys = set()
    counts = {"lease_60": 0, "lease_30": 0, "maintenance_7": 0}

    # --- Lease-ending alerts --------------------------------------------
    try:
        properties = db._get("investor_portfolio_properties", {"select": "*", "limit": "5000"})
    except Exception as e:
        print(f"Loading portfolio properties for lease alerts failed: {e}")
        properties = []

    for p in properties:
        if (p.get("relationship") or "owned") not in ("owned", "acquired"):
            continue
        if not p.get("lease_end_date"):
            continue
        try:
            end = date.fromisoformat(p["lease_end_date"])
        except (TypeError, ValueError):
            continue

        days_left = (end - today).days
        place = p.get("address") or "a property"
        if p.get("unit_label"):
            place = f"{place} ({p['unit_label']})"

        if 30 < days_left <= 60:
            alert_type = "lease_60"
        elif 0 <= days_left <= 30:
            alert_type = "lease_30"
        else:
            continue

        key = f"{alert_type}:{p['id']}"
        active_keys.add(key)
        counts[alert_type] += 1
        _upsert_alert(
            investor_id=p["investor_id"], alert_type=alert_type, dedupe_key=key,
            title=f"{ALERT_LABELS[alert_type]} - {place}",
            detail=f"Lease at {place} ends {end.isoformat()} ({days_left} day{'s' if days_left != 1 else ''} away).",
            reference_date=end.isoformat(), portfolio_property_id=p["id"],
        )

    # --- Maintenance-ticket alerts ---------------------------------------
    _backfill_ticket_investors()
    try:
        tickets = db._get("maintenance_tickets", {"investor_id": "not.is.null", "select": "*", "limit": "5000"})
    except Exception as e:
        print(f"Loading maintenance tickets for alerts failed: {e}")
        tickets = []

    for t in tickets:
        if t.get("ticket_status") not in OPEN_TICKET_STATUSES:
            continue
        try:
            opened = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")).astimezone(APP_TIMEZONE).date()
        except (TypeError, ValueError, KeyError):
            continue
        days_open = (today - opened).days
        if days_open <= 7:
            continue

        key = f"maintenance_7:{t['id']}"
        active_keys.add(key)
        counts["maintenance_7"] += 1
        where = t.get("property_title") or t.get("property_id") or "a property"
        issue = (t.get("issue_type") or "issue").replace("_", " ")
        _upsert_alert(
            investor_id=t["investor_id"], alert_type="maintenance_7", dedupe_key=key,
            title=f"{ALERT_LABELS['maintenance_7']} - {where}",
            detail=f"{issue.capitalize()} reported at {where}, still {t['ticket_status'].replace('_', ' ')} "
                   f"after {days_open} days (opened {opened.isoformat()}).",
            reference_date=opened.isoformat(), maintenance_ticket_id=t["id"],
        )

    resolved = _resolve_stale_alerts(active_keys)
    return {**counts, "resolved": resolved}


# ---------------------------------------------------------------------
# Staff dashboard
# ---------------------------------------------------------------------

def list_open_alerts(limit=500, investor_id=None):
    """All open alerts (staff dashboard), or just one investor's (self-
    service - GET /me/investor/alerts in investor_journey.py) when
    investor_id is given."""
    if not db.ENABLED:
        return []
    try:
        params = {"status": "eq.open", "order": "created_at.desc", "select": "*", "limit": str(limit)}
        if investor_id:
            params["investor_id"] = f"eq.{investor_id}"
        alerts = db._get("investor_alerts", params)
    except Exception as e:
        print(f"Loading open alerts failed: {e}")
        return []
    if not alerts:
        return []

    investor_ids = sorted({a["investor_id"] for a in alerts})
    property_ids = sorted({a["portfolio_property_id"] for a in alerts if a.get("portfolio_property_id")})
    ticket_ids = sorted({a["maintenance_ticket_id"] for a in alerts if a.get("maintenance_ticket_id")})

    investors_by_id = {i["id"]: i for i in (
        db._get("investors", {"id": f"in.({','.join(investor_ids)})", "select": "id,name,phone,journey,whatsapp_alerts_opt_in"})
        if investor_ids else [])}
    properties_by_id = {p["id"]: p for p in (
        db._get("investor_portfolio_properties", {"id": f"in.({','.join(property_ids)})", "select": "*"})
        if property_ids else [])}
    tickets_by_id = {t["id"]: t for t in (
        db._get("maintenance_tickets", {"id": f"in.({','.join(ticket_ids)})", "select": "*"})
        if ticket_ids else [])}

    out = []
    for a in alerts:
        investor = investors_by_id.get(a["investor_id"]) or {}
        prop = properties_by_id.get(a.get("portfolio_property_id"))
        ticket = tickets_by_id.get(a.get("maintenance_ticket_id"))
        out.append({
            **a,
            "investor_name": investor.get("name"),
            "investor_phone": investor.get("phone"),
            "investor_opted_in": bool(investor.get("whatsapp_alerts_opt_in")),
            "property": prop,
            "ticket": ticket,
        })
    return out


def _open_counts():
    counts = {t: 0 for t in ALERT_TYPES}
    try:
        rows = db._get("investor_alerts", {"status": "eq.open", "select": "alert_type", "limit": "5000"})
    except Exception as e:
        print(f"Counting open alerts failed: {e}")
        rows = []
    for r in rows:
        counts[r["alert_type"]] = counts.get(r["alert_type"], 0) + 1
    return counts


@router.get("")
def get_alerts():
    configured()
    return {"alerts": list_open_alerts(), "counts": _open_counts()}


@router.post("/refresh")
def refresh(x_staybot_staff: str | None = Header(default=None)):
    """Recompute alerts on demand (the dashboard's Refresh button) instead
    of waiting for the next digest run."""
    configured()
    result = refresh_alerts()
    return {**result, "alerts": list_open_alerts(), "counts": _open_counts()}


@router.post("/{alert_id}/dismiss")
def dismiss_alert(alert_id: UUID, x_staybot_staff: str | None = Header(default=None)):
    configured()
    updated = q(db._patch, "investor_alerts", {"status": "dismissed"}, {"id": f"eq.{alert_id}", "status": "eq.open"})
    if not updated:
        raise HTTPException(404, "Open alert not found - it may already be resolved or dismissed.")
    return updated[0] if isinstance(updated, list) else updated


# ---------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------

def _within_sending_hours(now):
    return DIGEST_START_HOUR <= now.hour < DIGEST_END_HOUR


def _digest_text(investor, alerts):
    by_type = {}
    for a in alerts:
        by_type.setdefault(a["alert_type"], []).append(a)

    name = (investor.get("name") or "there").split()[0]
    lines = [f"Hi {name}, your Staybot property digest for today:", ""]
    for t in ALERT_TYPES:
        rows = by_type.get(t)
        if not rows:
            continue
        lines.append(f"{ALERT_LABELS[t]} ({len(rows)}):")
        for a in rows[:10]:
            lines.append(f"- {a['title'].split(' - ', 1)[-1]}")
        if len(rows) > 10:
            lines.append(f"...and {len(rows) - 10} more")
        lines.append("")
    lines.append("Reply to this message if you'd like help with any of these.")
    return "\n".join(lines)


def _digest_summary_line(alerts):
    """Short one-line summary for a template's text parameter - templates
    can't carry an arbitrary bulleted list, only short fixed-shape text."""

    counts = {}
    for a in alerts:
        counts[a["alert_type"]] = counts.get(a["alert_type"], 0) + 1
    parts = [f"{counts[t]} {ALERT_LABELS[t].lower()}" for t in ALERT_TYPES if counts.get(t)]
    return "; ".join(parts) or "updates on your properties"


def _send_digest_for_investor(investor_id, alerts, today):
    try:
        rows = db._get("investors", {"id": f"eq.{investor_id}", "select": "*", "limit": "1"})
    except Exception as e:
        return {"investor_id": investor_id, "status": "failed", "mode": None, "error": f"load_investor_failed: {e}", "alerts": len(alerts)}
    investor = rows[0] if rows else None
    if not investor:
        return {"investor_id": investor_id, "status": "skipped", "mode": None, "error": "investor_not_found", "alerts": len(alerts)}

    if not investor.get("whatsapp_alerts_opt_in"):
        return {"investor_id": investor_id, "status": "skipped", "mode": None, "error": "no_opt_in", "alerts": len(alerts)}

    phone = investor.get("phone")
    if not phone:
        return {"investor_id": investor_id, "status": "skipped", "mode": None, "error": "no_phone_on_file", "alerts": len(alerts)}

    try:
        already = db._get("investor_alert_digests", {
            "investor_id": f"eq.{investor_id}", "sent_date": f"eq.{today.isoformat()}", "select": "id", "limit": "1"})
    except Exception as e:
        print(f"Checking existing digest for investor {investor_id} failed: {e}")
        already = []
    if already:
        return {"investor_id": investor_id, "status": "skipped", "mode": None, "error": "already_sent_today", "alerts": len(alerts)}

    from src.services import onboarding_whatsapp  # deferred: avoids a hard dependency for non-WhatsApp callers

    text = _digest_text(investor, alerts)
    digits = re.sub(r"\D", "", phone)
    mode, error = None, None

    if whatsapp.DRY_RUN:
        # Same as owner_leads.py's send_intro(): dry run always uses the
        # plain-text path so it can be tested without real credentials.
        sent = whatsapp.send_text(digits, text)
        mode = "session"
        error = next((r.get("error") for r in sent if r.get("error")), None)
    elif onboarding_whatsapp.window_open(phone):
        sent = whatsapp.send_text(digits, text)
        mode = "session"
        error = next((r.get("error") for r in sent if r.get("error")), None)
    elif TEMPLATE_DIGEST:
        mode = "template"
        try:
            onboarding_whatsapp.send_payload(phone, {"type": "template", "template": {
                "name": TEMPLATE_DIGEST, "language": {"code": onboarding_whatsapp.TEMPLATE_LANGUAGE},
                "components": [{"type": "body", "parameters": [
                    {"type": "text", "text": (investor.get("name") or "there").split()[0]},
                    {"type": "text", "text": _digest_summary_line(alerts)},
                ]}],
            }})
        except Exception as e:
            error = str(e)[:300]
    else:
        error = "outside_24h_window_no_template"

    status = "failed" if error else "sent"
    try:
        db._post("investor_alert_digests", {
            "investor_id": investor_id, "sent_date": today.isoformat(),
            "alert_ids": [a["id"] for a in alerts], "channel": "whatsapp", "mode": mode,
            "status": status, "error": error,
        })
    except Exception as e:
        print(f"Logging digest for investor {investor_id} failed: {e}")

    return {"investor_id": investor_id, "status": status, "mode": mode, "error": error, "alerts": len(alerts)}


def run_daily_digest(force: bool = False):
    if not db.ENABLED:
        return {"sent": 0, "skipped": 0, "failed": 0, "reason": "database_not_configured"}

    now = datetime.now(APP_TIMEZONE)
    if not force and not _within_sending_hours(now):
        return {"sent": 0, "skipped": 0, "failed": 0, "reason": "outside_sending_hours",
                "local_time": now.isoformat(timespec="seconds"),
                "sending_hours": f"{DIGEST_START_HOUR:02d}:00-{DIGEST_END_HOUR:02d}:00 {APP_TIMEZONE.key}"}

    refresh_alerts()
    today = now.date()

    try:
        alerts = db._get("investor_alerts", {"status": "eq.open", "select": "*", "limit": "5000"})
    except Exception as e:
        print(f"Loading alerts for the digest failed: {e}")
        return {"sent": 0, "skipped": 0, "failed": 0, "reason": "load_alerts_failed"}

    by_investor = {}
    for a in alerts:
        by_investor.setdefault(a["investor_id"], []).append(a)

    results = []
    sent = skipped = failed = 0
    for investor_id, investor_alerts in by_investor.items():
        outcome = _send_digest_for_investor(investor_id, investor_alerts, today)
        results.append(outcome)
        if outcome["status"] == "sent":
            sent += 1
        elif outcome["status"] == "failed":
            failed += 1
        else:
            skipped += 1

    return {
        "sent": sent, "skipped": skipped, "failed": failed, "investors_with_open_alerts": len(by_investor),
        "local_time": now.isoformat(timespec="seconds"), "results": results,
    }


@router.post("/run-digest")
def trigger_digest(
    force: bool = Query(False, description="Send even outside daytime hours - for testing the flow immediately."),
    x_staybot_staff: str | None = Header(default=None),
):
    """The one endpoint an external daily cron (Render Cron Job, GitHub
    Actions schedule, cron-job.org, ...) should call once a day, using the
    same team Basic Auth credentials as every other staff route here - no
    new secret to manage. See the module docstring for exactly what it
    does and why."""
    configured()
    return run_daily_digest(force=force)
