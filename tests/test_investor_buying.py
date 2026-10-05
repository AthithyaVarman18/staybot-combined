"""An investor lists a home for sale -> another investor selects it and fills
in the buyer checklist -> the listing investor sees the buyer (Tenants tab)
-> sends a purchase offer -> the buyer accepts / counters on their Purchase
offers tab -> the sale is agreed and the home is marked sold."""

from datetime import date, datetime, timedelta, timezone
from unittest import mock

from src.services import investor_acquisition

from tests.test_rentals import RentalFixture


def terms(price="18000"):
    return {"price": price, "closing_date": (date.today() + timedelta(days=30)).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()}


class InvestorBuysFromInvestor(RentalFixture):

    def setUp(self):
        super().setUp()
        for t in ("investor_activity", "property_inquiries", "mls_listings", "investor_portfolio_properties"):
            self.fake.t.setdefault(t, [])
        # Olga's investor profile (what /me/investor and her Portfolio tab read).
        self.fake.t["investors"] = [{"id": "inv-olga", "status": "active", "session_id": "acct-i2"}]
        p = mock.patch.object(investor_acquisition, "_account_investor",
                              lambda request: {"id": "inv-olga", "name": "Olga Other"})
        p.start()
        self.addCleanup(p.stop)
        r = self.c.post("/me/listings", headers=self.owner, json={
            "listing_type": "sale", "property_type": "house", "bedrooms": 5, "location": "Garner", "sale_price": 18000})
        self.assertEqual(r.status_code, 200, r.text)
        self.sale = r.json()
        self.buyer = self.other_owner   # Olga, a New Property Investor

    def select(self, **extra):
        body = {"list_number": self.sale["id"], "financing": "cash", "offer_price": 17500, "note": "Can close fast",
                "details": {"full_name": "Olga Other", "phone": "+19195550199", "closing_timeline": "30_days"}, **extra}
        r = self.c.post("/me/investor/selected-homes", headers=self.buyer, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def buyer_rows(self):
        """Olga's Portfolio rows. (The seller's own row - made from their
        listing by listing_portfolio.sync - leaves when the home is sold.)"""
        rows = self.fake.t["investor_portfolio_properties"]
        if self.fake.t["properties"] and any(p["id"] == self.sale["id"] and p.get("status") == "sold"
                                             for p in self.fake.t["properties"]):
            others = [r for r in rows if r.get("investor_id") != "inv-olga" and r.get("property_id") == self.sale["id"]]
            self.assertEqual(others, [], "the sold home is still in the seller's Portfolio")
        return [r for r in rows if r.get("investor_id") == "inv-olga"]

    def owner_buyers(self):
        apps = self.c.get("/me/owner/applications", headers=self.owner).json()["applications"]
        return [a for a in apps if a.get("kind") == "purchase"]

    def test_full_flow_owner_offer_accepted_by_buyer(self):
        res = self.select()
        self.assertIsNotNone(res["owner_request"])

        # 1. The listing investor sees the buyer straight away, with their onboarding answers.
        [req] = self.owner_buyers()
        self.assertEqual(req["status"], "team_approved")
        self.assertEqual(req["tenant_name"], "Olga Other")
        self.assertIn("Cash buyer", req["summary"]["headline"])
        self.assertTrue(any(l.startswith("Price in mind: $17,500") for l in req["summary"]["lines"]))

        # 2. Owners can't "approve" a buyer - they send a purchase offer.
        r = self.c.post(f"/me/owner/applications/{req['id']}/decision", headers=self.owner, json={"decision": "approve"})
        self.assertEqual(r.status_code, 409)
        r = self.c.post(f"/me/owner/applications/{req['id']}/offer", headers=self.owner, json={"terms": terms()})
        self.assertEqual(r.status_code, 200, r.text)

        # 3. The buyer sees the owner's offer on their Purchase offers tab and accepts it.
        [mine] = self.c.get("/me/buying", headers=self.buyer).json()["requests"]
        self.assertEqual(mine["offer"]["waiting_on"], "tenant")
        self.assertNotIn("owner_session_id", mine)
        r = self.c.post(f"/me/buying/offers/{mine['offer']['id']}/respond", headers=self.buyer, json={"decision": "accept"})
        self.assertEqual(r.status_code, 200, r.text)

        # 4. Sale agreed: request approved, home marked sold (off every listing).
        [mine] = self.c.get("/me/buying", headers=self.buyer).json()["requests"]
        self.assertEqual(mine["status"], "approved")
        self.assertEqual(next(p for p in self.fake.t["properties"] if p["id"] == self.sale["id"])["status"], "sold")

        # 5. ...and it's in Olga's portfolio, counted in her Portfolio totals.
        [owned] = self.buyer_rows()
        self.assertEqual(owned["investor_id"], "inv-olga")
        self.assertEqual(owned["relationship"], "acquired")
        self.assertEqual(owned["purchase_price"], 18000)
        self.assertEqual(owned["outstanding_mortgage"], 0)          # she paid cash
        self.assertEqual(owned["property_id"], self.sale["id"])
        self.assertEqual(owned["address"], "5 bedroom house in Garner")   # the owner only gave the area
        from src.services import deals
        totals = deals.portfolio_totals(self.buyer_rows(), deals.Assumptions())["totals"]
        self.assertEqual(totals["equity"], 18000)
        self.assertEqual(totals["properties_counted_for_equity"], 1)

    def test_buyer_counter_offer_accepted_by_owner(self):
        self.select()
        [req] = self.owner_buyers()
        self.c.post(f"/me/owner/applications/{req['id']}/offer", headers=self.owner, json={"terms": terms("18000")})
        [mine] = self.c.get("/me/buying", headers=self.buyer).json()["requests"]
        r = self.c.post(f"/me/buying/offers/{mine['offer']['id']}/counter", headers=self.buyer, json={"terms": terms("17000")})
        self.assertEqual(r.status_code, 200, r.text)
        [req] = self.owner_buyers()
        self.assertEqual(req["offer"]["waiting_on"], "owner")
        r = self.c.post(f"/me/owner/applications/{req['id']}/offer/respond", headers=self.owner, json={"decision": "accept"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.owner_buyers()[0]["status"], "approved")
        # The portfolio records the agreed (counter-offer) price, once.
        [owned] = self.buyer_rows()
        self.assertEqual(owned["purchase_price"], 17000)

    def test_mortgage_buyer_gets_estimated_loan_balance_and_rent(self):
        self.select(financing="mortgage", details={"full_name": "Olga Other", "down_payment_percent": 25,
                                                   "expected_rent": 400})
        [req] = self.owner_buyers()
        self.c.post(f"/me/owner/applications/{req['id']}/offer", headers=self.owner, json={"terms": terms("20000")})
        [mine] = self.c.get("/me/buying", headers=self.buyer).json()["requests"]
        self.c.post(f"/me/buying/offers/{mine['offer']['id']}/respond", headers=self.buyer, json={"decision": "accept"})
        [owned] = self.buyer_rows()
        self.assertEqual(owned["outstanding_mortgage"], 15000)      # 75% of $20,000
        self.assertEqual(owned["monthly_rent"], 400)
        self.assertIn("25% down payment", owned["condition_notes"])

    def test_other_people_cannot_see_or_answer_the_offer(self):
        self.select()
        [req] = self.owner_buyers()
        offer = self.c.post(f"/me/owner/applications/{req['id']}/offer", headers=self.owner, json={"terms": terms()}).json()
        self.assertEqual(self.c.get("/me/buying", headers=self.owner).json()["requests"], [])
        r = self.c.post(f"/me/buying/offers/{offer['id']}/respond", headers=self.owner, json={"decision": "accept"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.c.get("/me/buying", headers=self.tenant).status_code, 403)

    def test_reselecting_updates_and_unselecting_withdraws(self):
        self.select()
        self.select(offer_price=17900)                 # edited answers: same request, refreshed
        self.assertEqual(len(self.owner_buyers()), 1)
        self.assertEqual(self.owner_buyers()[0]["details"]["offer_price"], 17900)
        r = self.c.delete(f"/me/investor/selected-homes/{self.sale['id']}", headers=self.buyer)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.owner_buyers()[0]["status"], "withdrawn")

    def test_rent_listing_and_own_home_file_no_request(self):
        # The owner "buying" their own sale home: no request to themselves.
        with mock.patch.object(investor_acquisition, "_account_investor", lambda request: {"id": "inv-ivan"}):
            r = self.c.post("/me/investor/selected-homes", headers=self.owner,
                            json={"list_number": self.sale["id"], "details": {}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["owner_request"])
        self.assertEqual(self.owner_buyers(), [])
