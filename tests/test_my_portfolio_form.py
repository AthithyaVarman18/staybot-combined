"""Portfolio tab: Existing Property Investors add the homes they own with a
form (no CSV) and can remove them; New Property Investors can't."""

from unittest import mock

from src.services import investor_journey

from tests.test_rentals import RentalFixture

INVESTORS = {"acct-i1": {"id": "inv-ivan"}, "acct-i2": {"id": "inv-olga"}}


class MyPortfolioForm(RentalFixture):

    def setUp(self):
        super().setUp()
        for t in ("investor_portfolio_properties", "investor_activity"):
            self.fake.t.setdefault(t, [])
        from src.services import accounts
        patches = [
            mock.patch.object(investor_journey, "_account_investor",
                              lambda request: dict(INVESTORS[accounts.current_account(request)["session_id"]])),
            mock.patch.object(investor_journey, "_maybe_graduate", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def add(self, who, **body):
        return self.c.post("/me/investor/portfolio", headers=who, json={"address": "12 Oak St, Garner", **body})

    def test_existing_investor_adds_several_homes_with_full_details(self):
        r = self.add(self.owner, monthly_rent=1800, monthly_expenses=450, estimated_value=260000,
                     outstanding_mortgage=150000, bedrooms=3, tenant_name="Ann", lease_start_date="2026-01-01",
                     lease_end_date="2026-12-31")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.add(self.owner, address="9 Elm Rd, Cary", monthly_rent=1500).status_code, 200)
        rows = self.fake.t["investor_portfolio_properties"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["outstanding_mortgage"], 150000)
        self.assertEqual(rows[0]["relationship"], "owned")
        self.assertTrue(all(r["investor_id"] == "inv-ivan" for r in rows))

    def test_bad_values_are_rejected(self):
        self.assertEqual(self.add(self.owner, monthly_rent=-5).status_code, 422)
        r = self.add(self.owner, lease_start_date="2026-06-01", lease_end_date="2026-01-01")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.add(self.owner, address="").status_code, 422)

    def test_remove_only_your_own_property(self):
        pid = self.add(self.owner).json()["id"]
        # Someone else's investor profile can't remove it.
        with mock.patch.object(investor_journey, "_account_investor", lambda request: {"id": "inv-someone-else"}):
            self.assertEqual(self.c.delete(f"/me/investor/portfolio/{pid}", headers=self.owner).status_code, 404)
        r = self.c.delete(f"/me/investor/portfolio/{pid}", headers=self.owner)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.fake.t["investor_portfolio_properties"], [])

    def test_new_investor_cannot_use_it(self):
        self.assertEqual(self.add(self.other_owner).status_code, 403)      # Olga is a New Property Investor
        pid = self.add(self.owner).json()["id"]
        self.assertEqual(self.c.delete(f"/me/investor/portfolio/{pid}", headers=self.other_owner).status_code, 403)
        self.assertEqual(len(self.fake.t["investor_portfolio_properties"]), 1)
