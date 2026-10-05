"""
Per-account portfolio: the structured details the AI has picked up about
this person across every chat turn, merged over time so a later message
that only answers one question doesn't erase what was already known.

Tenant turns (result["role"] == "tenant") fill it from
result["requirements"] (location, budget, bedrooms, ...). Owner turns
(result["role"] == "owner") fill it from result["property_details"]
(what they're listing/renting out). Investor turns
(result["role"] == "investor") fill it from result["investor_profile"]
(their own buying brief: cash_available, goal, areas, timeline, ...) -
see SYSTEM_PROMPT in src/prompts/system_prompt.py for all three shapes.

Only logged-in accounts get a portfolio (see src/services/accounts.py) -
an anonymous browser chat has no account_id to key it on. That's also why
this is separate from the investor journeys table (src/services/investor_journey.py):
investors.py tracks the guided investing journey itself (stage, portfolio
*properties*, activity log); this is just "what do we currently know about
this person", for any account role, shown back to them on their dashboard.
"""

from src.services import db

# Which key in the AI's response holds this turn's structured details,
# depending on who the AI decided is speaking this turn. Investors are
# their own case: an "owner" turn fills result["property_details"] (the
# home *they're renting out*), but an "investor" turn fills
# result["investor_profile"] instead (cash_available, goal, areas,
# timeline, experience, ... - see SYSTEM_PROMPT) - it's the investor's
# own buying brief, a different shape, not a listing.
FIELD_SOURCES = {
    "tenant": "requirements",
    "owner": "property_details",
    "investor": "investor_profile",
}


def _new_fields(result: dict) -> dict | None:
    role = result.get("role")
    source_key = FIELD_SOURCES.get(role)
    if not source_key:
        return None

    fields = result.get(source_key)
    if not isinstance(fields, dict):
        return None

    # Only keep what the AI actually filled in this turn - a null/empty
    # answer must never overwrite something already known.
    kept = {k: v for k, v in fields.items() if v not in (None, "", [], {})}
    return kept or None


def _merge_into_portfolio(account_id: str, new_values: dict):
    """Shared save path: merge new_values into whatever's already stored
    for this account, never dropping a field the new turn didn't mention."""

    rows = db._get("account_portfolios", {
        "account_id": f"eq.{account_id}", "select": "details", "limit": "1",
    })
    existing = (rows[0]["details"] if rows else {}) or {}
    merged = {**existing, **new_values}

    if rows:
        db._patch("account_portfolios", {"details": merged}, {"account_id": f"eq.{account_id}"})
    else:
        db._post("account_portfolios", {"account_id": account_id, "details": merged})


def handle_portfolio_update(result: dict, account_id: str | None):
    """Best-effort: merge this turn's extracted fields into the account's
    portfolio. Called from chat.process_message alongside the other
    handle_* hooks (viewings, maintenance, owner_listings, investors).
    Never raises - a portfolio save failing must never break the chat
    reply the person is waiting on."""

    if not (db.ENABLED and account_id):
        return

    new_values = _new_fields(result)
    if not new_values:
        return

    try:
        _merge_into_portfolio(account_id, new_values)
    except Exception as e:
        print(f"Portfolio update failed (non-fatal): {e}")


def seed_from_registration(account_id: str, *, name: str = None, email: str = None,
                            existing_property_count: int | None = None):
    """Called once at signup (src/services/accounts.py POST /auth/register)
    so the portfolio already has the few details the registration form
    collected, instead of sitting empty until the person's first chat
    message. Later chat turns merge on top of this the same as always -
    handle_portfolio_update never overwrites a field with a blank answer,
    so these seeded values stick unless the person actually corrects them
    in conversation."""

    if not (db.ENABLED and account_id):
        return

    values = {}
    if name:
        values["name"] = name
    if email:
        values["email"] = email
    if existing_property_count is not None:
        values["existing_property_count"] = existing_property_count

    if not values:
        return

    try:
        _merge_into_portfolio(account_id, values)
    except Exception as e:
        print(f"Portfolio seed failed (non-fatal): {e}")


def merge_fields(account_id: str, values: dict):
    """Merge externally-collected fields (not from an AI chat turn) into the
    account's portfolio - e.g. the tenant screening intake (src/services/accounts.py
    POST /auth/screening/details): employment, income, EMI, ... Same merge
    path and same never-drop-a-field guarantee as handle_portfolio_update."""

    if not (db.ENABLED and account_id):
        return

    kept = {k: v for k, v in values.items() if v not in (None, "", [], {})}
    if not kept:
        return

    _merge_into_portfolio(account_id, kept)


# The tenant's home search ("requirements", see SYSTEM_PROMPT) - stale once
# they've bought the home they rented.
TENANT_SEARCH_KEYS = ("location", "property_type", "bedrooms", "bathrooms", "budget", "move_in_date",
                      "rent_or_buy", "pets", "parking", "furnished", "amenities")


def became_existing_investor(account_id: str, *, owned_count: int, home: str | None = None):
    """A tenant bought the home they rented (home_purchase.complete_purchase):
    their profile now reads as an Existing Property Investor's - the rent
    search is dropped, what they own is added. Screening details (income,
    employment) are kept. Never raises."""

    if not (db.ENABLED and account_id):
        return
    try:
        rows = db._get("account_portfolios", {"account_id": f"eq.{account_id}", "select": "details", "limit": "1"})
        details = {k: v for k, v in ((rows[0]["details"] if rows else {}) or {}).items() if k not in TENANT_SEARCH_KEYS}
        details.update({"account_type": "Existing Property Investor", "previously": "Tenant",
                        "existing_property_count": max(owned_count, 1)})
        if home:
            details["home_bought"] = home
        if rows:
            db._patch("account_portfolios", {"details": details}, {"account_id": f"eq.{account_id}"})
        else:
            db._post("account_portfolios", {"account_id": account_id, "details": details})
    except Exception as e:
        print(f"Updating the buyer's profile failed (non-fatal): {e}")


def get_portfolio(account_id: str) -> dict:
    """The merged details saved for this account, or {} if none yet /
    Supabase isn't configured."""

    if not db.ENABLED:
        return {}

    try:
        rows = db._get("account_portfolios", {
            "account_id": f"eq.{account_id}", "select": "details,updated_at", "limit": "1",
        })
        return rows[0] if rows else {}
    except Exception as e:
        print(f"Portfolio fetch failed (non-fatal): {e}")
        return {}
