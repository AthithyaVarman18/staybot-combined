"""
TurboTenant integration boundary.

The specification names TurboTenant as the system of record. TurboTenant
does not publish public API documentation, so no endpoints, onboarding URLs
or synchronization are implemented or simulated here. Until real API access
and documentation are provided, status is "not_connected" and staff use the
manual handoff: create the tenant/lease in TurboTenant themselves, then
record the link TurboTenant gives them.

To connect later: obtain API access from TurboTenant, set TURBOTENANT_API_BASE
and TURBOTENANT_API_KEY, and implement `sync_case` against their documented
endpoints. Setting the variables alone does not claim a connection.
"""

import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

API_BASE = (os.getenv("TURBOTENANT_API_BASE") or "").strip()
API_KEY = (os.getenv("TURBOTENANT_API_KEY") or "").strip()
ADAPTER_IMPLEMENTED = False


def status() -> dict:
    if API_BASE and API_KEY and not ADAPTER_IMPLEMENTED:
        label, detail = "needs_configuration", ("TurboTenant credentials are set, but no API adapter is implemented because "
                                               "TurboTenant's API endpoints have not been verified. Use the manual handoff.")
    else:
        label, detail = "not_connected", ("Not connected. TurboTenant has no public API documentation, so Staybot cannot create "
                                         "records or onboarding links there. Use the manual handoff below.")
    return {
        "status": label,
        "detail": detail,
        "link_generation": "not_implemented",
        "required_for_integration": [
            "API access and official API documentation from TurboTenant",
            "TURBOTENANT_API_BASE and TURBOTENANT_API_KEY in the server environment",
            "An adapter that creates the tenant, property link and lease records and returns their real IDs",
        ],
    }


def handoff_packet(case: dict) -> str:
    """Plain text staff can paste into TurboTenant when entering the case manually."""
    tenant, owner, terms = case.get("tenant") or {}, case.get("owner") or {}, case.get("terms") or {}
    lines = [
        f"Staybot onboarding case {case.get('reference')}",
        f"Property: {case.get('property', {}).get('title')} ({case.get('property_id')}), unit {case.get('unit') or '-'}",
        f"Address: {case.get('property_address') or '-'}",
        f"Tenant: {tenant.get('full_name', '-')} · WhatsApp {tenant.get('whatsapp', '-')}",
        f"Owner: {owner.get('full_name', '-')} · WhatsApp {owner.get('whatsapp', '-')}",
        f"Lease: {terms.get('lease_start', '-')} to {terms.get('lease_end', '-')}, move-in {terms.get('move_in_date', '-')}",
        f"Rent: {terms.get('currency', 'USD')} {terms.get('rent', '-')} ({terms.get('payment_schedule', '-')}, due day {terms.get('rent_due_day', '-')})",
        f"Deposit: {terms.get('currency', 'USD')} {terms.get('deposit', '-')}",
    ]
    return "\n".join(lines)


def validate_manual_link(link: str | None) -> str | None:
    """Staff-pasted TurboTenant link: must be https on turbotenant.com."""
    if not link:
        return None
    parsed = urlparse(link.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "turbotenant.com" or host.endswith(".turbotenant.com")):
        raise ValueError("Paste the https link from turbotenant.com.")
    if re.search(r"\s", link.strip()):
        raise ValueError("The link cannot contain spaces.")
    return link.strip()


def record_manual_handoff(staff_name: str, link: str | None, note: str) -> dict:
    return {
        "status": "manual_handoff_recorded",
        "recorded_by": staff_name,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "staff_entered_link": validate_manual_link(link),
        "note": note[:500],
        "synced_by_staybot": False,
    }
