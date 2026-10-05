import unittest
from unittest.mock import patch

from src.services import home_purchase, investor_journey


class GraduationKeepsExistingInvestor(unittest.TestCase):
    def test_graduation_records_an_owned_property(self):
        investor = {"id": "inv1", "investor_type": "new", "journey": "new_investor",
                    "stage": "closing", "existing_property_count": 0}
        with patch.object(investor_journey, "load", return_value=investor), \
             patch.object(investor_journey.db, "_patch") as write, \
             patch.object(investor_journey.db, "_post"), \
             patch.object(investor_journey.db, "_get", return_value=[]):
            investor_journey.graduate_to_existing_investor("inv1", "test")
        fields = write.call_args_list[0].args[1]
        self.assertEqual(fields["journey"], "existing_investor")
        self.assertEqual(fields["existing_property_count"], 1)
        # ...so the chat's classifier keeps them on the existing track.
        self.assertEqual(investor_journey.classify_type({**investor, **fields}), "existing")


class EnsureInvestorClassifiesChatProfile(unittest.TestCase):
    def test_unclassified_profile_joins_new_journey_at_offer_preparation(self):
        account = {"id": "a1", "session_id": "acct-1", "investor_id": "inv1", "name": "Tia"}
        unclassified = {"id": "inv1", "journey": None, "investor_type": None, "existing_property_count": None}
        with patch.object(home_purchase, "one", return_value=account), \
             patch.object(home_purchase.rentals, "buyer_investor", return_value=unclassified), \
             patch.object(home_purchase.db, "_patch", side_effect=lambda t, body, p: {**unclassified, **body}) as write, \
             patch.object(home_purchase.db, "_post"):
            investor = home_purchase.ensure_investor({"tenant_account_id": "a1", "property_id": "p1"}, "test")
        self.assertEqual(investor["journey"], "new_investor")
        self.assertEqual(investor["stage"], "offer_preparation")
        self.assertEqual(investor["investor_type"], "new")
        self.assertEqual(write.call_args.args[0], "investors")


class BuyerProfileBecomesInvestor(unittest.TestCase):
    def test_rent_search_dropped_and_ownership_added(self):
        from src.services import portfolio
        stored = {"details": {"location": "Garner", "budget": 1800, "bedrooms": 3,
                              "employment_status": "employed", "monthly_income": 6000}}
        with patch.object(portfolio.db, "ENABLED", True), \
             patch.object(portfolio.db, "_get", return_value=[stored]), \
             patch.object(portfolio.db, "_patch") as write:
            portfolio.became_existing_investor("a1", owned_count=1, home="12 Oak St")
        details = write.call_args.args[1]["details"]
        for gone in ("location", "budget", "bedrooms"):
            self.assertNotIn(gone, details)
        self.assertEqual(details["monthly_income"], 6000)  # screening kept
        self.assertEqual(details["account_type"], "Existing Property Investor")
        self.assertEqual(details["existing_property_count"], 1)
        self.assertEqual(details["home_bought"], "12 Oak St")

    def test_complete_purchase_updates_investor_count_and_profile(self):
        buyer = {"id": "a1", "email": "tia@example.com"}
        investor = {"id": "inv1"}
        with patch.object(home_purchase.db, "_get", return_value=[{"id": "r1"}, {"id": "r2"}]), \
             patch.object(home_purchase.db, "_patch") as write, \
             patch("src.services.portfolio.became_existing_investor") as profile:
            home_purchase.update_buyer_profile(buyer, investor, {"property_title": "3 bed in Garner"}, {}, {})
        self.assertEqual(write.call_args.args[1]["existing_property_count"], 2)
        self.assertEqual(write.call_args.args[1]["email"], "tia@example.com")
        profile.assert_called_once_with("a1", owned_count=2, home="3 bed in Garner")


if __name__ == "__main__":
    unittest.main()
