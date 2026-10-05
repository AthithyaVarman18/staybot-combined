"""Repairs reported in the main Chat tab or on WhatsApp get the same repair
assistant as the Maintenance tab (maintenance_chat.handle_chat_turn): help
first, the owner + team ticket only when it's needed (with a summary and the
chat), the conversation carries on across messages, and an unknown WhatsApp
number's ticket goes to the team as "home not identified"."""

from unittest import mock

from src.services import chat, db, maintenance_chat
from tests.test_rentals import RentalFixture


def turn(**kw):
    base = {"reply": "Could you check the breaker panel and flip the kitchen switch off and on?",
            "action": "continue", "emergency": False, "issue_type": "electrical", "urgency": "normal",
            "escalation_reason": None, "summary": "Kitchen power is out.", "home_address": None}
    return {**base, **kw}


class ChatRepairs(RentalFixture):

    def setUp(self):
        super().setUp()
        self.messages = []          # what db.add_message saved, with each reply's analysis
        self.intent = "maintenance_issue"

        def add_message(conversation_id, role, content, analysis=None):
            self.messages.append({"conversation_id": conversation_id, "role": role, "content": content,
                                  "analysis": analysis})

        for p in (
            mock.patch.object(chat, "analyze_message", side_effect=lambda **kw: {
                "role": "tenant", "intent": self.intent, "response": "Which unit are you in?",
                "maintenance_description": "power out"}),
            mock.patch.object(chat, "_fact_check", lambda *a, **k: None),
            mock.patch.object(chat.quick_replies, "quick_reply", lambda *a, **k: None),
            mock.patch.object(db, "get_or_create_conversation", lambda **kw: {"id": "conv-1"}),
            mock.patch.object(db, "add_message", side_effect=add_message),
            mock.patch.object(db, "list_messages", lambda cid: [m for m in self.messages if m["conversation_id"] == cid]),
            mock.patch.object(db, "update_conversation", lambda *a, **k: None),
            mock.patch.object(db, "find_open_maintenance_ticket", lambda cid: None),
            mock.patch.object(db, "get_maintenance_ticket",
                              lambda tid: (self.fake.get("maintenance_tickets", {"id": f"eq.{tid}"}) or [None])[0]),
        ):
            p.start()
            self.addCleanup(p.stop)

    def house_tenant(self):
        app = self.apply(self.tenant)
        self.c.post(f"/applications/{app['id']}/review", headers=self.team, json={"decision": "approve"})
        self.c.post(f"/me/owner/applications/{app['id']}/decision", headers=self.owner, json={"decision": "approve"})

    def tenant_says(self, text, **ai):
        with mock.patch.object(maintenance_chat, "ask_ai", return_value=turn(**ai)) as asked:
            r = chat.process_message(text, session_id="acct-t1", account_role="tenant", account_id="t1")
        return r, asked

    def test_help_first_then_ticket_with_summary_then_same_ticket_updated(self):
        self.house_tenant()

        # 1. The assistant helps first - nothing is sent to the owner yet.
        r, _ = self.tenant_says("The power went out in my kitchen")
        self.assertIn("breaker", r["response"])
        self.assertEqual(r["repair_assistant"]["action"], "continue")
        self.assertEqual(self.fake.t["maintenance_tickets"], [])

        # 2. The follow-up stays with the assistant even though the chat AI
        #    didn't call it a repair, and it hands it to the owner + team.
        self.intent = "general_question"
        r, asked = self.tenant_says("I flipped it, still no power", action="escalate",
                                    reply="Thanks for trying that - I've sent this to your owner and the team.",
                                    escalation_reason="Breaker reset didn't help",
                                    summary="Kitchen power out; breaker reset didn't help.")
        history = asked.call_args.args[1]
        self.assertEqual([m["role"] for m in history], ["tenant", "assistant"])   # the repair chat so far
        [t] = self.fake.t["maintenance_tickets"]
        self.assertEqual(t["property_id"], self.home["id"])
        self.assertEqual(t["conversation_id"], "conv-1")
        self.assertEqual(t["chat_summary"], "Kitchen power out; breaker reset didn't help.")
        self.assertEqual(len(t["chat_transcript"]), 4)
        self.assertIn("Maintenance tab", r["response"])

        # ...and only this tenant's owner sees it.
        owner_tickets = self.c.get("/me/owner/maintenance", headers=self.owner).json()["tickets"]
        self.assertEqual([x["id"] for x in owner_tickets], [t["id"]])
        self.assertEqual(self.c.get("/me/owner/maintenance", headers=self.other_owner).json()["tickets"], [])

        # 3. More messages update the same ticket - no duplicates.
        self.tenant_says("Now the fridge is off too", summary="Kitchen power and fridge out.")
        [t] = self.fake.t["maintenance_tickets"]
        self.assertEqual(t["chat_summary"], "Kitchen power and fridge out.")

    def test_emergency_is_urgent_straight_away(self):
        self.house_tenant()
        with mock.patch.object(maintenance_chat, "ask_ai", return_value=maintenance_chat.normalize({
                "reply": "Leave the home now and call 911 from outside.", "action": "continue",
                "emergency": True, "issue_type": "gas", "summary": "Tenant smells gas."})):
            r = chat.process_message("I smell gas in the kitchen", session_id="acct-t1",
                                     account_role="tenant", account_id="t1")
        self.assertIn("Leave the home", r["response"])
        self.assertIn("URGENT", r["response"])
        [t] = self.fake.t["maintenance_tickets"]
        self.assertEqual(t["urgency"], "urgent")

    def test_unknown_whatsapp_number_goes_to_the_team_as_home_not_identified(self):
        with mock.patch.object(maintenance_chat, "ask_ai", return_value=turn(
                action="escalate", reply="I've sent this to our team.", home_address="12 Oak St, Unit 4",
                summary="Water leaking from the ceiling.")) as asked:
            r = chat.process_message("Water is leaking from my ceiling, 12 Oak St unit 4", session_id="wa:+19195550199",
                                     customer_phone="+19195550199", channel="whatsapp")
        self.assertTrue(asked.call_args.args[0]["unidentified"])
        [t] = self.fake.t["maintenance_tickets"]
        self.assertIsNone(t["property_id"])
        self.assertEqual(t["tenant_id"], "12 Oak St, Unit 4")
        self.assertIn("Home not identified", t["escalation_reason"])
        self.assertIn("Sent to the team", r["response"])
        self.assertNotIn("Maintenance tab", r["response"])

    def test_ai_down_falls_back_to_the_basic_report(self):
        self.house_tenant()
        with mock.patch.object(maintenance_chat, "ask_ai", side_effect=RuntimeError("quota")):
            chat.process_message("The kitchen tap is leaking", session_id="acct-t1", account_role="tenant", account_id="t1")
        [t] = self.fake.t["maintenance_tickets"]          # the old classifier ticket, as before
        self.assertEqual(t["property_id"], self.home["id"])

    def test_changing_the_subject_leaves_the_repair_conversation(self):
        self.house_tenant()
        self.tenant_says("The power went out in my kitchen")
        self.intent = "property_search"
        r, asked = self.tenant_says("Actually, show me 3 bed homes in Garner")
        asked.assert_not_called()
        self.assertNotIn("repair_assistant", r)

    def test_a_resolved_ticket_is_not_reopened_by_new_messages(self):
        self.house_tenant()
        self.tenant_says("Tap is leaking badly", action="escalate", summary="Leak.")
        [t] = self.fake.t["maintenance_tickets"]
        t["ticket_status"] = "resolved"
        self.intent = "general_question"
        self.tenant_says("Thanks, all good now - by the way, when is rent due?", summary="changed")
        self.assertEqual(self.fake.t["maintenance_tickets"][0]["chat_summary"], "Leak.")

    def test_not_renting_yet_is_still_told_how_to_get_maintenance(self):
        from src.services import rentals
        r, asked = self.tenant_says("The kitchen tap is leaking")
        asked.assert_not_called()
        self.assertEqual(r["response"], rentals.MAINTENANCE_LOCKED)
