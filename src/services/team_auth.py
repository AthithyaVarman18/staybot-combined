"""
Team password for the web pages and team APIs.

Customers use WhatsApp only, so everything except the WhatsApp webhook and
the health check is for the team. Protection is a single shared password
(HTTP Basic auth: the browser shows its own login box once, then remembers it).

.env:
    ADMIN_PASSWORD=long-random-password     # required once the server is online
    ADMIN_USERNAME=team                     # optional, default "team"

Without ADMIN_PASSWORD, the pages only open from this computer (localhost),
so forgetting to set it on a server never leaves customer data public.
"""

import base64
import hmac
import os

from dotenv import load_dotenv
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse


load_dotenv()

ADMIN_USERNAME = (os.getenv("ADMIN_USERNAME") or "team").strip()
ADMIN_PASSWORD = (os.getenv("ADMIN_PASSWORD") or "").strip()

# Reachable by anyone: Meta's webhook (checked by signature) and uptime checks.
PUBLIC_PATHS = {
    "/health", "/webhook/whatsapp",
    # Customer-facing account system and chat portal (src/services/accounts.py):
    # separate from the team-only pages below, protected by account login
    # (email/password + session cookie) instead of the shared team password.
    "/login", "/register", "/portal",
    # Forgot password / email password reset (src/services/password_reset.py).
    # Customer-account feature, NOT the Team login: both pages must open for
    # someone who is logged out of everything. They only ever show a form - the
    # POST endpoints (/auth/forgot-password, /auth/reset-password) are public
    # through the "/auth/" prefix below and never create a session.
    "/forgot-password", "/reset-password",
    "/dashboard.html",  # legacy leads-only page; the route itself requires an account session
    "/investor-education",  # New Property Investor primer; also requires an account session
    "/tenant-screening",  # Tenant screening intake; also requires an account session
    "/existing-investor-welcome",  # New -> Existing investor welcome; also requires an account session
    "/analyze", "/analyze/photo", "/properties", "/conversations/resume",
    "/me/investor", "/me/investor/portfolio",
    "/auth/requirements", "/requirements",
    # Hidden staff admin login page (not linked from any customer page).
    # POST /auth/admin/login is already public via the /auth/ prefix below.
    "/admin/login",
    # Privacy policy (src/services/privacy_page.py) - Meta and customers must be able to read it.
    "/privacy",
    # Login/register page scripts (3D property viewer).
    "/assets/property-scene.js",
    # Per-tab account session script (every account page, incl. login).
    "/assets/tab-session.js",
}

# Staff-only data: every lead, every owner lead, the outcomes report, the
# owner-submitted draft listings and every investor's alerts. A logged-in
# customer account (tenant / investor) is refused these even if it calls the
# API directly - hiding the tabs in index.html alone isn't enough. The admin
# account and the shared team password still get everything.
# The purchase-offer and inspection workspaces (pages and APIs) are staff-only
# too: they list every investor's offers, inspectors, reports and repairs, and
# let the caller change terms. Investor accounts get their own scoped, mostly
# read-only view instead under /me/investor/ (investor_acquisition.py).
STAFF_ONLY_PREFIXES = ("/leads", "/owner-leads", "/reports/outcomes", "/listings/pending", "/alerts",
                       "/purchase-offers", "/inspections", "/team/purchase-offers", "/team/inspections",
                       # Every tenant's rental application (the team's approval step), every
                       # maintenance ticket, and every tenant's screening answers/income
                       # documents. Tenants and owners get their own scoped views instead
                       # under /me/rentals/ and /me/owner/ (src/services/rentals.py).
                       "/applications", "/maintenance", "/screening",
                       # Every tenant's journey (src/services/tenant_journeys.py).
                       "/tenant-journeys",
                       # Every viewing request with every customer's phone. Owners
                       # see the ones for their own homes at /me/viewings (main.py).
                       "/viewings",
                       # Every lease onboarding case (both /onboarding/cases and the
                       # older /onboarding records): tenant and owner names, phones,
                       # rents, deposits, ID / income documents and agreements.
                       # Tenants and owners use their own private /p/<token> link.
                       "/onboarding",
                       # The team's investor CRM, every investor's journey, and the
                       # team's saved deal analyses (with staff-entered assumptions).
                       # Investors use their own scoped views under /me/investor.
                       "/investors", "/journeys", "/deals",
                       # Every WhatsApp message sent in test mode (every customer's
                       # phone number and text), and the delivery-status simulator.
                       "/whatsapp/outbox", "/whatsapp/simulate-status",
                       # One card per customer with their phone, chats and the staff
                       # member's notes (src/services/customers.py).
                       "/customers",
                       # The Stats tab: every customer's traffic, reply times and models.
                       "/metrics", "/staff/requirements", "/team/client-requirements")


# Paths inside a staff-only prefix that any logged-in account may still read
# because they hold no customer data (the investor journey stage names shown
# on the New Property Investor primer, investor_education.html).
ACCOUNT_READABLE_PATHS = {"/journeys/config/journeys"}

# Readable by customers, but only the team may change them: approving or
# rejecting any listing (PATCH /properties/<id>) and importing MLS data.
STAFF_ONLY_WRITES = ("/properties", "/mls")


# Staff-only except for sending: customers create an enquiry from the chat's
# "Enquire about this" (POST /inquiries), but only the team may list them
# (every customer's name and phone, plus owner contacts) or change them.
STAFF_ONLY_UNLESS_POST = ("/inquiries",)


def _under(path: str, prefixes) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def staff_only(path: str, method: str = "GET") -> bool:
    method = method.upper()
    if method in ("GET", "HEAD") and path in ACCOUNT_READABLE_PATHS:
        return False
    if _under(path, STAFF_ONLY_PREFIXES):
        return True
    if method not in ("GET", "HEAD", "OPTIONS") and _under(path, STAFF_ONLY_WRITES):
        return True
    return method.upper() != "POST" and any(path == p or path.startswith(p + "/") for p in STAFF_ONLY_UNLESS_POST)

# Tenant/owner onboarding pages: protected by personal, expiring, case-scoped
# link tokens (checked in src/services/onboarding_party.py), not the team password.
# /o/ = owner reply page from the outreach email (token-protected).
# /auth/ = register/login/logout/me (src/services/accounts.py; its own auth).
# /purchase-approval/ = investor's private, expiring, version-bound offer
# summary approval link (token-checked in src/services/purchase_offers.py).
PUBLIC_PREFIXES = ("/p/", "/o/", "/auth/", "/purchase-approval/")

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def credentials_ok(header: str) -> bool:

    if not header or not header.lower().startswith("basic "):
        return False

    try:
        username, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False

    return (
        hmac.compare_digest(username.encode(), ADMIN_USERNAME.encode())
        and hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode())
    )


# Typing a data API into the address bar (e.g. /onboarding/cases) used to
# print every record as raw JSON. Those URLs are for the dashboard's own
# fetch() calls, so a direct browser visit is sent to the matching tab
# instead. PDFs, file downloads and HTML pages are unaffected (only JSON
# responses are swapped), and API tools / fetch() still get the JSON.
UI_TAB_FOR_PREFIX = (
    ("/onboarding", "onboarding"), ("/leads", "leads"), ("/owner-leads", "ownerleads"),
    ("/inquiries", "inquiries"), ("/viewings", "viewings"), ("/me/viewings", "viewings"),
    ("/maintenance", "maintenance"), ("/listings/pending", "listings"), ("/alerts", "alerts"),
    ("/applications", "applications"), ("/tenant-journeys", "tjourneys"),
    ("/deals", "deals"), ("/mls", "deals"), ("/investors", "investors"), ("/journeys", "journeys"),
    ("/purchase-offers", "offers"), ("/whatsapp", "whatsapp"), ("/metrics", "stats"), ("/customers", "customers"),
    ("/me/messages", "messages"), ("/me/notifications", "notifications"),
    ("/me/account-notifications", "notifications"), ("/me/listings", "mylistings"),
)

# FastAPI's own API explorer stays reachable for the team.
RAW_JSON_OK = {"/openapi.json", "/docs", "/redoc", "/", "/health"}


def is_browser_navigation(request) -> bool:
    if request.method not in ("GET", "HEAD"):
        return False
    mode = request.headers.get("sec-fetch-mode")
    if mode:
        return mode == "navigate"
    # Older browsers without Fetch Metadata: an address-bar visit asks for HTML.
    return "text/html" in request.headers.get("accept", "")


def ui_url_for(path: str) -> str:
    for prefix, tab in UI_TAB_FOR_PREFIX:
        if path == prefix or path.startswith(prefix + "/"):
            return f"/ui#{tab}"
    return "/ui"


async def hide_raw_json(request, response):
    if (
        request.url.path not in RAW_JSON_OK
        and is_browser_navigation(request)
        and response.headers.get("content-type", "").startswith("application/json")
    ):
        return RedirectResponse(ui_url_for(request.url.path), status_code=303)
    return response


class TeamAuthMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request, call_next):
        response = await self.check_access(request, call_next)
        return await hide_raw_json(request, response)

    async def check_access(self, request, call_next):

        if request.url.path in PUBLIC_PATHS or request.url.path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        # Logged-in customer accounts (tenant / new_investor / existing_investor)
        # now land on /ui after login instead of the team password prompt -
        # their account cookie stands in for the shared team password on
        # every page and API call /ui needs (chat, maintenance, whatsapp
        # test, stats, its /ui/*.js assets, etc). Checked first (and ahead
        # of the ADMIN_PASSWORD checks below) so a real account always works
        # even if the team password isn't configured yet; current_account()
        # returns fast with no DB call when there's no account cookie at all,
        # so this costs nothing extra for ordinary team-password traffic.
        # Local import avoids a circular import at module load time
        # (accounts.py doesn't import this module).
        from src.services import accounts

        account = accounts.current_account(request)
        if account:
            if staff_only(request.url.path, request.method) and not accounts.is_admin(account):
                # Someone typed a staff page into the address bar (e.g.
                # /inspections, /team/purchase-offers): send them back to
                # their own dashboard instead of a bare error page. API calls
                # from the dashboard's fetch() still get a plain 403.
                if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                    return RedirectResponse("/ui", status_code=303)
                return PlainTextResponse("Staff only.", status_code=403)
            return await call_next(request)

        if not ADMIN_PASSWORD:

            client = request.client.host if request.client else ""
            forwarded = request.headers.get("x-forwarded-for")

            # Behind a hosting proxy the client is never localhost.
            if client in LOCAL_HOSTS and not forwarded:
                return await call_next(request)

            return PlainTextResponse(
                "Set ADMIN_PASSWORD in the server's environment to open the team pages.",
                status_code=503,
            )

        if credentials_ok(request.headers.get("authorization")):
            return await call_next(request)

        return PlainTextResponse(
            "Team password required.",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Staybot team", charset="UTF-8"'},
        )
