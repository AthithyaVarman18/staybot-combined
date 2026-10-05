import unittest
from unittest.mock import MagicMock

from src.services import investor_journey, investors


class DealQuestions(unittest.TestCase):
    def test_chat_answers_are_kept(self):
        out = investors.clean_profile({"home_age_preference": "Older", "strategy": "short term rental",
                                       "risk_tolerance": "low", "strategy_bogus": "x"})
        self.assertEqual(out, {"home_age_preference": "older", "strategy": "short_term_rental", "risk_tolerance": "low"})

    def test_made_up_values_are_dropped(self):
        self.assertEqual(investors.clean_profile({"home_age_preference": "vintage", "strategy": "crypto"}), {})

    def test_strategy_reaches_the_journey(self):
        details = investor_journey.details_from_profile({"goal": "growth", "strategy": "long_term_rental"})
        self.assertEqual(details["investment_strategy"], "rental_income")

    def test_save_still_works_before_the_sql_is_run(self):
        error = Exception("PGRST204: Could not find the 'strategy' column of 'investor_profiles'")
        write = MagicMock(side_effect=[error, {"id": "p1"}])
        investors.write_profile(write, "investor_profiles", {"goal": "income", "strategy": "fix_and_flip"})
        self.assertEqual(write.call_args.args[1], {"goal": "income"})

    def test_other_errors_are_not_swallowed(self):
        write = MagicMock(side_effect=Exception("network down"))
        with self.assertRaises(Exception):
            investors.write_profile(write, "investor_profiles", {"strategy": "fix_and_flip"})


if __name__ == "__main__":
    unittest.main()
