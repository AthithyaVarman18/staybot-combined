import unittest
from unittest.mock import patch

from src.services import investor_journey as investors


class ClassifyType(unittest.TestCase):

    def test_zero_existing_properties_is_new(self):
        self.assertEqual(investors.classify_type({"existing_property_count": 0}), "new")

    def test_positive_existing_properties_is_existing(self):
        self.assertEqual(investors.classify_type({"existing_property_count": 3}), "existing")

    def test_unknown_until_count_given(self):
        self.assertIsNone(investors.classify_type({"investment_goals": "steady income"}))


class NormalizedDetails(unittest.TestCase):

    def test_parses_and_cleans_fields(self):
        fields = investors.normalized_details({
            "investment_goals": "steady rental income",
            "budget": "40 lakhs",
            "location": "OMR, Chennai",
            "investment_strategy": "buy and hold",
            "risk_tolerance": "I'd say moderate/balanced",
            "timeline": "next 3 months",
            "existing_property_count": "2",
            "financing_requirements": "mortgage",
        })
        self.assertEqual(fields["budget"], 4000000.0)
        self.assertEqual(fields["location"], "OMR, Chennai")
        self.assertEqual(fields["risk_tolerance"], "moderate")
        self.assertEqual(fields["existing_property_count"], 2)
        self.assertEqual(fields["financing_requirements"], "mortgage")

    def test_drops_unknown_and_empty_values(self):
        fields = investors.normalized_details({"location": "unknown", "budget": None, "risk_tolerance": ""})
        self.assertEqual(fields, {})

    def test_risk_tolerance_left_out_when_unrecognized(self):
        fields = investors.normalized_details({"risk_tolerance": "whatever works"})
        self.assertNotIn("risk_tolerance", fields)


class JourneyIntegrity(unittest.TestCase):

    def test_both_journeys_start_with_lead_generation(self):
        for journey in investors.JOURNEYS.values():
            self.assertEqual(journey["stages"][0][0], "lead_generation")

    def test_stage_keys_are_unique_within_each_journey(self):
        for journey in investors.JOURNEYS.values():
            keys = [key for key, _, _ in journey["stages"]]
            self.assertEqual(len(keys), len(set(keys)))

    def test_existing_investor_journey_matches_spec(self):
        keys = [k for k, _, _ in investors.EXISTING_INVESTOR_STAGES]
        self.assertEqual(keys, [
            "lead_generation", "contracting", "onboarding", "portfolio_collection",
            "property_search_analysis", "acquisition", "tenant_lead_generation",
            "tenant_screening", "tenant_contracting", "property_management",
        ])

    def test_new_investor_journey_matches_spec(self):
        keys = [k for k, _, _ in investors.NEW_INVESTOR_STAGES]
        self.assertEqual(keys, [
            "lead_generation", "contracting", "onboarding", "investment_education",
            "property_identification", "property_market_analysis", "offer_preparation",
            "mortgage_assistance", "property_inspection", "closing", "post_closing_management",
        ])


class StageNote(unittest.TestCase):

    def test_none_investor_returns_none(self):
        self.assertIsNone(investors.stage_note(None))

    def test_unclassified_investor_lists_missing_fields(self):
        note = investors.stage_note({"stage": "lead_generation", "investment_goals": "cash flow"})
        self.assertIn("not yet assigned", note)
        self.assertIn("budget", note)

    def test_classified_lead_generation_lists_only_missing(self):
        investor = {
            "investor_type": "new", "journey": "new_investor", "stage": "lead_generation",
            "investment_goals": "cash flow", "budget": 500000, "location": "Chennai",
            "investment_strategy": "buy and hold", "risk_tolerance": "moderate", "timeline": None,
            "existing_property_count": 0,
        }
        note = investors.stage_note(investor)
        self.assertIn("timeline", note)
        self.assertNotIn("budget", note.split("Still missing")[-1])

    def test_later_stage_does_not_re_ask_qualifying_fields(self):
        investor = {"investor_type": "existing", "journey": "existing_investor", "stage": "onboarding"}
        note = investors.stage_note(investor)
        self.assertIn("onboarding", note.lower())
        self.assertIn("don't re-ask", note)


class Advance(unittest.TestCase):
    """advance() writes directly via db._patch/_post (best-effort, chat-path
    style, not gated through configured()/q()) so these mock the DB calls."""

    def setUp(self):
        self.investor = {"id": "abc-123", "journey": "new_investor", "stage": "lead_generation"}

    @patch("src.services.investor_journey.db._post")
    @patch("src.services.investor_journey.db._patch")
    def test_advances_to_next_stage(self, patched_patch, patched_post):
        patched_patch.return_value = {**self.investor, "stage": "contracting", "stage_index": 1}
        result = investors.advance(self.investor, actor="ai:auto")
        self.assertEqual(result["stage"], "contracting")
        patched_patch.assert_called_once_with(
            "investors", {"stage": "contracting", "stage_index": 1}, {"id": "eq.abc-123"})
        self.assertEqual(patched_post.call_count, 2)  # stage history + activity log

    @patch("src.services.investor_journey.db._post")
    @patch("src.services.investor_journey.db._patch")
    def test_jump_to_target_stage(self, patched_patch, patched_post):
        patched_patch.return_value = {**self.investor, "stage": "closing", "stage_index": 9}
        result = investors.advance(self.investor, actor="staff:jane", target_stage="closing")
        self.assertEqual(result["stage"], "closing")

    def test_unknown_target_stage_raises(self):
        with self.assertRaises(ValueError):
            investors.advance(self.investor, actor="staff:jane", target_stage="not_a_real_stage")

    def test_unclassified_investor_cannot_advance(self):
        with self.assertRaises(ValueError):
            investors.advance({"id": "x", "journey": None, "stage": "lead_generation"}, actor="staff:jane")

    @patch("src.services.investor_journey.db._post")
    @patch("src.services.investor_journey.db._patch")
    def test_already_at_final_stage_is_a_no_op(self, patched_patch, patched_post):
        final_stage = investors.EXISTING_INVESTOR_STAGES[-1][0]
        investor = {"id": "abc", "journey": "existing_investor", "stage": final_stage}
        result = investors.advance(investor, actor="ai:auto")
        self.assertEqual(result, investor)
        patched_patch.assert_not_called()


class AccountInvestorLookup(unittest.TestCase):
    """_account_investor() is what the /me/investor self-service routes use
    to scope a logged-in account to exactly its own investor row."""

    def test_not_logged_in_raises_401(self):
        with patch("src.services.investor_journey.db.ENABLED", True), \
             patch("src.services.accounts.current_account", return_value=None):
            request = object()
            with self.assertRaises(Exception) as ctx:
                investors._account_investor(request)
            self.assertEqual(ctx.exception.status_code, 401)

    def test_tenant_account_is_forbidden(self):
        with patch("src.services.investor_journey.db.ENABLED", True), \
             patch("src.services.accounts.current_account", return_value={"role": "tenant", "session_id": "s1"}):
            with self.assertRaises(Exception) as ctx:
                investors._account_investor(object())
            self.assertEqual(ctx.exception.status_code, 403)

    def test_investor_account_without_profile_yet_raises_404(self):
        account = {"role": "new_investor", "session_id": "s1", "investor_id": None}
        with patch("src.services.investor_journey.db.ENABLED", True), \
             patch("src.services.accounts.current_account", return_value=account), \
             patch("src.services.investor_journey.db._get", return_value=[]):
            with self.assertRaises(Exception) as ctx:
                investors._account_investor(object())
            self.assertEqual(ctx.exception.status_code, 404)

    def test_investor_account_resolves_by_investor_id(self):
        account = {"role": "existing_investor", "session_id": "s1", "investor_id": "inv-1"}
        row = {"id": "inv-1", "journey": "existing_investor", "stage": "lead_generation"}
        with patch("src.services.investor_journey.db.ENABLED", True), \
             patch("src.services.accounts.current_account", return_value=account), \
             patch("src.services.investor_journey.db._get", return_value=[row]):
            found = investors._account_investor(object())
            self.assertEqual(found["id"], "inv-1")


if __name__ == "__main__":
    unittest.main()
