"""Inquiries -> "All chats" and the customer pop-up (src/services/customers.py):
one card per person with their contact details from wherever they gave them,
the staff member's status / notes / follow-up, and staff-only access."""

from unittest import mock

from src.services import db
from tests.test_rentals import RentalFixture


class Customers(RentalFixture):

    def setUp(self):
        super().setUp()
        t = self.fake.t
        t.update({"conversations": [], "messages": [], "property_inquiries": [], "viewings": [],
                  "investor_profiles": [], "investors": [], "customer_followups": []})
        # Two chats from one WhatsApp number (general + about a listing), one from a tenant account.
        t["conversations"] += [
            {"id": "c1", "session_id": "wa:+19195550199", "lead_status": "warm", "intent_score": 60, "role": "tenant",
             "summary": "Wants a 2 bed in Garner.", "updated_at": "2026-10-01T10:00:00+00:00"},
            {"id": "c2", "session_id": "wa:+19195550199", "lead_status": "hot", "intent_score": 80, "role": "tenant",
             "summary": "Ready to rent now.", "updated_at": "2026-10-02T10:00:00+00:00"},
            {"id": "c3", "session_id": "acct-t1", "lead_status": "nurture", "intent_score": 30, "role": "tenant",
             "summary": None, "updated_at": "2026-09-30T10:00:00+00:00"},
        ]
        t["property_inquiries"].append({"id": "i1", "session_id": "wa:+19195550199", "customer_name": "Maria Lopez",
                                        "customer_phone": "+19195550199", "property_title": "2 bed in Garner",
                                        "status": "new", "created_at": "2026-10-02T09:00:00+00:00"})
        t["messages"].append({"conversation_id": "c2", "role": "user", "content": "Can I rent it this month?",
                              "created_at": "2026-10-02T10:00:00+00:00"})
        for p in (mock.patch.object(db, "list_leads", lambda limit=200: sorted(
                      self.fake.get("conversations"), key=lambda c: c["updated_at"], reverse=True)),
                  mock.patch.object(db, "list_messages", lambda cid: self.fake.get("messages", {"conversation_id": f"eq.{cid}"}))):
            p.start()
            self.addCleanup(p.stop)

    def test_one_card_per_person_with_their_details(self):
        r = self.c.get("/customers", headers=self.team)
        self.assertEqual(r.status_code, 200, r.text)
        cards = {c["session_id"]: c for c in r.json()["customers"]}
        self.assertEqual(set(cards), {"wa:+19195550199", "acct-t1"})
        maria = cards["wa:+19195550199"]
        self.assertEqual((maria["name"], maria["phone"], maria["channel"]), ("Maria Lopez", "+19195550199", "whatsapp"))
        self.assertEqual((maria["lead_status"], maria["score"]), ("hot", 80))       # best of her chats
        self.assertEqual(maria["homes_asked"], 1)
        tia = cards["acct-t1"]
        self.assertEqual((tia["name"], tia["phone"], tia["email"]), ("Tia Tenant", "+19195550101", "t@x.com"))  # from her account

    def test_popup_and_follow_up_tracking(self):
        r = self.c.get("/customers/wa%3A%2B19195550199", headers=self.team)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["customer"]["name"], "Maria Lopez")
        self.assertEqual([m["content"] for m in d["messages"]], ["Can I rent it this month?"])
        self.assertEqual([i["property_title"] for i in d["inquiries"]], ["2 bed in Garner"])

        r = self.c.patch("/customers/wa%3A%2B19195550199", headers=self.team, json={
            "status": "contacted", "notes": "Called - viewing Saturday", "follow_up_at": "2026-10-04T15:00:00Z"})
        self.assertEqual(r.status_code, 200, r.text)
        [row] = self.fake.t["customer_followups"]
        self.assertEqual((row["status"], row["notes"]), ("contacted", "Called - viewing Saturday"))
        self.assertTrue(row["contacted_at"])
        self.assertEqual(row["updated_by"], "staff:Priya")

        # Updating again changes the same row; clearing the date works.
        self.c.patch("/customers/wa%3A%2B19195550199", headers=self.team, json={"status": "won", "clear_follow_up": True})
        [row] = self.fake.t["customer_followups"]
        self.assertEqual((row["status"], row["follow_up_at"]), ("won", None))
        card = next(c for c in self.c.get("/customers", headers=self.team).json()["customers"]
                    if c["session_id"] == "wa:+19195550199")
        self.assertEqual(card["status"], "won")

    def test_staff_only(self):
        for who in (self.tenant, self.owner):
            self.assertEqual(self.c.get("/customers", headers=who).status_code, 403)
            self.assertEqual(self.c.get("/customers/acct-t1", headers=who).status_code, 403)
            self.assertEqual(self.c.patch("/customers/acct-t1", headers=who, json={"status": "won"}).status_code, 403)

    def test_bad_status_is_refused(self):
        r = self.c.patch("/customers/acct-t1", headers=self.team, json={"status": "maybe"})
        self.assertEqual(r.status_code, 422)


class CustomerDeals(Customers):
    """The pop-up's "Best deals": investors get the Investors page's matches,
    families buying get homes for sale in their budget, renters get nothing."""

    def test_investor_gets_matched_deals(self):
        self.fake.t["investor_profiles"].append({"id": "p1", "conversation_id": "c2", "cash_available": 60000,
                                                 "whatsapp": "+19195550199", "full_name": "Maria Lopez"})
        from src.services import investors
        match = {"listing": {"list_number": "MLS1", "street_address": "12 Oak St", "city": "Garner", "list_price": 240000,
                             "bedrooms": 3}, "cash_needed": 61000, "cash_flow_monthly": 310, "cap_rate_percent": 6.2,
                 "rent_source": "assumed 0.8% of price"}
        with mock.patch.object(investors, "match_listings", return_value={"matches": [match]}) as m:
            d = self.c.get("/customers/wa%3A%2B19195550199/deals", headers=self.team).json()
        self.assertEqual(m.call_args.args[0]["id"], "p1")
        self.assertEqual((d["kind"], d["profile_id"], d["can_send"]), ("investor", "p1", True))
        self.assertEqual((d["matches"][0]["street_address"], d["matches"][0]["cash_flow_monthly"]), ("12 Oak St", 310))

    def test_family_buyer_gets_homes_in_budget_and_area(self):
        self.fake.t["messages"].append({"conversation_id": "c2", "role": "assistant", "content": "ok",
                                        "analysis": {"requirements": {"rent_or_buy": "buy", "location": "Garner",
                                                                      "bedrooms": 3, "budget": "$300k"}}})
        self.fake.t["mls_listings"] = [
            {"list_number": "A", "street_address": "1 Elm", "city": "Garner", "list_price": 280000, "bedrooms": 3, "status_label": "active"},
            {"list_number": "B", "street_address": "2 Elm", "city": "Garner", "list_price": 350000, "bedrooms": 4, "status_label": "active"},
            {"list_number": "C", "street_address": "3 Elm", "city": "Garner", "list_price": 250000, "bedrooms": 2, "status_label": "active"},
            {"list_number": "D", "street_address": "4 Elm", "city": "Raleigh", "list_price": 290000, "bedrooms": 3, "status_label": "active"},
        ]
        with mock.patch.object(db, "_get", side_effect=lambda t, p=None: [
                r for r in self.fake.get(t, {k: v for k, v in (p or {}).items() if not (t == "mls_listings" and k == "list_price")})
                if t != "mls_listings" or r["list_price"] <= 300000]), \
             mock.patch.object(db, "owner_sale_listings", return_value=[]):
            d = self.c.get("/customers/wa%3A%2B19195550199/deals", headers=self.team).json()
        self.assertEqual(d["kind"], "buyer")
        self.assertEqual([h["list_number"] for h in d["matches"]], ["A"])   # in budget, 3+ bed, in Garner

    def test_renter_gets_nothing(self):
        self.fake.t["messages"].append({"conversation_id": "c2", "role": "assistant", "content": "ok",
                                        "analysis": {"requirements": {"rent_or_buy": "rent", "location": "Garner"}}})
        d = self.c.get("/customers/wa%3A%2B19195550199/deals", headers=self.team).json()
        self.assertEqual((d["kind"], d["matches"]), ("renter", []))

    def test_deals_are_staff_only(self):
        self.assertEqual(self.c.get("/customers/acct-t1/deals", headers=self.tenant).status_code, 403)


class SimilarHomes(Customers):
    """A Sales enquirer the AI has no budget for: homes like the one they asked about."""

    def test_homes_like_the_one_they_asked_about(self):
        self.fake.t["property_inquiries"][0]["property_id"] = "MLS-REF"
        self.fake.t["mls_listings"] = [
            {"list_number": "MLS-REF", "street_address": "9 Ref Rd", "city": "Garner", "list_price": 300000, "bedrooms": 3, "status_label": "active"},
            {"list_number": "N1", "street_address": "1 Near", "city": "Garner", "list_price": 310000, "bedrooms": 3, "status_label": "active"},
            {"list_number": "N2", "street_address": "2 Far", "city": "Raleigh", "list_price": 295000, "bedrooms": 3, "status_label": "active"},
            {"list_number": "X1", "street_address": "3 Pricey", "city": "Garner", "list_price": 500000, "bedrooms": 4, "status_label": "active"},
        ]
        def fake_get(t, p=None):
            p = dict(p or {})
            cap = p.pop("list_price", None) if t == "mls_listings" else None
            rows = self.fake.get(t, p)
            if cap and cap.startswith("lte."):
                rows = [r for r in rows if r["list_price"] <= float(cap[4:])]
            return rows
        with mock.patch.object(db, "_get", side_effect=fake_get), \
             mock.patch.object(db, "owner_sale_listings", return_value=[]):
            d = self.c.get("/customers/wa%3A%2B19195550199/deals", headers=self.team).json()
        self.assertEqual(d["kind"], "similar")
        self.assertEqual(d["reference"]["price"], 300000)
        # Within +/-15%, not the home itself, same town first.
        self.assertEqual([h["list_number"] for h in d["matches"]], ["N1", "N2"])
