import unittest
from unittest import mock

from fastapi.testclient import TestClient
from pydantic import ValidationError

from src import main
from src.services import accounts


class Passwords(unittest.TestCase):

    def test_verify_accepts_correct_password(self):
        password_hash, salt = accounts.hash_password("supersecret123")
        self.assertTrue(accounts.verify_password("supersecret123", password_hash, salt))

    def test_verify_rejects_wrong_password(self):
        password_hash, salt = accounts.hash_password("supersecret123")
        self.assertFalse(accounts.verify_password("wrong-password", password_hash, salt))

    def test_same_password_different_salts_produce_different_hashes(self):
        hash_a, salt_a = accounts.hash_password("supersecret123")
        hash_b, salt_b = accounts.hash_password("supersecret123")
        self.assertNotEqual(salt_a, salt_b)
        self.assertNotEqual(hash_a, hash_b)

    def test_password_is_never_stored_in_plaintext_form(self):
        password_hash, _ = accounts.hash_password("supersecret123")
        self.assertNotIn("supersecret123", password_hash)


class RegisterValidation(unittest.TestCase):

    def test_lowercases_and_trims_email(self):
        body = accounts.RegisterIn(name="Jane Doe", email="  JANE@Example.com ", password="supersecret", role="tenant")
        self.assertEqual(body.email, "jane@example.com")

    def test_rejects_invalid_email(self):
        with self.assertRaises(ValidationError):
            accounts.RegisterIn(name="Jane", email="not-an-email", password="supersecret", role="tenant")

    def test_rejects_short_password(self):
        with self.assertRaises(ValidationError):
            accounts.RegisterIn(name="Jane", email="jane@example.com", password="short", role="tenant")

    def test_rejects_unknown_role(self):
        with self.assertRaises(ValidationError):
            accounts.RegisterIn(name="Jane", email="jane@example.com", password="supersecret", role="landlord")

    def test_accepts_all_three_workflows(self):
        for role in ("tenant", "new_investor", "existing_investor"):
            body = accounts.RegisterIn(name="Jane", email="jane@example.com", password="supersecret", role=role,
                                        existing_property_count=2 if role == "existing_investor" else None)
            self.assertEqual(body.role, role)


class LoginValidation(unittest.TestCase):

    def test_lowercases_email(self):
        body = accounts.LoginIn(email="JANE@EXAMPLE.COM", password="anything")
        self.assertEqual(body.email, "jane@example.com")


class PostAuthRedirect(unittest.TestCase):
    """Register and login land on the main dashboard (/ui) - or, for a new
    tenant, the one-time screening intake first. /dashboard.html is the
    legacy leads page: it needs a session, and customers go to /ui."""

    def setUp(self):
        self.client = TestClient(main.app, follow_redirects=False)

    def test_redirect_target_is_dashboard_html(self):
        self.assertEqual(accounts.POST_AUTH_REDIRECT, "/ui")

    def _post_auth(self, path, payload):
        """Call /auth/register or /auth/login with the database faked out."""
        fake_account = {"id": "a1", "name": "Jane", "email": "jane@example.com", "role": "tenant",
                        "session_id": "acct-1", "investor_id": None,
                        "password_hash": "h", "password_salt": "s"}
        with mock.patch.object(accounts.db, "ENABLED", True), \
             mock.patch.object(accounts, "q", side_effect=lambda fn, *a, **k: [] if fn is accounts.db._get and a[0] == "accounts" and path == "/auth/register" else ([fake_account] if fn is accounts.db._get else fake_account)), \
             mock.patch.object(accounts, "verify_password", return_value=True), \
             mock.patch.object(accounts, "hash_password", return_value=("h", "s")), \
             mock.patch.object(accounts, "issue_session"):
            return self.client.post(path, json=payload)

    def test_register_redirects_to_dashboard_html(self):
        res = self._post_auth("/auth/register", {"name": "Jane Doe", "email": "jane@example.com",
                                                   "password": "supersecret", "role": "tenant"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["redirect_url"], "/tenant-screening")  # new tenant: screening first

    def test_login_redirects_to_dashboard_html(self):
        res = self._post_auth("/auth/login", {"email": "jane@example.com", "password": "supersecret"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["redirect_url"], "/tenant-screening")  # screening not done yet

    def test_dashboard_html_without_session_goes_to_login(self):
        res = self.client.get("/dashboard.html")
        self.assertEqual(res.status_code, 307)
        self.assertEqual(res.headers["location"], "/login")

    def test_dashboard_html_with_customer_session_goes_to_ui(self):
        with mock.patch.object(accounts, "current_account", return_value={"id": "a1", "role": "tenant"}):
            res = self.client.get("/dashboard.html")
        self.assertIn(res.status_code, (302, 303, 307))
        self.assertEqual(res.headers["location"], "/ui")

    def test_dashboard_html_with_admin_session_serves_the_page(self):
        with mock.patch.object(accounts, "current_account", return_value={"id": "a1", "role": "admin"}):
            res = self.client.get("/dashboard.html")
        self.assertEqual(res.status_code, 200)
        self.assertIn("dashboard", res.text.lower())

    def test_dashboard_html_is_not_behind_the_team_password(self):
        # Must not get the team Basic-auth challenge, even with ADMIN_PASSWORD set.
        with mock.patch.object(main.team_auth, "ADMIN_PASSWORD", "secret"):
            res = self.client.get("/dashboard.html")
        self.assertNotEqual(res.status_code, 401)


class NewInvestorEducationRedirect(unittest.TestCase):
    """New Property Investor accounts see the real-estate-basics primer once,
    before the dashboard; every other role is unaffected."""

    def setUp(self):
        self.client = TestClient(main.app, follow_redirects=False)

    def _post_auth(self, path, payload, account_overrides):
        fake_account = {"id": "a1", "name": "Jane", "email": "jane@example.com",
                        "session_id": "acct-1", "investor_id": None,
                        "password_hash": "h", "password_salt": "s", "education_seen": False,
                        **account_overrides}
        with mock.patch.object(accounts.db, "ENABLED", True), \
             mock.patch.object(accounts, "q", side_effect=lambda fn, *a, **k: [] if fn is accounts.db._get and a[0] == "accounts" and path == "/auth/register" else ([fake_account] if fn is accounts.db._get else fake_account)), \
             mock.patch.object(accounts, "verify_password", return_value=True), \
             mock.patch.object(accounts, "hash_password", return_value=("h", "s")), \
             mock.patch.object(accounts, "issue_session"), \
             mock.patch.object(accounts, "create_investor_for_signup", return_value="inv-1"):
            return self.client.post(path, json=payload)

    def test_new_investor_register_redirects_to_education_page(self):
        res = self._post_auth("/auth/register", {"name": "Jane Doe", "email": "jane@example.com",
                                                   "password": "supersecret", "role": "new_investor"},
                               {"role": "new_investor"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["redirect_url"], "/investor-education")

    def test_existing_investor_register_still_goes_straight_to_dashboard(self):
        res = self._post_auth("/auth/register", {"name": "Jane Doe", "email": "jane@example.com",
                                                   "password": "supersecret", "role": "existing_investor",
                                                   "existing_property_count": 2},
                               {"role": "existing_investor"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["redirect_url"], "/requirements")

    def test_new_investor_login_redirects_to_education_page_when_unseen(self):
        res = self._post_auth("/auth/login", {"email": "jane@example.com", "password": "supersecret"},
                               {"role": "new_investor", "education_seen": False})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["redirect_url"], "/investor-education")

    def test_new_investor_login_goes_to_dashboard_once_already_seen(self):
        res = self._post_auth("/auth/login", {"email": "jane@example.com", "password": "supersecret"},
                               {"role": "new_investor", "education_seen": True})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["redirect_url"], "/requirements")

    def test_investor_education_page_without_session_goes_to_login(self):
        res = self.client.get("/investor-education")
        self.assertEqual(res.status_code, 307)
        self.assertEqual(res.headers["location"], "/login")

    def test_investor_education_page_with_session_serves_the_page(self):
        with mock.patch.object(accounts, "current_account", return_value={"id": "a1", "role": "new_investor"}):
            res = self.client.get("/investor-education")
        self.assertEqual(res.status_code, 200)
        self.assertIn("real estate", res.text.lower())

    def test_education_complete_marks_seen_and_returns_dashboard(self):
        account = {"id": "a1", "role": "new_investor", "education_seen": False,
                   "name": "Jane", "email": "jane@example.com", "session_id": "acct-1", "investor_id": None}
        with mock.patch.object(accounts, "current_account", return_value=account), \
             mock.patch.object(accounts, "q") as patched_q:
            res = self.client.post("/auth/education-complete")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["redirect_url"], "/requirements")
        self.assertTrue(res.json()["account"]["education_seen"])
        patched_q.assert_called_once()

    def test_education_complete_requires_login(self):
        with mock.patch.object(accounts, "current_account", return_value=None):
            res = self.client.post("/auth/education-complete")
        self.assertEqual(res.status_code, 401)


class ExistingInvestorWelcome(unittest.TestCase):
    """A New Property Investor who gets a property is moved to Existing
    Property Investor and sees a one-time welcome page before the dashboard."""

    PENDING = {"id": "a1", "role": "existing_investor", "existing_welcome_pending": True,
               "name": "Jane Doe", "email": "jane@example.com", "session_id": "acct-1", "investor_id": None}

    def setUp(self):
        self.client = TestClient(main.app, follow_redirects=False)

    def test_login_redirects_to_welcome_while_pending(self):
        self.assertEqual(accounts.post_auth_redirect(self.PENDING), "/existing-investor-welcome")

    def test_login_goes_to_dashboard_once_seen(self):
        self.assertEqual(accounts.post_auth_redirect({**self.PENDING, "existing_welcome_pending": False}), "/requirements")

    def test_registered_existing_investor_never_gets_it(self):
        self.assertEqual(accounts.post_auth_redirect({"role": "existing_investor"}), "/ui")

    def test_ui_redirects_to_welcome_while_pending(self):
        with mock.patch.object(accounts, "current_account", return_value=self.PENDING):
            res = self.client.get("/ui")
        self.assertIn(res.status_code, (302, 303, 307))
        self.assertEqual(res.headers["location"], "/existing-investor-welcome")

    def test_welcome_page_without_session_goes_to_login(self):
        with mock.patch.object(accounts, "current_account", return_value=None):
            res = self.client.get("/existing-investor-welcome")
        self.assertEqual(res.headers["location"], "/login")

    def test_welcome_page_once_seen_goes_to_dashboard(self):
        with mock.patch.object(accounts, "current_account", return_value={**self.PENDING, "existing_welcome_pending": False}):
            res = self.client.get("/existing-investor-welcome")
        self.assertEqual(res.headers["location"], "/ui")

    def test_welcome_page_served_while_pending(self):
        with mock.patch.object(accounts, "current_account", return_value=self.PENDING):
            res = self.client.get("/existing-investor-welcome")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Existing Property Investor", res.text)

    def test_welcome_complete_clears_the_flag(self):
        with mock.patch.object(accounts, "current_account", return_value=self.PENDING), \
             mock.patch.object(accounts, "q") as patched_q:
            res = self.client.post("/auth/existing-welcome-complete")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["redirect_url"], "/requirements")
        self.assertFalse(res.json()["account"]["existing_welcome_pending"])
        patched_q.assert_called_once()

    def test_graduating_moves_the_account_and_queues_the_welcome(self):
        from src.services import investor_journey
        patches = []
        with mock.patch.object(investor_journey, "load", return_value={"id": "inv1", "investor_type": "new", "stage": "closing"}), \
             mock.patch.object(investor_journey.db, "_get", return_value=[{"id": "a1", "role": "new_investor"}]), \
             mock.patch.object(investor_journey.db, "_post"), \
             mock.patch.object(investor_journey.db, "_patch", side_effect=lambda table, body, where: patches.append((table, body))):
            investor_journey.graduate_to_existing_investor("inv1", "test")
        self.assertIn(("accounts", {"role": "existing_investor"}), patches)
        self.assertIn(("accounts", {"existing_welcome_pending": True}), patches)

    def test_graduating_a_tenant_buyer_queues_nothing(self):
        # A tenant who bought their rented home is upgraded by home_purchase.py, not here.
        from src.services import investor_journey
        patches = []
        with mock.patch.object(investor_journey, "load", return_value={"id": "inv1", "investor_type": "new", "stage": "closing"}), \
             mock.patch.object(investor_journey.db, "_get", return_value=[{"id": "t1", "role": "tenant"}]), \
             mock.patch.object(investor_journey.db, "_post"), \
             mock.patch.object(investor_journey.db, "_patch", side_effect=lambda table, body, where: patches.append((table, body))):
            investor_journey.graduate_to_existing_investor("inv1", "test")
        self.assertFalse([p for p in patches if p[0] == "accounts"])


if __name__ == "__main__":
    unittest.main()
