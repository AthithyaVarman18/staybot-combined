"""
Listing checks: is this home ready to show customers?

Run before a listing goes live - from the owner chat (owner_listings.py),
an investor's "My listings" form (my_listings.py) and the team's
"Approve & publish" (main.py PATCH /properties/{id}).

  problems  - stop it going live until fixed: no price, no address,
              no bedrooms, or a price / size that looks like a typo
              (rent of $18 instead of $1,800).
  warnings  - shown to the owner and the team, but don't stop it:
              no photos yet, no description, a very large deposit.

Plain-English messages, shown as-is in the app and in the chat reply.
"""

from src.prompts.system_prompt import CURRENCY
from src.services.properties import parse_int, parse_money

# Sensible ranges for the market. Outside them it's almost always a typo
# (a missing zero, or rent typed into the price box).
if CURRENCY == "USD":
    RENT_RANGE = (300, 25_000)               # $ a month
    SALE_RANGE = (10_000, 10_000_000)        # $ (cheap mobile homes / lots exist)
    SYMBOL = "$"
else:
    RENT_RANGE = (2_000, 1_000_000)          # ₹ a month
    SALE_RANGE = (300_000, 1_000_000_000)    # ₹
    SYMBOL = "₹"

MAX_ROOMS = 12
DEPOSIT_MAX_MONTHS = 3
# Homes that don't have bedrooms, so none is fine.
NO_BEDROOM_TYPES = ("studio", "land", "lot", "plot", "commercial", "office", "shop", "retail", "warehouse")


def _money(amount) -> str:
    return f"{SYMBOL}{amount:,.0f}"


def check(listing: dict) -> dict:
    """{"ok": bool, "problems": [...], "warnings": [...]} for one listing
    (a properties row, or the fields about to be saved)."""

    p = listing or {}
    problems, warnings = [], []
    sale = p.get("listing_type") == "sale" or (p.get("sale_price") and not p.get("rent"))
    price = parse_money(p.get("sale_price") if sale else p.get("rent"))

    # -- price
    if not price:
        problems.append("Add the sale price." if sale else "Add the monthly rent.")
    else:
        low, high = SALE_RANGE if sale else RENT_RANGE
        what = "A sale price" if sale else "Rent"
        per = "" if sale else " a month"
        if price < low:
            problems.append(f"{what} of {_money(price)}{per} looks too low - please check it (most are over {_money(low)}).")
        elif price > high:
            problems.append(f"{what} of {_money(price)}{per} looks too high - please check it.")

    # -- address
    if not str(p.get("location") or p.get("street_address") or "").strip():
        problems.append("Add the address or area.")

    # -- bedrooms
    kind = str(p.get("property_type") or "").lower()
    beds = parse_int(p.get("bedrooms"))
    if beds is None and not any(t in kind for t in NO_BEDROOM_TYPES):
        problems.append("Add the number of bedrooms.")
    elif beds is not None and beds > MAX_ROOMS:
        problems.append(f"{beds} bedrooms looks wrong - please check it.")
    baths = parse_int(p.get("bathrooms"))
    if baths is not None and baths > MAX_ROOMS:
        problems.append(f"{baths} bathrooms looks wrong - please check it.")

    # -- smaller things: shown, but don't stop it going live
    if not p.get("photos"):
        warnings.append("No photos yet - homes with photos get many more enquiries.")
    if not str(p.get("description") or "").strip():
        warnings.append("No description yet.")
    deposit = parse_money(p.get("deposit"))
    rent = parse_money(p.get("rent"))
    if deposit and rent and not sale and deposit > rent * DEPOSIT_MAX_MONTHS:
        warnings.append(f"Deposit of {_money(deposit)} is more than {DEPOSIT_MAX_MONTHS} months' rent - please check it.")
    if beds is not None and baths is not None and baths > beds + 3:
        warnings.append(f"{baths} bathrooms for {beds} bedrooms - please check it.")

    return {"ok": not problems, "problems": problems, "warnings": warnings}


def problems_text(result: dict) -> str:
    return " ".join(result.get("problems") or [])
