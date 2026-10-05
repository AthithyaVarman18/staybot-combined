import unittest
from unittest.mock import patch

from src.services import email_sender, high_priority_inquiry_email as hp, lead_scoring


class HighPriorityEmailTests(unittest.TestCase):
    def setUp(self):
        self.inquiry = {
            "id": "inq-a",
            "property_id": "property-a",
            "property_title": "Property A",
            "area": "Area A",
            "price_label": "$500,000",
            "session_id": "tenant-a",
        }
        self.store = []
        self.sent = []

    def accounts_get(self, params):
        sid = params.get("session_id")
        iid = params.get("investor_id")
        if sid == "eq.tenant-a":
            return [{"id": "tenant-a", "name": "Tenant A", "email": "tenant-a@example.com",
                     "role": "tenant", "session_id": "tenant-a", "investor_id": None}]
        if sid == "eq.owner-a":
            return [{"id": "owner-a", "name": "Owner A", "email": "owner-a@example.com",
                     "role": "existing_investor", "session_id": "owner-a", "investor_id": "inv-a"}]
        if iid == "eq.inv-new-a":
            return [{"id": "new-a", "name": "New Investor A", "email": "new-a@example.com",
                     "role": "new_investor", "session_id": "new-a", "investor_id": "inv-new-a"}]
        if iid == "eq.inv-no-email":
            return [{"id": "no-email", "name": "No Email", "email": None,
                     "role": "existing_investor", "session_id": "no-email", "investor_id": "inv-no-email"}]
        return []

    def base_get(self, table, params):
        if table == "high_priority_inquiry_email_notifications":
            rows = list(self.store)
            for key in ("inquiry_id", "recipient_account_id", "trigger_type"):
                if key in params:
                    rows = [r for r in rows if r.get(key) == params[key].split(".", 1)[1]]
            return rows[: int(params.get("limit", "50"))]
        if table == "accounts":
            return self.accounts_get(params)
        if table == "properties":
            if params.get("id") == "eq.property-a":
                return [{"id": "property-a", "session_id": "owner-a"}]
            return []
        if table == "investor_portfolio_properties":
            if params.get("property_id") == "eq.property-a":
                return [{"investor_id": "inv-new-a"}]
            return []
        if table == "rental_applications":
            return []
        return []

    def fake_post(self, table, body, params=None):
        self.assertEqual(table, "high_priority_inquiry_email_notifications")
        row = {"id": f"mail-{len(self.store)+1}", **body}
        self.store.append(row)
        return row

    def fake_patch(self, table, body, params):
        row_id = params["id"].split(".", 1)[1]
        for row in self.store:
            if row["id"] == row_id:
                row.update(body)
        return [r for r in self.store if r["id"] == row_id]

    def send(self, to, subject, text, html, unsubscribe_url=None):
        self.sent.append((to, subject, text))
        return {"ok": True, "test_mode": False, "error": None}

    def call(self, inquiry=None, previous_score=89, previous_status="warm",
             score=91, status="warm", get_fn=None, send_fn=None):
        get_fn = get_fn or self.base_get
        send_fn = send_fn or self.send
        with patch.object(hp.db, "ENABLED", True), \
             patch.object(hp.db, "_get", side_effect=get_fn), \
             patch.object(hp.db, "_post", side_effect=self.fake_post), \
             patch.object(hp.db, "_patch", side_effect=self.fake_patch), \
             patch.object(hp.email_sender, "send", side_effect=send_fn):
            return hp.notify_for_update(
                inquiry or self.inquiry,
                previous_score, previous_status, score, status
            )

    def test_score_below_90_no_email(self):
        self.assertEqual(self.call(score=89), 0)
        self.assertEqual(self.sent, [])

    def test_score_exactly_90_no_email(self):
        self.assertEqual(self.call(previous_score=89, score=90), 0)
        self.assertEqual(self.sent, [])

    def test_score_91_triggers_email(self):
        self.assertEqual(self.call(score=91), 2)  # owner + investor associations (never the customer)
        self.assertTrue(self.sent)

    def test_score_95_triggers_email(self):
        self.assertEqual(self.call(previous_score=90, score=95), 2)
        self.assertTrue(self.sent)

    def test_status_changes_to_hot_triggers_email(self):
        self.assertEqual(self.call(previous_score=74, previous_status="warm", score=75, status="hot"), 2)
        self.assertEqual({r["trigger_type"] for r in self.store}, {"hot"})

    def test_repeated_hot_update_is_idempotent(self):
        self.assertEqual(self.call(previous_score=74, previous_status="warm", score=75, status="hot"), 2)
        self.assertEqual(self.call(previous_score=75, previous_status="hot", score=80, status="hot"), 0)
        self.assertEqual(len(self.store), 2)
        self.assertEqual(len(self.sent), 2)

    def isolated_get(self, allowed_role):
        def get(table, params):
            if table == "high_priority_inquiry_email_notifications":
                return self.base_get(table, params)
            if table == "accounts":
                if allowed_role == "tenant":
                    if params.get("session_id") == "eq.tenant-a":
                        return self.accounts_get(params)
                    return []
                if allowed_role == "existing_investor":
                    if params.get("session_id") == "eq.owner-a":
                        return self.accounts_get(params)
                    return []
                if allowed_role == "new_investor":
                    if params.get("investor_id") == "eq.inv-new-a":
                        return self.accounts_get(params)
                    return []
            if table == "properties":
                if allowed_role == "existing_investor" and params.get("id") == "eq.property-a":
                    return [{"id": "property-a", "session_id": "owner-a"}]
                return [{"id": "property-a", "session_id": None}] if allowed_role == "tenant" else []
            if table == "investor_portfolio_properties":
                if allowed_role == "new_investor" and params.get("property_id") == "eq.property-a":
                    return [{"investor_id": "inv-new-a"}]
                return []
            if table == "rental_applications":
                return []
            return []
        return get

    def test_customer_who_enquired_is_never_emailed(self):
        # The alert shows our internal lead score - the staff member is told
        # instead (staff_alerts.py), not the customer.
        self.assertEqual(self.call(
            get_fn=self.isolated_get("tenant"),
            previous_score=89, previous_status="warm", score=91, status="warm"
        ), 0)
        self.assertEqual(self.sent, [])

    def test_only_existing_investor_associated_with_property_a_receives_property_a_email(self):
        self.assertEqual(self.call(
            inquiry={**self.inquiry, "session_id": "unknown"},
            get_fn=self.isolated_get("existing_investor"),
            previous_score=89, previous_status="warm", score=91, status="warm"
        ), 1)
        self.assertEqual([x[0] for x in self.sent], ["owner-a@example.com"])

    def test_only_new_investor_associated_with_property_a_receives_property_a_email(self):
        self.assertEqual(self.call(
            inquiry={**self.inquiry, "session_id": "unknown"},
            get_fn=self.isolated_get("new_investor"),
            previous_score=89, previous_status="warm", score=91, status="warm"
        ), 1)
        self.assertEqual([x[0] for x in self.sent], ["new-a@example.com"])

    def test_property_b_users_receive_nothing_for_property_a(self):
        def get(table, params):
            if table == "high_priority_inquiry_email_notifications":
                return self.base_get(table, params)
            if table == "accounts":
                if params.get("session_id") == "eq.owner-b":
                    return [{"id": "owner-b", "name": "Owner B", "email": "owner-b@example.com",
                             "role": "existing_investor", "session_id": "owner-b", "investor_id": "inv-b"}]
                return []
            if table == "properties":
                return [{"id": "property-a", "session_id": "owner-a"}]
            if table in ("investor_portfolio_properties", "rental_applications"):
                return []
            return []
        self.assertEqual(self.call(get_fn=get, previous_score=89, score=91), 0)
        self.assertEqual(self.sent, [])

    def test_missing_recipient_email_is_recorded_as_failed(self):
        def get(table, params):
            if table == "high_priority_inquiry_email_notifications":
                return self.base_get(table, params)
            if table == "accounts":
                if params.get("investor_id") == "eq.inv-no-email":
                    return self.accounts_get(params)
                return []
            if table == "properties":
                return [{"id": "property-a", "session_id": None}]
            if table == "investor_portfolio_properties":
                return [{"investor_id": "inv-no-email"}]
            if table == "rental_applications":
                return []
            return []
        self.assertEqual(self.call(
            inquiry={**self.inquiry, "session_id": "unknown"},
            get_fn=get, previous_score=89, score=91
        ), 1)
        self.assertEqual(self.store[0]["delivery_status"], "failed")
        self.assertIn("No registered email", self.store[0]["error"])
        self.assertEqual(self.sent, [])

    def test_smtp_failure_does_not_break_processing(self):
        def fail_send(*args, **kwargs):
            return {"ok": False, "test_mode": False, "error": "SMTP unavailable"}
        self.assertEqual(self.call(
            previous_score=89, score=91, send_fn=fail_send
        ), 2)
        self.assertTrue(all(r["delivery_status"] == "failed" for r in self.store))
        self.assertTrue(all(r["error"] == "SMTP unavailable" for r in self.store))

    def test_dry_run_uses_existing_test_outbox(self):
        email_sender.OUTBOX.clear()
        with patch.object(email_sender, "DRY_RUN", True):
            result = email_sender.send(
                "tenant-a@example.com", "Staybot test", "body", "<p>body</p>"
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["test_mode"])
        self.assertEqual(email_sender.OUTBOX[-1]["to"], "tenant-a@example.com")

    def test_existing_lead_scoring_thresholds_are_unchanged(self):
        self.assertEqual(lead_scoring.tier_for(74), "warm")
        self.assertEqual(lead_scoring.tier_for(75), "hot")
        self.assertEqual(lead_scoring.tier_for(90), "hot")
        self.assertEqual(lead_scoring.tier_for(91), "hot")


if __name__ == "__main__":
    unittest.main()
