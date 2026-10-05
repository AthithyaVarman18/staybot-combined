import unittest
from unittest import mock

from fastapi.testclient import TestClient

from src import main


class PropertySceneAssets(unittest.TestCase):
    """The login/register 3D viewer scripts must load without the team password."""

    def setUp(self):
        self.client = TestClient(main.app)

    def test_scripts_are_public(self):
        with mock.patch.object(main.team_auth, "ADMIN_PASSWORD", "secret"):
            for path in ("/assets/property-scene.js",):
                res = self.client.get(path, headers={"x-forwarded-for": "1.2.3.4"})
                self.assertEqual(res.status_code, 200, path)
                self.assertIn("javascript", res.headers["content-type"])

    def test_login_and_register_include_the_viewer(self):
        for path in ("/login", "/register"):
            html = self.client.get(path).text
            self.assertIn("/assets/property-scene.js", html, path)


if __name__ == "__main__":
    unittest.main()
