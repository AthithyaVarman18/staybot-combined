"""
One-CSV bulk upload of an investor's own properties/units.

Same shape as owner_leads.py's provider-CSV import (column-name matching by
alias, one cleaned dict per row, "why this row was skipped" if it was), just
for investor_portfolio_properties instead of owner_leads - see
src/services/investor_journey.py POST /me/investor/portfolio/csv, which
calls parse_csv() then inserts the "new" rows.

Every column is optional except address; a row with no address is skipped
since there'd be nothing to show on the dashboard or link an alert to.
"""

import csv
import io
import re

from fastapi import HTTPException

from src.services.owner_leads import clean_date, header_key
from src.services.properties import parse_int, parse_money

MAX_FILE = 2 * 1024 * 1024
MAX_ROWS = 2000

RELATIONSHIPS = {"owned", "target", "under_contract", "acquired"}

# Canonical field -> accepted CSV header spellings (lower-case, spaces as _).
ALIASES = {
    "address": ["address", "street_address", "property_address", "property", "location"],
    "unit_label": ["unit_label", "unit", "unit_number", "apartment", "apt"],
    "property_type": ["property_type", "type"],
    "bedrooms": ["bedrooms", "beds", "bhk", "bedroom"],
    "bathrooms": ["bathrooms", "baths", "bathroom"],
    "estimated_value": ["estimated_value", "value", "current_value", "market_value"],
    "purchase_price": ["purchase_price", "bought_for", "acquisition_price"],
    "outstanding_mortgage": ["outstanding_mortgage", "mortgage_balance", "mortgage", "loan_balance"],
    "monthly_rent": ["monthly_rent", "rent", "rent_per_month"],
    "monthly_expenses": ["monthly_expenses", "expenses", "operating_expenses", "monthly_costs"],
    "tenant_name": ["tenant_name", "tenant", "resident", "occupant"],
    "lease_start_date": ["lease_start_date", "lease_start", "lease_begins"],
    "lease_end_date": ["lease_end_date", "lease_end", "lease_expires", "lease_expiry"],
    "relationship": ["relationship", "status", "ownership_status"],
    "condition_notes": ["condition_notes", "notes", "remarks", "comments"],
}


def map_headers(headers):
    keys = {header_key(h): h for h in headers if h}
    mapping = {}
    for field, names in ALIASES.items():
        for name in names:
            if name in keys and keys[name] not in mapping.values():
                mapping[field] = keys[name]
                break
    unused = [h for h in headers if h and h not in mapping.values()]
    return mapping, unused


def clean_relationship(value):
    v = re.sub(r"[^a-z]+", "_", (value or "").strip().lower()).strip("_")
    return v if v in RELATIONSHIPS else "owned"


def parse_csv(data: bytes):
    """Returns (rows, mapping, unused_columns). Each row has "result"
    ("new" or "skipped") and "reason" when skipped, same convention as
    owner_leads.parse_csv, so the upload response can report both."""

    if len(data) > MAX_FILE:
        raise HTTPException(413, "The file is larger than 2 MB. Split it into smaller files.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "The file has no header row.")

    mapping, unused = map_headers(reader.fieldnames)
    if "address" not in mapping:
        raise HTTPException(400, "The file needs an address column (e.g. \"address\" or \"property\").")

    rows = []
    for number, raw in enumerate(reader, start=2):
        if len(rows) >= MAX_ROWS:
            raise HTTPException(413, f"More than {MAX_ROWS} rows. Split the file.")

        get = lambda field: (raw.get(mapping[field]) or "").strip() if field in mapping else ""

        row = {
            "line": number,
            "address": get("address")[:300] or None,
            "unit_label": get("unit_label")[:100] or None,
            "property_type": get("property_type")[:100] or None,
            "bedrooms": parse_int(get("bedrooms")),
            "bathrooms": parse_int(get("bathrooms")),
            "estimated_value": parse_money(get("estimated_value")),
            "purchase_price": parse_money(get("purchase_price")),
            "outstanding_mortgage": parse_money(get("outstanding_mortgage")),
            "monthly_rent": parse_money(get("monthly_rent")),
            "monthly_expenses": parse_money(get("monthly_expenses")),
            "tenant_name": get("tenant_name")[:200] or None,
            "lease_start_date": clean_date(get("lease_start_date")),
            "lease_end_date": clean_date(get("lease_end_date")),
            "relationship": clean_relationship(get("relationship")),
            "condition_notes": get("condition_notes")[:2000] or None,
        }

        if not row["address"]:
            row["result"], row["reason"] = "skipped", "no address"
        else:
            row["result"], row["reason"] = "new", None
        rows.append(row)

    return rows, mapping, unused
