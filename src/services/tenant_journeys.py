"""
Tenant journey tracker - the "Tenant journeys" tab in the admin dashboard.

A tenant's journey starts the moment they sign up (their first login) and is
rebuilt on every request from what the app already records - nothing extra
to keep in sync:

  accounts               signed up, last login (supabase_tenant_journeys.sql), screening done
  account_portfolios     screening answers (income, employment)
  account_documents      proof of income uploaded / reviewed
  conversations          chatting with the Staybot assistant
  rental_threads         messaging a home's owner (rental_chat.py)
  rental_applications    applied -> team approved -> owner approved (rentals.py)
  maintenance_tickets    requests once housed

Pipeline (one current stage per tenant):
  signed_up -> screening -> exploring -> applied -> awaiting_owner -> housed
  (+ tenancy_ended)

For each tenant: current stage and since when, whose move it is next
(tenant / team / owner) and what it is, and "needs attention" flags when a
step has waited too long. Staff-only (/tenant-journeys in team_auth.STAFF_ONLY_PREFIXES).

(Investor journeys are a separate thing: src/services/investor_journey.py, /journeys.)
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.services import db

router = APIRouter(prefix="/tenant-journeys", tags=["Tenant journeys"])
customer_router = APIRouter(prefix="/me", tags=["Customer journeys"])

STAGES = [
    ("signed_up", "Signed up"),
    ("screening", "Screening done"),
    ("exploring", "Exploring homes"),
    ("applied", "Applied"),
    ("awaiting_owner", "Team approved"),
    ("housed", "Housed"),
]
STAGE_INDEX = {k: i for i, (k, _) in enumerate(STAGES)}
STAGE_LABEL = dict(STAGES) | {"tenancy_ended": "Tenancy ended"}

# How long a step can wait before it's flagged "needs attention" (days).
LIMITS = {
    "screening_not_done": 3,
    "doc_review": 2,
    "team_review": 2,
    "owner_decision": 3,
    "inactive": 14,
    "ticket_open": 7,
    "urgent_ticket": 1,
}

CHUNK = 80


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------

def parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def days_since(ts, now: datetime) -> Optional[float]:
    d = parse(ts)
    return round((now - d).total_seconds() / 86400, 1) if d else None


def latest(*values):
    ds = [(parse(v), v) for v in values if parse(v)]
    return max(ds)[1] if ds else None


def earliest(*values):
    ds = [(parse(v), v) for v in values if parse(v)]
    return min(ds)[1] if ds else None


def fetch_in(table: str, column: str, values: list, select: str = "*", warnings: list = None, setup: str = None, extra: dict = None) -> list:
    """Rows where column is in values, chunked; a missing table (setup file
    not run yet) becomes a warning instead of breaking the whole tracker."""
    values = sorted({v for v in values if v})
    rows = []
    for i in range(0, len(values), CHUNK):
        try:
            rows += db._get(table, {column: f"in.({','.join(values[i:i + CHUNK])})", "select": select, **(extra or {})}) or []
        except Exception as e:
            if warnings is not None:
                note = f"Run {setup} to include {table.replace('_', ' ')}." if setup else f"Couldn't read {table}: {e}"
                if note not in warnings:
                    warnings.append(note)
            return rows
    return rows


def group(rows: list, key: str) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r.get(key), []).append(r)
    return out


# ---------------------------------------------------------------------
# build
# ---------------------------------------------------------------------

def load_related(tenants: list, warnings: list) -> dict:
    ids = [t["id"] for t in tenants]
    sessions = [t["session_id"] for t in tenants]
    apps = fetch_in("rental_applications", "tenant_account_id", ids, warnings=warnings, setup="supabase_rental_applications.sql")
    owner_sessions = [a.get("owner_session_id") for a in apps]
    return {
        "portfolios": {r["account_id"]: r for r in fetch_in("account_portfolios", "account_id", ids, "account_id,details,updated_at", warnings)},
        "docs": group(fetch_in("account_documents", "account_id", ids,
                               "id,account_id,status,uploaded_at,reviewed_at,reviewed_by,review_note,doc_key",
                               warnings, "supabase_tenant_screening.sql"), "account_id"),
        "convos": group(fetch_in("conversations", "session_id", sessions, "session_id,created_at", warnings), "session_id"),
        "threads": group(fetch_in("rental_threads", "tenant_account_id", ids,
                                  "id,tenant_account_id,property_title,owner_name,created_at,last_message_at",
                                  warnings, "supabase_rental_chat.sql"), "tenant_account_id"),
        "apps": group(apps, "tenant_account_id"),
        "tickets": group(fetch_in("maintenance_tickets", "session_id", sessions,
                                  "id,session_id,property_title,issue_type,urgency,ticket_status,created_at,updated_at,summary",
                                  warnings, "supabase_maintenance_tickets.sql"), "session_id"),
        "owners": {a["session_id"]: a.get("name") for a in fetch_in("accounts", "session_id", owner_sessions, "session_id,name", warnings)},
    }


def journey(t: dict, rel: dict, now: datetime, with_events: bool = False) -> dict:
    portfolio = rel["portfolios"].get(t["id"]) or {}
    details = portfolio.get("details") or {}
    docs = sorted((d for d in rel["docs"].get(t["id"], []) if d.get("doc_key") in (None, "proof_of_income")),
                  key=lambda d: d.get("uploaded_at") or "")
    doc = docs[-1] if docs else None
    convos = sorted(rel["convos"].get(t["session_id"], []), key=lambda c: c.get("created_at") or "")
    threads = sorted(rel["threads"].get(t["id"], []), key=lambda x: x.get("created_at") or "")
    apps = sorted(rel["apps"].get(t["id"], []), key=lambda a: a.get("created_at") or "")
    tickets = sorted(rel["tickets"].get(t["session_id"], []), key=lambda x: x.get("created_at") or "")
    owners = rel["owners"]

    signed_up = t.get("created_at")
    screened_at = t.get("screening_completed_at") or (portfolio.get("updated_at") if t.get("screening_seen") else None)
    screened = bool(t.get("screening_seen"))
    first_explore = earliest(*(c.get("created_at") for c in convos), *(x.get("created_at") for x in threads),
                             *(a.get("created_at") for a in apps))

    approved = [a for a in apps if a["status"] == "approved"]
    waiting_owner = [a for a in apps if a["status"] == "team_approved"]
    waiting_team = [a for a in apps if a["status"] == "submitted"]
    ended = [a for a in apps if a["status"] == "ended"]
    declined = [a for a in apps if a["status"] in ("team_declined", "owner_declined")]

    # ---- current stage (furthest point reached that's still live)
    if approved:
        a = approved[-1]
        stage, since = "housed", a.get("owner_decided_at") or a.get("team_reviewed_at") or a.get("updated_at")
    elif waiting_owner:
        a = waiting_owner[-1]
        stage, since = "awaiting_owner", a.get("team_reviewed_at") or a.get("updated_at")
    elif waiting_team:
        a = waiting_team[-1]
        stage, since = "applied", a.get("created_at")
    elif ended:
        stage, since = "tenancy_ended", ended[-1].get("ended_at") or ended[-1].get("updated_at")
    elif first_explore and screened:
        stage, since = "exploring", first_explore
    elif screened:
        stage, since = "screening", screened_at or signed_up
    else:
        stage, since = "signed_up", signed_up

    # ---- whose move is it, and what
    open_tickets = [x for x in tickets if x.get("ticket_status") in ("needs_review", "open", "in_progress")]
    attention: list[str] = []
    d_since = days_since(since, now)

    if stage == "signed_up":
        nxt = ("tenant", "Complete screening - details and proof of income")
        if (d_since or 0) > LIMITS["screening_not_done"]:
            attention.append(f"Screening not finished after {int(d_since)} days")
    elif stage == "housed":
        a = approved[-1]
        if open_tickets:
            nxt = ("owner", f"{len(open_tickets)} open maintenance request{'s' if len(open_tickets) > 1 else ''} at {a.get('property_title') or 'their home'}")
        else:
            nxt = (None, f"Renting {a.get('property_title') or 'a home'} - nothing pending")
    elif stage == "awaiting_owner":
        a = waiting_owner[-1]
        owner = owners.get(a.get("owner_session_id")) or "the owner"
        nxt = ("owner", f"{owner} to approve or decline for {a.get('property_title') or 'the home'}")
        if (d_since or 0) > LIMITS["owner_decision"]:
            attention.append(f"Waiting on owner for {int(d_since)} days")
    elif stage == "applied":
        a = waiting_team[-1]
        nxt = ("team", f"Review application for {a.get('property_title') or 'a home'}")
        if (d_since or 0) > LIMITS["team_review"]:
            attention.append(f"Application waiting on team for {int(d_since)} days")
    elif stage == "tenancy_ended":
        nxt = ("tenant", "Tenancy ended - find their next home")
    elif stage == "exploring":
        nxt = ("tenant", "Apply for a home" + (" (previous application declined)" if declined else ""))
    else:  # screening
        nxt = ("tenant", "Browse homes and chat with the assistant")

    # Income document review is the team's job whatever the stage.
    if doc and doc.get("status") == "awaiting_review" and stage not in ("housed", "tenancy_ended"):
        waited = days_since(doc.get("uploaded_at"), now) or 0
        if nxt[0] != "owner" and stage != "applied":
            nxt = ("team", "Review proof of income")
        if waited > LIMITS["doc_review"]:
            attention.append(f"Proof of income unreviewed for {int(waited)} days")
    if doc and doc.get("status") == "changes_required" and stage not in ("housed", "tenancy_ended"):
        nxt = ("tenant", "Re-upload proof of income (changes requested)")

    for x in open_tickets:
        age = days_since(x.get("created_at"), now) or 0
        if x.get("urgency") == "urgent" and age > LIMITS["urgent_ticket"]:
            attention.append(f"Urgent {str(x.get('issue_type') or 'maintenance').replace('_', ' ')} request open {int(age)} days")
        elif age > LIMITS["ticket_open"]:
            attention.append(f"Maintenance request open {int(age)} days")

    last_activity = latest(
        signed_up, t.get("last_login_at"), screened_at, doc and doc.get("uploaded_at"),
        *(c.get("created_at") for c in convos), *(x.get("last_message_at") or x.get("created_at") for x in threads),
        *(a.get("updated_at") for a in apps), *(x.get("created_at") for x in tickets),
    )
    idle = days_since(last_activity, now)
    if stage in ("signed_up", "screening", "exploring") and (idle or 0) > LIMITS["inactive"]:
        attention.append(f"No activity for {int(idle)} days")

    # ---- milestone strip (for the stepper)
    stage_i = STAGE_INDEX.get(stage, len(STAGES) - 1)
    first_app = apps[0] if apps else None
    team_ok = next((a for a in apps if a.get("team_reviewed_at") and a["status"] in ("team_approved", "approved", "owner_declined", "ended", "closed")), None)
    milestone_at = {
        "signed_up": signed_up,
        "screening": screened_at if screened else None,
        "exploring": first_explore if screened else None,
        "applied": first_app and first_app.get("created_at"),
        "awaiting_owner": team_ok and team_ok.get("team_reviewed_at"),
        "housed": (approved[-1].get("owner_decided_at") or approved[-1].get("team_reviewed_at")) if approved
                  else ((ended[-1].get("owner_decided_at") or ended[-1].get("team_reviewed_at")) if ended else None),
    }
    milestones = []
    for i, (key, label) in enumerate(STAGES):
        if stage == "tenancy_ended":
            state = "done" if milestone_at.get(key) else "skipped"
        elif i < stage_i:
            state = "done"
        elif i == stage_i:
            state = "done" if key == "housed" else "current"
        else:
            state = "todo"
        milestones.append({"key": key, "label": label, "at": milestone_at.get(key), "state": state})

    home = approved[-1] if approved else None
    out = {
        "account_id": t["id"],
        "name": t.get("name"),
        "email": t.get("email"),
        "phone": details.get("phone"),
        "stage": stage,
        "stage_label": STAGE_LABEL[stage],
        "stage_index": stage_i,
        "stage_since": since,
        "days_in_stage": d_since,
        "next_by": nxt[0],
        "next_action": nxt[1],
        "attention": attention,
        "started_at": signed_up,
        "last_login_at": t.get("last_login_at"),
        "login_count": t.get("login_count"),
        "last_activity": last_activity,
        "days_idle": idle,
        "milestones": milestones,
        "home": home and {"title": home.get("property_title"), "owner": owners.get(home.get("owner_session_id"))},
        "counts": {
            "chats": len(convos), "owner_threads": len(threads), "applications": len(apps),
            "declined": len(declined), "open_tickets": len(open_tickets), "tickets": len(tickets),
        },
        "income": {
            "employment_status": details.get("employment_status"),
            "monthly_income": details.get("monthly_income"),
            "proof_status": (doc or {}).get("status") or ("not_uploaded" if screened else None),
        },
    }
    if with_events:
        out["events"] = timeline(t, screened_at, docs, convos, threads, apps, tickets, owners)
        out["applications"] = [
            {k: a.get(k) for k in ("id", "property_title", "status", "created_at", "team_reviewed_at", "team_reviewed_by",
                                   "team_note", "owner_decided_at", "owner_note", "move_in_date")}
            | {"owner": owners.get(a.get("owner_session_id"))} for a in reversed(apps)
        ]
    return out


def timeline(t, screened_at, docs, convos, threads, apps, tickets, owners) -> list[dict]:
    ev = []

    def add(at, kind, title, detail=None, actor=None, tone="neutral"):
        if at:
            ev.append({"at": at, "kind": kind, "title": title, "detail": detail, "actor": actor, "tone": tone})

    add(t.get("created_at"), "start", "Signed up - journey started", t.get("email"), "tenant", "brand")
    if t.get("screening_seen"):
        add(screened_at, "screening", "Completed tenant screening", None, "tenant", "good")
    for d in docs:
        add(d.get("uploaded_at"), "document", "Uploaded proof of income", None, "tenant")
        if d.get("reviewed_at"):
            ok = d.get("status") == "accepted"
            add(d["reviewed_at"], "document", "Proof of income " + ("accepted" if ok else "needs changes"),
                d.get("review_note"), d.get("reviewed_by") or "team", "good" if ok else "warn")
    if convos:
        add(convos[0].get("created_at"), "chat", "First chat with the Staybot assistant",
            f"{len(convos)} conversation{'s' if len(convos) != 1 else ''} in total", "tenant")
    for x in threads:
        add(x.get("created_at"), "message", f"Started messaging the owner of {x.get('property_title') or 'a home'}",
            x.get("owner_name"), "tenant")
    for a in apps:
        home = a.get("property_title") or "a home"
        owner = owners.get(a.get("owner_session_id"))
        add(a.get("created_at"), "application", f"Applied for {home}", a.get("message"), "tenant", "brand")
        if a.get("team_reviewed_at"):
            ok = a["status"] not in ("team_declined",)
            add(a["team_reviewed_at"], "team", ("Team approved" if ok else "Team declined") + f" - {home}",
                a.get("team_note"), a.get("team_reviewed_by") or "team", "good" if ok else "bad")
        if a.get("owner_decided_at"):
            ok = a["status"] in ("approved", "ended")
            add(a["owner_decided_at"], "owner", ("Owner approved - now renting " if ok else "Owner declined - ") + home,
                a.get("owner_note"), owner or "owner", "good" if ok else "bad")
        if a["status"] == "withdrawn":
            add(a.get("updated_at"), "application", f"Withdrew application for {home}", None, "tenant", "warn")
        if a["status"] == "closed":
            add(a.get("updated_at"), "application", f"{home} was let to another applicant", None, None, "warn")
        if a["status"] == "ended":
            add(a.get("ended_at") or a.get("updated_at"), "owner", f"Tenancy ended - {home}", None, owner or "owner", "warn")
    for x in tickets:
        issue = str(x.get("issue_type") or "maintenance").replace("_", " ")
        add(x.get("created_at"), "maintenance", f"Reported a {issue} issue" + (f" ({x['urgency']})" if x.get("urgency") else ""),
            x.get("summary"), "tenant", "warn" if x.get("urgency") == "urgent" else "neutral")
        if x.get("ticket_status") == "resolved":
            add(x.get("updated_at"), "maintenance", f"{issue.capitalize()} request resolved", None, None, "good")
    ev.sort(key=lambda e: parse(e["at"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return ev


def tenant_accounts(account_id: str = None) -> list:
    params = {"role": "eq.tenant", "order": "created_at.desc", "select": "*"}
    if account_id:
        params["id"] = f"eq.{account_id}"
    return db._get("accounts", params) or []



# ---------------------------------------------------------------------
# tenant self-service journey
# ---------------------------------------------------------------------

@customer_router.get("/tenant-journey")
def my_tenant_journey(request: Request):
    """Return only the logged-in tenant's own journey."""
    from src.services import accounts

    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase to track tenant journeys.")

    account = accounts.current_account(request)
    if not account:
        raise HTTPException(401, "Not logged in.")
    if account.get("role") != "tenant":
        raise HTTPException(403, "This account is not a tenant.")

    tenants = tenant_accounts(account.get("id"))
    if not tenants:
        raise HTTPException(404, "Tenant journey not found.")

    warnings: list[str] = []
    rel = load_related(tenants, warnings)
    row = journey(tenants[0], rel, datetime.now(timezone.utc), with_events=True)
    return {"type": "tenant", "type_label": "Tenant", "journey": row, "warnings": warnings}

# ---------------------------------------------------------------------
# endpoints (staff-only)
# ---------------------------------------------------------------------

@router.get("")
def tenant_journeys():
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase to track tenant journeys.")
    now = datetime.now(timezone.utc)
    warnings: list[str] = []
    tenants = tenant_accounts()
    if tenants and "last_login_at" not in tenants[0]:
        warnings.append("Run supabase_tenant_journeys.sql to track logins and exact screening dates.")
    rel = load_related(tenants, warnings)
    rows = [journey(t, rel, now) for t in tenants]

    by_stage = {k: 0 for k, _ in STAGES} | {"tenancy_ended": 0}
    for r in rows:
        by_stage[r["stage"]] += 1
    # Funnel: how many tenants have reached at least each stage.
    reached = [sum(1 for r in rows if any(m["key"] == k and m["state"] in ("done", "current") for m in r["milestones"]))
               for k, _ in STAGES]
    week_ago = sum(1 for r in rows if (days_since(r["started_at"], now) or 99) <= 7)
    return {
        "generated_at": now.isoformat(),
        "stages": [{"key": k, "label": l} for k, l in STAGES],
        "summary": {
            "total": len(rows),
            "new_this_week": week_ago,
            "by_stage": by_stage,
            "attention": sum(1 for r in rows if r["attention"]),
            "waiting_on": {w: sum(1 for r in rows if r["next_by"] == w) for w in ("tenant", "team", "owner")},
        },
        "funnel": [{"key": k, "label": l, "reached": n} for (k, l), n in zip(STAGES, reached)],
        "tenants": rows,
        "warnings": warnings,
    }


@router.get("/{account_id}")
def tenant_journey_detail(account_id: str):
    if not db.ENABLED:
        raise HTTPException(503, "Connect Supabase to track tenant journeys.")
    tenants = tenant_accounts(account_id)
    if not tenants:
        raise HTTPException(404, "Tenant not found.")
    warnings: list[str] = []
    rel = load_related(tenants, warnings)
    return {**journey(tenants[0], rel, datetime.now(timezone.utc), with_events=True), "warnings": warnings}
