"""
Fact-check the AI's reply before it reaches the customer.

The reply is compared with the real listing data the AI was given
(the selected property, or listings already shown in a search):

- money:     every ₹ amount must be a real rent / deposit / price, or an
             amount the customer said themselves (their budget)
- pets, parking: "allowed / available" vs "not allowed / no parking"
- furnishing: fully / semi / unfurnished
- amenities: pool, gym, lift ... claimed without being listed
- rooms:     "3BHK" claims about the selected home

Checks are deliberately conservative: a sentence that hedges ("needs to
be checked", "not listed", "doesn't have") is never flagged, so honest
replies pass and only confident wrong statements are caught.
"""

import re

from src.services import properties


HEDGES = re.compile(
    r"\b(check(?:ed|ing)?|confirm(?:ed|ing)?|verif(?:y|ied|ying)|not sure|unsure|don't know|"
    r"do not know|not listed|not mentioned|no information|not available in|need to ask|find out)\b",
    re.IGNORECASE,
)

NEGATION = re.compile(
    r"\b(no|not|isn't|aren't|doesn't|don't|without|unfortunately|cannot|can't|lacks?)\b|n't\b",
    re.IGNORECASE,
)

MONEY_RE = re.compile(
    r"(?:₹|\$|rs\.?|inr|usd)\s*(\d[\d,]*(?:\.\d+)?)\s*(k|lakhs?|lacs?|l|crores?|cr)?\b"
    r"|\b(\d[\d,]*(?:\.\d+)?)\s*(k|lakhs?|lacs?|crores?|cr)\b"
    r"|\b(\d{1,3}(?:,\d{2,3})+)\b",
    re.IGNORECASE,
)

UNITS = {"k": 1e3, "l": 1e5, "lac": 1e5, "lacs": 1e5, "lakh": 1e5, "lakhs": 1e5,
         "cr": 1e7, "crore": 1e7, "crores": 1e7}

AMENITIES = {
    "swimming pool": ["pool"],
    "gym": ["gym", "fitness"],
    "lift": ["lift", "elevator"],
    "power backup": ["power backup", "generator", "backup power"],
    "security": ["security guard", "24x7 security", "24/7 security", "cctv", "security staff"],
    "clubhouse": ["clubhouse", "club house"],
    "garden": ["garden"],
    "sea view": ["sea view", "beach view"],
    "gated community": ["gated"],
    "wifi": ["wifi", "wi-fi", "internet"],
    "play area": ["play area", "playground"],
}


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")) if s.strip()]


# A size, not a price: "1,906 sq ft", "2,052 square feet".
NOT_MONEY_AFTER = re.compile(r"^\s*(sq\.?\s*ft|sqft|square\s*(feet|foot)|ft²|acres?)", re.IGNORECASE)

# Bare amounts, used only for what the CUSTOMER said ("150000 dollars cash",
# "my budget is 2000"). Widening what is allowed can never create a false alarm.
LOOSE_MONEY_RE = re.compile(r"\b(\d{4,}(?:\.\d+)?)\b")


def amounts_in(text: str, loose: bool = False) -> list[float]:

    text = str(text or "")
    found = []

    if loose:
        for m in LOOSE_MONEY_RE.finditer(text):
            try:
                found.append(float(m.group(1)))
            except ValueError:
                pass

    for m in MONEY_RE.finditer(str(text or "")):
        if NOT_MONEY_AFTER.match(text[m.end():m.end() + 16]):
            continue
        number = m.group(1) or m.group(3) or m.group(5)
        unit = (m.group(2) or m.group(4) or "").lower()
        try:
            value = float(number.replace(",", "")) * UNITS.get(unit, 1)
        except ValueError:
            continue
        # Skip small bare numbers (times, counts); keep ₹ amounts and units.
        if value >= 1000:
            found.append(value)

    return found


def money_text(value: float) -> str:
    from src.prompts.system_prompt import CURRENCY
    return f"${value:,.0f}" if CURRENCY == "USD" else "₹" + properties.format_inr(value)


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1.0, 0.01 * max(a, b))


def mentions(sentence: str, words: list[str]) -> bool:
    """Whole-word match, so "carpet" isn't "pet"."""
    return any(re.search(rf"\b{re.escape(w)}", sentence, re.IGNORECASE) for w in words)


def claims_yes(sentence: str, words: list[str]) -> bool:
    return mentions(sentence, words) and not NEGATION.search(sentence) and not HEDGES.search(sentence)


def claims_no(sentence: str, words: list[str]) -> bool:
    return mentions(sentence, words) and bool(NEGATION.search(sentence)) and not HEDGES.search(sentence)


def listings_named(reply: str, listings: list[dict]) -> list[dict]:
    lower = reply.lower()
    return [l for l in listings if l.get("title") and l["title"].lower() in lower]


def check_reply(
    reply: str,
    property_context: dict = None,
    shown_listings: list = None,
    customer_messages: list = None,
    requirements: dict = None,
) -> list[dict]:
    """Return a list of problems: {"type", "claim", "truth"}. Empty = passed."""

    reply = str(reply or "")
    issues = []

    # Which listings could the reply be talking about?
    if property_context:
        relevant = [property_context]
        single = property_context
    else:
        relevant = list(shown_listings or [])
        named = listings_named(reply, relevant)
        single = named[0] if len(named) == 1 else (relevant[0] if len(relevant) == 1 else None)

    # ---------------- money ----------------
    allowed = []
    for listing in relevant:
        for key in ["rent", "deposit", "sale_price"]:
            if listing.get(key):
                allowed.append(float(listing[key]))
    for text in customer_messages or []:
        allowed.extend(amounts_in(text, loose=True))
    budget = properties.parse_money((requirements or {}).get("budget"))
    if budget:
        allowed.append(budget)

    for sentence in sentences(reply):
        if HEDGES.search(sentence):
            continue
        for value in amounts_in(sentence):
            if not any(close(value, a) for a in allowed):
                issues.append({
                    "type": "money",
                    "claim": sentence,
                    "truth": money_text(value) + " is not a price, deposit or budget in the data",
                })

    if not single:
        return issues

    # ---------------- pets / parking ----------------
    for field, words, label in [
        ("pets_allowed", ["pet"], "pets"),
        ("parking", ["parking", "car park"], "parking"),
    ]:
        truth = single.get(field)
        for sentence in sentences(reply):
            says_yes = claims_yes(sentence, words) and re.search(r"allowed|welcome|available|friendly|yes|has|includes|comes with|there is", sentence, re.I)
            says_no = claims_no(sentence, words)
            if says_yes and truth is not True:
                issues.append({"type": label, "claim": sentence,
                               "truth": f"{label}: " + ("not allowed/available" if truth is False else "not listed")})
            elif says_no and truth is not False:
                issues.append({"type": label, "claim": sentence,
                               "truth": f"{label}: " + ("allowed/available" if truth is True else "not listed")})

    # ---------------- furnishing ----------------
    furnished = str(single.get("furnished") or "").lower()
    for sentence in sentences(reply):
        s = sentence.lower()
        if HEDGES.search(s):
            continue
        said = "semi" if "semi" in s and "furnish" in s else "un" if "unfurnished" in s else "fully" if "fully furnished" in s or "fully-furnished" in s else None
        if said and said not in (furnished or "none"):
            issues.append({"type": "furnishing", "claim": sentence,
                           "truth": f"furnishing: {furnished or 'not listed'}"})

    # ---------------- amenities ----------------
    listed = " ".join(str(a) for a in (single.get("amenities") or [])).lower() + " " + str(single.get("description") or "").lower()
    for amenity, words in AMENITIES.items():
        if any(w in listed for w in words + [amenity]) or (amenity == "security" and "security" in listed):
            continue
        for sentence in sentences(reply):
            if claims_yes(sentence, words):
                issues.append({"type": "amenity", "claim": sentence,
                               "truth": f"{amenity} is not in the listing"})

    # ---------------- rooms (selected home only) ----------------
    if property_context and single.get("bedrooms"):
        wanted = properties.parse_int((requirements or {}).get("bedrooms"))
        for sentence in sentences(reply):
            for n in re.findall(r"\b(\d)\s*-?\s*bhk\b", sentence, re.I):
                n = int(n)
                if n != single["bedrooms"] and n != wanted and not NEGATION.search(sentence):
                    issues.append({"type": "rooms", "claim": sentence,
                                   "truth": f"this home is a {single['bedrooms']}BHK"})

    # One issue per sentence and type is enough.
    unique = {}
    for issue in issues:
        unique.setdefault((issue["type"], issue["claim"]), issue)

    return list(unique.values())


def correction_note(issues: list[dict]) -> str:
    lines = "\n".join(f"- You wrote: \"{i['claim']}\" but the data says: {i['truth']}." for i in issues[:5])
    return (
        "FACT CHECK FAILED on your previous draft:\n"
        f"{lines}\n"
        "Rewrite the reply using ONLY facts from PROPERTY CONTEXT / LISTINGS "
        "ALREADY SHOWN. If a fact isn't there, say it needs to be checked."
    )


SAFE_REPLY = (
    "I want to be sure I give you the right details, so let me check that "
    "with the team before I confirm."
)
