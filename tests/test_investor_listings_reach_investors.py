"""An investor lists a home for sale -> OTHER investors (new and existing)
see it too, not only tenants. Their own listing is never offered back to them."""

import unittest
from unittest import mock

from src.services import db, deals, investors, owner_listings

from tests.test_investor_listings import FakeDB


class FakeInvestorDB(FakeDB):
    """FakeDB plus the MLS feed (empty) and PostgREST lte / in filters."""

    def _match(self, row, params):
        for k, v in params.items():
            if k in ("select", "order", "limit"):
                continue
            op, _, val = v.partition(".")
            if op == "eq" and str(row.get(k)) != val:
                return False
            if op == "in" and str(row.get(k)) not in val.strip("()").split(","):
                return False
            if op == "lte" and not (row.get(k) is not None and float(row[k]) <= float(val)):
                return False
        return True

    def get(self, table, params=None):
        if table in ("mls_listings", "investor_profiles", "investor_activity"):
            return []
        return super().get(table, params)


class InvestorListingsReachInvestors(unittest.TestCase):

    def setUp(self):
        self.fake = FakeInvestorDB()
        for p in [
            mock.patch.object(db, "ENABLED", True),
            mock.patch.object(db, "_get", self.fake.get),
            mock.patch.object(db, "create_property", self.fake.create_property),
            mock.patch.object(db, "update_property", self.fake.update_property),
            mock.patch.object(db, "find_owner_draft", self.fake.find_owner_draft),
            mock.patch.object(owner_listings, "INVESTOR_LISTINGS_NEED_REVIEW", False),
            mock.patch.object(deals, "saved_assumptions", lambda: deals.Assumptions()),
            mock.patch.object(investors, "neighborhood_snapshot", lambda areas: None),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def list_via_chat(self, details, session_id="acct-existing"):
        result = {"role": "owner", "response": "Great!", "property_details": details}
        owner_listings.handle_owner_listing(result, conversation_id=f"c-{session_id}", session_id=session_id,
                                            account_role="existing_investor")
        self.assertTrue(result["owner_listing"]["live"])
        return result["owner_listing"]["id"]

    def test_sale_listing_shows_up_in_another_investors_matches(self):
        pid = self.list_via_chat({"property_type": "house", "bedrooms": 3, "location": "Garner",
                                  "sale_price": "250000"})
        data = investors.match_listings({"cash_available": 100000, "areas": ["Garner"]},
                                        exclude_session_id="acct-new")
        ids = [m["listing"]["list_number"] for m in data["matches"]]
        self.assertIn(pid, ids)
        match = next(m for m in data["matches"] if m["listing"]["list_number"] == pid)
        self.assertTrue(match["listing"]["owner_listed"])
        self.assertEqual(float(match["listing"]["list_price"]), 250000)

    def test_listed_home_is_not_pushed_out_by_better_roi_mls_homes(self):
        """The chat shows only the top 3 - a cheap owner listing with no rent
        entered ranks last on ROI and used to be cut off every time."""
        pid = self.list_via_chat({"property_type": "house", "bedrooms": 1, "location": "Garner", "sale_price": "45000"})
        mls = [{"list_number": f"M{i}", "status_label": "active", "street_address": f"{i} Main St", "city": "Garner",
                "bedrooms": 3, "list_price": 400000 + i * 5000, "tax_annual": 2500, "living_area": 2000,
                "year_built": 2020} for i in range(6)]
        base_get = self.fake.get
        with mock.patch.object(db, "_get", lambda t, p=None: [dict(r) for r in mls] if t == "mls_listings" else base_get(t, p)):
            data = investors.match_listings({"cash_available": 200000, "areas": ["Garner"], "min_bedrooms": 1,
                                             "goal": "cash_flow"}, limit=3, exclude_session_id="acct-new")
        ids = [m["listing"]["list_number"] for m in data["matches"]]
        self.assertEqual(ids[0], pid)
        self.assertEqual(len(ids), 3)              # MLS homes still fill the rest
        self.assertEqual(data["matches"][0]["listing"]["street_address"], "1 bedroom house for sale")

    def test_investor_is_not_offered_their_own_listing(self):
        pid = self.list_via_chat({"property_type": "house", "bedrooms": 3, "location": "Garner",
                                  "sale_price": "250000"})
        data = investors.match_listings({"cash_available": 100000, "areas": ["Garner"]},
                                        exclude_session_id="acct-existing")
        self.assertNotIn(pid, [m["listing"]["list_number"] for m in data["matches"]])

    def test_rent_listings_and_hidden_homes_are_not_buy_matches(self):
        self.list_via_chat({"property_type": "house", "bedrooms": 3, "location": "Garner", "rent": "1800"})
        pid = self.list_via_chat({"property_type": "house", "bedrooms": 2, "location": "Garner",
                                  "sale_price": "200000"}, session_id="acct-other")
        self.fake.props[pid]["status"] = "hidden"
        self.assertEqual(db.owner_sale_listings(), [])

    def test_selecting_or_analysing_an_owner_listed_home_resolves(self):
        pid = self.list_via_chat({"property_type": "house", "bedrooms": 3, "location": "Garner",
                                  "sale_price": "250000"})
        self.assertEqual(db.get_mls_listing(pid)["list_number"], pid)      # "Select this home"
        self.assertEqual(float(deals.listing_for(pid)["list_price"]), 250000)  # deal analysis


if __name__ == "__main__":
    unittest.main()


class NewInvestorSeesExistingInvestorsListing(unittest.TestCase):
    """Existing investor lists a home for sale -> a New Property Investor
    finds it in Investing > Deals > Find homes (GET /mls)."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from src import main
        from src.services import accounts
        from tests import test_investor_listings as base

        self.fake = FakeInvestorDB()
        base.ACCTS.setdefault("n", {"id": "n", "name": "Nia New", "email": "n@x.com", "role": "new_investor",
                                    "session_id": "acct-n", "password_hash": "h", "password_salt": "s",
                                    "education_seen": True})
        for p in [
            mock.patch.object(db, "ENABLED", True),
            mock.patch.object(accounts.db, "ENABLED", True),
            mock.patch.object(db, "_get", self.fake.get),
            mock.patch.object(db, "_post", self.fake.post),
            mock.patch.object(db, "create_property", self.fake.create_property),
            mock.patch.object(db, "update_property", self.fake.update_property),
            mock.patch.object(db, "list_properties", self.fake.list_properties),
            mock.patch.object(accounts, "verify_password", return_value=True),
            mock.patch.object(owner_listings, "INVESTOR_LISTINGS_NEED_REVIEW", False),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def login(self, email, role):
        r = self.client.post("/auth/login", json={"email": email, "password": "p", "role": role})
        self.assertEqual(r.status_code, 200, r.text)
        return {"X-Staybot-Session": r.headers["X-Staybot-Session-Issued"]}

    def test_new_investor_finds_it_in_deals_search(self):
        existing = self.login("i@x.com", "existing_investor")
        new = self.login("n@x.com", "new_investor")
        r = self.client.post("/me/listings", headers=existing, json={
            "listing_type": "sale", "property_type": "house", "bedrooms": 5, "location": "Garner", "sale_price": 18000})
        self.assertEqual(r.status_code, 200, r.text)
        pid = r.json()["id"]

        found = self.client.get("/mls", headers=new, params={"city": "Garner"})
        self.assertEqual(found.status_code, 200, found.text)
        home = next((l for l in found.json()["listings"] if l["list_number"] == pid), None)
        self.assertIsNotNone(home, found.json())
        self.assertTrue(home["owner_listed"])
        self.assertNotIn("owner_session_id", home)
        self.assertEqual(self.client.get(f"/mls/{pid}", headers=new).status_code, 200)

        # Filters still apply, and the lister doesn't see their own home here.
        self.assertNotIn(pid, [l["list_number"] for l in self.client.get("/mls", headers=new, params={"min_beds": 6}).json()["listings"]])
        self.assertNotIn(pid, [l["list_number"] for l in self.client.get("/mls", headers=existing).json()["listings"]])
