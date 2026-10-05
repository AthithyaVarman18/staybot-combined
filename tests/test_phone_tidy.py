"""Phone numbers typed into forms are saved in one +1 form, so the same
customer isn't two people and WhatsApp / staff alerts can reach them."""

import unittest
from unittest import mock

from src.services import db, inquiries
from src.services.viewings import tidy_phone


class TidyPhone(unittest.TestCase):

    def test_us_numbers_typed_any_way(self):
        for typed in ("919-555-0199", "(919) 555 0199", "919.555.0199", "9195550199",
                      "1 919 555 0199", "+1 919-555-0199", " +19195550199 "):
            self.assertEqual(tidy_phone(typed), "+19195550199", typed)

    def test_other_countries_keep_their_code(self):
        self.assertEqual(tidy_phone("+44 20 7946 0958"), "+442079460958")

    def test_never_loses_what_they_typed(self):
        self.assertEqual(tidy_phone("call my office"), "call my office")
        self.assertIsNone(tidy_phone("  "))
        self.assertIsNone(tidy_phone(None))


class EnquireButtonSavesTidyPhone(unittest.TestCase):

    def test_customer_phone_saved_in_plus_one_form(self):
        saved = {}
        with mock.patch.object(inquiries.properties, "get_property", return_value=None), \
             mock.patch.object(db, "get_mls_listing", return_value={"agency_phone": "(919) 555-0100", "city": "Garner"}), \
             mock.patch.object(db, "get_conversation", return_value=None), \
             mock.patch.object(db, "create_inquiry", side_effect=lambda f: saved.update(f) or f), \
             mock.patch.object(inquiries.high_priority_inquiry_email, "notify_from_saved_inquiry"):
            inquiries.create_inquiry(property_id="MLS1", property_title="2 bed", session_id="web-1",
                                     customer_name="Maria", customer_phone="919-555-0199")
        self.assertEqual(saved["customer_phone"], "+19195550199")
        self.assertEqual(saved["owner_phone"], "+19195550100")
