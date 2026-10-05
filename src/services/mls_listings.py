"""
Homes for sale, imported from an MLS CSV export.

MLS data is licensed by the broker's MLS: it is stored server-side only, served
to the team pages, and never published publicly. Re-importing a newer export
updates prices and statuses instead of creating duplicates (List Number is the key).

Nothing here guesses values: numbers come from the file, and rows without a
List Number or a price are skipped and reported.
"""

import csv
import io
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

import requests
from fastapi import APIRouter, Header, HTTPException, Request

from src.services import db

router = APIRouter(prefix="/mls", tags=["MLS listings"])

MAX_FILE = 30 * 1024 * 1024
CHUNK = 25          # rows per request: each row carries the full MLS record, so keep batches small
# MLS status codes vary by board; these are the common ones in this export.
STATUS_LABELS = {"A": "active", "P": "pending", "H": "hold", "C": "closed", "S": "sold",
                 "W": "withdrawn", "X": "expired", "T": "temporarily off market"}
COLUMNS = {
    "list_number": "List Number", "status": "Status", "property_type": "Property Type",
    "property_sub_type": "Property Sub Type", "unit_number": "Unit Number", "city": "City",
    "state": "State Or Province", "county": "County", "postal_code": "Postal Code",
    "subdivision": "Subdivision Name", "latitude": "Latitude", "longitude": "Longitude",
    "bedrooms": "Bedrooms Total", "bathrooms_full": "Bathrooms Full", "bathrooms_half": "Bathrooms Half",
    "living_area": "Living Area", "lot_size": "Lot Size Dimensions", "stories": "Stories",
    "year_built": "Year Built", "garage_spaces": "Garage Spaces", "list_price": "List Price",
    "original_list_price": "Original List Price", "close_price": "Close Price",
    "tax_assessed_value": "Tax Assessed Value", "tax_annual": "Tax Annual Amount",
    "hoa_fee": "Association Fee", "hoa_frequency": "Association Fee Frequency",
    "listing_date": "Listing Contract Date", "close_date": "Close Date",
    "agency_name": "Agency Name", "listing_agent": "Listing Agent", "agency_phone": "Agency Phone",
    "remarks": "Public Remarks", "photo_url": "Photo URL", "days_on_market": "Days on Market",
    "price_per_sqft": "List Price/SqFt", "lot_acres": "Lot Size Acres", "neighborhood": "Neighborhood",
    "new_construction": "New Construction YN", "parking_total": "Parking Total",
}
# Never kept: MLS fields that are for agents only, not for our screens or customers.
PRIVATE_FIELDS = {"Private Remarks", "Financial Remarks", "Syndication Remarks", "Owner Name",
                  "Concessions Comments", "Contingent Remarks", "Tax Legal Description"}
ADDRESS_PARTS = ["Street Number", "Street Direction Prefix", "Street Name", "Street Suffix", "Street Direction Suffix"]
NUMBERS = {"latitude", "longitude", "garage_spaces", "list_price", "original_list_price", "close_price",
           "tax_assessed_value", "tax_annual", "hoa_fee", "price_per_sqft", "lot_acres"}
INTEGERS = {"bedrooms", "bathrooms_full", "bathrooms_half", "living_area", "stories", "year_built",
            "days_on_market", "parking_total"}
DATES = {"listing_date", "close_date"}


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_mls_listings.sql to use MLS listings.")


def q(fn, *args, **kwargs):
    configured()
    try:
        return fn(*args, **kwargs)
    except requests.RequestException as error:
        text = getattr(getattr(error, "response", None), "text", "") or str(error)
        if "PGRST205" in text or "does not exist" in text:
            raise HTTPException(503, "Run supabase_mls_listings.sql in Supabase to enable MLS listings.")
        print(f"MLS database error: {text[:300]}")
        raise HTTPException(500, "The MLS database request failed. Please try again.")


def number(value):
    text = re.sub(r"[^\d.\-]", "", (value or "").strip())
    if not text or text in ("-", "."):
        return None
    try:
        return float(Decimal(text))
    except InvalidOperation:
        return None


def whole(value):
    n = number(value)
    return int(n) if n is not None else None


def day(value):
    value = (value or "").strip()[:10]
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def address_of(raw):
    parts = [(raw.get(p) or "").strip() for p in ADDRESS_PARTS]
    return " ".join(p for p in parts if p) or None


def build_row(raw, file_name):
    row = {}
    for field, column in COLUMNS.items():
        value = (raw.get(column) or "").strip()
        if field in NUMBERS:
            row[field] = number(value)
        elif field in INTEGERS:
            row[field] = whole(value)
        elif field in DATES:
            row[field] = day(value)
        else:
            row[field] = value or None
    row["street_address"] = address_of(raw)
    row["new_construction"] = (raw.get("New Construction YN") or "").strip().upper() in ("Y", "YES", "TRUE")
    photo = (row.get("photo_url") or "").strip()
    row["photo_url"] = photo if photo.startswith("http") else None
    row["status"] = (row.get("status") or "").upper() or "?"
    row["status_label"] = STATUS_LABELS.get(row["status"], "unknown")
    row["remarks"] = (row.get("remarks") or "")[:4000] or None
    row["source_file"] = file_name
    row["raw"] = {k: v for k, v in raw.items() if v not in (None, "") and k not in PRIVATE_FIELDS}
    return row


def parse(data: bytes, file_name: str):
    if len(data) > MAX_FILE:
        raise HTTPException(413, "The export is larger than 30 MB. Split it into smaller files.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")            # MLS exports are often Windows-encoded
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "List Number" not in reader.fieldnames:
        raise HTTPException(400, "This does not look like an MLS export: no 'List Number' column.")
    rows, skipped = [], []
    for line, raw in enumerate(reader, start=2):
        row = build_row(raw, file_name)
        if not row["list_number"]:
            skipped.append({"line": line, "reason": "no List Number"})
            continue
        if row["list_price"] is None:
            skipped.append({"line": line, "reason": f"no List Price ({row.get('street_address') or 'unknown address'})"})
            continue
        rows.append(row)
    seen, unique = set(), []
    for row in rows:
        if row["list_number"] in seen:
            skipped.append({"line": None, "reason": f"repeated List Number {row['list_number']} in the file"})
            continue
        seen.add(row["list_number"])
        unique.append(row)
    return unique, skipped


def upsert(rows):
    """Insert new listings and update existing ones in one request per chunk
    (PostgREST upsert on the List Number key). Row-by-row updates were too slow
    for a full export."""
    inserted = updated = 0
    for start in range(0, len(rows), CHUNK):
        chunk = rows[start:start + CHUNK]
        keys = ",".join(f'"{r["list_number"]}"' for r in chunk)
        existing = {r["list_number"] for r in q(db._get, "mls_listings", {"select": "list_number", "list_number": f"in.({keys})"})}
        last_error = None
        for attempt in range(3):
            try:
                response = requests.post(
                    f"{db.REST_URL}/mls_listings",
                    headers={**db.HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
                    params={"on_conflict": "list_number"},
                    json=chunk,
                    timeout=120,
                )
                if response.ok:
                    break
                last_error = f"{response.status_code} {response.text[:200]}"
            except requests.RequestException as error:      # dropped connection on a big batch
                last_error = str(error)[:200]
            time.sleep(1 + attempt)
        else:
            print(f"MLS upsert failed after 3 tries: {last_error}")
            raise HTTPException(500, "Saving the listings failed part-way. Run the import again; "
                                     "listings already saved are updated, not duplicated.")
        inserted += sum(1 for r in chunk if r["list_number"] not in existing)
        updated += sum(1 for r in chunk if r["list_number"] in existing)
    return inserted, updated


@router.post("/import")
async def import_export(request: Request, commit: bool = False, x_staybot_staff: str | None = Header(default=None),
                        x_file_name: str | None = Header(default=None)):
    """Preview (commit=false) or import (commit=true) an MLS CSV export."""
    configured()
    file_name = (x_file_name or "mls-export.csv")[:200]
    rows, skipped = parse(await request.body(), file_name)
    prices = sorted(r["list_price"] for r in rows if r["list_price"])
    summary = {
        "rows": len(rows), "skipped": len(skipped),
        "cities": sorted({r["city"] for r in rows if r["city"]})[:20],
        "statuses": {label: sum(r["status_label"] == label for r in rows) for label in sorted({r["status_label"] for r in rows})},
        "price_low": prices[0] if prices else None, "price_high": prices[-1] if prices else None,
        "price_middle": prices[len(prices) // 2] if prices else None,
    }
    preview = {"summary": summary, "skipped_rows": skipped[:20],
               "sample": [{k: r[k] for k in ("list_number", "street_address", "city", "list_price", "bedrooms",
                                             "bathrooms_full", "living_area", "year_built", "status_label")} for r in rows[:10]]}
    if not commit:
        return {**preview, "imported": False}
    inserted, updated = upsert(rows)
    record = q(db._post, "mls_imports", {"file_name": file_name, "row_count": len(rows), "inserted": inserted,
                                         "updated": updated, "skipped": len(skipped),
                                         "created_by": f"staff:{(x_staybot_staff or 'unnamed').strip()[:100]}"})
    return {**preview, "imported": True, "inserted": inserted, "updated": updated, "import_id": record["id"]}


@router.get("")
def search(request: Request, city: str | None = None, status: str = "active", min_price: float | None = None, max_price: float | None = None,
           min_beds: int | None = None, max_year: int | None = None, q_text: str | None = None,
           sort: Literal["price_asc", "price_desc", "newest"] = "price_asc", limit: int = 100):
    """Homes for sale, for staff and the investor advisor: the MLS feed plus
    homes investors listed for sale on Staybot (each tagged owner_listed).
    A logged-in investor never sees their own listings here."""
    configured()
    from src.services import accounts   # local import, avoids import cycles
    exclude_session_id = (accounts.current_account(request) or {}).get("session_id")
    params = {"select": "list_number,street_address,city,county,postal_code,subdivision,status_label,list_price,"
                        "tax_assessed_value,tax_annual,hoa_fee,hoa_frequency,bedrooms,bathrooms_full,bathrooms_half,"
                        "living_area,year_built,stories,garage_spaces,listing_date,latitude,longitude,photo_url,"
                        "days_on_market,price_per_sqft,lot_acres,neighborhood,new_construction,parking_total",
              "limit": str(max(1, min(limit, 500)))}
    params["order"] = {"price_asc": "list_price.asc", "price_desc": "list_price.desc", "newest": "listing_date.desc"}[sort]
    if status != "all":
        params["status_label"] = f"eq.{status}"
    if city:
        params["city"] = f"ilike.*{city}*"
    if min_price is not None:
        params["list_price"] = f"gte.{min_price}"
    elif max_price is not None:
        params["list_price"] = f"lte.{max_price}"
    if min_beds is not None:
        params["bedrooms"] = f"gte.{min_beds}"
    if max_year is not None:
        params["year_built"] = f"lte.{max_year}"
    rows = q(db._get, "mls_listings", params)
    if min_price is not None and max_price is not None:      # PostgREST takes one filter per column
        rows = [r for r in rows if r["list_price"] is not None and float(r["list_price"]) <= max_price]
    rows = list(rows or []) + owner_listed_for_search(status, city, min_price, max_price, min_beds, max_year, exclude_session_id)
    rows = sort_rows(rows, sort)[:max(1, min(limit, 500))]
    if q_text:
        needle = q_text.lower()
        rows = [r for r in rows if any(needle in str(r.get(f) or "").lower() for f in ("street_address", "city", "subdivision", "postal_code", "title"))]
    return {"count": len(rows), "listings": rows}


def owner_listed_for_search(status, city=None, min_price=None, max_price=None, min_beds=None, max_year=None,
                            exclude_session_id=None):
    """Homes investors listed for sale on Staybot (chat or My listings), in the
    same shape as MLS rows, filtered like the MLS query above. Without these,
    an Existing Investor's sale listing reached tenants but never showed up
    for New Property Investors browsing Investing > Deals."""
    if status not in ("active", "all") or max_year is not None:   # no year_built on owner listings
        return []
    rows = db.owner_sale_listings(max_price=max_price, exclude_session_id=exclude_session_id)
    out = []
    for r in rows:
        price = float(r.get("list_price") or 0)
        if min_price is not None and price < min_price:
            continue
        if min_beds is not None and (r.get("bedrooms") or 0) < min_beds:
            continue
        if city and city.lower() not in str(r.get("city") or "").lower():
            continue
        out.append({k: v for k, v in r.items() if k != "owner_session_id"})   # never expose whose session
    return out


def sort_rows(rows, sort):
    if sort == "newest":
        return sorted(rows, key=lambda r: str(r.get("listing_date") or ""), reverse=True)
    return sorted(rows, key=lambda r: float(r.get("list_price") or 0), reverse=(sort == "price_desc"))


@router.get("/stats")
def stats():
    configured()
    rows = q(db._get, "mls_listings", {"select": "city,status_label,list_price,bedrooms,living_area,year_built", "limit": "5000"})
    rows = list(rows or []) + db.owner_sale_listings()   # investor-listed homes count too (city filter, totals)
    prices = sorted(float(r["list_price"]) for r in rows if r["list_price"] is not None)
    cities = {}
    for row in rows:
        city = cities.setdefault(row["city"] or "Unknown", {"count": 0, "prices": []})
        city["count"] += 1
        if row["list_price"] is not None:
            city["prices"].append(float(row["list_price"]))
    return {
        "total": len(rows),
        "statuses": {s: sum(r["status_label"] == s for r in rows) for s in sorted({r["status_label"] for r in rows})},
        "price_low": prices[0] if prices else None, "price_high": prices[-1] if prices else None,
        "price_middle": prices[len(prices) // 2] if prices else None,
        "cities": sorted(({"city": name, "count": c["count"],
                           "price_middle": sorted(c["prices"])[len(c["prices"]) // 2] if c["prices"] else None}
                          for name, c in cities.items()), key=lambda x: -x["count"]),
        "last_import": (q(db._get, "mls_imports", {"select": "*", "order": "created_at.desc", "limit": "1"}) or [None])[0],
    }


@router.get("/{list_number}")
def one(list_number: str):
    configured()
    rows = q(db._get, "mls_listings", {"list_number": f"eq.{list_number}", "select": "*"})
    if rows:
        return rows[0]
    owner_listed = db.get_owner_sale_listing(list_number)
    if owner_listed:
        return {k: v for k, v in owner_listed.items() if k != "owner_session_id"}
    raise HTTPException(404, "Listing not found.")
