"""
Investment deal analysis: does this house make money as a rental?

Every number comes from the MLS listing, the team's saved assumptions, or what
staff type in. Nothing is invented: the rent estimate is only offered when there
are enough comparable rentals in our own listings, and it is always labelled an
estimate that staff must confirm. The output is a projection, not advice or a
promise, and the deal PDF says so.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

import requests
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from src.services import db, mls_listings

router = APIRouter(prefix="/deals", tags=["Investment deals"])

HOA_PER_YEAR = {"monthly": 12, "quarterly": 4, "semi-annually": 2, "semiannually": 2, "annually": 1, "yearly": 1,
                "one time": 0, "voluntary": 0}


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_deals.sql to use deal analysis.")


def q(fn, *args, **kwargs):
    configured()
    try:
        return fn(*args, **kwargs)
    except requests.RequestException as error:
        text = getattr(getattr(error, "response", None), "text", "") or str(error)
        if "PGRST205" in text or "does not exist" in text:
            raise HTTPException(503, "Run supabase_deals.sql in Supabase to enable deal analysis.")
        print(f"Deals database error: {text[:300]}")
        raise HTTPException(500, "The deals database request failed. Please try again.")


class Assumptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    down_payment_percent: float = Field(default=25, ge=0, le=100)
    interest_rate_percent: float = Field(default=6.5, ge=0, le=25)
    loan_years: int = Field(default=30, ge=1, le=40)
    closing_costs_percent: float = Field(default=3, ge=0, le=15)
    management_percent: float = Field(default=8, ge=0, le=30)
    maintenance_percent: float = Field(default=5, ge=0, le=30)
    vacancy_percent: float = Field(default=5, ge=0, le=30)
    insurance_annual: float = Field(default=1200, ge=0, le=100000)
    other_monthly: float = Field(default=0, ge=0, le=10000)


def saved_assumptions() -> Assumptions:
    rows = q(db._get, "deal_assumptions", {"id": "eq.1", "select": "values"})
    return Assumptions(**(rows[0]["values"] if rows else {}))


def money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def monthly_payment(loan, yearly_rate, years):
    if loan <= 0:
        return 0.0
    months = years * 12
    rate = yearly_rate / 100 / 12
    if rate == 0:
        return loan / months
    factor = (1 + rate) ** months
    return loan * rate * factor / (factor - 1)


def hoa_monthly(fee, frequency):
    if not fee:
        return 0.0
    per_year = HOA_PER_YEAR.get((frequency or "").strip().lower(), 12)
    return float(fee) * per_year / 12


def analyse(*, price, rent, taxes_annual, insurance_annual, hoa_month, rehab, assumptions: Assumptions):
    a = assumptions
    down = price * a.down_payment_percent / 100
    loan = price - down
    closing = price * a.closing_costs_percent / 100
    cash_needed = down + closing + rehab
    payment = monthly_payment(loan, a.interest_rate_percent, a.loan_years)

    gross_year = rent * 12
    percent_costs = (a.management_percent + a.maintenance_percent + a.vacancy_percent) / 100
    variable_year = gross_year * percent_costs
    fixed_year = taxes_annual + insurance_annual + hoa_month * 12 + a.other_monthly * 12
    operating_year = variable_year + fixed_year
    noi_year = gross_year - operating_year
    debt_year = payment * 12
    cash_flow_year = noi_year - debt_year

    breakeven_rent = (fixed_year + debt_year) / (12 * (1 - percent_costs)) if percent_costs < 1 else None
    return {
        "inputs": {"price": money(price), "monthly_rent": money(rent), "taxes_annual": money(taxes_annual),
                   "insurance_annual": money(insurance_annual), "hoa_monthly": money(hoa_month), "rehab": money(rehab),
                   **a.model_dump()},
        "cash_needed": {"down_payment": money(down), "closing_costs": money(closing), "repairs": money(rehab), "total": money(cash_needed)},
        "loan": {"amount": money(loan), "monthly_payment": money(payment), "annual_payment": money(debt_year)},
        "income": {"monthly_rent": money(rent), "annual_rent": money(gross_year)},
        "costs_monthly": {
            "taxes": money(taxes_annual / 12), "insurance": money(insurance_annual / 12), "hoa": money(hoa_month),
            "management": money(gross_year * a.management_percent / 100 / 12),
            "maintenance": money(gross_year * a.maintenance_percent / 100 / 12),
            "vacancy": money(gross_year * a.vacancy_percent / 100 / 12),
            "other": money(a.other_monthly),
            "total": money(operating_year / 12),
        },
        "results": {
            "cash_flow_monthly": money(cash_flow_year / 12),
            "cash_flow_annual": money(cash_flow_year),
            "noi_annual": money(noi_year),
            "cap_rate_percent": round(noi_year / price * 100, 2) if price else None,
            "cash_on_cash_percent": round(cash_flow_year / cash_needed * 100, 2) if cash_needed else None,
            "dscr": round(noi_year / debt_year, 2) if debt_year else None,
            "gross_yield_percent": round(gross_year / price * 100, 2) if price else None,
            "rent_to_price_percent": round(rent / price * 100, 2) if price else None,
            "breakeven_rent": money(breakeven_rent) if breakeven_rent else None,
        },
        "sensitivity": sensitivity(price=price, rent=rent, taxes_annual=taxes_annual, insurance_annual=insurance_annual,
                                   hoa_month=hoa_month, rehab=rehab, assumptions=a),
        "flags": flags(price, rent, cash_flow_year, noi_year, debt_year, cash_needed),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def sensitivity(*, price, rent, taxes_annual, insurance_annual, hoa_month, rehab, assumptions):
    """What happens if rent or the interest rate move. Same maths, different inputs."""
    def cash_flow(rent_value, rate):
        a = assumptions.model_copy(update={"interest_rate_percent": rate})
        loan = price - price * a.down_payment_percent / 100
        gross = rent_value * 12
        variable = gross * (a.management_percent + a.maintenance_percent + a.vacancy_percent) / 100
        fixed = taxes_annual + insurance_annual + hoa_month * 12 + a.other_monthly * 12
        return money((gross - variable - fixed - monthly_payment(loan, rate, a.loan_years) * 12) / 12)

    rate = assumptions.interest_rate_percent
    return {
        "rent_down_10_percent": cash_flow(rent * 0.9, rate),
        "rent_up_10_percent": cash_flow(rent * 1.1, rate),
        "rate_up_1_percent": cash_flow(rent, rate + 1),
        "rate_down_1_percent": cash_flow(rent, max(0.0, rate - 1)),
        "note": "Monthly cash flow if rent or the loan rate moves. Everything else stays the same.",
    }


def portfolio_totals(properties: list[dict], assumptions: Assumptions | None = None) -> dict:
    """Portfolio-level totals for an investor's already-owned properties
    (the investor_portfolio_properties rows - see
    src/services/investor_journey.py). Reuses the same building blocks as a
    single-deal analysis above (monthly_payment() for the debt payment,
    the same gross-yield formula as analyse()'s "gross_yield_percent")
    instead of inventing new maths - these properties are already owned,
    so there's no down payment or closing cost to project, just whatever
    value, mortgage balance, rent and expenses are already on file.

    Only properties marked "owned" or "acquired" count toward the totals -
    a "target" or "under_contract" property isn't a real holding yet, so
    counting it would overstate what the investor owns. Those rows are
    still returned (for display), just flagged as not counted.

    We don't know the actual rate/term of an existing mortgage - only its
    current balance - so the monthly payment on outstanding_mortgage is
    estimated using the team's saved deal-assumption interest rate and
    loan term (the same Assumptions a fresh deal analysis uses). That's
    the one assumption this makes; it's returned in "assumptions" below so
    the screen can label it rather than presenting it as a known fact.
    """
    a = assumptions or saved_assumptions()
    rows = []
    total_equity = 0.0
    total_cash_flow = 0.0
    counted_equity = 0
    counted_cash_flow = 0

    for p in properties:
        relationship = p.get("relationship") or "owned"
        counts = relationship in ("owned", "acquired")
        value = p.get("estimated_value") or p.get("purchase_price")
        mortgage = p.get("outstanding_mortgage") or 0.0
        rent = p.get("monthly_rent")
        expenses = p.get("monthly_expenses") or 0.0

        debt_service = money(monthly_payment(mortgage, a.interest_rate_percent, a.loan_years))
        equity = money(value - mortgage) if (counts and value is not None) else None
        cash_flow = money(rent - expenses - debt_service) if (counts and rent is not None) else None
        gross_yield = round(rent * 12 / value * 100, 2) if (counts and rent is not None and value) else None

        if equity is not None:
            total_equity += equity
            counted_equity += 1
        if cash_flow is not None:
            total_cash_flow += cash_flow
            counted_cash_flow += 1

        missing = []
        if counts and value is None:
            missing.append("current value (no estimated value or purchase price on file)")
        if counts and rent is None:
            missing.append("monthly rent")
        if not counts:
            missing.append(f'not yet owned ("{relationship}") - excluded from the totals')

        rows.append({
            "id": p.get("id"), "address": p.get("address"), "relationship": relationship,
            "counted_in_totals": counts,
            "value": money(value) if value is not None else None,
            "outstanding_mortgage": money(mortgage),
            "estimated_monthly_debt_service": debt_service,
            "monthly_rent": money(rent) if rent is not None else None,
            "monthly_expenses": money(expenses),
            "equity": equity,
            "monthly_cash_flow": cash_flow,
            "gross_yield_percent": gross_yield,
            "missing": missing,
        })

    return {
        "properties": rows,
        "totals": {
            "equity": money(total_equity),
            "monthly_cash_flow": money(total_cash_flow),
            "properties_counted_for_equity": counted_equity,
            "properties_counted_for_cash_flow": counted_cash_flow,
            "properties_total": len(rows),
        },
        "assumptions": {
            "mortgage_interest_rate_percent": a.interest_rate_percent,
            "mortgage_loan_term_years": a.loan_years,
            "note": (
                "Equity = current value (estimated value, or purchase price if there's no estimate) minus "
                "outstanding mortgage balance. Monthly cash flow = rent minus recorded monthly expenses minus "
                "an estimated mortgage payment on the outstanding balance, using the team's saved deal-analysis "
                f"assumptions of {a.interest_rate_percent}% interest over a {a.loan_years}-year term (the actual "
                "rate/term of an existing loan isn't on file, so this is an estimate). Gross yield = annual rent "
                "\u00f7 current value, the same formula as a single-deal analysis. Only properties marked \"owned\" "
                "or \"acquired\" count toward the totals."
            ),
        },
    }


def area_context(listing):
    """How this home's price per square foot compares with active listings in the same city.
    Uses only real MLS rows; returns None when there aren't enough."""
    city, area, price = listing.get("city"), listing.get("living_area"), listing.get("list_price")
    if not (city and area and price):
        return None
    try:
        rows = db._get("mls_listings", {"select": "list_price,living_area", "city": f"eq.{city}",
                                        "status_label": "eq.active", "limit": "500"})
    except Exception:
        return None
    others = sorted(float(r["list_price"]) / float(r["living_area"]) for r in rows
                    if r.get("list_price") and r.get("living_area") and float(r["living_area"]) > 0)
    if len(others) < 10:
        return None
    middle = others[len(others) // 2]
    this = float(price) / float(area)
    difference = (this - middle) / middle * 100
    return {"price_per_sqft": round(this, 2), "city_middle_per_sqft": round(middle, 2),
            "difference_percent": round(difference, 1), "based_on": len(others), "city": city,
            "summary": f"${this:,.0f}/sq ft vs ${middle:,.0f} middle in {city} "
                       f"({abs(difference):.0f}% {'above' if difference > 0 else 'below'}), from {len(others)} active listings"}


def flags(price, rent, cash_flow_year, noi_year, debt_year, cash_needed):
    out = []
    if cash_flow_year < 0:
        out.append({"level": "bad", "text": f"Loses ${abs(cash_flow_year) / 12:,.0f} a month at this rent and price."})
    elif cash_flow_year / 12 < 100:
        out.append({"level": "warn", "text": "Thin cash flow: under $100 a month leaves no room for surprises."})
    if debt_year and noi_year / debt_year < 1.25:
        out.append({"level": "warn", "text": "DSCR under 1.25: many lenders want at least that for a rental loan."})
    if rent / price * 100 < 0.7:
        out.append({"level": "warn", "text": "Rent is under 0.7% of price, which is usually weak for cash flow."})
    if cash_needed > 0:
        out.append({"level": "info", "text": f"Cash needed to buy: ${cash_needed:,.0f}."})
    return out


def rent_estimate(city, bedrooms):
    """Rough rent guide from our own rental listings. Returns None when there aren't enough."""
    if not city:
        return None
    try:
        rows = db._get("properties", {"select": "rent,bedrooms,city,status,listing_type", "listing_type": "eq.rent",
                                      "city": f"ilike.*{city}*", "limit": "500"})
    except Exception:
        return None
    rents = sorted(float(r["rent"]) for r in rows if r.get("rent") and (bedrooms is None or r.get("bedrooms") == bedrooms))
    if len(rents) < 3:
        return None
    return {"estimate": money(rents[len(rents) // 2]), "based_on": len(rents), "city": city, "bedrooms": bedrooms,
            "note": "Middle rent of our own listings in this city. Confirm with local rental comparables before offering."}


class AnalyseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    list_number: str | None = None
    price: float | None = Field(default=None, gt=0, le=50_000_000)
    monthly_rent: float = Field(gt=0, le=100_000)
    taxes_annual: float | None = Field(default=None, ge=0, le=500_000)
    insurance_annual: float | None = Field(default=None, ge=0, le=100_000)
    hoa_monthly: float | None = Field(default=None, ge=0, le=5_000)
    rehab: float = Field(default=0, ge=0, le=2_000_000)
    assumptions: Assumptions | None = None


def listing_for(list_number):
    rows = q(db._get, "mls_listings", {"list_number": f"eq.{list_number}", "select": "*"})
    if rows:
        return rows[0]
    # A home an investor listed for sale through Staybot (db.owner_sale_listings()).
    owner_listed = db.get_owner_sale_listing(list_number)
    if owner_listed:
        return owner_listed
    raise HTTPException(404, "MLS listing not found.")


@router.post("/analyze")
def analyze(body: AnalyseIn):
    configured()
    listing = listing_for(body.list_number) if body.list_number else None
    price = body.price or (float(listing["list_price"]) if listing and listing.get("list_price") else None)
    if not price:
        raise HTTPException(400, "Enter a price, or choose an MLS listing that has one.")
    taxes = body.taxes_annual if body.taxes_annual is not None else float(listing.get("tax_annual") or 0) if listing else 0.0
    hoa = body.hoa_monthly if body.hoa_monthly is not None else (hoa_monthly(listing.get("hoa_fee"), listing.get("hoa_frequency")) if listing else 0.0)
    assumptions = body.assumptions or saved_assumptions()
    insurance = body.insurance_annual if body.insurance_annual is not None else assumptions.insurance_annual
    result = analyse(price=price, rent=body.monthly_rent, taxes_annual=taxes, insurance_annual=insurance,
                     hoa_month=hoa, rehab=body.rehab, assumptions=assumptions)
    if listing:
        context = area_context(listing)
        if context:
            result["area"] = context
            result["flags"].append({"level": "info" if abs(context["difference_percent"]) < 10 else "warn",
                                    "text": "Price per sq ft: " + context["summary"]})
        result["listing"] = {k: listing.get(k) for k in ("list_number", "street_address", "city", "postal_code", "county",
                                                          "bedrooms", "bathrooms_full", "bathrooms_half", "living_area",
                                                          "year_built", "status_label", "list_price", "tax_annual",
                                                          "hoa_fee", "hoa_frequency", "subdivision", "photo_url",
                                                          "days_on_market", "price_per_sqft", "neighborhood")}
    result["sources"] = {
        "price": "MLS list price" if listing and body.price is None else "entered by staff",
        "taxes_annual": "MLS tax amount" if listing and body.taxes_annual is None else "entered by staff",
        "hoa_monthly": "MLS HOA fee" if listing and body.hoa_monthly is None else "entered by staff",
        "insurance_annual": "team assumption" if body.insurance_annual is None else "entered by staff",
        "monthly_rent": "entered by staff",
    }
    return result


@router.get("/rent-estimate")
def rent_guide(list_number: str | None = None, city: str | None = None, bedrooms: int | None = None):
    configured()
    if list_number:
        listing = listing_for(list_number)
        city, bedrooms = listing.get("city"), listing.get("bedrooms")
    estimate = rent_estimate(city, bedrooms)
    return estimate or {"estimate": None, "based_on": 0, "city": city, "bedrooms": bedrooms,
                        "note": "Not enough rentals of this size in our own listings. Enter the rent from local comparables."}


@router.get("/assumptions")
def get_assumptions():
    configured()
    rows = q(db._get, "deal_assumptions", {"id": "eq.1", "select": "*"})
    return rows[0] if rows else {"values": Assumptions().model_dump(), "updated_by": "defaults"}


@router.put("/assumptions")
def set_assumptions(body: Assumptions, x_staybot_staff: str | None = Header(default=None)):
    configured()
    actor = f"staff:{(x_staybot_staff or 'unnamed').strip()[:100]}"
    fields = {"values": body.model_dump(), "updated_by": actor, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if q(db._get, "deal_assumptions", {"id": "eq.1", "select": "id"}):
        q(db._patch, "deal_assumptions", fields, {"id": "eq.1"})
    else:
        q(db._post, "deal_assumptions", {"id": 1, **fields})
    return get_assumptions()


class SaveDeal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    list_number: str | None = None
    label: str = Field(min_length=1, max_length=200)
    analysis: dict
    notes: str = Field(default="", max_length=2000)
    person_id: UUID | None = None


@router.post("")
def save_deal(body: SaveDeal, x_staybot_staff: str | None = Header(default=None)):
    configured()
    listing = listing_for(body.list_number) if body.list_number else None
    # investment_deals.list_number references mls_listings; a home an investor
    # listed on Staybot isn't in that table, so its id stays in results.listing.
    mls_number = None if (listing or {}).get("owner_listed") else body.list_number
    row = q(db._post, "investment_deals", {
        "list_number": mls_number, "label": body.label[:200], "inputs": body.analysis.get("inputs", {}),
        "results": {k: body.analysis.get(k) for k in ("results", "cash_needed", "loan", "costs_monthly", "income", "flags", "listing", "sources")},
        "notes": body.notes.strip() or None, "person_id": str(body.person_id) if body.person_id else None,
        "created_by": f"staff:{(x_staybot_staff or 'unnamed').strip()[:100]}"})
    return row


@router.get("")
def list_deals(status: str | None = None):
    configured()
    params = {"select": "*", "order": "updated_at.desc", "limit": "200"}
    if status:
        params["status"] = f"eq.{status}"
    return q(db._get, "investment_deals", params)


class DealUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["shortlist", "offer_made", "bought", "rejected"] | None = None
    notes: str | None = Field(default=None, max_length=2000)


@router.patch("/{deal_id}")
def update_deal(deal_id: UUID, body: DealUpdate):
    configured()
    fields = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not fields:
        raise HTTPException(400, "Nothing to update.")
    updated = q(db._patch, "investment_deals", fields, {"id": f"eq.{deal_id}"})
    if not updated:
        raise HTTPException(404, "Deal not found.")
    return updated


@router.get("/{deal_id}/summary.pdf")
def deal_pdf(deal_id: UUID):
    from src.services import deal_pdf as pdf_maker
    configured()
    rows = q(db._get, "investment_deals", {"id": f"eq.{deal_id}", "select": "*"})
    if not rows:
        raise HTTPException(404, "Deal not found.")
    deal = rows[0]
    data = pdf_maker.render(deal)
    name = f"Deal-{(deal.get('list_number') or 'analysis')}.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "no-store"})
