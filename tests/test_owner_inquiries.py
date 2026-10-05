"""The investor who listed a home hears about every "Enquire about this"
enquiry on it: an email, plus the Enquiries list and badge on their Tenants
tab (src/services/inquiries.py, GET /me/owner/inquiries)."""

import unittest
from unittest import mock

from src.services import inquiries, rentals

OWNER = {"id": "o1", "name": "Olga Owner", "email": "olga@example.com", "role": "new_investor", "session_id": "acct-o1"}
HOME = {"id": "h1", "title": "3 bed house in Garner", "session_id": "acct-o1", "source": "investor_form"}


def fake_get(table, params):
    if table == "properties" and "session_id" in params:
        return [{"id": "h1"}]
    if table == "property_inquiries":
        return [
            {"id": "q1", "property_id": "h1", "property_title": HOME["title"], "customer_name": "Ivy",
             "customer_phone": "555-0100", "session_id": "acct-ivy", "status": "new", "created_at": "2026-10-01T10:00:00Z",
             "owner_name": "Olga Owner", "owner_phone": "555-0199", "owner_seen_at": None},
            {"id": "q2", "property_id": "h1", "property_title": HOME["title"], "customer_name": "Olga",
             "session_id": "acct-o1", "status": "new", "created_at": "2026-10-01T09:00:00Z"},  # the owner's own
            {"id": "q3", "property_id": "h1", "property_title": HOME["title"], "customer_name": "Sam",
             "session_id": "acct-sam", "status": "contacted", "created_at": "2026-09-30T09:00:00Z",
             "owner_seen_at": "2026-09-30T10:00:00Z"},
        ]
    return []


class OwnerInquiries(unittest.TestCase):

    def test_owner_is_emailed_about_a_new_enquiry(self):
        from src.services import email_sender
        with mock.patch.object(rentals, "one", return_value=HOME), \
             mock.patch.object(rentals, "owner_account_for", return_value=OWNER), \
             mock.patch.object(email_sender, "send") as send:
            inquiries.notify_owner({"property_id": "h1", "property_title": HOME["title"],
                                    "customer_name": "Ivy", "session_id": "acct-ivy"})
        send.assert_called_once()
        to, subject, body = send.call_args.args[:3]
        self.assertEqual(to, "olga@example.com")
        self.assertIn(HOME["title"], subject)
        self.assertIn("Ivy", body)

    def test_no_email_when_the_owner_enquires_about_their_own_home(self):
        from src.services import email_sender
        with mock.patch.object(rentals, "one", return_value=HOME), \
             mock.patch.object(rentals, "owner_account_for", return_value=OWNER), \
             mock.patch.object(email_sender, "send") as send:
            inquiries.notify_owner({"property_id": "h1", "property_title": HOME["title"], "session_id": "acct-o1"})
        send.assert_not_called()

    def test_no_email_for_team_managed_or_mls_homes(self):
        from src.services import email_sender
        with mock.patch.object(rentals, "one", return_value=None), \
             mock.patch.object(email_sender, "send") as send:
            inquiries.notify_owner({"property_id": "mls-123", "property_title": "MLS home", "session_id": "acct-ivy"})
        send.assert_not_called()

    def test_notify_never_raises(self):
        with mock.patch.object(rentals, "one", side_effect=RuntimeError("db down")):
            inquiries.notify_owner({"property_id": "h1", "property_title": "x"})

    def test_owner_list_hides_contacts_and_their_own_enquiries(self):
        with mock.patch.object(inquiries.db, "_get", side_effect=fake_get):
            rows = inquiries.for_owner(OWNER)
        self.assertEqual([r["id"] for r in rows], ["q1", "q3"])
        for r in rows:
            self.assertNotIn("customer_phone", r)
            self.assertNotIn("owner_phone", r)
            self.assertNotIn("session_id", r)
        self.assertTrue(rows[0]["is_new"])
        self.assertFalse(rows[1]["is_new"])

    def test_mark_seen_only_touches_unseen_ones(self):
        with mock.patch.object(inquiries.db, "_get", side_effect=fake_get), \
             mock.patch.object(inquiries.db, "_patch") as patch:
            marked = inquiries.mark_seen_by_owner(OWNER)
        self.assertEqual(marked, 1)
        self.assertEqual(patch.call_args.args[2], {"id": "in.(q1)"})


if __name__ == "__main__":
    unittest.main()
