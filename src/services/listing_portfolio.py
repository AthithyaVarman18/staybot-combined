"""Keep an investor's Portfolio in step with the homes they list.

A home an investor lists (My listings form, or the chat - my_listings.py /
owner_listings.py) is a home they own, so it gets a row in their Portfolio
(investor_portfolio_properties, relationship 'owned', linked by
property_id). The listing is the source of truth for what it shares:

  rent listing  -> monthly_rent  = the listing's rent
  sale listing  -> estimated_value = the listing's sale price
  both          -> address / type / bedrooms / bathrooms (filled if missing)

so changing the rent or price on My listings changes the Portfolio too.
A home that's sold (to a buyer through Staybot, or marked sold) leaves the
seller's Portfolio; if the sale is cancelled and the home relisted, it comes
back. (A home the account BOUGHT from its landlord - home_purchase.py - is
also 'sold', off the market, but it's theirs: it stays in their Portfolio.) A row the investor removed from their Portfolio themselves stays
removed (investor_activity 'portfolio_property_removed' with its property_id).

sync() never raises - the Portfolio and My listings still load without it.
"""

from typing import Optional

from src.services import db

SOURCES = ("owner_chat", "investor_form")
INVESTOR_ROLES = ("new_investor", "existing_investor")


def _num(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _address(p: dict) -> Optional[str]:
    location = str(p.get("location") or "").strip()
    area = str(p.get("area") or "").strip()
    title = str(p.get("title") or "").strip()
    if location and location.lower() != area.lower():
        return location
    return title or location or area or None


def listing_fields(p: dict) -> dict:
    """What a listing tells the Portfolio (None values are left out)."""
    out = {
        "address": _address(p),
        "property_type": p.get("property_type"),
        "bedrooms": p.get("bedrooms"),
        "bathrooms": p.get("bathrooms"),
    }
    if p.get("listing_type") == "sale":
        out["estimated_value"] = _num(p.get("sale_price"))
    else:
        out["monthly_rent"] = _num(p.get("rent"))
    return {k: v for k, v in out.items() if v is not None}


# The listing always wins for these; the rest only fill gaps.
LISTING_OWNS = ("monthly_rent", "estimated_value")


def investor_for(account: dict) -> Optional[dict]:
    if not account or account.get("role") not in INVESTOR_ROLES:
        return None
    from src.services import rentals  # local import - rentals imports a lot
    return rentals.buyer_investor_or_create(account["id"])


def own_listings(account: dict) -> list[dict]:
    return db._get("properties", {
        "session_id": f"eq.{account['session_id']}", "source": f"in.({','.join(SOURCES)})", "select": "*",
    }) or []


def removed_by_investor(investor_id: str) -> set[str]:
    try:
        rows = db._get("investor_activity", {
            "investor_id": f"eq.{investor_id}", "action": "eq.portfolio_property_removed",
            "select": "details", "limit": "1000"}) or []
    except Exception:
        return set()
    return {str((r.get("details") or {}).get("property_id")) for r in rows if (r.get("details") or {}).get("property_id")}


def bought_homes(account: dict) -> dict[str, dict]:
    """property_id -> the completed home_buy_requests row for homes this
    account bought from their landlord ("Own this house", home_purchase.py).
    Those listings move to the buyer marked 'sold' (off the market) - but
    here 'sold' means "you own it", not "you sold it". Never raises."""
    if not db.ENABLED or not account or not account.get("id"):
        return {}
    try:
        rows = db._get("home_buy_requests", {
            "tenant_account_id": f"eq.{account['id']}", "status": "eq.completed",
            "select": "id,property_id,plan", "limit": "200"}) or []
    except Exception:
        return {}  # table not created yet - nobody bought a rented home
    return {r["property_id"]: r for r in rows if r.get("property_id")}


def sync(account: dict, listings: Optional[list[dict]] = None) -> None:
    if not db.ENABLED:
        return
    try:
        investor = investor_for(account)
        if not investor:
            return
        if listings is None:
            listings = own_listings(account)
        listings = [p for p in listings if p.get("id") and p.get("source") in SOURCES]
        if not listings:
            return
        ids = [p["id"] for p in listings]
        linked = {r["property_id"]: r for r in (db._get("investor_portfolio_properties", {
            "investor_id": f"eq.{investor['id']}", "property_id": f"in.({','.join(ids)})",
            "relationship": "eq.owned", "select": "*"}) or [])}
        removed = None
        bought = None

        for p in listings:
            row = linked.get(p["id"])
            if p.get("status") == "sold":
                if bought is None:
                    bought = bought_homes(account)
                if p["id"] in bought:
                    # A home they bought (home_purchase.complete_purchase
                    # already put it in their Portfolio) - never remove it.
                    continue
            if p.get("status") in ("sold", "pending"):
                # Sold: not theirs any more. Pending: not live yet (team review).
                if row and p.get("status") == "sold":
                    db._delete("investor_portfolio_properties", {"id": f"eq.{row['id']}"})
                continue
            fields = listing_fields(p)
            if row:
                changes = {k: v for k, v in fields.items()
                           if (k in LISTING_OWNS and _num(row.get(k)) != v) or (k not in LISTING_OWNS and row.get(k) in (None, ""))}
                if changes:
                    db._patch("investor_portfolio_properties", changes, {"id": f"eq.{row['id']}"})
                continue
            if removed is None:
                removed = removed_by_investor(investor["id"])
            if p["id"] in removed:
                continue
            db._post("investor_portfolio_properties", {
                **fields, "investor_id": investor["id"], "relationship": "owned", "property_id": p["id"],
                "condition_notes": "Added from your listing on Staybot (My listings).",
            })
    except Exception as e:
        print(f"Syncing listings into the portfolio failed (non-fatal): {e}")
