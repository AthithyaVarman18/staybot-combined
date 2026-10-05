"""Direct chat between a tenant and the investor who listed a home."""

import unittest
from unittest import mock

from src.services import rental_chat
from tests.test_rentals import RentalFixture


class TenantOwnerChat(RentalFixture):

    def start(self, who, **payload):
        return self.c.post("/me/messages/threads", headers=who, json=payload)

    def send(self, who, thread_id, text):
        r = self.c.post(f"/me/messages/threads/{thread_id}", headers=who, json={"body": text})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def threads(self, who):
        return self.c.get("/me/messages/threads", headers=who).json()

    def test_tenant_messages_owner_and_owner_replies(self):
        r = self.start(self.tenant, property_id=self.home["id"])
        self.assertEqual(r.status_code, 200, r.text)
        thread = r.json()
        self.assertEqual((thread["with_name"], thread["with_role"]), ("Ivan Investor", "owner"))
        self.assertNotIn("owner_session_id", thread)

        # Starting again re-opens the same conversation.
        self.assertEqual(self.start(self.tenant, property_id=self.home["id"]).json()["id"], thread["id"])

        m = self.send(self.tenant, thread["id"], "Hi! Is the yard fenced?")
        self.assertTrue(m["mine"])

        # Owner sees it, unread, with the tenant's name.
        mine = self.threads(self.owner)
        self.assertEqual(mine["unread"], 1)
        self.assertEqual(mine["threads"][0]["with_name"], "Tia Tenant")
        self.assertEqual(mine["threads"][0]["last_message_preview"], "Hi! Is the yard fenced?")

        # Opening it marks it read; the owner replies.
        r = self.c.get(f"/me/messages/threads/{thread['id']}", headers=self.owner).json()
        self.assertEqual([(x["body"], x["mine"]) for x in r["messages"]], [("Hi! Is the yard fenced?", False)])
        self.assertEqual(self.c.get("/me/messages/unread", headers=self.owner).json()["unread"], 0)
        reply = self.send(self.owner, thread["id"], "Yes, fully fenced.")

        # The tenant's poll picks up only the new message.
        self.assertEqual(self.threads(self.tenant)["unread"], 1)
        after = self.c.get(f"/me/messages/threads/{thread['id']}", headers=self.tenant, params={"after": m["created_at"]}).json()
        self.assertEqual([x["body"] for x in after["messages"]], ["Yes, fully fenced."])
        self.assertEqual(after["messages"][0]["id"], reply["id"])
        self.assertEqual(self.threads(self.tenant)["unread"], 0)

    def test_outsiders_cannot_read_or_post(self):
        thread = self.start(self.tenant, property_id=self.home["id"]).json()
        self.send(self.tenant, thread["id"], "Private question")
        for who in (self.tenant2, self.other_owner):
            self.assertEqual(self.c.get(f"/me/messages/threads/{thread['id']}", headers=who).status_code, 404)
            self.assertEqual(self.c.post(f"/me/messages/threads/{thread['id']}", headers=who, json={"body": "hi"}).status_code, 404)
            self.assertEqual(self.threads(who)["threads"], [])
        # The team/admin isn't part of these conversations.
        self.assertEqual(self.c.get("/me/messages/threads", headers=self.team).status_code, 403)

    def test_owner_starts_from_an_application(self):
        app = self.apply(self.tenant)
        # The owner sees a new application straight away and can message the applicant.
        r = self.start(self.owner, application_id=app["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["with_name"], "Tia Tenant")
        # Another owner can't.
        self.assertEqual(self.start(self.other_owner, application_id=app["id"]).status_code, 404)
        # Same conversation the tenant gets from the home.
        self.assertEqual(self.start(self.tenant, property_id=self.home["id"]).json()["id"], r.json()["id"])

    def test_tenant_can_keep_chatting_after_the_home_is_let_to_them(self):
        app = self.apply(self.tenant)
        self.c.post(f"/applications/{app['id']}/review", headers=self.team, json={"decision": "approve"})
        self.c.post(f"/me/owner/applications/{app['id']}/decision", headers=self.owner, json={"decision": "approve"})
        # Home is 'let' now - still reachable through their application.
        r = self.start(self.tenant, application_id=app["id"])
        self.assertEqual(r.status_code, 200, r.text)
        # But someone with no application can't open a new chat about a let home.
        self.assertEqual(self.start(self.tenant2, property_id=self.home["id"]).status_code, 404)

    def test_team_managed_homes_have_no_owner_chat(self):
        self.fake.t["properties"].append({"id": "team-home", "title": "Team home", "status": "active", "listing_type": "rent", "source": "admin"})
        self.assertEqual(self.start(self.tenant, property_id="team-home").status_code, 400)

    def test_optional_approval_gate(self):
        with mock.patch.object(rental_chat, "NEEDS_APPROVAL", True):
            self.assertEqual(self.start(self.tenant, property_id=self.home["id"]).status_code, 403)
            app = self.apply(self.tenant)
            self.c.post(f"/applications/{app['id']}/review", headers=self.team, json={"decision": "approve"})
            self.assertEqual(self.start(self.tenant, property_id=self.home["id"]).status_code, 200)

    def test_empty_message_rejected(self):
        thread = self.start(self.tenant, property_id=self.home["id"]).json()
        self.assertEqual(self.c.post(f"/me/messages/threads/{thread['id']}", headers=self.tenant, json={"body": "   "}).status_code, 400)


class MissingTableMessage(RentalFixture):
    """A setup SQL file not run yet -> a clear 503 naming the file, not a bare 500."""

    def test_missing_rental_applications_table(self):
        import requests
        from src.services import db

        real_get = db._get

        def get(table, params=None):
            if table == "rental_applications":
                resp = requests.models.Response()
                resp.status_code = 404
                resp._content = b'{"code":"PGRST205","message":"Could not find the table \'public.rental_applications\' in the schema cache"}'
                raise requests.exceptions.HTTPError("404", response=resp)
            return real_get(table, params)

        with mock.patch.object(db, "_get", get):
            r = self.c.get("/me/rentals", headers=self.tenant)
        self.assertEqual(r.status_code, 503)
        self.assertIn("supabase_rental_applications.sql", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
