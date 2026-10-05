"""
Property listings and search.

Listings come from the Supabase `properties` table. Until
supabase_properties.sql has been run (or if Supabase isn't configured),
the sample listings in src/data/properties.json are used instead.

Search is plain rules, not the LLM: the LLM extracts what the customer
wants (requirements), and this module finds listings that fit. That way
the AI can never invent a property that doesn't exist.
"""

import json
import re
import time
from pathlib import Path

from src.prompts.system_prompt import CURRENCY
from src.services import db


SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "properties.json"

CACHE_SECONDS = 30

MAX_MATCHES = 3

# Allow a listing slightly over budget as a "close match".
BUDGET_STRETCH = 1.10

_cache = {"at": 0.0, "rows": None, "source": None}


# ---------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------

def all_properties():
    """Return (listings, source) where source is 'supabase' or 'sample'."""

    now = time.time()

    if _cache["rows"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["rows"], _cache["source"]

    rows, source = None, "sample"

    if db.ENABLED:
        try:
            rows = db.list_properties()
            source = "supabase"
        except Exception as e:
            print(f"Properties table not available, using sample listings: {e}")

    if rows is None:
        rows = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        source = "sample"

    _cache.update(at=now, rows=rows, source=source)

    return rows, source


def get_property(property_id: str):

    rows, _ = all_properties()

    return next((p for p in rows if p.get("id") == property_id), None)


def property_context(p: dict) -> dict:
    """The facts the AI is allowed to use about one listing.
    Unknown values are left out so the AI says they need checking."""

    keys = [
        "property_type", "bedrooms", "bathrooms", "location", "listing_type",
        "rent", "sale_price", "deposit", "pets_allowed", "parking",
        "furnished", "amenities", "available_from", "description",
    ]

    context = {"property_id": p.get("ref") or p.get("id")}

    for key in keys:
        value = p.get(key)
        if value is None or value == [] or value == "":
            continue
        context[key] = value

    if context.get("rent") or context.get("sale_price") or context.get("deposit"):
        context["currency"] = CURRENCY

    return context


# ---------------------------------------------------------------------
# Parsing what the AI extracted
# ---------------------------------------------------------------------

MONEY_UNITS = {
    "k": 1e3, "thousand": 1e3,
    "l": 1e5, "lac": 1e5, "lacs": 1e5, "lakh": 1e5, "lakhs": 1e5,
    "cr": 1e7, "crore": 1e7, "crores": 1e7,
}

MONEY_RE = re.compile(
    r"(\d+(?:,\d+)*(?:\.\d+)?)\s*(k|thousand|lakhs?|lacs?|l|crores?|cr)?\b",
    re.IGNORECASE
)


def parse_money(value):
    """'40k' -> 40000, 'under ₹35,000/month' -> 35000, '1.5 Cr' -> 15000000.
    For ranges like '30-40k' the upper value is used."""

    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None

    amounts = [
        float(number.replace(",", "")) * MONEY_UNITS.get(unit.lower(), 1)
        for number, unit in MONEY_RE.findall(str(value))
    ]

    amounts = [a for a in amounts if a > 0]

    return max(amounts) if amounts else None


def parse_int(value):

    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, (int, float)):
        return int(value)

    match = re.search(r"\d+", str(value))

    return int(match.group()) if match else None


def bhk_in(text):
    """'3BHK apartment' / '2 bhk' -> 3 / 2."""
    match = re.search(r"(\d)\s*-?\s*bhk", str(text or ""), re.IGNORECASE)
    return int(match.group(1)) if match else None


def wants(value) -> bool:
    """True when the customer said they need it (pets, parking)."""

    if value is True:
        return True

    return isinstance(value, str) and value.strip().lower() in [
        "yes", "true", "required", "needed", "must", "y"
    ]


def listing_type_from(rent_or_buy):

    text = str(rent_or_buy or "").lower()

    if any(word in text for word in ["buy", "purchase", "sale", "own"]):
        return "sale"

    if any(word in text for word in ["rent", "lease", "let"]):
        return "rent"

    return None


LOCATION_ALIASES = {
    # State / country names say nothing about WHICH town - drop them, or
    # "Garner, NC" would match every home whose address ends in ", NC".
    "north carolina": " ",
    "united states": " ",
    "old mahabalipuram road": "omr",
    "rajiv gandhi salai": "omr",
    "east coast road": "ecr",
    "grand southern trunk": "gst",
}

LOCATION_STOPWORDS = {
    "nc", "usa", "county",
    "chennai", "tamil", "nadu", "india", "near", "in", "at", "the", "area",
    "around", "and", "or", "city", "road", "any", "anywhere", "side", "location",
}

# Words that appear in many place names, so they can't match on their own
# ("Anna Nagar" must not match "T. Nagar").
LOCATION_GENERIC = {"nagar", "puram", "pet", "pakkam", "kottai", "salai", "new", "old", "east", "west", "north", "south"}


def location_tokens(text: str) -> list[str]:

    text = str(text or "").lower()

    for phrase, alias in LOCATION_ALIASES.items():
        text = text.replace(phrase, alias)

    return [
        token for token in re.findall(r"[a-z]+", text)
        if len(token) > 1 and token not in LOCATION_STOPWORDS
    ]


def location_matches(wanted: str, p: dict) -> bool:

    tokens = location_tokens(wanted)

    if not tokens:
        return True

    haystack = set(location_tokens(" ".join(
        str(p.get(key) or "") for key in ["location", "area", "city", "title"]
    )))

    significant = [t for t in tokens if t not in LOCATION_GENERIC] or tokens

    return any(token in haystack for token in significant)


# ---------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------

def format_inr(amount) -> str:
    """Indian digit grouping: 200000 -> 2,00,000."""

    digits = str(int(round(amount)))

    if len(digits) <= 3:
        return digits

    head, tail = digits[:-3], digits[-3:]
    groups = []

    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]

    if head:
        groups.insert(0, head)

    return ",".join(groups + [tail])


def price_of(p: dict):
    return p.get("rent") if p.get("listing_type") == "rent" else p.get("sale_price")


def price_label(p: dict) -> str:

    price = price_of(p)

    if not price:
        return "Price on request"

    # US market: plain dollars. India: rupees with lakh/crore shorthand.
    if CURRENCY == "USD":
        return f"${price:,.0f}/month" if p.get("listing_type") == "rent" else f"${price:,.0f}"

    if p.get("listing_type") == "rent":
        return f"₹{format_inr(price)}/month"

    if price >= 1e7:
        return f"₹{price / 1e7:.2f}".rstrip("0").rstrip(".") + " Cr"

    if price >= 1e5:
        return f"₹{price / 1e5:.2f}".rstrip("0").rstrip(".") + " L"

    return f"₹{format_inr(price)}"


# ---------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------

def search(requirements: dict, limit: int = MAX_MATCHES):
    """
    Find listings that fit the customer's requirements.

    Returns None when there is nothing to search on yet (no location,
    size, budget, type or rent/buy), otherwise
    {"criteria": {...}, "total": n, "matches": [...]}.
    """

    req = requirements or {}

    criteria = {
        "listing_type": listing_type_from(req.get("rent_or_buy")),
        "location": (str(req.get("location")).strip() if req.get("location") else None),
        # The AI sometimes files "3BHK" under property_type instead of bedrooms.
        "bedrooms": parse_int(req.get("bedrooms")) or bhk_in(req.get("property_type")),
        "budget": parse_money(req.get("budget")),
        "property_type": (str(req.get("property_type")).strip().lower() if req.get("property_type") else None),
        "pets": wants(req.get("pets")),
        "parking": wants(req.get("parking")),
    }

    searchable = ["listing_type", "location", "bedrooms", "budget", "property_type"]

    if not any(criteria[key] for key in searchable):
        return None

    rows, _ = all_properties()
    found = [m for m in (score_listing(p, criteria) for p in rows) if m]
    relaxed = None

    # Nothing fits everything? Don't leave the customer with an empty reply -
    # show the closest homes we do have, each labelled with how it differs:
    #   1. the same area, any size / price (their area matters most), and
    #   2. the same size and budget in other areas.
    if not found and any(criteria[k] for k in ("location", "bedrooms", "budget")):
        seen = set()
        for p in rows:
            m = (score_listing(p, criteria, relax_size_price=True) if criteria["location"] else None) \
                or score_listing(p, {**criteria, "location": None}, outside=criteria["location"])
            if m and m["id"] not in seen:
                seen.add(m["id"])
                found.append(m)
        if not found:  # still nothing: the rest of what's available to rent / buy
            found = [m for m in (score_listing(p, {**criteria, "location": None}, relax_size_price=True,
                                               outside=criteria["location"]) for p in rows) if m]
        relaxed = bool(found)

    # Exact matches first, then best score, then cheapest.
    found.sort(key=lambda m: (m["exact"], m["match_score"], -(m["price"] or 0)), reverse=True)

    return {
        "criteria": {key: value for key, value in criteria.items() if value},
        "total": len(found),
        "matches": found[:limit],
        "relaxed": bool(relaxed),
    }


def score_listing(p: dict, criteria: dict, relax_size_price: bool = False, outside: str = None):
    """One listing against the criteria: a match card, or None if it doesn't fit.
    relax_size_price: keep a home whatever its size or price, noting the difference.
    outside: the area the customer asked for, when this home is shown from elsewhere."""

    score = 0
    reasons = []
    notes = []

    if criteria["listing_type"] and p.get("listing_type") != criteria["listing_type"]:
        return None

    if criteria["location"]:
        if not location_matches(criteria["location"], p):
            return None
        score += 40
        reasons.append(f"In {p.get('area') or p.get('location')}")
    elif outside:
        city = str(p.get("city") or "").strip()
        town = city if city and city.lower() not in ("north carolina", "nc") else (p.get("area") or p.get("location"))
        notes.append(f"in {town}, not {outside}")

    if criteria["bedrooms"]:
        beds = p.get("bedrooms")
        if beds == criteria["bedrooms"]:
            score += 25
            reasons.append(f"{beds}BHK")
        elif beds is not None and abs(beds - criteria["bedrooms"]) == 1:
            score += 8
            notes.append(f"{beds}BHK instead of {criteria['bedrooms']}BHK")
        elif relax_size_price and beds is not None:
            notes.append(f"{beds}BHK instead of {criteria['bedrooms']}BHK")
        else:
            return None

    if criteria["budget"]:
        price = price_of(p)
        if not price:
            notes.append("price to confirm")
        elif price <= criteria["budget"]:
            score += 25
            reasons.append("Within budget")
        elif price <= criteria["budget"] * BUDGET_STRETCH:
            score += 8
            notes.append("slightly over budget")
        elif relax_size_price:
            notes.append("over budget")
        else:
            return None

    if criteria["property_type"]:
        wanted = criteria["property_type"].replace("flat", "apartment")
        actual = str(p.get("property_type") or "").lower()
        if wanted in actual or actual in wanted:
            score += 5

    if criteria["pets"]:
        if p.get("pets_allowed") is False:
            return None
        if p.get("pets_allowed") is True:
            score += 5
            reasons.append("Pets allowed")
        else:
            notes.append("pets to confirm")

    if criteria["parking"]:
        if p.get("parking") is False:
            return None
        if p.get("parking") is True:
            score += 5
            reasons.append("Parking")

    return {
        "id": p.get("id"),
        "title": p.get("title"),
        "area": p.get("area"),
        "location": p.get("location"),
        "listing_type": p.get("listing_type"),
        "bedrooms": p.get("bedrooms"),
        "price": price_of(p),
        "price_label": price_label(p),
        "match_score": score,
        "exact": not notes,
        "reasons": reasons,
        "notes": notes,
    }


def matches_text(result: dict) -> str:
    """A plain-text list of the matches, so the reply also makes sense
    outside the web page (for example on WhatsApp later)."""

    matches = result["matches"]

    if not matches:
        return "I couldn't find a listing that matches all of that right now."

    if result.get("relaxed") or not any(m["exact"] for m in matches):
        lines = ["Nothing fits all of that exactly right now, but here are the closest options:"]
    else:
        lines = ["Here are the best matches:" if len(matches) > 1 else "Here's a match:"]

    for number, m in enumerate(matches, start=1):
        extra = "" if m["exact"] else f" ({', '.join(m['notes'])})"
        lines.append(f"{number}. {m['title']} - {m['price_label']}, {m['area']}{extra}")

    if result["total"] > len(matches):
        lines.append(f"(+{result['total'] - len(matches)} more)")

    return "\n".join(lines)


LIST_LINE_RE = re.compile(r"^\s*(\d+)\.\s+(.+)$", re.MULTILINE)


def last_shown_list(conversation_history: list) -> list[dict]:
    """
    The listings in the most recent numbered list the assistant sent
    ("1. Title - price, area"), in list order. Replies that only mention
    a listing by name don't count, so "the second one" keeps referring
    to the list the customer actually saw.
    """

    rows, _ = all_properties()
    by_title = sorted(
        (p for p in rows if p.get("title")),
        key=lambda p: len(p["title"]),
        reverse=True
    )

    for item in reversed(conversation_history or []):

        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue

        shown = []

        for _, line in LIST_LINE_RE.findall(str(item.get("content") or "")):
            p = next((p for p in by_title if line.startswith(p["title"])), None)
            if p:
                shown.append(p)

        if shown:
            return shown

    return []


def shown_in_history(conversation_history: list, limit: int = 6) -> list[dict]:
    """Facts for the last shown list, so the AI can answer
    "is the first one pet friendly?" from real data."""

    return [
        {"position": number, "id": p["id"], "title": p["title"], **property_context(p)}
        for number, p in enumerate(last_shown_list(conversation_history)[:limit], start=1)
    ]


BEDS_RE = re.compile(r"\b(\d{1,2}|one|two|three|four|five)\s*-?\s*(?:bed(?:room)?s?|br|bhk)\b", re.IGNORECASE)
STUDIO_RE = re.compile(r"\bstudio\b", re.IGNORECASE)
WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
BUDGET_RE = re.compile(r"(?:\$\s*\d[\d,]*(?:\.\d+)?\s*k?|\b\d[\d,]*(?:\.\d+)?\s*k?\s*(?:/\s*(?:mo|month)|a month|per month|dollars|usd)\b)", re.IGNORECASE)
RENT_RE = re.compile(r"\b(rent|renting|lease|to let)\b", re.IGNORECASE)
BUY_RE = re.compile(r"\b(buy|buying|purchase|for sale)\b", re.IGNORECASE)


def backfill_requirements(requirements: dict, messages: list[str]) -> dict:
    """Safety net for the search: the AI is asked to rebuild the requirements
    from the whole conversation every turn, but when it drops one (the town,
    bedrooms, budget or rent/buy) the search silently ran without it - or not
    at all. Fill only what's missing, from the customer's own messages, newest
    first. Town names are only taken when they're towns we have listings in."""

    req = dict(requirements or {})
    texts = [str(t or "") for t in reversed(messages or []) if str(t or "").strip()]
    if not texts:
        return req

    if not req.get("bedrooms") and not bhk_in(req.get("property_type")):
        for t in texts:
            m = BEDS_RE.search(t)
            if m:
                word = m.group(1).lower()
                req["bedrooms"] = WORD_NUMBERS.get(word) or int(word)
                break
            if STUDIO_RE.search(t):
                break

    if not parse_money(req.get("budget")):
        for t in texts:
            m = BUDGET_RE.search(t)
            if m:
                req["budget"] = m.group(0).strip()
                break

    if not listing_type_from(req.get("rent_or_buy")):
        for t in texts:
            if RENT_RE.search(t):
                req["rent_or_buy"] = "rent"
                break
            if BUY_RE.search(t):
                req["rent_or_buy"] = "buy"
                break

    if not str(req.get("location") or "").strip():
        rows, _ = all_properties()
        towns = {}
        for p in rows:
            for key in ("area", "city"):
                name = str(p.get(key) or "").strip()
                if name and location_tokens(name):
                    towns.setdefault(name.lower(), name)
        for t in texts:
            low = t.lower()
            hit = next((towns[n] for n in sorted(towns, key=len, reverse=True)
                        if re.search(r"\b" + re.escape(n) + r"\b", low)), None)
            if hit:
                req["location"] = hit
                break

    return req


def attach_matches(result: dict, property_context: dict = None, conversation_history: list = None,
                   message: str = None):
    """
    Add tenant property matches only after the search is properly qualified.

    A general tenant search must have these four core details before cards are
    shown:

    - rent or buy
    - location / town / area
    - bedrooms
    - budget

    Until those details are known, the assistant can keep asking one question
    at a time and the UI will not receive ``matches`` / ``search`` fields.
    """

    # A specific property is already being discussed, so do not start a new
    # general search underneath that conversation.
    if property_context:
        return

    # Owners are listing property, not searching for a home. Investors use the
    # separate MLS/investor flow in src/services/investors.py.
    if str(result.get("role") or "").lower() in ("owner", "investor"):
        return

    # Rebuild requirements from the customer's own messages. This is a safety
    # net for cases where the LLM accidentally drops a requirement that was
    # supplied on an earlier turn.
    said = [
        str(item.get("content") or "")
        for item in (conversation_history or [])
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    if message:
        said.append(str(message))

    try:
        requirements = backfill_requirements(result.get("requirements"), said)
    except Exception as e:
        print(f"Requirement backfill failed (non-fatal): {e}")
        requirements = result.get("requirements") or {}

    if not isinstance(requirements, dict):
        requirements = {}

    # Keep the recovered values in the result so later turns and other parts
    # of the app see the same requirement state.
    result["requirements"] = requirements

    # Normalise the core search fields using the same parsers as search().
    rent_or_buy = listing_type_from(requirements.get("rent_or_buy"))
    location = str(requirements.get("location") or "").strip()
    bedrooms = parse_int(requirements.get("bedrooms")) or bhk_in(requirements.get("property_type"))
    budget = parse_money(requirements.get("budget"))
    move_in_date = str(requirements.get("move_in_date") or "").strip()

    # Do not show property cards until ALL five core questions are answered:
    # rent/buy -> location -> bedrooms -> budget -> move-in date.
    search_ready = bool(
        rent_or_buy
        and location
        and bedrooms
        and budget
        and move_in_date
    )

    if not search_ready:
        # Defensive cleanup in case another path populated these fields before
        # this function ran. The frontend renders cards when matches exists.
        result.pop("matches", None)
        result.pop("search", None)
        return

    try:
        found = search(requirements)
    except Exception as e:
        print(f"Property search failed (non-fatal): {e}")
        return

    if found is None:
        return

    result["matches"] = found["matches"]
    result["search"] = {
        "criteria": found["criteria"],
        "total": found["total"],
        "relaxed": found.get("relaxed", False),
    }

    # Don't repeat a list the customer has already seen (same homes, same
    # order), e.g. while they ask questions about "the second one".
    previous = [p["id"] for p in last_shown_list(conversation_history)]
    current = [m["id"] for m in found["matches"]]

    if found["matches"] and previous == current:
        return

    last_reply = next(
        (
            str(item.get("content") or "")
            for item in reversed(conversation_history or [])
            if isinstance(item, dict) and item.get("role") == "assistant"
        ),
        "",
    )

    if not found["matches"] and "couldn't find a listing" in last_reply.lower():
        return

    reply = str(result.get("response") or "").rstrip()
    result["response"] = f"{reply}\n\n{matches_text(found)}" if reply else matches_text(found)


# ---------------------------------------------------------------------
# "I want to visit the 3 bedroom house for rent in Wilmington"
# ---------------------------------------------------------------------

def _norm(text) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _beds_said(text: str):
    m = BEDS_RE.search(text or "")
    if not m:
        return None
    v = m.group(1).lower()
    return WORD_NUMBERS.get(v) or (int(v) if v.isdigit() else None)


# Words owners sometimes type into the area field that aren't places.
NOT_PLACE_WORDS = {"for", "sale", "rent", "house", "home", "homes", "bedroom", "bedrooms", "bed", "beds", "bath",
                   "apartment", "flat", "villa", "bungalow", "townhouse", "condo", "near", "with", "this", "that",
                   "want", "visit", "view", "viewing", "the", "downtown", "center", "centre", "street"}


def find_mentioned(texts: list[str]):
    """The one live listing the customer is talking about, from their own
    words (newest message first), or None when it isn't clear.

    1. A listing's title quoted (the customer copied it from Homes / a card).
    2. Otherwise its town + (when said) bedrooms, rent/sale and type - but
       only when exactly one listing fits best, never a guess between two."""
    rows, _ = all_properties()
    rows = [p for p in rows if (p.get("status") or "active") == "active" and p.get("id")]
    for text in texts:
        t = _norm(text)
        if not t:
            continue
        titled = [p for p in rows if len(_norm(p.get("title"))) >= 8 and _norm(p.get("title")) in t]
        if titled:
            return max(titled, key=lambda p: len(_norm(p.get("title"))))

        words = set(location_tokens(text))
        beds = _beds_said(text)
        kind = "sale" if BUY_RE.search(text) else "rent" if RENT_RE.search(text) else None
        scored = []
        for p in rows:
            place = [w for w in location_tokens(" ".join(str(p.get(k) or "") for k in ("area", "city", "location")))
                     if w not in LOCATION_GENERIC and w not in NOT_PLACE_WORDS and len(w) >= 4]
            if not place or not any(w in words for w in place):
                continue
            score = 3
            if beds is not None:
                if p.get("bedrooms") != beds:
                    continue
                score += 2
            if kind:
                if p.get("listing_type") != kind:
                    continue
                score += 1
            ptype = str(p.get("property_type") or "").lower()
            if ptype and ptype in t:
                score += 1
            scored.append((score, p))
        if scored:
            scored.sort(key=lambda x: -x[0])
            if len(scored) == 1 or scored[0][0] > scored[1][0]:
                return scored[0][1]
            return None  # two homes fit equally - let the customer choose
    return None


def clear_cache():
    _cache.update(at=0.0, rows=None, source=None)
