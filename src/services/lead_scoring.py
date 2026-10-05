"""
AI Lead Score.

Nine component scores (0-100 each, every one with a plain-English reason)
are combined with fixed weights into one lead score, then a tier:

    Intent, Engagement, Property fit, Readiness, Sentiment,
    Response behaviour, Qualification completeness, Financial fit,
    Human verification
            -> AI Lead Score (0-100)
            -> hot / warm / nurture / unqualified

The AI only supplies the raw facts (requirements, signals, sentiment).
Every number here is calculated by code, so the same conversation always
gets the same score and each point can be explained.
"""

import re
import statistics
from datetime import date, datetime, timedelta

from src.services import properties
from src.services.real_estate_ai import (
    ACTION_SIGNALS, APP_TIMEZONE, NEGATIVE_SIGNALS, OWNER_INTENTS, has_value
)


WEIGHTS = {
    "intent": 20,
    "engagement": 10,
    "property_fit": 15,
    "readiness": 15,
    "sentiment": 5,
    "response_behavior": 10,
    "completeness": 10,
    "financial_fit": 10,
    "human_verification": 5,
}

LABELS = {
    "intent": "Intent",
    "engagement": "Engagement",
    "property_fit": "Property fit",
    "readiness": "Buying/moving readiness",
    "sentiment": "Sentiment",
    "response_behavior": "Response behaviour",
    "completeness": "Qualification completeness",
    "financial_fit": "Financial/requirement fit",
    "human_verification": "Human verification",
}

TIERS = [(75, "hot"), (50, "warm"), (25, "nurture"), (0, "unqualified")]

# Hard limits that no amount of other points can override.
CAP_JUST_BROWSING = 35       # at most nurture
CAP_UNREALISTIC = 45         # at most nurture
CAP_NOT_INTERESTED = 15      # unqualified
CAP_NOT_GENUINE = 10         # unqualified

HUMAN_VERIFICATION = {"verified": 100, "unverified": 50, "not_genuine": 0}

ACTION_LABELS = {label: points for _, points, label, _, _ in ACTION_SIGNALS}
NEGATIVE_LABELS = {label: points for _, points, label, _ in NEGATIVE_SIGNALS}

INTENT_BASE = {
    "application": 70, "schedule_viewing": 60, "negotiate": 55,
    "property_inquiry": 40, "property_search": 35, "rent_question": 40,
    "buy_question": 40, "property_requirements": 35, "general_question": 20,
    "list_property": 60, "advertise_property": 55, "update_property": 50,
    "property_question": 35,
}

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]


def clamp(value) -> int:
    return int(max(0, min(100, round(value))))


def part(score, reason) -> dict:
    return {"score": clamp(score), "reason": reason}


# ---------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------

def intent_part(result: dict) -> dict:

    intent = str(result.get("intent") or "").lower()
    reasons = [b.get("reason") for b in (result.get("qualification") or {}).get("score_breakdown") or []]

    score = INTENT_BASE.get(intent, 20)
    notes = [intent.replace("_", " ") or "unclear intent"]

    for reason in reasons:
        if reason in ACTION_LABELS:
            score += ACTION_LABELS[reason] * 1.5
            notes.append(reason.lower())
        elif reason in NEGATIVE_LABELS:
            score += NEGATIVE_LABELS[reason] * 1.2
            notes.append(reason.lower())

    return part(score, ", ".join(notes))


def engagement_part(message: str, history: list) -> dict:

    user_messages = [str(m.get("content") or "") for m in history if m.get("role") == "user"] + [message]
    turns = len(user_messages)

    score = {1: 25, 2: 45, 3: 60, 4: 70}.get(turns, 80)
    notes = [f"{turns} message{'s' if turns != 1 else ''}"]

    if any("?" in m for m in user_messages):
        score += 10
        notes.append("asks questions")

    if statistics.mean(len(m) for m in user_messages) >= 25:
        score += 10
        notes.append("detailed messages")

    return part(score, ", ".join(notes))


def listing_fit(req: dict, listing: dict) -> tuple[int, list]:
    """How well one listing fits the tenant's stated needs."""

    checks = []

    bedrooms = properties.parse_int(req.get("bedrooms"))
    if bedrooms and listing.get("bedrooms") is not None:
        diff = abs(listing["bedrooms"] - bedrooms)
        checks.append((100 if diff == 0 else 60 if diff == 1 else 0, "size"))

    budget = properties.parse_money(req.get("budget"))
    price = properties.price_of(listing)
    if budget and price:
        checks.append((100 if price <= budget else 60 if price <= budget * 1.1 else 0, "budget"))

    if req.get("location"):
        checks.append((100 if properties.location_matches(req["location"], listing) else 30, "area"))

    for need, field in [("pets", "pets_allowed"), ("parking", "parking")]:
        if properties.wants(req.get(need)):
            value = listing.get(field)
            checks.append((0 if value is False else 100 if value is True else 60, need))

    if not checks:
        return None, []

    return statistics.mean(s for s, _ in checks), [name for s, name in checks if s < 100]


def property_fit_part(result: dict, property_context: dict, is_owner: bool) -> dict:

    if is_owner:
        details = result.get("property_details") or {}
        fields = ["property_type", "location", "bedrooms", "deposit", "available_from",
                  "pets_allowed", "parking", "furnished", "amenities"]
        known = sum(1 for f in fields if has_value(details.get(f)))
        known += 1 if has_value(details.get("rent")) or has_value(details.get("sale_price")) else 0
        return part(100 * known / (len(fields) + 1), f"listing {known}/{len(fields) + 1} details complete")

    req = result.get("requirements") or {}

    if property_context:
        score, misses = listing_fit(req, property_context)
        if score is None:
            return part(50, "selected home, needs not known yet")
        return part(score, "selected home fits" if not misses else f"selected home misses: {', '.join(misses)}")

    if "matches" not in result:
        return part(40, "no search yet")

    matches = result.get("matches") or []

    if not matches:
        return part(15, "no listing matches their needs")

    best = matches[0]
    if best.get("exact"):
        return part(90, f"exact match found ({len(matches)} options)")

    return part(60, f"close match only ({', '.join(best.get('notes') or [])})")


def parse_move_date(text: str):
    """'next month' / 'immediately' / 'October' / '2026-10-01' -> date or None."""

    today = datetime.now(APP_TIMEZONE).date()
    t = str(text or "").lower()

    if re.search(r"immediate|asap|urgent|right away|this week|now\b", t):
        return today
    if "next week" in t:
        return today + timedelta(days=7)
    if "next month" in t:
        return today + timedelta(days=30)

    match = re.search(r"(\d+)\s*(day|week|month)", t)
    if match:
        n, unit = int(match.group(1)), match.group(2)
        return today + timedelta(days=n * {"day": 1, "week": 7, "month": 30}[unit])

    try:
        return date.fromisoformat(t.strip()[:10])
    except ValueError:
        pass

    for index, name in enumerate(MONTHS, start=1):
        if name in t or re.search(rf"\b{name[:3]}\b", t):
            year = today.year + (1 if index < today.month else 0)
            return date(year, index, 1)

    return None


def readiness_part(result: dict, viewing: dict, is_owner: bool, reasons: list) -> dict:

    today = datetime.now(APP_TIMEZONE).date()

    if is_owner:
        details = result.get("property_details") or {}
        when = parse_move_date(details.get("available_from"))
        score, note = (40, "availability not given")
        if when:
            days = (when - today).days
            score, note = (90, "available now") if days <= 14 else (70, "available within 2 months") if days <= 60 else (45, "available later")
        if has_value(details.get("owner_phone")):
            score += 10
            note += ", contact given"
        return part(score, note)

    req = result.get("requirements") or {}
    candidates = [(30, "move-in date unknown")]

    move = req.get("move_in_date")
    if move:
        if re.search(r"not sure|later|next year|no rush|someday", str(move).lower()):
            candidates.append((25, f"no fixed move date ({move})"))
        else:
            when = parse_move_date(move)
            if when:
                days = (when - today).days
                score = 95 if days <= 14 else 80 if days <= 45 else 60 if days <= 90 else 35
                candidates.append((score, f"moving {move}"))
            else:
                candidates.append((45, f"move date: {move}"))

    if viewing and viewing.get("saved"):
        candidates.append((85, "viewing requested"))
    elif (result.get("viewing_request") or {}).get("wants_viewing"):
        candidates.append((70, "wants to view"))

    if "Wants to apply / take it" in reasons:
        candidates.append((100, "wants to apply"))

    score, note = max(candidates, key=lambda c: c[0])
    return part(score, note)


def sentiment_part(result: dict) -> dict:
    sentiment = str(result.get("sentiment") or "neutral").lower()
    score = {"positive": 100, "neutral": 60, "mixed": 45, "negative": 25}.get(sentiment, 60)
    return part(score, sentiment)


def response_part(message_times: list) -> dict:
    """message_times: [(role, datetime)] in order, including the current message."""

    gaps = []
    last_bot = None

    for role, at in message_times or []:
        if role == "assistant":
            last_bot = at
        elif role == "user" and last_bot:
            gaps.append((at - last_bot).total_seconds())
            last_bot = None

    if not gaps:
        return part(50, "no reply history yet")

    typical = statistics.median(gaps)

    if typical <= 300:
        return part(100, "replies within minutes")
    if typical <= 3600:
        return part(75, "replies within an hour")
    if typical <= 86400:
        return part(45, "replies within a day")
    return part(20, "slow to reply")


def completeness_part(result: dict, is_owner: bool) -> dict:

    if is_owner:
        d = result.get("property_details") or {}
        items = {
            "type": d.get("property_type") or d.get("bedrooms"),
            "area": d.get("location"),
            "bedrooms": d.get("bedrooms"),
            "price": d.get("rent") or d.get("sale_price"),
            "availability": d.get("available_from"),
            "phone": d.get("owner_phone"),
        }
    else:
        r = result.get("requirements") or {}
        items = {
            "area": r.get("location"),
            "size/type": r.get("bedrooms") or r.get("property_type"),
            "budget": r.get("budget"),
            "rent or buy": r.get("rent_or_buy"),
            "move date": r.get("move_in_date"),
        }

    known = [k for k, v in items.items() if has_value(v)]
    missing = [k for k in items if k not in known]
    note = f"{len(known)}/{len(items)} known" + (f", missing {', '.join(missing)}" if missing else "")

    return part(100 * len(known) / len(items), note)


def comparable_prices(listing_type, bedrooms, location) -> list:

    rows, _ = properties.all_properties()

    def pick(use_location):
        return [
            properties.price_of(p) for p in rows
            if properties.price_of(p)
            and (not listing_type or p.get("listing_type") == listing_type)
            and (not bedrooms or p.get("bedrooms") == bedrooms)
            and (not (use_location and location) or properties.location_matches(location, p))
        ]

    return pick(True) or pick(False)


def financial_part(result: dict, is_owner: bool, flags_text: list) -> dict:

    if is_owner:
        d = result.get("property_details") or {}
        rent, sale = properties.parse_money(d.get("rent")), properties.parse_money(d.get("sale_price"))
        price = rent or sale
        if not price:
            return part(30, "no price yet")
        prices = comparable_prices("rent" if rent else "sale", properties.parse_int(d.get("bedrooms")), d.get("location"))
        if not prices:
            return part(60, "no similar homes to compare")
        median = statistics.median(prices)
        gap = abs(price - median) / median
        score = 100 if gap <= 0.15 else 70 if gap <= 0.30 else 40
        return part(score, f"asking {properties.format_inr(price)} vs typical {properties.format_inr(median)}")

    req = result.get("requirements") or {}
    budget = properties.parse_money(req.get("budget"))

    if not budget:
        return part(40, "no budget yet")

    listing_type = properties.listing_type_from(req.get("rent_or_buy"))
    if not listing_type:
        listing_type = "sale" if budget >= 500000 else "rent"

    prices = comparable_prices(listing_type, properties.parse_int(req.get("bedrooms")), req.get("location"))

    if not prices:
        return part(50, "no similar homes to compare")

    median, cheapest = statistics.median(prices), min(prices)

    if budget >= median:
        score, note = 100, "budget covers typical prices"
    elif budget >= cheapest:
        score, note = 75, "budget covers some homes"
    elif budget >= cheapest * 0.9:
        score, note = 45, "budget just below the cheapest match"
    else:
        score, note = 15, "budget well below market"

    if "Unrealistic requirements" in flags_text:
        score, note = min(score, 10), "unrealistic requirements"

    return part(score, f"{note} ({properties.format_inr(budget)} vs from {properties.format_inr(cheapest)})")


# ---------------------------------------------------------------------
# Combine
# ---------------------------------------------------------------------

def tier_for(score: int) -> str:
    return next(name for threshold, name in TIERS if score >= threshold)


def combine(components: dict, reasons: list = None, verification: str = "unverified") -> dict:
    """Weighted total, hard caps, and tier. Also used to re-score a lead
    when the team changes its human verification."""

    reasons = reasons or []

    total = sum(components[key]["score"] * weight for key, weight in WEIGHTS.items()) / sum(WEIGHTS.values())
    score = clamp(total)
    caps = []

    if "Just browsing" in reasons and score > CAP_JUST_BROWSING:
        score, caps = CAP_JUST_BROWSING, caps + ["just browsing"]
    if "Unrealistic requirements" in reasons and score > CAP_UNREALISTIC:
        score, caps = CAP_UNREALISTIC, caps + ["unrealistic requirements"]
    if "Not interested" in reasons and score > CAP_NOT_INTERESTED:
        score, caps = CAP_NOT_INTERESTED, caps + ["not interested"]
    if verification == "not_genuine" and score > CAP_NOT_GENUINE:
        score, caps = CAP_NOT_GENUINE, caps + ["marked not genuine"]

    return {
        "intent_score": score,          # kept under this name for the Leads table
        "lead_score": score,
        "lead_status": tier_for(score),
        "components": components,
        "weights": WEIGHTS,
        "caps_applied": caps,
        "reasons": reasons,
        "human_verification": verification,
    }


def score_lead(
    result: dict,
    message: str,
    history: list,
    property_context: dict = None,
    viewing: dict = None,
    message_times: list = None,
    verification: str = "unverified",
) -> dict:

    role = str(result.get("role") or "").lower()
    is_owner = role == "owner" or str(result.get("intent") or "") in OWNER_INTENTS
    reasons = [b.get("reason") for b in (result.get("qualification") or {}).get("score_breakdown") or []]
    verification = verification if verification in HUMAN_VERIFICATION else "unverified"

    components = {
        "intent": intent_part(result),
        "engagement": engagement_part(message, history or []),
        "property_fit": property_fit_part(result, property_context, is_owner),
        "readiness": readiness_part(result, viewing or result.get("viewing"), is_owner, reasons),
        "sentiment": sentiment_part(result),
        "response_behavior": response_part(message_times),
        "completeness": completeness_part(result, is_owner),
        "financial_fit": financial_part(result, is_owner, reasons),
        "human_verification": part(HUMAN_VERIFICATION[verification], verification.replace("_", " ")),
    }

    for key, value in components.items():
        value["label"] = LABELS[key]
        value["weight"] = WEIGHTS[key]

    return combine(components, reasons, verification)
