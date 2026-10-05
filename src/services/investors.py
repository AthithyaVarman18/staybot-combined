"""
Investors: people who want to BUY property to rent out or resell.

The AI collects their budget, financing, goal, areas and timeline during the
normal chat (see INVESTORS in src/prompts/system_prompt.py) and this module
saves that profile, scores it, and matches it against the MLS listings using
the same deal maths staff use by hand (src/services/deals.py).

Honesty rules: the AI never promises returns, the match list uses an assumed
rent (a percent of price, shown as an assumption) until staff type the real
expected rent, and every match is a projection labelled as such.
"""

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.services import db, deals

router = APIRouter(prefix="/investors", tags=["Investors"])

TIMELINE_MONTHS = {"now": 0, "3_months": 3, "6_months": 6, "12_months": 12, "unsure": None}
TIERS = [("hot", 75), ("warm", 50), ("nurture", 25)]

# The brief we need before showing homes. Until every one of these is known the
# chat keeps asking questions instead of dumping a list of listings.
BRIEF_CORE = ("cash_available", "financing", "goal", "areas", "timeline",
              "home_age_preference", "strategy", "risk_tolerance")


def brief_missing(profile: dict) -> list:
    """Which of the core questions are still unanswered."""
    return [key for key in BRIEF_CORE if not profile.get(key)]


def brief_ready(profile: dict) -> bool:
    return not brief_missing(profile)


# New Property Investor accounts get homes within two exchanges: the first
# reply asks for cash and area together, the second shows the list with the
# full numbers for each home. The rest of the brief (financing, goal,
# timeline...) is asked afterwards, while they look at real homes.
QUICK_BRIEF = ("cash_available", "areas")
SEARCH_HINTS = ("cash_available", "areas", "min_bedrooms", "property_type", "goal")


def user_turn_number(conversation_history: list) -> int:
    """Which customer message this is (1 = their first), counting the one being answered now."""
    return 1 + sum(1 for item in (conversation_history or [])
                   if isinstance(item, dict) and item.get("role") == "user")


def quick_ready(profile: dict, turn: int) -> bool:
    """Enough to show homes in fast mode: cash and area known, or this is at
    least their second message and we know something to search on (or their
    third, whatever they said)."""
    profile = profile or {}
    if all(profile.get(key) for key in QUICK_BRIEF):
        return True
    if turn >= 2 and any(profile.get(key) for key in SEARCH_HINTS):
        return True
    return turn >= 3


def listing_ready(profile: dict, fast: bool = False, turn: int = 1) -> bool:
    return brief_ready(profile) or (fast and quick_ready(profile, turn))


def fast_listing_note(profile: dict, conversation_history: list) -> str | None:
    """Briefing for a New Property Investor chat that hasn't been shown homes yet."""
    profile = profile or {}
    if profile.get("last_matched_at"):
        return None
    turn = user_turn_number(conversation_history)
    if quick_ready(profile, turn):
        return (
            "FAST LISTING (overrides the brief rule): the system WILL add real matching MLS homes, "
            "each with its full numbers (cash to buy, monthly cash flow, cap rate, cash-on-cash return, DSCR, "
            "break-even rent), underneath your reply this turn. Capture anything new they said in "
            "investor_profile, do NOT ask a qualifying question, do NOT describe or invent any home - just say "
            "in one short line that here are the best fits for them, and that they can compare the numbers "
            "and tap \"Enquire about this\" on the one they like."
        )
    return (
        "FAST LISTING (overrides the brief rule): this investor must see real homes by their "
        "second message. In THIS reply ask ONE combined question: roughly how much cash they can put in, "
        "and which town or area they want (bedrooms too, if they like). If this message already gives both "
        "cash and an area, do not ask anything - just say in one line that you are pulling the best fits "
        "with the full numbers for each."
    )


REPEAT_AFTER_MINUTES = 15      # a new chat later on gets the list again


def recently_matched(profile: dict) -> bool:
    """Did we already send this investor the list in this same conversation?"""
    when = profile.get("last_matched_at")
    if not when:
        return False
    try:
        sent_at = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - sent_at).total_seconds() < REPEAT_AFTER_MINUTES * 60


def match_signature(profile: dict) -> str:
    """What the matches depend on - used so we don't repeat the same list every turn."""
    parts = [str(profile.get("cash_available") or ""), str(profile.get("goal") or ""),
             str(profile.get("min_bedrooms") or ""), str(profile.get("management_preference") or ""),
             str(profile.get("home_age_preference") or ""),
             ",".join(sorted(a.lower() for a in (profile.get("areas") or [])))]
    return "|".join(parts)


# The chat activity log. (An older `investor_activity` table in this project
# belongs to a different feature and has a foreign key we cannot satisfy.)
ACTIVITY_TABLE = "investor_profile_activity"


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_investors.sql to use investors.")


def q(fn, *args, **kwargs):
    configured()
    try:
        return fn(*args, **kwargs)
    except requests.RequestException as error:
        text = getattr(getattr(error, "response", None), "text", "") or str(error)
        if "PGRST205" in text or "does not exist" in text:
            raise HTTPException(503, "Run supabase_investors.sql in Supabase to enable investors.")
        print(f"Investor database error: {text[:300]}")
        raise HTTPException(500, "The investor database request failed. Please try again.")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(investor_id, actor, action, details=None):
    try:
        db._post(ACTIVITY_TABLE, {"investor_id": str(investor_id), "actor": actor, "action": action, "details": details or {}})
    except Exception as error:
        print(f"Investor activity log failed: {error}")


# ---------------------------------------------------------------------
# Scoring: what makes an investor ready to buy
# ---------------------------------------------------------------------

def score_investor(profile: dict) -> dict:
    parts = {}
    cash = float(profile.get("cash_available") or 0)
    parts["budget"] = {"score": 30 if cash >= 100000 else 24 if cash >= 60000 else 16 if cash >= 30000 else 8 if cash > 0 else 0,
                       "reason": f"cash available {cash:,.0f}" if cash else "cash not known"}
    approved = profile.get("pre_approved")
    financing = profile.get("financing")
    parts["financing"] = {"score": 20 if approved == "yes" or financing == "cash" else 10 if financing == "mortgage" else 4,
                          "reason": "pre-approved or paying cash" if approved == "yes" or financing == "cash"
                          else "mortgage, not pre-approved yet" if financing == "mortgage" else "financing not known"}
    months = TIMELINE_MONTHS.get(profile.get("timeline"))
    parts["timeline"] = {"score": 20 if months == 0 else 16 if months == 3 else 10 if months == 6 else 5 if months == 12 else 3,
                         "reason": f"wants to buy: {(profile.get('timeline') or 'unsure').replace('_', ' ')}"}
    experience = profile.get("experience")
    parts["experience"] = {"score": 10 if experience == "owns_many" else 8 if experience == "owns_some" else 5 if experience == "first_time" else 3,
                           "reason": (experience or "experience not known").replace("_", " ")}
    clarity = sum(bool(profile.get(k)) for k in ("goal", "property_type", "min_bedrooms")) + bool(profile.get("areas"))
    parts["clarity"] = {"score": min(10, clarity * 3), "reason": f"{clarity} of 4 preferences known"}
    parts["contact"] = {"score": 10 if profile.get("whatsapp") else 5 if profile.get("email") else 0,
                        "reason": "WhatsApp on file" if profile.get("whatsapp") else "email only" if profile.get("email") else "no contact yet"}
    total = sum(p["score"] for p in parts.values())
    tier = next((name for name, floor in TIERS if total >= floor), "unqualified")
    return {"score": total, "tier": tier, "parts": parts}


# ---------------------------------------------------------------------
# Matching: which homes fit this investor's money
# ---------------------------------------------------------------------

def budget_ceiling(cash, assumptions):
    """Highest price this cash can buy, once down payment and closing costs are paid."""
    share = (assumptions.down_payment_percent + assumptions.closing_costs_percent) / 100
    return cash / share if cash and share else None


def area_match(row, areas):
    return any(a in (row.get("city") or "").lower() or a in (row.get("subdivision") or "").lower()
               or a in (row.get("neighborhood") or "").lower() or a in (row.get("postal_code") or "") for a in areas)


def match_listings(profile, limit=5, assumed_rent_percent=0.8, rent_overrides=None, exclude_session_id=None):
    """Homes that fit this investor: the MLS feed plus homes other investors
    listed for sale through Staybot (chat or My listings - see
    db.owner_sale_listings()). exclude_session_id leaves out the asking
    investor's own listings, so nobody is offered their own home."""
    assumptions = deals.saved_assumptions()
    if profile.get("management_preference") == "self":
        assumptions = assumptions.model_copy(update={"management_percent": 0})   # they manage it themselves
    cash = float(profile.get("cash_available") or 0)
    # A caller that already knows the price ceiling (e.g. the investor journey's
    # own `budget`, which is itself derived from cash - see
    # investor_journey.details_from_profile) passes it directly so we don't
    # run the down-payment math twice and undersize their real budget.
    ceiling = float(profile["price_ceiling"]) if profile.get("price_ceiling") else budget_ceiling(cash, assumptions)
    min_beds = profile.get("min_bedrooms")
    areas = [a.lower() for a in (profile.get("areas") or []) if a]

    params = {"select": "*", "status_label": "eq.active", "order": "list_price.asc", "limit": "300"}
    if ceiling:
        params["list_price"] = f"lte.{int(ceiling)}"
    all_rows = q(db._get, "mls_listings", params)
    # Homes investors listed for sale on Staybot sit in the properties table,
    # not the MLS feed - without this, an Existing Investor's listing only
    # ever reached tenants and never showed up for other investors.
    all_rows = list(all_rows or []) + db.owner_sale_listings(max_price=ceiling, exclude_session_id=exclude_session_id)
    all_rows.sort(key=lambda r: float(r.get("list_price") or 0))

    def build(rows_subset, require_affordable=True):
        out = []
        for row in rows_subset[:150]:
            price = float(row.get("list_price") or 0)
            if not price:
                continue
            rent = (rent_overrides or {}).get(row["list_number"]) or price * assumed_rent_percent / 100
            analysis = deals.analyse(price=price, rent=rent, taxes_annual=float(row.get("tax_annual") or 0),
                                     insurance_annual=assumptions.insurance_annual,
                                     hoa_month=deals.hoa_monthly(row.get("hoa_fee"), row.get("hoa_frequency")),
                                     rehab=0, assumptions=assumptions)
            needed = analysis["cash_needed"]["total"]
            affordable = not (cash and needed > cash)
            if require_affordable and not affordable:
                continue
            out.append({
                "listing": {k: row.get(k) for k in ("list_number", "street_address", "city", "postal_code", "bedrooms",
                                                     "bathrooms_full", "living_area", "year_built", "list_price", "tax_annual",
                                                     "hoa_fee", "hoa_frequency", "subdivision", "photo_url", "days_on_market",
                                                     "price_per_sqft", "neighborhood", "owner_listed")},
                "rent_used": deals.money(rent),
                "rent_source": "entered by staff" if (rent_overrides or {}).get(row["list_number"]) else f"assumed {assumed_rent_percent}% of price",
                "cash_needed": needed,
                "affordable": affordable,
                "shortfall": deals.money(needed - cash) if not affordable else None,
                "cash_flow_monthly": analysis["results"]["cash_flow_monthly"],
                "cap_rate_percent": analysis["results"]["cap_rate_percent"],
                "cash_on_cash_percent": analysis["results"]["cash_on_cash_percent"],
                "breakeven_rent": analysis["results"]["breakeven_rent"],
                "sensitivity": analysis["sensitivity"],
                "analysis": analysis,
            })
        return out

    # The investor's exact brief first, then relax one thing at a time - same area,
    # same budget before anything else changes: size, then price, then finally
    # other areas. "Right place, different size or price" always beats "right
    # size and price, wrong town" - the area they asked for comes first.
    tiers = [("exact", lambda r: (not areas or area_match(r, areas)) and (not min_beds or (r.get("bedrooms") or 0) >= min_beds))]
    if areas and min_beds:
        tiers.append(("same_area_other_sizes", lambda r: area_match(r, areas)))
    if areas:
        tiers.append(("other_areas_same_size", lambda r: not min_beds or (r.get("bedrooms") or 0) >= min_beds))
    tiers.append(("other_areas_other_sizes", lambda r: True))

    # Which tier is the last one that still keeps them in their requested
    # area, before the remaining tiers start looking elsewhere.
    last_same_area_tier = "same_area_other_sizes" if (areas and min_beds) else "exact" if areas else None

    used_tier, matches, considered = "exact", [], len(all_rows)
    for name, keep in tiers:
        subset = [r for r in all_rows if keep(r)]
        considered = len(subset)
        matches = build(subset)
        if matches:
            used_tier = name
            break
        if name == last_same_area_tier:
            # Nothing in their area fits the cash on hand, at any size. Before
            # jumping to another town, check whether the area has real
            # inventory that's simply priced above budget - "other rate
            # properties in the same place" - and show the closest of those
            # instead of silently widening the search to somewhere else.
            try:
                uncapped = q(db._get, "mls_listings", {"select": "*", "status_label": "eq.active",
                                                        "order": "list_price.asc", "limit": "200"})
            except Exception:
                uncapped = []
            uncapped = list(uncapped or []) + db.owner_sale_listings(exclude_session_id=exclude_session_id, limit=200)
            local = [r for r in uncapped if area_match(r, areas)]
            if min_beds:
                sized = [r for r in local if (r.get("bedrooms") or 0) >= min_beds]
                local = sized or local     # no size match in-area either: show the cheapest anyway
            over_budget = build(local, require_affordable=False)
            over_budget = [m for m in over_budget if not m["affordable"]]
            over_budget.sort(key=lambda m: m["shortfall"])       # closest to affordable first
            if over_budget:
                used_tier, matches = "same_area_over_budget", over_budget[:limit]
                break

    goal = profile.get("goal")
    if used_tier == "same_area_over_budget":
        pass    # already sorted by how close to affordable each one is - leave that order alone
    elif goal == "growth":
        matches.sort(key=lambda m: (-(m["listing"].get("year_built") or 0), m["cash_needed"]))
    else:
        matches.sort(key=lambda m: -(m["cash_on_cash_percent"] or -999))
    # New build vs older home: the kind they asked for first, the rest after
    # (stable, so the order above holds within each group).
    age = profile.get("home_age_preference")
    if age in ("new", "older"):
        cutoff = datetime.now(timezone.utc).year - NEW_BUILD_YEARS

        def wrong_age(m):
            built = m["listing"].get("year_built")
            return built is None or (built >= cutoff) != (age == "new")
        matches.sort(key=wrong_age)
    # Homes other investors listed on Staybot go first. Their numbers are
    # built from the asking price alone (no rent, taxes or size entered), so
    # ranked by ROI they always landed below the MLS homes and were cut off
    # by `limit` - the New Property Investor never saw them in the chat.
    matches.sort(key=lambda m: not m["listing"].get("owner_listed"))   # stable: keeps the order within each group
    neighborhood = neighborhood_snapshot(areas)

    # An "exact" match only needs ANY one of the investor's areas to hit -
    # areas accumulates across the whole conversation and is never cleared,
    # so a much earlier "Garner" can silently satisfy a brand new "Wilmington"
    # ask, and the reply reads as a clean "good news" match with no sign
    # that the specific place they just asked about has nothing at all.
    # Named here so matches_message() can say so honestly instead of
    # blending every area the investor has ever mentioned into one line.
    matched_cities = {str(m["listing"].get("city") or "").lower() for m in matches}
    unmatched_areas = [
        a for a in (profile.get("areas") or [])
        if a and not any(a.lower() in city or city in a.lower() for city in matched_cities)
    ] if used_tier == "exact" and len(areas) > 1 else []

    return {
        "budget_ceiling": deals.money(ceiling) if ceiling else None,
        "cash_available": deals.money(cash) if cash else None,
        "assumed_rent_percent": assumed_rent_percent,
        "assumptions": assumptions.model_dump(),
        "considered": considered,
        "matches": matches[:limit],
        "match_tier": used_tier,
        "relaxed": {"area": areas and used_tier in ("other_areas_same_size", "other_areas_other_sizes"),
                   "bedrooms": bool(min_beds) and used_tier in ("same_area_other_sizes", "other_areas_other_sizes"),
                   "budget": used_tier == "same_area_over_budget"},
        "alternative_note": alternative_note(used_tier, areas, min_beds, neighborhood),
        "unmatched_areas": unmatched_areas,
        "neighborhood": neighborhood,
        "note": "Rent is assumed as a percent of price unless staff entered one. Confirm rents, taxes and insurance before advising.",
    }


def alternative_note(tier, areas, min_beds, neighborhood=None):
    """What to tell the investor when we couldn't match their exact brief.
    Distinguishes "we have zero listings in that area at all" from "we have
    listings there but none fit the budget/size" - very different things to
    tell an investor, and conflating them reads as the bot ignoring the area
    they actually asked for."""
    area_label = areas[0].title() if areas else None
    no_inventory = bool(area_label) and neighborhood is not None and not neighborhood.get("active_listings")
    if tier == "exact":
        return None
    if tier == "same_area_other_sizes":
        return f"Here are the closest homes to {min_beds}+ beds in {area_label} within your budget - other bedroom counts in the same area."
    if tier == "same_area_over_budget":
        return (f"Here are the closest homes to your budget in {area_label} itself - each needs a little more cash "
                "than you have on hand right now, but they're the nearest fit in the area you asked for.")
    if tier == "other_areas_same_size":
        beds = f"{min_beds}+ bed " if min_beds else ""
        if no_inventory:
            return f"{area_label} doesn't have active listings in our MLS data right now, so here are {beds}homes in the other areas we cover."
        return f"Here are {beds}homes in the other areas we cover that fit your budget better than what's currently available in {area_label}."
    if tier == "other_areas_other_sizes":
        if no_inventory:
            return f"{area_label} doesn't have active listings in our MLS data right now, so here are some options in the other areas we cover."
        tail = f" in {area_label}" if area_label else ""
        return f"Here are some options in the other areas we cover that fit your budget better than what's currently available{tail}."
    return None


def neighborhood_snapshot(areas):
    """Investor-facing market facts for the requested area, from active MLS listings only:
    median price, price per sq ft, days on market, bedroom mix. Property/market facts only -
    never used to describe or steer anyone toward/away from an area (Fair Housing). Investor-only:
    do not reuse this for tenant-facing search."""
    if not areas:
        return None
    try:
        rows = q(db._get, "mls_listings", {"select": "city,subdivision,neighborhood,postal_code,list_price,"
                                                      "living_area,price_per_sqft,days_on_market,bedrooms",
                                            "status_label": "eq.active", "limit": "1000"})
    except Exception:
        return None
    local = [r for r in rows if area_match(r, areas)]
    if len(local) < 3:
        return {"area": areas[0], "active_listings": len(local),
                "note": "Too few active MLS listings in this area for a reliable snapshot."}
    prices = sorted(float(r["list_price"]) for r in local if r.get("list_price"))
    ppsf = sorted(float(r["price_per_sqft"]) for r in local if r.get("price_per_sqft"))
    dom = sorted(float(r["days_on_market"]) for r in local if r.get("days_on_market") is not None)
    beds = {}
    for r in local:
        b = r.get("bedrooms")
        if b:
            beds[b] = beds.get(b, 0) + 1
    return {
        "area": areas[0],
        "active_listings": len(local),
        "median_list_price": deals.money(prices[len(prices) // 2]) if prices else None,
        "median_price_per_sqft": round(ppsf[len(ppsf) // 2], 2) if ppsf else None,
        "median_days_on_market": int(dom[len(dom) // 2]) if dom else None,
        "bedroom_mix": dict(sorted(beds.items())),
        "source": "MLS active listings",
        "as_of": datetime.now(timezone.utc).date().isoformat(),
    }


# ---------------------------------------------------------------------
# From the AI chat
# ---------------------------------------------------------------------

STRATEGIES = ("long_term_rental", "short_term_rental", "fix_and_flip", "buy_and_hold", "unsure")
# Added (or, for risk_tolerance, allowed "unsure") by the latest
# supabase_investors.sql - a database without that yet still saves the rest.
NEWER_COLUMNS = ("home_age_preference", "strategy", "risk_tolerance")
# Homes built this recently count as "new" for home_age_preference.
NEW_BUILD_YEARS = 5


def write_profile(fn, *args):
    """db._post / db._patch on investor_profiles, retried without the
    NEWER_COLUMNS if supabase_investors.sql hasn't been re-run."""
    try:
        return fn(*args)
    except Exception as error:
        text = getattr(getattr(error, "response", None), "text", "") or str(error)
        body = args[1]
        skip = [c for c in NEWER_COLUMNS if c in text and c in body]
        if not skip:
            raise
        print(f"investor_profiles can't store {skip} yet - run the latest supabase_investors.sql")
        return fn(args[0], {k: v for k, v in body.items() if k not in skip}, *args[2:])


def clean_profile(raw: dict) -> dict:
    """Keep only values the AI is allowed to set, in the shapes the table accepts."""
    out = {}
    cash = raw.get("cash_available")
    if isinstance(cash, (int, float)) and 0 < cash < 1e9:
        out["cash_available"] = float(cash)
    elif isinstance(cash, str):
        digits = re.sub(r"[^\d.]", "", cash)
        if digits:
            try:
                out["cash_available"] = float(Decimal(digits))
            except Exception:
                pass
    for key, allowed in (("financing", {"cash", "mortgage", "unsure"}), ("pre_approved", {"yes", "no", "unsure"}),
                         ("goal", {"income", "growth", "both"}), ("timeline", set(TIMELINE_MONTHS)),
                         ("experience", {"first_time", "owns_some", "owns_many"}),
                         ("management_preference", {"self", "company", "unsure"}),
                         ("condition_preference", {"turnkey", "light_work", "heavy_work", "unsure"}),
                         ("ownership", {"personal", "company", "unsure"}),
                         ("risk_tolerance", {"low", "moderate", "high", "unsure"}),
                         ("home_age_preference", {"new", "older", "any"}),
                         ("strategy", set(STRATEGIES))):
        value = str(raw.get(key) or "").strip().lower().replace(" ", "_")
        if value in allowed:
            out[key] = value
    if isinstance(raw.get("areas"), list):
        areas = [" ".join(str(a).split())[:80] for a in raw["areas"] if str(a).strip()][:8]
        if areas:
            out["areas"] = areas
    if raw.get("property_type"):
        out["property_type"] = str(raw["property_type"])[:80]
    beds = raw.get("min_bedrooms")
    if isinstance(beds, (int, float)) and 0 < beds <= 20:
        out["min_bedrooms"] = int(beds)
    for key, top in (("target_cash_flow", 100000), ("max_loan", 1e9), ("hold_years", 60)):
        value = raw.get(key)
        if isinstance(value, str):
            digits = re.sub(r"[^\d.]", "", value)
            value = float(digits) if digits else None
        if isinstance(value, (int, float)) and 0 < value <= top:
            out[key] = int(value) if key == "hold_years" else float(value)
    if raw.get("notes"):
        out["notes"] = str(raw["notes"])[:1000]
    return out


LABELS = {"cash_available": "cash available", "financing": "financing", "pre_approved": "pre-approved",
          "goal": "goal", "areas": "areas", "min_bedrooms": "minimum bedrooms", "timeline": "timeline",
          "experience": "experience", "management_preference": "management", "condition_preference": "condition",
          "target_cash_flow": "target monthly cash flow", "hold_years": "years they will hold",
          "ownership": "buying as", "max_loan": "pre-approved loan", "property_type": "property type",
          "risk_tolerance": "risk level", "home_age_preference": "new or older home", "strategy": "strategy"}


def find_known_profile(conversation_id=None, known_phone=None):
    """This investor's saved profile, looked up by conversation (web chat)
    or WhatsApp number - shared by known_brief_note and
    mentioned_listing_note so chat.py only has to fetch it once."""
    if not db.ENABLED:
        return None
    try:
        rows = []
        if conversation_id:
            rows = db._get("investor_profiles", {"conversation_id": f"eq.{conversation_id}", "select": "*"})
        if not rows and known_phone:
            rows = db._get("investor_profiles", {"whatsapp": f"eq.{known_phone}", "select": "*"})
        return rows[0] if rows else None
    except Exception as error:
        print(f"Reading the investor profile failed: {error}")
        return None


def known_brief_note(profile: dict = None):
    """A line for the AI listing what this investor has ALREADY told us, so a
    returning customer is never asked the same questions again."""
    try:
        if not profile:
            return None
        known = []
        for key, label in LABELS.items():
            value = profile.get(key)
            if value in (None, "", []):
                continue
            if key == "cash_available":
                value = f"${float(value):,.0f}"
            elif key == "areas":
                value = ", ".join(value)
            known.append(f"{label}: {str(value).replace('_', ' ')}")
        if not known:
            return None
        note = ("This investor has already told us: " + "; ".join(known) +
                ". Do NOT ask about any of these again - use them, and ask at most one thing that is still missing "
                "(and only if you have not already asked it earlier in this conversation)")
        missing = brief_missing(profile)
        note += (" (still missing: " + ", ".join(LABELS[k] for k in missing) + ")." if missing
                 else "; their brief is complete, so do not re-qualify them.")
        return note
    except Exception as error:
        print(f"Reading the investor brief failed: {error}")
        return None


# The line the chat sends after "Enquire about this" is confirmed (index.html: submitEnquiry()).
ENQUIRY_SENT_RE = re.compile(r"\bsent (an|my|the) enquiry\b", re.IGNORECASE)


def mentioned_listing_note(message: str, conversation_history: list, profile: dict) -> str | None:
    """When this message names one of the homes already shown, give the AI
    the real computed numbers for it (same maths as match_listings()), so
    it states them directly instead of saying "a team member will send
    the numbers over" for numbers the system already has right now."""

    if not db.ENABLED:
        return None

    address = next(
        (a for a in shown_addresses(conversation_history) if a.lower() in str(message or "").lower()),
        None,
    )
    if not address:
        return None

    try:
        rows = db._get("mls_listings", {
            "street_address": f"eq.{address}", "status_label": "eq.active", "select": "*", "limit": "1",
        })
        if not rows:
            return None
        row = rows[0]
        price = float(row.get("list_price") or 0)
        if not price:
            return None

        assumptions = deals.saved_assumptions()
        if (profile or {}).get("management_preference") == "self":
            assumptions = assumptions.model_copy(update={"management_percent": 0})

        rent = price * 0.8 / 100    # same default as match_listings()'s assumed_rent_percent
        analysis = deals.analyse(
            price=price, rent=rent, taxes_annual=float(row.get("tax_annual") or 0),
            insurance_annual=assumptions.insurance_annual,
            hoa_month=deals.hoa_monthly(row.get("hoa_fee"), row.get("hoa_frequency")),
            rehab=0, assumptions=assumptions,
        )
        match = {
            "listing": row,
            "rent_used": deals.money(rent),
            "cash_needed": analysis["cash_needed"]["total"],
            "cash_flow_monthly": analysis["results"]["cash_flow_monthly"],
            "breakeven_rent": analysis["results"]["breakeven_rent"],
        }

        return (
            f"The customer is asking about {row.get('street_address')}, {row.get('city')} - one of the "
            "homes already shown. These are the real numbers for it, computed the same way as every "
            f"match:\n{detail_lines(match)}\n"
            "State these plainly in your reply, labelled as a projection (not a promise, the team "
            "confirms the real rent). Do NOT say a team member will send the numbers over - they're "
            "already right here, so answer with them now."
            + (" They have just sent an enquiry for this home from the chat, and it has been saved for the "
               "team: confirm that in one line (the team will contact them about it), then recap the key "
               "numbers and ask one useful next question (e.g. financing, or whether they'd like us to "
               "manage it)." if ENQUIRY_SENT_RE.search(str(message or "")) else "")
        )

    except Exception as error:
        print(f"Building mentioned-listing note failed (non-fatal): {error}")
        return None


def repeat_match_note(profile: dict, message: str = None, fast: bool = False, turn: int = 1) -> str | None:
    """When this investor's brief is already complete and matches_for_chat()
    is about to suppress the list because we showed them the exact same
    search (cash/goal/area/bedrooms unchanged) within the repeat window -
    tell the AI BEFORE it replies, not after. The system prompt tells the AI
    to say "I'm pulling the best fits for them right now" the moment the
    brief is complete, with no way for it to know matches_for_chat() (called
    later, in chat.py's persistence block) is about to return nothing this
    turn - so that promise was left hanging with no list ever following it,
    every time a customer re-sent a similar message inside the repeat
    window. See REPEAT_AFTER_MINUTES / recently_matched().

    Skipped when the customer directly asked to see the list again
    (wants_matches_again) - matches_for_chat() honours that same request by
    bypassing the suppression, so a fresh list IS coming this turn."""

    if not profile or not listing_ready(profile, fast, turn) or wants_matches_again(message):
        return None

    signature = match_signature(profile)

    if profile.get("last_match_signature") != signature or not recently_matched(profile):
        return None

    return (
        "You already showed this investor matching homes recently for this exact same brief "
        "(cash, goal, area and bedrooms all unchanged) - the system will NOT add a fresh list below "
        "this turn. Do NOT say you are pulling, finding or searching for homes - nothing will follow "
        "that promise. Instead answer whatever they actually asked, or if they seem to want to see "
        "the homes again, say so plainly and that the team can resend the list."
    )


def handle_investor(result: dict, conversation_id=None, known_phone=None, known_name=None):
    """Save/update the investor profile from this turn's analysis (called by chat.py)."""
    if not db.ENABLED or result.get("role") != "investor":
        return None
    profile = clean_profile(result.get("investor_profile") or {})
    if not profile and not conversation_id:
        return None
    try:
        existing = None
        if conversation_id:
            rows = db._get("investor_profiles", {"conversation_id": f"eq.{conversation_id}", "select": "*"})
            existing = rows[0] if rows else None
        if not existing and known_phone:
            rows = db._get("investor_profiles", {"whatsapp": f"eq.{known_phone}", "select": "*"})
            existing = rows[0] if rows else None
        merged = {**(existing or {}), **profile}
        merged.setdefault("full_name", known_name or (existing or {}).get("full_name") or "Investor (name not given yet)")
        if known_phone:
            merged["whatsapp"] = known_phone
        scored = score_investor(merged)
        fields = {**profile, "score": scored["score"], "tier": scored["tier"], "score_parts": scored["parts"]}
        if brief_ready(merged) and (merged.get("status") or "new") == "new":
            fields["status"] = "qualified"      # every core question answered
        if existing:
            if known_phone and not existing.get("whatsapp"):
                fields["whatsapp"] = known_phone
            write_profile(db._patch, "investor_profiles", fields, {"id": f"eq.{existing['id']}"})
            investor_id = existing["id"]
            action = "profile_updated"
        else:
            row = write_profile(db._post, "investor_profiles", {**fields, "full_name": merged["full_name"], "whatsapp": known_phone,
                                                 "conversation_id": conversation_id, "source": "chat", "created_by": "ai_chat"})
            investor_id = row["id"]
            action = "profile_created"
        log(investor_id, "ai_chat", action, {"fields": sorted(profile)})
        result["investor"] = {"id": investor_id, "score": scored["score"], "tier": scored["tier"],
                              "saved": sorted(profile), "still_missing": brief_missing(merged)}
        return investor_id
    except Exception as error:
        print(f"Saving investor profile failed: {error}")
        return None


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------

class InvestorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str = Field(min_length=1, max_length=200)
    whatsapp: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=200)
    cash_available: float | None = Field(default=None, ge=0, le=1e9)
    financing: Literal["cash", "mortgage", "unsure"] | None = None
    pre_approved: Literal["yes", "no", "unsure"] | None = None
    goal: Literal["income", "growth", "both"] | None = None
    areas: list[str] = Field(default_factory=list, max_length=8)
    property_type: str | None = Field(default=None, max_length=80)
    min_bedrooms: int | None = Field(default=None, ge=0, le=20)
    timeline: Literal["now", "3_months", "6_months", "12_months", "unsure"] | None = None
    experience: Literal["first_time", "owns_some", "owns_many"] | None = None
    management_preference: Literal["self", "company", "unsure"] | None = None
    condition_preference: Literal["turnkey", "light_work", "heavy_work", "unsure"] | None = None
    home_age_preference: Literal["new", "older", "any"] | None = None
    strategy: Literal["long_term_rental", "short_term_rental", "fix_and_flip", "buy_and_hold", "unsure"] | None = None
    risk_tolerance: Literal["low", "moderate", "high", "unsure"] | None = None
    ownership: Literal["personal", "company", "unsure"] | None = None
    target_cash_flow: float | None = Field(default=None, ge=0, le=100000)
    max_loan: float | None = Field(default=None, ge=0, le=1e9)
    hold_years: int | None = Field(default=None, ge=0, le=60)
    notes: str | None = Field(default=None, max_length=2000)
    status: Literal["new", "qualified", "advisory", "buying", "bought", "not_now"] | None = None


def normalise_phone(value):
    from src.services.onboarding_cases import normalize_whatsapp
    try:
        return normalize_whatsapp(value)
    except ValueError:
        raise HTTPException(400, "Enter a WhatsApp number with country code, e.g. +1 704 555 0101.")


@router.get("")
def list_investors(tier: str | None = None):
    configured()
    params = {"select": "*", "order": "score.desc,updated_at.desc", "limit": "300"}
    if tier:
        params["tier"] = f"eq.{tier}"
    rows = q(db._get, "investor_profiles", params)
    return {"investors": rows, "counts": {t: sum(r["tier"] == t for r in rows) for t in ("hot", "warm", "nurture", "unqualified")}}


def load(investor_id):
    rows = q(db._get, "investor_profiles", {"id": f"eq.{investor_id}", "select": "*"})
    if not rows:
        raise HTTPException(404, "Investor not found.")
    return rows[0]


@router.get("/{investor_id}")
def get_investor(investor_id: UUID):
    configured()
    return load(investor_id)


@router.post("")
def create_investor(body: InvestorIn, x_staybot_staff: str | None = Header(default=None)):
    configured()
    fields = body.model_dump(exclude_none=True)
    if body.whatsapp:
        fields["whatsapp"] = normalise_phone(body.whatsapp)
        same = q(db._get, "investor_profiles", {"whatsapp": f"eq.{fields['whatsapp']}", "select": "id"})
        if same:
            raise HTTPException(409, "An investor with this WhatsApp number already exists.")
    scored = score_investor(fields)
    row = q(db._post, "investor_profiles", {**fields, "score": scored["score"], "tier": scored["tier"],
                                            "score_parts": scored["parts"], "source": "staff",
                                            "created_by": f"staff:{(x_staybot_staff or 'unnamed').strip()[:100]}"})
    log(row["id"], f"staff:{(x_staybot_staff or 'unnamed').strip()[:100]}", "created_by_staff", {})
    return row


@router.patch("/{investor_id}")
def update_investor(investor_id: UUID, body: InvestorIn, x_staybot_staff: str | None = Header(default=None)):
    configured()
    current = load(investor_id)
    fields = body.model_dump(exclude_none=True)
    if body.whatsapp:
        fields["whatsapp"] = normalise_phone(body.whatsapp)
    merged = {**current, **fields}
    scored = score_investor(merged)
    fields.update(score=scored["score"], tier=scored["tier"], score_parts=scored["parts"])
    q(db._patch, "investor_profiles", fields, {"id": f"eq.{investor_id}"})
    log(investor_id, f"staff:{(x_staybot_staff or 'unnamed').strip()[:100]}", "updated_by_staff", {"fields": sorted(body.model_dump(exclude_none=True))})
    return load(investor_id)


@router.get("/{investor_id}/matches")
def matches(investor_id: UUID, limit: int = 5, assumed_rent_percent: float = 0.8):
    configured()
    investor = load(investor_id)
    if not investor.get("cash_available"):
        raise HTTPException(400, "Add how much cash the investor has before matching homes.")
    if not 0.1 <= assumed_rent_percent <= 5:
        raise HTTPException(400, "The assumed rent percent must be between 0.1 and 5.")
    return {"investor": {k: investor[k] for k in ("id", "full_name", "cash_available", "goal", "areas", "min_bedrooms", "tier")},
            **match_listings(investor, limit=limit, assumed_rent_percent=assumed_rent_percent)}


class SendMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    list_number: str
    monthly_rent: float | None = Field(default=None, gt=0, le=100000)
    message: str | None = Field(default=None, max_length=900)


@router.post("/{investor_id}/send-deal")
def send_deal(investor_id: UUID, body: SendMatch, x_staybot_staff: str | None = Header(default=None)):
    """Save the deal for this investor and send the one-page PDF on WhatsApp."""
    from src.services import deal_pdf, onboarding_whatsapp, whatsapp
    configured()
    actor = f"staff:{(x_staybot_staff or 'unnamed').strip()[:100]}"
    investor = load(investor_id)
    if not investor.get("whatsapp"):
        raise HTTPException(400, "Add the investor's WhatsApp number first.")
    listing = deals.listing_for(body.list_number)
    assumptions = deals.saved_assumptions()
    price = float(listing["list_price"])
    rent = body.monthly_rent or price * 0.8 / 100
    analysis = deals.analyse(price=price, rent=rent, taxes_annual=float(listing.get("tax_annual") or 0),
                             insurance_annual=assumptions.insurance_annual,
                             hoa_month=deals.hoa_monthly(listing.get("hoa_fee"), listing.get("hoa_frequency")),
                             rehab=0, assumptions=assumptions)
    analysis["listing"] = {k: listing.get(k) for k in ("list_number", "street_address", "city", "postal_code", "bedrooms",
                                                        "bathrooms_full", "living_area", "year_built", "list_price",
                                                        "tax_annual", "hoa_fee", "hoa_frequency", "subdivision")}
    analysis["sources"] = {"price": "MLS list price", "taxes_annual": "MLS tax amount", "hoa_monthly": "MLS HOA fee",
                           "insurance_annual": "team assumption",
                           "monthly_rent": "entered by staff" if body.monthly_rent else "assumed 0.8% of price - confirm"}
    deal = q(db._post, "investment_deals", {
        "list_number": body.list_number, "investor_id": str(investor_id), "label": listing.get("street_address") or body.list_number,
        "inputs": analysis["inputs"],
        "results": {k: analysis.get(k) for k in ("results", "cash_needed", "loan", "costs_monthly", "income", "flags", "listing", "sources")},
        "created_by": actor})
    pdf = deal_pdf.render({**deal, "inputs": analysis["inputs"],
                           "results": {k: analysis.get(k) for k in ("results", "cash_needed", "loan", "costs_monthly", "income", "flags", "listing", "sources")}})
    flow = analysis["results"]["cash_flow_monthly"]
    text = (body.message or "").strip() or (
        f"Hi {investor['full_name'].split()[0]}, here's a home that fits your budget: "
        f"{listing.get('street_address')}, {listing.get('city')} at ${price:,.0f}. "
        f"Cash needed about ${analysis['cash_needed']['total']:,.0f}, projected cash flow "
        f"{'+' if flow >= 0 else '-'}${abs(flow):,.0f} a month at the rent shown in the PDF. "
        "These are projections, not a promise. Want to see it?")
    filename = f"Deal-{body.list_number}.pdf"
    phone = investor["whatsapp"]
    if whatsapp.DRY_RUN:
        whatsapp.OUTBOX.append({"to": re.sub(r"\D", "", phone), "text": f"[document {filename}] {text}", "at": now_iso(),
                                "dry_run": True, "kind": "investor_deal"})
        sent = {"status": "accepted", "test_mode": True}
    elif onboarding_whatsapp.window_open(phone):
        try:
            media = onboarding_whatsapp.graph_post(f"{whatsapp.PHONE_NUMBER_ID}/media",
                                                   data={"messaging_product": "whatsapp", "type": "application/pdf"},
                                                   files={"file": (filename, pdf, "application/pdf")})
            onboarding_whatsapp.send_payload(phone, {"type": "document", "document": {"id": media["id"], "filename": filename, "caption": text[:1024]}})
            sent = {"status": "accepted", "test_mode": False}
        except Exception as error:
            log(investor_id, actor, "deal_send_failed", {"error": str(error)[:300]})
            raise HTTPException(502, f"WhatsApp did not accept the message: {str(error)[:200]}")
    else:
        raise HTTPException(409, "The investor has not messaged in the last 24 hours, so WhatsApp needs an approved template. "
                                 "Ask them to message first, or send the PDF another way.")
    log(investor_id, actor, "deal_sent", {"list_number": body.list_number, "deal_id": deal["id"], "test_mode": sent["test_mode"]})
    return {"sent": sent, "deal_id": deal["id"], "message": text, "investor": load(investor_id)}


@router.get("/{investor_id}/activity")
def activity(investor_id: UUID):
    configured()
    load(investor_id)
    return q(db._get, ACTIVITY_TABLE, {"investor_id": f"eq.{investor_id}", "select": "*", "order": "id.desc", "limit": "100"})



def brief_line(profile) -> str:
    """One line repeating back what they told us, so the list feels like an answer."""
    bits = []
    if profile.get("cash_available"):
        bits.append(f"${float(profile['cash_available']):,.0f} cash")
    if profile.get("areas"):
        bits.append(" / ".join(profile["areas"][:3]))
    if profile.get("min_bedrooms"):
        bits.append(f"{profile['min_bedrooms']}+ bed")
    if profile.get("goal"):
        bits.append({"income": "monthly income", "growth": "long-term growth", "both": "income and growth"}[profile["goal"]])
    return ", ".join(bits)



def neighborhood_recommendation(neighborhood) -> str | None:
    """The next best thing to offer once every fallback tier in
    match_listings() has come up empty - no home to show at any budget or
    size, anywhere nearby. Real numbers for the area itself, so the
    investor isn't just left with "nothing". Neutral, sourced market
    facts only - never a description of who lives there, never a steer
    toward or away from an area (Fair Housing). Zero listings is itself a
    real, sayable fact."""
    if not neighborhood:
        return None
    if not neighborhood.get("active_listings"):
        return f"For context, {neighborhood['area'].title()} has no active listings in our MLS data at all right now, at any price."
    if neighborhood["active_listings"] < 3:
        return f"For context on {neighborhood['area'].title()}: {neighborhood['note']}"
    bits = [f"{neighborhood['active_listings']} active listings", f"median ${neighborhood['median_list_price']:,.0f}"]
    if neighborhood.get("median_price_per_sqft"):
        bits.append(f"${neighborhood['median_price_per_sqft']:,.0f}/sq ft")
    if neighborhood.get("median_days_on_market") is not None:
        bits.append(f"median {neighborhood['median_days_on_market']} days on market")
    return f"For context, here's what {neighborhood['area'].title()} looks like right now: " + ", ".join(bits) + f" (MLS, as of {neighborhood['as_of']})."


def matches_message(profile, matches_data, limit=3, with_photos=True):
    """The short message that introduces the homes. The photos carry the
    detail, so this stays a plain-language intro - no raw MLS/market
    counts alongside actual matches (see neighborhood_recommendation,
    only used below when there are no matches at all to soften)."""
    matches = matches_data["matches"][:limit]
    if not matches:
        base = (f"We don't have a home in that area within about ${matches_data['budget_ceiling'] or 0:,.0f} just yet - "
                "our team can widen the search or look at other areas for you.")
        hood = neighborhood_recommendation(matches_data.get("neighborhood"))
        return base + (f"\n\n{hood}" if hood else "")
    lines = []
    note = matches_data.get("alternative_note")
    unmatched = matches_data.get("unmatched_areas") or []
    if note:
        lines.append(note)
        lines.append(f"{len(matches)} {'home' if len(matches) == 1 else 'homes'} instead, close to {brief_line(profile)}:")
    elif unmatched:
        matched_areas = [a for a in (profile.get("areas") or []) if a not in unmatched]
        where = " or ".join(matched_areas) if matched_areas else "elsewhere on your list"
        lines.append(f"We don't have anything in {' or '.join(unmatched)} just yet, but here's what's available in {where}:")
    else:
        lines.append(f"Good news - {len(matches)} {'home fits' if len(matches) == 1 else 'homes fit'} {brief_line(profile)}:")
    for m in matches:
        l = m["listing"]
        extra = f" (needs ${m['shortfall']:,.0f} more cash than you have now)" if m.get("affordable") is False else ""
        lines.append(f"• {l.get('street_address')}, {l.get('city')} - ${float(l['list_price']):,.0f}{extra}")
    if with_photos:
        lines.append("\nPhotos and the numbers for each one are coming right now.")
        return "\n".join(lines)
    # No pictures on this channel, so spell the numbers out here instead.
    lines.append("")
    for n, m in enumerate(matches, 1):
        lines.append(f"{n}. {detail_lines(m)}")
    lines.append("\n" + matches_followup(matches_data))
    return "\n".join(lines)


def detail_lines(match) -> str:
    """The numbers for one home, as a person would read them out."""
    l = match["listing"]
    size = f"{int(l['living_area']):,} sq ft" if l.get("living_area") else "size n/a"
    flow = match["cash_flow_monthly"]
    shortfall_line = (f"\nThis needs about ${match['shortfall']:,.0f} more cash than you have on hand right now."
                      if match.get("affordable") is False else "")
    return (f"{l.get('street_address')}, {l.get('city')} - ${float(l['list_price']):,.0f}\n"
            f"{l.get('bedrooms') or '?'} bed · {size} · built {l.get('year_built') or '?'}\n"
            f"Cash needed ~${match['cash_needed']:,.0f} · projected cash flow "
            f"{'+' if flow >= 0 else '-'}${abs(flow):,.0f}/month\n"
            f"(assumed rent ${match['rent_used']:,.0f}; break-even ${match['breakeven_rent']:,.0f})" + shortfall_line)


def matches_followup(matches_data) -> str:
    """The one line that comes after the pictures."""
    return (f"Those rents are assumed at {matches_data['assumed_rent_percent']}% of the price with our standard costs "
            "and the MLS taxes, so they are projections, not a promise - our team confirms the real rent before you decide. "
            "Which one would you like the full breakdown on?")


BULLET_LISTING_RE = re.compile(r"^\s*•\s+(.+?),\s*[^,]+?\s*-\s*\$", re.MULTILINE)


def shown_addresses(conversation_history: list) -> list[str]:
    """Street addresses from the most recent matches bullet list this
    investor was shown, so a later turn can tell "asking about one of
    these" apart from "give me a fresh list"."""

    for item in reversed(conversation_history or []):
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        found = BULLET_LISTING_RE.findall(str(item.get("content") or ""))
        if found:
            return [addr.strip() for addr in found if addr.strip()]

    return []


def mentions_shown_address(message: str, conversation_history: list) -> bool:
    """True when this message names one of the homes we already showed -
    they're asking about that specific home, not requesting a new list."""

    text = str(message or "").lower()

    return any(address.lower() in text for address in shown_addresses(conversation_history))


def portfolio_context_for_account(account_id):
    """The logged-in existing-investor account's own portfolio (owned
    properties, combined equity/cash flow), used to (a) fold a conservative
    estimate of their extra buying power into this search's price ceiling
    and (b) open the reply with "you already own N properties...". Lives
    here (not investor_journey.py) so it feeds match_listings() directly -
    keeping everything on the one path that actually renders cards in the
    chat UI, instead of the separate plain-text-only recommendation system
    in investor_journey.py (see the comment above its genuine-lead check).
    None for a tenant/new-investor account, an anonymous WhatsApp lead (no
    account_id at all), or an account whose investor_journey record isn't
    classified "existing" yet."""
    if not db.ENABLED or not account_id:
        return None
    try:
        accounts = db._get("accounts", {"id": f"eq.{account_id}", "select": "role,investor_id"})
        account = accounts[0] if accounts else None
        if not account or account.get("role") != "existing_investor" or not account.get("investor_id"):
            return None
        journey_rows = db._get("investors", {"id": f"eq.{account['investor_id']}", "select": "investor_type"})
        journey_investor = journey_rows[0] if journey_rows else None
        if not journey_investor or journey_investor.get("investor_type") != "existing":
            return None
        portfolio = db._get("investor_portfolio_properties", {"investor_id": f"eq.{account['investor_id']}", "select": "*"})
        if not portfolio:
            return None
        totals = deals.portfolio_totals(portfolio)
        equity = totals["totals"]["equity"] or 0
        return {
            "owned_properties": totals["totals"]["properties_total"],
            "owned_monthly_cash_flow": totals["totals"]["monthly_cash_flow"],
            "owned_equity": equity,
            # Same conservative rule of thumb as investor_journey.py's
            # USABLE_EQUITY_FRACTION: roughly half of raw equity being
            # realistically tappable via a cash-out refi, before real
            # qualifying/DTI checks. Never a promise - see portfolio_intro_line.
            "extra_buying_power": round(equity * 0.5, -3) if equity > 0 else 0,
        }
    except Exception as error:
        print(f"Portfolio context lookup failed (non-fatal): {error}")
        return None


def portfolio_intro_line(ctx: dict) -> str:
    n = ctx["owned_properties"]
    line = (f"You already own {n} propert{'y' if n == 1 else 'ies'}, "
            f"${ctx['owned_monthly_cash_flow']:,.0f}/month combined cash flow, "
            f"${ctx['owned_equity']:,.0f} combined equity.")
    if ctx.get("extra_buying_power"):
        line += (f" With roughly ${ctx['extra_buying_power']:,.0f} more potentially available from that equity "
                 "(a rough estimate, not a qualified number), your search below includes that - "
                 "our team runs the real refinance numbers before you commit to anything.")
    return line


REQUEST_MATCHES_RE = re.compile(
    r"\b(show|send|see|list|pull|resend|update)\b.{0,30}\b(propert(y|ies)|home(s)?|house(s)?|option(s)?|listing(s)?)\b"
    r"|\b(propert(y|ies)|home(s)?|listing(s)?)\b.{0,20}\b(again|please)\b",
    re.IGNORECASE,
)


def wants_matches_again(message: str) -> bool:
    """The customer directly asking to see the list, e.g. "show me the
    properties again" / "can you send the listings". An explicit ask like
    this should never wait out the repeat-suppression timer (see
    REPEAT_AFTER_MINUTES) - that timer is only meant to stop the SYSTEM
    from repeating the list unprompted, never to block it when they
    actually ask for it."""
    return bool(REQUEST_MATCHES_RE.search(str(message or "")))


def add_full_numbers(match, rent_percent):
    """Everything the staff deal screen shows (Investing > Analyse), attached to
    one chat match so the investor can compare homes without leaving the chat:
    price per sq ft against the area, watch-outs, and where each number came from."""
    listing = match["listing"]
    analysis = match["analysis"]
    context = deals.area_context(listing)
    if context:
        analysis["area"] = context
        analysis["flags"].append({"level": "info" if abs(context["difference_percent"]) < 10 else "warn",
                                  "text": "Price per sq ft: " + context["summary"]})
    owner_listed = bool(listing.get("owner_listed"))
    analysis["sources"] = {
        "price": "owner's asking price (listed on Staybot)" if owner_listed else "MLS list price",
        "taxes_annual": "not provided by owner - confirm" if owner_listed else "MLS tax amount",
        "hoa_monthly": "not provided by owner - confirm" if owner_listed else "MLS HOA fee",
        "insurance_annual": "team assumption",
        "monthly_rent": match.get("rent_source") or f"assumed {rent_percent}% of price",
    }
    match["dscr"] = analysis["results"]["dscr"]
    match["gross_yield_percent"] = analysis["results"]["gross_yield_percent"]
    return match


def matches_for_chat(investor_id, limit=3, message: str = None, conversation_history: list = None,
                     account_id: str = None, fast: bool = False, session_id: str = None):
    """Matches for an investor, or None when we don't know enough yet (or
    they're now asking about one specific home we already showed them -
    resending the whole list would bury the answer to their question).
    fast=True (New Property Investor accounts) lists homes by the second
    message instead of waiting for the whole brief - see quick_ready()."""
    try:
        if mentions_shown_address(message, conversation_history):
            return None
        rows = db._get("investor_profiles", {"id": f"eq.{investor_id}", "select": "*"})
        if not rows:
            return None
        profile = rows[0]
        if not listing_ready(profile, fast, user_turn_number(conversation_history)):
            return None                       # still collecting - keep the conversation going
        signature = match_signature(profile)
        # A home another investor lists after this chat's last list changes
        # the answer, so it must not be suppressed as "same brief, same list".
        try:
            owner_ids = sorted(r["list_number"] for r in db.owner_sale_listings(exclude_session_id=session_id))
        except Exception:
            owner_ids = []
        if owner_ids:
            signature = f"{signature}|owner:{','.join(owner_ids)}"[:1000]
        if (profile.get("last_match_signature") == signature and recently_matched(profile)
                and not wants_matches_again(message)):
            return None            # same brief, same chat - don't repeat the list every turn

        portfolio_ctx = portfolio_context_for_account(account_id)
        search_profile = profile
        if portfolio_ctx and portfolio_ctx.get("extra_buying_power") and not profile.get("price_ceiling"):
            assumptions = deals.saved_assumptions()
            if profile.get("management_preference") == "self":
                assumptions = assumptions.model_copy(update={"management_percent": 0})
            base_ceiling = budget_ceiling(float(profile.get("cash_available") or 0), assumptions) or 0
            search_profile = {**profile, "price_ceiling": base_ceiling + portfolio_ctx["extra_buying_power"]}

        data = match_listings(search_profile, limit=limit, exclude_session_id=session_id)
        for m in data["matches"]:
            try:
                add_full_numbers(m, data["assumed_rent_percent"])
            except Exception as error:        # extra context only - the core numbers are already there
                print(f"Adding full deal numbers failed (non-fatal): {error}")
        try:
            db._patch("investor_profiles", {"last_matched_at": now_iso(), "last_match_signature": signature},
                      {"id": f"eq.{investor_id}"})
        except Exception:                     # column missing: run supabase_investors.sql again
            db._patch("investor_profiles", {"last_matched_at": now_iso()}, {"id": f"eq.{investor_id}"})
        log(investor_id, "ai_chat", "matches_sent", {"count": len(data["matches"]), "signature": signature,
                                                     "list_numbers": [m["listing"]["list_number"] for m in data["matches"]]})
        photos = match_photos(data, limit)
        message_text = matches_message(profile, data, limit, with_photos=bool(photos))
        if portfolio_ctx:
            message_text = portfolio_intro_line(portfolio_ctx) + "\n\n" + message_text
        return {"profile": profile, "data": data, "photos": photos,
                "message": message_text,
                "followup": matches_followup(data) if photos else None}
    except Exception as error:
        print(f"Investor matching for chat failed: {error}")
        return None



def match_photos(matches_data, limit=3):
    """Photo + one-line caption for each match, so the chat can send pictures.
    Only listings that actually have an MLS photo are included."""
    photos = []
    for m in matches_data.get("matches", [])[:limit]:
        l = m["listing"]
        url = (l.get("photo_url") or "").strip()
        if not url.startswith("http"):
            continue
        photos.append({
            "url": url.replace("http://", "https://", 1),      # WhatsApp needs https
            "list_number": l["list_number"],
            "caption": detail_lines(m),
        })
    return photos
