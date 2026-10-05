"""
Customer accounts must not be able to read the team's data by typing an API
URL (e.g. /onboarding/cases listed every lease case to a logged-in tenant),
and typing a data URL into the browser must open the dashboard, not raw JSON.
"""

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from src import main
from src.services import accounts, team_auth

TENANT = {"id": "t1", "name": "Jane", "email": "jane@example.com", "role": "tenant", "session_id": "s-t1",
          "investor_id": None, "education_seen": True, "screening_seen": True}
INVESTOR = {**TENANT, "id": "i1", "role": "existing_investor"}
ADMIN = {**TENANT, "id": "adm", "role": "admin"}

BROWSER = {"sec-fetch-mode": "navigate", "accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

STAFF_READS = (
    "/onboarding/cases", "/onboarding/cases/metrics", "/onboarding/cases/setup",
    "/onboarding/cases/options", "/onboarding/cases/abc", "/onboarding/cases/abc/final.pdf",
    "/onboarding", "/onboarding/abc",
    "/investors", "/investors/abc", "/journeys", "/journeys/abc", "/deals", "/deals/assumptions",
    "/whatsapp/outbox",
)


class CustomersCannotReadTeamData(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(main.app, follow_redirects=False)

    def test_tenant_and_investor_get_403_from_api(self):
        for account in (TENANT, INVESTOR):
            with mock.patch.object(accounts, "current_account", return_value=account):
                for path in STAFF_READS:
                    res = self.client.get(path)
                    self.assertEqual(res.status_code, 403, f"{account['role']} {path}")

    def test_customer_typing_the_url_goes_back_to_dashboard(self):
        with mock.patch.object(accounts, "current_account", return_value=TENANT):
            res = self.client.get("/onboarding/cases", headers=BROWSER)
        self.assertEqual(res.status_code, 303)
        self.assertEqual(res.headers["location"], "/ui")

    def test_customer_cannot_write_team_data(self):
        with mock.patch.object(accounts, "current_account", return_value=TENANT):
            for method, path in (("post", "/onboarding/cases"), ("put", "/onboarding/abc"),
                                 ("patch", "/properties/abc"), ("post", "/mls/import"),
                                 ("post", "/whatsapp/simulate-status"), ("post", "/deals")):
                res = getattr(self.client, method)(path, json={})
                self.assertEqual(res.status_code, 403, f"{method} {path}")

    def test_customer_pages_still_work(self):
        # Journey stage names (investor primer) and the public listing feed stay readable.
        self.assertFalse(team_auth.staff_only("/journeys/config/journeys", "GET"))
        self.assertFalse(team_auth.staff_only("/properties", "GET"))
        self.assertFalse(team_auth.staff_only("/mls", "GET"))
        self.assertFalse(team_auth.staff_only("/me/investor", "GET"))
        self.assertFalse(team_auth.staff_only("/whatsapp/simulate", "POST"))
        self.assertFalse(team_auth.staff_only("/ui/onboarding.js", "GET"))

    def test_admin_still_gets_the_api(self):
        with mock.patch.object(accounts, "current_account", return_value=ADMIN), \
             mock.patch("src.services.onboarding_cases.db.ENABLED", False):
            res = self.client.get("/onboarding/cases")
        self.assertNotIn(res.status_code, (401, 403))


class NoRawJsonInTheAddressBar(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(main.app, follow_redirects=False)

    def test_staff_typing_api_url_opens_the_matching_tab(self):
        with mock.patch.object(accounts, "current_account", return_value=ADMIN), \
             mock.patch.object(main.whatsapp, "OUTBOX", [{"to": "15550001111", "text": "hi"}]):
            res = self.client.get("/whatsapp/outbox", headers=BROWSER)
        self.assertEqual(res.status_code, 303)
        self.assertEqual(res.headers["location"], "/ui#whatsapp")

    def test_dashboard_fetch_still_gets_json(self):
        with mock.patch.object(accounts, "current_account", return_value=ADMIN), \
             mock.patch.object(main.whatsapp, "OUTBOX", [{"to": "15550001111", "text": "hi"}]):
            res = self.client.get("/whatsapp/outbox", headers={"sec-fetch-mode": "cors", "accept": "*/*"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()[0]["text"], "hi")

    def test_tab_mapping(self):
        self.assertEqual(team_auth.ui_url_for("/onboarding/cases"), "/ui#onboarding")
        self.assertEqual(team_auth.ui_url_for("/leads/abc"), "/ui#leads")
        self.assertEqual(team_auth.ui_url_for("/something-else"), "/ui")

    def test_health_and_docs_untouched(self):
        res = self.client.get("/health", headers=BROWSER)
        self.assertNotEqual(res.status_code, 303)


if __name__ == "__main__":
    unittest.main()
