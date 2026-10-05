"""The investor chat: ask until the brief is complete, then show homes with photos."""

import unittest

from src.services import investors


class BriefTests(unittest.TestCase):
    def test_incomplete_brief_lists_what_is_missing(self):
        self.assertEqual(investors.brief_missing({"cash_available": 90000}),
                         ["financing", "goal", "areas", "timeline",
                          "home_age_preference", "strategy", "risk_tolerance"])
        self.assertFalse(investors.brief_ready({"cash_available": 90000}))

    def test_complete_brief_is_ready(self):
        profile = {"cash_available": 90000, "financing": "mortgage", "goal": "income",
                   "areas": ["Garner"], "timeline": "3_months"}
        # New build vs older, strategy and risk are asked of every investor too.
        self.assertFalse(investors.brief_ready(profile))
        profile.update(home_age_preference="any", strategy="long_term_rental", risk_tolerance="unsure")
        self.assertEqual(investors.brief_missing(profile), [])
        self.assertTrue(investors.brief_ready(profile))

    def test_new_investor_sees_homes_by_second_message(self):
        first, second = [], [{"role": "user", "content": "I want to invest"}, {"role": "assistant", "content": "?"}]
        self.assertEqual(investors.user_turn_number(second), 2)
        # First message: nothing known yet -> ask cash + area together.
        self.assertFalse(investors.listing_ready({}, fast=True, turn=investors.user_turn_number(first)))
        self.assertIn("ONE combined question", investors.fast_listing_note({}, first))
        # Cash and area in the very first message -> list straight away.
        self.assertTrue(investors.listing_ready({"cash_available": 60000, "areas": ["Garner"]}, fast=True, turn=1))
        # Second message with anything to search on -> list, even with the brief incomplete.
        self.assertTrue(investors.listing_ready({"cash_available": 60000}, fast=True, turn=2))
        self.assertIn("WILL add real matching", investors.fast_listing_note({"cash_available": 60000}, second))
        # Not in fast mode -> still the full five-field brief.
        self.assertFalse(investors.listing_ready({"cash_available": 60000, "areas": ["Garner"]}, fast=False, turn=2))
        # Already shown the list -> no fast-listing briefing.
        self.assertIsNone(investors.fast_listing_note({"last_matched_at": "2026-01-01T00:00:00+00:00"}, second))

    def test_enquiry_line_is_recognised(self):
        self.assertTrue(investors.ENQUIRY_SENT_RE.search("I'd like to go ahead with 1 Main St. I've sent an enquiry for it."))

    def test_signature_changes_only_when_matching_details_change(self):
        profile = {"cash_available": 90000, "goal": "income", "areas": ["Garner"], "min_bedrooms": 3}
        same = investors.match_signature({**profile, "experience": "first_time"})
        self.assertEqual(investors.match_signature(profile), same)
        self.assertNotEqual(investors.match_signature({**profile, "areas": ["Fuquay Varina"]}), same)

    def test_brief_line_repeats_their_own_words(self):
        line = investors.brief_line({"cash_available": 90000, "areas": ["Garner"],
                                     "min_bedrooms": 3, "goal": "income"})
        self.assertEqual(line, "$90,000 cash, Garner, 3+ bed, monthly income")


class MatchPresentationTests(unittest.TestCase):
    data = {"budget_ceiling": 321429, "assumed_rent_percent": 0.8, "matches": [{
        "listing": {"list_number": "1", "street_address": "329 Parker Street", "city": "Garner",
                    "list_price": 320000, "bedrooms": 3, "living_area": 1906, "year_built": 2026,
                    "photo_url": "http://cdn.example.com/a.jpg"},
        "rent_used": 2560, "cash_needed": 89600, "cash_flow_monthly": 402, "breakeven_rent": 2069}]}

    profile = {"cash_available": 90000, "areas": ["Garner"], "goal": "income"}

    def test_intro_is_short_and_personal_when_photos_follow(self):
        text = investors.matches_message(self.profile, self.data, with_photos=True)
        self.assertIn("$90,000 cash, Garner, monthly income", text)
        self.assertIn("329 Parker Street", text)
        self.assertNotIn("break-even", text)          # the photo captions carry the numbers
        self.assertLess(len(text.splitlines()), 6)

    def test_without_photos_the_numbers_are_spelled_out(self):
        text = investors.matches_message(self.profile, self.data, with_photos=False)
        self.assertIn("break-even", text)
        self.assertIn("assumed", text)
        self.assertIn("not a promise", text)

    def test_followup_labels_the_rent_as_an_assumption(self):
        line = investors.matches_followup(self.data)
        self.assertIn("assumed at 0.8%", line)
        self.assertIn("not a promise", line)

    def test_photos_are_https_with_a_one_line_caption(self):
        photos = investors.match_photos(self.data)
        self.assertEqual(len(photos), 1)
        self.assertTrue(photos[0]["url"].startswith("https://"))
        self.assertIn("329 Parker Street, Garner - $320,000", photos[0]["caption"])
        self.assertIn("Cash needed ~$89,600", photos[0]["caption"])

    def test_listings_without_a_photo_are_skipped(self):
        no_photo = {**self.data, "matches": [{**self.data["matches"][0],
                                              "listing": {**self.data["matches"][0]["listing"], "photo_url": ""}}]}
        self.assertEqual(investors.match_photos(no_photo), [])


if __name__ == "__main__":
    unittest.main()
