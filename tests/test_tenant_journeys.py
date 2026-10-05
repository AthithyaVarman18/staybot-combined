"""Tenant journey tracker: stages, whose move it is, needs-attention flags."""

import unittest
from datetime import datetime, timedelta, timezone

from tests.test_rentals import RentalFixture


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class TenantJourneys(RentalFixture):

    def journeys(self):
        r = self.c.get("/tenant-journeys", headers=self.team)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def row(self, data, account_id="t1"):
        return next(t for t in data["tenants"] if t["account_id"] == account_id)

    def setUp(self):
        super().setUp()
        for a in self.fake.t["accounts"]:
            a.setdefault("created_at", ago(10))  # real accounts always have one

    def test_journey_follows_the_tenant_to_housed(self):
        # Screened, nothing else yet.
        j = self.row(self.journeys())
        self.assertEqual((j["stage"], j["next_by"]), ("screening", "tenant"))

        # Chats with the assistant -> exploring.
        self.fake.t.setdefault("conversations", []).append({"session_id": "acct-t1", "created_at": ago(1)})
        self.assertEqual(self.row(self.journeys())["stage"], "exploring")

        # Applies -> team's move.
        app = self.apply(self.tenant)
        j = self.row(self.journeys())
        self.assertEqual((j["stage"], j["next_by"]), ("applied", "team"))
        self.assertIn(self.home["title"], j["next_action"])
        self.assertEqual(j["attention"], [])

        # ...and it's flagged once it has waited too long.
        self.fake.t["rental_applications"][0]["created_at"] = ago(4)
        j = self.row(self.journeys())
        self.assertTrue(any("waiting on team" in a for a in j["attention"]))

        # Team approves -> owner's move, named.
        self.c.post(f"/applications/{app['id']}/review", headers=self.team, json={"decision": "approve"})
        j = self.row(self.journeys())
        self.assertEqual((j["stage"], j["next_by"]), ("awaiting_owner", "owner"))
        self.assertIn("Ivan Investor", j["next_action"])

        # Owner approves -> housed; every milestone done.
        self.c.post(f"/me/owner/applications/{app['id']}/decision", headers=self.owner, json={"decision": "approve"})
        j = self.row(self.journeys())
        self.assertEqual(j["stage"], "housed")
        self.assertEqual([m["state"] for m in j["milestones"]], ["done"] * 6)
        self.assertEqual(j["home"]["owner"], "Ivan Investor")
        self.assertIsNone(j["next_by"])

        # A maintenance request -> owner's move again.
        self.c.post("/me/rentals/maintenance", headers=self.tenant, data={"description": "Tap leaking"})
        j = self.row(self.journeys())
        self.assertEqual(j["next_by"], "owner")
        self.assertEqual(j["counts"]["open_tickets"], 1)

        # Detail: a full, newest-first timeline.
        d = self.c.get("/tenant-journeys/t1", headers=self.team).json()
        titles = [e["title"] for e in d["events"]]
        self.assertEqual(titles[-1], "Signed up - journey started")
        for expected in ("Applied for", "Team approved", "Owner approved", "Reported a plumbing issue"):
            self.assertTrue(any(expected in t for t in titles), expected)

    def test_summary_and_funnel(self):
        app = self.apply(self.tenant)
        self.c.post(f"/applications/{app['id']}/review", headers=self.team, json={"decision": "approve"})
        data = self.journeys()
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(data["summary"]["by_stage"]["awaiting_owner"], 1)
        self.assertEqual(data["summary"]["waiting_on"]["owner"], 1)
        funnel = {f["key"]: f["reached"] for f in data["funnel"]}
        self.assertEqual((funnel["signed_up"], funnel["applied"], funnel["housed"]), (2, 1, 0))

    def test_new_signup_not_screened_gets_flagged(self):
        self.fake.t["accounts"].append({"id": "t3", "name": "New Nina", "email": "n@x.com", "role": "tenant",
                                        "session_id": "acct-t3", "screening_seen": False, "created_at": ago(5)})
        j = self.row(self.journeys(), "t3")
        self.assertEqual((j["stage"], j["next_by"]), ("signed_up", "tenant"))
        self.assertTrue(any("Screening not finished" in a for a in j["attention"]))

    def test_proof_of_income_review_is_the_teams_move(self):
        self.fake.t["account_documents"].append({"id": "d1", "account_id": "t1", "doc_key": "proof_of_income",
                                                 "status": "awaiting_review", "uploaded_at": ago(3)})
        j = self.row(self.journeys())
        self.assertEqual((j["next_by"], j["next_action"]), ("team", "Review proof of income"))
        self.assertTrue(any("unreviewed" in a for a in j["attention"]))

    def test_staff_only(self):
        for who in (self.tenant, self.owner):
            self.assertEqual(self.c.get("/tenant-journeys", headers=who).status_code, 403)
            self.assertEqual(self.c.get("/tenant-journeys/t1", headers=who).status_code, 403)

    def test_login_is_recorded(self):
        self.login("t@x.com", "tenant")
        acct = next(a for a in self.fake.t["accounts"] if a["id"] == "t1")
        self.assertTrue(acct.get("last_login_at"))
        self.assertGreaterEqual(acct.get("login_count"), 1)


if __name__ == "__main__":
    unittest.main()
