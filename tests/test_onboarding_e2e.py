"""
End-to-end onboarding test: the real API code in src/services/onboarding.py
running against a real (throwaway) Postgres with every migration applied.

Only the HTTP hop to Supabase is replaced: db._get / db._post are answered by
psql with the same PostgREST semantics the app relies on (eq filters, select,
order, insert returning the row, RPC calls). Skipped unless TENANCY_TEST_PG is
set; run it with:   bash tests/run_tenancy_db_tests.sh
"""

import json
import os
import subprocess
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import requests
from fastapi import HTTPException

from src.services import db, onboarding as ob
from tests.test_tenancy_sql import BASE_MIGRATIONS, DSN, PSQL, ROOT

COMPLETE = dict(
    resident_name='TEST E2E Resident', phone='+17045550101', move_in_date='2026-10-01', occupants=2,
    rent=1850, deposit=1850, lease_start='2026-10-01', lease_end='2027-09-30',
    documents={'identity': 'verified', 'signed_lease': 'verified'}, lease_signed=True, notes='TEST record',
)


def lit(value):
    return "'" + str(value).replace("'", "''") + "'"


class FakePostgrest:
    def __init__(self, dbname):
        self.dbname = dbname

    def sql(self, statement, parse=True):
        proc = subprocess.run(
            [PSQL, f'{DSN} dbname={self.dbname}', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1'],
            input=statement, capture_output=True, text=True,
            env={**os.environ, 'LC_ALL': 'C', 'PGOPTIONS': '-c client_min_messages=warning'})
        if proc.returncode:
            status = 404 if 'does not exist' in proc.stderr and 'relation' in proc.stderr else 400
            raise requests.HTTPError(proc.stderr, response=SimpleNamespace(status_code=status, text=proc.stderr))
        out = proc.stdout.strip()
        return json.loads(out) if parse and out else None

    def get(self, path, params=None):
        params = dict(params or {})
        columns = params.pop('select', '*')
        order = params.pop('order', None)
        params.pop('limit', None)
        where = ' and '.join(f'{k}::text = {lit(v[3:])}' for k, v in params.items() if v.startswith('eq.'))
        query = f'select {columns} from {path}' + (f' where {where}' if where else '')
        if order:
            column, _, direction = order.partition('.')
            query += f' order by {column} {direction or "asc"}'
        return self.sql(f"select coalesce(json_agg(t), '[]') from ({query}) t")

    def post(self, path, body, params=None):
        if path.startswith('rpc/'):
            args = ', '.join(f'{k} => {lit(json.dumps(v) if isinstance(v, (dict, list)) else str(v).lower() if isinstance(v, bool) else v)}'
                             for k, v in body.items())
            rows = self.sql(f"select coalesce(json_agg(t), '[]') from {path[4:]}({args}) t")
        else:
            cols = ', '.join(body)
            rows = self.sql(f"with r as (insert into {path} ({cols}) select {cols} from json_populate_record(null::{path}, {lit(json.dumps(body))}) returning *) "
                            f"select coalesce(json_agg(r), '[]') from r")
        return rows[0] if rows else rows


@unittest.skipUnless(DSN, 'set TENANCY_TEST_PG to run database tests')
class OnboardingEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dbname = 'onboarding_e2e_' + uuid.uuid4().hex[:8]
        admin = FakePostgrest('postgres')
        admin.sql(f'create database {cls.dbname}')
        cls.pg = FakePostgrest(cls.dbname)
        cls.pg.sql("""do $$ begin
            if not exists (select from pg_roles where rolname = 'anon') then create role anon nologin; end if;
            if not exists (select from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
            if not exists (select from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
          end $$;""")
        for name in BASE_MIGRATIONS + ['supabase_tenancy.sql']:
            cls.pg.sql((ROOT / name).read_text(), parse=False)
        cls.patches = [
            patch.object(db, 'ENABLED', True),
            patch.object(db, '_get', cls.pg.get),
            patch.object(db, '_post', cls.pg.post),
            patch.object(ob.properties, 'clear_cache', lambda: None),
        ]
        for p in cls.patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        FakePostgrest('postgres').sql(f'drop database if exists {cls.dbname} with (force)')

    def lead(self, label):
        return self.pg.post('conversations', {'session_id': f'wa:+1704555{uuid.uuid4().int % 10000:04d}', 'listing_title': label})['id']

    def start(self, conversation_id, property_id):
        return ob.start(ob.Start(conversation_id=conversation_id, property_id=property_id))

    def save(self, record, complete=False, **changes):
        return ob.save(uuid.UUID(record['id']), ob.Save(**{**COMPLETE, **changes, 'version': record['version'], 'complete': complete}))

    def expect(self, status, fn, *args, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.status_code, status, caught.exception.detail)
        return caught.exception.detail

    def test_full_flow_start_draft_refresh_complete_retry(self):
        lead = self.lead('TEST e2e full flow')
        record = self.start(lead, 'garner-3br')
        self.assertEqual(record['property'], {'id': 'garner-3br', 'ref': 'NC-GAR-301', 'name': 'Sunlit 3BR near downtown Garner'})
        self.assertIsNone(record['tenant'])
        self.assertTrue(record['tenant_linking_ready'])
        self.assertEqual(record['data']['phone'], record['data']['phone'].strip())

        # Starting again (double click) returns the same record, no second row.
        self.assertEqual(self.start(lead, 'garner-3br')['id'], record['id'])
        # Asking for another property is refused, not silently reassigned.
        self.assertIn('cannot be changed', self.expect(409, self.start, lead, 'adyar-2bhk'))

        draft = self.save(record, notes='TEST draft', lease_signed=False)
        self.assertEqual(draft['status'], 'in_progress')
        self.assertEqual(draft['property']['id'], 'garner-3br')
        self.assertEqual(self.pg.get('properties', {'id': 'eq.garner-3br'})[0]['status'], 'active', 'draft must not occupy')

        # Stale form (old version) cannot overwrite a newer save.
        self.assertIn('changed elsewhere', self.expect(409, self.save, record))
        # Missing items block completion before touching the database.
        self.assertIn('signed lease confirmation', self.expect(400, self.save, draft, complete=True, lease_signed=False))

        refreshed = ob.get_record(uuid.UUID(record['id']))
        self.assertEqual((refreshed['property']['id'], refreshed['version'], refreshed['data']['notes']), ('garner-3br', 2, 'TEST draft'))

        done = self.save(refreshed, complete=True)
        self.assertEqual(done['status'], 'completed')
        self.assertRegex(done['tenant']['tenant_ref'], r'^TEN-\d{6}$')
        self.assertEqual(done['tenancy']['status'], 'active')
        self.assertEqual(done['property']['id'], 'garner-3br')
        self.assertEqual(self.pg.get('properties', {'id': 'eq.garner-3br'})[0]['status'], 'let')

        # Retrying completion (lost response, double click, stale version) creates nothing new.
        again = self.save(refreshed, complete=True)
        self.assertEqual(again['tenant']['id'], done['tenant']['id'])
        self.assertEqual(len(self.pg.get('tenants', {'source_onboarding_id': f"eq.{record['id']}"})), 1)
        self.assertEqual(len(self.pg.get('tenancies', {'property_id': 'eq.garner-3br'})), 1)
        # Editing a completed record is refused.
        self.expect(409, self.save, done)

        # After refresh and in the list the link is still there.
        listed = [r for r in ob.list_records() if r['id'] == record['id']][0]
        self.assertEqual((listed['tenant']['tenant_ref'], listed['property']['id']), (done['tenant']['tenant_ref'], 'garner-3br'))

        # Another lead cannot start on the now-occupied property.
        self.expect(400, self.start, self.lead('TEST e2e second lead'), 'garner-3br')

    def test_invalid_and_unknown_property_ids(self):
        lead = self.lead('TEST e2e invalid property')
        self.expect(400, self.start, lead, 'TEST-does-not-exist')
        self.expect(400, self.start, lead, 'sale-only-or-missing')
        with self.assertRaises(Exception):
            ob.Start(conversation_id=lead, property_id="garner-3br' or '1'='1")
        self.assertEqual(self.pg.get('resident_onboardings', {'conversation_id': f'eq.{lead}'}), [])

    def test_property_taken_between_start_and_completion(self):
        first = self.start(self.lead('TEST e2e race A'), 'velachery-1bhk')
        second = self.start(self.lead('TEST e2e race B'), 'velachery-1bhk')
        self.save(first, complete=True, resident_name='TEST Race A')
        detail = self.expect(409, self.save, second, complete=True, resident_name='TEST Race B')
        self.assertIn('no longer available', detail)
        still = ob.get_record(uuid.UUID(second['id']))
        self.assertEqual((still['status'], still['tenant']), ('in_progress', None))
        self.assertEqual(len(self.pg.get('tenancies', {'property_id': 'eq.velachery-1bhk'})), 1)

    def test_unknown_record(self):
        self.expect(404, ob.get_record, uuid.uuid4())


if __name__ == '__main__':
    unittest.main()
