"""Congratulation pop-ups: tenant assigned, investor bought, owner sold."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from src.services import celebrations

NOW = datetime.now(timezone.utc).isoformat()
OLD = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

TENANT = {"id": "t1", "session_id": "acct-t1", "role": "tenant"}
BUYER = {"id": "b1", "session_id": "acct-b1", "role": "new_investor"}
OWNER = {"id": "o1", "session_id": "acct-o1", "role": "existing_investor"}

APPS = [
    {"id": "a1", "property_id": "p1", "property_title": "12 Oak St", "tenant_account_id": "t1",
     "owner_session_id": "acct-o1", "status": "approved", "details": {}, "owner_decided_at": NOW},
    {"id": "a2", "property_id": "p2", "property_title": "4 Elm Ave", "tenant_account_id": "b1",
     "owner_session_id": "acct-o1", "status": "approved", "details": {"kind": "purchase"}, "owner_decided_at": NOW},
    {"id": "a3", "property_id": "p3", "property_title": "Old Rental", "tenant_account_id": "t1",
     "owner_session_id": "acct-o1", "status": "approved", "details": {}, "owner_decided_at": OLD},
    {"id": "a4", "property_id": "p4", "property_title": "Pending", "tenant_account_id": "t1",
     "owner_session_id": "acct-o1", "status": "team_approved", "details": {}, "owner_decided_at": NOW},
]
BUYS = [
    {"id": "h1", "property_id": "p5", "property_title": "9 Pine Rd", "tenant_account_id": "t1",
     "owner_session_id": "acct-o1", "status": "completed", "completed_at": NOW},
]


def fake_get(table, params):
    rows = {"rental_applications": APPS, "home_buy_requests": BUYS}[table]
    out = []
    for r in rows:
        if all(k in ("select",) or str(r.get(k)) == str(v).split(".", 1)[1] for k, v in params.items()):
            out.append(r)
    return out


@mock.patch.object(celebrations.db, "_get", side_effect=fake_get)
class CelebrationTests(unittest.TestCase):
    def kinds(self, account):
        return sorted((e["kind"], e["property_id"]) for e in celebrations.events_for(account))

    def test_tenant_assigned_and_bought_their_rental(self, _):
        self.assertEqual(self.kinds(TENANT), [("home_bought", "p5"), ("tenant_assigned", "p1")])

    def test_investor_buyer(self, _):
        self.assertEqual(self.kinds(BUYER), [("home_bought", "p2")])

    def test_owner_sold_both_ways_but_not_rentals(self, _):
        self.assertEqual(self.kinds(OWNER), [("home_sold", "p2"), ("home_sold", "p5")])

    def test_messages(self, _):
        ev = next(e for e in celebrations.events_for(OWNER) if e["property_id"] == "p2")
        self.assertIn("4 Elm Ave has been sold", ev["message"])


if __name__ == "__main__":
    unittest.main()
