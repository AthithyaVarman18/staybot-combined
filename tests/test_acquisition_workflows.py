import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.services.inspections import build_summary, validate_slots
from src.services.purchase_offer_pdf import render
from src.services.purchase_offers import NegotiationIn, Terms


ROOT = Path(__file__).resolve().parents[1]


def future_dt(hours=48):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def future_day(days=5):
    return date.today() + timedelta(days=days)


def test_offer_terms_require_future_dates_and_validate_order():
    terms = Terms(
        price="295000",
        earnest_money="10000",
        due_diligence_fee="5000",
        due_diligence_period_days=10,
        due_diligence_end=future_day(10),
        financing_deadline=future_day(15),
        closing_date=future_day(20),
        expires_at=future_dt(),
        financing_contingency="Loan approval subject to lender process",
        included="Existing appliances as confirmed by the broker",
    )
    assert terms.price == "295000.00"
    assert terms.due_diligence_end < terms.closing_date

    with pytest.raises(ValueError, match="Due diligence end cannot be after the closing date"):
        Terms(
            price="295000", closing_date=future_day(5), due_diligence_end=future_day(6), expires_at=future_dt()
        )


@pytest.mark.parametrize("event", ["counter", "accepted", "rejected", "withdrawn", "expired"])
def test_negotiation_events_keep_structured_human_action(event):
    item = NegotiationIn(expected_version=2, event_type=event, actor_type="seller", amount="305000", note="Recorded by staff.")
    assert item.actor_type == "seller"
    assert item.amount == "305000.00"


def test_inspection_slots_use_viewing_scheduler_rules():
    d = future_day(2).isoformat()
    slots = validate_slots([
        {"date": d, "time": "10:00"},
        {"date": d, "time": "11:00"},
    ])
    assert len(slots) == 2
    assert slots[0]["date"] == d

    with pytest.raises(ValueError):
        validate_slots([{"date": d, "time": "10:00"}, {"date": d, "time": "10:00"}])


def test_inspection_booking_requires_accepted_purchase_offer(monkeypatch):
    from src.services import inspections
    from fastapi import HTTPException
    from uuid import uuid4

    offer_id = uuid4()
    inspector_id = uuid4()
    monkeypatch.setattr(inspections, "get_one", lambda table, params, missing="Not found": {
        "id": str(inspector_id), "name": "Inspector One", "active": True
    })
    monkeypatch.setattr(inspections.purchase_offers, "get_offer", lambda oid: {
        "id": str(offer_id), "status": "negotiating", "deal_id": None
    })
    with pytest.raises(HTTPException, match="accepted"):
        inspections.create_booking(inspections.BookingIn(
            offer_id=offer_id, inspector_id=inspector_id,
            proposed_slots=[{"date": future_day(2).isoformat(), "time": "10:00"},
                            {"date": future_day(2).isoformat(), "time": "11:00"}],
            listing_agent_name="Agent", listing_agent_contact="agent@example.com",
            access_confirmed=True, fee_payer="investor", note=""
        ))


def test_inspection_booking_allows_accepted_purchase_offer(monkeypatch):
    from src.services import inspections
    from uuid import uuid4

    offer_id = uuid4()
    inspector_id = uuid4()
    saved = {"id": "booking-1", "offer_id": str(offer_id), "status": "proposed"}
    monkeypatch.setattr(inspections, "get_one", lambda table, params, missing="Not found": {
        "id": str(inspector_id), "name": "Inspector One", "active": True
    })
    monkeypatch.setattr(inspections.purchase_offers, "get_offer", lambda oid: {
        "id": str(offer_id), "status": "accepted", "deal_id": None
    })
    monkeypatch.setattr(inspections, "q", lambda fn, *args, **kwargs: saved)
    result = inspections.create_booking(inspections.BookingIn(
        offer_id=offer_id, inspector_id=inspector_id,
        proposed_slots=[{"date": future_day(2).isoformat(), "time": "10:00"},
                        {"date": future_day(2).isoformat(), "time": "11:00"}],
        listing_agent_name="Agent", listing_agent_contact="agent@example.com",
        access_confirmed=True, fee_payer="investor", note=""
    ), x_staybot_staff="tester")
    assert result["booking"]["status"] == "proposed"


def test_inspection_summary_quotes_findings_and_does_not_invent_costs():
    offer = {"property_title": "Test Home"}
    report = {"id": "r1"}
    findings = [
        {"severity": "major", "summary": "Roof flashing is damaged", "report_quote": "Damaged flashing observed", "source_page": "4"},
        {"severity": "minor", "summary": "Loose cabinet hinge", "report_quote": "Hinge is loose", "source_page": "7"},
    ]
    text = build_summary(offer, report, findings)
    assert "Roof flashing is damaged" in text
    assert "“Damaged flashing observed”" in text
    assert "Loose cabinet hinge" in text
    assert "No repair cost estimate has been inferred" in text


def test_summary_pdf_is_explicitly_not_a_contract():
    data = render({
        "reference": "PO-TEST",
        "property": {"id": "p1", "title": "Test Home", "address": "123 Main", "city": "Raleigh"},
        "investor": {"name": "Test Investor", "email": "i@example.com", "phone": "+17045550101"},
        "terms": {
            "price": "295000.00", "earnest_money": "10000.00", "due_diligence_fee": "5000.00",
            "due_diligence_end": future_day(10).isoformat(), "financing_deadline": future_day(15).isoformat(),
            "closing_date": future_day(20).isoformat(), "expires_at": future_dt().isoformat(),
            "financing_contingency": "Loan approval", "included": "Existing appliances", "currency": "USD",
        },
        "handoff": {"platform": "unselected", "status": "pending", "link": None},
    }, version=1, generated_at=future_dt().isoformat())
    assert data.startswith(b"%PDF-")
    # Plain text appears in the PDF content stream in many ReportLab builds; also check the source implementation.
    source = (ROOT / "src/services/purchase_offer_pdf.py").read_text(encoding="utf-8")
    assert "Summary for review — not a contract" in source
    assert "is not a purchase contract" in source.lower()


def test_workflow_sql_uses_new_names_and_not_destructive_table_drops():
    sql = (ROOT / "supabase_acquisition_workflows.sql").read_text().lower()
    assert "create table if not exists acquisition_offers" in sql
    assert "acquisition_offer_update_terms" in sql
    assert "acquisition_offer_record_event" in sql
    assert "version_id uuid not null references acquisition_offer_versions(id)" in sql
    assert "revoked_at = now()" in sql
    assert "create table if not exists inspectors" in sql
    assert "create table if not exists inspection_reports" in sql
    assert not re.search(r"\bdrop\s+table\b", sql)
    assert not re.search(r"\btruncate\b", sql)
    assert not re.search(r"\bdelete\s+from\b", sql)


def test_confirm_booking_offer_linked_sends_whatsapp(monkeypatch):
    from src.services import inspections
    from uuid import uuid4

    booking_id = uuid4()
    inspector_id = uuid4()
    offer_id = uuid4()
    booking = {
        "id": str(booking_id), "status": "proposed", "inspector_id": str(inspector_id),
        "offer_id": str(offer_id), "deal_id": None,
        "proposed_slots": [{"date": future_day(2).isoformat(), "time": "10:00"},
                           {"date": future_day(2).isoformat(), "time": "11:00"}],
    }
    updated = {**booking, "status": "confirmed", "confirmed_date": future_day(2).isoformat(), "confirmed_time": "10:00"}
    monkeypatch.setattr(inspections, "get_one", lambda table, params, missing="Not found": booking if table == "inspection_bookings" else {"name": "Inspector One"})
    monkeypatch.setattr(inspections, "q", lambda fn, *args, **kwargs: updated if fn.__name__ == "_patch" else [])
    monkeypatch.setattr(inspections, "check_slot", lambda day, at: None)
    monkeypatch.setattr(inspections.purchase_offers, "get_offer", lambda oid: {"investor_phone": "+15550000001", "property_title": "Test Home"})
    sent = []
    monkeypatch.setattr(inspections.whatsapp, "send_text", lambda phone, text: sent.append((phone, text)))

    result = inspections.confirm_booking(booking_id, inspections.ConfirmBooking(slot_index=0, access_confirmed=True))
    assert result["status"] == "confirmed"
    assert sent and sent[0][0] == "+15550000001"


def test_confirm_booking_deal_only_sends_whatsapp_from_investor_profile(monkeypatch):
    from src.services import inspections
    from uuid import uuid4

    booking_id = uuid4()
    inspector_id = uuid4()
    deal_id = uuid4()
    investor_id = uuid4()
    booking = {
        "id": str(booking_id), "status": "proposed", "inspector_id": str(inspector_id),
        "offer_id": None, "deal_id": str(deal_id),
        "proposed_slots": [{"date": future_day(3).isoformat(), "time": "10:00"},
                           {"date": future_day(3).isoformat(), "time": "11:00"}],
    }
    updated = {**booking, "status": "confirmed", "confirmed_date": future_day(3).isoformat(), "confirmed_time": "10:00"}
    monkeypatch.setattr(inspections, "get_one", lambda table, params, missing="Not found": booking if table == "inspection_bookings" else {"name": "Inspector Two"})
    def fake_q(fn, *args):
        table = args[0]
        params = args[1] if len(args) > 1 else {}
        if fn.__name__ == "_patch":
            return updated
        if table == "investment_deals":
            return [{"id": str(deal_id), "label": "Deal Home", "investor_id": str(investor_id)}]
        if table == "investor_profiles":
            return [{"whatsapp": "+15550000002"}]
        return []
    monkeypatch.setattr(inspections, "q", fake_q)
    monkeypatch.setattr(inspections, "check_slot", lambda day, at: None)
    sent = []
    monkeypatch.setattr(inspections.whatsapp, "send_text", lambda phone, text: sent.append((phone, text)))

    result = inspections.confirm_booking(booking_id, inspections.ConfirmBooking(slot_index=0, access_confirmed=True))
    assert result["status"] == "confirmed"
    assert sent and sent[0][0] == "+15550000002"
    assert "Deal Home" in sent[0][1]


def test_confirm_booking_without_resolvable_investor_phone_still_confirms(monkeypatch):
    from src.services import inspections
    from uuid import uuid4

    booking_id = uuid4()
    inspector_id = uuid4()
    deal_id = uuid4()
    investor_id = uuid4()
    booking = {
        "id": str(booking_id), "status": "proposed", "inspector_id": str(inspector_id),
        "offer_id": None, "deal_id": str(deal_id),
        "proposed_slots": [{"date": future_day(4).isoformat(), "time": "10:00"},
                           {"date": future_day(4).isoformat(), "time": "11:00"}],
    }
    updated = {**booking, "status": "confirmed", "confirmed_date": future_day(4).isoformat(), "confirmed_time": "10:00"}
    monkeypatch.setattr(inspections, "get_one", lambda table, params, missing="Not found": booking if table == "inspection_bookings" else {"name": "Inspector Three"})
    def fake_q(fn, *args):
        table = args[0]
        params = args[1] if len(args) > 1 else {}
        if fn.__name__ == "_patch":
            return updated
        if table == "investment_deals":
            return [{"id": str(deal_id), "label": "No Phone Deal", "investor_id": str(investor_id)}]
        if table == "investor_profiles":
            return [{"whatsapp": None}]
        return []
    monkeypatch.setattr(inspections, "q", fake_q)
    monkeypatch.setattr(inspections, "check_slot", lambda day, at: None)
    sent = []
    monkeypatch.setattr(inspections.whatsapp, "send_text", lambda phone, text: sent.append((phone, text)))

    result = inspections.confirm_booking(booking_id, inspections.ConfirmBooking(slot_index=0, access_confirmed=True))
    assert result["status"] == "confirmed"
    assert sent == []


def test_due_diligence_period_requires_end_date():
    with pytest.raises(ValueError, match="Due diligence end is required"):
        Terms(price="295000", due_diligence_period_days=10, closing_date=future_day(20), expires_at=future_dt())


def test_deadline_worker_code_has_offer_expiry_path():
    source = (ROOT / "src/services/purchase_offers.py").read_text()
    assert "def expire_due_offers" in source
    assert 'p_event_type": "expired' in source


def test_bulk_inspector_import_reports_created_duplicate_and_invalid(monkeypatch):
    from src.services import inspections
    from uuid import uuid4

    existing_id = uuid4()
    calls = []

    def fake_q(fn, *args):
        table = args[0]
        if fn.__name__ == "_get":
            params = args[1]
            license_number = (params.get("license_number") or "").replace("eq.", "")
            if table == "inspectors" and license_number == "NC-DUP":
                return [{"id": str(existing_id)}]
            return []
        if fn.__name__ == "_patch":
            calls.append(("patch", args[1], args[2]))
            return [{"id": str(existing_id), **args[1]}]
        if fn.__name__ == "_post":
            calls.append(("post", args[1]))
            return [{"id": str(uuid4()), **args[1]}]
        raise AssertionError(fn.__name__)

    monkeypatch.setattr(inspections, "q", fake_q)
    rows = [
        {"name": "New Inspector", "license_number": "NC-NEW", "areas_covered": "Wake", "price": "450", "turnaround": "24h", "contact": "new@example.com"},
        {"name": "Updated Inspector", "license_number": "NC-DUP", "areas_covered": "Wake, Durham", "price": "500", "turnaround": "48h", "contact": "dup@example.com"},
        {"name": "", "license_number": "", "areas_covered": "", "price": "not-a-price", "turnaround": "", "contact": ""},
    ]

    result = inspections.bulk_import_inspectors(rows, "team")
    assert [item["status"] for item in result["results"]] == ["created", "updated-duplicate", "rejected"]
    assert result["summary"] == {"total": 3, "created": 1, "updated_duplicate": 1, "rejected": 1}
    assert result["results"][2]["reason"]
    assert any(call[0] == "patch" for call in calls)
    assert any(call[0] == "post" for call in calls)


def test_bulk_inspector_import_is_staff_protected(monkeypatch):
    from fastapi.testclient import TestClient
    from src.main import app
    from src.services import team_auth, inspections

    monkeypatch.setattr(team_auth, "ADMIN_PASSWORD", "test-password")
    monkeypatch.setattr(inspections, "q", lambda *args, **kwargs: [])
    client = TestClient(app)
    payload = [{"name": "Inspector", "license_number": "NC-AUTH", "areas_covered": "Wake", "contact": "x@example.com"}]

    assert client.post("/inspections/inspectors/bulk", json=payload).status_code == 401
    response = client.post("/inspections/inspectors/bulk", json=payload, auth=("team", "test-password"))
    assert response.status_code == 200


def test_deadline_reminder_uses_template_only_for_meta_131047(monkeypatch):
    from src.services import purchase_offers

    monkeypatch.setenv("WHATSAPP_DEADLINE_REMINDER_TEMPLATE", "deadline_reminder")
    monkeypatch.setenv("WHATSAPP_DEADLINE_REMINDER_TEMPLATE_LANGUAGE", "en_US")
    monkeypatch.setattr(purchase_offers, "send_text_or_record", lambda phone, text: {
        "status": "failed", "detail": "(#131047) Re-engagement message: more than 24 hours have passed."
    })
    sent = []
    monkeypatch.setattr(purchase_offers.whatsapp, "send_template", lambda phone, name, language, components: sent.append((phone, name, language, components)) or [{"to": phone, "template": name}])

    result = purchase_offers.send_deadline_reminder(
        "+15550000003",
        "Reminder: Due diligence end for PO-TEST is due tomorrow.",
        {"reference": "PO-TEST"},
        {"label": "Due diligence end", "due_at": future_day(1).isoformat() + "T23:59:59+00:00"},
    )
    assert result["status"] == "accepted"
    assert result["channel"] == "template"
    assert sent and sent[0][1] == "deadline_reminder"
    assert sent[0][2] == "en_US"
    assert len(sent[0][3][0]["parameters"]) == 3


def test_deadline_reminder_keeps_non_window_whatsapp_failure(monkeypatch):
    from src.services import purchase_offers

    monkeypatch.setenv("WHATSAPP_DEADLINE_REMINDER_TEMPLATE", "deadline_reminder")
    monkeypatch.setattr(purchase_offers, "send_text_or_record", lambda phone, text: {
        "status": "failed", "detail": "(#131026) Message rejected for another reason."
    })
    monkeypatch.setattr(purchase_offers.whatsapp, "send_template", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("template fallback must not run")))

    result = purchase_offers.send_deadline_reminder(
        "+15550000004",
        "Reminder text",
        {"reference": "PO-TEST"},
        {"label": "Closing date", "due_at": future_day(2).isoformat() + "T17:00:00+00:00"},
    )
    assert result["status"] == "failed"
    assert "131026" in result["detail"]


def test_deadline_reminder_preserves_131047_failure_when_template_unconfigured(monkeypatch):
    from src.services import purchase_offers

    monkeypatch.delenv("WHATSAPP_DEADLINE_REMINDER_TEMPLATE", raising=False)
    monkeypatch.setattr(purchase_offers, "send_text_or_record", lambda phone, text: {
        "status": "failed", "detail": "(#131047) Re-engagement message"
    })
    monkeypatch.setattr(purchase_offers.whatsapp, "send_template", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("template must not run without configuration")))

    result = purchase_offers.send_deadline_reminder(
        "+15550000005",
        "Reminder text",
        {"reference": "PO-TEST"},
        {"label": "Financing deadline", "due_at": future_day(3).isoformat() + "T23:59:59+00:00"},
    )
    assert result == {"status": "failed", "detail": "(#131047) Re-engagement message"}


def test_whatsapp_send_template_uses_dry_run_outbox(monkeypatch):
    from src.services import whatsapp

    monkeypatch.setattr(whatsapp, "DRY_RUN", True)
    before = len(whatsapp.OUTBOX)
    result = whatsapp.send_template(
        "+15550000006", "deadline_reminder", "en_US", [{"type": "body", "parameters": [{"type": "text", "text": "PO-TEST"}]}]
    )
    assert result and result[0]["template"] == "deadline_reminder"
    assert len(whatsapp.OUTBOX) == before + 1
