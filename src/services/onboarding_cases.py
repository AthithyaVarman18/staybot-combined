"""
Guided resident onboarding cases (staff side).

Staff start a case -> assign property, tenant, owner and staff member ->
collect details and documents step by step -> agree rental terms -> prepare
an agreement version -> tenant and owner approve that exact version ->
staff review -> final lease PDF -> WhatsApp delivery to both parties.

Every rule that matters (required details, document review, approvals of
the same current version, staff review, no silent overwrite) is enforced here
and again inside the database functions in supabase_lease_onboarding.sql.
All routes are team-only (TeamAuthMiddleware). Tenants and owners use the
token-scoped routes in onboarding_party.py instead.
"""

import hashlib
import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

import requests
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.services import db, file_store, lease_pdf, onboarding_handoff, properties, turbotenant

router = APIRouter(prefix="/onboarding/cases", tags=["Onboarding cases"])

PARTIES = ("tenant", "owner")
STEPS = [
    (1, "home", "Let's choose the home."),
    (2, "tenant", "Let's meet the tenant."),
    (3, "owner", "Let's confirm the owner."),
    (4, "documents", "Let's collect the required documents."),
    (5, "terms", "Let's agree on the rental terms."),
    (6, "review", "Let's review the agreement together."),
    (7, "final", "Your final lease PDF is ready."),
]
STEP_TITLES = {n: title for n, _, title in STEPS}
LINK_DAYS = 7
MAX_UPLOAD = 10 * 1024 * 1024
FILE_TYPES = {"application/pdf": b"%PDF-", "image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}
CONSENT_SOURCES = {
    "customer_messaged_first": "They messaged us on WhatsApp first",
    "opted_in_on_application": "They opted in to WhatsApp messages on their application",
    "confirmed_by_phone_or_in_person": "They agreed by phone or in person (staff confirmed)",
}
LEASE_SETUP = {"digital_copy_whatsapp": "Digital copy on WhatsApp", "printed_copy": "Printed copy", "both": "Digital and printed copy"}
ACCOUNT_SETUP = {"turbotenant_portal": "TurboTenant tenant portal (set up by staff)", "other_payment_method": "Another agreed payment method", "undecided": "Not decided yet"}
STATUS_RANK = {"queued": 0, "sending": 0, "accepted": 1, "sent": 2, "delivered": 3, "read": 4}

ERRORS = {
    "VERSION_CONFLICT": (409, "Someone else updated this case. Refresh to load the latest details, then try again."),
    "CASE_NOT_ACTIVE": (409, "This case is no longer active (it was finalized or cancelled)."),
    "CASE_NOT_FOUND": (404, "Onboarding case not found."),
    "INVALID_STATUS_CHANGE": (400, "That status change is not allowed."),
    "OUTDATED_AGREEMENT_VERSION": (409, "This agreement version is out of date. Please review the latest version."),
    "TENANT_APPROVAL_MISSING": (409, "The tenant has not approved the current agreement version."),
    "OWNER_APPROVAL_MISSING": (409, "The owner has not approved the current agreement version."),
    "STAFF_REVIEW_MISSING": (409, "A staff member must review the current agreement version first."),
    "SIGNING_NOT_AVAILABLE": (409, "This case requires electronic signatures, but no e-signature provider is connected."),
    "DOCUMENTS_NOT_ACCEPTED": (409, "Every required document must be reviewed and accepted first."),
    "PROPERTY_UNAVAILABLE": (409, "This home already has an active tenancy or is marked let/sold."),
    "WRONG_PERSON": (403, "This link does not belong to the person on this case."),
    "onboarding_cases_one_active_per_unit": (409, "An active onboarding case already exists for this home and unit."),
    "onboarding_people_whatsapp_uq": (409, "Someone with this WhatsApp number already exists. Select the existing person."),
    "onboarding_cases_parties_differ": (400, "The tenant and the owner must be different people."),
}


# ---------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------

def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_lease_onboarding.sql to use onboarding cases.")


def translate(error: Exception):
    text = getattr(getattr(error, "response", None), "text", "") or str(error)
    for token, (status, message) in ERRORS.items():
        if token in text:
            return HTTPException(status, message)
    if "PGRST205" in text or "PGRST202" in text or "does not exist" in text:
        return HTTPException(503, "Run supabase_lease_onboarding.sql in Supabase to enable onboarding cases.")
    print(f"Onboarding database error: {text[:500]}")
    return HTTPException(500, "The onboarding database request failed. Please try again.")


def q(fn, *args, **kwargs):
    configured()
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except requests.RequestException as error:
        raise translate(error)
    except Exception as error:
        raise translate(error)


def get(table, params):
    return q(db._get, table, {"select": "*", **params})


def one(table, params, missing="Not found."):
    rows = get(table, {**params, "limit": "1"})
    if not rows:
        raise HTTPException(404, missing)
    return rows[0]


def insert(table, body):
    return q(db._post, table, body)


def patch(table, body, params):
    result = q(db._patch, table, body, params)
    return result or None


def rpc(name, body):
    return q(db._post, f"rpc/{name}", body)


def ids_filter(ids):
    ids = sorted({str(i) for i in ids if i})
    return f"in.({','.join(ids)})" if ids else None


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(case_id, actor, action, details=None):
    try:
        db._post("onboarding_audit_log", {"case_id": str(case_id) if case_id else None, "actor": actor, "action": action, "details": details or {}})
    except Exception as error:   # auditing must never be skipped silently
        print(f"Audit log write failed for {action}: {error}")
        raise HTTPException(500, "Could not write the audit log, so the action was not completed.")


def staff_actor(header_value: str | None) -> str:
    name = (header_value or "").strip()[:120]
    return f"staff:{name}" if name else "staff:unnamed"


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------

def normalize_whatsapp(value: str | None) -> str | None:
    """E.164. US/NC numbers may be typed without +1; other countries need their + code."""
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        candidate = "+" + digits
    elif len(digits) == 10:
        candidate = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        candidate = "+" + digits
    else:
        raise ValueError("Enter a WhatsApp number with country code, e.g. +1 704 555 0101.")
    if not re.fullmatch(r"\+[1-9]\d{7,14}", candidate):
        raise ValueError("Enter a valid WhatsApp number with country code.")
    return candidate


def money(value, field, minimum=Decimal("0")):
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError(f"{field} must be a number.")
    if amount < minimum or amount > Decimal("1000000"):
        raise ValueError(f"{field} must be between {minimum} and 1,000,000.")
    return f"{amount:.2f}"


class Communication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["whatsapp", "whatsapp_and_email"] = "whatsapp"
    best_time: Literal["any", "morning", "afternoon", "evening"] = "any"


class PersonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str = Field(min_length=1, max_length=200)
    whatsapp: str
    email: str | None = Field(default=None, max_length=200)
    preferred_language: Literal["en", "es"] = "en"
    communication: Communication = Field(default_factory=Communication)
    is_test: bool = False

    @field_validator("full_name")
    @classmethod
    def clean_name(cls, v):
        v = " ".join(v.split())
        if not v:
            raise ValueError("Name is required.")
        return v

    @field_validator("whatsapp")
    @classmethod
    def clean_phone(cls, v):
        return normalize_whatsapp(v)

    @field_validator("email")
    @classmethod
    def clean_email(cls, v):
        v = (v or "").strip() or None
        if v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("Enter a valid email address or leave it empty.")
        return v


class Charge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    amount: str
    frequency: Literal["monthly", "one_time", "yearly"]

    @field_validator("amount", mode="before")
    @classmethod
    def clean_amount(cls, v):
        return money(v, "Charge amount")


class Terms(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_start: date | None = None
    lease_end: date | None = None
    move_in_date: date | None = None
    rent: str | None = None
    deposit: str | None = None
    currency: Literal["USD"] = "USD"
    payment_schedule: Literal["monthly", "twice_monthly", "weekly"] | None = None
    rent_due_day: int | None = Field(default=None, ge=1, le=28)
    charges: list[Charge] = Field(default_factory=list, max_length=20)
    occupants: list[str] = Field(default_factory=list, max_length=10)
    pets: str = Field(default="", max_length=200)
    utilities: str = Field(default="", max_length=300)
    additional_terms: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("rent", mode="before")
    @classmethod
    def clean_rent(cls, v):
        return money(v, "Rent", Decimal("0.01"))

    @field_validator("deposit", mode="before")
    @classmethod
    def clean_deposit(cls, v):
        return money(v, "Deposit")

    @field_validator("occupants", "additional_terms", mode="before")
    @classmethod
    def clean_lines(cls, v):
        items = v.splitlines() if isinstance(v, str) else (v or [])
        cleaned = [" ".join(str(x).split()) for x in items if str(x).strip()]
        if any(len(x) > 500 for x in cleaned):
            raise ValueError("Each line must be 500 characters or fewer.")
        return cleaned

    @model_validator(mode="after")
    def check_dates(self):
        if self.lease_start and self.lease_end and self.lease_end <= self.lease_start:
            raise ValueError("Lease end must be after lease start.")
        if self.move_in_date and self.lease_start and self.move_in_date < self.lease_start:
            raise ValueError("Move-in date cannot be before the lease starts.")
        if self.move_in_date and self.lease_end and self.move_in_date > self.lease_end:
            raise ValueError("Move-in date cannot be after the lease ends.")
        return self

    def missing(self):
        labels = {"lease_start": "lease start date", "lease_end": "lease end date", "move_in_date": "move-in date",
                  "rent": "monthly rent", "deposit": "security deposit", "payment_schedule": "payment schedule",
                  "rent_due_day": "rent due day"}
        absent = [label for key, label in labels.items() if getattr(self, key) in (None, "")]
        if not self.occupants:
            absent.append("occupants (at least the tenant)")
        return absent


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_setup: Literal["digital_copy_whatsapp", "printed_copy", "both"] | None = None
    account_setup: Literal["turbotenant_portal", "other_payment_method", "undecided"] | None = None


# ---------------------------------------------------------------------
# Loading and computing the case view
# ---------------------------------------------------------------------

def requirements_for(jurisdiction, property_id, all_reqs):
    chosen = {}
    for r in sorted(all_reqs, key=lambda r: r.get("property_id") is not None):
        if r["jurisdiction"] == jurisdiction and r.get("property_id") in (None, property_id):
            chosen[(r["party"], r["doc_key"])] = r   # property-specific rows override jurisdiction defaults
    return [r for r in chosen.values() if r["active"]]


def load_bundle(cases):
    """Related rows for many cases with a fixed number of queries."""
    case_ids = ids_filter(c["id"] for c in cases)
    people_ids = ids_filter([c["tenant_id"] for c in cases] + [c["owner_id"] for c in cases])
    bundle = {"people": {}, "properties": {}, "staff": {}, "templates": {}, "requirements": [], "documents": [],
              "versions": [], "approvals": [], "finals": [], "messages": [], "progress": [], "escalations": []}
    if not cases:
        return bundle
    if people_ids:
        bundle["people"] = {p["id"]: p for p in get("onboarding_people", {"id": people_ids})}
    bundle["properties"] = {p["id"]: p for p in get("properties", {"id": f"in.({','.join(sorted({c['property_id'] for c in cases}))})"})}
    staff_ids = ids_filter(c["staff_id"] for c in cases)
    if staff_ids:
        bundle["staff"] = {s["id"]: s for s in get("onboarding_staff", {"id": staff_ids})}
    template_ids = ids_filter(c["template_id"] for c in cases)
    if template_ids:
        bundle["templates"] = {t["id"]: t for t in get("lease_templates", {"id": template_ids})}
    bundle["requirements"] = get("onboarding_document_requirements", {})
    for key, table, order in (("documents", "onboarding_case_documents", "created_at.desc"),
                              ("versions", "onboarding_agreement_versions", "version.desc"),
                              ("approvals", "onboarding_approvals", "created_at.desc"),
                              ("finals", "onboarding_final_documents", "created_at.desc"),
                              ("messages", "onboarding_whatsapp_messages", "created_at.asc"),
                              ("progress", "onboarding_party_progress", "updated_at.desc"),
                              ("escalations", "onboarding_escalations", "created_at.desc")):
        params = {"case_id": case_ids, "order": order}
        if key == "versions":
            params["select"] = "id,case_id,version,content_hash,template_id,is_demo,created_by,created_at"
        bundle[key] = get(table, params)
    return bundle


def public_person(p):
    if not p:
        return None
    return {k: p.get(k) for k in ("id", "full_name", "whatsapp", "email", "preferred_language", "communication", "is_test")}


def snapshot_for(case, prop, tenant, owner, template):
    terms = Terms(**(case.get("terms") or {})).model_dump(mode="json")
    return {
        "reference": case["reference"],
        "jurisdiction": case["jurisdiction"],
        "property": {"id": prop["id"], "ref": prop.get("ref"), "title": prop.get("title"),
                     "unit": case.get("unit") or "", "address": case.get("property_address") or ""},
        "tenant": {"id": tenant["id"], "full_name": tenant["full_name"], "whatsapp": tenant.get("whatsapp"), "email": tenant.get("email")},
        "owner": {"id": owner["id"], "full_name": owner["full_name"], "whatsapp": owner.get("whatsapp"), "email": owner.get("email")},
        "terms": terms,
        "template": {"id": template["id"], "name": template["name"], "template_version": template["template_version"],
                     "status": template["status"], "clauses": template["clauses"]},
        "signature_method": case["signature_method"],
    }


def content_hash(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_view(case, bundle):
    cid = case["id"]
    prop = bundle["properties"].get(case["property_id"]) or {"id": case["property_id"], "title": case["property_id"]}
    tenant = bundle["people"].get(case["tenant_id"])
    owner = bundle["people"].get(case["owner_id"])
    staff = bundle["staff"].get(case["staff_id"])
    template = bundle["templates"].get(case["template_id"])
    consent = case.get("consent") or {}
    terms = Terms(**(case.get("terms") or {}))
    prefs = Preferences(**(case.get("preferences") or {}))

    # Documents: latest upload per requirement.
    docs = [d for d in bundle["documents"] if d["case_id"] == cid]
    checklist = []
    for req in sorted(requirements_for(case["jurisdiction"], case["property_id"], bundle["requirements"]),
                      key=lambda r: (r["party"], r["label"])):
        uploads = [d for d in docs if d["party"] == req["party"] and d["doc_key"] == req["doc_key"]]
        latest = uploads[0] if uploads else None
        checklist.append({
            "party": req["party"], "doc_key": req["doc_key"], "label": req["label"], "reason": req["reason"],
            "required": req["required"], "status": latest["status"] if latest else "not_uploaded",
            "latest": {k: latest[k] for k in ("id", "file_name", "status", "review_note", "reviewed_by", "reviewed_at", "uploaded_by", "created_at")} if latest else None,
            "history_count": len(uploads),
        })

    versions = [v for v in bundle["versions"] if v["case_id"] == cid]
    latest_version = versions[0] if versions else None
    agreement_current = False
    current_hash = None
    if tenant and owner and template:
        try:
            current_hash = content_hash(snapshot_for(case, prop, tenant, owner, template))
        except Exception:
            current_hash = None
    if latest_version and current_hash:
        agreement_current = latest_version["content_hash"] == current_hash

    def decision(party):
        if not latest_version:
            return {"status": "pending", "at": None, "note": None, "actor": None}
        rows = [a for a in bundle["approvals"] if a["agreement_version_id"] == latest_version["id"] and a["party"] == party]
        if not rows:
            return {"status": "pending", "at": None, "note": None, "actor": None}
        return {"status": rows[0]["decision"], "at": rows[0]["created_at"], "note": rows[0]["note"], "actor": rows[0]["actor"],
                "channel": rows[0]["channel"]}

    approvals = {p: decision(p) for p in PARTIES}
    final = next((f for f in bundle["finals"] if f["case_id"] == cid), None)
    staff_review = case.get("staff_review") or None
    staff_reviewed = bool(latest_version and staff_review and staff_review.get("agreement_version") == latest_version["version"])

    missing = {n: [] for n, _, _ in STEPS}
    if not (case.get("property_address") or "").strip():
        missing[1].append("property address")
    for step, party, person in ((2, "tenant", tenant), (3, "owner", owner)):
        if not person:
            missing[step].append(f"{party} not assigned")
        else:
            if not person.get("whatsapp"):
                missing[step].append(f"{party} WhatsApp number")
            if not (consent.get(party) or {}).get("whatsapp_opt_in"):
                missing[step].append(f"{party} WhatsApp consent")
    if not staff:
        missing[1].append("responsible staff member")
    for item in checklist:
        if item["required"] and item["status"] != "accepted":
            state = {"not_uploaded": "not uploaded", "awaiting_review": "awaiting staff review",
                     "changes_required": "changes required"}[item["status"]]
            missing[4].append(f"{item['party']} {item['label'].lower()} ({state})")
    missing[5] = terms.missing()
    if not template:
        missing[5].append("lease template")
    if not prefs.lease_setup:
        missing[5].append("lease copy preference")
    if not prefs.account_setup:
        missing[5].append("rent account setup preference")
    if not latest_version:
        missing[6].append("agreement not prepared")
    elif not agreement_current:
        missing[6].append("details changed since the last agreement version (prepare a new version)")
    else:
        for p in PARTIES:
            if approvals[p]["status"] != "approved":
                missing[6].append(f"{p} approval ({approvals[p]['status'].replace('_', ' ')})")
        if not staff_reviewed:
            missing[6].append("staff review of this version")
    if case["signature_method"] == "external_esign":
        missing[6].append("electronic signatures (no e-signature provider connected)")
    msgs = [m for m in bundle["messages"] if m["case_id"] == cid]
    final_msgs = {m["party"]: m for m in msgs if m["kind"] == "final_pdf" and final and m.get("final_document_id") == final["id"]}
    if not final:
        missing[7].append("final lease PDF not generated")
    else:
        for p in PARTIES:
            m = final_msgs.get(p)
            if not m:
                missing[7].append(f"final PDF not sent to {p}")
            elif m["status"] == "failed":
                missing[7].append(f"WhatsApp to {p} failed")

    steps = [{"number": n, "key": key, "title": title, "missing": missing[n], "complete": not missing[n]} for n, key, title in STEPS]
    if case["status"] == "finalized":
        for s in steps[:6]:
            s["complete"], s["missing"] = True, []

    next_actor, next_action = next_step_for(case, missing, checklist, approvals, agreement_current, latest_version, staff_reviewed, final, final_msgs)
    progress = {p["party"]: p for p in bundle["progress"] if p["case_id"] == cid}
    return {
        **{k: case[k] for k in ("id", "reference", "status", "current_step", "property_id", "unit", "property_address",
                                "jurisdiction", "signature_method", "is_test", "version", "created_at", "updated_at",
                                "finalized_at", "conversation_id", "turbotenant", "staff_review")},
        "terms": terms.model_dump(mode="json"),
        "preferences": prefs.model_dump(mode="json"),
        "consent": consent,
        "property": {"id": prop["id"], "ref": prop.get("ref"), "title": prop.get("title"), "status": prop.get("status"),
                     "city": prop.get("city"), "location": prop.get("location"), "owner_name": prop.get("owner_name")},
        "tenant": public_person(tenant),
        "owner": public_person(owner),
        "staff": {"id": staff["id"], "name": staff["name"]} if staff else None,
        "template": {k: template[k] for k in ("id", "name", "status", "template_version", "jurisdiction")} if template else None,
        "steps": steps,
        "completed_steps": sum(s["complete"] for s in steps),
        "missing_count": sum(len(s["missing"]) for s in steps),
        "documents": checklist,
        "agreement": {
            "latest_version": {k: latest_version[k] for k in ("id", "version", "content_hash", "is_demo", "created_at", "created_by")} if latest_version else None,
            "versions": [{"id": v["id"], "version": v["version"], "created_at": v["created_at"], "content_hash": v["content_hash"]} for v in versions],
            "is_current": agreement_current,
            "approvals": approvals,
            "staff_reviewed": staff_reviewed,
            "signing": "not_required_approval_only" if case["signature_method"] == "approval_only" else "e_signature_not_connected",
        },
        "final_document": {k: final[k] for k in ("id", "agreement_version_id", "sha256", "size_bytes", "is_demo", "created_at", "created_by")} if final else None,
        "whatsapp": [{k: m[k] for k in ("id", "kind", "party", "to_phone", "status", "test_mode", "error", "attempts", "status_updated_at", "created_at", "final_document_id")} for m in msgs],
        "party_progress": {p: {k: (progress.get(p) or {}).get(k) for k in ("invited_at", "first_response_at", "self_service_done_at", "staff_help_requested", "confirmed")} for p in PARTIES},
        "open_escalations": [e for e in bundle["escalations"] if e["case_id"] == cid and e["status"] == "open"],
        "next_actor": next_actor,
        "next_action": next_action,
    }


def next_step_for(case, missing, checklist, approvals, agreement_current, latest_version, staff_reviewed, final, final_msgs):
    if case["status"] == "cancelled":
        return "nobody", "Case cancelled."
    if case["status"] == "finalized":
        failed = [p for p, m in final_msgs.items() if m["status"] == "failed"]
        unsent = [p for p in PARTIES if p not in final_msgs]
        if failed:
            return "staff", f"Retry the WhatsApp send to the {' and '.join(failed)}."
        if unsent:
            return "staff", "Send the final lease PDF through WhatsApp."
        waiting = [p for p, m in final_msgs.items() if m["status"] in ("queued", "sending", "accepted", "sent")]
        if waiting:
            return "whatsapp", f"Waiting for WhatsApp delivery confirmation for the {' and '.join(waiting)}."
        if any(m["test_mode"] for m in final_msgs.values()):
            return "nobody", "Onboarding complete in test mode. WhatsApp deliveries were simulated; nothing reached real phones."
        return "nobody", "Onboarding complete."
    for n in (1, 2, 3):
        if missing[n]:
            return "staff", f"Complete step {n}: {STEP_TITLES[n]}"
    if missing[5]:
        return "staff", "Complete step 5: agree on the rental terms."
    uploads_needed = [i for i in checklist if i["required"] and i["status"] in ("not_uploaded", "changes_required")]
    reviews_needed = [i for i in checklist if i["required"] and i["status"] == "awaiting_review"]
    if reviews_needed:
        return "staff", f"Review {len(reviews_needed)} uploaded document(s)."
    if not latest_version or not agreement_current:
        return "staff", "Prepare the agreement for review."
    changes = [p for p in PARTIES if approvals[p]["status"] == "changes_requested"]
    if changes:
        return "staff", f"The {' and '.join(changes)} requested changes. Update the terms and prepare a new version."
    pending = [p for p in PARTIES if approvals[p]["status"] != "approved"]
    if uploads_needed:
        parties = sorted({i["party"] for i in uploads_needed})
        return " & ".join(parties), f"Upload required documents ({', '.join(i['label'] for i in uploads_needed)})."
    if pending:
        return " & ".join(pending), f"Review and approve agreement version {latest_version['version']}."
    if not staff_reviewed:
        return "staff", f"Review agreement version {latest_version['version']}."
    if case["signature_method"] == "external_esign":
        return "staff", "Connect an e-signature provider or switch the case to approval-only."
    return "staff", "Generate the final lease PDF."


def load_case(case_id):
    return one("onboarding_cases", {"id": f"eq.{case_id}"}, "Onboarding case not found.")


def case_view(case_id):
    case = load_case(case_id)
    return build_view(case, load_bundle([case]))


def full_context(case_id):
    """Case row plus the records needed to build the agreement snapshot."""
    case = load_case(case_id)
    bundle = load_bundle([case])
    return case, bundle, build_view(case, bundle)


# ---------------------------------------------------------------------
# Setup and options
# ---------------------------------------------------------------------

@router.get("/setup")
def setup_status():
    from src.services import onboarding_whatsapp
    configured()
    templates = get("lease_templates", {"order": "created_at.desc"})
    return {
        "whatsapp": onboarding_whatsapp.setup_status(),
        "turbotenant": turbotenant.status(),
        "templates": {
            "approved_available": any(t["status"] == "approved" for t in templates),
            "demo_available": any(t["status"] == "demo" for t in templates),
            "message": None if any(t["status"] == "approved" for t in templates) else
            "Template setup required: no staff-approved lease template exists. The DEMO template is for testing only.",
        },
        "e_signature": {"status": "not_connected", "detail": "No e-signature provider is integrated. Cases use approval-only agreements with blank hand-signature lines."},
        "access": {"staff": "Team password (shared login). Staff names in the audit log are selected by the staff member, not verified individually.",
                   "tenants_owners": f"Personal secure links, scoped to one case and one person, expiring after {LINK_DAYS} days."},
    }


@router.get("/options")
def options():
    configured()
    homes = [p for p in get("properties", {"order": "title.asc"}) if p.get("listing_type") == "rent" and p.get("status") not in ("let", "sold")]
    return {
        "properties": [{k: p.get(k) for k in ("id", "ref", "title", "location", "city", "status", "owner_name", "owner_phone", "rent", "deposit")} for p in homes],
        "people": [public_person(p) for p in get("onboarding_people", {"order": "full_name.asc"})],
        "staff": get("onboarding_staff", {"active": "eq.true", "order": "name.asc"}),
        "templates": [{k: t[k] for k in ("id", "name", "status", "template_version", "jurisdiction")} for t in get("lease_templates", {"status": "in.(approved,demo)", "order": "created_at.desc"})],
        "consent_sources": CONSENT_SOURCES,
        "lease_setup": LEASE_SETUP,
        "account_setup": ACCOUNT_SETUP,
    }


@router.post("/people")
def create_person(body: PersonIn, x_staybot_staff: str | None = Header(default=None)):
    configured()
    existing = get("onboarding_people", {"whatsapp": f"eq.{body.whatsapp}"})
    if existing:
        return {**public_person(existing[0]), "reused": True}
    row = insert("onboarding_people", body.model_dump(mode="json"))
    audit(None, staff_actor(x_staybot_staff), "person_created", {"person_id": row["id"]})
    return {**public_person(row), "reused": False}


class StaffIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)


@router.post("/staff")
def create_staff(body: StaffIn):
    configured()
    name = " ".join(body.name.split())
    existing = get("onboarding_staff", {"name": f"eq.{name}"})
    return existing[0] if existing else insert("onboarding_staff", {"name": name})


# ---------------------------------------------------------------------
# Start, list, detail
# ---------------------------------------------------------------------

class NewProperty(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")
    title: str = Field(min_length=3, max_length=160)
    city: str = Field(default="", max_length=80)
    owner_name: str = Field(default="", max_length=200)
    owner_phone: str = Field(default="", max_length=40)


class StartCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    new_property: NewProperty | None = None
    unit: str = Field(default="", max_length=40)
    property_address: str = Field(default="", max_length=300)
    tenant_id: UUID | None = None
    owner_id: UUID | None = None
    staff_id: UUID | None = None
    conversation_id: UUID | None = None
    is_test: bool = False

    @model_validator(mode="after")
    def one_property(self):
        if bool(self.property_id) == bool(self.new_property):
            raise ValueError("Choose an existing property or enter a new one.")
        self.unit = " ".join(self.unit.split())
        return self


@router.post("")
def start_case(body: StartCase, x_staybot_staff: str | None = Header(default=None)):
    configured()
    actor = staff_actor(x_staybot_staff)
    if body.new_property:
        from src.services.viewings import tidy_phone   # +1 form for the owner's number
        if get("properties", {"id": f"eq.{body.new_property.id}"}):
            raise HTTPException(409, "A property with this identifier already exists. Select it instead.")
        prop = insert("properties", {"id": body.new_property.id, "title": body.new_property.title, "city": body.new_property.city or None,
                                     "location": body.property_address or None, "listing_type": "rent", "status": "hidden",
                                     "owner_name": body.new_property.owner_name or None, "owner_phone": tidy_phone(body.new_property.owner_phone)})
        properties.clear_cache()
        audit(None, actor, "property_created_for_onboarding", {"property_id": prop["id"], "status": "hidden"})
    else:
        rows = get("properties", {"id": f"eq.{body.property_id}"})
        if not rows:
            raise HTTPException(400, "That property does not exist. Select a stored property or create a new one.")
        prop = rows[0]
        if prop.get("listing_type") != "rent" or prop.get("status") in ("let", "sold"):
            raise HTTPException(400, "Choose a rental home that is not already let or sold.")
    if body.conversation_id and not q(db.get_lead, str(body.conversation_id)):
        raise HTTPException(404, "Lead not found.")
    duplicate = get("onboarding_cases", {"property_id": f"eq.{prop['id']}", "unit": f"eq.{body.unit}", "status": "eq.active"})
    if duplicate:
        raise HTTPException(409, f"An active onboarding case ({duplicate[0]['reference']}) already exists for this home. Open it instead.")
    for field in ("tenant_id", "owner_id"):
        if getattr(body, field) and not get("onboarding_people", {"id": f"eq.{getattr(body, field)}"}):
            raise HTTPException(400, "Selected person not found.")
    if body.tenant_id and body.tenant_id == body.owner_id:
        raise HTTPException(400, "The tenant and the owner must be different people.")
    templates = get("lease_templates", {"status": "eq.approved", "jurisdiction": "eq.US-NC", "order": "approved_at.desc"}) or \
        get("lease_templates", {"status": "eq.demo", "order": "created_at.desc"})
    row = insert("onboarding_cases", {
        "property_id": prop["id"], "unit": body.unit,
        "property_address": " ".join(body.property_address.split()) or (prop.get("location") or ""),
        "tenant_id": str(body.tenant_id) if body.tenant_id else None, "owner_id": str(body.owner_id) if body.owner_id else None,
        "staff_id": str(body.staff_id) if body.staff_id else None,
        "conversation_id": str(body.conversation_id) if body.conversation_id else None,
        "template_id": templates[0]["id"] if templates else None, "is_test": body.is_test, "created_by": actor,
        "terms": {"currency": "USD", "rent": str(prop["rent"]) if prop.get("rent") else None,
                  "deposit": str(prop["deposit"]) if prop.get("deposit") is not None else None},
    })
    audit(row["id"], actor, "case_started", {"property_id": prop["id"], "unit": body.unit, "from_lead": bool(body.conversation_id)})
    return case_view(row["id"])


@router.get("")
def list_cases(status: Literal["active", "finalized", "cancelled", "all"] = "all"):
    configured()
    params = {"order": "updated_at.desc", "limit": "200"}
    if status != "all":
        params["status"] = f"eq.{status}"
    cases = get("onboarding_cases", params)
    bundle = load_bundle(cases)
    return [build_view(c, bundle) for c in cases]


@router.get("/metrics")
def completion_metrics():
    """WhatsApp self-service completion, excluding test/demo cases."""
    configured()
    real_cases = {c["id"] for c in get("onboarding_cases", {"is_test": "eq.false", "select": "id"})}
    progress = [p for p in get("onboarding_party_progress", {}) if p["case_id"] in real_cases and p.get("invited_at")]
    done = [p for p in progress if p.get("self_service_done_at") and not p.get("staff_help_requested")]
    rate = round(100 * len(done) / len(progress), 1) if progress else None
    return {
        "invited_parties": len(progress), "completed_without_staff": len(done), "completion_rate_percent": rate,
        "target_percent": 80, "test_cases_excluded": True,
        "note": "Measured from real (non-test) cases only. The 80% target can only be judged from real usage.",
    }


@router.get("/{case_id}")
def get_case(case_id: UUID):
    configured()
    return case_view(case_id)


@router.get("/{case_id}/audit")
def case_audit(case_id: UUID):
    configured()
    load_case(case_id)
    return get("onboarding_audit_log", {"case_id": f"eq.{case_id}", "order": "id.desc", "limit": "200"})


# ---------------------------------------------------------------------
# Step saving
# ---------------------------------------------------------------------

class ConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    whatsapp_opt_in: bool
    source: Literal["customer_messaged_first", "opted_in_on_application", "confirmed_by_phone_or_in_person"] | None = None


class StepSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    advance: bool = False
    unit: str | None = Field(default=None, max_length=40)
    property_address: str | None = Field(default=None, max_length=300)
    staff_id: UUID | None = None
    person_id: UUID | None = None
    person: PersonIn | None = None
    consent: ConsentIn | None = None
    terms: Terms | None = None
    preferences: Preferences | None = None
    template_id: UUID | None = None
    signature_method: Literal["approval_only", "external_esign"] | None = None


def update_case(case_id, version, changes, actor, action):
    return rpc("onboarding_update_case", {"p_case_id": str(case_id), "p_expected_version": version,
                                          "p_changes": changes, "p_actor": actor, "p_action": action})


@router.put("/{case_id}/steps/{step}")
def save_step(case_id: UUID, step: int, body: StepSave, x_staybot_staff: str | None = Header(default=None)):
    configured()
    if step not in STEP_TITLES:
        raise HTTPException(404, "Unknown step.")
    actor = staff_actor(x_staybot_staff)
    case = load_case(case_id)
    if case["status"] != "active":
        raise HTTPException(409, ERRORS["CASE_NOT_ACTIVE"][1])
    if case["version"] != body.version:
        raise HTTPException(409, ERRORS["VERSION_CONFLICT"][1])
    changes = {}
    if step == 1:
        if body.unit is not None and " ".join(body.unit.split()) != case["unit"]:
            clash = get("onboarding_cases", {"property_id": f"eq.{case['property_id']}", "unit": f"eq.{' '.join(body.unit.split())}", "status": "eq.active"})
            if clash:
                raise HTTPException(409, ERRORS["onboarding_cases_one_active_per_unit"][1])
            changes["unit"] = " ".join(body.unit.split())
        if body.property_address is not None:
            changes["property_address"] = " ".join(body.property_address.split())
        if body.staff_id:
            one("onboarding_staff", {"id": f"eq.{body.staff_id}"}, "Staff member not found.")
            changes["staff_id"] = str(body.staff_id)
    elif step in (2, 3):
        party = "tenant" if step == 2 else "owner"
        other = case["owner_id"] if party == "tenant" else case["tenant_id"]
        person_id = str(body.person_id) if body.person_id else case[f"{party}_id"]
        if body.person:
            existing = get("onboarding_people", {"whatsapp": f"eq.{body.person.whatsapp}"})
            if existing and existing[0]["id"] != person_id:
                if body.person_id or not person_id:
                    person_id = existing[0]["id"]     # reuse the existing record for this number
                else:
                    raise HTTPException(409, ERRORS["onboarding_people_whatsapp_uq"][1])
            if person_id:
                patch("onboarding_people", {**body.person.model_dump(mode="json", exclude={"is_test"}), "updated_at": now_iso()}, {"id": f"eq.{person_id}"})
                audit(case_id, actor, f"{party}_details_updated", {"person_id": person_id})
            else:
                person_id = insert("onboarding_people", {**body.person.model_dump(mode="json"), "is_test": case["is_test"]})["id"]
                audit(case_id, actor, "person_created", {"person_id": person_id, "party": party})
        elif body.person_id:
            one("onboarding_people", {"id": f"eq.{person_id}"}, "Selected person not found.")
        if person_id and person_id == other:
            raise HTTPException(400, ERRORS["onboarding_cases_parties_differ"][1])
        if person_id != case[f"{party}_id"]:
            changes[f"{party}_id"] = person_id
            consent = dict(case.get("consent") or {})
            consent.pop(party, None)                    # consent belongs to the person, re-record for a new person
            changes["consent"] = consent
            # Links issued to the previous person stop working.
            patch("onboarding_access_links", {"revoked_at": now_iso()}, {"case_id": f"eq.{case_id}", "party": f"eq.{party}", "revoked_at": "is.null"})
        if body.consent is not None:
            if body.consent.whatsapp_opt_in and not body.consent.source:
                raise HTTPException(400, "Choose how the person agreed to receive WhatsApp messages.")
            consent = dict(changes.get("consent", case.get("consent") or {}))
            consent[party] = {"whatsapp_opt_in": body.consent.whatsapp_opt_in, "source": body.consent.source,
                              "recorded_by": actor, "recorded_at": now_iso()} if body.consent.whatsapp_opt_in else {"whatsapp_opt_in": False}
            changes["consent"] = consent
    elif step == 5:
        if body.terms is not None:
            changes["terms"] = body.terms.model_dump(mode="json")
        if body.preferences is not None:
            changes["preferences"] = body.preferences.model_dump(mode="json")
        if body.template_id:
            template = one("lease_templates", {"id": f"eq.{body.template_id}"}, "Template not found.")
            if template["status"] not in ("approved", "demo"):
                raise HTTPException(400, "That template is retired.")
            if template["status"] == "demo" and not case["is_test"]:
                pass   # allowed, but the agreement and PDF are clearly labelled DEMO and setup is flagged
            changes["template_id"] = template["id"]
        if body.signature_method:
            changes["signature_method"] = body.signature_method

    saved = update_case(case_id, body.version, changes, actor, f"step_{step}_saved") if changes else case
    view = case_view(case_id)
    if body.advance:
        blocking = view["steps"][step - 1]["missing"] if step != 4 else []
        if step == 4:
            blocking = []   # documents can continue in parallel with terms; finalization still requires them
        if blocking:
            raise HTTPException(400, "Before continuing, please add: " + "; ".join(blocking))
        target = min(step + 1, 7)
        if target > view["current_step"]:
            update_case(case_id, view["version"], {"current_step": target}, actor, "step_advanced")
        view = case_view(case_id)
    refresh_agreement_if_started(case_id, actor)
    return case_view(case_id) if saved else view


@router.post("/{case_id}/cancel")
def cancel_case(case_id: UUID, x_staybot_staff: str | None = Header(default=None)):
    configured()
    case = load_case(case_id)
    update_case(case_id, case["version"], {"status": "cancelled"}, staff_actor(x_staybot_staff), "case_cancelled")
    return case_view(case_id)


# ---------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------

async def store_upload(case, party, doc_key, request: Request, uploaded_by):
    if case["status"] != "active":
        raise HTTPException(409, ERRORS["CASE_NOT_ACTIVE"][1])
    reqs = requirements_for(case["jurisdiction"], case["property_id"], get("onboarding_document_requirements", {}))
    requirement = next((r for r in reqs if r["party"] == party and r["doc_key"] == doc_key), None)
    if not requirement:
        raise HTTPException(400, "That document is not on this case's checklist.")
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type not in FILE_TYPES:
        raise HTTPException(415, "Upload a PDF, JPEG or PNG file.")
    data = await request.body()
    if not data or len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Files must be between 1 byte and 10 MB.")
    if not data.startswith(FILE_TYPES[content_type]):
        raise HTTPException(415, "The file content does not match its type.")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", (request.headers.get("x-file-name") or f"{doc_key}").strip())[:120] or doc_key
    digest = file_store.sha256(data)
    path = f"cases/{case['id']}/documents/{party}/{doc_key}/{secrets.token_hex(8)}-{digest[:12]}"
    try:
        file_store.put(path, data, content_type)
    except file_store.StorageError as error:
        raise HTTPException(502, str(error))
    row = insert("onboarding_case_documents", {
        "case_id": case["id"], "party": party, "doc_key": doc_key, "file_name": name, "content_type": content_type,
        "size_bytes": len(data), "sha256": digest, "storage_path": path, "status": "awaiting_review", "uploaded_by": uploaded_by,
    })
    audit(case["id"], uploaded_by, "document_uploaded", {"document_id": row["id"], "party": party, "doc_key": doc_key, "sha256": digest})
    return row


@router.post("/{case_id}/uploads/{party}/{doc_key}")
async def staff_upload(case_id: UUID, party: Literal["tenant", "owner"], doc_key: str, request: Request,
                       x_staybot_staff: str | None = Header(default=None)):
    configured()
    row = await store_upload(load_case(case_id), party, doc_key, request, staff_actor(x_staybot_staff))
    return {"document": {k: row[k] for k in ("id", "status", "file_name")}, "case": case_view(case_id)}


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accepted", "changes_required"]
    note: str = Field(default="", max_length=500)


@router.post("/{case_id}/documents/{document_id}/review")
def review_document(case_id: UUID, document_id: UUID, body: Review, x_staybot_staff: str | None = Header(default=None)):
    configured()
    actor = staff_actor(x_staybot_staff)
    if actor == "staff:unnamed":
        raise HTTPException(400, "Choose which staff member is reviewing before accepting or rejecting documents.")
    case = load_case(case_id)
    if case["status"] != "active":
        raise HTTPException(409, ERRORS["CASE_NOT_ACTIVE"][1])
    if body.decision == "changes_required" and not body.note.strip():
        raise HTTPException(400, "Explain what needs to change.")
    updated = patch("onboarding_case_documents",
                    {"status": body.decision, "review_note": body.note.strip() or None, "reviewed_by": actor, "reviewed_at": now_iso()},
                    {"id": f"eq.{document_id}", "case_id": f"eq.{case_id}", "status": "eq.awaiting_review"})
    if not updated:
        raise HTTPException(409, "This document was already reviewed or does not belong to this case. Refresh.")
    audit(case_id, actor, f"document_{body.decision}", {"document_id": str(document_id), "note": body.note.strip() or None})
    return case_view(case_id)


@router.get("/{case_id}/documents/{document_id}/file")
def download_document(case_id: UUID, document_id: UUID, x_staybot_staff: str | None = Header(default=None)):
    configured()
    doc = one("onboarding_case_documents", {"id": f"eq.{document_id}", "case_id": f"eq.{case_id}"}, "Document not found.")
    data = file_store.get(doc["storage_path"])
    audit(case_id, staff_actor(x_staybot_staff), "document_downloaded", {"document_id": str(document_id)})
    return Response(data, media_type=doc["content_type"], headers={
        "Content-Disposition": f'attachment; filename="{doc["file_name"]}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------------
# Agreement versions, approvals, staff review
# ---------------------------------------------------------------------

def agreement_blockers(view):
    blockers = []
    for n in (1, 2, 3, 5):
        blockers += view["steps"][n - 1]["missing"]
    return blockers


def prepare_agreement(case_id, actor):
    case, bundle, view = full_context(case_id)
    if case["status"] != "active":
        raise HTTPException(409, ERRORS["CASE_NOT_ACTIVE"][1])
    blockers = agreement_blockers(view)
    if blockers:
        raise HTTPException(400, "Complete these before preparing the agreement: " + "; ".join(blockers))
    template = bundle["templates"][case["template_id"]]
    snap = snapshot_for(case, bundle["properties"][case["property_id"]], bundle["people"][case["tenant_id"]],
                        bundle["people"][case["owner_id"]], template)
    return rpc("onboarding_create_agreement_version", {
        "p_case_id": str(case_id), "p_snapshot": snap, "p_hash": content_hash(snap), "p_template_id": template["id"],
        "p_is_demo": template["status"] != "approved", "p_actor": actor})


def refresh_agreement_if_started(case_id, actor):
    """Once an agreement exists, any change to its details creates a new version
    immediately, which invalidates earlier approvals."""
    case = load_case(case_id)
    if case["status"] != "active" or not get("onboarding_agreement_versions", {"case_id": f"eq.{case_id}", "select": "id", "limit": "1"}):
        return None
    try:
        return prepare_agreement(case_id, actor)
    except HTTPException as error:
        if error.status_code == 400:
            return None     # details incomplete; the review step shows what is missing
        raise


@router.post("/{case_id}/agreement")
def create_agreement(case_id: UUID, x_staybot_staff: str | None = Header(default=None)):
    configured()
    result = prepare_agreement(case_id, staff_actor(x_staybot_staff))
    return {"created": result.get("created"), "version": result.get("version"), "case": case_view(case_id)}


def version_row(case_id, version=None):
    params = {"case_id": f"eq.{case_id}", "order": "version.desc", "limit": "1"}
    if version:
        params["version"] = f"eq.{version}"
    rows = get("onboarding_agreement_versions", params)
    if not rows:
        raise HTTPException(404, "No agreement has been prepared yet.")
    return rows[0]


def approvals_for_pdf(version_id, snapshot):
    rows = get("onboarding_approvals", {"agreement_version_id": f"eq.{version_id}", "order": "created_at.desc"})
    out = []
    for party in PARTIES:
        latest = next((r for r in rows if r["party"] == party), None)
        if latest and latest["decision"] == "approved":
            out.append({"party": party, "name": snapshot[party]["full_name"], "decision": "approved", "at": latest["created_at"], "channel": latest["channel"]})
    return out


def draft_pdf(case_id, version=None):
    row = version_row(case_id, version)
    return row, lease_pdf.render(row["snapshot"], version=row["version"], content_hash=row["content_hash"], final=False, generated_at=now_iso())


@router.get("/{case_id}/agreement/preview.pdf")
def preview_agreement(case_id: UUID, version: int | None = None):
    configured()
    row, data = draft_pdf(case_id, version)
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="DRAFT-{row["snapshot"]["reference"]}-v{row["version"]}.pdf"', "Cache-Control": "no-store"})


class StaffReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agreement_version: int = Field(ge=1)


@router.post("/{case_id}/staff-review")
def record_staff_review(case_id: UUID, body: StaffReview, x_staybot_staff: str | None = Header(default=None)):
    configured()
    actor = staff_actor(x_staybot_staff)
    if actor == "staff:unnamed":
        raise HTTPException(400, "Choose which staff member is reviewing.")
    case, bundle, view = full_context(case_id)
    latest = view["agreement"]["latest_version"]
    if not latest or latest["version"] != body.agreement_version or not view["agreement"]["is_current"]:
        raise HTTPException(409, ERRORS["OUTDATED_AGREEMENT_VERSION"][1])
    update_case(case_id, case["version"], {"staff_review": {"agreement_version": body.agreement_version, "reviewed_by": actor, "reviewed_at": now_iso()}},
                actor, "staff_review_recorded")
    return case_view(case_id)


def record_decision(case_id, version_id, party, person_id, decision, note, actor, channel):
    case, bundle, view = full_context(case_id)
    latest = view["agreement"]["latest_version"]
    if not latest or latest["id"] != str(version_id) or not view["agreement"]["is_current"]:
        raise HTTPException(409, ERRORS["OUTDATED_AGREEMENT_VERSION"][1])
    result = rpc("onboarding_record_approval", {
        "p_case_id": str(case_id), "p_version_id": str(version_id), "p_party": party, "p_person_id": str(person_id),
        "p_decision": decision, "p_note": note or None, "p_actor": actor, "p_channel": channel})
    if decision == "changes_requested" and result.get("created"):
        raise_escalation(case, view, party, "conflict", note or "Changes requested on the agreement.")
    return result


def raise_escalation(case, view, party, category, message):
    current = next((s for s in view["steps"] if not s["complete"]), view["steps"][-1])
    handoff = onboarding_handoff.build(case, party, category, message,
                                       [m for s in view["steps"] for m in s["missing"]][:8], current["title"])
    row = insert("onboarding_escalations", {"case_id": case["id"], "party": party, "category": category, "message": message[:2000], "handoff": handoff})
    if party in PARTIES:
        progress_upsert(case["id"], party, {"staff_help_requested": True})
    audit(case["id"], f"{party}" if party in PARTIES else "staff", "escalation_created", {"escalation_id": row["id"], "category": category})
    return row


def progress_upsert(case_id, party, fields):
    existing = get("onboarding_party_progress", {"case_id": f"eq.{case_id}", "party": f"eq.{party}"})
    if existing:
        return patch("onboarding_party_progress", {**fields, "updated_at": now_iso()}, {"case_id": f"eq.{case_id}", "party": f"eq.{party}"})
    try:
        return insert("onboarding_party_progress", {"case_id": str(case_id), "party": party, **fields})
    except HTTPException as error:
        if error.status_code == 409:
            return patch("onboarding_party_progress", {**fields, "updated_at": now_iso()}, {"case_id": f"eq.{case_id}", "party": f"eq.{party}"})
        raise


class EscalationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    party: Literal["tenant", "owner", "staff"] = "staff"
    category: Literal["help_request", "legal", "pricing", "conflict", "documents", "other"] = "other"
    message: str = Field(min_length=1, max_length=2000)


@router.post("/{case_id}/escalations")
def create_escalation(case_id: UUID, body: EscalationIn):
    configured()
    case, bundle, view = full_context(case_id)
    raise_escalation(case, view, body.party, body.category, body.message)
    return case_view(case_id)


@router.post("/{case_id}/escalations/{escalation_id}/resolve")
def resolve_escalation(case_id: UUID, escalation_id: UUID, x_staybot_staff: str | None = Header(default=None)):
    configured()
    actor = staff_actor(x_staybot_staff)
    if not patch("onboarding_escalations", {"status": "resolved", "resolved_by": actor, "resolved_at": now_iso()},
                 {"id": f"eq.{escalation_id}", "case_id": f"eq.{case_id}", "status": "eq.open"}):
        raise HTTPException(409, "Already resolved or not found.")
    audit(case_id, actor, "escalation_resolved", {"escalation_id": str(escalation_id)})
    return case_view(case_id)


# ---------------------------------------------------------------------
# Finalization and final PDF
# ---------------------------------------------------------------------

@router.post("/{case_id}/finalize")
def finalize(case_id: UUID, x_staybot_staff: str | None = Header(default=None)):
    configured()
    actor = staff_actor(x_staybot_staff)
    if actor == "staff:unnamed":
        raise HTTPException(400, "Choose which staff member is finalizing.")
    case, bundle, view = full_context(case_id)
    if view["final_document"]:
        return {"created": False, "case": view}
    if case["status"] != "active":
        raise HTTPException(409, ERRORS["CASE_NOT_ACTIVE"][1])
    blockers = [m for s in view["steps"][:6] for m in s["missing"]]
    if blockers:
        raise HTTPException(409, "Cannot finalize yet: " + "; ".join(blockers))
    row = version_row(case_id)
    # The PDF comes from the stored snapshot of the exact approved version.
    data = lease_pdf.render(row["snapshot"], version=row["version"], content_hash=row["content_hash"], final=True,
                            approvals=approvals_for_pdf(row["id"], row["snapshot"]), generated_at=now_iso())
    digest = file_store.sha256(data)
    path = f"cases/{case_id}/final/{row['snapshot']['reference']}-v{row['version']}-{digest[:16]}.pdf"
    try:
        file_store.put(path, data, "application/pdf")
    except file_store.StorageError as error:
        if "exists" not in str(error) and "Duplicate" not in str(error):
            raise HTTPException(502, str(error))
    result = rpc("onboarding_finalize_case", {
        "p_case_id": str(case_id), "p_expected_version": case["version"], "p_version_id": row["id"],
        "p_storage_path": path, "p_sha256": digest, "p_size": len(data), "p_actor": actor})
    properties.clear_cache()
    return {"created": result.get("created"), "case": case_view(case_id)}


def final_pdf_bytes(case_id):
    finals = get("onboarding_final_documents", {"case_id": f"eq.{case_id}"})
    if not finals:
        raise HTTPException(404, "The final lease PDF has not been generated yet.")
    final = finals[0]
    data = file_store.get(final["storage_path"])
    if file_store.sha256(data) != final["sha256"]:
        raise HTTPException(500, "The stored PDF does not match its recorded fingerprint. It was not sent or downloaded.")
    return final, data


@router.get("/{case_id}/final.pdf")
def download_final(case_id: UUID, x_staybot_staff: str | None = Header(default=None)):
    configured()
    final, data = final_pdf_bytes(case_id)
    case = load_case(case_id)
    audit(case_id, staff_actor(x_staybot_staff), "final_pdf_downloaded", {"final_document_id": final["id"]})
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="Lease-{case["reference"]}.pdf"', "Cache-Control": "no-store"})


# ---------------------------------------------------------------------
# Links, WhatsApp and TurboTenant
# ---------------------------------------------------------------------

def issue_link(case, party, actor, base_url):
    person_id = case[f"{party}_id"]
    if not person_id:
        raise HTTPException(400, f"Assign the {party} first.")
    token = secrets.token_urlsafe(32)
    patch("onboarding_access_links", {"revoked_at": now_iso()}, {"case_id": f"eq.{case['id']}", "party": f"eq.{party}", "revoked_at": "is.null"})
    expires = datetime.now(timezone.utc) + timedelta(days=LINK_DAYS)
    row = insert("onboarding_access_links", {"case_id": case["id"], "party": party, "person_id": person_id,
                                             "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                                             "expires_at": expires.isoformat(timespec="seconds"), "created_by": actor})
    audit(case["id"], actor, "access_link_issued", {"party": party, "link_id": row["id"], "expires_at": row["expires_at"]})
    return row, f"{base_url.rstrip('/')}/p/{token}"


def base_url_for(request: Request):
    import os
    return (os.getenv("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")


class PartyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    party: Literal["tenant", "owner"]


@router.post("/{case_id}/invite")
def invite(case_id: UUID, body: PartyAction, request: Request, x_staybot_staff: str | None = Header(default=None)):
    from src.services import onboarding_whatsapp
    configured()
    actor = staff_actor(x_staybot_staff)
    case, bundle, view = full_context(case_id)
    if case["status"] != "active":
        raise HTTPException(409, ERRORS["CASE_NOT_ACTIVE"][1])
    person = bundle["people"].get(case[f"{body.party}_id"])
    if not person or not person.get("whatsapp"):
        raise HTTPException(400, f"Add the {body.party}'s WhatsApp number first.")
    if not (case.get("consent") or {}).get(body.party, {}).get("whatsapp_opt_in"):
        raise HTTPException(400, f"Record the {body.party}'s WhatsApp consent before sending messages.")
    link, url = issue_link(case, body.party, actor, base_url_for(request))
    message = onboarding_whatsapp.send_invite(case, view, body.party, person, url, link, actor)
    progress_upsert(case_id, body.party, {"invited_at": now_iso()})
    return {"message": message, "link": url if message.get("test_mode") else None, "case": case_view(case_id)}


@router.post("/{case_id}/send-final")
def send_final(case_id: UUID, x_staybot_staff: str | None = Header(default=None)):
    from src.services import onboarding_whatsapp
    configured()
    actor = staff_actor(x_staybot_staff)
    case, bundle, view = full_context(case_id)
    final, data = final_pdf_bytes(case_id)
    results = {}
    for party in PARTIES:
        person = bundle["people"][case[f"{party}_id"]]
        if not (case.get("consent") or {}).get(party, {}).get("whatsapp_opt_in"):
            results[party] = {"status": "failed", "error": "No WhatsApp consent recorded."}
            continue
        results[party] = onboarding_whatsapp.send_final_pdf(case, party, person, final, data, actor)
    return {"results": results, "case": case_view(case_id)}


@router.post("/{case_id}/messages/{message_id}/retry")
def retry_message(case_id: UUID, message_id: UUID, x_staybot_staff: str | None = Header(default=None)):
    from src.services import onboarding_whatsapp
    configured()
    actor = staff_actor(x_staybot_staff)
    case, bundle, view = full_context(case_id)
    msg = one("onboarding_whatsapp_messages", {"id": f"eq.{message_id}", "case_id": f"eq.{case_id}"}, "Message not found.")
    if msg["status"] != "failed":
        raise HTTPException(409, "Only failed messages can be retried.")
    if msg["kind"] != "final_pdf":
        raise HTTPException(400, "Send a new invitation instead (a fresh secure link is issued).")
    final, data = final_pdf_bytes(case_id)
    person = bundle["people"][case[f"{msg['party']}_id"]]
    return {"result": onboarding_whatsapp.send_final_pdf(case, msg["party"], person, final, data, actor), "case": case_view(case_id)}


class SimulateStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: UUID
    status: Literal["sent", "delivered", "read", "failed"]


@router.post("/{case_id}/test/simulate-status")
def simulate_status(case_id: UUID, body: SimulateStatus):
    """Test mode only: apply a simulated WhatsApp status event to a simulated
    message. It goes through the same idempotent handler as real webhooks."""
    from src.services import onboarding_whatsapp, whatsapp
    configured()
    if not whatsapp.DRY_RUN:
        raise HTTPException(403, "Status simulation is only available in WhatsApp test mode.")
    msg = one("onboarding_whatsapp_messages", {"id": f"eq.{body.message_id}", "case_id": f"eq.{case_id}"}, "Message not found.")
    if not msg["test_mode"] or not msg.get("provider_message_id"):
        raise HTTPException(400, "Only simulated test-mode messages can receive simulated statuses.")
    payload = onboarding_whatsapp.simulated_status_payload(msg["provider_message_id"], body.status, msg["to_phone"])
    for status in onboarding_whatsapp.parse_statuses(payload):
        onboarding_whatsapp.apply_status(status)
    return {"case": case_view(case_id)}


class ManualHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    link: str | None = Field(default=None, max_length=500)
    note: str = Field(default="", max_length=500)


@router.get("/{case_id}/turbotenant")
def turbotenant_packet(case_id: UUID):
    configured()
    view = case_view(case_id)
    return {"integration": turbotenant.status(), "handoff_packet": turbotenant.handoff_packet(view), "recorded": view["turbotenant"]}


@router.post("/{case_id}/turbotenant")
def turbotenant_manual(case_id: UUID, body: ManualHandoff, x_staybot_staff: str | None = Header(default=None)):
    configured()
    actor = staff_actor(x_staybot_staff)
    if actor == "staff:unnamed":
        raise HTTPException(400, "Choose which staff member completed the handoff.")
    try:
        record = turbotenant.record_manual_handoff(actor, body.link, body.note)
    except ValueError as error:
        raise HTTPException(400, str(error))
    case = load_case(case_id)
    if case["status"] == "cancelled":
        raise HTTPException(409, ERRORS["CASE_NOT_ACTIVE"][1])
    if case["status"] == "finalized":
        # Finalized cases are locked by the database function; record the handoff in the audit log only.
        audit(case_id, actor, "turbotenant_manual_handoff", record)
    else:
        update_case(case_id, case["version"], {"turbotenant": record}, actor, "turbotenant_manual_handoff")
    return case_view(case_id)


# ---------------------------------------------------------------------
# Configuration: document checklist and lease templates
# ---------------------------------------------------------------------

class RequirementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jurisdiction: str = Field(default="US-NC", pattern=r"^[A-Z]{2}-[A-Z]{2}$")
    property_id: str | None = None
    party: Literal["tenant", "owner"]
    doc_key: str = Field(pattern=r"^[a-z0-9_]{1,60}$")
    label: str = Field(min_length=2, max_length=80)
    reason: str = Field(min_length=5, max_length=300)
    required: bool = True
    active: bool = True


@router.get("/config/requirements")
def list_requirements():
    configured()
    return get("onboarding_document_requirements", {"order": "jurisdiction.asc"})


@router.post("/config/requirements")
def save_requirement(body: RequirementIn, x_staybot_staff: str | None = Header(default=None)):
    configured()
    if body.property_id and not get("properties", {"id": f"eq.{body.property_id}"}):
        raise HTTPException(400, "Property not found.")
    params = {"jurisdiction": f"eq.{body.jurisdiction}", "party": f"eq.{body.party}", "doc_key": f"eq.{body.doc_key}",
              "property_id": f"eq.{body.property_id}" if body.property_id else "is.null"}
    fields = body.model_dump()
    row = patch("onboarding_document_requirements", fields, params) if get("onboarding_document_requirements", params) \
        else insert("onboarding_document_requirements", fields)
    audit(None, staff_actor(x_staybot_staff), "document_requirement_saved", fields)
    return row


class TemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=120)
    jurisdiction: str = Field(default="US-NC", pattern=r"^[A-Z]{2}-[A-Z]{2}$")
    clauses_text: str = Field(min_length=20, max_length=60000)
    approved_by: str = Field(min_length=2, max_length=120)
    approval_confirmed: bool

    def clauses(self):
        clauses, title, body = [], None, []
        for line in self.clauses_text.splitlines():
            if line.startswith("## "):
                if title:
                    clauses.append({"title": title, "body": "\n".join(body).strip()})
                title, body = line[3:].strip(), []
            elif title is not None:
                body.append(line)
        if title:
            clauses.append({"title": title, "body": "\n".join(body).strip()})
        if not clauses or any(not c["title"] or not c["body"] for c in clauses):
            raise ValueError("Write each clause as a '## Title' line followed by its text.")
        return clauses


@router.get("/config/templates")
def list_templates():
    configured()
    return get("lease_templates", {"order": "created_at.desc"})


@router.post("/config/templates")
def add_template(body: TemplateIn, x_staybot_staff: str | None = Header(default=None)):
    configured()
    if not body.approval_confirmed:
        raise HTTPException(400, "Only add a template after an authorized person has approved its wording.")
    try:
        clauses = body.clauses()
    except ValueError as error:
        raise HTTPException(400, str(error))
    existing = get("lease_templates", {"name": f"eq.{body.name}", "order": "template_version.desc", "limit": "1"})
    row = insert("lease_templates", {"name": body.name, "jurisdiction": body.jurisdiction, "status": "approved", "clauses": clauses,
                                     "template_version": (existing[0]["template_version"] + 1) if existing else 1,
                                     "approved_by": body.approved_by, "approved_at": now_iso()})
    audit(None, staff_actor(x_staybot_staff), "lease_template_approved", {"template_id": row["id"], "approved_by": body.approved_by})
    return row
