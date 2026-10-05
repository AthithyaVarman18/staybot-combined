"""End-to-end smoke test for the Item 3 + Item 4 acquisition workflow.

This script intentionally exercises the running FastAPI app against a real
configured Supabase database. It creates uniquely tagged DEMO data and leaves
those rows in place for inspection after the run.

Prerequisites:
  1. Configure SUPABASE_URL and SUPABASE_SERVICE_KEY in .env.
  2. Run the existing schema/property/investor/deal/viewing SQL migrations and
     supabase_acquisition_workflows.sql.
  3. Start Staybot, normally at http://127.0.0.1:8000.
  4. Keep WHATSAPP_DRY_RUN=true so the smoke test never sends a real message.
  5. Install development requirements so ReportLab is available.

Example:
  python dev/smoke_test_acquisition.py --password "$ADMIN_PASSWORD"

The current runtime deliberately does not allow a new offer-linked inspection
booking once an offer is terminal. Therefore the smoke flow creates the
inspection booking/report/repair while the offer is still negotiating, and
records the final accepted event after those inspection checks. This keeps the
smoke test faithful to the existing workflow instead of changing runtime rules
just to satisfy test ordering.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from src.services import db  # noqa: E402


def iso_date(days_from_today: int) -> str:
    return (date.today() + timedelta(days=days_from_today)).isoformat()


def iso_dt(hours_from_now: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).isoformat(timespec="seconds")


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def make_pdf() -> bytes:
    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=LETTER)
    pdf.drawString(72, 720, "DEMO inspection report — smoke test")
    pdf.drawString(72, 700, "Major: roof flashing is damaged.")
    pdf.drawString(72, 680, "Minor: kitchen cabinet hinge is loose.")
    pdf.drawString(72, 660, "This PDF is synthetic test data, not a real inspection report.")
    pdf.save()
    return out.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default=os.getenv("ADMIN_USERNAME", "team"))
    parser.add_argument("--password", default=os.getenv("ADMIN_PASSWORD", ""))
    parser.add_argument("--platform", choices=("zipform", "dotloop", "skyslope", "other"), default="zipform")
    parser.add_argument("--handoff-link", default="https://broker-platform.example.test/smoke-handoff")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    if not db.ENABLED:
        print("FAIL 0 Database: SUPABASE_URL / SUPABASE_SERVICE_KEY are not configured.")
        return 1

    session = requests.Session()
    session.headers.update({"X-Staybot-Staff": "smoke-test"})
    if args.password:
        session.auth = (args.username, args.password)

    tag = uuid.uuid4().hex[:10]
    property_id = f"smoke-sale-{tag}"
    investor_email = f"smoke-{tag}@example.test"
    investor_phone = "+1555" + "01" + tag[:7]
    closing_date = iso_date(20)
    due_diligence_end = iso_date(10)
    financing_deadline = iso_date(15)
    expires_at = iso_dt(48)
    report_pdf = make_pdf()

    def call(method: str, path: str, **kwargs):
        url = path if path.startswith("http") else base + path
        response = session.request(method, url, timeout=60, **kwargs)
        if not response.ok:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:500]}")
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response

    def step(number: str, label: str, fn):
        try:
            result = fn()
            print(f"PASS {number} — {label}")
            return result
        except Exception as exc:  # pragma: no cover - the CLI itself is being verified
            print(f"FAIL {number} — {label}: {exc}")
            return False

    demo = step("1", "Create DEMO sale property and investor", lambda: _create_property_and_investor(property_id, investor_email, investor_phone, tag))
    if not demo:
        return 1

    investor = demo["investor"]
    offer = step("2", "Create purchase offer with valid terms", lambda: call("POST", "/purchase-offers", json={
        "property_id": property_id,
        "deal_id": None,
        "investor_id": investor["id"],
        "investor_name": investor["name"],
        "investor_email": investor["email"],
        "investor_phone": investor["phone"],
        "terms": {
            "price": "295000",
            "earnest_money": "10000",
            "due_diligence_fee": "5000",
            "due_diligence_period_days": 10,
            "due_diligence_end": due_diligence_end,
            "financing_deadline": financing_deadline,
            "closing_date": closing_date,
            "expires_at": expires_at,
            "financing_contingency": "Loan approval",
            "included": "Existing appliances",
            "currency": "USD",
        },
    }))
    if not offer:
        return 1
    offer_id = offer["id"]
    version = int(offer["current_version"])

    link = step("3", "Investor approval state and summary PDF", lambda: _approval_flow(call, offer_id))
    if not link:
        return 1

    current = call("GET", f"/purchase-offers/{offer_id}")["offer"]
    version = int(current["state_version"])
    handoff = step("4", "Record broker platform handoff", lambda: call("POST", f"/purchase-offers/{offer_id}/handoff", json={
        "expected_version": version,
        "platform": args.platform,
        "link": args.handoff_link,
        "status": "handed_off",
        "note": "Smoke-test handoff.",
    }))
    if not handoff:
        return 1

    current = call("GET", f"/purchase-offers/{offer_id}")["offer"]
    version = int(current["state_version"])
    counter = step("5", "Record negotiation counter-offer", lambda: call("POST", f"/purchase-offers/{offer_id}/negotiation", json={
        "expected_version": version,
        "event_type": "counter",
        "actor_type": "seller",
        "actor_id": "smoke-seller",
        "amount": "305000",
        "note": "DEMO smoke-test seller counter.",
        "terms": {},
    }))
    if not counter:
        return 1

    deadlines = step("6", "Verify all four expected deadline rows", lambda: _verify_deadlines(
        call("GET", f"/purchase-offers/{offer_id}/deadlines"),
        due_diligence_end, financing_deadline, closing_date, expires_at,
    ))
    if not deadlines:
        return 1

    inspector = step("7a", "Create DEMO inspector", lambda: call("POST", "/inspections/inspectors", json={
        "name": f"DEMO Inspector {tag}",
        "license_number": f"NC-SMOKE-{tag}",
        "areas_covered": "Wake, Durham",
        "price": "450",
        "turnaround": "24 hours",
        "contact": "smoke-inspector@example.test",
        "active": True,
    }))
    if not inspector:
        return 1
    inspector_id = inspector["id"]

    current = call("GET", f"/purchase-offers/{offer_id}")["offer"]
    booking = step("7b", "Book inspection with two proposed slots", lambda: call("POST", "/inspections/bookings", json={
        "offer_id": offer_id,
        "inspector_id": inspector_id,
        "proposed_slots": [
            {"date": iso_date(3), "time": "10:00"},
            {"date": iso_date(3), "time": "11:00"},
        ],
        "listing_agent_name": "DEMO Listing Agent",
        "listing_agent_contact": "agent@example.test",
        "access_confirmed": False,
        "fee_payer": "investor",
        "note": "DEMO smoke-test inspection booking.",
    }))
    if not booking:
        return 1
    booking_id = booking["booking"]["id"]

    confirmed = step("7c", "Confirm one inspection slot", lambda: call("POST", f"/inspections/bookings/{booking_id}/confirm", json={
        "slot_index": 0,
        "access_confirmed": True,
        "note": "DEMO access confirmed.",
    }))
    if not confirmed:
        return 1

    uploaded = step("8a", "Upload a real DEMO inspection PDF to private storage", lambda: call("POST", f"/inspections/reports?offer_id={offer_id}&inspector_id={inspector_id}",
        files={"file": ("DEMO-inspection.pdf", report_pdf, "application/pdf")}))
    if not uploaded:
        return 1
    report_id = uploaded["report"]["id"]

    major = step("8b", "Add major finding from report", lambda: call("POST", f"/inspections/reports/{report_id}/findings", json={
        "severity": "major", "summary": "Roof flashing is damaged", "report_quote": "Damaged flashing observed.", "source_page": "1",
    }))
    if not major:
        return 1
    minor = step("8c", "Add minor finding from report", lambda: call("POST", f"/inspections/reports/{report_id}/findings", json={
        "severity": "minor", "summary": "Kitchen cabinet hinge is loose", "report_quote": "Hinge is loose.", "source_page": "1",
    }))
    if not minor:
        return 1

    summary = step("8d", "Publish inspection summary and verify WhatsApp dry-run outbox", lambda: call("POST", f"/inspections/reports/{report_id}/publish-summary"))
    if not summary:
        return 1
    outbox = call("GET", "/whatsapp/outbox")
    if not any("Inspection summary for" in (item.get("text") or "") and "DEMO" in (item.get("text") or "") for item in outbox):
        print("FAIL 8d — Expected inspection summary not found in WhatsApp dry-run outbox")
        return 1
    print("PASS 8d — Inspection summary appears in WhatsApp dry-run outbox")

    repair = step("9a", "Create repair request tied to the offer", lambda: call("POST", f"/inspections/repairs/{offer_id}", json={
        "request": "Replace damaged roof flashing.",
        "cost_amount": "750.00",
        "cost_source": "staff_quote",
    }))
    if not repair:
        return 1
    repair_id = repair["id"] if isinstance(repair, dict) and "id" in repair else repair[0]["id"]

    response = step("9b", "Record seller response to repair request", lambda: call("PATCH", f"/inspections/repairs/{repair_id}/response", json={
        "seller_response": "Seller agrees to the requested repair.",
        "agreed": True,
        "agreed_amount": "750.00",
    }))
    if not response:
        return 1

    current = call("GET", f"/purchase-offers/{offer_id}")["offer"]
    version = int(current["state_version"])
    accepted = step("9c", "Record final accepted event", lambda: call("POST", f"/purchase-offers/{offer_id}/negotiation", json={
        "expected_version": version,
        "event_type": "accepted",
        "actor_type": "seller",
        "actor_id": "smoke-seller",
        "amount": "305000",
        "note": "DEMO smoke-test seller accepted the counter.",
        "terms": {},
    }))
    if not accepted:
        return 1

    final_offer = call("GET", f"/purchase-offers/{offer_id}")["offer"]
    if final_offer.get("status") != "accepted":
        print(f"FAIL 9c — Expected accepted status, got {final_offer.get('status')}")
        return 1
    print("PASS 9c — Final offer status is accepted")

    print(f"PASS — acquisition smoke test completed for {offer_id} (tag {tag})")
    return 0


def _verify_deadlines(rows, due_diligence_end: str, financing_deadline: str, closing_date: str, expires_at: str):
    expected_dates = {
        "due_diligence_end": due_diligence_end,
        "financing_deadline": financing_deadline,
        "closing_date": closing_date,
    }
    kinds = {row["kind"]: row for row in rows}
    if set(expected_dates) | {"offer_expiration"} != set(kinds):
        raise RuntimeError(f"Deadline kinds mismatch: {sorted(kinds)}")
    for kind, expected_date in expected_dates.items():
        if kinds[kind]["due_at"][:10] != expected_date:
            raise RuntimeError(f"{kind} due_at mismatch: {kinds[kind]['due_at']} != {expected_date}")
    if abs((parse_dt(kinds["offer_expiration"]["due_at"]) - parse_dt(expires_at)).total_seconds()) > 2:
        raise RuntimeError("offer_expiration due_at does not match offer expiry")
    return rows


def _create_property_and_investor(property_id: str, investor_email: str, investor_phone: str, tag: str):
    investor = db._post("investors", {
        "name": f"DEMO Smoke Investor {tag}",
        "email": investor_email,
        "phone": investor_phone,
        "status": "active",
    })
    db._post("properties", {
        "id": property_id,
        "ref": f"SMOKE-{tag}",
        "title": f"DEMO Smoke Sale Home {tag}",
        "area": "Raleigh",
        "city": "Raleigh",
        "location": "Raleigh, NC",
        "listing_type": "sale",
        "property_type": "single-family",
        "bedrooms": 3,
        "bathrooms": 2,
        "sale_price": 310000,
        "status": "active",
        "description": "DEMO smoke-test property — not a real listing.",
    })
    return {"investor": investor, "property_id": property_id}


def _approval_flow(call, offer_id):
    response = call("POST", f"/purchase-offers/{offer_id}/send-approval")
    url = response["url"]
    if not url.rstrip("/").split("/")[-1]:
        raise RuntimeError("Approval URL did not contain a token.")
    token = url.rstrip("/").split("/")[-1]
    state = call("GET", f"/purchase-approval/{token}/api/state")
    if int(state["version"]["version_number"]) != int(response["version"]):
        raise RuntimeError("Approval-page version does not match the generated summary version.")
    pdf = call("GET", f"/purchase-approval/{token}/api/summary.pdf")
    if not pdf.content.startswith(b"%PDF-"):
        raise RuntimeError("Approval summary was not a PDF.")
    decision = call("POST", f"/purchase-approval/{token}/api/decision", json={
        "decision": "approved", "note": "DEMO smoke-test approval.", "reviewed_summary": True,
    })
    if decision.get("decision", {}).get("decision") not in ("approved", None):
        raise RuntimeError(f"Unexpected approval decision response: {decision}")
    return response


if __name__ == "__main__":
    raise SystemExit(main())
