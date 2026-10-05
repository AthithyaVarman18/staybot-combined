"""
The Inquiries tab pop-up: GET /inquiries/<id>/profile gathers everything
about the customer behind one enquiry.
"""

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from src import main
from src.services import accounts, db, inquiry_profile

ADMIN = {"id": "adm", "name": "Admin", "email": "a@x.com", "role": "admin", "session_id": "adm-s",
         "investor_id": None, "education_seen": True, "screening_seen": True}
TENANT_ACCOUNT = {"id": "acc-1", "name": "akilesh", "email": "ak@example.com", "role": "tenant",
                  "session_id": "sess-ak", "investor_id": None, "password_hash": "h", "password_salt": "s"}

INQUIRY = {"id": "iq-1", "session_id": "sess-ak", "conversation_id": "conv-1", "property_id": "garner-2bed",
           "property_title": "2 bedroom apartment in Garner", "price_label": "$1,800/mo", "area": "Garner",
           "customer_name": "akilesh", "customer_phone": "7845674316", "status": "new",
           "lead_score": 66, "lead_status": "warm",
           "lead_summary": "Wants to rent a 2 bed in Garner, around $1,800/month. Moving next month."}
OTHER_INQUIRY = {**INQUIRY, "id": "iq-0", "session_id": "another-browser", "customer_phone": "+1 784-567-4316",
                 "property_title": "3 bedroom house near downtown", "status": "closed", "conversation_id": None}
STRANGER = {**INQUIRY, "id": "iq-9", "session_id": "x", "customer_phone": "5550001111", "property_title": "Not theirs"}

CONVERSATION = {
    "id": "conv-1", "session_id": "sess-ak", "listing_title": "General enquiry", "role": "tenant",
    "intent_score": 70, "lead_status": "warm", "summary": "live summary", "human_verification": "unverified",
    "lead_components": {"caps_applied": [], "components": {
        "intent": {"score": 80, "reason": "wants a viewing", "weight": 20},
        "property_fit": {"score": 90, "reason": "exact match found (2 options)", "weight": 15},
        "completeness": {"score": 70, "reason": "knows the area and budget", "weight": 10},
        "sentiment": {"score": 40, "reason": "neutral", "weight": 5},
        "human_verification": {"score": 50, "reason": "unverified", "weight": 5},
    }},
}
MESSAGES = [
    {"role": "user", "content": "2 bed in Garner"},
    {"role": "assistant", "content": "ok", "analysis": {"requirements": {"bedrooms": 2, "location": "Garner"}}},
    {"role": "assistant", "content": "ok", "analysis": {"requirements": {
        "budget": 1800, "rent_or_buy": "rent", "move_in_date": "next month", "location": "Garner"}}},
]
VIEWING = {"id": "v1", "session_id": "sess-ak", "property_title": "2 bedroom apartment in Garner",
           "viewing_date": "2026-10-03", "viewing_time": "11:00:00", "status": "requested"}
APPLICATION = {"id": "app1", "tenant_session_id": "sess-ak", "property_title": "2 bedroom apartment in Garner",
               "status": "team_approved", "details": {}}


def fake_get(table, params=None):
    params = params or {}
    if table == "accounts":
        return [TENANT_ACCOUNT] if params.get("session_id") == "eq.sess-ak" else []
    if table == "conversations":
        return [CONVERSATION]
    if table == "rental_applications":
        return [APPLICATION]
    if table == "property_inquiries":
        return [INQUIRY] if params.get("id") == "eq.iq-1" else []
    if table == "properties":
        return [{"id": "garner-2bed", "listing_type": "rent"}]
    return []


class Profile(unittest.TestCase):

    def build(self):
        with mock.patch.object(db, "_get", side_effect=fake_get), \
             mock.patch.object(db, "list_messages", return_value=MESSAGES), \
             mock.patch.object(db, "list_inquiries", return_value=[INQUIRY, OTHER_INQUIRY, STRANGER]), \
             mock.patch.object(db, "list_viewings", return_value=[VIEWING]):
            return inquiry_profile.build({**INQUIRY, "kind": "tenant"})

    def test_who_and_contact(self):
        p = self.build()
        self.assertEqual(p["customer"]["name"], "akilesh")
        self.assertEqual(p["customer"]["role_label"], "Tenant")
        self.assertEqual(p["customer"]["phone_digits"], "7845674316")
        self.assertNotIn("password_hash", str(p))

    def test_score_is_the_card_snapshot_with_reasons(self):
        p = self.build()
        self.assertEqual(p["lead"]["score"], 66)          # same number the card shows
        self.assertEqual(p["lead"]["status"], "warm")
        self.assertEqual(p["lead"]["reasons"][0], "wants a viewing")  # biggest weighted part first
        self.assertNotIn("neutral", p["lead"]["reasons"])            # weak parts left out

    def test_summary_and_wants(self):
        p = self.build()
        self.assertTrue(p["summary"].startswith("Wants to rent a 2 bed"))
        self.assertEqual(p["wants"], ["2 bed", "Garner", "$1,800/month", "move in next month"])

    def test_homes_match_by_session_or_phone(self):
        p = self.build()
        titles = {h["title"]: h for h in p["homes"]}
        self.assertIn("2 bedroom apartment in Garner", titles)
        self.assertTrue(titles["2 bedroom apartment in Garner"]["current"])
        self.assertEqual(titles["3 bedroom house near downtown"]["status"], "closed")  # same phone, other browser
        self.assertNotIn("Not theirs", titles)

    def test_viewings_applications_chat(self):
        p = self.build()
        self.assertEqual(p["viewings"][0]["when"], "Sat 3 Oct, 11:00am")
        self.assertEqual(p["viewings"][0]["status"], "requested")
        self.assertEqual(p["applications"][0]["text"], "Approved by our team - waiting for the owner")
        self.assertIsNone(p["progress"])        # tenants have no investor journey
        self.assertEqual(p["conversation_id"], "conv-1")

    def test_missing_tables_do_not_break_it(self):
        def broken(table, params=None):
            if table in ("rental_applications", "conversations"):
                raise RuntimeError("relation does not exist")
            return fake_get(table, params)
        with mock.patch.object(db, "_get", side_effect=broken), \
             mock.patch.object(db, "list_messages", return_value=[]), \
             mock.patch.object(db, "list_inquiries", side_effect=RuntimeError("down")), \
             mock.patch.object(db, "list_viewings", return_value=[]), \
             mock.patch.object(db, "get_lead", return_value=None):
            p = inquiry_profile.build({**INQUIRY, "kind": "tenant"})
        self.assertEqual(p["applications"], [])
        self.assertEqual(p["homes"], [])
        self.assertEqual(p["lead"]["score"], 66)

    def test_investor_progress_and_offers(self):
        investor_account = {**TENANT_ACCOUNT, "role": "new_investor", "investor_id": "inv-1"}

        def get(table, params=None):
            if table == "accounts":
                return [investor_account]
            if table == "investors":
                return [{"id": "inv-1", "journey": "new_investor", "stage": "lead_generation"}]
            if table == "acquisition_offers":
                return [{"id": "o1", "property_title": "123 Main St", "status": "negotiating"}]
            return fake_get(table, params)
        with mock.patch.object(db, "_get", side_effect=get), \
             mock.patch.object(db, "list_messages", return_value=[]), \
             mock.patch.object(db, "list_inquiries", return_value=[]), \
             mock.patch.object(db, "list_viewings", return_value=[]):
            p = inquiry_profile.build({**INQUIRY, "kind": "sales"})
        self.assertEqual(p["customer"]["role_label"], "New investor")
        self.assertTrue(p["progress"]["title"])
        self.assertIn("negotiating", [a["status"] for a in p["applications"]])


class Endpoint(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(main.app, follow_redirects=False)

    def test_staff_gets_profile(self):
        with mock.patch.object(accounts, "current_account", return_value=ADMIN), \
             mock.patch.object(db, "ENABLED", True), \
             mock.patch.object(db, "_get", side_effect=fake_get), \
             mock.patch.object(db, "list_messages", return_value=MESSAGES), \
             mock.patch.object(db, "list_inquiries", return_value=[INQUIRY]), \
             mock.patch.object(db, "list_viewings", return_value=[VIEWING]):
            res = self.client.get("/inquiries/iq-1/profile")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["customer"]["name"], "akilesh")

    def test_unknown_inquiry_404(self):
        with mock.patch.object(accounts, "current_account", return_value=ADMIN), \
             mock.patch.object(db, "ENABLED", True), \
             mock.patch.object(db, "_get", side_effect=fake_get):
            self.assertEqual(self.client.get("/inquiries/nope/profile").status_code, 404)

    def test_customers_cannot_read_profiles(self):
        with mock.patch.object(accounts, "current_account", return_value=TENANT_ACCOUNT):
            self.assertEqual(self.client.get("/inquiries/iq-1/profile").status_code, 403)


if __name__ == "__main__":
    unittest.main()
