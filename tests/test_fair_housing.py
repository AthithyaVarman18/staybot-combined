"""Fair Housing (src/services/fair_housing.py + # FAIR HOUSING in the prompt):
replies that steer - who a home suits, who lives in an area, whether an area
or its schools are good/safe - are caught, rewritten, or replaced."""

import unittest
from unittest import mock

from src.prompts import system_prompt
from src.services import chat, fair_housing


class Check(unittest.TestCase):

    def test_steering_is_caught(self):
        for reply in ("This 3 bed is perfect for a growing family.",
                      "It's a very family-friendly street.",
                      "Great for young professionals!",
                      "That part of Garner is mostly Hispanic.",
                      "It's a safe neighborhood with low crime.",
                      "The area is really safe.",
                      "It has great schools nearby.",
                      "The owner prefers adults only.",
                      "It's a quiet Christian community."):
            self.assertTrue(fair_housing.check(reply), reply)

    def test_facts_about_the_home_pass(self):
        for reply in ("3 bedrooms, 2 baths, a fenced yard and a 2-car garage. Rent is $1,800 a month.",
                      "It's 5 minutes from Lake Benson Park and 20 minutes to downtown Raleigh.",
                      "Pets are allowed with a $300 deposit.",
                      "The home is in the Wake County Public School System - its website lets you compare schools.",
                      "I can't say whether an area is safe - the Garner Police crime map is the best source.",
                      "Would you like to book a viewing on Saturday?"):
            self.assertEqual(fair_housing.check(reply), [], reply)

    def test_rules_are_in_the_ai_prompt(self):
        self.assertIn("# FAIR HOUSING", system_prompt.SYSTEM_PROMPT)
        self.assertIn("reasonable accommodation", system_prompt.SYSTEM_PROMPT)


class InTheChat(unittest.TestCase):

    def run_check(self, reply, retry_reply, role="tenant"):
        result = {"role": role, "response": reply}
        calls = []
        def analyze(**kw):
            calls.append(kw)
            return {"response": retry_reply}
        with mock.patch.object(chat, "analyze_message", analyze):
            chat._fact_check(result, "Is it a good area for my kids?", [], None, [])
        return result, calls

    def test_rewritten_when_the_ai_steers(self):
        result, calls = self.run_check("Yes! It's perfect for families and the schools are great.",
                                       "It has 3 bedrooms and a fenced yard. For schools, see the Wake County district website.")
        self.assertEqual(result["fact_check"]["status"], "fixed")
        self.assertIn("FAIR HOUSING CHECK FAILED", calls[0]["correction_note"])
        self.assertIn("fenced yard", result["response"])

    def test_safe_reply_when_the_rewrite_still_steers(self):
        result, _ = self.run_check("It's a safe neighborhood.", "Honestly it's a good area for families.")
        self.assertEqual(result["fact_check"]["status"], "blocked")
        self.assertEqual(result["response"], fair_housing.SAFE_REPLY)

    def test_investors_are_checked_too(self):
        result, _ = self.run_check("Garner has great schools, so it rents well.", "Cash flow is projected at $310 a month.",
                                   role="investor")
        self.assertEqual(result["fact_check"]["status"], "fixed")
        result, calls = self.run_check("Projected cash flow is $310 a month.", "unused", role="investor")
        self.assertEqual((result["fact_check"]["status"], calls), ("skipped_investor", []))
