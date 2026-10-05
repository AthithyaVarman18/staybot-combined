"""
Tenant screening - staff side.

Tenants submit their screening intake once, right after their first login
(src/services/accounts.py POST /auth/screening/*, src/static/tenant_screening.html).
This module is where staff review it: the structured answers already show up
per-tenant in their portfolio (src/services/portfolio.py); this adds the
proof-of-income document review/download that a portfolio snapshot can't
hold. Team-only (TeamAuthMiddleware), same as onboarding_cases.py, which this
mirrors for its upload/review/download shape.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from src.services import accounts, db, file_store, portfolio

router = APIRouter(prefix="/screening", tags=["Tenant screening"])


def configured():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase and run supabase_tenant_screening.sql to enable tenant screening.")


def staff_actor(x_staybot_staff: str | None) -> str:
    name = (x_staybot_staff or "").strip()
    return f"staff:{name}" if name else "staff:unnamed"


@router.get("")
def list_tenants():
    """Every tenant account that has completed the intake, with their
    portfolio snapshot and document status, for a staff overview list."""

    configured()
    tenants = db._get("accounts", {"role": "eq.tenant", "screening_seen": "eq.true", "select": "id,name,email,created_at"})
    docs = db._get("account_documents", {"doc_key": "eq.proof_of_income", "order": "uploaded_at.desc", "select": "*"})
    docs_by_account: dict[str, list[dict]] = {}
    for d in docs:
        docs_by_account.setdefault(d["account_id"], []).append(d)

    out = []
    for t in tenants:
        details = portfolio.get_portfolio(t["id"]).get("details") or {}
        out.append({
            "account_id": t["id"], "name": t["name"], "email": t["email"],
            "employment_status": details.get("employment_status"),
            "monthly_income": details.get("monthly_income"),
            "annual_income": details.get("annual_income"),
            "has_emi": details.get("has_emi"),
            "emi_details": details.get("emi_details"),
            "notes": details.get("screening_notes"),
            "documents": [{k: doc[k] for k in ("id", "file_name", "status", "uploaded_at")} for doc in docs_by_account.get(t["id"], [])],
        })
    return out


@router.get("/{account_id}/document/{document_id}/file")
def download_document(account_id: UUID, document_id: UUID):
    configured()
    rows = db._get("account_documents", {"id": f"eq.{document_id}", "account_id": f"eq.{account_id}", "select": "*"})
    if not rows:
        raise HTTPException(404, "Document not found.")
    doc = rows[0]
    data = file_store.get(doc["storage_path"])
    return Response(data, media_type=doc["content_type"] or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{doc["file_name"] or "proof_of_income"}"'})


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accepted", "changes_required"]
    note: str = Field(default="", max_length=500)


@router.post("/{account_id}/document/{document_id}/review")
def review_document(account_id: UUID, document_id: UUID, body: Review, x_staybot_staff: str | None = Header(default=None)):
    configured()
    actor = staff_actor(x_staybot_staff)
    if actor == "staff:unnamed":
        raise HTTPException(400, "Choose which staff member is reviewing before accepting or rejecting.")
    if body.decision == "changes_required" and not body.note.strip():
        raise HTTPException(400, "Explain what needs to change.")

    updated = db._patch("account_documents",
                        {"status": body.decision, "review_note": body.note.strip() or None, "reviewed_by": actor, "reviewed_at": accounts.now_iso()},
                        {"id": f"eq.{document_id}", "account_id": f"eq.{account_id}", "status": "eq.awaiting_review"})
    if not updated:
        raise HTTPException(409, "This document was already reviewed or does not belong to this account. Refresh.")
    return {"status": body.decision}
