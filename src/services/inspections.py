"""Inspection assistance: inspectors, booking, private reports, findings and repairs."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID
import secrets

import requests
from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.services import db, file_store, purchase_offers, whatsapp
from src.services.viewings import check_slot, resolve_date, resolve_time, slot_label

router = APIRouter(prefix="/inspections", tags=["Inspection assistance"])

MAX_UPLOAD = 20 * 1024 * 1024


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_acquisition_workflows.sql to use inspection assistance.")


def q(fn, *args, **kwargs):
    configured()
    try:
        return fn(*args, **kwargs)
    except requests.RequestException as error:
        text = getattr(getattr(error, "response", None), "text", "") or str(error)
        if "PGRST205" in text or "does not exist" in text:
            raise HTTPException(503, "Run supabase_acquisition_workflows.sql in Supabase to enable inspection assistance.")
        print(f"Inspection database error: {text[:500]}")
        raise HTTPException(503, "Supabase is currently unavailable. Check the database connection and try again.")


def actor(staff):
    return f"staff:{(staff or 'unnamed').strip()[:120]}"


def get_one(table, params, missing="Not found"):
    rows = q(db._get, table, {"select": "*", **params, "limit": "1"})
    if not rows:
        raise HTTPException(404, missing)
    return rows[0]


class InspectorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    license_number: str = Field(min_length=1, max_length=120)
    areas_covered: str = Field(min_length=1, max_length=1000)
    price: str | None = Field(default=None, max_length=80)
    turnaround: str | None = Field(default=None, max_length=200)
    contact: str = Field(min_length=1, max_length=300)
    active: bool = True


class BookingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offer_id: UUID | None = None
    deal_id: UUID | None = None
    inspector_id: UUID
    proposed_slots: list[dict] = Field(min_length=2, max_length=3)
    listing_agent_name: str | None = Field(default=None, max_length=200)
    listing_agent_contact: str | None = Field(default=None, max_length=300)
    access_confirmed: bool = False
    fee_payer: Literal["investor", "seller", "broker", "other", "unknown"] = "unknown"
    note: str = Field(default="", max_length=2000)


class ConfirmBooking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot_index: int = Field(ge=0, le=2)
    access_confirmed: bool
    note: str = Field(default="", max_length=2000)


class FindingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Literal["major", "minor"]
    summary: str = Field(min_length=1, max_length=1000)
    report_quote: str = Field(min_length=1, max_length=3000)
    source_page: str | None = Field(default=None, max_length=80)


class RepairIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str = Field(min_length=1, max_length=2000)
    cost_amount: str | None = None
    cost_source: Literal["staff_quote", "client_approved_table", "none"] = "none"
    due_date: str | None = None


class RepairResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seller_response: str = Field(min_length=1, max_length=2000)
    agreed: bool
    agreed_amount: str | None = None




@router.get("/options")
def options():
    configured()
    offers = q(db._get, "acquisition_offers", {"order": "updated_at.desc", "limit": "300", "select": "id,reference,property_title,status,investor_name,investor_phone,current_version,state_version"})
    deals = q(db._get, "investment_deals", {"order": "updated_at.desc", "limit": "300", "select": "id,label,status"})
    inspectors = q(db._get, "inspectors", {"active": "eq.true", "order": "name.asc", "limit": "200", "select": "*"})
    return {"offers": offers, "deals": deals, "inspectors": inspectors}

@router.get("/inspectors")
def list_inspectors(active: bool | None = None):
    params = {"order": "name.asc", "select": "*", "limit": "200"}
    if active is not None:
        params["active"] = f"eq.{str(active).lower()}"
    return q(db._get, "inspectors", params)


@router.post("/inspectors")
def create_inspector(body: InspectorIn, x_staybot_staff: str | None = Header(default=None)):
    return q(db._post, "inspectors", {**body.model_dump(), "created_by": actor(x_staybot_staff)})


@router.post("/inspectors/bulk")
def bulk_import_inspectors(rows: list[dict], x_staybot_staff: str | None = Header(default=None)):
    """Import trusted inspectors independently so one bad row does not fail the batch."""
    results = []
    for row_number, raw in enumerate(rows, start=1):
        license_number = raw.get("license_number") if isinstance(raw, dict) else None
        try:
            inspector = InspectorIn.model_validate(raw)
        except ValidationError as exc:
            reason = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            results.append({
                "row": row_number,
                "license_number": license_number,
                "status": "rejected",
                "reason": reason,
            })
            continue

        fields = {**inspector.model_dump(), "updated_at": datetime.now(timezone.utc).isoformat()}
        try:
            existing = q(db._get, "inspectors", {
                "license_number": f"eq.{inspector.license_number}",
                "select": "id",
                "limit": "1",
            })
            if existing:
                updated = q(db._patch, "inspectors", fields, {"id": f"eq.{existing[0]['id']}"})
                row = updated[0] if isinstance(updated, list) and updated else updated
                results.append({
                    "row": row_number,
                    "license_number": inspector.license_number,
                    "status": "updated-duplicate",
                    "inspector": row,
                })
            else:
                created = q(db._post, "inspectors", {**fields, "created_by": actor(x_staybot_staff)})
                row = created[0] if isinstance(created, list) and created else created
                results.append({
                    "row": row_number,
                    "license_number": inspector.license_number,
                    "status": "created",
                    "inspector": row,
                })
        except Exception as exc:
            results.append({
                "row": row_number,
                "license_number": inspector.license_number,
                "status": "rejected",
                "reason": str(exc)[:500],
            })

    summary = {
        "total": len(results),
        "created": sum(item["status"] == "created" for item in results),
        "updated_duplicate": sum(item["status"] == "updated-duplicate" for item in results),
        "rejected": sum(item["status"] == "rejected" for item in results),
    }
    return {"results": results, "summary": summary}


@router.patch("/inspectors/{inspector_id}")
def update_inspector(inspector_id: UUID, body: InspectorIn, x_staybot_staff: str | None = Header(default=None)):
    row = q(db._patch, "inspectors", {**body.model_dump(), "updated_at": datetime.now(timezone.utc).isoformat()}, {"id": f"eq.{inspector_id}"})
    if not row:
        raise HTTPException(404, "Inspector not found.")
    return row[0] if isinstance(row, list) else row


def validate_slots(slots):
    if len(slots) < 2 or len(slots) > 3:
        raise ValueError("Offer two or three inspection slots.")
    out = []
    for item in slots:
        day = resolve_date(item.get("date_text"), item.get("date"))
        at = resolve_time(item.get("time_text"), item.get("time"))
        if not day or not at:
            raise ValueError("Each inspection slot needs a valid date and time.")
        problem = check_slot(day, at)
        if problem:
            raise ValueError(problem)
        out.append({"date": day.isoformat(), "time": at.strftime("%H:%M"), "label": slot_label(day, at)})
    if len({(x["date"], x["time"]) for x in out}) != len(out):
        raise ValueError("Inspection slots must be different.")
    return out


@router.get("/bookings")
def list_bookings():
    rows = q(db._get, "inspection_bookings", {"order": "created_at.desc", "limit": "300", "select": "*"})
    for row in rows:
        ins = q(db._get, "inspectors", {"id": f"eq.{row['inspector_id']}", "select": "name", "limit": "1"})
        row["inspector_name"] = ins[0]["name"] if ins else None
        if row.get("offer_id"):
            offer = q(db._get, "acquisition_offers", {"id": f"eq.{row['offer_id']}", "select": "reference,property_title,investor_name", "limit": "1"})
            if offer:
                row["offer_reference"] = offer[0].get("reference")
                row["property_title"] = offer[0].get("property_title")
                row["investor_name"] = offer[0].get("investor_name")
    return rows


@router.get("/bookings-by-offer/{offer_id}")
def list_bookings_by_offer(offer_id: UUID):
    rows = q(db._get, "inspection_bookings", {"offer_id": f"eq.{offer_id}", "order": "created_at.desc", "select": "*"})
    for row in rows:
        ins = q(db._get, "inspectors", {"id": f"eq.{row['inspector_id']}", "select": "name", "limit": "1"})
        row["inspector_name"] = ins[0]["name"] if ins else None
        offer = q(db._get, "acquisition_offers", {"id": f"eq.{offer_id}", "select": "reference,property_title,investor_name", "limit": "1"})
        if offer:
            row["offer_reference"] = offer[0].get("reference")
            row["property_title"] = offer[0].get("property_title")
            row["investor_name"] = offer[0].get("investor_name")
    return rows


@router.post("/bookings")
def create_booking(body: BookingIn, x_staybot_staff: str | None = Header(default=None)):
    if not body.offer_id and not body.deal_id:
        raise HTTPException(400, "Link the inspection to an offer or an investment deal.")
    try:
        slots = validate_slots(body.proposed_slots)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    inspector = get_one("inspectors", {"id": f"eq.{body.inspector_id}", "active": "eq.true"}, "Inspector not found or inactive.")
    offer = None
    deal_id = body.deal_id
    if body.offer_id:
        offer = purchase_offers.get_offer(body.offer_id)
        # Inspection is the next acquisition step only after offer negotiations
        # are finalized. Keep the existing inspection workflow, but do not
        # allow a draft/negotiating offer to bypass the purchase-offer step.
        if offer["status"] != "accepted":
            raise HTTPException(409, "Inspection can start only after the purchase offer is accepted.")
        deal_id = deal_id or (UUID(offer["deal_id"]) if offer.get("deal_id") else None)
    row = q(db._post, "inspection_bookings", {
        "offer_id": str(body.offer_id) if body.offer_id else None, "deal_id": str(deal_id) if deal_id else None,
        "inspector_id": str(body.inspector_id), "proposed_slots": slots, "listing_agent_name": body.listing_agent_name,
        "listing_agent_contact": body.listing_agent_contact, "access_confirmed": body.access_confirmed,
        "fee_payer": body.fee_payer, "status": "proposed", "note": body.note.strip(), "created_by": actor(x_staybot_staff),
    })
    return {"booking": row, "inspector": inspector}


@router.post("/bookings/{booking_id}/confirm")
def confirm_booking(booking_id: UUID, body: ConfirmBooking, x_staybot_staff: str | None = Header(default=None)):
    booking = get_one("inspection_bookings", {"id": f"eq.{booking_id}"}, "Inspection booking not found.")
    if booking["status"] in ("confirmed", "cancelled", "completed"):
        raise HTTPException(409, "This inspection booking is already closed.")
    slots = booking.get("proposed_slots") or []
    if body.slot_index >= len(slots):
        raise HTTPException(400, "Choose one of the proposed slots.")
    chosen = slots[body.slot_index]
    if not body.access_confirmed:
        raise HTTPException(400, "Confirm that access has been confirmed with the listing agent.")
    day = datetime.fromisoformat(chosen["date"]).date()
    from datetime import time as dtime
    hour, minute = [int(x) for x in chosen["time"].split(":")]
    at = dtime(hour, minute)
    problem = check_slot(day, at)
    if problem:
        raise HTTPException(400, problem)
    updated = q(db._patch, "inspection_bookings", {
        "confirmed_date": day.isoformat(), "confirmed_time": chosen["time"], "access_confirmed": True,
        "status": "confirmed", "confirmation_note": body.note.strip(), "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }, {"id": f"eq.{booking_id}", "status": "eq.proposed"})
    if not updated:
        raise HTTPException(409, "The booking changed before it could be confirmed. Refresh and try again.")
    row = updated[0] if isinstance(updated, list) else updated

    # The booking is already confirmed at this point. WhatsApp delivery is
    # best-effort and must never make a successful confirmation fail.
    offer = None
    deal = None
    investor_phone = None
    try:
        if row.get("offer_id"):
            offer = purchase_offers.get_offer(UUID(row["offer_id"]))
            investor_phone = offer.get("investor_phone")

        if not investor_phone and row.get("deal_id"):
            deals = q(db._get, "investment_deals", {
                "id": f"eq.{row['deal_id']}",
                "select": "id,label,investor_id",
                "limit": "1",
            })
            deal = deals[0] if deals else None
            investor_id = (deal or {}).get("investor_id")
            if investor_id:
                investors = q(db._get, "investor_profiles", {
                    "id": f"eq.{investor_id}",
                    "select": "whatsapp",
                    "limit": "1",
                })
                investor_phone = investors[0].get("whatsapp") if investors else None
    except Exception as exc:
        print(f"Inspection WhatsApp lookup skipped: {str(exc)[:300]}")

    if investor_phone:
        inspector = get_one("inspectors", {"id": f"eq.{row['inspector_id']}"})
        property_title = ((offer or {}).get("property_title")
                          or (deal or {}).get("label")
                          or "the property")
        text = (f"Inspection booked for {property_title} on {day:%B %d, %Y} at {at.strftime('%I:%M %p')} "
                f"with {inspector['name']}. Access has been confirmed with the listing agent.")
        whatsapp.send_text(investor_phone, text)
    return row


def extract_pdf_text(data: bytes):
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages[:20]:
            pages.append(page.extract_text() or "")
        text = "\n\n".join(pages).strip()
        if not text:
            return {"status": "manual_required", "text": ""}
        return {"status": "extracted", "text": text[:40000]}
    except Exception as exc:
        return {"status": "manual_required", "text": "", "error": str(exc)[:200]}


@router.post("/reports")
async def upload_report(
    offer_id: UUID | None = None,
    deal_id: UUID | None = None,
    inspector_id: UUID | None = None,
    file: UploadFile = File(...),
    x_staybot_staff: str | None = Header(default=None),
):
    if not offer_id and not deal_id:
        raise HTTPException(400, "Link the report to an offer or an investment deal.")
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(400, "Inspection reports must be PDF files.")
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Inspection report is too large.")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "The uploaded file is not a valid PDF.")
    digest = file_store.sha256(data)
    path = f"inspections/{offer_id or deal_id}/reports/{digest[:20]}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{secrets.token_hex(6)}.pdf"
    file_store.put(path, data, "application/pdf")
    extracted = extract_pdf_text(data)
    row = q(db._post, "inspection_reports", {
        "offer_id": str(offer_id) if offer_id else None, "deal_id": str(deal_id) if deal_id else None,
        "inspector_id": str(inspector_id) if inspector_id else None, "file_name": file.filename or "inspection.pdf",
        "storage_path": path, "sha256": digest, "size_bytes": len(data), "extraction_status": extracted["status"],
        "uploaded_by": actor(x_staybot_staff),
    })
    return {"report": row, "extraction": {"status": extracted["status"], "text_preview": extracted["text"][:12000]}}


@router.get("/reports/{report_id}/file")
def download_report(report_id: UUID):
    report = get_one("inspection_reports", {"id": f"eq.{report_id}"}, "Inspection report not found.")
    data = file_store.get(report["storage_path"])
    return Response(data, media_type="application/pdf", headers={"Cache-Control": "no-store", "Content-Disposition": f'inline; filename="{report["file_name"].replace(chr(34), "_")}"'})


@router.post("/reports/{report_id}/findings")
def add_finding(report_id: UUID, body: FindingIn, x_staybot_staff: str | None = Header(default=None)):
    get_one("inspection_reports", {"id": f"eq.{report_id}"}, "Inspection report not found.")
    return q(db._post, "inspection_findings", {**body.model_dump(), "report_id": str(report_id), "created_by": actor(x_staybot_staff)})


@router.get("/reports/{report_id}/findings")
def list_findings(report_id: UUID):
    get_one("inspection_reports", {"id": f"eq.{report_id}"}, "Inspection report not found.")
    return q(db._get, "inspection_findings", {"report_id": f"eq.{report_id}", "order": "severity.desc, id.asc", "select": "*"})


def build_summary(offer, report, findings):
    property_title = offer.get("property_title") if offer else "the property"
    lines = [f"Inspection summary for {property_title}.", "", "This summary is based only on the inspector's report. The report remains the source of truth.", ""]
    majors = [f for f in findings if f["severity"] == "major"]
    minors = [f for f in findings if f["severity"] == "minor"]
    lines.append("Major findings:")
    if majors:
        for f in majors:
            lines.append(f"• {f['summary']} — report quote: “{f['report_quote']}”" + (f" (page {f['source_page']})" if f.get("source_page") else ""))
    else:
        lines.append("• None recorded by staff.")
    lines.append("")
    lines.append("Minor findings:")
    if minors:
        for f in minors:
            lines.append(f"• {f['summary']} — report quote: “{f['report_quote']}”" + (f" (page {f['source_page']})" if f.get("source_page") else ""))
    else:
        lines.append("• None recorded by staff.")
    lines += ["", "No repair cost estimate has been inferred. Any repair amount shown in the app comes from a staff-entered real quote or a client-approved cost table."]
    return "\n".join(lines)


@router.post("/reports/{report_id}/publish-summary")
def publish_summary(report_id: UUID):
    report = get_one("inspection_reports", {"id": f"eq.{report_id}"}, "Inspection report not found.")
    findings = q(db._get, "inspection_findings", {"report_id": f"eq.{report_id}", "order": "id.asc", "select": "*"})
    if not findings:
        raise HTTPException(400, "Add at least one report finding before sending the summary.")
    offer = purchase_offers.get_offer(UUID(report["offer_id"])) if report.get("offer_id") else None
    if not offer or not offer.get("investor_phone"):
        raise HTTPException(409, "A linked offer with the investor's WhatsApp number is required to send the summary.")
    text = build_summary(offer, report, findings)
    sent = whatsapp.send_text(offer["investor_phone"], text)
    failed = any(x.get("error") for x in sent)
    status = "failed" if failed else "accepted"
    q(db._post, "inspection_summary_deliveries", {"report_id": str(report_id), "channel": "whatsapp", "status": status, "message": text})
    return {"sent": not failed, "message": text, "dry_run": whatsapp.DRY_RUN}


@router.get("/repairs/{offer_id}")
def list_repairs(offer_id: UUID):
    return q(db._get, "inspection_repairs", {"offer_id": f"eq.{offer_id}", "order": "id.asc", "select": "*"})


@router.post("/repairs/{offer_id}")
def create_repair(offer_id: UUID, body: RepairIn, x_staybot_staff: str | None = Header(default=None)):
    offer = purchase_offers.get_offer(offer_id)
    deadlines = q(db._get, "acquisition_offer_deadlines", {"offer_id": f"eq.{offer_id}", "kind": "eq.due_diligence_end", "status": "eq.active", "select": "*", "limit": "1"})
    if deadlines and datetime.now(timezone.utc) >= datetime.fromisoformat(deadlines[0]["due_at"].replace("Z", "+00:00")):
        raise HTTPException(409, "The due-diligence deadline has passed; the broker must handle any further repair request.")
    if body.cost_amount and body.cost_source == "none":
        raise HTTPException(400, "A repair cost needs a real staff quote or a client-approved cost table.")
    return q(db._post, "inspection_repairs", {"offer_id": str(offer_id), "request": body.request, "cost_amount": body.cost_amount,
                                               "cost_source": body.cost_source, "requested_by": actor(x_staybot_staff), "status": "requested"})


@router.patch("/repairs/{repair_id}/response")
def respond_repair(repair_id: int, body: RepairResponse, x_staybot_staff: str | None = Header(default=None)):
    repair = get_one("inspection_repairs", {"id": f"eq.{repair_id}"}, "Repair request not found.")
    offer = purchase_offers.get_offer(UUID(repair["offer_id"]))
    deadlines = q(db._get, "acquisition_offer_deadlines", {"offer_id": f"eq.{offer['id']}", "kind": "eq.due_diligence_end", "status": "eq.active", "select": "*", "limit": "1"})
    if deadlines and datetime.now(timezone.utc) >= datetime.fromisoformat(deadlines[0]["due_at"].replace("Z", "+00:00")):
        raise HTTPException(409, "The due-diligence deadline has passed; record this with the broker instead.")
    if body.agreed_amount and not body.agreed:
        raise HTTPException(400, "An agreed repair amount requires agreed=yes.")
    row = q(db._patch, "inspection_repairs", {"seller_response": body.seller_response, "agreed": body.agreed,
                                               "agreed_amount": body.agreed_amount, "responded_by": actor(x_staybot_staff),
                                               "responded_at": datetime.now(timezone.utc).isoformat(), "status": "agreed" if body.agreed else "rejected"},
            {"id": f"eq.{repair_id}", "status": "eq.requested"})
    if not row:
        raise HTTPException(409, "That repair request was already answered. Refresh the deal.")
    return row[0] if isinstance(row, list) else row
