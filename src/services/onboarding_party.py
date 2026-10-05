"""
Tenant/owner self-service through personal secure links (no accounts).

A link is a random 256-bit token; only its SHA-256 hash is stored. Each link
belongs to one case, one party and one person, expires after a few days,
and is revoked when a new one is issued or the person on the case changes.
A link only ever shows that person's own details, their own documents
checklist, and the agreement they are a party to.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from src.services import lease_pdf, onboarding_cases as oc

router = APIRouter(prefix="/p", tags=["Tenant and owner onboarding"], include_in_schema=False)
PAGE = Path(__file__).resolve().parents[1] / "static" / "party.html"
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}


def resolve(token: str):
    if not token or len(token) > 100:
        raise HTTPException(404, "This link is not valid.")
    oc.configured()
    rows = oc.get("onboarding_access_links", {"token_hash": f"eq.{hashlib.sha256(token.encode()).hexdigest()}"})
    if not rows:
        raise HTTPException(404, "This link is not valid.")
    link = rows[0]
    if link.get("revoked_at"):
        raise HTTPException(410, "This link has been replaced by a newer one. Reply LINK on WhatsApp for a new link.")
    if datetime.fromisoformat(link["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        raise HTTPException(410, "This link has expired. Reply LINK on WhatsApp for a new link.")
    case = oc.load_case(link["case_id"])
    if case[f"{link['party']}_id"] != link["person_id"]:
        raise HTTPException(403, "This link no longer matches the person on this onboarding.")
    oc.patch("onboarding_access_links", {"last_used_at": oc.now_iso()}, {"id": f"eq.{link['id']}"})
    return link, case


def actor_for(link):
    return f"{link['party']}:secure_link:{link['id'][:8]}"


@router.get("/{token}")
def page(token: str):
    return FileResponse(PAGE, headers=NO_STORE)


@router.get("/{token}/api/state")
def state(token: str):
    link, case = resolve(token)
    party = link["party"]
    view = oc.case_view(case["id"])
    other = "owner" if party == "tenant" else "tenant"
    agreement = view["agreement"]
    latest = agreement["latest_version"]
    return Response(content=__import__("json").dumps({
        "party": party,
        "reference": view["reference"],
        "status": view["status"],
        "home": {"title": view["property"]["title"], "unit": view["unit"], "address": view["property_address"]},
        "me": {k: (view[party] or {}).get(k) for k in ("full_name", "whatsapp", "email", "preferred_language", "communication")},
        "other_party_name": (view[other] or {}).get("full_name"),
        "staff_name": (view["staff"] or {}).get("name"),
        "confirmed": (view["party_progress"][party] or {}).get("confirmed") or {},
        "documents": [{k: d[k] for k in ("doc_key", "label", "reason", "required", "status")} |
                      {"review_note": (d["latest"] or {}).get("review_note")} for d in view["documents"] if d["party"] == party],
        "terms": view["terms"] if latest else None,
        "agreement": {
            "version": latest["version"] if latest else None,
            "version_id": latest["id"] if latest and agreement["is_current"] else None,
            "is_demo": latest["is_demo"] if latest else None,
            "ready_for_decision": bool(latest and agreement["is_current"] and view["status"] == "active"),
            "my_decision": agreement["approvals"][party],
            "other_decision": agreement["approvals"][other]["status"],
        },
        "final_available": bool(view["final_document"]),
        "expires_at": link["expires_at"],
    }, default=str), media_type="application/json", headers=NO_STORE)


class Details(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=200)
    preferred_language: Literal["en", "es"] = "en"
    communication: oc.Communication = Field(default_factory=oc.Communication)
    role_confirmed: bool


@router.put("/{token}/api/details")
def save_details(token: str, body: Details):
    link, case = resolve(token)
    if case["status"] != "active":
        raise HTTPException(409, "This onboarding is already finalized.")
    party = link["party"]
    if not body.role_confirmed:
        view = oc.case_view(case["id"])
        oc.raise_escalation(case, view, party, "conflict", f"Says the {party} role on this case is not correct.")
        return {"escalated": True, "message": "Thanks. A staff member will check the case and contact you on WhatsApp."}
    person = oc.one("onboarding_people", {"id": f"eq.{link['person_id']}"})
    validated = oc.PersonIn(full_name=body.full_name, whatsapp=person["whatsapp"], email=body.email,
                            preferred_language=body.preferred_language, communication=body.communication)
    oc.patch("onboarding_people", {**validated.model_dump(mode="json", exclude={"whatsapp", "is_test"}), "updated_at": oc.now_iso()},
             {"id": f"eq.{person['id']}"})
    oc.audit(case["id"], actor_for(link), f"{party}_self_updated_details", {"person_id": person["id"]})
    progress = oc.get("onboarding_party_progress", {"case_id": f"eq.{case['id']}", "party": f"eq.{party}"})
    confirmed = {**((progress[0].get("confirmed") if progress else None) or {}),
                 "role": True, "name": True, "email": True if body.email else "skipped", "language": True, "communication": True}
    oc.progress_upsert(case["id"], party, {"confirmed": confirmed, "awaiting": None,
                                           "first_response_at": (progress[0].get("first_response_at") if progress else None) or oc.now_iso()})
    oc.refresh_agreement_if_started(case["id"], actor_for(link))
    return {"escalated": False}


@router.post("/{token}/api/documents/{doc_key}")
async def upload(token: str, doc_key: str, request: Request):
    link, case = resolve(token)
    row = await oc.store_upload(case, link["party"], doc_key, request, actor_for(link))
    return {"status": row["status"], "file_name": row["file_name"]}


@router.get("/{token}/api/agreement.pdf")
def agreement_pdf(token: str):
    link, case = resolve(token)
    if case["status"] == "finalized":
        final, data = oc.final_pdf_bytes(case["id"])
        oc.audit(case["id"], actor_for(link), "final_pdf_downloaded", {"final_document_id": final["id"]})
        name = f"Lease-{case['reference']}.pdf"
    else:
        row, data = oc.draft_pdf(case["id"])
        name = f"DRAFT-{case['reference']}-v{row['version']}.pdf"
    return Response(data, media_type="application/pdf", headers={**NO_STORE, "Content-Disposition": f'inline; filename="{name}"'})


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: UUID
    decision: Literal["approved", "changes_requested"]
    note: str = Field(default="", max_length=2000)
    reviewed_agreement: bool


@router.post("/{token}/api/decision")
def decide(token: str, body: Decision):
    link, case = resolve(token)
    if body.decision == "approved" and not body.reviewed_agreement:
        raise HTTPException(400, "Please confirm you have read the agreement before approving.")
    if body.decision == "changes_requested" and not body.note.strip():
        raise HTTPException(400, "Tell us what should change.")
    result = oc.record_decision(case["id"], body.version_id, link["party"], link["person_id"], body.decision,
                                body.note.strip(), actor_for(link), "secure_link")
    from src.services import onboarding_whatsapp
    onboarding_whatsapp.update_completion(case["id"], link["party"])
    return {"recorded": True, "repeated": not result.get("created")}


class HelpIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)


@router.post("/{token}/api/help")
def ask_for_help(token: str, body: HelpIn):
    link, case = resolve(token)
    view = oc.case_view(case["id"])
    category = oc.onboarding_handoff.classify(body.message) or "help_request"
    oc.raise_escalation(case, view, link["party"], category, body.message)
    return {"message": "Thanks. A staff member will contact you on WhatsApp soon."}
