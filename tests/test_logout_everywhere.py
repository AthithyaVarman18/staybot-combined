"""Every page a signed-in person can land on has a Log out button."""

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "static"


class LogoutEverywhere(unittest.TestCase):

    def test_customer_pages_have_log_out(self):
        for page in ("tenant_screening.html", "investor_education.html", "dashboard.html", "portal.html"):
            html = (STATIC / page).read_text(encoding="utf-8")
            self.assertRegex(html, r">\s*(<svg[\s\S]*?</svg>\s*)?Log out\s*<", page)
            self.assertIn("/auth/logout", html + (STATIC / "tab-session.js").read_text(), page)

    def test_dashboard_log_out_is_for_everyone(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        button = re.search(r'<button[^>]*id="admin-logout"[^>]*>', html).group(0)
        # A .view-tab is hidden by the role filter for tenants/investors - the
        # Log out button must not be one.
        self.assertNotIn("view-tab", button)
        self.assertIn("function showAccountBox()", html)
        self.assertNotIn('$("admin-box").hidden = !isAdmin', html)

    def test_shared_logout_helper(self):
        js = (STATIC / "tab-session.js").read_text()
        self.assertIn("window.staybotLogout", js)


if __name__ == "__main__":
    unittest.main()
