"""Staff-managed offer terms, investor approval, broker handoff and negotiation tracking.

North Carolina boundary: this module never creates a purchase contract. It only
collects terms, creates a plain review summary, records investor approval of that
summary, stores a handoff link/status for the broker's contract platform, and
tracks negotiations/deadlines.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import os
import secrets
import threading
from typing import Literal
from uuid import UUID

import requests
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.services import db, file_store, purchase_offer_pdf, whatsapp

router = APIRouter(prefix="/purchase-offers", tags=["Purchase offers"])
public_router = APIRouter(prefix="/purchase-approval", tags=["Purchase offer approval"], include_in_schema=False)

MAX_PDF = 4 * 1024 * 1024
LINK_DAYS = 7
PLATFORMS = ("unselected", "zipform", "dotloop", "skyslope", "other")
STATUSES = ("draft", "awaiting_investor_approval", "investor_approved", "handed_off", "negotiating", "accepted", "rejected", "withdrawn", "expired")
EVENT_TYPES = ("offer", "counter", "accepted", "rejected", "withdrawn", "expired")
TERMINAL = {"accepted", "rejected", "withdrawn", "expired"}

_worker_started = False
_worker_lock = threading.Lock()
_worker_stop = threading.Event()


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_acquisition_workflows.sql to use purchase offer workflows.")


def q(fn, *args, **kwargs):
    configured()
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except requests.RequestException as error:
        text = getattr(getattr(error, "response", None), "text", "") or str(error)
        upper = text.upper()
        if "PGRST205" in text or "DOES NOT EXIST" in upper:
            raise HTTPException(503, "Run supabase_acquisition_workflows.sql in Supabase to enable purchase offer workflows.")
        # Postgres exceptions raised by the optimistic-concurrency RPCs arrive as
        # P0001 JSON/text errors. Preserve their business meaning instead of
        # incorrectly reporting them as a database outage.
        rpc_errors = {
            "VERSION_CONFLICT": (409, "This offer changed before your action was saved. Refresh and try again."),
            "OFFER_NOT_FOUND": (404, "Purchase offer not found."),
            "VERSION_NOT_FOUND": (404, "Offer version not found."),
            "OFFER_NOT_EDITABLE": (409, "This offer version cannot be changed in its current state."),
            "OFFER_CLOSED": (409, "This negotiation is already closed."),
            "OUTDATED_APPROVAL_VERSION": (409, "This approval link is for an older offer version."),
            "APPROVAL_NOT_OPEN": (409, "This offer is not waiting for investor approval."),
        }
        for marker, (status, message) in rpc_errors.items():
            if marker in upper:
                raise HTTPException(status, message)
        print(f"Purchase offer database error: {text[:500]}")
        raise HTTPException(503, "Supabase is currently unavailable. Check the database connection and try again.")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def actor(staff: str | None = None):
    return f"staff:{(staff or 'unnamed').strip()[:120]}"


def parse_dt(value: str | None):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def money(value, field, *, allow_zero=True):
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a number.")
    if amount < (Decimal("0") if allow_zero else Decimal("0.01")) or amount > Decimal("100000000"):
        raise ValueError(f"{field} must be between 0 and 100,000,000.")
    return f"{amount:.2f}"


class Terms(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price: str
    earnest_money: str | None = None
    due_diligence_fee: str | None = None
    due_diligence_period_days: int | None = Field(default=None, ge=0, le=3650)
    due_diligence_end: date | None = None
    closing_date: date
    closing_time: time | None = None
    financing_contingency: str | None = Field(default=None, max_length=1000)
    included: str | None = Field(default=None, max_length=2000)
    financing_deadline: date | None = None
    expires_at: datetime
    currency: Literal["USD"] = "USD"

    @field_validator("price", mode="before")
    @classmethod
    def price_valid(cls, v):
        value = money(v, "Offer price", allow_zero=False)
        return value

    @field_validator("earnest_money", "due_diligence_fee", mode="before")
    @classmethod
    def fee_valid(cls, v, info):
        return money(v, info.field_name.replace("_", " "))

    @field_validator("expires_at", mode="before")
    @classmethod
    def expiry_valid(cls, v):
        dt = parse_dt(str(v)) if v else None
        if not dt:
            raise ValueError("Offer expiration is required.")
        return dt

    @model_validator(mode="after")
    def dates_valid(self):
        today = datetime.now(timezone.utc).date()
        if self.closing_date < today:
            raise ValueError("Closing date must be today or later.")
        if self.due_diligence_period_days is not None and self.due_diligence_end is None:
            raise ValueError("Due diligence end is required when a due diligence period is provided.")
        if self.due_diligence_end and self.due_diligence_end < today:
            raise ValueError("Due diligence end cannot be in the past.")
        if self.due_diligence_end and self.due_diligence_end > self.closing_date:
            raise ValueError("Due diligence end cannot be after the closing date.")
        if self.financing_deadline and self.financing_deadline < today:
            raise ValueError("Financing deadline cannot be in the past.")
        if self.financing_deadline and self.financing_deadline > self.closing_date:
            raise ValueError("Financing deadline cannot be after the closing date.")
        if self.expires_at <= datetime.now(timezone.utc):
            raise ValueError("Offer expiration must be in the future.")
        return self

    def snapshot(self):
        data = self.model_dump(mode="json")
        if data.get("expires_at"):
            data["expires_at"] = parse_dt(data["expires_at"]).isoformat()
        return data


class OfferCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_id: str = Field(min_length=1, max_length=200)
    deal_id: UUID | None = None
    investor_id: UUID | None = None
    investor_name: str = Field(min_length=1, max_length=200)
    investor_email: str | None = Field(default=None, max_length=200)
    investor_phone: str | None = Field(default=None, max_length=40)
    terms: Terms


class OfferTermsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    terms: Terms


class HandoffIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    platform: Literal["unselected", "zipform", "dotloop", "skyslope", "other"]
    link: str | None = Field(default=None, max_length=2000)
    status: Literal["pending", "handed_off", "active", "completed"] = "handed_off"
    note: str = Field(default="", max_length=2000)


class NegotiationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    event_type: Literal["offer", "counter", "accepted", "rejected", "withdrawn", "expired"]
    actor_type: Literal["buyer", "seller", "broker", "staff"]
    actor_id: str | None = Field(default=None, max_length=200)
    amount: str | None = None
    note: str = Field(default="", max_length=2000)
    terms: dict = Field(default_factory=dict)

    @field_validator("amount", mode="before")
    @classmethod
    def amount_valid(cls, v):
        return money(v, "Negotiation amount")


class GenerateLinkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    expires_at: str
    version: int
    delivery: dict


def get_one(table, params, missing="Not found"):
    rows = q(db._get, table, {"select": "*", **params, "limit": "1"})
    if not rows:
        raise HTTPException(404, missing)
    return rows[0]


def get_offer(offer_id: UUID):
    return get_one("acquisition_offers", {"id": f"eq.{offer_id}"}, "Purchase offer not found.")


def get_terms_version(offer_id, version):
    return get_one("acquisition_offer_versions", {"offer_id": f"eq.{offer_id}", "version_number": f"eq.{version}"}, "Offer version not found.")


def property_data(property_id):
    rows = q(db._get, "properties", {"id": f"eq.{property_id}", "select": "*", "limit": "1"})
    if not rows:
        raise HTTPException(404, "Sale property not found.")
    if rows[0].get("listing_type") != "sale":
        raise HTTPException(400, "Purchase offers can only be created for sale properties.")
    return rows[0]


def investor_data(investor_id: UUID | None):
    if not investor_id:
        return None
    return get_one("investors", {"id": f"eq.{investor_id}"}, "Investor not found.")


def snapshot_for_offer(offer, version):
    property_row = property_data(offer["property_id"])
    investor = None
    if offer.get("investor_id"):
        investor = investor_data(offer["investor_id"])
    investor = investor or {"name": offer.get("investor_name"), "email": offer.get("investor_email"), "phone": offer.get("investor_phone")}
    handoffs = q(db._get, "acquisition_offer_handoffs", {
        "offer_id": f"eq.{offer['id']}", "order": "created_at.desc", "limit": "1", "select": "platform,status,platform_link"
    })
    handoff = handoffs[0] if handoffs else {}
    return {
        "reference": offer["reference"],
        "property": property_row,
        "investor": {"name": investor.get("name"), "email": investor.get("email"), "phone": investor.get("phone") or investor.get("whatsapp")},
        "terms": version["terms"],
        "handoff": {"platform": handoff.get("platform"), "status": handoff.get("status"), "link": handoff.get("platform_link")},
    }


def content_hash(payload):
    import json
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def render_version_pdf(offer, version):
    summary = snapshot_for_offer(offer, version)
    data = purchase_offer_pdf.render(summary, version=int(version["version_number"]), generated_at=now_iso())
    return data, file_store.sha256(data)


def create_access_link(offer_id, version_row, investor_id):
    raw = secrets.token_urlsafe(48)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    expiry = datetime.now(timezone.utc) + timedelta(days=LINK_DAYS)
    # A new approval request replaces any still-active link for this exact version.
    q(db._patch, "acquisition_offer_access_links", {"revoked_at": now_iso()}, {
        "offer_id": f"eq.{offer_id}", "version_id": f"eq.{version_row['id']}", "revoked_at": "is.null"
    })
    q(db._post, "acquisition_offer_access_links", {
        "offer_id": str(offer_id), "version_id": str(version_row["id"]), "version_number": int(version_row["version_number"]),
        "actor_id": str(investor_id) if investor_id else None, "token_hash": digest, "expires_at": expiry.isoformat(),
    })
    return raw, expiry


def approval_message(offer, url):
    return (
        f"Hi {offer.get('investor_name') or 'there'}, this is Staybot. Your purchase-offer summary is ready for review.\n\n"
        f"Review the summary here (private link, expires in {LINK_DAYS} days):\n{url}\n\n"
        "The document is a review summary, not a contract. The broker/attorney's approved contract platform handles the actual purchase contract."
    )


def send_text_or_record(phone, text):
    if not phone:
        return {"status": "not_sent", "reason": "Investor WhatsApp number is missing."}
    try:
        result = whatsapp.send_text(phone, text)
    except Exception as exc:
        return {"status": "failed", "detail": str(exc)[:500]}
    failed = [x for x in result if x.get("error")]
    if failed:
        return {"status": "failed", "detail": failed[-1].get("error")}
    return {"status": "accepted", "dry_run": whatsapp.DRY_RUN}


def _whatsapp_window_closed(error_text):
    """Return True only for Meta's 131047 closed 24-hour window error."""
    return "131047" in str(error_text or "")


def send_deadline_reminder(phone, text, offer, deadline):
    """Try free-form first; use an approved template only for Meta 131047."""
    delivery = send_text_or_record(phone, text)
    if delivery.get("status") != "failed" or not _whatsapp_window_closed(delivery.get("detail")):
        return delivery

    template_name = (os.getenv("WHATSAPP_DEADLINE_REMINDER_TEMPLATE") or "").strip()
    if not template_name:
        return delivery

    language = (
        os.getenv("WHATSAPP_DEADLINE_REMINDER_TEMPLATE_LANGUAGE")
        or os.getenv("WHATSAPP_TEMPLATE_LANGUAGE")
        or "en_US"
    ).strip()
    due = parse_dt(deadline.get("due_at"))
    components = [{
        "type": "body",
        "parameters": [
            {"type": "text", "text": offer.get("reference") or "offer"},
            {"type": "text", "text": deadline.get("label") or "deadline"},
            {"type": "text", "text": due.strftime("%B %d, %Y") if due else "soon"},
        ],
    }]
    template_result = whatsapp.send_template(phone, template_name, language, components)
    failed = [item for item in template_result if item.get("error")]
    if failed:
        return {"status": "failed", "detail": failed[-1].get("error"), "channel": "template"}
    return {
        "status": "accepted",
        "dry_run": whatsapp.DRY_RUN,
        "channel": "template",
        "template": template_name,
    }


def create_pdf_document(offer, version, data, sha):
    path = f"purchase-offers/{offer['id']}/v{version['version_number']}-{secrets.token_hex(8)}.pdf"
    file_store.put(path, data, "application/pdf")
    row = q(db._post, "acquisition_offer_documents", {
        "offer_id": offer["id"], "version_id": version["id"], "kind": "review_summary_pdf",
        "file_name": f"Summary-{offer['reference']}-v{version['version_number']}.pdf", "storage_path": path,
        "sha256": sha, "size_bytes": len(data), "created_by": "system",
    })
    return row


def _rpc(name, body):
    return q(db._post, f"rpc/{name}", body)




@router.get("/options")
def options():
    """Lists used by the staff UI; no legal or pricing advice is returned."""
    configured()
    sale_properties = q(db._get, "properties", {"listing_type": "eq.sale", "status": "neq.hidden", "order": "title.asc", "limit": "500", "select": "id,ref,title,city,area,sale_price"})
    investors = q(db._get, "investors", {"status": "eq.active", "order": "name.asc", "limit": "500", "select": "id,name,email,phone"})
    deals = q(db._get, "investment_deals", {"order": "updated_at.desc", "limit": "300", "select": "id,label,status,list_number,person_id"})
    configured_platform = (os.getenv("BROKER_CONTRACT_PLATFORM") or "unselected").strip().lower()
    if configured_platform not in PLATFORMS:
        configured_platform = "unselected"
    return {"properties": sale_properties, "investors": investors, "deals": deals, "broker_platform": configured_platform}

@router.get("/by-deal/{deal_id}")
def offer_summary_by_deal(deal_id: UUID):
    """Return the latest offer workflow state for a deal, or an empty result."""
    rows = q(db._get, "acquisition_offers", {
        "deal_id": f"eq.{deal_id}", "order": "updated_at.desc", "limit": "1", "select": "*"
    })
    if not rows:
        return {"offer": None, "handoff": None, "deadlines": []}
    offer = rows[0]
    handoffs = q(db._get, "acquisition_offer_handoffs", {
        "offer_id": f"eq.{offer['id']}", "order": "created_at.desc", "limit": "1", "select": "platform,status,platform_link,note,created_at"
    })
    deadlines = q(db._get, "acquisition_offer_deadlines", {
        "offer_id": f"eq.{offer['id']}", "status": "eq.active", "order": "due_at.asc", "select": "kind,label,due_at,remind_at,status"
    })
    return {
        "offer": {
            "id": offer["id"], "reference": offer["reference"], "status": offer["status"],
            "current_version": offer["current_version"], "state_version": offer["state_version"],
            "updated_at": offer["updated_at"], "property_title": offer.get("property_title"),
            "investor_name": offer.get("investor_name")
        },
        "handoff": handoffs[0] if handoffs else None,
        "deadlines": deadlines
    }


@router.get("")
def list_offers(status: str | None = None, limit: int = 100):
    configured()
    params = {"select": "*", "order": "updated_at.desc", "limit": str(max(1, min(limit, 200)))}
    if status:
        if status not in STATUSES:
            raise HTTPException(400, "Unknown offer status.")
        params["status"] = f"eq.{status}"
    return q(db._get, "acquisition_offers", params)


@router.get("/{offer_id}")
def offer_detail(offer_id: UUID):
    offer = get_offer(offer_id)
    versions = q(db._get, "acquisition_offer_versions", {"offer_id": f"eq.{offer_id}", "order": "version_number.desc", "select": "*"})
    events = q(db._get, "acquisition_offer_events", {"offer_id": f"eq.{offer_id}", "order": "sequence.asc", "select": "*"})
    deadlines = q(db._get, "acquisition_offer_deadlines", {"offer_id": f"eq.{offer_id}", "order": "due_at.asc", "select": "*"})
    handoff = q(db._get, "acquisition_offer_handoffs", {"offer_id": f"eq.{offer_id}", "order": "id.desc", "limit": "1", "select": "*"})
    approvals = q(db._get, "acquisition_offer_approvals", {"offer_id": f"eq.{offer_id}", "order": "created_at.desc", "select": "*"})
    notifications = q(db._get, "acquisition_offer_notifications", {"offer_id": f"eq.{offer_id}", "order": "created_at.desc", "select": "*"})
    return {"offer": offer, "versions": versions, "events": events, "deadlines": deadlines, "handoff": handoff[0] if handoff else None, "approvals": approvals, "notifications": notifications}


@router.post("")
def create_offer(body: OfferCreate, x_staybot_staff: str | None = Header(default=None)):
    prop = property_data(body.property_id)
    investor = investor_data(body.investor_id)
    name = (body.investor_name or (investor or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Investor name is required.")
    row = q(db._post, "acquisition_offers", {
        "reference": f"PO-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3).upper()}",
        "deal_id": str(body.deal_id) if body.deal_id else None,
        "property_id": body.property_id,
        "property_ref": prop.get("ref"),
        "property_title": prop.get("title"),
        "investor_id": str(body.investor_id) if body.investor_id else None,
        "investor_name": name,
        "investor_email": body.investor_email or (investor or {}).get("email"),
        "investor_phone": body.investor_phone or (investor or {}).get("phone") or (investor or {}).get("whatsapp"),
        "status": "draft", "current_version": 1, "state_version": 1,
        "created_by": actor(x_staybot_staff),
    })
    version_payload = body.terms.snapshot()
    version = q(db._post, "acquisition_offer_versions", {
        "offer_id": row["id"], "version_number": 1, "terms": version_payload,
        "content_hash": content_hash(version_payload), "created_by": actor(x_staybot_staff),
    })
    _rpc("acquisition_offer_rebuild_deadlines", {"p_offer_id": row["id"], "p_version": 1})
    _rpc("acquisition_offer_record_event", {"p_offer_id": row["id"], "p_expected_state_version": 1, "p_event_type": "offer",
                                          "p_actor_type": "staff", "p_actor_id": actor(x_staybot_staff), "p_amount": version_payload["price"],
                                          "p_terms": version_payload, "p_note": "Offer prepared.", "p_status_after": "draft"})
    return {**row, "current_version": 1, "version": version}


@router.patch("/{offer_id}/terms")
def update_terms(offer_id: UUID, body: OfferTermsUpdate, x_staybot_staff: str | None = Header(default=None)):
    offer = get_offer(offer_id)
    if offer["status"] in TERMINAL or offer["status"] in ("handed_off", "negotiating"):
        raise HTTPException(409, "This offer is already in broker negotiation or closed and cannot be silently changed. Record the next negotiation response instead.")
    version_payload = body.terms.snapshot()
    try:
        result = _rpc("acquisition_offer_update_terms", {
            "p_offer_id": str(offer_id), "p_expected_state_version": body.expected_version,
            "p_terms": version_payload, "p_actor_id": actor(x_staybot_staff), "p_content_hash": content_hash(version_payload),
        })
    except HTTPException as exc:
        raise exc
    _rpc("acquisition_offer_rebuild_deadlines", {"p_offer_id": str(offer_id), "p_version": result["current_version"]})
    return result


@router.post("/{offer_id}/send-approval", response_model=GenerateLinkResponse)
def send_for_approval(offer_id: UUID, request: Request, x_staybot_staff: str | None = Header(default=None)):
    offer = get_offer(offer_id)
    if offer["status"] not in ("draft", "awaiting_investor_approval"):
        raise HTTPException(409, "This offer is not in a state where investor approval can be requested.")
    version = get_terms_version(offer_id, offer["current_version"])
    data, sha = render_version_pdf(offer, version)
    create_pdf_document(offer, version, data, sha)
    raw, expiry = create_access_link(offer_id, version, offer.get("investor_id"))
    base = str(request.base_url).rstrip("/")
    url = f"{base}/purchase-approval/{raw}"
    phone = offer.get("investor_phone")
    delivery = send_text_or_record(phone, approval_message(offer, url))
    q(db._post, "acquisition_offer_approvals", {"offer_id": str(offer_id), "version_id": version["id"], "decision": "pending", "actor_id": offer.get("investor_id"), "note": "Approval requested."})
    updated = q(db._patch, "acquisition_offers", {"status": "awaiting_investor_approval"}, {"id": f"eq.{offer_id}", "state_version": f"eq.{offer['state_version']}"})
    if not updated:
        raise HTTPException(409, "This offer changed before the approval request was sent. Refresh the offer and try again.")
    return GenerateLinkResponse(url=url, expires_at=expiry.isoformat(), version=int(version["version_number"]), delivery=delivery)


@router.post("/{offer_id}/handoff")
def handoff(offer_id: UUID, body: HandoffIn, x_staybot_staff: str | None = Header(default=None)):
    offer = get_offer(offer_id)
    if body.platform == "unselected":
        raise HTTPException(400, "Select the broker's contract platform before handoff.")
    if body.status in ("handed_off", "active", "completed") and not body.link:
        raise HTTPException(400, "Save the broker platform link before marking the handoff active.")
    if offer["status"] not in ("investor_approved", "handed_off", "negotiating"):
        raise HTTPException(409, "Investor approval is required before broker handoff.")
    updated = _rpc("acquisition_offer_record_handoff", {"p_offer_id": str(offer_id), "p_expected_state_version": body.expected_version,
                                                     "p_platform": body.platform, "p_link": body.link, "p_status": body.status,
                                                     "p_actor_id": actor(x_staybot_staff), "p_note": body.note.strip()})
    return updated


@router.post("/{offer_id}/negotiation")
def negotiation(offer_id: UUID, body: NegotiationIn):
    offer = get_offer(offer_id)
    if offer["status"] in TERMINAL:
        raise HTTPException(409, "This negotiation is already closed.")
    current_terms = get_terms_version(offer_id, offer["current_version"])
    expires_at = (current_terms.get("terms") or {}).get("expires_at")
    if expires_at and parse_dt(expires_at) <= datetime.now(timezone.utc):
        if offer["status"] not in TERMINAL:
            try:
                _rpc("acquisition_offer_record_event", {
                    "p_offer_id": str(offer_id), "p_expected_state_version": offer["state_version"],
                    "p_event_type": "expired", "p_actor_type": "staff", "p_actor_id": "system:expiry-check",
                    "p_amount": (current_terms.get("terms") or {}).get("price"), "p_terms": current_terms.get("terms") or {},
                    "p_note": "Offer expiration deadline passed.", "p_status_after": "expired"
                })
            except HTTPException as exc:
                if exc.status_code not in (409,):
                    raise
        raise HTTPException(409, "The offer has expired and cannot receive another negotiation response.")
    if body.event_type != "offer" and offer["status"] not in ("investor_approved", "handed_off", "negotiating"):
        raise HTTPException(409, "Investor approval and broker handoff are required before recording negotiation responses.")
    if body.event_type in ("counter", "accepted", "rejected", "withdrawn", "expired") and not body.note.strip() and body.event_type != "accepted":
        raise HTTPException(400, "Add a note describing the response.")
    status_after = {"offer": "negotiating", "counter": "negotiating", "accepted": "accepted", "rejected": "rejected",
                    "withdrawn": "withdrawn", "expired": "expired"}[body.event_type]
    terms_version = current_terms
    amount = body.amount or (terms_version.get("terms") or {}).get("price")
    if amount is None and body.event_type in ("offer", "counter", "accepted"):
        raise HTTPException(400, "Record an amount for this negotiation step.")
    result = _rpc("acquisition_offer_record_event", {"p_offer_id": str(offer_id), "p_expected_state_version": body.expected_version,
                                                 "p_event_type": body.event_type, "p_actor_type": body.actor_type,
                                                 "p_actor_id": body.actor_id or actor(None), "p_amount": amount,
                                                 "p_terms": body.terms, "p_note": body.note.strip(), "p_status_after": status_after})
    return result


@router.post("/{offer_id}/withdraw")
def withdraw(offer_id: UUID, expected_version: int = 1, x_staybot_staff: str | None = Header(default=None)):
    body = NegotiationIn(expected_version=expected_version, event_type="withdrawn", actor_type="staff", note="Offer withdrawn by staff.")
    return negotiation(offer_id, body)


@router.get("/{offer_id}/summary.pdf")
def summary_pdf(offer_id: UUID):
    offer = get_offer(offer_id)
    version = get_terms_version(offer_id, offer["current_version"])
    data, _ = render_version_pdf(offer, version)
    return Response(data, media_type="application/pdf", headers={"Cache-Control": "no-store", "Content-Disposition": f'inline; filename="Summary-{offer["reference"]}-v{version["version_number"]}.pdf"'})


@public_router.get("/{token}")
def approval_page(token: str):
    page = FileResponse(__import__("pathlib").Path(__file__).resolve().parents[1] / "static" / "purchase_approval.html", headers={"Cache-Control": "no-store"})
    return page


def resolve_link(token):
    if not token or len(token) > 200:
        raise HTTPException(404, "This approval link is not valid.")
    rows = q(db._get, "acquisition_offer_access_links", {"token_hash": f"eq.{hashlib.sha256(token.encode()).hexdigest()}", "select": "*", "limit": "1"})
    if not rows:
        raise HTTPException(404, "This approval link is not valid.")
    link = rows[0]
    if link.get("revoked_at"):
        raise HTTPException(410, "This approval link has been replaced.")
    if parse_dt(link["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(410, "This approval link has expired.")
    offer = get_offer(UUID(link["offer_id"]))
    return link, offer


@public_router.get("/{token}/api/state")
def approval_state(token: str):
    link, offer = resolve_link(token)
    version = get_terms_version(offer["id"], int(link["version_number"]))
    if int(offer["current_version"]) != int(link["version_number"]):
        raise HTTPException(409, "This summary version has been replaced. Request a new approval link.")
    prop = property_data(offer["property_id"])
    return {"offer": offer, "version": version, "property": prop, "expires_at": link["expires_at"]}


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "changes_requested"]
    note: str = Field(default="", max_length=2000)
    reviewed_summary: bool


@public_router.get("/{token}/api/summary.pdf")
def approval_pdf(token: str):
    link, offer = resolve_link(token)
    if int(offer["current_version"]) != int(link["version_number"]):
        raise HTTPException(409, "This summary version has been replaced. Request a new approval link.")
    version = get_terms_version(offer["id"], int(link["version_number"]))
    data, _ = render_version_pdf(offer, version)
    return Response(data, media_type="application/pdf", headers={"Cache-Control": "no-store", "Content-Disposition": f'inline; filename="Summary-{offer["reference"]}-v{version["version_number"]}.pdf"'})


@public_router.post("/{token}/api/decision")
def approval_decision(token: str, body: ApprovalDecision):
    link, offer = resolve_link(token)
    version = get_terms_version(offer["id"], int(link["version_number"]))
    terms = version.get("terms") or {}
    if terms.get("expires_at") and parse_dt(terms["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(409, "The offer has expired and cannot be approved.")
    if offer["status"] not in ("awaiting_investor_approval", "investor_approved"):
        raise HTTPException(409, "This offer is not waiting for investor approval.")
    if body.decision == "approved" and not body.reviewed_summary:
        raise HTTPException(400, "Please confirm that you reviewed the summary before approving it.")
    if body.decision == "changes_requested" and not body.note.strip():
        raise HTTPException(400, "Tell the team what should change.")
    decision = _rpc("acquisition_offer_record_approval", {"p_offer_id": offer["id"], "p_version_id": link["version_id"],
                                                       "p_actor_id": link.get("actor_id"), "p_decision": body.decision,
                                                       "p_note": body.note.strip()})
    return {"recorded": True, "decision": decision}


@router.get("/{offer_id}/deadlines")
def deadlines(offer_id: UUID):
    return q(db._get, "acquisition_offer_deadlines", {"offer_id": f"eq.{offer_id}", "order": "due_at.asc", "select": "*"})


def expire_due_offers(now=None):
    """Move open offers to expired when their offer-expiration deadline passes."""
    if not db.ENABLED:
        return 0
    now = now or datetime.now(timezone.utc)
    rows = db._get("acquisition_offer_deadlines", {
        "status": "eq.active", "kind": "eq.offer_expiration", "due_at": f"lte.{now.isoformat()}", "select": "*", "limit": "100"
    })
    expired = 0
    for deadline in rows:
        offer = get_offer(UUID(deadline["offer_id"]))
        if offer["status"] in TERMINAL:
            continue
        version = get_terms_version(offer["id"], offer["current_version"])
        try:
            _rpc("acquisition_offer_record_event", {
                "p_offer_id": offer["id"], "p_expected_state_version": offer["state_version"],
                "p_event_type": "expired", "p_actor_type": "staff", "p_actor_id": "system:deadline-worker",
                "p_amount": (version.get("terms") or {}).get("price"), "p_terms": version.get("terms") or {},
                "p_note": "Offer expiration deadline passed.", "p_status_after": "expired"
            })
            expired += 1
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
    return expired


def run_due_reminders_once():
    if not db.ENABLED:
        return {"processed": 0, "sent": 0, "skipped": 0, "expired": 0}
    now = datetime.now(timezone.utc).isoformat()
    expired_count = expire_due_offers(datetime.now(timezone.utc))
    rows = db._get("acquisition_offer_deadlines", {"status": "eq.active", "remind_at": f"lte.{now}", "reminder_sent_at": "is.null", "select": "*", "limit": "100"})
    processed = sent = skipped = 0
    for deadline in rows:
        processed += 1
        claimed = db._patch("acquisition_offer_deadlines", {"reminder_claimed_at": now}, {"id": f"eq.{deadline['id']}", "reminder_sent_at": "is.null", "reminder_claimed_at": "is.null"})
        if not claimed:
            skipped += 1
            continue
        offer = get_offer(UUID(deadline["offer_id"]))
        due = parse_dt(deadline["due_at"])
        text = (f"Reminder: {deadline['label']} for {offer['reference']} is due "
                f"{due.strftime('%B %d, %Y')}." if due else f"Reminder: {deadline['label']} is due soon.")
        text += " Please coordinate with the broker/team before the deadline."
        staff_notice = q(db._post, "acquisition_offer_notifications", {"offer_id": offer["id"], "kind": "deadline", "message": text})
        delivery = send_deadline_reminder(offer.get("investor_phone"), text, offer, deadline)
        db._patch("acquisition_offer_deadlines", {"reminder_sent_at": now, "reminder_status": delivery["status"]}, {"id": f"eq.{deadline['id']}"})
        sent += 1 if delivery["status"] in ("accepted", "not_sent") else 0
    return {"processed": processed, "sent": sent, "skipped": skipped, "expired": expired_count}


def start_deadline_worker():
    global _worker_started
    if _worker_started or (str(__import__("os").environ.get("PURCHASE_DEADLINE_WORKER", "true")).lower() != "true"):
        return
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        def loop():
            while not _worker_stop.wait(60):
                try:
                    run_due_reminders_once()
                except Exception as error:
                    print(f"Purchase deadline worker error: {error}")
        threading.Thread(target=loop, name="purchase-deadline-worker", daemon=True).start()


start_deadline_worker()
