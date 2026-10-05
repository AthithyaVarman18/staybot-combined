"""Persistent, role-aware property requirement profiles.

This module is deliberately separate from conversations, investor CRM and
property listings. It gives the authenticated account one structured source
of truth for what they are looking for and uses deterministic application
logic for property matching before the LLM is involved.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.services import db, properties

CUSTOMER_ROLES = {"tenant", "new_investor", "existing_investor"}
SUPPORTED_PROFILES = CUSTOMER_ROLES | {"owner", "buyer"}

ROLE_TITLES = {
    "tenant": "Tenant / Renter",
    "new_investor": "New Property Investor",
    "existing_investor": "Existing Property Investor",
    "owner": "Owner / Seller",
    "buyer": "Buyer",
}


def _clean_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [x.strip() for x in value.split(",")]
    if not isinstance(value, list):
        raise ValueError("Expected a list.")
    return [str(x).strip() for x in value if str(x).strip()]


def _date_or_none(value: Any):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ValueError("Enter a valid date.") from exc


class RequirementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Shared / tenant
    search_intent: str | None = None
    city_area: str | None = Field(default=None, max_length=200)
    preferred_neighborhoods: list[str] = Field(default_factory=list)
    property_type: str | None = Field(default=None, max_length=80)
    bedrooms: int | None = Field(default=None, ge=0, le=50)
    bathrooms: int | None = Field(default=None, ge=0, le=50)
    budget_min: float | None = Field(default=None, ge=0, le=1_000_000_000)
    budget_max: float | None = Field(default=None, ge=0, le=1_000_000_000)
    move_in_date: str | None = None
    lease_duration: str | None = None
    furnishing: str | None = None
    required_features: list[str] = Field(default_factory=list)
    additional_requirements: str | None = Field(default=None, max_length=3000)

    # Investor
    investment_goal: str | None = None
    available_cash: float | None = Field(default=None, ge=0, le=1_000_000_000)
    purchase_method: str | None = None
    financing_status: str | None = None
    target_rental_income: float | None = Field(default=None, ge=0, le=1_000_000_000)
    target_roi: float | None = Field(default=None, ge=0, le=1000)
    hold_period: str | None = None
    investment_timeline: str | None = None
    risk_preference: str | None = None
    properties_to_consider: str | None = None
    primary_investment_objective: str | None = Field(default=None, max_length=3000)
    current_portfolio_count: int | None = Field(default=None, ge=0, le=10000)
    current_portfolio_value: float | None = Field(default=None, ge=0, le=10_000_000_000)
    current_investment_properties: str | None = Field(default=None, max_length=5000)
    looking_for: list[str] = Field(default_factory=list)
    investment_strategy: str | None = None
    additional_budget: float | None = Field(default=None, ge=0, le=1_000_000_000)
    financing: str | None = None
    target_property_type: str | None = None
    purchase_timeline: str | None = None

    # Owner / seller
    property_address: str | None = Field(default=None, max_length=500)
    approximate_size: float | None = Field(default=None, ge=0, le=10_000_000)
    occupancy: str | None = None
    selling_timeline: str | None = None
    expected_price: float | None = Field(default=None, ge=0, le=10_000_000_000)
    expected_price_min: float | None = Field(default=None, ge=0, le=10_000_000_000)
    expected_price_max: float | None = Field(default=None, ge=0, le=10_000_000_000)
    property_condition: str | None = None
    major_improvements: str | None = Field(default=None, max_length=5000)
    current_tenant_status: str | None = Field(default=None, max_length=3000)
    selling_goal: str | None = Field(default=None, max_length=3000)
    preferred_communication: str | None = None

    @field_validator("preferred_neighborhoods", "required_features", "looking_for", mode="before")
    @classmethod
    def clean_lists(cls, value):
        return _clean_list(value)

    @field_validator("move_in_date", mode="before")
    @classmethod
    def clean_date(cls, value):
        return _date_or_none(value)

    @model_validator(mode="after")
    def valid_ranges(self):
        if self.budget_min is not None and self.budget_max is not None and self.budget_min > self.budget_max:
            raise ValueError("Minimum budget cannot be greater than maximum budget.")
        if self.expected_price_min is not None and self.expected_price_max is not None and self.expected_price_min > self.expected_price_max:
            raise ValueError("Minimum expected price cannot be greater than maximum expected price.")
        return self


def require_account(request: Request) -> dict:
    from src.services import accounts
    account = accounts.current_account(request)
    if not account:
        raise HTTPException(401, "Please log in to manage your requirements.")
    return account


def _table_rows(user_id: str, profile_type: str | None = None) -> list[dict]:
    params = {"user_id": f"eq.{user_id}", "is_active": "eq.true", "order": "updated_at.desc", "select": "*"}
    if profile_type:
        params["profile_type"] = f"eq.{profile_type}"
    return db._get("user_requirements", params) or []


def get_active(user_id: str, profile_type: str | None = None) -> dict | None:
    if not db.ENABLED:
        return None
    rows = _table_rows(user_id, profile_type)
    return rows[0] if rows else None


def has_active(user_id: str, profile_type: str) -> bool:
    try:
        return get_active(user_id, profile_type) is not None
    except Exception as exc:
        # Keep authentication usable before the optional requirements SQL has
        # been run. The requirements page will show the actionable setup error.
        print(f"Requirements lookup unavailable (non-fatal): {exc}")
        return False


def _completion(role: str, data: dict) -> dict:
    if role in ("tenant", "buyer"):
        groups = [
            ("city_area", "Location"), ("property_type", "Property type"),
            ("bedrooms", "Bedrooms"), ("bathrooms", "Bathrooms"),
            ("budget_max", "Maximum rent"), ("move_in_date", "Move-in date"),
            ("lease_duration", "Lease duration"), ("furnishing", "Furnishing"),
            ("required_features", "Features"), ("additional_requirements", "Additional requirements"),
        ]
    elif role in ("new_investor", "existing_investor"):
        groups = [
            ("investment_goal", "Investment goal"), ("city_area", "Location"),
            ("budget_max" if role == "new_investor" else "additional_budget", "Budget"),
            ("available_cash", "Available cash"), ("purchase_method", "Purchase method"),
            ("financing_status" if role == "new_investor" else "financing", "Financing"),
            ("target_property_type", "Property type"), ("bedrooms", "Bedrooms"),
            ("bathrooms", "Bathrooms"), ("target_roi", "Target ROI"),
            ("investment_timeline" if role == "new_investor" else "Purchase timeline", "Timeline"),
            ("risk_preference", "Risk preference"),
        ]
    else:
        groups = [
            ("property_address", "Property address"), ("property_type", "Property type"),
            ("bedrooms", "Bedrooms"), ("bathrooms", "Bathrooms"),
            ("approximate_size", "Approximate size"), ("occupancy", "Occupancy"),
            ("selling_timeline", "Selling timeline"), ("expected_price", "Expected price"),
            ("property_condition", "Condition"), ("selling_goal", "Selling goal"),
            ("preferred_communication", "Communication"),
        ]
    required = 7 if role != "owner" else 6
    filled = 0
    missing = []
    for key, label in groups:
        value = data.get(key)
        if value not in (None, "", [], {}):
            filled += 1
        else:
            missing.append(label)
    pct = round((filled / len(groups)) * 100) if groups else 0
    return {"percentage": pct, "filled": filled, "total": len(groups), "missing": missing, "required_recommended": required}


def public_row(row: dict) -> dict:
    data = row.get("requirements_json") or {}
    return {
        "id": row.get("id"),
        "role": row.get("role"),
        "profile_type": row.get("profile_type"),
        "requirements": data,
        "completion": _completion(row.get("profile_type") or row.get("role"), data),
        "version": row.get("version", 1),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "is_active": bool(row.get("is_active", True)),
    }


def _validate_user_minimum(role: str, data: dict) -> None:
    if role in ("tenant", "buyer"):
        required = {"city_area": "City / Area", "property_type": "Property type", "budget_max": "Maximum budget"}
    elif role in ("new_investor", "existing_investor"):
        budget_key = "additional_budget" if role == "existing_investor" else "budget_max"
        required = {"investment_goal": "Primary investment goal", "city_area": "City / Area", budget_key: "Investment budget", "target_property_type": "Target property type"}
    else:
        required = {"property_address": "Property address", "property_type": "Property type", "selling_timeline": "Selling timeline", "expected_price": "Expected price"}
    missing = [label for key, label in required.items() if data.get(key) in (None, "", [])]
    if missing:
        raise ValueError("Please complete: " + ", ".join(missing) + ".")


def save(user_id: str, role: str, payload: dict, *, created_by: str = "user", profile_type: str | None = None) -> dict:
    if role not in SUPPORTED_PROFILES:
        raise HTTPException(400, "Unsupported requirement profile.")
    if not db.ENABLED:
        raise HTTPException(503, "Requirements storage is not configured. Run supabase_requirements.sql in Supabase.")

    clean = RequirementPayload.model_validate(payload).model_dump(exclude_none=True)
    if created_by == "user":
        _validate_user_minimum(role, clean)
    profile_type = profile_type or role
    existing = get_active(user_id, profile_type)
    now = datetime.utcnow().isoformat() + "Z"

    if existing:
        next_version = int(existing.get("version") or 1) + 1
        db._patch("user_requirements", {"requirements_json": clean, "version": next_version, "updated_at": now}, {"id": f"eq.{existing['id']}", "user_id": f"eq.{user_id}"})
        requirement_id = existing["id"]
    else:
        next_version = 1
        requirement_id = str(uuid4())
        db._post("user_requirements", {
            "id": requirement_id, "user_id": user_id, "role": role,
            "profile_type": profile_type, "requirements_json": clean,
            "version": 1, "created_by": created_by, "is_active": True,
        })

    db._post("requirement_versions", {
        "requirement_id": requirement_id, "version": next_version,
        "requirements_json": clean, "created_by": created_by,
    })
    row = get_active(user_id, profile_type)
    return public_row(row)


def deactivate(user_id: str, profile_type: str) -> None:
    if not db.ENABLED:
        return
    db._patch("user_requirements", {"is_active": False}, {"user_id": f"eq.{user_id}", "profile_type": f"eq.{profile_type}", "is_active": "eq.true"})


def _tokens(text: Any) -> set[str]:
    return {x for x in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(x) > 1}


def _location_match(wanted: str, p: dict) -> bool:
    wanted_tokens = _tokens(wanted)
    hay = _tokens(" ".join(str(p.get(k) or "") for k in ("location", "area", "city", "title")))
    return bool(wanted_tokens) and bool(wanted_tokens & hay)


def _feature_match(feature: str, p: dict) -> bool:
    feature = str(feature).lower().strip()
    amenities = " ".join(str(x).lower() for x in (p.get("amenities") or []))
    mapping = {
        "parking": bool(p.get("parking")),
        "pet-friendly": p.get("pets_allowed") is True,
        "pet friendly": p.get("pets_allowed") is True,
        "laundry": "laundry" in amenities or "washer" in amenities,
        "pool": "pool" in amenities,
        "garage": "garage" in amenities,
        "garden/yard": any(x in amenities for x in ("yard", "garden")),
        "accessibility features": "access" in amenities,
    }
    if feature in mapping:
        return mapping[feature]
    return feature in amenities or feature in str(p.get("description") or "").lower()


def _match_one(p: dict, role: str, req: dict) -> dict | None:
    is_rental = role == "tenant"
    is_buyer = role == "buyer"
    if is_rental and p.get("listing_type") != "rent":
        return None
    if is_buyer and p.get("listing_type") != "sale":
        return None
    if role in ("new_investor", "existing_investor") and p.get("listing_type") != "sale":
        return None

    checks: list[tuple[int, bool, str]] = []
    notes: list[str] = []

    location = req.get("city_area")
    if location:
        ok = _location_match(location, p)
        if not ok:
            return None
        checks.append((30, True, f"Preferred location: {p.get('area') or p.get('city') or p.get('location')}"))

    ptype = req.get("property_type") or req.get("target_property_type")
    if ptype:
        wanted = str(ptype).lower().replace("single family", "house")
        actual = str(p.get("property_type") or "").lower().replace("single family", "house")
        if wanted != actual and wanted not in actual and actual not in wanted:
            return None
        checks.append((15, True, f"Property type: {p.get('property_type')}"))

    beds = req.get("bedrooms")
    if beds is not None:
        if p.get("bedrooms") is None or p["bedrooms"] < beds:
            return None
        checks.append((10, True, f"Bedrooms: {p.get('bedrooms')}"))

    baths = req.get("bathrooms")
    if baths is not None:
        if p.get("bathrooms") is None or p["bathrooms"] < baths:
            return None
        checks.append((5, True, f"Bathrooms: {p.get('bathrooms')}"))

    max_budget = req.get("budget_max") if is_rental else req.get("budget_max") or req.get("additional_budget")
    min_budget = req.get("budget_min")
    price = properties.price_of(p)
    if max_budget is not None:
        if price is None or price > max_budget:
            return None
        checks.append((25, True, "Within budget"))
    if min_budget is not None and price is not None and price < min_budget:
        return None

    if is_rental and req.get("furnishing") and req["furnishing"] != "either":
        wanted = str(req["furnishing"]).lower().replace("furnished", "furnished")
        actual = str(p.get("furnished") or "").lower()
        if wanted == "unfurnished" and actual != "unfurnished":
            return None
        if wanted == "furnished" and "furnished" not in actual:
            return None
        checks.append((5, True, f"Furnishing: {p.get('furnished')}"))

    features = req.get("required_features") or []
    if features:
        for feature in features:
            if not _feature_match(feature, p):
                return None
            checks.append((5, True, f"Matches {feature}"))

    if not checks:
        return None

    total_weight = sum(w for w, _, _ in checks)
    score = round(sum(w for w, ok, _ in checks if ok) / total_weight * 100) if total_weight else 0
    reasons = [reason for _, _, reason in checks]
    return {
        "id": p.get("id"), "ref": p.get("ref"), "title": p.get("title"),
        "area": p.get("area"), "city": p.get("city"), "location": p.get("location"),
        "property_type": p.get("property_type"), "bedrooms": p.get("bedrooms"), "bathrooms": p.get("bathrooms"),
        "listing_type": p.get("listing_type"), "price": price,
        "price_label": properties.price_label(p), "match_score": score,
        "reasons": reasons, "why": reasons[:5],
        "context": properties.property_context(p),
    }


def match_properties(role: str, req: dict, limit: int = 6) -> dict:
    if role not in ("tenant", "buyer", "new_investor", "existing_investor"):
        return {"matches": [], "total": 0, "role": role}
    rows, source = properties.all_properties()
    matches = [m for p in rows if (m := _match_one(p, role, req))]
    matches.sort(key=lambda x: (-x["match_score"], x.get("price") or 10**18, x.get("title") or ""))
    return {"matches": matches[:limit], "total": len(matches), "source": source, "role": role}


def detect_update_request(message: str, row: dict | None) -> dict | None:
    """Detect a small, explicit preference change without mutating storage.

    The chatbot uses this only to propose an update. The user must confirm in
    the UI before `apply_update` is called, so conversational text can never
    silently rewrite the saved profile.
    """
    if not row or not message:
        return None
    text = str(message).strip()
    lower = text.lower()
    data = dict(row.get("requirements_json") or {})
    role = row.get("profile_type") or row.get("role")
    patch = {}

    money = re.search(r"(?:budget|price|rent|spend|investment)\D{0,25}\$?([0-9][0-9,]*(?:\.[0-9]+)?)(?:\s*(k|thousand|m|million))?", lower)
    if money and any(w in lower for w in ("increase", "raise", "change", "update", "set", "make", "maximum", "max")):
        value = float(money.group(1).replace(',', ''))
        unit = (money.group(2) or '').lower()
        value *= {'k': 1000, 'thousand': 1000, 'm': 1_000_000, 'million': 1_000_000}.get(unit, 1)
        key = 'budget_max' if role in ('tenant', 'buyer', 'new_investor') else 'additional_budget'
        if role == 'existing_investor':
            key = 'additional_budget'
        patch[key] = value

    beds = re.search(r"(?:bedrooms?|beds?|bhk)\D{0,12}(?:to|=|at|of)?\s*(\d{1,2})", lower)
    if beds and any(w in lower for w in ("change", "update", "increase", "decrease", "set", "make", "need")):
        patch['bedrooms'] = int(beds.group(1))

    roi = re.search(r"(?:roi|return)\D{0,12}(?:to|=|at|of)?\s*([0-9]+(?:\.[0-9]+)?)\s*%", lower)
    if roi and role in ('new_investor', 'existing_investor'):
        patch['target_roi'] = float(roi.group(1))

    loc = re.search(r"(?:location|area|neighborhood|neighbourhood)\D{0,12}(?:to|=|in)?\s+([A-Za-z][A-Za-z .'-]{2,60})", text, re.I)
    if loc and any(w in lower for w in ("change", "update", "move", "switch", "set")):
        patch['city_area'] = loc.group(1).strip(" .")

    if not patch:
        return None
    before = {k: data.get(k) for k in patch}
    return {"profile_type": row.get("profile_type") or row.get("role"), "patch": patch, "before": before}


def apply_update(user_id: str, row: dict, patch: dict, *, created_by: str = "user") -> dict:
    data = dict(row.get("requirements_json") or {})
    data.update(patch)
    role = row.get("role") or row.get("profile_type")
    return save(user_id, role, data, created_by=created_by, profile_type=row.get("profile_type") or role)


def context_note(row: dict | None) -> str | None:
    if not row:
        return None
    role = row.get("profile_type") or row.get("role")
    data = row.get("requirements_json") or {}
    compact = {k: v for k, v in data.items() if v not in (None, "", [], {})}
    if not compact:
        return None
    return (
        "The customer has a saved requirement profile below. Treat these as KNOWN USER PREFERENCES. "
        "Do not ask again for any field that is present unless the customer explicitly says it has changed. "
        "Use the structured property matching results supplied by the application; do not invent listings "
        "or financial metrics. If a required detail is genuinely missing, ask only for that missing detail.\n\n"
        f"PROFILE ROLE: {ROLE_TITLES.get(role, role)}\n"
        f"SAVED REQUIREMENTS:\n{json.dumps(compact, indent=2, default=str)}"
    )


def staff_rows(limit: int = 200) -> list[dict]:
    rows = db._get("user_requirements", {"is_active": "eq.true", "order": "updated_at.desc", "limit": str(limit), "select": "*"}) or []
    if not rows:
        return []
    account_ids = list({r.get("user_id") for r in rows if r.get("user_id")})
    accounts = {}
    if account_ids:
        # Supabase/PostgREST `in` syntax is safe here because ids are UUIDs returned by the DB.
        found = db._get("accounts", {"id": f"in.({','.join(account_ids)})", "select": "id,name,email,role", "limit": str(limit)}) or []
        accounts = {a["id"]: a for a in found}
    out = []
    for row in rows:
        a = accounts.get(row.get("user_id"), {})
        item = public_row(row)
        item.update({"client_id": row.get("user_id"), "client_name": a.get("name"), "client_email": a.get("email"), "client_role": a.get("role")})
        out.append(item)
    return out
