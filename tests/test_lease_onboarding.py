"""
Full onboarding journey tests against a real (throwaway) PostgreSQL database.

The real Staybot app and the local Supabase stand-in (dev/local_supabase.py)
run as HTTP servers inside the test process; every request goes through the
real team-auth middleware, routes, database functions, constraints and
private file storage. WhatsApp stays in test mode unless a test switches a
flag on purpose (no network calls are made).

Skipped unless TENANCY_TEST_PG is set. Run:  bash tests/run_tenancy_db_tests.sh
"""

import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import requests

from tests.test_tenancy_sql import DSN, PSQL, ROOT

MIGRATIONS = ["supabase_schema.sql", "supabase_properties.sql", "supabase_owner_listings.sql", "supabase_viewings.sql",
              "supabase_ai_calls.sql", "supabase_lead_scoring.sql", "supabase_outcomes.sql", "supabase_onboarding.sql",
              "supabase_tenancy.sql", "supabase_lease_onboarding.sql"]
PASSWORD = "test-team-password"
KEY = "test-service-key"
PDF = b"%PDF-1.4\n% TEST document\n"
TERMS = {"lease_start": "2026-10-01", "lease_end": "2027-09-30", "move_in_date": "2026-10-02", "rent": "1850", "deposit": "1850.00",
         "currency": "USD", "payment_schedule": "monthly", "rent_due_day": 1,
         "charges": [{"label": "TEST trash service", "amount": "25", "frequency": "monthly"}],
         "occupants": ["TEST Tenant Tara"], "pets": "No pets", "utilities": "Tenant pays electricity",
         "additional_terms": ["No smoking inside the home."]}
PREFS = {"lease_setup": "digital_copy_whatsapp", "account_setup": "undecided"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def psql(dbname, sql):
    proc = subprocess.run([PSQL, f"{DSN} dbname={dbname}", "-X", "-q", "-v", "ON_ERROR_STOP=1"], input=sql, capture_output=True, text=True,
                          env={**os.environ, "LC_ALL": "C", "PGOPTIONS": "-c client_min_messages=warning"})
    if proc.returncode:
        raise AssertionError(proc.stderr)
    return proc.stdout


def serve(app, port):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return server
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


@unittest.skipUnless(DSN, "set TENANCY_TEST_PG to run database tests")
class LeaseOnboardingJourney(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dbname = "lease_onboarding_" + uuid.uuid4().hex[:8]
        psql("postgres", f"create database {cls.dbname}")
        psql(cls.dbname, """do $$ begin
            if not exists (select from pg_roles where rolname = 'anon') then create role anon nologin; end if;
            if not exists (select from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
            if not exists (select from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
          end $$;""")
        for name in MIGRATIONS:
            psql(cls.dbname, (ROOT / name).read_text())
        psql(cls.dbname, (ROOT / "supabase_lease_onboarding.sql").read_text())   # re-running is safe

        cls.files = Path(tempfile.mkdtemp(prefix="staybot-files-"))
        from dev import local_supabase
        from src import main
        from src.services import db, onboarding_whatsapp, team_auth, whatsapp
        cls.whatsapp, cls.onboarding_whatsapp = whatsapp, onboarding_whatsapp
        api_port, app_port = free_port(), free_port()
        cls.base = f"http://127.0.0.1:{app_port}"
        cls.rest = f"http://127.0.0.1:{api_port}/rest/v1"
        cls.patches = [
            patch.object(local_supabase, "DSN", f"{DSN} dbname={cls.dbname}"),
            patch.object(local_supabase, "KEY", KEY),
            patch.object(local_supabase, "FILES", cls.files),
            patch.object(db, "ENABLED", True),
            patch.object(db, "REST_URL", cls.rest),
            patch.object(db, "HEADERS", {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}),
            patch.object(team_auth, "ADMIN_PASSWORD", PASSWORD),
            patch.object(whatsapp, "DRY_RUN", True),
            patch.object(whatsapp, "APP_SECRET", ""),
            patch.dict(os.environ, {"PUBLIC_BASE_URL": cls.base}),
        ]
        for p in cls.patches:
            p.start()
        cls.servers = [serve(local_supabase.app, api_port), serve(main.app, app_port)]
        cls.staff = requests.Session()
        cls.staff.auth = ("team", PASSWORD)
        cls.staff.headers["X-Staybot-Staff"] = "TEST Staff Sam"
        cls.staff_row = cls.call("POST", "/onboarding/cases/staff", json={"name": "TEST Staff Sam"})

    @classmethod
    def tearDownClass(cls):
        for server in cls.servers:
            server.should_exit = True
        time.sleep(0.3)
        for p in reversed(cls.patches):
            p.stop()
        shutil.rmtree(cls.files, ignore_errors=True)
        psql("postgres", f"drop database if exists {cls.dbname} with (force)")

    # ------------------------------------------------------------------ helpers
    @classmethod
    def call(cls, method, path, expect=200, session=None, **kw):
        r = (session or cls.staff).request(method, path if path.startswith("http") else cls.base + path, timeout=30, **kw)
        if expect is not None and r.status_code != expect:
            raise AssertionError(f"{method} {path} -> {r.status_code} (expected {expect}): {r.text[:500]}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r

    def db(self, table, **filters):
        params = {"select": "*", **{k: f"eq.{v}" for k, v in filters.items()}}
        return requests.get(f"{self.rest}/{table}", params=params, headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"}).json()

    def person(self, label):
        number = f"+1704555{uuid.uuid4().int % 10000:04d}"
        return self.call("POST", "/onboarding/cases/people", json={"full_name": f"TEST {label}", "whatsapp": number, "is_test": True})

    def new_case(self, *, terms=TERMS, with_people=True, conversation_id=None):
        tag = uuid.uuid4().hex[:6]
        tenant = self.person("Tenant Tara") if with_people else None
        owner = self.person("Owner Omar") if with_people else None
        body = {"new_property": {"id": f"test-home-{tag}", "title": f"TEST Home {tag}", "city": "Raleigh"}, "unit": "1A",
                "property_address": f"{tag} Test Street, Unit 1A, Raleigh, NC 27601", "staff_id": self.staff_row["id"], "is_test": True,
                "tenant_id": tenant["id"] if tenant else None, "owner_id": owner["id"] if owner else None,
                "conversation_id": conversation_id}
        case = self.call("POST", "/onboarding/cases", json=body)
        if with_people:
            for step in (2, 3):
                case = self.save(case, step, consent={"whatsapp_opt_in": True, "source": "customer_messaged_first"})
            if terms:
                case = self.save(case, 5, terms=terms, preferences=PREFS)
        return case

    def save(self, case, step, expect=200, advance=False, **body):
        return self.call("PUT", f"/onboarding/cases/{case['id']}/steps/{step}", expect=expect,
                         json={"version": case["version"], "advance": advance, **body})

    def link(self, case, party):
        return self.call("POST", f"/onboarding/cases/{case['id']}/invite", json={"party": party})["link"]

    def upload_all(self, case, links):
        for party, key in (("tenant", "photo_id"), ("tenant", "proof_of_income"), ("owner", "ownership_authority")):
            self.call("POST", f"{links[party]}/api/documents/{key}", data=PDF, headers={"Content-Type": "application/pdf", "X-File-Name": f"{key}.pdf"},
                      session=requests.Session())

    def accept_all(self, case):
        view = self.call("GET", f"/onboarding/cases/{case['id']}")
        for d in view["documents"]:
            if d["status"] == "awaiting_review":
                self.call("POST", f"/onboarding/cases/{case['id']}/documents/{d['latest']['id']}/review", json={"decision": "accepted"})

    def approve(self, link, decision="approved", expect=200, version_id=None, note=""):
        public = requests.Session()
        state = self.call("GET", f"{link}/api/state", session=public)
        return self.call("POST", f"{link}/api/decision", expect=expect, session=public,
                         json={"version_id": version_id or state["agreement"]["version_id"], "decision": decision, "reviewed_agreement": True, "note": note})

    def ready_case(self):
        """Documents accepted, agreement prepared, both links issued. No approvals yet."""
        case = self.new_case()
        links = {p: self.link(case, p) for p in ("tenant", "owner")}
        self.upload_all(case, links)
        self.accept_all(case)
        self.call("POST", f"/onboarding/cases/{case['id']}/agreement")
        return self.call("GET", f"/onboarding/cases/{case['id']}"), links

    def staff_review(self, case):
        view = self.call("GET", f"/onboarding/cases/{case['id']}")
        return self.call("POST", f"/onboarding/cases/{case['id']}/staff-review", json={"agreement_version": view["agreement"]["latest_version"]["version"]})

    def finalized_case(self):
        case, links = self.ready_case()
        self.approve(links["tenant"])
        self.approve(links["owner"])
        self.staff_review(case)
        result = self.call("POST", f"/onboarding/cases/{case['id']}/finalize")
        return result["case"], links

    # ------------------------------------------------------------------ tests
    def test_01_staff_link_tenant_owner_property_and_prevent_duplicates(self):
        case = self.new_case()
        self.assertTrue(case["reference"].startswith("OB-"))
        self.assertEqual((case["tenant"]["full_name"], case["owner"]["full_name"], case["staff"]["name"]),
                         ("TEST Tenant Tara", "TEST Owner Omar", "TEST Staff Sam"))
        self.assertEqual(case["unit"], "1A")
        self.assertEqual(case["steps"][0]["missing"], [])
        again = self.call("POST", "/onboarding/cases", expect=409, json={"property_id": case["property_id"], "unit": "1A", "staff_id": self.staff_row["id"]})
        self.assertIn(case["reference"], again["detail"])
        # The same WhatsApp number reuses the existing person record.
        reused = self.call("POST", "/onboarding/cases/people", json={"full_name": "Someone Else", "whatsapp": case["tenant"]["whatsapp"]})
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["id"], case["tenant"]["id"])
        self.call("POST", "/onboarding/cases", expect=400, json={"property_id": "TEST-no-such-home", "staff_id": self.staff_row["id"]})

    def test_02_works_for_lead_without_chat_history(self):
        conversation = requests.post(f"{self.rest}/conversations", headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
                                     json={"session_id": "wa:+17045550199", "listing_title": "TEST lead with no messages"}).json()[0]
        self.assertEqual(self.db("messages", conversation_id=conversation["id"]), [])
        case = self.new_case(conversation_id=conversation["id"])
        self.assertEqual(case["conversation_id"], conversation["id"])

    def test_03_refresh_and_another_browser_resume_from_database(self):
        case = self.new_case(terms=None)
        partial = {**TERMS, "rent": "2000", "move_in_date": None}
        saved = self.save(case, 5, terms=partial, preferences=PREFS)
        other_browser = requests.Session()
        other_browser.auth = ("team", PASSWORD)
        resumed = self.call("GET", f"/onboarding/cases/{case['id']}", session=other_browser)
        self.assertEqual(resumed["terms"]["rent"], "2000.00")
        self.assertIsNone(resumed["terms"]["move_in_date"])
        self.assertEqual(resumed["version"], saved["version"])
        self.assertIn("move-in date", resumed["steps"][4]["missing"])
        continued = self.call("PUT", f"/onboarding/cases/{case['id']}/steps/5", session=other_browser,
                              json={"version": resumed["version"], "terms": {**partial, "move_in_date": "2026-10-01"}})
        self.assertEqual(continued["steps"][4]["missing"], [])
        # The first browser's form is now stale and cannot overwrite the newer save.
        self.save(saved, 5, expect=409, terms=TERMS)

    def test_04_missing_required_fields_block_progression(self):
        case = self.new_case(with_people=False)
        blocked = self.save(case, 2, expect=400, advance=True)
        self.assertIn("tenant not assigned", blocked["detail"])
        case = self.call("GET", f"/onboarding/cases/{case['id']}")
        tenant = self.person("Tenant No Consent")
        case = self.save(case, 2, person_id=tenant["id"])
        self.assertIn("WhatsApp consent", self.save(case, 2, expect=400, advance=True)["detail"])
        case = self.new_case(terms=None)
        self.assertIn("monthly rent", self.save(case, 5, expect=400, advance=True, terms={**TERMS, "rent": None})["detail"])
        bad = self.save(case, 5, expect=422, terms={**TERMS, "lease_end": "2026-01-01"})
        self.assertIn("Lease end must be after lease start", json.dumps(bad))
        self.call("POST", f"/onboarding/cases/{case['id']}/agreement", expect=400)

    def test_05_upload_alone_is_not_verification(self):
        case = self.new_case()
        links = {p: self.link(case, p) for p in ("tenant", "owner")}
        self.upload_all(case, links)
        view = self.call("GET", f"/onboarding/cases/{case['id']}")
        self.assertEqual({d["status"] for d in view["documents"]}, {"awaiting_review"})
        self.assertTrue(any("awaiting staff review" in m for m in view["steps"][3]["missing"]))
        # Even a direct database write cannot mark a document accepted without a reviewer.
        doc_id = view["documents"][0]["latest"]["id"]
        r = requests.patch(f"{self.rest}/onboarding_case_documents", params={"id": f"eq.{doc_id}"}, json={"status": "accepted"},
                           headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"})
        self.assertEqual(r.status_code, 400)
        # Wrong content type and a tenant uploading the owner's document are rejected.
        self.call("POST", f"{links['tenant']}/api/documents/photo_id", expect=415, session=requests.Session(),
                  data=b"not a pdf", headers={"Content-Type": "application/pdf"})
        self.call("POST", f"{links['tenant']}/api/documents/ownership_authority", expect=400, session=requests.Session(),
                  data=PDF, headers={"Content-Type": "application/pdf"})
        unnamed = requests.Session(); unnamed.auth = ("team", PASSWORD)
        self.call("POST", f"/onboarding/cases/{case['id']}/documents/{doc_id}/review", expect=400, session=unnamed, json={"decision": "accepted"})

    def test_06_single_approvals_cannot_finalize(self):
        case, links = self.ready_case()
        self.staff_review(case)
        self.approve(links["tenant"])
        detail = self.call("POST", f"/onboarding/cases/{case['id']}/finalize", expect=409)["detail"]
        self.assertIn("owner approval", detail)
        case2, links2 = self.ready_case()
        self.staff_review(case2)
        self.approve(links2["owner"])
        self.assertIn("tenant approval", self.call("POST", f"/onboarding/cases/{case2['id']}/finalize", expect=409)["detail"])
        self.assertEqual(self.db("onboarding_final_documents", case_id=case["id"]), [])
        # The database function enforces the same rule if the API checks were bypassed.
        row = self.db("onboarding_cases", id=case["id"])[0]
        version = self.db("onboarding_agreement_versions", case_id=case["id"])[0]
        r = requests.post(f"{self.rest}/rpc/onboarding_finalize_case", headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
                          json={"p_case_id": case["id"], "p_expected_version": row["version"], "p_version_id": version["id"],
                                "p_storage_path": f"bypass-{uuid.uuid4()}", "p_sha256": "x", "p_size": 1, "p_actor": "test"})
        self.assertIn("OWNER_APPROVAL_MISSING", r.text)

    def test_07_editing_terms_creates_new_version_and_invalidates_approvals(self):
        case, links = self.ready_case()
        v1 = case["agreement"]["latest_version"]
        self.approve(links["tenant"])
        self.approve(links["owner"])
        self.staff_review(case)
        case = self.call("GET", f"/onboarding/cases/{case['id']}")
        changed = self.save(case, 5, terms={**TERMS, "rent": "1900"}, preferences=PREFS)
        v2 = changed["agreement"]["latest_version"]
        self.assertEqual(v2["version"], v1["version"] + 1)
        self.assertEqual(changed["agreement"]["approvals"]["tenant"]["status"], "pending")
        self.assertEqual(changed["agreement"]["approvals"]["owner"]["status"], "pending")
        self.assertFalse(changed["agreement"]["staff_reviewed"])
        # An approval of the old version is rejected; history is preserved.
        self.approve(links["tenant"], version_id=v1["id"], expect=409)
        self.assertEqual(len(self.db("onboarding_approvals", agreement_version_id=v1["id"])), 2)
        # Both must approve the same (current) version.
        self.approve(links["owner"])
        self.staff_review(changed)
        self.assertIn("tenant approval", self.call("POST", f"/onboarding/cases/{case['id']}/finalize", expect=409)["detail"])
        self.approve(links["tenant"])
        self.assertTrue(self.call("POST", f"/onboarding/cases/{case['id']}/finalize")["created"])
        self.assertEqual(len(self.db("onboarding_agreement_versions", case_id=case["id"])), 2)

    def test_08_expired_revoked_and_wrong_person_links_are_rejected(self):
        case, links = self.ready_case()
        public = requests.Session()
        self.call("GET", f"{self.base}/p/{'x' * 43}/api/state", expect=404, session=public)
        # A new invitation revokes the previous link.
        old = links["tenant"]
        new = self.link(case, "tenant")
        self.call("GET", f"{old}/api/state", expect=410, session=public)
        self.call("GET", f"{new}/api/state", session=public)
        # Expired link.
        token = new.rsplit("/", 1)[1]
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        requests.patch(f"{self.rest}/onboarding_access_links", params={"token_hash": f"eq.{token_hash}"},
                       json={"expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
                       headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"})
        self.call("POST", f"{new}/api/decision", expect=410, session=public,
                  json={"version_id": case["agreement"]["latest_version"]["id"], "decision": "approved", "reviewed_agreement": True})
        # Person changed on the case: the owner's existing link no longer works, even before revocation.
        other = self.person("Replacement Owner")
        requests.patch(f"{self.rest}/onboarding_access_links", params={"case_id": f"eq.{case['id']}", "party": "eq.owner"},
                       json={"revoked_at": None}, headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"})
        requests.post(f"{self.rest}/rpc/onboarding_update_case", headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
                      json={"p_case_id": case["id"], "p_expected_version": None, "p_changes": {"owner_id": other["id"]}, "p_actor": "test", "p_action": "test"})
        self.call("GET", f"{links['owner']}/api/state", expect=403, session=public)
        self.assertEqual(self.db("onboarding_approvals", case_id=case["id"]), [])

    def test_09_unauthorized_users_cannot_access_cases_or_documents(self):
        case, _ = self.finalized_case()
        anonymous = requests.Session()
        wrong = requests.Session(); wrong.auth = ("team", "wrong-password")
        doc = case["documents"][0]["latest"]["id"]
        for path in ("/onboarding/cases", f"/onboarding/cases/{case['id']}", f"/onboarding/cases/{case['id']}/final.pdf",
                     f"/onboarding/cases/{case['id']}/documents/{doc}/file", f"/onboarding/cases/{case['id']}/agreement/preview.pdf",
                     f"/onboarding/cases/{case['id']}/audit"):
            self.call("GET", path, expect=401, session=anonymous)
            self.call("GET", path, expect=401, session=wrong)
        self.call("POST", f"/onboarding/cases/{case['id']}/send-final", expect=401, session=anonymous)
        # Knowing a case or property ID does not open the public pages.
        self.call("GET", f"{self.base}/p/{case['id']}/api/state", expect=404, session=anonymous)
        self.call("GET", f"{self.base}/p/{case['property_id']}/api/agreement.pdf", expect=404, session=anonymous)

    def test_10_final_pdf_matches_the_approved_version_and_downloads(self):
        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext (poppler) not installed")
        case, links = self.finalized_case()
        self.assertEqual(case["status"], "finalized")
        final = self.db("onboarding_final_documents", case_id=case["id"])[0]
        version = self.db("onboarding_agreement_versions", id=final["agreement_version_id"])[0]
        response = self.call("GET", f"/onboarding/cases/{case['id']}/final.pdf")
        data = response.content
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertEqual(hashlib.sha256(data).hexdigest(), final["sha256"])
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(data); f.flush()
            info = subprocess.run(["pdfinfo", f.name], capture_output=True, text=True)
            self.assertEqual(info.returncode, 0, info.stderr)
            text = " ".join(subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True).stdout.split())
        snap = version["snapshot"]
        for expected in (snap["tenant"]["full_name"], snap["owner"]["full_name"], snap["property"]["address"], "USD 1,850.00",
                         "October 1, 2026", "September 30, 2027", "October 2, 2026", "TEST trash service", "No smoking inside the home.",
                         f"Version {version['version']}", "Final", case["reference"], version["content_hash"][:40], "DEMO TEMPLATE",
                         "approved agreement version", "An approval is not a signature", "No electronic signatures were collected"):
            self.assertIn(expected, text)
        self.assertNotIn("DRAFT - NOT FOR SIGNATURE", text)
        # The tenant downloads the same stored file through their link.
        self.assertEqual(self.call("GET", f"{links['tenant']}/api/agreement.pdf", session=requests.Session()).content, data)

    def test_11_both_recipients_get_same_pdf_statuses_tracked_separately_and_retry_is_safe(self):
        case, _ = self.finalized_case()
        outbox_before = len(self.whatsapp.OUTBOX)
        sent = self.call("POST", f"/onboarding/cases/{case['id']}/send-final")
        self.assertEqual({m["status"] for m in sent["results"].values()}, {"accepted"})
        self.assertTrue(all(m["test_mode"] for m in sent["results"].values()))
        rows = [m for m in self.db("onboarding_whatsapp_messages", case_id=case["id"]) if m["kind"] == "final_pdf"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["final_document_id"] for r in rows}), 1)
        pdf_notes = [m for m in list(self.whatsapp.OUTBOX)[outbox_before:] if m.get("kind") == "final_lease_pdf"]
        self.assertEqual(len({m["final_document_id"] for m in pdf_notes}), 1)
        self.assertEqual(len(pdf_notes), 2)
        # Repeating the send request does not send again.
        again = self.call("POST", f"/onboarding/cases/{case['id']}/send-final")
        self.assertEqual([m["attempts"] for m in again["results"].values()], [1, 1])
        tenant = next(r for r in rows if r["party"] == "tenant")
        owner = next(r for r in rows if r["party"] == "owner")
        # Status events: tenant delivered, owner failed. A duplicate webhook is applied once.
        payload = self.onboarding_whatsapp.simulated_status_payload(tenant["provider_message_id"], "delivered", tenant["to_phone"])
        for _ in range(2):
            self.call("POST", "/webhook/whatsapp", json=payload, session=requests.Session())
        time.sleep(0.5)
        self.assertEqual(len(self.db("onboarding_whatsapp_status_events", provider_message_id=tenant["provider_message_id"])), 1)
        self.call("POST", f"/onboarding/cases/{case['id']}/test/simulate-status", json={"message_id": owner["id"], "status": "failed"})
        late_sent = self.onboarding_whatsapp.simulated_status_payload(tenant["provider_message_id"], "sent", tenant["to_phone"])
        late_sent["entry"][0]["changes"][0]["value"]["statuses"][0]["timestamp"] = "1"
        self.call("POST", "/webhook/whatsapp", json=late_sent, session=requests.Session())
        time.sleep(0.5)
        view = self.call("GET", f"/onboarding/cases/{case['id']}")
        statuses = {m["party"]: m for m in view["whatsapp"] if m["kind"] == "final_pdf"}
        self.assertEqual(statuses["tenant"]["status"], "delivered")      # late 'sent' did not move it backwards
        self.assertEqual(statuses["owner"]["status"], "failed")
        self.assertIn("retry", view["next_action"].lower())
        # Only the failed message can be retried, once per failure.
        self.call("POST", f"/onboarding/cases/{case['id']}/messages/{statuses['tenant']['id']}/retry", expect=409)
        retried = self.call("POST", f"/onboarding/cases/{case['id']}/messages/{owner['id']}/retry")
        self.assertEqual((retried["result"]["status"], retried["result"]["attempts"]), ("accepted", 2))
        self.call("POST", f"/onboarding/cases/{case['id']}/messages/{owner['id']}/retry", expect=409)

    def test_12_repeated_requests_do_not_repeat_actions(self):
        case, links = self.ready_case()
        self.approve(links["tenant"])
        self.approve(links["tenant"])
        version_id = case["agreement"]["latest_version"]["id"]
        self.assertEqual(len([a for a in self.db("onboarding_approvals", agreement_version_id=version_id) if a["party"] == "tenant"]), 1)
        self.approve(links["owner"])
        self.staff_review(case)
        first = self.call("POST", f"/onboarding/cases/{case['id']}/finalize")
        second = self.call("POST", f"/onboarding/cases/{case['id']}/finalize")
        self.assertEqual((first["created"], second["created"]), (True, False))
        self.assertEqual(len(self.db("onboarding_final_documents", case_id=case["id"])), 1)
        self.assertEqual(len(self.db("tenants", case_id=case["id"])), 1)
        self.assertEqual(self.db("properties", id=case["property_id"])[0]["status"], "let")
        prep = self.call("POST", f"/onboarding/cases/{case['id']}/agreement", expect=409)
        self.assertIn("no longer active", prep["detail"])
        # History is append-only.
        audit_row = self.db("onboarding_audit_log", case_id=case["id"])[0]
        r = requests.delete(f"{self.rest}/onboarding_audit_log", params={"id": f"eq.{audit_row['id']}"}, headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"})
        self.assertEqual(r.status_code, 400)
        actions = {a["action"] for a in self.db("onboarding_audit_log", case_id=case["id"])}
        for expected in ("case_started", "document_uploaded", "document_accepted", "agreement_version_created", "agreement_approved",
                         "staff_review_recorded", "case_finalized", "access_link_issued", "whatsapp_invite_accepted"):
            self.assertIn(expected, actions)

    def test_13_missing_credentials_are_reported_honestly(self):
        setup = self.call("GET", "/onboarding/cases/setup")
        self.assertEqual(setup["whatsapp"]["mode"], "test")
        self.assertEqual(setup["turbotenant"]["status"], "not_connected")
        self.assertEqual(setup["turbotenant"]["link_generation"], "not_implemented")
        self.assertFalse(setup["templates"]["approved_available"])
        self.assertIn("Template setup required", setup["templates"]["message"])
        # Live mode without templates or an open 24-hour window: honest failure, no fake success, no network call.
        case = self.new_case()
        real_case = {**self.db("onboarding_cases", id=case["id"])[0], "is_test": False}
        with patch.object(self.whatsapp, "DRY_RUN", False), patch.object(self.onboarding_whatsapp, "TEMPLATE_INVITE", ""), \
                patch.object(self.onboarding_whatsapp, "graph_post", side_effect=AssertionError("no network call expected")), \
                patch.object(self.onboarding_whatsapp, "window_open", return_value=False):
            requests.patch(f"{self.rest}/onboarding_cases", params={"id": f"eq.{case['id']}"}, json={"is_test": False},
                           headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"})
            result = self.call("POST", f"/onboarding/cases/{case['id']}/invite", json={"party": "tenant"})
            requests.patch(f"{self.rest}/onboarding_cases", params={"id": f"eq.{case['id']}"}, json={"is_test": True},
                           headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"})
        self.assertFalse(real_case["is_test"])
        self.assertEqual(result["message"]["status"], "failed")
        self.assertFalse(result["message"]["test_mode"])
        self.assertIn("template", result["message"]["error"])
        self.assertIsNone(result["link"])
        # Live webhooks without an app secret are refused (cannot be verified); a bad signature is refused.
        with patch.object(self.whatsapp, "DRY_RUN", False):
            self.call("POST", "/webhook/whatsapp", expect=403, json={"entry": []}, session=requests.Session())
        with patch.object(self.whatsapp, "APP_SECRET", "secret"):
            body = json.dumps({"entry": []}).encode()
            self.call("POST", "/webhook/whatsapp", expect=403, data=body, session=requests.Session(),
                      headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bad"})
            good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
            self.call("POST", "/webhook/whatsapp", data=body, session=requests.Session(),
                      headers={"Content-Type": "application/json", "X-Hub-Signature-256": good})
        # A signing requirement without a provider blocks finalization.
        ready, links = self.ready_case()
        ready = self.save(ready, 5, terms=TERMS, preferences=PREFS, signature_method="external_esign")
        self.approve(links["tenant"]); self.approve(links["owner"]); self.staff_review(ready)
        self.assertIn("electronic signatures", self.call("POST", f"/onboarding/cases/{ready['id']}/finalize", expect=409)["detail"])

    def test_14_whatsapp_self_service_asks_once_and_escalates(self):
        case = self.new_case()
        self.link(case, "tenant")
        phone = case["tenant"]["whatsapp"]

        def say(text):
            r = self.call("POST", "/whatsapp/simulate", json={"phone": phone, "text": text, "message_id": f"wamid.TEST{uuid.uuid4().hex}"})
            self.assertTrue(r.get("onboarding"), r)
            return r["reply"]

        self.assertIn("TENANT", say("Hi"))
        self.assertIn("full legal name", say("1"))
        self.assertIn("email", say("TEST Tara Updated"))
        self.assertIn("language", say("skip"))
        self.assertIn("keep in touch", say("1"))
        done = say("2")
        self.assertIn("All your details are confirmed", done)
        self.assertIn("Documents still needed", done)
        self.assertNotIn("full legal name", say("hello again"))          # confirmed details are not asked again
        self.assertEqual(self.db("onboarding_people", id=case["tenant"]["id"])[0]["full_name"], "TEST Tara Updated")
        self.assertIn("passed this to our team", say("I want a lawyer to check this lease"))
        view = self.call("GET", f"/onboarding/cases/{case['id']}")
        escalation = view["open_escalations"][0]
        self.assertEqual(escalation["category"], "legal")
        for field in ("role", "advisor_type", "urgency", "understood", "context", "next_action", "follow_up", "escalation_reason", "summary", "confidence"):
            self.assertTrue(escalation["handoff"][field], field)
        self.assertIn("new private link", say("LINK"))
        metrics = self.call("GET", "/onboarding/cases/metrics")
        self.assertTrue(metrics["test_cases_excluded"])
        self.assertEqual(metrics["target_percent"], 80)

    def test_15_approved_template_configuration_and_turbotenant_manual_handoff(self):
        self.call("POST", "/onboarding/cases/config/templates", expect=400,
                  json={"name": "TEST NC lease", "approved_by": "TEST Attorney", "clauses_text": "## Rent\nThe tenant pays rent.", "approval_confirmed": False})
        template = self.call("POST", "/onboarding/cases/config/templates",
                             json={"name": "TEST NC lease", "approved_by": "TEST Attorney", "approval_confirmed": True,
                                   "clauses_text": "## Rent\nThe tenant pays the rent shown.\n\n## Deposit\nThe deposit is held as the law requires."})
        self.assertEqual((template["status"], len(template["clauses"])), ("approved", 2))
        case = self.new_case()
        self.assertEqual(case["template"]["id"], template["id"])      # new cases pick the approved template
        self.call("POST", f"/onboarding/cases/{case['id']}/turbotenant", expect=400, json={"link": "https://evil.example.com/x"})
        recorded = self.call("POST", f"/onboarding/cases/{case['id']}/turbotenant", json={"link": "https://app.turbotenant.com/some/path", "note": "TEST"})
        self.assertEqual(recorded["turbotenant"]["status"], "manual_handoff_recorded")
        self.assertFalse(recorded["turbotenant"]["synced_by_staybot"])
        requests.patch(f"{self.rest}/lease_templates", params={"id": f"eq.{template['id']}"}, json={"status": "retired"},
                       headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"})

    def test_16_existing_chat_leads_viewings_and_legacy_onboarding_still_work(self):
        for path in ("/ui", "/ui/onboarding.js", "/health", "/properties", "/leads", "/viewings", "/onboarding", "/whatsapp/status"):
            self.call("GET", path)
        page = self.call("GET", "/ui").text
        for tab in ('data-view="chat"', 'data-view="leads"', 'data-view="viewings"', 'data-view="onboarding"', "/ui/onboarding.js"):
            self.assertIn(tab, page)


if __name__ == "__main__":
    unittest.main()
