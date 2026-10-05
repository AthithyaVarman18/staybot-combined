"""Public privacy policy at /privacy (Meta needs it for WhatsApp)."""

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from src.main import app


class PrivacyPage(unittest.TestCase):

    def test_public_and_uses_placeholders_until_set(self):
        with mock.patch.dict(os.environ, {"BUSINESS_NAME": "", "PRIVACY_EMAIL": "", "BUSINESS_POSTAL_ADDRESS": ""}):
            r = TestClient(app).get("/privacy")          # no login
        self.assertEqual(r.status_code, 200)
        self.assertIn("Privacy Policy", r.text)
        self.assertIn("Staybot Realty", r.text)
        self.assertIn("privacy@example.com", r.text)
        self.assertIn("do not sell", r.text)

    def test_real_details_from_env_are_escaped(self):
        with mock.patch.dict(os.environ, {"BUSINESS_NAME": "Oak & Pine <Homes>", "PRIVACY_EMAIL": "hi@oakpine.com",
                                          "BUSINESS_POSTAL_ADDRESS": "1 Main St, Garner, NC"}):
            r = TestClient(app).get("/privacy")
        self.assertIn("Oak &amp; Pine &lt;Homes&gt;", r.text)
        self.assertIn("mailto:hi@oakpine.com", r.text)
        self.assertNotIn("privacy@example.com", r.text)
