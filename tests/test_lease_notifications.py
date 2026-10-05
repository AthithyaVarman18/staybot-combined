import unittest
from datetime import date, timedelta
from unittest.mock import patch
from fastapi.testclient import TestClient

from src import main
from src.services import lease_notifications as ln


class LeaseNotificationLogic(unittest.TestCase):
    def test_calendar_month_addition_is_safe(self):
        self.assertEqual(ln.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(ln.add_months(date(2028, 1, 31), 1), date(2028, 2, 29))

    def test_explicit_lease_end_wins(self):
        app = {"details": {"lease_end": "2027-03-15", "move_in_date": "2026-10-01", "lease_months": 12}}
        self.assertEqual(ln.lease_end_from_application(app), date(2027, 3, 15))

    def test_lease_end_is_derived_from_move_in_and_months(self):
        app = {"details": {"move_in_date": "2026-10-01", "lease_months": 12}}
        self.assertEqual(ln.lease_end_from_application(app), date(2027, 9, 30))

    def test_lease_schedule_switches_from_30_day_reminders_to_15_day_daily(self):
        self.assertEqual(ln.expiry_bucket(300, 0)[0], "start_day")
        self.assertEqual(ln.expiry_bucket(270, 30)[0], "every_30_days_30")
        self.assertEqual(ln.expiry_bucket(210, 90)[0], "every_30_days_90")
        self.assertEqual(ln.expiry_bucket(60, 300)[0], "every_30_days_300")
        self.assertIsNone(ln.expiry_bucket(59, 301))
        self.assertIsNone(ln.expiry_bucket(16, 344))
        self.assertEqual(ln.expiry_bucket(15, 345)[0], "day_15")
        self.assertEqual(ln.expiry_bucket(1, 359)[0], "day_1")
        self.assertEqual(ln.expiry_bucket(0, 360)[0], "day_0")
        self.assertEqual(ln.expiry_bucket(-1, 361)[0], "expired")
        self.assertIsNone(ln.expiry_bucket(61, 299))

    def test_offer_schedule_has_three_two_tomorrow_then_hours(self):
        self.assertEqual(ln._offer_time_bucket(3 * 86400 - 1)[0], "d_3")
        self.assertEqual(ln._offer_time_bucket(2 * 86400 - 1)[0], "d_2")
        self.assertEqual(ln._offer_time_bucket(20 * 3600)[0], "d_1")
        self.assertEqual(ln._offer_time_bucket(12 * 3600)[0], "h_12")
        self.assertEqual(ln._offer_time_bucket(6 * 3600)[0], "h_6")
        self.assertEqual(ln._offer_time_bucket(3 * 3600)[0], "h_3")
        self.assertEqual(ln._offer_time_bucket(3600)[0], "h_1")
        self.assertEqual(ln._offer_time_bucket(-1)[0], "expired")


class AccountNotificationAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.store = []
        self.account = {
            "id": "tenant-account-1",
            "role": "tenant",
            "session_id": "tenant-session-1",
            "name": "Tenant One",
        }
        self.app_row = {
            "id": "application-1",
            "property_id": "demo-home",
            "property_title": "Demo Home",
            "tenant_account_id": "tenant-account-1",
            "tenant_session_id": "tenant-session-1",
            "owner_session_id": "owner-session-1",
            "status": "approved",
            "details": {"lease_end": "2026-10-11", "move_in_date": "2026-10-01", "lease_months": 1},
        }

    def fake_get(self, table, params):
        if table == "rental_applications":
            if params.get("tenant_account_id") == "eq.tenant-account-1":
                return [self.app_row]
            if params.get("owner_session_id") == "eq.owner-session-1":
                return [self.app_row]
            return []
        if table == "account_notifications":
            rows = list(self.store)
            if params.get("account_id"):
                rows = [r for r in rows if r["account_id"] == params["account_id"][3:]]
            if params.get("dedupe_key"):
                rows = [r for r in rows if r["dedupe_key"] == params["dedupe_key"][3:]]
            if params.get("read_at") == "is.null":
                rows = [r for r in rows if not r.get("read_at")]
            return rows[: int(params.get("limit", "50"))]
        if table == "properties":
            return [{"title": "Demo Home"}]
        return []

    def fake_post(self, table, body, params=None):
        self.assertEqual(table, "account_notifications")
        row = {
            "id": f"notification-{len(self.store) + 1}",
            **body,
            "read_at": None,
            "created_at": "2026-10-01T10:00:00+00:00",
        }
        self.store.append(row)
        return row

    def fake_patch(self, table, body, params):
        self.assertEqual(table, "account_notifications")
        matched = [r for r in self.store if r["id"] == params["id"][3:] and r["account_id"] == params["account_id"][3:]]
        for row in matched:
            row.update(body)
        return matched

    def test_tenant_get_creates_lease_notification_and_returns_it(self):
        with patch.object(ln.db, "ENABLED", True), \
             patch.object(ln.db, "_get", side_effect=self.fake_get), \
             patch.object(ln.db, "_post", side_effect=self.fake_post), \
             patch.object(ln.db, "_patch", side_effect=self.fake_patch), \
             patch.object(ln.accounts, "current_account", return_value=self.account):
            response = self.client.get("/me/account-notifications")

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["unread"], 1)
        self.assertEqual(data["notifications"][0]["notification_type"], "lease_expiry")
        self.assertIn("Demo Home", data["notifications"][0]["message"])

    def test_same_lease_milestone_is_not_duplicated_for_same_account(self):
        with patch.object(ln.db, "ENABLED", True), \
             patch.object(ln.db, "_get", side_effect=self.fake_get), \
             patch.object(ln.db, "_post", side_effect=self.fake_post), \
             patch.object(ln.accounts, "current_account", return_value=self.account):
            first = self.client.get("/me/account-notifications")
            second = self.client.get("/me/account-notifications")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.store), 1)
        self.assertEqual(second.json()["unread"], 1)

    def test_owner_and_tenant_each_receive_their_own_notification(self):
        owner = {"id": "owner-account-1", "role": "existing_investor", "session_id": "owner-session-1"}

        with patch.object(ln.db, "ENABLED", True), \
             patch.object(ln.db, "_get", side_effect=self.fake_get), \
             patch.object(ln.db, "_post", side_effect=self.fake_post), \
             patch.object(ln.accounts, "current_account", return_value=self.account):
            self.client.get("/me/account-notifications")

        # Owner account has the same approved application through owner_session_id.
        with patch.object(ln.db, "ENABLED", True), \
             patch.object(ln.db, "_get", side_effect=self.fake_get), \
             patch.object(ln.db, "_post", side_effect=self.fake_post), \
             patch.object(ln.accounts, "current_account", return_value=owner):
            response = self.client.get("/me/account-notifications")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.store), 2)
        self.assertEqual({r["recipient_role"] for r in self.store}, {"tenant", "owner"})

    def test_notification_can_be_marked_read_only_by_own_account(self):
        self.store.append({
            "id": "notification-1", "account_id": "tenant-account-1", "dedupe_key": "x",
            "notification_type": "lease_expiry", "title": "Lease", "message": "Expires", "read_at": None,
        })
        with patch.object(ln.db, "ENABLED", True), \
             patch.object(ln.db, "_get", side_effect=self.fake_get), \
             patch.object(ln.db, "_patch", side_effect=self.fake_patch), \
             patch.object(ln.accounts, "current_account", return_value=self.account):
            response = self.client.post("/me/account-notifications/notification-1/read")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(self.store[0]["read_at"])


class MergedNotificationSystems(unittest.TestCase):
    """The customer reminders (lease_notifications.py) and the owners'
    "someone asked about your house" pop-ups (owner_notifications.py) used to
    share /me/notifications - each must keep its own routes."""

    def test_routes_do_not_clash(self):
        paths = main.app.openapi()["paths"]
        tags = lambda prefix: {tag for path, ops in paths.items() if path.startswith(prefix)
                               for op in ops.values() for tag in op.get("tags", [])}
        self.assertEqual(tags("/me/notifications"), {"Owner notifications"})
        self.assertEqual(tags("/me/account-notifications"), {"Account notifications"})
        for prefix in ("/me/notifications", "/me/account-notifications"):
            for suffix in ("", "/read-all", "/{notification_id}/read"):
                self.assertIn(prefix + suffix, paths)

class MaintenanceAndOfferSync(unittest.TestCase):
    def setUp(self):
        self.store = []

    def fake_post(self, table, body, params=None):
        row = {"id": f"n-{len(self.store) + 1}", **body, "read_at": None}
        self.store.append(row)
        return row

    def fake_get(self, table, params):
        if table == "account_notifications":
            return [r for r in self.store if r["dedupe_key"] == params.get("dedupe_key", "")[3:]]
        if table == "maintenance_tickets":
            self.ticket_params = params
            return [{"id": "t-1", "session_id": "tenant-session-1", "property_id": "demo-home",
                     "property_title": "Demo Home", "issue_type": "plumbing", "ticket_status": "open",
                     "created_at": "2026-01-01T09:00:00+00:00"}]
        if table == "acquisition_offers":
            self.offer_params = params
            return [{"id": "o-1", "property_id": "demo-home", "property_title": "Demo Home", "current_version": 1}]
        if table == "acquisition_offer_versions":
            soon = (ln.datetime.now(ln.APP_TIMEZONE) + timedelta(hours=2)).isoformat()
            return [{"terms": {"expires_at": soon}}]
        return []

    def test_tenant_maintenance_reminder_is_scoped_and_labelled(self):
        tenant = {"id": "tenant-account-1", "role": "tenant", "session_id": "tenant-session-1"}
        with patch.object(ln.db, "_get", side_effect=self.fake_get), \
             patch.object(ln.db, "_post", side_effect=self.fake_post):
            self.assertEqual(ln.sync_maintenance_notifications(tenant), 1)
            self.assertEqual(ln.sync_maintenance_notifications(tenant), 0)  # deduplicated
        self.assertEqual(self.ticket_params["session_id"], "eq.tenant-session-1")
        self.assertIn("Plumbing".lower(), self.store[0]["message"].lower())
        self.assertEqual(self.store[0]["recipient_role"], "tenant")

    def test_offer_reminder_only_for_the_investors_own_offers(self):
        investor = {"id": "inv-account-1", "role": "existing_investor", "session_id": "s", "investor_id": "inv-1"}
        with patch.object(ln.db, "_get", side_effect=self.fake_get), \
             patch.object(ln.db, "_post", side_effect=self.fake_post):
            self.assertEqual(ln.sync_offer_notifications(investor), 1)
        self.assertEqual(self.offer_params["investor_id"], "eq.inv-1")
        self.assertEqual(self.store[0]["notification_type"], "offer_expiry")
        self.assertIn("3 hours left", self.store[0]["message"])
        # A tenant has no offers to check.
        self.assertEqual(ln.sync_offer_notifications({"id": "t", "role": "tenant"}), 0)


if __name__ == "__main__":
    unittest.main()
