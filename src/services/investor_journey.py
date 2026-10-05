"""
Investor workflows: Existing Property Investor and New Property Investor.

1. When the AI classifies a chat turn as role "investor" (see
   src/prompts/system_prompt.py), handle_investor_turn() below saves or
   updates one row in the `investors` table from result["investor_details"],
   the same way owner_listings.py turns result["property_details"] into a
   listing draft.
2. Once existing_property_count is known, the investor is classified as
   "existing" (already owns investment property) or "new" (first-time) and
   assigned the matching journey - each a fixed, ordered list of stages
   (JOURNEYS below), taken from the product spec:

     Existing Property Investor: investor lead generation, contracting,
     investor onboarding, portfolio information collection, property search
     & investment analysis, property acquisition, tenant lead generation,
     tenant screening, tenant contracting, ongoing property management.

     New Property Investor: investor lead generation, contracting, investor
     onboarding, investment education, property identification, property &
     market analysis, offer preparation, mortgage assistance, property
     inspection, closing, post-closing property management.

3. The investor stays on "lead_generation" until every qualifying field is
   known, then auto-advances to "contracting". Every later stage move is a
   deliberate action - the AI or a staff member calling advance() - logged
   to investor_stage_history, because contracting, onboarding, acquisitions
   and tenant placement all involve steps (signatures, financing, closing)
   this app doesn't itself execute.
4. Every stage move and profile change is best-effort mirrored to HubSpot
   (see hubspot.sync_investor) so the CRM never needs manual re-entry.

Staff routes (GET/PATCH/advance/portfolio/activity) are team-only
(TeamAuthMiddleware, applied in main.py). handle_investor_turn() is called
from chat.py for both web and WhatsApp turns.
"""

import os
from datetime import date, datetime, timezone
from typing import Literal
from uuid import UUID

import requests
from fastapi import APIRouter, File, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.services import db
from src.services import deals
from src.services.properties import parse_int, parse_money
from src.services.real_estate_ai import has_value
from src.services.viewings import append_line, normalize_phone

router = APIRouter(prefix="/journeys", tags=["Investor journeys"])

# ---------------------------------------------------------------------
# Journeys
# ---------------------------------------------------------------------

# (stage key, title, what this stage covers - shown to staff and used to
# brief the AI so it doesn't re-ask things already settled).
EXISTING_INVESTOR_STAGES = [
    ("lead_generation", "Investor lead generation",
     "Qualify the lead: goals, budget, location, strategy, risk tolerance and timeline."),
    ("contracting", "Contracting",
     "Send and countersign the investor services / property management agreement."),
    ("onboarding", "Investor onboarding",
     "Collect KYC, payment details and set up the investor's account."),
    ("portfolio_collection", "Portfolio information collection",
     "Record every property the investor already owns: address, value, mortgage, rent, condition."),
    ("property_search_analysis", "Property search & investment analysis",
     "Search matching listings and run cash-flow, cap-rate and ROI analysis on candidates."),
    ("acquisition", "Property acquisition",
     "Support the offer, due diligence and closing on the next acquisition."),
    ("tenant_lead_generation", "Tenant lead generation",
     "Market the newly acquired unit and generate prospective tenant leads."),
    ("tenant_screening", "Tenant screening",
     "Screen applicants: credit, background, income and references."),
    ("tenant_contracting", "Tenant contracting",
     "Prepare and execute the lease with the selected tenant."),
    ("property_management", "Ongoing property management",
     "Rent collection, maintenance, inspections and lease renewals."),
]

NEW_INVESTOR_STAGES = [
    ("lead_generation", "Investor lead generation",
     "Qualify the lead: goals, budget, location, strategy, risk tolerance and timeline."),
    ("contracting", "Contracting",
     "Send and countersign the investor services agreement."),
    ("onboarding", "Investor onboarding",
     "Collect KYC, payment details and set up the investor's account."),
    ("investment_education", "Investment education",
     "Walk the investor through how real-estate investing works: financing types, cash flow, risk, glossary."),
    ("property_identification", "Property identification",
     "Identify candidate properties matching the investor's goals and budget."),
    ("property_market_analysis", "Property & market analysis",
     "Run comparables, rental-yield and appreciation analysis on shortlisted properties."),
    ("offer_preparation", "Offer preparation",
     "Prepare and submit a purchase offer."),
    ("mortgage_assistance", "Mortgage assistance",
     "Help the investor secure financing / mortgage pre-approval."),
    ("property_inspection", "Property inspection",
     "Schedule the inspection and review the report."),
    ("closing", "Closing",
     "Coordinate closing: title, funds and signatures."),
    ("post_closing_management", "Post-closing property management",
     "Ongoing rent, maintenance, inspections and lease renewals for the new owner."),
]

JOURNEYS = {
    "existing": {"key": "existing_investor", "label": "Existing Property Investor", "stages": EXISTING_INVESTOR_STAGES},
    "new": {"key": "new_investor", "label": "New Property Investor", "stages": NEW_INVESTOR_STAGES},
}
JOURNEY_KEY_TO_TYPE = {v["key"]: k for k, v in JOURNEYS.items()}

# Fields that must all be known before "lead_generation" auto-advances.
QUALIFYING_FIELDS = [
    "existing_property_count", "investment_goals", "budget", "location",
    "investment_strategy", "risk_tolerance", "timeline",
]
FIELD_LABELS = {
    "existing_property_count": "number of properties already owned", "investment_goals": "investment goals",
    "budget": "budget", "location": "target location", "investment_strategy": "investment strategy",
    "risk_tolerance": "risk tolerance", "timeline": "timeline", "financing_requirements": "financing plan",
}

RISK_LEVELS = ("low", "moderate", "high")
STATUSES = ("active", "paused", "converted", "lost")


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_investors.sql to use investor workflows.")


def q(fn, *args, **kwargs):
    configured()
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except requests.RequestException as error:
        text = getattr(getattr(error, "response", None), "text", "") or str(error)
        if "PGRST205" in text or "does not exist" in text:
            raise HTTPException(503, "Run supabase_investor_journeys.sql in Supabase to enable investor workflows.")
        print(f"Investor database error: {text[:300]}")
        raise HTTPException(500, "The investor database request failed. Please try again.")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def staff_actor(header_value: str | None) -> str:
    name = (header_value or "").strip()[:120]
    return f"staff:{name}" if name else "staff:unnamed"


def clean_text(value):
    text = str(value or "").strip()
    return text if text and text.lower() not in ("null", "none", "unknown", "n/a", "") else None


def canonical_risk(value):
    text = clean_text(value)
    if not text:
        return None
    low = text.lower()
    if "low" in low or "conservative" in low or "safe" in low:
        return "low"
    if "high" in low or "aggressive" in low:
        return "high"
    if "moderate" in low or "medium" in low or "balanced" in low:
        return "moderate"
    return None


def stages_for(journey_key: str | None):
    investor_type = JOURNEY_KEY_TO_TYPE.get(journey_key)
    return JOURNEYS[investor_type]["stages"] if investor_type else None


def stage_title(investor: dict) -> str:
    stages = stages_for(investor.get("journey"))
    if not stages:
        return investor.get("stage") or "lead_generation"
    return next((title for key, title, _ in stages if key == investor.get("stage")), investor.get("stage"))


def classify_type(merged: dict) -> str | None:
    """'existing' / 'new' once existing_property_count is known, else None
    (still being qualified)."""
    count = merged.get("existing_property_count")
    if count is None:
        return None
    return "existing" if count > 0 else "new"


# ---------------------------------------------------------------------
# AI-facing: normalize investor_details -> DB fields, and brief the AI
# on where this investor currently is (called from chat.py).
# ---------------------------------------------------------------------

def normalized_details(details: dict) -> dict:
    d = details or {}
    fields = {
        "investment_goals": clean_text(d.get("investment_goals")),
        "budget": parse_money(d.get("budget")),
        "location": clean_text(d.get("location")),
        "investment_strategy": clean_text(d.get("investment_strategy")),
        "risk_tolerance": canonical_risk(d.get("risk_tolerance")),
        "timeline": clean_text(d.get("timeline")),
        "existing_property_count": parse_int(d.get("existing_property_count")),
        "financing_requirements": clean_text(d.get("financing_requirements")),
    }
    return {key: value for key, value in fields.items() if value is not None}


# The chat asks ONE set of questions (see INVESTORS in
# src/prompts/system_prompt.py) and saves them as an investor_profile
# (src/services/investors.py). These maps turn that same brief into the
# journey's own fields, so the customer is never asked twice.
BUDGET_CURRENCY = (os.getenv("BUSINESS_CURRENCY") or "USD").strip().upper()   # matches the market in .env

GOAL_TO_STRATEGY = {"income": "rental_income", "growth": "appreciation", "both": "buy_and_hold"}
PROFILE_STRATEGY = {"long_term_rental": "rental_income", "short_term_rental": "short_term_rental",
                    "fix_and_flip": "fix_and_flip", "buy_and_hold": "buy_and_hold"}
GOAL_TO_WORDS = {"income": "monthly rental income", "growth": "long-term property value growth",
                 "both": "a mix of rental income and long-term growth"}
EXPERIENCE_TO_COUNT = {"first_time": 0, "owns_some": 1, "owns_many": 3}
TIMELINE_WORDS = {"now": "immediate", "3_months": "3_months", "6_months": "6_months",
                  "12_months": "1_year", "unsure": "exploring"}


def details_from_profile(profile: dict) -> dict:
    """investor_profile (what the chat collected) -> investor_details (journey fields)."""
    if not profile:
        return {}
    from src.services import investors as scoring

    details = {}
    goal = profile.get("goal")
    if goal:
        details["investment_goals"] = GOAL_TO_WORDS.get(goal, goal)
        details["investment_strategy"] = GOAL_TO_STRATEGY.get(goal)
    if profile.get("condition_preference") == "heavy_work":
        details["investment_strategy"] = "fix_and_flip"
    if profile.get("strategy") in PROFILE_STRATEGY:       # what they actually told us beats the guesses above
        details["investment_strategy"] = PROFILE_STRATEGY[profile["strategy"]]
    cash = profile.get("cash_available")
    if cash:
        # What they can actually buy with that cash, not the cash itself.
        try:
            ceiling = scoring.budget_ceiling(float(cash), scoring.deals.saved_assumptions())
        except Exception as error:          # saved assumptions unavailable: fall back to the cash
            print(f"Budget ceiling unavailable: {error}")
            ceiling = None
        details["budget"] = round(ceiling or float(cash))
    if profile.get("areas"):
        details["location"] = ", ".join(profile["areas"])
    if profile.get("timeline"):
        details["timeline"] = TIMELINE_WORDS.get(profile["timeline"], profile["timeline"])
    if profile.get("experience") in EXPERIENCE_TO_COUNT:
        details["existing_property_count"] = EXPERIENCE_TO_COUNT[profile["experience"]]
    if profile.get("risk_tolerance"):
        details["risk_tolerance"] = profile["risk_tolerance"]
    if profile.get("financing"):
        details["financing_requirements"] = {"unsure": "undecided"}.get(profile["financing"], profile["financing"])
    return details


def find_open(conversation_id: str = None, session_id: str = None):
    """The investor record already being built up in this conversation, if any."""
    if not db.ENABLED or not (conversation_id or session_id):
        return None
    params = {"status": "eq.active", "order": "created_at.desc", "limit": "1", "select": "*"}
    params["conversation_id"] = f"eq.{conversation_id}" if conversation_id else "is.null"
    if not conversation_id:
        params["session_id"] = f"eq.{session_id}"
    rows = db._get("investors", params)
    return rows[0] if rows else None


def stage_note(investor: dict | None) -> str | None:
    """Briefing injected into the AI's system prompt so it tailors its
    question(s) to where this investor actually is, instead of restarting
    the qualifying questions every turn."""

    if not investor:
        return None

    stages = stages_for(investor.get("journey"))

    if not stages:
        missing = [FIELD_LABELS[f] for f in QUALIFYING_FIELDS if not has_value(investor.get(f))]
        return (
            "This investor is still being qualified (not yet assigned a journey). "
            f"Still missing: {', '.join(missing)}." if missing else
            "This investor is still being qualified."
        )

    title, blurb = next(((t, b) for k, t, b in stages if k == investor.get("stage")), (investor.get("stage"), ""))
    label = JOURNEYS[investor["investor_type"]]["label"]
    lines = [f'{"Existing" if investor["investor_type"] == "existing" else "First-time"} investor, '
             f'on the "{label}" journey, currently at stage "{title}". {blurb}']

    if investor.get("stage") == "lead_generation":
        missing = [FIELD_LABELS[f] for f in QUALIFYING_FIELDS if not has_value(investor.get(f))]
        if missing:
            lines.append(f"Still missing: {', '.join(missing)}.")
    else:
        lines.append("Qualifying details are already collected - don't re-ask them; help with what this stage needs.")

    return " ".join(lines)


def sync_crm(investor: dict):
    """Best-effort HubSpot mirror, called after any staff or AI change."""
    try:
        from src.services import hubspot
        if hubspot.ENABLED and investor.get("phone"):
            hubspot.sync_investor(investor, stage_label=stage_title(investor))
            db._patch("investors", {"crm_synced_at": now_iso()}, {"id": f"eq.{investor['id']}"})
    except Exception as e:
        print(f"Investor CRM sync failed (non-fatal): {e}")


def advance(investor: dict, actor: str, note: str = None, target_stage: str = None) -> dict:
    """Move an investor to the next stage in their journey, or to
    target_stage directly. Logs the move; never silently loses history."""

    stages = stages_for(investor.get("journey"))
    if not stages:
        raise ValueError("This investor hasn't been assigned a journey yet (existing_property_count is unknown).")

    keys = [key for key, _, _ in stages]

    if target_stage:
        if target_stage not in keys:
            raise ValueError(f"'{target_stage}' is not a stage of this investor's journey.")
        new_stage, new_index = target_stage, keys.index(target_stage)
    else:
        current_index = keys.index(investor["stage"]) if investor.get("stage") in keys else 0
        if current_index >= len(keys) - 1:
            return investor  # already at the final stage
        new_index = current_index + 1
        new_stage = keys[new_index]

    from_stage = investor.get("stage")
    if new_stage == from_stage:
        return investor

    updated = db._patch("investors", {"stage": new_stage, "stage_index": new_index}, {"id": f"eq.{investor['id']}"})
    db._post("investor_stage_history", {"investor_id": investor["id"], "from_stage": from_stage,
                                        "to_stage": new_stage, "actor": actor, "note": note})
    db._post("investor_activity", {"investor_id": investor["id"], "actor": actor, "action": "stage_advanced",
                                   "details": {"from": from_stage, "to": new_stage, "note": note}})
    return (updated[0] if isinstance(updated, list) else updated) or {**investor, "stage": new_stage, "stage_index": new_index}


def handle_investor_turn(
    result: dict,
    conversation_id: str = None,
    session_id: str = None,
    known_phone: str = None,
    known_name: str = None,
    account_role: str = None,
):
    """Save or update the investor's profile from this turn's analysis, add
    result["investor"], and append a line to the reply on meaningful events
    (profile started, journey assigned, stage advanced). Best-effort: a
    tracking hiccup never breaks the chat reply the investor is waiting on."""

    if str(result.get("role") or "").lower() != "investor":
        return

    # "investor" itself belongs to the scoring/matching module
    # (src/services/investors.py); the journey keeps its own key.
    summary = {"tracked": False}
    result["investor_journey"] = summary

    if not (conversation_id or session_id) or not db.ENABLED:
        return

    fields = normalized_details(result.get("investor_details")
                                or details_from_profile(result.get("investor_profile")))
    name, phone = clean_text(known_name), normalize_phone(known_phone)

    try:
        existing = find_open(conversation_id, session_id)
    except Exception as e:
        print(f"Investor lookup failed: {e}")
        summary["error"] = "Investor tracking isn't available yet. Run supabase_investor_journeys.sql in Supabase."
        return

    if not existing and not fields and not name and not phone:
        return  # nothing to start a record with yet

    merged = {**(existing or {}), **fields}
    if name:
        merged["name"] = merged.get("name") or name
    if phone:
        merged["phone"] = merged.get("phone") or phone

    investor_type = classify_type(merged)
    just_classified = investor_type and (not existing or existing.get("investor_type") != investor_type)

    try:
        if existing:
            changes = {k: v for k, v in fields.items() if existing.get(k) != v}
            if name and existing.get("name") != name:
                changes["name"] = name
            if phone and existing.get("phone") != phone:
                changes["phone"] = phone
            if just_classified:
                changes["investor_type"] = investor_type
                changes["journey"] = JOURNEYS[investor_type]["key"]
            saved = db._patch("investors", changes, {"id": f"eq.{existing['id']}"}) if changes else existing
            saved = (saved[0] if isinstance(saved, list) else saved) or existing
            action = "updated" if changes else "unchanged"
        else:
            base = {**fields, "conversation_id": conversation_id, "session_id": session_id,
                    "stage": "lead_generation", "stage_index": 0, "budget_currency": BUDGET_CURRENCY}
            if name:
                base["name"] = name
            if phone:
                base["phone"] = phone
            if investor_type:
                base["investor_type"] = investor_type
                base["journey"] = JOURNEYS[investor_type]["key"]
            saved = db._post("investors", base)
            action = "created"
            if saved:
                db._post("investor_activity", {"investor_id": saved["id"], "actor": "ai:chat", "action": "profile_started", "details": {}})
    except Exception as e:
        print(f"Saving investor failed: {e}")
        summary["error"] = "Your details couldn't be saved yet. Run supabase_investor_journeys.sql in Supabase."
        return

    saved = saved or {}

    # Auto-advance out of lead_generation once the investor is classified
    # and every qualifying field is known.
    if saved.get("stage") == "lead_generation" and saved.get("journey") and all(has_value(saved.get(f)) for f in QUALIFYING_FIELDS):
        try:
            saved = advance(saved, actor="ai:auto", note="Qualifying details complete.")
        except ValueError:
            pass

    summary.update(tracked=True, action=action, id=saved.get("id"), investor_type=saved.get("investor_type"),
                    journey=saved.get("journey"), stage=saved.get("stage"), stage_title=stage_title(saved))

    if action == "created" and fields:
        # Only worth saying once they have actually told us something; a bare
        # "hi, I want to invest" should just get a normal reply.
        append_line(result, "📋 I've started your investor profile - our team will follow up to confirm next steps.")
    elif just_classified:
        append_line(result, f"📋 Based on what you've shared, you're on our {JOURNEYS[investor_type]['label']} track.")
    elif existing and saved.get("stage") != existing.get("stage"):
        append_line(result, f"📋 Moving you on to the next step: {stage_title(saved)}.")

    # Genuine lead (at or past property search & analysis): show new-property
    # matches. Fires the first time they arrive at that stage, and again
    # whenever what they're asking for changes (new budget/location/strategy)
    # or the repeat window has passed - so asking again later, or after
    # changing their criteria, gets a fresh list instead of silence, but a
    # turn that changes nothing never repeats the same list back to back.
    #
    # This path is plain text only (see recommendation_message()) and was
    # never wired to the chat's card UI - it duplicates/collides with
    # investors.py's matches_for_chat() below, which IS card-rendered. A
    # brief attempt to also fire this for any logged-in existing-investor
    # account (so portfolio-aware budgeting would show up more often)
    # regressed the chat to a plain-text wall with no "Enquire about this"
    # buttons - reverted. Portfolio-aware budgeting for account-based chat
    # now lives directly in investors.matches_for_chat() / match_listings()
    # instead, which keeps the card UI intact (see investors.py:
    # portfolio_context_for_account()).
    if (saved.get("journey") == "existing_investor" and (saved.get("stage_index") or 0) >= GENUINE_LEAD_STAGE_INDEX):
        signature = journey_match_signature(saved)
        if signature != (saved.get("last_recommendation_signature") or "") or not recently_recommended(saved):
            try:
                recommendation = recommend_new_property(saved)
                if recommendation:
                    result["investor_new_property_recommendations"] = recommendation
                    append_line(result, recommendation_message(saved, recommendation))
                    update = {"last_recommended_at": now_iso(), "last_recommendation_signature": signature}
                    db._patch("investors", update, {"id": f"eq.{saved['id']}"})
                    saved.update(update)
            except Exception as error:
                print(f"New-property recommendation in chat failed (non-fatal): {error}")

    if saved.get("id"):
        sync_crm(saved)


# ---------------------------------------------------------------------
# New-property recommendations for an Existing Property Investor, factoring
# in their own portfolio (src/services/investors.py's MLS matching, fed
# with this journey's data instead of a fresh chat brief).
# ---------------------------------------------------------------------

# Recording their own owned properties (the "portfolio_collection" stage) is
# not, by itself, a signal they want to buy again - it's just bookkeeping.
# Only once they've moved on to actually searching does showing new listings
# make sense, so this stage is the gate: an investor at or past it is a
# genuine lead for a next acquisition, not just someone updating their file.
GENUINE_LEAD_STAGE = "property_search_analysis"
_EXISTING_STAGE_KEYS = [key for key, _, _ in EXISTING_INVESTOR_STAGES]
GENUINE_LEAD_STAGE_INDEX = _EXISTING_STAGE_KEYS.index(GENUINE_LEAD_STAGE)

STRATEGY_TO_GOAL = {"rental_income": "income", "buy_and_hold": "both", "appreciation": "growth",
                    "fix_and_flip": "growth", "short_term_rental": "income", "commercial": "both"}

# Same cadence as the WhatsApp quick-match flow's repeat window
# (REPEAT_AFTER_MINUTES in src/services/investors.py), so an investor who
# keeps chatting isn't sent the identical list every single turn.
JOURNEY_REPEAT_AFTER_MINUTES = 45


def journey_match_signature(investor: dict) -> str:
    """What the recommendation depends on - resend only when this changes
    (or the repeat window has passed), same pattern as investors.match_signature."""
    return "|".join([str(investor.get("budget") or ""), (investor.get("location") or "").strip().lower(),
                     investor.get("investment_strategy") or ""])


def recently_recommended(investor: dict) -> bool:
    when = investor.get("last_recommended_at")
    if not when:
        return False
    try:
        sent_at = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - sent_at).total_seconds() < JOURNEY_REPEAT_AFTER_MINUTES * 60


# A cash-out refi typically goes up to ~70-75% loan-to-value; on top of an
# existing mortgage that already eats into that, roughly half of raw equity
# being realistically tappable is a conservative rule of thumb - never a
# real number (the team runs actual refinance/DTI numbers), always shown as
# an estimate that only pads the ceiling, never replaces their stated budget.
USABLE_EQUITY_FRACTION = 0.5


def recommend_new_property(investor: dict, force: bool = False) -> dict | None:
    """New-property matches for an Existing Property Investor, with their
    owned portfolio attached as context and factored into what they can
    likely afford (stated budget + a conservative slice of their existing
    equity). Normally only once their stage shows they're actually looking
    to buy again (see GENUINE_LEAD_STAGE above); force=True (a real logged-in
    existing-investor account chatting directly - see handle_investor_turn)
    skips that stage check, since asking to buy IS the genuine-lead signal
    at that point, not a CRM stage someone else has to advance first."""
    if investor.get("journey") != "existing_investor":
        return None
    if not force and (investor.get("stage_index") or 0) < GENUINE_LEAD_STAGE_INDEX:
        return None
    if not investor.get("budget"):
        return None       # nothing to size a search on yet

    from src.services import investors as scoring

    try:
        portfolio = q(db._get, "investor_portfolio_properties", {"investor_id": f"eq.{investor['id']}", "select": "*"})
        totals = deals.portfolio_totals(portfolio)
        owned_equity = totals["totals"]["equity"] or 0
        extra_buying_power = round(owned_equity * USABLE_EQUITY_FRACTION, -3) if owned_equity > 0 else 0
        areas = [a.strip() for a in (investor.get("location") or "").split(",") if a.strip()]
        profile = {"price_ceiling": investor["budget"] + extra_buying_power, "areas": areas,
                  "goal": STRATEGY_TO_GOAL.get(investor.get("investment_strategy"), "both")}
        data = scoring.match_listings(profile, limit=5)
    except Exception as error:
        print(f"New-property recommendation failed (non-fatal): {error}")
        return None

    owned_here = sum(1 for p in portfolio if p.get("relationship") in ("owned", "acquired")
                     and areas and any(a.lower() in (p.get("address") or "").lower() for a in areas))
    data["lead_status"] = "targeted"    # stage-gated (or account-genuine) lead, not idle portfolio browsing
    data["portfolio_context"] = {
        "owned_properties": totals["totals"]["properties_total"],
        "owned_monthly_cash_flow": totals["totals"]["monthly_cash_flow"],
        "owned_equity": totals["totals"]["equity"],
        "already_own_in_requested_area": owned_here,
        "stated_budget": investor["budget"],
        "estimated_extra_buying_power": extra_buying_power,
        "note": "Totals cover properties marked owned/acquired; see portfolio_totals for the full breakdown and assumptions.",
    }
    return data


def recommendation_message(investor: dict, data: dict) -> str:
    """Chat-ready text for recommend_new_property()'s result: the owned-portfolio
    context first, then the same match presentation the WhatsApp chat brief uses
    (src/services/investors.py), spelled out as text since this flow has no photo
    step of its own."""
    from src.services import investors as scoring

    ctx = data.get("portfolio_context") or {}
    lines = []
    if ctx.get("owned_properties"):
        n = ctx["owned_properties"]
        lines.append(f"You already own {n} propert{'y' if n == 1 else 'ies'}, "
                     f"${ctx['owned_monthly_cash_flow']:,.0f}/month combined cash flow, "
                     f"${ctx['owned_equity']:,.0f} combined equity"
                     + (f" - {ctx['already_own_in_requested_area']} of those already in this area."
                        if ctx.get("already_own_in_requested_area") else "."))
        if ctx.get("estimated_extra_buying_power"):
            lines.append(f"With roughly ${ctx['estimated_extra_buying_power']:,.0f} more potentially "
                         f"available from that equity (a rough estimate, not a qualified number), "
                         f"your effective budget is closer to "
                         f"${ctx['stated_budget'] + ctx['estimated_extra_buying_power']:,.0f} - "
                         "our team runs the real refinance numbers before you commit to anything.")
    profile_like = {"areas": [investor["location"]] if investor.get("location") else [],
                    "goal": {"income": "income", "growth": "growth"}.get(
                        STRATEGY_TO_GOAL.get(investor.get("investment_strategy"), "both"), "both")}
    lines.append("Here's what's available for your next property:")
    lines.append(scoring.matches_message(profile_like, data, limit=3, with_photos=False))
    return "\n\n".join(lines)


# ---------------------------------------------------------------------
# Self-service routes (logged-in investor accounts - src/services/accounts.py)
#
# Separate from the staff routes below: no team password, gated only by
# the account's own login, and scoped to exactly that account's investor
# row. This is what the "Your journey" panel in src/static/portal.html
# calls for New Property Investor / Existing Property Investor accounts.
# ---------------------------------------------------------------------

customer_router = APIRouter(prefix="/me", tags=["Investor self-service"])


def _account_investor(request: Request) -> dict:
    from src.services import accounts  # deferred: accounts.py also deferred-imports this module

    configured()
    account = accounts.current_account(request)
    if not account:
        raise HTTPException(401, "Not logged in.")
    if account["role"] not in ("new_investor", "existing_investor"):
        raise HTTPException(403, "This account isn't on an investor workflow.")

    investor = None
    if account.get("investor_id"):
        rows = q(db._get, "investors", {"id": f"eq.{account['investor_id']}", "select": "*"})
        investor = rows[0] if rows else None
    if not investor:
        investor = find_open(session_id=account["session_id"])
    if not investor:
        raise HTTPException(404, "Your investor profile hasn't been created yet - send a message in chat first.")
    return investor


@customer_router.get("/investor")
def my_investor(request: Request):
    # Homes this account bought from another investor through Staybot go in
    # its Portfolio (normally when the sale is agreed - this catches any
    # that were missed). Before the profile lookup, so a missing profile is
    # created by it rather than ending in a 404.
    from src.services import accounts, rentals  # deferred - both import a lot
    account = accounts.current_account(request)
    if account and account.get("role") in ("new_investor", "existing_investor") and db.ENABLED:
        rentals.sync_bought_homes(account)
        from src.services import listing_portfolio  # homes they list (My listings) -> Portfolio
        listing_portfolio.sync(account)
    investor = _account_investor(request)
    portfolio = q(db._get, "investor_portfolio_properties", {"investor_id": f"eq.{investor['id']}", "order": "created_at.asc", "select": "*"})
    stages = stages_for(investor.get("journey"))
    return {
        "investor": investor,
        "journey_label": JOURNEYS[investor["investor_type"]]["label"] if investor.get("investor_type") else None,
        "stages": [{"key": k, "title": t, "description": b} for k, t, b in (stages or [])],
        "current_stage": investor.get("stage"),
        "portfolio": portfolio,
        # Equity / monthly cash flow / gross yield per property, reusing the
        # exact deal-analysis maths (src/services/deals.py) instead of new
        # formulas - see deals.portfolio_totals() for what each number means
        # and which assumption it relies on.
        "portfolio_totals": deals.portfolio_totals(portfolio),
        "new_property_recommendations": recommend_new_property(investor),
    }


class MyPortfolioIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    address: str = Field(min_length=3, max_length=300)
    property_type: str | None = Field(default=None, max_length=100)
    bedrooms: int | None = Field(default=None, ge=0, le=50)
    bathrooms: int | None = Field(default=None, ge=0, le=50)
    estimated_value: float | None = Field(default=None, ge=0, le=1_000_000_000)
    purchase_price: float | None = Field(default=None, ge=0, le=1_000_000_000)
    outstanding_mortgage: float | None = Field(default=None, ge=0, le=1_000_000_000)
    monthly_rent: float | None = Field(default=None, ge=0, le=10_000_000)
    monthly_expenses: float | None = Field(default=None, ge=0, le=10_000_000)
    unit_label: str | None = Field(default=None, max_length=100)
    tenant_name: str | None = Field(default=None, max_length=200)
    lease_start_date: date | None = None
    lease_end_date: date | None = None
    condition_notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def lease_dates(self):
        if self.lease_start_date and self.lease_end_date and self.lease_end_date < self.lease_start_date:
            raise ValueError("The lease end date can't be before the lease start date.")
        return self


def _existing_investor(request: Request) -> dict:
    """Recording owned properties is for Existing Property Investor
    accounts only - a New Property Investor doesn't own one yet (the
    Portfolio tab's "Your properties" form is hidden for them too)."""
    from src.services import accounts  # deferred, same as _account_investor
    account = accounts.current_account(request)
    if account and account.get("role") == "new_investor":
        raise HTTPException(403, "Adding your own properties is for Existing Property Investor accounts.")
    return _account_investor(request)


@customer_router.post("/investor/portfolio")
def add_my_portfolio_property(body: MyPortfolioIn, request: Request):
    """Existing Property Investor accounts record the homes they own, one at
    a time, from the Portfolio tab's "Your properties" form (or the portal's
    side panel) - alongside doing it through the chat, or staff PATCHing it
    from the Investors dashboard tab."""
    investor = _existing_investor(request)
    row = q(db._post, "investor_portfolio_properties",
            {"investor_id": investor["id"], "relationship": "owned", **body.model_dump(exclude_none=True, mode="json")})
    q(db._post, "investor_activity", {"investor_id": investor["id"], "actor": "self:account",
                                      "action": "portfolio_property_added", "details": {"address": body.address}})
    _maybe_graduate(investor["id"], "owned", "self:account")
    return row


@customer_router.delete("/investor/portfolio/{property_row_id}")
def remove_my_portfolio_property(property_row_id: UUID, request: Request):
    """Remove one of your own properties (Portfolio tab). Only ever touches
    a row that belongs to this account's investor profile."""
    investor = _existing_investor(request)
    rows = q(db._get, "investor_portfolio_properties", {
        "id": f"eq.{property_row_id}", "investor_id": f"eq.{investor['id']}", "select": "id,address,property_id", "limit": "1"})
    if not rows:
        raise HTTPException(404, "That property isn't in your portfolio.")
    q(db._delete, "investor_portfolio_properties", {"id": f"eq.{property_row_id}", "investor_id": f"eq.{investor['id']}"})
    q(db._post, "investor_activity", {"investor_id": investor["id"], "actor": "self:account",
                                      "action": "portfolio_property_removed",
                                      # property_id: a listing-linked row stays removed (listing_portfolio.sync)
                                      "details": {"address": rows[0].get("address"), "property_id": rows[0].get("property_id")}})
    return {"removed": str(property_row_id)}


@customer_router.post("/investor/portfolio/csv")
async def upload_my_portfolio_csv(request: Request, file: UploadFile = File(...)):
    """One CSV with every property/unit the investor owns - see
    src/services/investor_portfolio_csv.py for the accepted column names.
    Each "new" row becomes its own investor_portfolio_properties row
    (relationship "owned" unless the file says otherwise), the same table
    the single-property form above and the Portfolio tab's totals both use -
    so uploaded properties show up with correct totals immediately, no
    separate code path. Rows missing an address are skipped and reported,
    nothing else is inferred or invented."""
    from src.services import investor_portfolio_csv

    investor = _existing_investor(request)
    data = await file.read()
    rows, mapping, unused = investor_portfolio_csv.parse_csv(data)

    fields = ("address", "unit_label", "property_type", "bedrooms", "bathrooms", "estimated_value",
              "purchase_price", "outstanding_mortgage", "monthly_rent", "monthly_expenses", "tenant_name",
              "lease_start_date", "lease_end_date", "relationship", "condition_notes")
    created = []
    for row in rows:
        if row["result"] != "new":
            continue
        body = {k: row[k] for k in fields if row.get(k) is not None}
        created.append(q(db._post, "investor_portfolio_properties", {"investor_id": investor["id"], **body}))

    q(db._post, "investor_activity", {"investor_id": investor["id"], "actor": "self:account",
                                      "action": "portfolio_csv_uploaded",
                                      "details": {"file_name": file.filename, "rows": len(rows), "created": len(created)}})

    if any((r or {}).get("relationship", "owned") == "owned" for r in created):
        _maybe_graduate(investor["id"], "owned", "self:account")

    return {
        "total_rows": len(rows), "created": len(created),
        "skipped": [{"line": r["line"], "reason": r["reason"]} for r in rows if r["result"] != "new"],
        "columns_matched": mapping, "columns_ignored": unused,
        "properties": created,
    }


@customer_router.get("/investor/alerts")
def my_alerts(request: Request):
    """This investor's own open alerts (lease-ending, stale-maintenance -
    see src/services/investor_alerts.py) for their Portfolio dashboard tab.
    Recomputes first so an alert shows up immediately after e.g. a CSV
    import adds a lease_end_date, rather than waiting for the next staff
    refresh or daily digest run."""
    from src.services import investor_alerts

    investor = _account_investor(request)
    investor_alerts.refresh_alerts()
    return {"alerts": investor_alerts.list_open_alerts(investor_id=investor["id"])}


class WhatsAppAlertsOptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opt_in: bool


@customer_router.post("/investor/whatsapp-alerts-opt-in")
def set_whatsapp_alerts_opt_in(body: WhatsAppAlertsOptIn, request: Request):
    """Turns the daily WhatsApp alerts digest on/off for this investor - see
    src/services/investor_alerts.py. Off by default; no opt-in, no message,
    however urgent the alert, per WhatsApp's own policy on this kind of
    proactive business-initiated messaging."""
    investor = _account_investor(request)
    fields = {"whatsapp_alerts_opt_in": body.opt_in}
    fields["whatsapp_alerts_opt_in_at"] = now_iso() if body.opt_in else None
    updated = q(db._patch, "investors", fields, {"id": f"eq.{investor['id']}"})
    q(db._post, "investor_activity", {"investor_id": investor["id"], "actor": "self:account",
                                      "action": "whatsapp_alerts_opt_in" if body.opt_in else "whatsapp_alerts_opt_out",
                                      "details": {}})
    return updated[0] if isinstance(updated, list) else updated


# ---------------------------------------------------------------------
# Staff routes
# ---------------------------------------------------------------------

@router.get("/config/journeys")
def list_journeys():
    return {key: {"key": j["key"], "label": j["label"],
                  "stages": [{"key": k, "title": t, "description": b} for k, t, b in j["stages"]]}
            for key, j in JOURNEYS.items()}


@router.get("")
def list_investors(journey: str | None = None, stage: str | None = None,
                    status: str | None = None, query: str | None = Query(default=None)):
    configured()
    params = {"order": "updated_at.desc", "limit": "500", "select": "*"}
    if journey:
        params["journey"] = f"eq.{journey}"
    if stage:
        params["stage"] = f"eq.{stage}"
    if status:
        params["status"] = f"eq.{status}"
    rows = q(db._get, "investors", params)
    if query:
        needle = query.strip().lower()
        rows = [r for r in rows if needle in " ".join(str(r.get(f) or "") for f in
                ("name", "phone", "email", "location", "investment_goals")).lower()]
    counts = {}
    for r in rows:
        counts[r.get("stage") or "lead_generation"] = counts.get(r.get("stage") or "lead_generation", 0) + 1
    return {"investors": rows, "counts": counts, "journeys": list_journeys()}


def load(investor_id: UUID) -> dict:
    rows = q(db._get, "investors", {"id": f"eq.{investor_id}", "select": "*"})
    if not rows:
        raise HTTPException(404, "Investor not found.")
    return rows[0]


@router.get("/{investor_id}")
def get_investor(investor_id: UUID):
    investor = load(investor_id)
    portfolio = q(db._get, "investor_portfolio_properties", {"investor_id": f"eq.{investor_id}", "order": "created_at.asc", "select": "*"})
    stage_history = q(db._get, "investor_stage_history", {"investor_id": f"eq.{investor_id}", "order": "id.desc", "select": "*"})
    activity = q(db._get, "investor_activity", {"investor_id": f"eq.{investor_id}", "order": "id.desc", "limit": "200", "select": "*"})
    return {**investor, "portfolio": portfolio, "portfolio_totals": deals.portfolio_totals(portfolio),
            "stage_history": stage_history, "activity": activity,
            "journey_label": JOURNEYS[investor["investor_type"]]["label"] if investor.get("investor_type") else None,
            "stage_title": stage_title(investor),
            "new_property_recommendations": recommend_new_property(investor)}


class InvestorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=200)
    investor_type: Literal["new", "existing"] | None = None
    investment_goals: str | None = Field(default=None, max_length=500)
    budget: float | None = None
    budget_currency: Literal["INR", "USD"] | None = None
    location: str | None = Field(default=None, max_length=200)
    investment_strategy: str | None = Field(default=None, max_length=200)
    risk_tolerance: Literal["low", "moderate", "high"] | None = None
    timeline: str | None = Field(default=None, max_length=100)
    existing_property_count: int | None = Field(default=None, ge=0)
    financing_requirements: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)
    assigned_staff: str | None = Field(default=None, max_length=200)
    status: Literal["active", "paused", "converted", "lost"] | None = None


@router.patch("/{investor_id}")
def update_investor(investor_id: UUID, body: InvestorUpdate, x_staybot_staff: str | None = Header(default=None)):
    investor = load(investor_id)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        return get_investor(investor_id)

    # A staff correction to investor_type reassigns the journey. If the
    # current stage isn't part of the new journey, restart at lead_generation
    # (its stages up to onboarding are identical, so nothing is really lost).
    if "investor_type" in changes and changes["investor_type"] != investor.get("investor_type"):
        new_journey = JOURNEYS[changes["investor_type"]]["key"]
        changes["journey"] = new_journey
        if investor.get("stage") not in [k for k, _, _ in stages_for(new_journey)]:
            changes["stage"], changes["stage_index"] = "lead_generation", 0

    q(db._patch, "investors", changes, {"id": f"eq.{investor_id}"})
    q(db._post, "investor_activity", {"investor_id": str(investor_id), "actor": staff_actor(x_staybot_staff),
                                      "action": "profile_updated", "details": changes})
    view = get_investor(investor_id)
    sync_crm(view)
    return view


class AdvanceStage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_stage: str | None = None
    note: str | None = Field(default=None, max_length=1000)


@router.post("/{investor_id}/advance")
def advance_investor(investor_id: UUID, body: AdvanceStage, x_staybot_staff: str | None = Header(default=None)):
    investor = load(investor_id)
    try:
        advance(investor, staff_actor(x_staybot_staff), note=body.note, target_stage=body.target_stage)
    except ValueError as error:
        raise HTTPException(400, str(error))
    view = get_investor(investor_id)
    sync_crm(view)
    return view


class ActivityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(min_length=2, max_length=80)
    details: str = Field(default="", max_length=2000)


@router.post("/{investor_id}/activity")
def log_activity(investor_id: UUID, body: ActivityIn, x_staybot_staff: str | None = Header(default=None)):
    load(investor_id)
    q(db._post, "investor_activity", {"investor_id": str(investor_id), "actor": staff_actor(x_staybot_staff),
                                      "action": body.action, "details": {"note": body.details} if body.details else {}})
    return get_investor(investor_id)


def graduate_to_existing_investor(investor_id, actor: str):
    """A property has just settled into "owned" for an investor still on
    the new-investor track - they're not a first-time buyer anymore.
    Reassigns them to the existing-investor journey, landing on "tenant
    lead generation" rather than back at square one (acquisition is done;
    marketing the new unit is the real next step) - and upgrades any
    linked logged-in account's role, so the chat, recommendations (see
    recommend_new_property(), which only runs the portfolio-aware
    equity/cash-flow analysis for existing investors) and every tab that
    reads accounts.role treat them as an existing investor from here on.

    Safe to call unconditionally after any portfolio write: a no-op for
    an investor who's already "existing" or not yet classified."""

    try:
        investor = load(investor_id)
    except HTTPException:
        return

    if investor.get("investor_type") != "new":
        # The investor record was already moved on, but a linked account
        # can still be on new_investor (e.g. an earlier upgrade failed).
        _upgrade_linked_accounts(investor_id)
        return

    existing_keys = [key for key, _, _ in EXISTING_INVESTOR_STAGES]
    landing_stage = "tenant_lead_generation" if "tenant_lead_generation" in existing_keys else existing_keys[0]

    # They own at least one property now. Left at 0, the next chat turn's
    # classify_type() would read them as "new" again and flip the journey
    # back (with a stage that isn't on the new-investor track).
    db._patch("investors", {
        "investor_type": "existing",
        "journey": JOURNEYS["existing"]["key"],
        "stage": landing_stage,
        "stage_index": existing_keys.index(landing_stage),
        "existing_property_count": max(investor.get("existing_property_count") or 0, 1),
    }, {"id": f"eq.{investor_id}"})

    db._post("investor_stage_history", {
        "investor_id": str(investor_id), "from_stage": investor.get("stage"), "to_stage": landing_stage,
        "actor": actor, "note": "Graduated to existing investor: a property reached \"owned\" in their portfolio.",
    })
    db._post("investor_activity", {
        "investor_id": str(investor_id), "actor": actor, "action": "graduated_to_existing_investor", "details": {},
    })

    _upgrade_linked_accounts(investor_id)


def _upgrade_linked_accounts(investor_id):
    """Move every New Property Investor account linked to this investor to
    existing_investor, and queue the one-time welcome page they see before
    their dashboard next time (/existing-investor-welcome). Never raises."""

    try:
        linked_accounts = db._get("accounts", {"investor_id": f"eq.{investor_id}", "select": "id,role"})
        for account in linked_accounts or []:
            if account.get("role") != "new_investor":
                continue
            db._patch("accounts", {"role": "existing_investor"}, {"id": f"eq.{account['id']}"})
            # Separate write: without supabase_existing_investor_welcome.sql
            # the column is missing - the role upgrade above must still stick.
            try:
                db._patch("accounts", {"existing_welcome_pending": True}, {"id": f"eq.{account['id']}"})
            except Exception as error:
                print(f"Queueing the existing-investor welcome failed (non-fatal): {error}")
    except Exception as error:
        print(f"Upgrading linked account role failed (non-fatal): {error}")


class PortfolioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relationship: Literal["owned", "target", "under_contract", "acquired"] = "owned"
    address: str | None = Field(default=None, max_length=300)
    property_type: str | None = Field(default=None, max_length=100)
    bedrooms: int | None = Field(default=None, ge=0)
    bathrooms: int | None = Field(default=None, ge=0)
    estimated_value: float | None = None
    purchase_price: float | None = None
    outstanding_mortgage: float | None = None
    monthly_rent: float | None = None
    monthly_expenses: float | None = None
    unit_label: str | None = Field(default=None, max_length=100)
    tenant_name: str | None = Field(default=None, max_length=200)
    lease_start_date: date | None = None
    lease_end_date: date | None = None
    condition_notes: str | None = Field(default=None, max_length=2000)
    property_id: str | None = None


def _maybe_graduate(investor_id, relationship: str, actor: str):
    if relationship == "owned":
        try:
            graduate_to_existing_investor(investor_id, actor)
        except Exception as error:
            print(f"Graduating investor failed (non-fatal): {error}")


@router.post("/{investor_id}/portfolio")
def add_portfolio_property(investor_id: UUID, body: PortfolioIn, x_staybot_staff: str | None = Header(default=None)):
    load(investor_id)
    row = q(db._post, "investor_portfolio_properties",
            {"investor_id": str(investor_id), **body.model_dump(exclude_none=True, mode="json")})
    q(db._post, "investor_activity", {"investor_id": str(investor_id), "actor": staff_actor(x_staybot_staff),
                                      "action": "portfolio_property_added", "details": {"address": body.address}})
    _maybe_graduate(investor_id, body.relationship, staff_actor(x_staybot_staff))
    return row


@router.patch("/{investor_id}/portfolio/{property_row_id}")
def update_portfolio_property(investor_id: UUID, property_row_id: UUID, body: PortfolioIn,
                               x_staybot_staff: str | None = Header(default=None)):
    load(investor_id)
    changes = body.model_dump(exclude_none=True, mode="json")
    row = q(db._patch, "investor_portfolio_properties", changes, {"id": f"eq.{property_row_id}", "investor_id": f"eq.{investor_id}"})
    if not row:
        raise HTTPException(404, "Portfolio property not found.")
    q(db._post, "investor_activity", {"investor_id": str(investor_id), "actor": staff_actor(x_staybot_staff),
                                      "action": "portfolio_property_updated", "details": changes})
    if changes.get("relationship") == "owned":
        _maybe_graduate(investor_id, "owned", staff_actor(x_staybot_staff))
    return row[0] if isinstance(row, list) else row
