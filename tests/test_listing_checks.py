"""Listing checks (src/services/listing_checks.py): a home with no price,
address or bedrooms - or a price that looks like a typo - doesn't go live
until it's fixed. Missing photos / description are tips, not blockers."""

import unittest
from unittest import mock

from src.services import db, listing_checks, owner_listings

from tests.test_investor_listings_reach_investors import FakeInvestorDB

GOOD = {"listing_type": "rent", "property_type": "house", "bedrooms": 3, "bathrooms": 2,
        "location": "Garner, NC", "rent": 1800, "photos": ["a.jpg"], "description": "Nice yard."}


class Checks(unittest.TestCase):

    def test_good_listing_passes(self):
        self.assertEqual(listing_checks.check(GOOD), {"ok": True, "problems": [], "warnings": []})

    def test_missing_price_address_bedrooms(self):
        r = listing_checks.check({"listing_type": "rent", "property_type": "house"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["problems"], ["Add the monthly rent.", "Add the address or area.",
                                         "Add the number of bedrooms."])

    def test_typo_prices(self):
        self.assertIn("$18 a month looks too low", listing_checks.check({**GOOD, "rent": 18})["problems"][0])
        self.assertIn("looks too high", listing_checks.check({**GOOD, "rent": 180000})["problems"][0])
        sale = {**GOOD, "listing_type": "sale", "rent": None}
        self.assertIn("$2,500 looks too low", listing_checks.check({**sale, "sale_price": 2500})["problems"][0])
        self.assertTrue(listing_checks.check({**sale, "sale_price": 250000})["ok"])

    def test_studio_needs_no_bedrooms_but_40_bedrooms_is_wrong(self):
        self.assertTrue(listing_checks.check({**GOOD, "property_type": "studio", "bedrooms": None})["ok"])
        self.assertIn("40 bedrooms looks wrong", listing_checks.check({**GOOD, "bedrooms": 40})["problems"][0])

    def test_tips_dont_block(self):
        r = listing_checks.check({**GOOD, "photos": None, "description": None, "deposit": 9000})
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["warnings"]), 3)
        self.assertIn("No photos yet", r["warnings"][0])


class OwnerChat(unittest.TestCase):
    """An investor's chat listing normally goes live at once - unless a check fails."""

    def setUp(self):
        self.fake = FakeInvestorDB()
        for p in [mock.patch.object(db, "ENABLED", True),
                  mock.patch.object(db, "_get", self.fake.get),
                  mock.patch.object(db, "create_property", self.fake.create_property),
                  mock.patch.object(db, "update_property", self.fake.update_property),
                  mock.patch.object(db, "find_owner_draft", self.fake.find_owner_draft),
                  mock.patch.object(owner_listings, "INVESTOR_LISTINGS_NEED_REVIEW", False)]:
            p.start()
            self.addCleanup(p.stop)

    def say(self, details):
        result = {"role": "owner", "response": "Great!", "property_details": details}
        owner_listings.handle_owner_listing(result, conversation_id="c1", session_id="acct-1",
                                            account_role="existing_investor")
        return result

    def test_typo_rent_stays_a_draft_then_goes_live_when_fixed(self):
        r = self.say({"property_type": "house", "bedrooms": 3, "location": "Garner", "rent": "18"})
        self.assertFalse(r["owner_listing"]["live"])
        self.assertIn("looks too low", r["response"])
        r = self.say({"property_type": "house", "bedrooms": 3, "location": "Garner", "rent": "1800"})
        self.assertTrue(r["owner_listing"]["live"])
        self.assertIn("it's live now", r["response"])

    def test_live_listing_changed_to_a_typo_comes_off(self):
        r = self.say({"property_type": "house", "bedrooms": 3, "location": "Garner", "rent": "1800"})
        self.assertTrue(r["owner_listing"]["live"])
        r = self.say({"rent": "18"})
        self.assertEqual(r["owner_listing"]["status"], "pending")


class Endpoints(unittest.TestCase):
    """Investor form and the team's Approve & publish."""

    def setUp(self):
        from tests.test_investor_listings_reach_investors import NewInvestorSeesExistingInvestorsListing as T
        self.t = T("test_new_investor_finds_it_in_deals_search")
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def test_form_refuses_typo_price_and_missing_bedrooms(self):
        me = self.t.login("i@x.com", "existing_investor")
        r = self.t.client.post("/me/listings", headers=me, json={
            "listing_type": "rent", "property_type": "house", "bedrooms": 3, "location": "Garner", "rent": 18})
        self.assertEqual(r.status_code, 400)
        self.assertIn("looks too low", r.json()["detail"])
        r = self.t.client.post("/me/listings", headers=me, json={
            "listing_type": "rent", "property_type": "house", "location": "Garner", "rent": 1800})
        self.assertEqual(r.status_code, 400)
        self.assertIn("bedrooms", r.json()["detail"])

        r = self.t.client.post("/me/listings", headers=me, json={
            "listing_type": "rent", "property_type": "house", "bedrooms": 3, "location": "Garner", "rent": 1800})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["checks"]["ok"])
        self.assertIn("No photos yet", body["checks"]["warnings"][0])     # tip, still live
        self.assertTrue(body["visible_to_tenants"])

        r = self.t.client.patch(f"/me/listings/{body['id']}", headers=me, json={"rent": 18})
        self.assertEqual(r.status_code, 400)


from tests.test_rentals import RentalFixture


class TeamApprove(RentalFixture):

    def test_approve_blocked_until_fixed(self):
        self.fake.t["properties"].append({"id": "owner-x", "status": "pending", "listing_type": "rent",
                                          "title": "3 bed", "bedrooms": 3, "location": "Garner", "rent": 18})
        with mock.patch.object(db, "update_property", side_effect=lambda pid, f: {"id": pid, **f}):
            r = self.c.patch("/properties/owner-x", headers=self.team, json={"status": "active"})
            self.assertEqual(r.status_code, 409)
            self.assertIn("looks too low", r.json()["detail"])
            # Rejecting (hidden) is always allowed.
            self.assertEqual(self.c.patch("/properties/owner-x", headers=self.team,
                                          json={"status": "hidden"}).status_code, 200)
            self.fake.t["properties"][-1]["rent"] = 1800
            self.assertEqual(self.c.patch("/properties/owner-x", headers=self.team,
                                          json={"status": "active"}).status_code, 200)
