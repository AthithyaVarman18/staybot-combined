import unittest
from unittest import mock

from fastapi.testclient import TestClient

from src import main
from src.services import accounts

# Must match supabase_admin.sql
ADMIN_HASH = "1efb0573772ab310866dfffada52b9205e3cea9e85c3bcb72f451cf826693f6d"
ADMIN_SALT = "5579e8de3b1d23bc564b203f9a018731"

ADMIN = {"id": "adm", "name": "Admin", "email": "akileshkumar123@gmail.com", "role": "admin",
         "session_id": "admin-x", "investor_id": None, "education_seen": True,
         "password_hash": ADMIN_HASH, "password_salt": ADMIN_SALT}
TENANT = {**ADMIN, "id": "t1", "email": "jane@example.com", "role": "tenant"}


class AdminLogin(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(main.app, follow_redirects=False)

    def _post(self, path, payload, row):
        with mock.patch.object(accounts.db, "ENABLED", True), \
             mock.patch.object(accounts, "q", return_value=[row] if row else []), \
             mock.patch.object(accounts, "issue_session") as issued:
            return self.client.post(path, json=payload), issued

    def test_sql_hash_matches_the_password(self):
        self.assertTrue(accounts.verify_password("12345678", ADMIN_HASH, ADMIN_SALT))

    def test_admin_logs_in_on_hidden_page(self):
        res, issued = self._post("/auth/admin/login", {"email": "akileshkumar123@gmail.com", "password": "12345678"}, ADMIN)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["redirect_url"], "/ui")
        issued.assert_called_once()

    def test_admin_wrong_password(self):
        res, issued = self._post("/auth/admin/login", {"email": "akileshkumar123@gmail.com", "password": "nope"}, ADMIN)
        self.assertEqual(res.status_code, 401)
        issued.assert_not_called()

    def test_customer_cannot_use_admin_login(self):
        res, issued = self._post("/auth/admin/login", {"email": "jane@example.com", "password": "12345678"}, TENANT)
        self.assertEqual(res.status_code, 401)
        issued.assert_not_called()

    def test_public_login_refuses_admin(self):
        res, issued = self._post("/auth/login", {"email": "akileshkumar123@gmail.com", "password": "12345678"}, ADMIN)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["detail"], "Incorrect email or password.")
        issued.assert_not_called()

    def test_admin_page_is_public_and_noindex(self):
        with mock.patch.object(main.team_auth, "ADMIN_PASSWORD", "secret"):
            res = self.client.get("/admin/login")
        self.assertEqual(res.status_code, 200)
        self.assertIn("noindex", res.headers.get("x-robots-tag", ""))

    def test_admin_page_redirects_when_already_admin(self):
        with mock.patch.object(accounts, "current_account", return_value=ADMIN):
            res = self.client.get("/admin/login")
        self.assertEqual(res.headers["location"], "/ui")

    def test_admin_page_not_linked_from_customer_pages(self):
        for page in ("login.html", "register.html", "portal.html", "dashboard.html", "index.html"):
            text = (main.STATIC_DIR / page).read_text(encoding="utf-8")
            self.assertNotIn("/admin/login\"", text.replace("location.href = \"/admin/login\"", ""), page)


class StaffOnlyData(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(main.app, follow_redirects=False)

    def test_customer_blocked_from_leads_and_owner_leads(self):
        with mock.patch.object(accounts, "current_account", return_value=TENANT):
            for path in ("/leads", "/leads/abc", "/owner-leads", "/reports/outcomes", "/alerts", "/listings/pending"):
                self.assertEqual(self.client.get(path).status_code, 403, path)

    def test_admin_reaches_leads(self):
        with mock.patch.object(accounts, "current_account", return_value=ADMIN), \
             mock.patch.object(main.db, "ENABLED", True), \
             mock.patch.object(main.db, "list_leads", return_value=[{"id": "1"}]):
            res = self.client.get("/leads")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [{"id": "1"}])

    def test_customer_still_reaches_their_own_routes(self):
        with mock.patch.object(accounts, "current_account", return_value=TENANT):
            self.assertNotEqual(self.client.get("/ui").status_code, 403)
            # /viewings (every customer's phone) is staff-only; a customer's own
            # visits are at /me/viewings.
            self.assertEqual(self.client.get("/viewings").status_code, 403)
            self.assertNotEqual(self.client.get("/me/viewings").status_code, 403)


if __name__ == "__main__":
    unittest.main()
