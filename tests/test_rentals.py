"""Tenant applies -> team approves -> owner approves -> maintenance works."""

import itertools
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from fastapi.testclient import TestClient

from src import main
from src.services import accounts, db, maintenance, owner_listings, properties, rentals

ACCTS = [
    {"id": "t1", "name": "Tia Tenant", "email": "t@x.com", "role": "tenant", "session_id": "acct-t1", "screening_seen": True},
    {"id": "t2", "name": "Tom Tenant", "email": "t2@x.com", "role": "tenant", "session_id": "acct-t2", "screening_seen": True},
    {"id": "i1", "name": "Ivan Investor", "email": "i@x.com", "role": "existing_investor", "session_id": "acct-i1"},
    {"id": "i2", "name": "Olga Other", "email": "o@x.com", "role": "new_investor", "session_id": "acct-i2", "education_seen": True},
    {"id": "a1", "name": "Admin", "email": "admin@x.com", "role": "admin", "session_id": "acct-a1"},
]


class FakeRest:
    """Just enough PostgREST (eq / in filters) for these flows."""

    def __init__(self):
        self.t = {"accounts": [dict(a, password_hash="h", password_salt="s") for a in ACCTS],
                  "account_sessions": [], "properties": [], "rental_applications": [], "maintenance_tickets": [],
                  "rental_threads": [], "rental_messages": [], "tenant_offers": [],
                  "account_portfolios": [{"account_id": "t1", "details": {"employment_status": "employed", "monthly_income": 6000, "phone": "+19195550101"}}],
                  "account_documents": []}

    @staticmethod
    def match(row, params):
        for k, v in (params or {}).items():
            if k in ("select", "order", "limit"):
                continue
            op, _, val = str(v).partition(".")
            if op == "eq" and str(row.get(k)).lower() != val.lower():
                return False
            # PostgREST allows quoted values in in.(...) - "wa:+1919..." etc.
            if op == "in" and str(row.get(k)) not in [v.strip('"') for v in val.strip("()").split(",")]:
                return False
            if op == "gt" and not (row.get(k) and str(row.get(k)) > val):
                return False
        return True

    def get(self, table, params=None):
        rows = [dict(r) for r in self.t.get(table, []) if self.match(r, params)]
        return rows[: int(params["limit"])] if params and "limit" in params else rows

    _tick = itertools.count(1)

    def post(self, table, body, params=None):
        # Real, strictly increasing timestamps (read markers use real now()).
        now = (datetime.now(timezone.utc) + timedelta(microseconds=next(self._tick))).isoformat()
        row = {"id": str(uuid.uuid4()), "created_at": now, "updated_at": now, **body}
        self.t[table].append(row)
        return dict(row)

    def patch(self, table, body, params):
        hit = [r for r in self.t[table] if self.match(r, params)]
        for r in hit:
            r.update(body)
        return dict(hit[0]) if hit else []

    def delete(self, table, params):
        self.t[table] = [r for r in self.t[table] if not self.match(r, params)]


class RentalFixture(unittest.TestCase):
    """Logged-in tenant(s), owners and admin, and one owner-listed home."""

    def setUp(self):
        f = self.fake = FakeRest()
        patches = [
            mock.patch.object(db, "ENABLED", True),
            mock.patch.object(db, "_get", f.get), mock.patch.object(db, "_post", f.post),
            mock.patch.object(db, "_patch", f.patch), mock.patch.object(db, "_delete", f.delete),
            mock.patch.object(db, "create_property", lambda fields: f.post("properties", fields)),
            mock.patch.object(db, "update_property", lambda pid, fields: f.patch("properties", fields, {"id": f"eq.{pid}"})),
            mock.patch.object(db, "list_properties", lambda: f.get("properties", {"status": "eq.active"})),
            mock.patch.object(db, "create_maintenance_ticket", lambda fields: f.post("maintenance_tickets", fields)),
            mock.patch.object(db, "update_maintenance_ticket", lambda tid, fields: f.patch("maintenance_tickets", fields, {"id": f"eq.{tid}"})),
            mock.patch.object(maintenance, "classify_maintenance_ticket", lambda **kw: {
                "issueType": "plumbing", "urgency": "normal", "confidence": 0.9, "photoTextMatch": None,
                "summary": "Leaking tap", "recommendedAction": "Send a plumber", "status": "classified"}),
            mock.patch.object(accounts, "verify_password", return_value=True),
            mock.patch.object(owner_listings, "INVESTOR_LISTINGS_NEED_REVIEW", False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        properties.clear_cache()
        self.addCleanup(properties.clear_cache)
        self.c = TestClient(main.app)
        self.tenant = self.login("t@x.com", "tenant")
        self.tenant2 = self.login("t2@x.com", "tenant")
        self.owner = self.login("i@x.com", "existing_investor")
        self.other_owner = self.login("o@x.com", "new_investor")
        r = self.c.post("/auth/admin/login", json={"email": "admin@x.com", "password": "p"})
        self.team = {"X-Staybot-Session": r.headers["X-Staybot-Session-Issued"], "X-Staybot-Staff": "Priya"}

        r = self.c.post("/me/listings", headers=self.owner, json={
            "listing_type": "rent", "property_type": "house", "bedrooms": 3, "location": "431 Longfellow St, Fuquay Varina", "rent": 1800})
        self.home = r.json()

    def login(self, email, role):
        r = self.c.post("/auth/login", json={"email": email, "password": "p", "role": role})
        self.assertEqual(r.status_code, 200, r.text)
        return {"X-Staybot-Session": r.headers["X-Staybot-Session-Issued"]}

    def apply(self, who):
        r = self.c.post("/me/rentals/applications", headers=who, json={"property_id": self.home["id"], "message": "Quiet couple, no pets"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def owner_apps(self, who=None):
        return self.c.get("/me/owner/applications", headers=who or self.owner).json()["applications"]


class RentalFlow(RentalFixture):

    def test_full_flow(self):
        app = self.apply(self.tenant)
        self.assertEqual(app["status"], "submitted")
        self.assertNotIn("tenant_session_id", app)

        # The owner sees it straight away - marked as with our team, without the
        # tenant's contact details or income yet - but can't decide on it yet.
        [seen] = self.owner_apps()
        self.assertEqual((seen["id"], seen["status"]), (app["id"], "submitted"))
        self.assertEqual(seen["tenant_name"], "Tia Tenant")
        self.assertEqual(seen["message"], "Quiet couple, no pets")
        for private in ("tenant_email", "tenant_phone", "screening"):
            self.assertNotIn(private, seen)
        r = self.c.post(f"/me/owner/applications/{app['id']}/decision", headers=self.owner, json={"decision": "approve"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("still reviewing", r.json()["detail"])
        self.assertEqual(self.owner_apps(self.other_owner), [])  # never another owner's
        # Maintenance stays locked for the tenant.
        r = self.c.post("/me/rentals/maintenance", headers=self.tenant, data={"description": "Tap leaking"})
        self.assertEqual(r.status_code, 403)

        # Tenants and owners can't use the team's approval step or see every ticket.
        self.assertEqual(self.c.get("/applications", headers=self.tenant).status_code, 403)
        self.assertEqual(self.c.get("/maintenance/tickets", headers=self.owner).status_code, 403)

        # The team's list says whose home it is.
        team_view = self.c.get("/applications", headers=self.team).json()["applications"]
        self.assertEqual(team_view[0]["owner_name"], "Ivan Investor")

        # Team approves -> goes to the owner, with the screening summary.
        r = self.c.post(f"/applications/{app['id']}/review", headers=self.team, json={"decision": "approve", "note": "Income verified"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "team_approved")
        self.assertEqual(self.fake.t["rental_applications"][0]["team_reviewed_by"], "staff:Priya")
        mine = self.owner_apps()
        self.assertEqual([a["status"] for a in mine], ["team_approved"])
        self.assertEqual(mine[0]["screening"]["monthly_income"], 6000)
        self.assertEqual(mine[0]["tenant_email"], "t@x.com")  # contact details once the team approves
        self.assertEqual(self.owner_apps(self.other_owner), [])  # another owner never sees it

        # Still locked until the owner approves too.
        self.assertIsNone(self.c.get("/me/rentals", headers=self.tenant).json()["home"])

        # A second applicant for the same home.
        other = self.apply(self.tenant2)

        # Owner approves -> tenant rents the home.
        r = self.c.post(f"/me/owner/applications/{app['id']}/decision", headers=self.owner, json={"decision": "approve"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "approved")
        me = self.c.get("/me/rentals", headers=self.tenant).json()
        self.assertEqual(me["home"]["property_id"], self.home["id"])
        # Home is let: off the tenants' Homes list; the other applicant is closed.
        self.assertNotIn(self.home["id"], [p["id"] for p in self.c.get("/properties", headers=self.tenant).json()["properties"]])
        self.assertEqual(next(a for a in self.fake.t["rental_applications"] if a["id"] == other["id"])["status"], "closed")

        # Maintenance now works, filed against the rented home...
        r = self.c.post("/me/rentals/maintenance", headers=self.tenant, data={"description": "Kitchen tap is leaking"})
        self.assertEqual(r.status_code, 200, r.text)
        ticket = r.json()["ticket"]
        self.assertEqual(ticket["property_id"], self.home["id"])
        # ...and reaches the owner, who can resolve it.
        tickets = self.c.get("/me/owner/maintenance", headers=self.owner).json()["tickets"]
        self.assertEqual([t["id"] for t in tickets], [ticket["id"]])
        self.assertEqual(self.c.get("/me/owner/maintenance", headers=self.other_owner).json()["tickets"], [])
        self.assertEqual(self.c.patch(f"/me/owner/maintenance/{ticket['id']}", headers=self.other_owner,
                                      json={"ticket_status": "resolved"}).status_code, 404)
        r = self.c.patch(f"/me/owner/maintenance/{ticket['id']}", headers=self.owner, json={"ticket_status": "resolved", "notes": "Washer replaced"})
        self.assertEqual(r.status_code, 200, r.text)
        mine = self.c.get("/me/rentals/maintenance", headers=self.tenant).json()["tickets"]
        self.assertEqual((mine[0]["ticket_status"], mine[0]["notes"]), ("resolved", "Washer replaced"))

        # Ending the tenancy locks maintenance again.
        self.c.post(f"/me/owner/applications/{app['id']}/end", headers=self.owner)
        self.assertEqual(self.c.post("/me/rentals/maintenance", headers=self.tenant, data={"description": "Another leak"}).status_code, 403)

    def test_declines(self):
        app = self.apply(self.tenant)
        r = self.c.post(f"/applications/{app['id']}/review", headers=self.team, json={"decision": "decline", "note": "Income too low"})
        self.assertEqual(r.json()["status"], "team_declined")
        # Declined by the team -> only in the owner's history, with no contact details.
        [seen] = self.owner_apps()
        self.assertEqual(seen["status"], "team_declined")
        self.assertNotIn("tenant_email", seen)
        self.assertEqual(self.c.post(f"/me/owner/applications/{app['id']}/decision", headers=self.owner,
                                     json={"decision": "approve"}).status_code, 409)

        app2 = self.apply(self.tenant2)
        self.c.post(f"/applications/{app2['id']}/review", headers=self.team, json={"decision": "approve"})
        r = self.c.post(f"/me/owner/applications/{app2['id']}/decision", headers=self.owner, json={"decision": "decline", "note": "Went with family"})
        self.assertEqual(r.json()["status"], "owner_declined")
        self.assertIsNone(self.c.get("/me/rentals", headers=self.tenant2).json()["home"])

    def test_no_duplicate_applications_and_withdraw(self):
        app = self.apply(self.tenant)
        r = self.c.post("/me/rentals/applications", headers=self.tenant, json={"property_id": self.home["id"]})
        self.assertEqual(r.status_code, 409)
        r = self.c.post(f"/me/rentals/applications/{app['id']}/withdraw", headers=self.tenant2)
        self.assertEqual(r.status_code, 404)  # not theirs
        r = self.c.post(f"/me/rentals/applications/{app['id']}/withdraw", headers=self.tenant)
        self.assertEqual(r.json()["status"], "withdrawn")

    def test_team_approval_is_final_for_homes_no_investor_owns(self):
        self.fake.t["properties"].append({"id": "team-home", "title": "Team listed home", "status": "active",
                                          "listing_type": "rent", "source": "admin", "rent": 1500})
        r = self.c.post("/me/rentals/applications", headers=self.tenant, json={"property_id": "team-home"})
        app = r.json()
        self.assertFalse(app["has_owner_step"])
        r = self.c.post(f"/applications/{app['id']}/review", headers=self.team, json={"decision": "approve"})
        self.assertEqual(r.json()["status"], "approved")
        self.assertEqual(self.c.get("/me/rentals", headers=self.tenant).json()["home"]["property_id"], "team-home")

    def test_chat_maintenance_locked_then_filed_against_home(self):
        from src.services import chat

        def ai(**kw):
            return {"role": "tenant", "intent": "maintenance_issue", "response": "Which unit are you in?",
                    "maintenance_description": "Kitchen tap leaking"}

        with mock.patch.object(chat, "analyze_message", side_effect=ai), \
             mock.patch.object(chat, "_fact_check", lambda *a, **k: None), \
             mock.patch.object(chat.quick_replies, "quick_reply", lambda *a, **k: None), \
             mock.patch.object(db, "get_or_create_conversation", lambda **kw: {"id": "conv-1"}), \
             mock.patch.object(db, "add_message", lambda *a, **k: None), \
             mock.patch.object(db, "update_conversation", lambda *a, **k: None), \
             mock.patch.object(db, "find_open_maintenance_ticket", lambda cid: None):
            # Not renting yet -> told how to get there, nothing filed.
            r = chat.process_message("The kitchen tap is leaking", session_id="acct-t1", account_role="tenant", account_id="t1")
            self.assertEqual(r["response"], rentals.MAINTENANCE_LOCKED)
            self.assertEqual(self.fake.t["maintenance_tickets"], [])

            app = self.apply(self.tenant)
            self.c.post(f"/applications/{app['id']}/review", headers=self.team, json={"decision": "approve"})
            self.c.post(f"/me/owner/applications/{app['id']}/decision", headers=self.owner, json={"decision": "approve"})

            seen = {}
            def ai2(**kw):
                seen.update(kw)
                return ai(**kw)
            # The repair assistant (maintenance_chat.py) answers chat repairs too;
            # here it hands the leak straight to the owner and team.
            from src.services import maintenance_chat
            turn = {"reply": "I've sent this to your owner and the team.", "action": "escalate", "emergency": False,
                    "issue_type": "plumbing", "urgency": "normal", "escalation_reason": "Leak needs a plumber",
                    "summary": "Kitchen tap leaking.", "home_address": None}
            with mock.patch.object(chat, "analyze_message", side_effect=ai2), \
                 mock.patch.object(maintenance_chat, "ask_ai", return_value=turn), \
                 mock.patch.object(db, "list_messages", lambda cid: []), \
                 mock.patch.object(db, "get_maintenance_ticket", lambda tid: None):
                chat.process_message("The kitchen tap is leaking", session_id="acct-t1", account_role="tenant", account_id="t1")
            self.assertIn("do NOT ask for a unit", seen["account_note"])
            [t] = self.fake.t["maintenance_tickets"]
            self.assertEqual(t["property_id"], self.home["id"])
            self.assertEqual(t["tenant_id"], "431 Longfellow St, Fuquay Varina")


class ApplicationOnboarding(RentalFixture):
    """The "Apply to rent" onboarding checklist reaches the owner as a summary."""

    def details(self, **over):
        d = {"full_name": "Tia Tenant", "phone": "+19195550101", "move_in_date": (date.today() + timedelta(days=20)).isoformat(),
             "lease_months": 12, "adults": 2, "children": 1, "has_pets": True, "pet_details": "1 small dog",
             "smoker": False, "vehicles": 1, "employment_status": "employed", "employer": "Duke Health",
             "job_title": "Nurse", "time_at_job": "3 years", "monthly_income": 5000,
             "current_address": "12 Oak St, Durham", "landlord_name": "Sam Lee", "landlord_phone": "+19195550999",
             "reason_for_moving": "Closer to work", "evicted_before": False,
             "emergency_name": "Ana Tenant", "emergency_relationship": "Sister", "emergency_phone": "+19195550777",
             "consent": True}
        d.update(over)
        return d

    def test_prefill_uses_account_and_screening(self):
        r = self.c.get(f"/me/rentals/apply-prefill?property_id={self.home['id']}", headers=self.tenant)
        self.assertEqual(r.status_code, 200, r.text)
        pre = r.json()
        self.assertEqual(pre["prefill"]["full_name"], "Tia Tenant")
        self.assertEqual(pre["prefill"]["monthly_income"], 6000)
        self.assertEqual(pre["prefill"]["phone"], "+19195550101")
        self.assertEqual(pre["listing"]["rent"], 1800)
        self.assertEqual(self.c.get(f"/me/rentals/apply-prefill?property_id={self.home['id']}", headers=self.owner).status_code, 403)

    def test_checklist_validation(self):
        for bad in (self.details(consent=False), self.details(move_in_date="2020-01-01"),
                    self.details(has_pets=True, pet_details=""), self.details(adults=0)):
            r = self.c.post("/me/rentals/applications", headers=self.tenant, json={"property_id": self.home["id"], "details": bad})
            self.assertEqual(r.status_code, 422, bad)
        self.assertEqual(self.fake.t["rental_applications"], [])

    def test_owner_gets_summary_and_contacts_after_team(self):
        self.fake.patch("properties", {"pets_allowed": False}, {"id": f"eq.{self.home['id']}"})
        r = self.c.post("/me/rentals/applications", headers=self.tenant,
                        json={"property_id": self.home["id"], "message": "We love the garden", "details": self.details()})
        self.assertEqual(r.status_code, 200, r.text)
        app = r.json()
        self.assertEqual(app["move_in_date"], self.details()["move_in_date"])
        self.assertEqual(app["details"]["listing"]["rent"], 1800)

        # Owner sees the whole onboarding summary straight away...
        [seen] = self.owner_apps()
        d = seen["details"]
        self.assertEqual((d["adults"], d["children"], d["employer"], d["monthly_income"]), (2, 1, "Duke Health", 5000))
        self.assertIn("2 adults, 1 child", seen["summary"]["headline"])
        self.assertEqual(seen["summary"]["income_to_rent"], 2.8)
        self.assertIn("Has pets - this home is listed as no pets", seen["summary"]["flags"])
        self.assertTrue(any("under the usual 3x" in f for f in seen["summary"]["flags"]))
        # ...but not the contact details until our team has cleared the tenant.
        for private in ("phone", "email", "current_address", "landlord_phone", "emergency_phone", "emergency_name"):
            self.assertNotIn(private, d)
        self.assertTrue(d["contact_hidden"])
        self.assertNotIn("tenant_phone", seen)

        self.c.post(f"/applications/{app['id']}/review", headers=self.team, json={"decision": "approve"})
        [seen] = self.owner_apps()
        self.assertEqual(seen["details"]["emergency_phone"], "+19195550777")
        self.assertEqual(seen["details"]["email"], "t@x.com")
        self.assertNotIn("contact_hidden", seen["details"])
        self.assertEqual(seen["tenant_phone"], "+19195550101")

        # The team always sees everything; the next application is prefilled from this one.
        team = self.c.get("/applications", headers=self.team).json()["applications"][0]
        self.assertEqual(team["details"]["landlord_phone"], "+19195550999")
        pre = self.c.get(f"/me/rentals/apply-prefill?property_id={self.home['id']}", headers=self.tenant).json()["prefill"]
        self.assertEqual((pre["employer"], pre["monthly_income"]), ("Duke Health", 5000))
        self.assertNotIn("consent", pre)

    def test_complete_an_application_sent_without_details(self):
        old = self.apply(self.tenant)  # the old dialog: no checklist
        self.assertFalse(old["has_details"])
        [seen] = self.owner_apps()
        self.assertNotIn("summary", seen)

        url = f"/me/rentals/applications/{old['id']}/details"
        self.assertEqual(self.c.post(url, headers=self.tenant2, json={"details": self.details()}).status_code, 404)
        self.assertEqual(self.c.post(url, headers=self.tenant, json={"details": self.details(consent=False)}).status_code, 422)
        r = self.c.post(url, headers=self.tenant, json={"details": self.details(), "message": "Updated note"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["has_details"])
        self.assertEqual(r.json()["move_in_date"], self.details()["move_in_date"])

        [seen] = self.owner_apps()
        self.assertIn("2 adults, 1 child", seen["summary"]["headline"])
        self.assertEqual(seen["message"], "Updated note")
        self.assertNotIn("phone", seen["details"])  # still hidden until the team approves

        # Once decided, details are locked.
        self.c.post(f"/applications/{old['id']}/review", headers=self.team, json={"decision": "decline", "note": "x"})
        self.assertEqual(self.c.post(url, headers=self.tenant, json={"details": self.details()}).status_code, 409)


class OwnerOffers(RentalFixture):
    """Owner sends a purchase offer (staff-page terms) -> tenant sees it on Homes and answers."""

    def terms(self, **over):
        soon = date.today() + timedelta(days=30)
        t = {"price": "245000", "earnest_money": "2500", "due_diligence_fee": "1000", "due_diligence_period_days": 14,
             "due_diligence_end": (date.today() + timedelta(days=14)).isoformat(), "closing_date": soon.isoformat(),
             "financing_deadline": (date.today() + timedelta(days=21)).isoformat(),
             "financing_contingency": "Conventional loan", "included": "Fridge, washer",
             "expires_at": (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()}
        t.update(over)
        return t

    def test_offer_round_trip(self):
        app = self.apply(self.tenant)
        url = f"/me/owner/applications/{app['id']}/offer"
        # Same validation as the staff page; only this home's owner can send it.
        self.assertEqual(self.c.post(url, headers=self.owner, json={"terms": self.terms(closing_date="2020-01-01")}).status_code, 422)
        self.assertEqual(self.c.post(url, headers=self.other_owner, json={"terms": self.terms()}).status_code, 404)
        self.assertEqual(self.c.post(url, headers=self.tenant, json={"terms": self.terms()}).status_code, 403)

        r = self.c.post(url, headers=self.owner, json={"terms": self.terms()})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["status"], r.json()["terms"]["price"]), ("sent", "245000.00"))
        # A new offer replaces the waiting one.
        second = self.c.post(url, headers=self.owner, json={"terms": self.terms(price="240000")}).json()
        self.assertEqual([o["status"] for o in self.fake.t["tenant_offers"]], ["replaced", "sent"])

        # The tenant sees it with their application; the owner sees its status.
        [mine] = self.c.get("/me/rentals", headers=self.tenant).json()["applications"]
        self.assertEqual((mine["offer"]["id"], mine["offer"]["terms"]["price"]), (second["id"], "240000.00"))
        self.assertEqual(self.owner_apps()[0]["offer"]["status"], "sent")

        reply = f"/me/rentals/offers/{second['id']}/respond"
        self.assertEqual(self.c.post(reply, headers=self.tenant2, json={"decision": "accept"}).status_code, 404)
        r = self.c.post(reply, headers=self.tenant, json={"decision": "accept", "note": "Happy with this"})
        self.assertEqual(r.json()["status"], "accepted")
        self.assertEqual(self.c.post(reply, headers=self.tenant, json={"decision": "decline"}).status_code, 409)
        self.assertEqual(self.owner_apps()[0]["offer"]["tenant_note"], "Happy with this")
        team = self.c.get("/applications", headers=self.team).json()["applications"][0]
        self.assertEqual(team["offer"]["status"], "accepted")

    def test_expired_and_withdrawn_offers_cant_be_answered(self):
        app = self.apply(self.tenant)
        url = f"/me/owner/applications/{app['id']}/offer"
        o = self.c.post(url, headers=self.owner, json={"terms": self.terms()}).json()
        self.fake.t["tenant_offers"][0]["terms"]["expires_at"] = "2020-01-01T00:00:00+00:00"
        self.assertEqual(self.c.get("/me/rentals", headers=self.tenant).json()["applications"][0]["offer"]["status"], "expired")
        self.assertEqual(self.c.post(f"/me/rentals/offers/{o['id']}/respond", headers=self.tenant, json={"decision": "accept"}).status_code, 409)

        o2 = self.c.post(url, headers=self.owner, json={"terms": self.terms()}).json()
        self.assertEqual(self.c.post(f"{url}/withdraw", headers=self.owner).json()["status"], "withdrawn")
        self.assertEqual(self.c.post(f"/me/rentals/offers/{o2['id']}/respond", headers=self.tenant, json={"decision": "accept"}).status_code, 409)

    def test_tenant_modifies_offer_and_owner_answers(self):
        app = self.apply(self.tenant)
        url = f"/me/owner/applications/{app['id']}/offer"
        o1 = self.c.post(url, headers=self.owner, json={"terms": self.terms()}).json()
        self.assertEqual((o1["from_party"], o1["waiting_on"]), ("owner", "tenant"))

        counter = f"/me/rentals/offers/{o1['id']}/counter"
        self.assertEqual(self.c.post(counter, headers=self.tenant2, json={"terms": self.terms()}).status_code, 404)
        self.assertEqual(self.c.post(counter, headers=self.tenant, json={"terms": self.terms(price="")}).status_code, 422)
        r = self.c.post(counter, headers=self.tenant, json={"terms": self.terms(price="230000", included="Fridge only"),
                                                             "message": "Can we meet at 230k?"})
        self.assertEqual(r.status_code, 200, r.text)
        o2 = r.json()
        self.assertEqual((o2["from_party"], o2["waiting_on"], o2["status_text"]), ("tenant", "owner", "Waiting for the owner"))

        # Both sides see the counter, what changed, and the tenant's note.
        [mine] = self.c.get("/me/rentals", headers=self.tenant).json()["applications"]
        self.assertEqual(mine["offer"]["changed"], ["included", "price"])
        # The form sends the expiry back to the minute; that alone isn't a change.
        exp = self.terms()["expires_at"].replace("+00:00", ".000Z")
        o3 = self.c.post(f"/me/rentals/offers/{o2['id']}/counter", headers=self.tenant,
                         json={"terms": self.terms(price="230000", included="Fridge only", expires_at=exp)}).json()
        [mine] = self.c.get("/me/rentals", headers=self.tenant).json()["applications"]
        self.assertEqual(mine["offer"]["changed"], [])
        o2 = o3
        self.assertEqual(mine["offer"]["previous_terms"]["price"], "230000.00")  # version 2 -> 3
        [seen] = self.owner_apps()
        self.assertEqual(seen["offer"]["version"], 3)

        # The tenant can't accept their own counter; the owner can't withdraw it.
        self.assertEqual(self.c.post(f"/me/rentals/offers/{o2['id']}/respond", headers=self.tenant, json={"decision": "accept"}).status_code, 409)
        self.assertEqual(self.c.post(f"{url}/withdraw", headers=self.owner).status_code, 409)
        # The old owner version can't be answered any more either.
        self.assertEqual(self.c.post(f"/me/rentals/offers/{o1['id']}/respond", headers=self.tenant, json={"decision": "accept"}).status_code, 409)

        # Owner modifies again -> back to the tenant, who modifies once more -> owner accepts.
        o3 = self.c.post(url, headers=self.owner, json={"terms": self.terms(price="237500")}).json()
        self.assertEqual(o3["waiting_on"], "tenant")
        o4 = self.c.post(f"/me/rentals/offers/{o3['id']}/counter", headers=self.tenant, json={"terms": self.terms(price="235000")}).json()
        self.assertEqual(self.c.post(f"{url}/respond", headers=self.other_owner, json={"decision": "accept"}).status_code, 404)
        r = self.c.post(f"{url}/respond", headers=self.owner, json={"decision": "accept", "note": "Deal at 235k"})
        self.assertEqual((r.json()["status"], r.json()["status_text"]), ("accepted", "Accepted by the owner"))
        [mine] = self.c.get("/me/rentals", headers=self.tenant).json()["applications"]
        self.assertEqual((mine["offer"]["id"], mine["offer"]["owner_note"], mine["offer"]["version"]), (o4["id"], "Deal at 235k", 5))
        self.assertEqual(self.c.post(f"{url}/respond", headers=self.owner, json={"decision": "decline"}).status_code, 409)
        # Once answered, it can't be modified.
        self.assertEqual(self.c.post(f"/me/rentals/offers/{o4['id']}/counter", headers=self.tenant, json={"terms": self.terms()}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
