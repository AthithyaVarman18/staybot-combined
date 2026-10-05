"""Maintenance tab chat: the AI helps, and hands what it can't fix (or
anything urgent) to the home's owner and the team with a chat summary."""

import json
from unittest import mock

from src.services import db, maintenance_chat
from tests.test_rentals import RentalFixture


def ai_turn(**kw):
    base = {"reply": "Could you check the breaker panel?", "action": "continue", "emergency": False,
            "issue_type": "electrical", "urgency": "normal", "escalation_reason": None,
            "summary": "Kitchen power is out."}
    return {**base, **kw}


class MaintenanceChat(RentalFixture):

    def setUp(self):
        super().setUp()
        self.fake.t["maintenance_tickets"] = []
        mock.patch.object(db, "get_maintenance_ticket",
                          lambda tid: (self.fake.get("maintenance_tickets", {"id": f"eq.{tid}"}) or [None])[0]).start()
        self.addCleanup(mock.patch.stopall)

    def house_tenant(self):
        app = self.apply(self.tenant)
        self.c.post(f"/applications/{app['id']}/review", headers=self.team, json={"decision": "approve"})
        self.c.post(f"/me/owner/applications/{app['id']}/decision", headers=self.owner, json={"decision": "approve"})

    def say(self, message, history=(), ticket_id="", send_now=False):
        data = {"message": message, "history": json.dumps(list(history))}
        if ticket_id:
            data["ticket_id"] = ticket_id
        if send_now:
            data["send_now"] = "true"
        return self.c.post("/me/rentals/maintenance/chat", headers=self.tenant, data=data)

    def test_locked_until_the_tenant_rents_the_home(self):
        self.assertEqual(self.say("power is out").status_code, 403)

    def test_continue_creates_nothing_then_escalation_reaches_only_that_owner(self):
        self.house_tenant()
        with mock.patch.object(maintenance_chat, "ask_ai", return_value=ai_turn()):
            r = self.say("the power went out in my kitchen")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["action"], r.json()["ticket"]), ("continue", None))
        self.assertEqual(self.fake.t["maintenance_tickets"], [])

        history = [{"role": "tenant", "content": "the power went out in my kitchen"},
                   {"role": "assistant", "content": "Could you check the breaker panel?"}]
        with mock.patch.object(maintenance_chat, "ask_ai", return_value=ai_turn(
                action="escalate", reply="I've sent this to your owner and the team.",
                escalation_reason="Breaker reset didn't help",
                summary="Kitchen power out; tenant reset the breaker with no luck.")):
            r = self.say("I reset the breaker, still nothing", history)
        body = r.json()
        self.assertTrue(body["escalated_now"])
        ticket = body["ticket"]
        self.assertEqual(ticket["property_id"], self.home["id"])
        self.assertEqual(ticket["chat_summary"], "Kitchen power out; tenant reset the breaker with no luck.")
        self.assertEqual(len(ticket["chat_transcript"]), 4)

        owners = self.c.get("/me/owner/maintenance", headers=self.owner).json()["tickets"]
        self.assertEqual([t["id"] for t in owners], [ticket["id"]])
        self.assertEqual(owners[0]["escalation_reason"], "Breaker reset didn't help")
        self.assertEqual(self.c.get("/me/owner/maintenance", headers=self.other_owner).json()["tickets"], [])

        # Later messages update the same ticket, not a new one.
        with mock.patch.object(maintenance_chat, "ask_ai", return_value=ai_turn(summary="Now the fridge is off too.")):
            r = self.say("the fridge is off now too", history, ticket_id=ticket["id"])
        self.assertEqual(len(self.fake.t["maintenance_tickets"]), 1)
        self.assertEqual(self.fake.t["maintenance_tickets"][0]["chat_summary"], "Now the fridge is off too.")

    def test_emergency_is_urgent(self):
        self.house_tenant()
        with mock.patch.object(maintenance_chat, "ask_ai", return_value=maintenance_chat.normalize(
                {"reply": "Leave the home now and call 911 from outside.", "action": "continue",
                 "emergency": True, "issue_type": "gas", "summary": "Tenant smells gas."})):
            body = self.say("I smell gas").json()
        self.assertTrue(body["escalated_now"])
        self.assertEqual(body["urgency"], "urgent")
        self.assertEqual(self.fake.t["maintenance_tickets"][0]["urgency"], "urgent")

    def test_send_now_works_even_when_the_ai_is_down(self):
        self.house_tenant()
        history = [{"role": "tenant", "content": "mould on the bathroom ceiling"}]
        with mock.patch.object(maintenance_chat, "ask_ai", side_effect=RuntimeError("quota")):
            self.assertEqual(self.say("hello?", history).status_code, 503)
            r = self.say("", history, send_now=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["escalated_now"])
        self.assertIn("sent this to your owner", r.json()["reply"])

    def test_a_foreign_ticket_id_is_ignored(self):
        self.house_tenant()
        other = self.fake.post("maintenance_tickets", {"session_id": "acct-t2", "tenant_id": "x", "message": ""})
        with mock.patch.object(maintenance_chat, "ask_ai", return_value=ai_turn(summary="changed")):
            self.say("hi", ticket_id=other["id"])
        self.assertNotIn("chat_summary", self.fake.t["maintenance_tickets"][0])
