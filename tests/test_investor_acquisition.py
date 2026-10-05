"""Purchase offers + inspections for New / Existing Property Investor accounts.

Covers: the two /ui nav tabs and who sees them, the staff workspaces staying
staff-only, the public token approval link, and the /me/investor/... routes
only ever returning the logged-in investor's own records.
"""
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi.testclient import TestClient

from src import main
from src.services import accounts, db, file_store, investor_acquisition, investor_journey, purchase_offers

ME_INV = "11111111-1111-1111-1111-111111111111"
OTHER_INV = "22222222-2222-2222-2222-222222222222"
MY_OFFER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_OFFER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
MY_DEAL = "dddddddd-dddd-dddd-dddd-dddddddddddd"
MY_REPORT = "cccccccc-cccc-cccc-cccc-cccccccccccc"
OTHER_REPORT = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

BASE = {"name": "Ivy", "email": "ivy@example.com", "session_id": "s-ivy", "education_seen": True, "screening_seen": True}
NEW_INVESTOR = {**BASE, "id": "acc-1", "role": "new_investor", "investor_id": ME_INV}
EXISTING_INVESTOR = {**BASE, "id": "acc-2", "role": "existing_investor", "investor_id": ME_INV}
TENANT = {**BASE, "id": "acc-3", "role": "tenant", "investor_id": None}
ADMIN = {**BASE, "id": "adm", "role": "admin", "investor_id": None}


def future(days=10):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def terms(**over):
    return {"price": "250000.00", "earnest_money": "5000.00", "due_diligence_fee": "1500.00",
            "due_diligence_end": "2099-01-10", "closing_date": "2099-02-01", "expires_at": future(), **over}


def seed():
    return {
        "investors": [{"id": ME_INV, "name": "Ivy"}, {"id": OTHER_INV, "name": "Other"}],
        "acquisition_offers": [
            {"id": MY_OFFER, "reference": "PO-1", "investor_id": ME_INV, "deal_id": MY_DEAL, "status": "awaiting_investor_approval",
             "current_version": 2, "state_version": 3, "property_id": "p1", "property_title": "12 Oak St",
             "created_by": "staff:Sam", "updated_at": "2026-09-01T00:00:00+00:00"},
            {"id": OTHER_OFFER, "reference": "PO-2", "investor_id": OTHER_INV, "deal_id": None, "status": "awaiting_investor_approval",
             "current_version": 1, "state_version": 1, "property_id": "p2", "property_title": "9 Elm St",
             "created_by": "staff:Sam", "updated_at": "2026-09-01T00:00:00+00:00"},
        ],
        "acquisition_offer_versions": [
            {"id": "v-my-1", "offer_id": MY_OFFER, "version_number": 1, "terms": terms(price="240000.00")},
            {"id": "v-my-2", "offer_id": MY_OFFER, "version_number": 2, "terms": terms()},
            {"id": "v-ot-1", "offer_id": OTHER_OFFER, "version_number": 1, "terms": terms(price="999999.00")},
        ],
        "acquisition_offer_handoffs": [],
        "acquisition_offer_events": [
            {"id": 1, "offer_id": MY_OFFER, "sequence": 1, "event_type": "offer", "actor_type": "staff", "actor_id": "staff:Sam",
             "amount": "250000.00", "note": "Offer prepared.", "created_at": "2026-09-01T00:00:00+00:00"},
        ],
        "acquisition_offer_deadlines": [
            {"id": 1, "offer_id": MY_OFFER, "kind": "due_diligence_end", "label": "Due diligence ends", "due_at": "2099-01-10T23:59:00+00:00", "status": "active"},
            {"id": 2, "offer_id": OTHER_OFFER, "kind": "due_diligence_end", "label": "Due diligence ends", "due_at": "2099-01-10T23:59:00+00:00", "status": "active"},
        ],
        "acquisition_offer_approvals": [],
        "inspectors": [{"id": "ins-1", "name": "Pat Inspect", "license_number": "NC-123", "turnaround": "48h", "contact": "pat@x"}],
        "inspection_bookings": [
            {"id": "b-1", "offer_id": MY_OFFER, "deal_id": MY_DEAL, "inspector_id": "ins-1", "status": "confirmed",
             "confirmed_date": "2099-01-02", "confirmed_time": "10:00", "proposed_slots": [], "access_confirmed": True,
             "fee_payer": "investor", "listing_agent_contact": "agent@secret", "created_by": "staff:Sam", "created_at": "2026-09-02"},
            {"id": "b-2", "offer_id": None, "deal_id": MY_DEAL, "inspector_id": "ins-1", "status": "proposed",
             "proposed_slots": [{"date": "2099-01-03", "time": "09:00", "label": "Jan 3 9am"}], "access_confirmed": False,
             "fee_payer": "unknown", "created_by": "staff:Sam", "created_at": "2026-09-03"},
            {"id": "b-3", "offer_id": OTHER_OFFER, "deal_id": None, "inspector_id": "ins-1", "status": "proposed",
             "proposed_slots": [], "access_confirmed": False, "fee_payer": "unknown", "created_by": "staff:Sam", "created_at": "2026-09-04"},
        ],
        "inspection_reports": [
            {"id": MY_REPORT, "offer_id": MY_OFFER, "deal_id": None, "inspector_id": "ins-1", "file_name": "report.pdf",
             "storage_path": "inspections/x/report.pdf", "sha256": "abc", "uploaded_by": "staff:Sam", "created_at": "2026-09-05"},
            {"id": OTHER_REPORT, "offer_id": OTHER_OFFER, "deal_id": None, "inspector_id": "ins-1", "file_name": "theirs.pdf",
             "storage_path": "inspections/y/theirs.pdf", "sha256": "def", "uploaded_by": "staff:Sam", "created_at": "2026-09-05"},
        ],
        "inspection_findings": [
            {"id": 1, "report_id": MY_REPORT, "severity": "major", "summary": "Roof leak", "report_quote": "Active leak at north valley",
             "source_page": "4", "created_by": "staff:Sam"},
            {"id": 2, "report_id": OTHER_REPORT, "severity": "major", "summary": "Their issue", "report_quote": "x", "created_by": "staff:Sam"},
        ],
        "inspection_repairs": [
            {"id": 1, "offer_id": MY_OFFER, "request": "Fix roof", "cost_amount": "1200.00", "cost_source": "staff_quote",
             "status": "requested", "requested_by": "staff:Sam"},
            {"id": 2, "offer_id": OTHER_OFFER, "request": "Theirs", "status": "requested", "requested_by": "staff:Sam"},
        ],
    }


def matches(row, params):
    for key, cond in params.items():
        if key in ("select", "order", "limit"):
            continue
        value = row.get(key)
        if cond.startswith("eq."):
            if str(value).lower() != cond[3:].lower():
                return False
        elif cond.startswith("in.("):
            if str(value) not in cond[4:-1].split(","):
                return False
        elif cond == "is.null":
            if value is not None:
                return False
        else:
            raise AssertionError(f"unsupported filter {key}={cond}")
    return True


class FakeDB:
    def __init__(self):
        self.tables = seed()
        self.rpcs = []

    def get(self, table, params=None):
        rows = [dict(r) for r in self.tables.get(table, []) if matches(r, params or {})]
        limit = (params or {}).get("limit")
        return rows[: int(limit)] if limit else rows

    def post(self, path, body, params=None):
        if path.startswith("rpc/"):
            self.rpcs.append((path[4:], body))
            return {"status": "investor_approved" if body.get("p_decision") == "approved" else "awaiting_investor_approval"}
        self.tables.setdefault(path, []).append(body)
        return body


class InvestorAcquisitionBase(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(main.app, follow_redirects=False)
        self.fake = FakeDB()
        self.patches = [
            mock.patch.object(db, "ENABLED", True),
            mock.patch.object(db, "_get", side_effect=self.fake.get),
            mock.patch.object(db, "_post", side_effect=self.fake.post),
        ]
        for p in self.patches:
            p.start()
        self.account = NEW_INVESTOR

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def as_account(self, account):
        return mock.patch.object(accounts, "current_account", return_value=account)


class NavTabs(unittest.TestCase):
    """The two new /ui tabs and who they're shown to."""

    html = (main.STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_purchase_offer_workflow_replaces_standalone_inspection_tab(self):
        self.assertIn('data-view="offers"', self.html)
        self.assertNotIn('data-view="inspections"', self.html)
        self.assertIn('id="view-offers"', self.html)
        self.assertIn('data-acq-step="1"', self.html)
        self.assertIn('data-acq-step="2"', self.html)
        self.assertIn('Next: Inspection', self.html)
        self.assertIn('Back: Purchase Offers', self.html)
        self.assertIn('Accept an offer before continuing to inspection.', self.html)

    def test_views_list_contains_only_purchase_offer_entry(self):
        views = re.search(r"const VIEWS = \[(.*?)\];", self.html).group(1)
        self.assertIn('"offers"', views)
        self.assertNotIn('"inspections"', views)

    def test_tenant_does_not_get_them(self):
        tenant_line = re.search(r'if \(role === "tenant"\) return \[(.*?)\];', self.html).group(1)
        self.assertNotIn("offers", tenant_line)
        self.assertNotIn("inspections", tenant_line)

    def test_investor_filter_keeps_purchase_offers(self):
        excluded = re.search(r"return VIEWS\.filter\(\(v\) => !\[(.*?)\]\.includes\(v\)\);", self.html).group(1)
        self.assertNotIn("offers", excluded)
        self.assertNotIn("inspections", excluded)

    def test_investor_panels_call_scoped_routes_only(self):
        self.assertIn('fetch("/me/investor/purchase-offers")', self.html)
        self.assertIn('fetch("/me/investor/inspections")', self.html)

    def test_staff_workspace_embedded_for_team(self):
        self.assertIn('"/team/purchase-offers"', self.html)
        self.assertIn('"/team/inspections"', self.html)


class StaffOnlyWorkspaces(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(main.app, follow_redirects=False)

    def test_investor_accounts_refused_staff_workspace(self):
        paths = ("/purchase-offers", "/purchase-offers/options", f"/purchase-offers/{MY_OFFER}", f"/purchase-offers/{MY_OFFER}/summary.pdf",
                 "/inspections", "/inspections/bookings", "/inspections/inspectors", f"/inspections/reports/{MY_REPORT}/file",
                 "/team/purchase-offers", "/team/inspections")
        for account in (NEW_INVESTOR, EXISTING_INVESTOR, TENANT):
            with mock.patch.object(accounts, "current_account", return_value=account):
                for path in paths:
                    self.assertEqual(self.client.get(path).status_code, 403, (account["role"], path))
                res = self.client.post("/purchase-offers", json={})
                self.assertEqual(res.status_code, 403)

    def test_admin_reaches_staff_workspace(self):
        with mock.patch.object(accounts, "current_account", return_value=ADMIN):
            self.assertEqual(self.client.get("/team/purchase-offers").status_code, 200)
            self.assertEqual(self.client.get("/team/inspections").status_code, 200)

    def test_approval_link_is_public(self):
        with mock.patch.object(main.team_auth, "ADMIN_PASSWORD", "secret"), \
             mock.patch.object(accounts, "current_account", return_value=None):
            res = self.client.get("/purchase-approval/some-token")
        self.assertEqual(res.status_code, 200)

    def test_investor_scoped_routes_need_login(self):
        with mock.patch.object(main.team_auth, "ADMIN_PASSWORD", "secret"), \
             mock.patch.object(accounts, "current_account", return_value=None):
            self.assertEqual(self.client.get("/me/investor/purchase-offers").status_code, 401)
            self.assertEqual(self.client.get("/me/investor/inspections").status_code, 401)


class MyOffers(InvestorAcquisitionBase):

    def test_new_and_existing_investor_see_only_their_offer(self):
        for account in (NEW_INVESTOR, EXISTING_INVESTOR):
            with self.as_account(account):
                res = self.client.get("/me/investor/purchase-offers")
            self.assertEqual(res.status_code, 200, res.text)
            offers = res.json()["offers"]
            self.assertEqual([o["offer"]["id"] for o in offers], [MY_OFFER])
            self.assertNotIn("999999.00", res.text)

    def test_shows_current_version_terms_and_hides_staff_fields(self):
        with self.as_account(NEW_INVESTOR):
            item = self.client.get("/me/investor/purchase-offers").json()["offers"][0]
        self.assertEqual(item["version_number"], 2)
        self.assertEqual(item["terms"]["price"], "250000.00")
        self.assertTrue(item["can_decide"])
        self.assertNotIn("created_by", item["offer"])
        self.assertNotIn("actor_id", item["events"][0])

    def test_tenant_refused(self):
        with self.as_account(TENANT):
            self.assertEqual(self.client.get("/me/investor/purchase-offers").status_code, 403)

    def test_other_investors_offer_is_404(self):
        with self.as_account(NEW_INVESTOR):
            self.assertEqual(self.client.get(f"/me/investor/purchase-offers/{OTHER_OFFER}/summary.pdf").status_code, 404)
            res = self.client.post(f"/me/investor/purchase-offers/{OTHER_OFFER}/decision",
                                   json={"version_number": 1, "decision": "approved", "reviewed_summary": True})
            self.assertEqual(res.status_code, 404)
        self.assertEqual(self.fake.rpcs, [])

    def test_own_summary_pdf(self):
        with self.as_account(NEW_INVESTOR), \
             mock.patch.object(purchase_offers, "render_version_pdf", return_value=(b"%PDF-1.4 x", "h")) as render:
            res = self.client.get(f"/me/investor/purchase-offers/{MY_OFFER}/summary.pdf")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["content-type"], "application/pdf")
        self.assertEqual(render.call_args[0][1]["version_number"], 2)

    def test_approve_records_current_version_for_this_investor(self):
        with self.as_account(NEW_INVESTOR):
            res = self.client.post(f"/me/investor/purchase-offers/{MY_OFFER}/decision",
                                   json={"version_number": 2, "decision": "approved", "reviewed_summary": True})
        self.assertEqual(res.status_code, 200, res.text)
        name, body = self.fake.rpcs[-1]
        self.assertEqual(name, "acquisition_offer_record_approval")
        self.assertEqual(body["p_version_id"], "v-my-2")
        self.assertEqual(body["p_actor_id"], ME_INV)
        self.assertEqual(body["p_decision"], "approved")

    def test_stale_version_refused(self):
        with self.as_account(NEW_INVESTOR):
            res = self.client.post(f"/me/investor/purchase-offers/{MY_OFFER}/decision",
                                   json={"version_number": 1, "decision": "approved", "reviewed_summary": True})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.fake.rpcs, [])

    def test_approve_needs_review_confirmation(self):
        with self.as_account(NEW_INVESTOR):
            res = self.client.post(f"/me/investor/purchase-offers/{MY_OFFER}/decision",
                                   json={"version_number": 2, "decision": "approved", "reviewed_summary": False})
        self.assertEqual(res.status_code, 400)

    def test_changes_need_a_note(self):
        with self.as_account(NEW_INVESTOR):
            res = self.client.post(f"/me/investor/purchase-offers/{MY_OFFER}/decision",
                                   json={"version_number": 2, "decision": "changes_requested", "note": " ", "reviewed_summary": False})
        self.assertEqual(res.status_code, 400)

    def test_not_open_for_approval(self):
        self.fake.tables["acquisition_offers"][0]["status"] = "handed_off"
        with self.as_account(NEW_INVESTOR):
            item = self.client.get("/me/investor/purchase-offers").json()["offers"][0]
            res = self.client.post(f"/me/investor/purchase-offers/{MY_OFFER}/decision",
                                   json={"version_number": 2, "decision": "approved", "reviewed_summary": True})
        self.assertFalse(item["can_decide"])
        self.assertEqual(res.status_code, 409)

    def test_expired_offer_cannot_be_approved(self):
        self.fake.tables["acquisition_offer_versions"][1]["terms"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with self.as_account(NEW_INVESTOR):
            res = self.client.post(f"/me/investor/purchase-offers/{MY_OFFER}/decision",
                                   json={"version_number": 2, "decision": "approved", "reviewed_summary": True})
        self.assertEqual(res.status_code, 409)


class MyInspections(InvestorAcquisitionBase):

    def test_scoped_to_own_offers_and_deal(self):
        with self.as_account(EXISTING_INVESTOR):
            res = self.client.get("/me/investor/inspections")
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(sorted(b["id"] for b in data["bookings"]), ["b-1", "b-2"])  # b-2 via the offer's deal
        self.assertEqual([r["id"] for r in data["reports"]], [MY_REPORT])
        self.assertEqual([f["summary"] for f in data["reports"][0]["findings"]], ["Roof leak"])
        self.assertEqual([r["request"] for r in data["repairs"]], ["Fix roof"])
        self.assertEqual([d["offer_id"] for d in data["due_diligence"]], [MY_OFFER])
        self.assertNotIn("Their issue", res.text)

    def test_private_fields_stripped(self):
        with self.as_account(NEW_INVESTOR):
            data = self.client.get("/me/investor/inspections").json()
        booking = next(b for b in data["bookings"] if b["id"] == "b-1")
        self.assertNotIn("listing_agent_contact", booking)
        self.assertNotIn("created_by", booking)
        self.assertNotIn("storage_path", data["reports"][0])
        self.assertNotIn("contact", booking["inspector"])
        self.assertNotIn("requested_by", data["repairs"][0])

    def test_own_report_file(self):
        with self.as_account(NEW_INVESTOR), mock.patch.object(file_store, "get", return_value=b"%PDF-1.4 r") as get:
            res = self.client.get(f"/me/investor/inspections/reports/{MY_REPORT}/file")
        self.assertEqual(res.status_code, 200)
        get.assert_called_once_with("inspections/x/report.pdf")

    def test_other_investors_report_is_404(self):
        with self.as_account(NEW_INVESTOR), mock.patch.object(file_store, "get") as get:
            res = self.client.get(f"/me/investor/inspections/reports/{OTHER_REPORT}/file")
        self.assertEqual(res.status_code, 404)
        get.assert_not_called()

    def test_investor_without_offers(self):
        self.fake.tables["acquisition_offers"] = [o for o in self.fake.tables["acquisition_offers"] if o["investor_id"] != ME_INV]
        with self.as_account(NEW_INVESTOR):
            data = self.client.get("/me/investor/inspections").json()
        self.assertEqual(data["bookings"], [])
        self.assertFalse(data["has_offers"])


if __name__ == "__main__":
    unittest.main()
