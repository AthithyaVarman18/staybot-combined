"""
Database tests for supabase_tenancy.sql against a real (throwaway) Postgres.

Skipped unless TENANCY_TEST_PG is set to a psql connection string, e.g.
    TENANCY_TEST_PG="host=127.0.0.1 port=55432 user=postgres" \
    .venv/bin/python -m unittest tests.test_tenancy_sql -v

Each run creates a fresh database, loads the project's existing migrations,
inserts labelled test records (names start with "TEST"), applies the tenancy
migration twice, and drops the database at the end. Never point this at a
production database.
"""

import os
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DSN = os.getenv("TENANCY_TEST_PG")
PSQL = os.getenv("TENANCY_TEST_PSQL") or shutil.which("psql") or "/opt/homebrew/opt/postgresql@17/bin/psql"
BASE_MIGRATIONS = ["supabase_schema.sql", "supabase_properties.sql", "supabase_owner_listings.sql", "supabase_onboarding.sql"]

COMPLETE_DATA = (
    '{"resident_name": "%s", "phone": "%s", "move_in_date": "2026-10-01", "occupants": 2,'
    ' "rent": 35000, "deposit": 200000, "lease_start": "2026-10-01", "lease_end": "2027-09-30",'
    ' "documents": {"identity": "verified", "signed_lease": "verified"}, "lease_signed": true, "notes": ""}'
)


def run(dbname, sql, role=None, expect_error=False):
    """Run SQL with psql; return rows as lists of strings (or the error text)."""
    script = (f"set role {role};\n" if role else "") + sql
    proc = subprocess.run(
        [PSQL, f"{DSN} dbname={dbname}", "-v", "ON_ERROR_STOP=1", "-X", "-q", "-A", "-t", "-F", "|"],
        input=script, capture_output=True, text=True,
        env={**os.environ, "LC_ALL": "C", "PGOPTIONS": "-c client_min_messages=warning"},
    )
    if expect_error:
        if proc.returncode == 0:
            raise AssertionError(f"expected an error from: {sql}\noutput: {proc.stdout}")
        return proc.stderr
    if proc.returncode != 0:
        raise AssertionError(f"SQL failed: {proc.stderr}\n{sql}")
    return [line.split("|") for line in proc.stdout.strip().splitlines() if line.strip()]


@unittest.skipUnless(DSN, "set TENANCY_TEST_PG to run database tests")
class TenancyMigrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = "tenancy_test_" + uuid.uuid4().hex[:8]
        run("postgres", f"create database {cls.db}")
        run(cls.db, """
            do $$ begin
              if not exists (select from pg_roles where rolname = 'anon') then create role anon nologin; end if;
              if not exists (select from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
              if not exists (select from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
            end $$;
            grant usage on schema public to anon, authenticated, service_role;
            -- Supabase grants new tables to these roles by default; the migration must revoke.
            alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
            alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
        """)
        for name in BASE_MIGRATIONS:
            run(cls.db, (ROOT / name).read_text())

        # A lead, and an onboarding completed BEFORE the tenancy migration
        # (old completion function), to prove existing records are backfilled.
        cls.old_conv = cls.conversation("TEST pre-migration lead")
        cls.old_onb = run(cls.db, f"""
            insert into resident_onboardings (conversation_id, property_id, property_title, data)
            values ('{cls.old_conv}', 'adyar-2bhk', 'Cozy 2BHK by the river', '{{}}') returning id""")[0][0]
        run(cls.db, f"select id from save_resident_onboarding('{cls.old_onb}', 1,"
                    f" '{COMPLETE_DATA % ('TEST Old Resident', '+919000000001')}'::jsonb, true)")

        migration = (ROOT / "supabase_tenancy.sql").read_text()
        run(cls.db, migration)
        run(cls.db, migration)   # re-running must be safe

    @classmethod
    def tearDownClass(cls):
        run("postgres", f"drop database if exists {cls.db} with (force)")

    # ---- helpers ----

    @classmethod
    def conversation(cls, label):
        return run(cls.db, f"insert into conversations (session_id, listing_title) values ('{uuid.uuid4()}', '{label}') returning id")[0][0]

    def start(self, property_id, label):
        conv = self.conversation(label)
        return run(self.db, f"""
            insert into resident_onboardings (conversation_id, property_id, property_title, data)
            select '{conv}', id, title, '{{}}' from properties where id = '{property_id}' returning id""")[0][0]

    def complete(self, onb, version, name, phone, expect_error=False):
        return run(self.db, f"select id, status, version from save_resident_onboarding('{onb}', {version},"
                            f" '{COMPLETE_DATA % (name, phone)}'::jsonb, true)", expect_error=expect_error)

    def scalar(self, sql):
        return run(self.db, sql)[0][0]

    # ---- tests ----

    def test_01_existing_completed_onboarding_is_backfilled_once(self):
        rows = run(self.db, f"""
            select t.tenant_ref, t.full_name, tn.property_id, tn.status
            from tenancies tn join tenants t on t.id = tn.tenant_id where tn.onboarding_id = '{self.old_onb}'""")
        self.assertEqual(len(rows), 1, "one tenancy after running the migration twice")
        ref, name, prop, status = rows[0]
        self.assertRegex(ref, r"^TEN-\d{6}$")
        self.assertEqual((name, prop, status), ("TEST Old Resident", "adyar-2bhk", "active"))
        self.assertEqual(self.scalar(f"select count(*) from tenants where source_onboarding_id = '{self.old_onb}'"), "1")

    def test_02_draft_keeps_property_id_and_does_not_occupy(self):
        onb = self.start("velachery-1bhk", "TEST draft lead")
        self.assertEqual(self.scalar(f"select property_id from resident_onboardings where id = '{onb}'"), "velachery-1bhk")
        run(self.db, f"select id from save_resident_onboarding('{onb}', 1, '{{\"resident_name\": \"TEST Draft\"}}'::jsonb, false)")
        self.assertEqual(self.scalar("select status from properties where id = 'velachery-1bhk'"), "active")
        self.assertEqual(self.scalar(f"select count(*) from tenancies where onboarding_id = '{onb}'"), "0")
        self.assertEqual(self.scalar(f"select count(*) from tenants where source_onboarding_id = '{onb}'"), "0")
        # "Refresh": re-reading the details view still shows the same property, no tenant yet.
        prop, name, tenant = run(self.db, f"select property_id, property_name, coalesce(tenant_ref, '') from resident_onboarding_details where id = '{onb}'")[0]
        self.assertEqual((prop, name, tenant), ("velachery-1bhk", "Modern 1BHK, walk to metro", ""))

    def test_03_invalid_property_is_rejected(self):
        conv = self.conversation("TEST invalid property lead")
        err = run(self.db, f"insert into resident_onboardings (conversation_id, property_id, property_title, data)"
                           f" values ('{conv}', 'no-such-property', 'x', '{{}}')", expect_error=True)
        self.assertIn("foreign key", err)

    def test_04_property_assignment_cannot_be_changed(self):
        onb = self.start("porur-2bhk", "TEST change-property lead")
        err = run(self.db, f"update resident_onboardings set property_id = 'guindy-1bhk' where id = '{onb}'", expect_error=True)
        self.assertIn("cannot be changed", err)
        self.assertEqual(self.scalar(f"select property_id from resident_onboardings where id = '{onb}'"), "porur-2bhk")

    def test_05_completion_creates_one_linked_tenant_and_retries_are_safe(self):
        onb = self.start("garner-3br", "TEST completion lead")
        _, status, version = self.complete(onb, 1, "TEST Priya", "+919000000002")[0]
        self.assertEqual((status, version), ("completed", "2"))

        ref, tenant_name, prop, prop_ref, prop_name, prop_status, tenancy_status = run(self.db, f"""
            select tenant_ref, tenant_name, property_id, property_ref, property_name, property_status, tenancy_status
            from resident_onboarding_details where id = '{onb}'""")[0]
        self.assertRegex(ref, r"^TEN-\d{6}$")
        self.assertEqual((tenant_name, prop, prop_ref, prop_status, tenancy_status),
                         ("TEST Priya", "garner-3br", "NC-GAR-301", "let", "active"))
        self.assertEqual(prop_name, "Sunlit 3BR near downtown Garner")

        # Retry with the same stale version, and again with the new version: nothing new is created.
        for retry_version in (1, 2):
            _, status, version = self.complete(onb, retry_version, "TEST Priya", "+919000000002")[0]
            self.assertEqual((status, version), ("completed", "2"))
        self.assertEqual(self.scalar(f"select count(*) from tenants where source_onboarding_id = '{onb}'"), "1")
        self.assertEqual(self.scalar(f"select count(*) from tenancies where onboarding_id = '{onb}'"), "1")
        self.assertEqual(self.scalar(f"select tenant_ref from resident_onboarding_details where id = '{onb}'"), ref,
                         "tenant ID stays the same after retries/refresh")

        # A plain save on a completed record is still refused.
        err = run(self.db, f"select id from save_resident_onboarding('{onb}', 2, '{{}}'::jsonb, false)", expect_error=True)
        self.assertIn("already completed", err)

    def test_06_second_onboarding_cannot_complete_on_occupied_property(self):
        first = self.start("kilpauk-studio", "TEST first on studio")
        second = self.start("kilpauk-studio", "TEST second on studio")
        self.complete(first, 1, "TEST First", "+919000000003")
        err = self.complete(second, 1, "TEST Second", "+919000000004", expect_error=True)
        self.assertIn("Property unavailable", err)
        self.assertEqual(self.scalar(f"select count(*) from tenants where source_onboarding_id = '{second}'"), "0",
                         "failed completion rolls back: no tenant created")
        self.assertEqual(self.scalar(f"select status from resident_onboardings where id = '{second}'"), "in_progress")

    def test_07_new_tenant_on_same_property_preserves_history_and_never_merges_by_name(self):
        first = self.start("medavakkam-2bhk", "TEST history tenant 1")
        self.complete(first, 1, "TEST Same Name", "+919000000005")
        first_tenancy, first_tenant = run(self.db, f"select id, tenant_id from tenancies where onboarding_id = '{first}'")[0]

        # The team ends that tenancy and re-lists the home (outside this feature).
        run(self.db, f"update tenancies set status = 'ended', ended_at = now() where id = '{first_tenancy}';"
                     " update properties set status = 'active' where id = 'medavakkam-2bhk'")

        second = self.start("medavakkam-2bhk", "TEST history tenant 2")
        self.complete(second, 1, "TEST Same Name", "+919000000006")

        rows = run(self.db, """
            select t.tenant_ref, tn.status, tn.property_id
            from tenancies tn join tenants t on t.id = tn.tenant_id
            where tn.property_id = 'medavakkam-2bhk' order by tn.started_at""")
        self.assertEqual([r[1] for r in rows], ["ended", "active"], "old tenancy kept, new one active")
        self.assertEqual({r[2] for r in rows}, {"medavakkam-2bhk"}, "same existing property ID, no new property")
        self.assertNotEqual(rows[0][0], rows[1][0], "same name, still two separate tenants")
        self.assertEqual(self.scalar("select count(*) from properties where id like 'medavakkam%'"), "1")
        self.assertEqual(self.scalar(f"select status from tenancies where id = '{first_tenancy}'"), "ended")
        self.assertEqual(self.scalar(f"select count(*) from tenants where id = '{first_tenant}'"), "1")

    def test_08_only_one_active_tenancy_per_property_even_if_bypassing_the_function(self):
        onb = self.start("ecr-villa", "TEST direct insert")   # sale listing, never completable
        tenant = run(self.db, f"insert into tenants (full_name, source_onboarding_id) values ('TEST Direct', '{onb}') returning id")[0][0]
        other = self.start("annanagar-3bhk", "TEST direct insert 2")
        tenant2 = run(self.db, f"insert into tenants (full_name, source_onboarding_id) values ('TEST Direct 2', '{other}') returning id")[0][0]
        run(self.db, f"insert into tenancies (tenant_id, property_id, onboarding_id) values ('{tenant}', 'annanagar-3bhk', '{onb}')")
        err = run(self.db, f"insert into tenancies (tenant_id, property_id, onboarding_id) values ('{tenant2}', 'annanagar-3bhk', '{other}')",
                  expect_error=True)
        self.assertIn("tenancies_one_active_per_property", err)

    def test_09_row_security_hides_records_even_with_table_access(self):
        # Layer 1: even if a public role had table rights, RLS returns nothing.
        run(self.db, "grant select on resident_onboardings, tenants, tenancies to anon")
        try:
            for relation in ("resident_onboardings", "tenants", "tenancies"):
                with self.subTest(relation=relation):
                    self.assertEqual(run(self.db, f"select count(*) from {relation}", role="anon"), [["0"]])
        finally:
            run(self.db, "revoke all on resident_onboardings, tenants, tenancies from anon")

    def test_09b_public_roles_cannot_read_tenants_or_link_by_property_id(self):
        for role in ("anon", "authenticated"):
            for relation in ("tenants", "tenancies", "resident_onboarding_details", "resident_onboardings"):
                with self.subTest(role=role, relation=relation):
                    err = run(self.db, f"select * from {relation} where property_id = 'garner-3br'"
                              if relation != "tenants" else "select * from tenants", role=role, expect_error=True)
                    self.assertTrue("permission denied" in err or "does not exist" in err, err)
            err = run(self.db, "select * from save_resident_onboarding(gen_random_uuid(), 1, '{}'::jsonb, true)",
                      role=role, expect_error=True)
            self.assertIn("permission denied", err)

    def test_10_existing_search_and_other_records_untouched(self):
        # Properties not involved in tests are still active and searchable; nothing was deleted.
        self.assertEqual(self.scalar("select count(*) from properties"), "14")
        self.assertEqual(self.scalar("select status from properties where id = 'navalur-3bhk'"), "active")
        self.assertEqual(self.scalar(f"select status from resident_onboardings where id = '{self.old_onb}'"), "completed")


if __name__ == "__main__":
    unittest.main()
