"""Owner gets a pop-up (with the asker's details) when a tenant or a new
investor asks about their house."""

import unittest
from unittest import mock

from src.services import owner_notifications as on

OWNER = {"id": "o1", "name": "Olga Owner", "role": "existing_investor", "session_id": "acct-o1"}
TENANT = {"id": "t1", "name": "Tia Tenant", "email": "tia@x.com", "role": "tenant", "session_id": "acct-t1",
          "created_at": "2026-08-01T00:00:00Z"}
NEWBIE = {"id": "n1", "name": "Nick New", "email": "nick@x.com", "role": "new_investor", "session_id": "acct-n1"}
VETERAN = {"id": "v1", "name": "Vera", "role": "existing_investor", "session_id": "acct-v1"}
PROPS = {
    "p1": {"id": "p1", "title": "Sunny 3 Bed House Garner", "session_id": "acct-o1", "source": "investor_form"},
    "team": {"id": "team", "title": "Team listing", "session_id": None, "source": "seed"},
}


class Fake:
    def __init__(self):
        self.notes = []

    def get(self, table, params):
        key = lambda k: str(params.get(k, "")).split(".", 1)[-1]
        if table == "properties":
            p = PROPS.get(key("id"))
            return [p] if p else []
        if table == "accounts":
            pool = [OWNER, TENANT, NEWBIE, VETERAN]
            if "session_id" in params:
                return [a for a in pool if a["session_id"] == key("session_id")]
            return [a for a in pool if a["id"] == key("id")]
        if table == "account_portfolios":
            if key("account_id") == "t1":
                return [{"details": {"phone": "+19195550111", "employment_status": "employed",
                                     "monthly_income": 6200, "has_pets": True, "secret": "x"}}]
            return []
        if table == "owner_notifications":
            rows = [n for n in self.notes if n["owner_account_id"] == key("owner_account_id")]
            if "asker_account_id" in params:
                rows = [n for n in rows if n["asker_account_id"] == key("asker_account_id")
                        and n["property_id"] == key("property_id")]
            return rows
        return []

    def post(self, table, body):
        row = {"id": f"n{len(self.notes) + 1}", "ask_count": 1, "read_at": None, **body}
        self.notes.append(row)
        return row

    def patch(self, table, body, params):
        for n in self.notes:
            if n["id"] == str(params["id"]).split(".", 1)[1]:
                n.update(body)
                return n


class OwnerNotificationTests(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        on.RUN_IN_BACKGROUND = False
        for name, fn in (("_get", self.fake.get), ("_post", self.fake.post), ("_patch", self.fake.patch)):
            p = mock.patch.object(on.db, name, side_effect=fn)
            p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(on.db, "ENABLED", True); p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        on.RUN_IN_BACKGROUND = True

    def test_tenant_question_notifies_owner_with_details(self):
        n = on.notify("p1", TENANT, "Is it pet friendly?", "chat")
        self.assertEqual(n["owner_account_id"], "o1")
        a = n["asker"]
        self.assertEqual((a["name"], a["role_label"], a["email"], a["phone"]),
                         ("Tia Tenant", "Tenant", "tia@x.com", "+19195550111"))
        labels = [f["label"] for f in a["facts"]]
        self.assertIn("Monthly income", labels)
        self.assertNotIn("secret", str(a))
        self.assertEqual(n["question"], "Is it pet friendly?")

    def test_new_investor_question_notifies_owner(self):
        n = on.notify("p1", NEWBIE, "What's the cap rate?", "enquiry")
        self.assertEqual(n["asker"]["role_label"], "New Property Investor")

    def test_no_notification_for_other_roles_own_home_or_team_listing(self):
        self.assertIsNone(on.notify("p1", VETERAN, "hi", "chat"))
        self.assertIsNone(on.notify("p1", OWNER, "hi", "chat"))
        self.assertIsNone(on.notify("team", TENANT, "hi", "chat"))
        self.assertEqual(self.fake.notes, [])

    def test_follow_ups_are_folded_into_one_notification(self):
        on.notify("p1", TENANT, "Is it pet friendly?", "chat")
        n = on.notify("p1", TENANT, "And is parking included?", "chat")
        self.assertEqual(len(self.fake.notes), 1)
        self.assertEqual((n["ask_count"], n["question"]), (2, "And is parking included?"))

    def test_chat_hook_resolves_the_second_one(self):
        shown = [{"id": "team", "title": "Team listing"}, {"id": "p1", "title": "Sunny 3 Bed House Garner"}]
        on.from_chat("Is the second one pet friendly?", "t1", "tenant", shown_listings=shown)
        self.assertEqual(self.fake.notes[0]["property_id"], "p1")
        self.assertEqual(self.fake.notes[0]["source"], "chat")

    def test_chat_hook_ignores_general_questions(self):
        on.from_chat("It's my first time renting, any tips?", "t1", "tenant",
                     shown_listings=[{"id": "p1", "title": "x"}])
        self.assertEqual(self.fake.notes, [])


class PositionTests(unittest.TestCase):
    def test_positions(self):
        cases = {"is the first one pet friendly?": 1, "tell me about #3": 3, "option 2 please": 2,
                 "the last house looks nice": -1, "number 4?": 4,
                 "first time investor here": None, "3 bedroom in Garner": None}
        for text, want in cases.items():
            self.assertEqual(on.position_in(text), want, text)


if __name__ == "__main__":
    unittest.main()
