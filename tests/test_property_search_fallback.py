"""The tenant search always shows homes: closest options when nothing fits
exactly, and requirements the AI dropped are filled from the customer's words."""

import json
import unittest
from pathlib import Path
from unittest import mock

from src.services import properties as P

ROWS = json.loads(Path(P.SAMPLE_PATH).read_text(encoding="utf-8"))
GARNER_2BR = {"rent_or_buy": "rent", "location": "Garner", "bedrooms": 2, "budget": "$1,800/month", "property_type": "apartment"}


class SearchFallback(unittest.TestCase):

    def search(self, rows, req):
        with mock.patch.object(P, "all_properties", lambda: (rows, "test")):
            return P.search(req)

    def test_exact_match_is_listed(self):
        r = self.search(ROWS, GARNER_2BR)
        self.assertEqual(r["matches"][0]["id"], "garner-2br-duplex")
        self.assertFalse(r["relaxed"])
        self.assertTrue(P.matches_text(r).startswith("Here are the best matches"))

    def test_no_homes_in_town_shows_same_spec_elsewhere(self):
        rows = [p for p in ROWS if "garner" not in p["id"]]
        r = self.search(rows, GARNER_2BR)
        self.assertTrue(r["relaxed"])
        self.assertTrue(r["matches"])
        for m in r["matches"]:
            self.assertEqual(m["bedrooms"], 2)
            self.assertLessEqual(m["price"], 1800)
            self.assertTrue(any(n.endswith("not Garner") for n in m["notes"]))
        text = P.matches_text(r)
        self.assertIn("closest options", text)
        self.assertNotIn("couldn't find", text)
        self.assertIn("in Greensboro, not Garner", text)  # the town, not an obscure neighbourhood

    def test_budget_too_low_shows_homes_in_town_marked_over_budget(self):
        r = self.search(ROWS, {**GARNER_2BR, "budget": "500"})
        self.assertTrue(r["relaxed"])
        self.assertIn("garner-2br-duplex", [m["id"] for m in r["matches"]])
        self.assertTrue(all("over budget" in " ".join(m["notes"]) for m in r["matches"]))

    def test_near_matches_only_are_not_called_a_match(self):
        rows = [p for p in ROWS if p["id"] != "garner-2br-duplex"]
        text = P.matches_text(self.search(rows, GARNER_2BR))
        self.assertIn("closest options", text)
        self.assertIn("Sunlit 3BR", text)

    def test_rent_search_never_shows_homes_for_sale(self):
        r = self.search(ROWS, {"rent_or_buy": "rent", "location": "Boone"})
        self.assertTrue(r["matches"])
        self.assertTrue(all(m["listing_type"] == "rent" for m in r["matches"]))


class Backfill(unittest.TestCase):

    def test_fills_only_what_is_missing(self):
        with mock.patch.object(P, "all_properties", lambda: (ROWS, "test")):
            req = P.backfill_requirements({"budget": "$1,800/month"}, ["2 bedroom apartment for rent in Garner", "$1,800/month"])
            self.assertEqual((req["location"], req["bedrooms"], req["rent_or_buy"]), ("Garner", 2, "rent"))
            kept = P.backfill_requirements({"location": "Apex", "bedrooms": 3}, ["2 bedroom in Garner"])
            self.assertEqual((kept["location"], kept["bedrooms"]), ("Apex", 3))
            newest = P.backfill_requirements({}, ["I want to buy a 3 bed in Cary", "actually two bedrooms, $450k"])
            self.assertEqual((newest["bedrooms"], newest["rent_or_buy"], newest["location"]), (2, "buy", "Cary"))
            self.assertNotIn("location", P.backfill_requirements({}, ["somewhere quiet please"]))

    def test_turn_with_no_extracted_requirements_still_lists_homes(self):
        # Homes are only shown once rent-or-buy, area, bedrooms, budget and
        # move-in date are all known (properties.attach_matches): without them
        # the AI keeps asking; once they're given - even if the AI's own
        # requirements dropped most of them - the homes are listed from the
        # customer's own words.
        rows = [p for p in ROWS if p["id"] != "garner-2br-duplex"]
        with mock.patch.object(P, "all_properties", lambda: (rows, "test")):
            result = {"role": "tenant", "response": "What is your budget?", "requirements": {}}
            P.attach_matches(result, conversation_history=[], message="2 bedroom apartment for rent in Garner")
            self.assertNotIn("matches", result)
            self.assertEqual(result["response"], "What is your budget?")

            result = {"role": "tenant", "response": "Here's what I found.", "requirements": {"move_in_date": "November 1"}}
            P.attach_matches(result, conversation_history=[
                {"role": "user", "content": "2 bedroom apartment for rent in Garner"}], message="up to $2,500 a month")
        self.assertIn("Sunlit 3BR near downtown Garner", result["response"])


if __name__ == "__main__":
    unittest.main()
