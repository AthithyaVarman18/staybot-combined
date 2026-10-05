"""Tenant: Chat, WhatsApp and Maintenance are one account.

A tenant's WhatsApp messages go into the same conversation as their web
Chat (the account's session_id), maintenance reports there are filed against
the home they rent (so they show on the Maintenance tab), and the AI on any
channel knows about requests made on the others."""

import uuid
import unittest
from unittest import mock

from src.services import rentals, whatsapp

TENANT = {"id": "t1", "name": "Tia Tenant", "role": "tenant", "session_id": "acct-t1"}
INVESTOR = {"id": "i1", "name": "Ivan", "role": "existing_investor", "session_id": "acct-i1"}



def fake_get(table, params):
    if table == "account_portfolios":
        return [{"account_id": "t1", "details": {"phone": "(919) 555-0111"}},
                {"account_id": "i1", "details": {"phone": "+1 919 555 0122"}}]
    if table == "rental_applications":
        return []
    if table == "accounts":
        acct = {"t1": TENANT, "i1": INVESTOR}.get(params["id"].split(".", 1)[1])
        return [acct] if acct else []
    if table == "maintenance_tickets":
        return [{"issue_type": "plumbing", "urgency": "high", "ticket_status": "in_progress",
                 "summary": "Kitchen sink leaking", "notes": "Plumber booked Friday", "created_at": "2026-09-30T10:00:00Z"}]
    return []


def msg(phone_digits, text="My kitchen sink is leaking"):
    return {
        "id": f"wamid.test-{uuid.uuid4().hex}",
        "from": phone_digits,
        "text": text,
        "name": "Tia",
        "type": "text",
    }


@mock.patch.object(whatsapp.db, "ENABLED", True)
@mock.patch.object(whatsapp.db, "_get", side_effect=fake_get)
class WhatsAppLinkTests(unittest.TestCase):

    def test_phone_lookup_finds_the_tenant_whatever_the_format(self, _):
        self.assertEqual(whatsapp.tenant_account_for_phone("+19195550111")["id"], "t1")

    def test_investor_phone_is_not_linked_as_a_tenant(self, _):
        self.assertIsNone(whatsapp.tenant_account_for_phone("+19195550122"))

    def test_unknown_phone_stays_anonymous(self, _):
        self.assertIsNone(whatsapp.tenant_account_for_phone("+15555550199"))

    def test_tenant_message_uses_their_chat_session_and_tenant_role(self, _):
        with mock.patch.object(whatsapp, "load_history", return_value=[]), \
             mock.patch.object(whatsapp, "onboarding_reply_for", return_value=None), \
             mock.patch("src.services.owner_leads.handle_opt_out", return_value=None), \
             mock.patch.object(whatsapp.chat, "process_message", return_value={"response": "Logged"}) as pm:
            out = whatsapp.handle_incoming(msg("19195550111"))
        kw = pm.call_args.kwargs
        self.assertEqual(kw["session_id"], "acct-t1")       # same thread as web Chat
        self.assertEqual(kw["account_role"], "tenant")       # maintenance -> their rented home
        self.assertEqual(kw["account_id"], "t1")
        self.assertTrue(out["linked_account"])

    def test_known_account_from_test_tab_wins_over_phone(self, _):
        with mock.patch.object(whatsapp, "load_history", return_value=[]), \
             mock.patch.object(whatsapp, "onboarding_reply_for", return_value=None), \
             mock.patch("src.services.owner_leads.handle_opt_out", return_value=None), \
             mock.patch.object(whatsapp.chat, "process_message", return_value={"response": "ok"}) as pm:
            whatsapp.handle_incoming(msg("15555550199"), account=TENANT)
        self.assertEqual(pm.call_args.kwargs["session_id"], "acct-t1")

    def test_strangers_keep_their_own_whatsapp_thread(self, _):
        with mock.patch.object(whatsapp, "load_history", return_value=[]), \
             mock.patch.object(whatsapp, "onboarding_reply_for", return_value=None), \
             mock.patch("src.services.owner_leads.handle_opt_out", return_value=None), \
             mock.patch.object(whatsapp.chat, "process_message", return_value={"response": "hi"}) as pm:
            out = whatsapp.handle_incoming(msg("15555550199", "hi there, 3 bed in Garner?"))
        self.assertEqual(pm.call_args.kwargs["session_id"], "wa:+15555550199")
        self.assertIsNone(pm.call_args.kwargs["account_role"])
        self.assertFalse(out["linked_account"])


@mock.patch.object(rentals.db, "ENABLED", True)
@mock.patch.object(rentals.db, "_get", side_effect=fake_get)
class OpenTicketsNoteTests(unittest.TestCase):

    def test_ai_sees_requests_from_every_channel(self, _):
        note = rentals.open_tickets_note("acct-t1")
        self.assertIn("Kitchen sink leaking", note)
        self.assertIn("in progress", note)
        self.assertIn("Plumber booked Friday", note)

    def test_ticket_view_says_where_it_was_reported(self, _):
        self.assertEqual(rentals.ticket_view({"id": "x", "conversation_id": "c1", "session_id": "s"})["reported_in"], "chat")
        self.assertEqual(rentals.ticket_view({"id": "y", "conversation_id": None})["reported_in"], "maintenance_tab")
        self.assertNotIn("session_id", rentals.ticket_view({"id": "x", "session_id": "s"}))


if __name__ == "__main__":
    unittest.main()
