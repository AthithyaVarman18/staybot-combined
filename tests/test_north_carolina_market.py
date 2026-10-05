"""The app runs in North Carolina - nothing Chennai/India-specific may leak
into what a customer sees or what the AI is told."""

import json
import unittest
from pathlib import Path
from unittest import mock

from src.prompts import system_prompt as sp
from src.services import properties, viewings

INDIA_WORDS = ["Chennai", "BHK", "OMR", "Velachery", "lakh", "crore", "₹", "Tamil"]
ROOT = Path(__file__).resolve().parents[1]


class Prompt(unittest.TestCase):

    def test_prompt_is_north_carolina(self):
        self.assertEqual(sp.CURRENCY, "USD")
        self.assertIn("North Carolina", sp.MARKET)
        self.assertIn(f"on WhatsApp in {sp.MARKET}.", sp.SYSTEM_PROMPT)
        self.assertIn("# MARKET", sp.SYSTEM_PROMPT)
        for word in INDIA_WORDS:
            self.assertNotIn(word, sp.SYSTEM_PROMPT, word)

    def test_a_missing_swap_target_fails_loudly(self):
        # This is the bug that kept the bot "in Chennai": a silent str.replace().
        with self.assertRaises(RuntimeError):
            sp._swap("You are an assistant in __ELSEWHERE__.", "__MARKET__", "North Carolina")


class HomesAndMoney(unittest.TestCase):

    def test_sample_homes_are_in_north_carolina(self):
        homes = json.loads((ROOT / "src/data/properties.json").read_text())
        self.assertEqual(len(homes), 14)
        for h in homes:
            self.assertTrue(h["location"].endswith(", NC"), h["id"])
            blob = json.dumps(h)
            for word in INDIA_WORDS:
                self.assertNotIn(word, blob, h["id"])

    def test_seed_sql_matches_and_defaults_city_to_nc(self):
        sql = (ROOT / "supabase_properties.sql").read_text()
        self.assertNotIn("Chennai", sql)
        self.assertIn("default 'North Carolina'", sql)
        self.assertIn("'garner-3br'", sql)

    def test_homes_sent_to_the_ai_are_in_dollars(self):
        ctx = properties.property_context({"id": "x", "rent": 1850, "deposit": 1850})
        self.assertEqual(ctx["currency"], "USD")
        self.assertEqual(properties.price_label({"listing_type": "rent", "rent": 1850}), "$1,850/month")

    def test_chat_page_demo_homes_and_presets(self):
        html = (ROOT / "src/static/index.html").read_text(encoding="utf-8")
        for word in INDIA_WORDS + ["+91"]:
            self.assertNotIn(word, html, word)


class Search(unittest.TestCase):
    CLT = {"location": "South End, Charlotte, NC", "area": "South End", "city": "Charlotte", "title": "1BR in South End"}
    GAR = {"location": "Main St area, Garner, NC", "area": "Garner", "city": "Garner", "title": "Sunlit 3BR"}

    def test_state_name_does_not_match_every_home(self):
        for q in ("Garner, NC", "Garner, North Carolina"):
            self.assertTrue(properties.location_matches(q, self.GAR), q)
            self.assertFalse(properties.location_matches(q, self.CLT), q)
        self.assertTrue(properties.location_matches("anywhere in NC", self.CLT))


class PhonesAndTime(unittest.TestCase):

    def test_raleigh_numbers_stay_american(self):
        self.assertEqual(viewings.normalize_phone("919-555-0123"), "+19195550123")
        self.assertEqual(viewings.normalize_phone("(984) 555 0199"), "+19845550199")
        self.assertEqual(viewings.normalize_phone("1 704 555 0100"), "+17045550100")
        self.assertEqual(viewings.normalize_phone("+91 98400 12345"), "+919840012345")  # explicit + kept
        self.assertIsNone(viewings.normalize_phone("this one"))

    def test_india_market_still_works_when_configured(self):
        with mock.patch.object(viewings, "CURRENCY", "INR"):
            self.assertEqual(viewings.normalize_phone("98400 12345"), "+919840012345")

    def test_viewings_are_read_in_eastern_time(self):
        from src.services.real_estate_ai import APP_TIMEZONE
        self.assertEqual(str(APP_TIMEZONE), "America/New_York")


if __name__ == "__main__":
    unittest.main()
