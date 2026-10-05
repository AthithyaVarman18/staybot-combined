"""Checks for the places where the two debugged versions were combined."""

import unittest
from unittest import mock

from src.services import cache, listing_portfolio, my_listings, rentals, whatsapp


class BrokenRedis:
    def __getattr__(self, name):
        def fail(*a, **k):
            raise ConnectionError("redis down")
        return fail


class RedisFallback(unittest.TestCase):
    def test_whatsapp_dedup_falls_back_to_memory_when_redis_is_down(self):
        with mock.patch.object(cache, "_client", BrokenRedis()):
            self.assertTrue(whatsapp.first_time_seen("merge-test-1"))
            self.assertFalse(whatsapp.first_time_seen("merge-test-1"))

    def test_cache_helpers_never_raise(self):
        with mock.patch.object(cache, "_client", BrokenRedis()):
            self.assertIsNone(cache.set_nx("k", "1", 5))
            self.assertEqual(cache.ttl("k"), 0)
            self.assertFalse(cache.set("k", "1"))


class TwoPurchaseFlows(unittest.TestCase):
    """Applicant (team_approved) -> owner's "Approve tenant" step.
    Current tenant (approved) with an "Own this house" request -> home_purchase.py."""

    def app(self, status):
        return {"id": "app-1", "status": status, "details": {}}

    def test_applicant_needs_owner_approval(self):
        with mock.patch.object(rentals, "accepted_offer", return_value={"id": "o1"}), \
             mock.patch.object(rentals, "home_purchase_apps", return_value=set()):
            self.assertTrue(rentals.needs_sale_approval(self.app("team_approved")))

    def test_current_tenant_buying_goes_through_home_purchase(self):
        with mock.patch.object(rentals, "accepted_offer", return_value={"id": "o1"}), \
             mock.patch.object(rentals, "home_purchase_apps", return_value={"app-1"}):
            self.assertFalse(rentals.needs_sale_approval(self.app("approved")))

    def test_current_tenant_falls_back_to_owner_approval_without_home_purchase_table(self):
        with mock.patch.object(rentals, "accepted_offer", return_value={"id": "o1"}), \
             mock.patch.object(rentals, "home_purchase_apps", return_value=set()):
            self.assertTrue(rentals.needs_sale_approval(self.app("approved")))


class BoughtHomeIsNotSold(unittest.TestCase):
    def test_bought_home_is_not_blocked_as_sold_once_plan_is_chosen(self):
        p = {"id": "h1", "status": "sold"}
        self.assertIn("already sold", my_listings.relist_blocked(p, None))
        self.assertIsNone(my_listings.relist_blocked(p, None, {"plan": "rent_out"}))
        self.assertIn("Portfolio tab", my_listings.relist_blocked(p, None, {"plan": None}))

    def test_sync_keeps_a_bought_home_in_the_portfolio(self):
        deleted = []
        with mock.patch.object(listing_portfolio.db, "ENABLED", True), \
             mock.patch.object(listing_portfolio, "investor_for", return_value={"id": "inv-1"}), \
             mock.patch.object(listing_portfolio, "bought_homes", return_value={"h1": {"plan": None}}), \
             mock.patch.object(listing_portfolio.db, "_get", return_value=[{"id": "row-1", "property_id": "h1"}]), \
             mock.patch.object(listing_portfolio.db, "_delete", side_effect=lambda *a: deleted.append(a)):
            listing_portfolio.sync({"id": "a1", "role": "existing_investor", "session_id": "s"},
                                   [{"id": "h1", "status": "sold", "source": "investor_form"}])
        self.assertEqual(deleted, [])


if __name__ == "__main__":
    unittest.main()
