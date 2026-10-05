"""An investor lists a home (chat or My listings tab) -> tenants see it."""

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from src import main
from src.services import accounts, db, owner_listings, properties

ACCTS = {
    "t": {"id": "t", "name": "Tia Tenant", "email": "t@x.com", "role": "tenant", "session_id": "acct-t",
          "password_hash": "h", "password_salt": "s", "screening_seen": True},
    "i": {"id": "i", "name": "Ivan Investor", "email": "i@x.com", "role": "existing_investor", "session_id": "acct-i",
          "password_hash": "h", "password_salt": "s"},
}


class FakeDB:
    def __init__(self):
        self.sessions, self.props = {}, {}

    def _match(self, row, params):
        for k, v in params.items():
            if k in ("select", "order", "limit"):
                continue
            op, _, val = v.partition(".")
            if op == "eq" and str(row.get(k)) != val:
                return False
            if op == "in" and str(row.get(k)) not in val.strip("()").split(","):
                return False
        return True

    def get(self, table, params=None):
        params = params or {}
        if table == "accounts":
            if "email" in params:
                return [a for a in ACCTS.values() if a["email"] == params["email"][3:]]
            return [ACCTS[params["id"][3:]]]
        if table == "account_sessions":
            h = params["token_hash"][3:]
            return [self.sessions[h]] if h in self.sessions else []
        if table == "properties":
            return [dict(p) for p in self.props.values() if self._match(p, params)]
        return []

    def post(self, table, row, params=None):
        if table == "account_sessions":
            self.sessions[row["token_hash"]] = row
        return row

    def create_property(self, fields):
        row = {**fields, "created_at": "2026-09-26T10:00:00+00:00", "updated_at": "2026-09-26T10:00:00+00:00"}
        self.props[row["id"]] = row
        return dict(row)

    def update_property(self, pid, fields):
        self.props[pid].update(fields)
        return dict(self.props[pid])

    def list_properties(self):
        return [dict(p) for p in self.props.values() if p.get("status") == "active"]

    def find_owner_draft(self, conversation_id=None, session_id=None, statuses=("pending",)):
        rows = [p for p in self.props.values() if p.get("source") == "owner_chat" and p.get("status") in statuses
                and (p.get("conversation_id") == conversation_id if conversation_id else p.get("session_id") == session_id)]
        return dict(rows[-1]) if rows else None


class InvestorListingsReachTenants(unittest.TestCase):

    def setUp(self):
        self.fake = FakeDB()
        patches = [
            mock.patch.object(db, "ENABLED", True),
            mock.patch.object(accounts.db, "ENABLED", True),
            mock.patch.object(db, "_get", self.fake.get),
            mock.patch.object(db, "_post", self.fake.post),
            mock.patch.object(db, "create_property", self.fake.create_property),
            mock.patch.object(db, "update_property", self.fake.update_property),
            mock.patch.object(db, "list_properties", self.fake.list_properties),
            mock.patch.object(db, "find_owner_draft", self.fake.find_owner_draft),
            mock.patch.object(accounts, "verify_password", return_value=True),
            mock.patch.object(owner_listings, "INVESTOR_LISTINGS_NEED_REVIEW", False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        properties.clear_cache()
        self.addCleanup(properties.clear_cache)
        self.client = TestClient(main.app)

    def login(self, email, role):
        r = self.client.post("/auth/login", json={"email": email, "password": "p", "role": role})
        self.assertEqual(r.status_code, 200, r.text)
        return {"X-Staybot-Session": r.headers["X-Staybot-Session-Issued"]}

    def tenant_home_ids(self, tenant):
        r = self.client.get("/properties", headers=tenant)
        self.assertEqual(r.status_code, 200, r.text)
        return [p["id"] for p in r.json()["properties"]]

    def test_form_listing_is_live_for_tenants(self):
        investor = self.login("i@x.com", "existing_investor")
        tenant = self.login("t@x.com", "tenant")

        r = self.client.post("/me/listings", headers=investor, json={
            "listing_type": "rent", "property_type": "house", "bedrooms": 3, "bathrooms": 2,
            "location": "431 Longfellow St, Fuquay Varina", "rent": 1800, "pets_allowed": True,
        })
        self.assertEqual(r.status_code, 200, r.text)
        listing = r.json()
        self.assertTrue(listing["visible_to_tenants"])
        self.assertEqual(listing["source"], "investor_form")
        self.assertIn(listing["id"], self.tenant_home_ids(tenant))

        # Private owner fields never reach the tenant side.
        home = next(p for p in self.client.get("/properties", headers=tenant).json()["properties"] if p["id"] == listing["id"])
        self.assertNotIn("session_id", home)
        self.assertNotIn("owner_name", home)

        # The investor sees it on My listings; hiding it removes it for tenants.
        mine = self.client.get("/me/listings", headers=investor).json()["listings"]
        self.assertEqual([p["id"] for p in mine], [listing["id"]])
        r = self.client.patch(f"/me/listings/{listing['id']}", headers=investor, json={"status": "hidden"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(listing["id"], self.tenant_home_ids(tenant))

    def test_tenants_cannot_list_or_touch_others_listings(self):
        investor = self.login("i@x.com", "existing_investor")
        tenant = self.login("t@x.com", "tenant")
        r = self.client.post("/me/listings", headers=tenant, json={"location": "Garner", "rent": 1500, "bedrooms": 2})
        self.assertEqual(r.status_code, 403)
        pid = self.client.post("/me/listings", headers=investor, json={"location": "Garner", "rent": 1500, "bedrooms": 2}).json()["id"]
        self.assertEqual(self.client.patch(f"/me/listings/{pid}", headers=tenant, json={"status": "hidden"}).status_code, 403)

    def test_missing_price_is_rejected(self):
        investor = self.login("i@x.com", "existing_investor")
        r = self.client.post("/me/listings", headers=investor, json={"location": "Garner", "bedrooms": 2})
        self.assertEqual(r.status_code, 400)

    def test_chat_listing_from_investor_goes_live_and_updates_in_place(self):
        result = {"role": "owner", "response": "Great!", "property_details": {
            "property_type": "house", "bedrooms": 3, "location": "Garner", "rent": "1800"}}
        owner_listings.handle_owner_listing(result, conversation_id="c1", session_id="acct-i",
                                            known_name="Ivan Investor", account_role="existing_investor")
        self.assertTrue(result["owner_listing"]["live"])
        self.assertIn("live", result["response"])
        pid = result["owner_listing"]["id"]
        self.assertEqual(self.fake.props[pid]["status"], "active")

        # A later detail updates the same live listing instead of creating a second one.
        more = {"role": "owner", "response": "Noted.", "property_details": {
            "property_type": "house", "bedrooms": 3, "location": "Garner", "rent": "1800", "parking": "yes"}}
        owner_listings.handle_owner_listing(more, conversation_id="c1", session_id="acct-i", account_role="existing_investor")
        self.assertEqual(len(self.fake.props), 1)
        self.assertTrue(self.fake.props[pid]["parking"])

    def test_anonymous_owner_chat_still_needs_review(self):
        result = {"role": "owner", "response": "Great!", "property_details": {
            "property_type": "house", "bedrooms": 3, "location": "Garner", "rent": "1800"}}
        owner_listings.handle_owner_listing(result, conversation_id="c2", session_id="anon")
        self.assertEqual(self.fake.props[result["owner_listing"]["id"]]["status"], "pending")
        self.assertIn("review", result["response"])

    def test_review_mode_keeps_investor_listings_pending(self):
        investor = self.login("i@x.com", "existing_investor")
        with mock.patch.object(owner_listings, "INVESTOR_LISTINGS_NEED_REVIEW", True):
            r = self.client.post("/me/listings", headers=investor, json={"location": "Garner", "rent": 1500, "bedrooms": 2})
            self.assertFalse(r.json()["visible_to_tenants"])
            self.assertEqual(self.client.patch(f"/me/listings/{r.json()['id']}", headers=investor,
                                               json={"status": "active"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()


class ListingDetailsGivenOverSeveralMessages(unittest.TestCase):
    """'I want to list my house' ... then '3 beds in Garner, $1,800' in a later
    message (no listing words). Before, that follow-up was filed as the
    investor's own buying criteria and the home never reached tenants."""

    def setUp(self):
        from src.services import chat
        self.chat = chat
        self.fake = FakeDB()
        self.calls = []
        for p in [
            mock.patch.object(db, "ENABLED", False),  # skip conversation persistence
            mock.patch.object(db, "create_property", self.fake.create_property),
            mock.patch.object(db, "update_property", self.fake.update_property),
            mock.patch.object(db, "find_owner_draft", self.fake.find_owner_draft),
            mock.patch.object(owner_listings, "INVESTOR_LISTINGS_NEED_REVIEW", False),
            mock.patch.object(chat, "_fact_check", lambda *a, **k: None),
            mock.patch.object(chat.quick_replies, "quick_reply", lambda *a, **k: None),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def fake_ai(self, first_role):
        details = {"property_type": "house", "bedrooms": 3, "location": "Garner", "rent": "1800", "pets_allowed": "yes"}

        def analyze(**kw):
            self.calls.append(kw)
            retried = "MUST" in (kw.get("account_note") or "")
            if first_role == "owner" or retried:
                return {"role": "owner", "intent": "list_property", "response": "Got it.", "property_details": details}
            # The old mistake: the home's details read as buying criteria.
            return {"role": "investor", "intent": "investment_enquiry", "response": "Nice budget!",
                    "investor_profile": {"areas": ["Garner"], "min_bedrooms": 3}}
        return analyze

    def run_turn(self, message, history, first_role):
        with mock.patch.object(self.chat, "analyze_message", side_effect=self.fake_ai(first_role)):
            return self.chat.process_message(message, conversation_history=history, session_id="acct-i",
                                             customer_name="Ivan Investor", account_role="existing_investor")

    def test_follow_up_details_become_a_live_listing(self):
        history = [{"role": "user", "content": "I want to list my house for tenants"},
                   {"role": "assistant", "content": "Happy to help! Where is the house?"}]
        result = self.run_turn("It's in Garner, 3 beds, $1,800 a month, pets are fine", history, first_role="investor")

        self.assertEqual(result["role"], "owner")
        self.assertIn("LISTING IN PROGRESS", self.calls[0]["account_note"])  # the AI was told up front
        self.assertIsNone(self.calls[0]["investor_stage_note"])             # no investing nudges mid-listing
        self.assertEqual(len(self.calls), 2)                                # and re-asked once when it slipped
        self.assertEqual(len(self.fake.props), 1)
        home = next(iter(self.fake.props.values()))
        self.assertEqual(home["status"], "active")
        self.assertEqual(home["session_id"], "acct-i")
        self.assertIn("live", result["response"])

    def test_plain_investment_search_is_left_alone(self):
        result = self.run_turn("Show me 3 bed houses in Garner under $300k", [], first_role="investor")
        self.assertEqual(result["role"], "investor")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("LISTING IN PROGRESS", self.calls[0]["account_note"] or "")
        self.assertEqual(self.fake.props, {})

    def test_tenants_are_never_put_in_listing_mode(self):
        self.assertEqual(self.chat.listing_mode("list my house", [], "tenant"), (False, False))
