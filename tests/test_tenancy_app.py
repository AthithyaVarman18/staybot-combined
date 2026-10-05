"""App-level tests for tenant/property linking (no database needed)."""
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from src.services import onboarding as ob

LEAD = str(uuid4())
RECORD = str(uuid4())


def details_row(**extra):
    row = {'id': RECORD, 'conversation_id': LEAD, 'property_id': 'omr-3bhk', 'property_title': 'Sunlit 3BHK',
           'data': ob.OnboardingData().model_dump(mode='json'), 'status': 'in_progress', 'version': 1,
           'property_ref': 'CHN-OMR-301', 'property_name': 'Sunlit 3BHK near OMR IT corridor',
           'tenancy_id': None, 'tenancy_status': None, 'tenancy_started_at': None, 'tenancy_ended_at': None,
           'tenant_id': None, 'tenant_ref': None, 'tenant_name': None}
    row.update(extra)
    return row


def missing_view_error():
    error = Exception('404')
    error.response = MagicMock(status_code=404, text='{"code":"PGRST205"}')
    return error


class PropertyIdValidation(unittest.TestCase):

    def test_bad_property_ids_rejected_before_any_database_call(self):
        for bad in ['', ' ', 'omr-3bhk&status=eq.let', '../omr', 'omr 3bhk', 'omr,3bhk', '-leading', 'x' * 201, "o'brien"]:
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                ob.Start(conversation_id=uuid4(), property_id=bad)

    def test_real_property_id_formats_accepted(self):
        for good in ['omr-3bhk', 'owner-anna-nagar-58ec39', 'CHN.OMR_301']:
            ob.Start(conversation_id=uuid4(), property_id=good)


class StartOnboarding(unittest.TestCase):

    def setUp(self):
        for target, value in [('db.ENABLED', True), ('db.get_lead', MagicMock(return_value={'id': LEAD, 'session_id': 'browser'})),
                              ('db.list_messages', MagicMock(return_value=[]))]:
            p = patch(f'src.services.onboarding.{target}', value)
            p.start()
            self.addCleanup(p.stop)

    def test_nonexistent_or_unavailable_property_is_rejected_and_nothing_saved(self):
        get = MagicMock(side_effect=lambda table, params: [])
        with patch('src.services.onboarding.db._get', get), patch('src.services.onboarding.db._post') as write:
            with self.assertRaises(HTTPException) as error:
                ob.start(ob.Start(conversation_id=LEAD, property_id='no-such-home'))
        self.assertEqual(error.exception.status_code, 400)
        write.assert_not_called()

    def test_saves_the_existing_property_id_from_the_database(self):
        def get(table, params):
            if table == 'properties':
                return [{'id': 'omr-3bhk', 'title': 'Sunlit 3BHK near OMR IT corridor'}]
            if table == ob.DETAILS_VIEW:
                return [details_row()]
            return []
        with patch('src.services.onboarding.db._get', side_effect=get), \
             patch('src.services.onboarding.db._post', return_value={'id': RECORD}) as write:
            result = ob.start(ob.Start(conversation_id=LEAD, property_id='omr-3bhk'))
        saved = write.call_args.args[1]
        self.assertEqual(saved['property_id'], 'omr-3bhk')
        self.assertNotIn('tenant', str(saved), 'starting a draft creates no tenant')
        self.assertEqual(result['property'], {'id': 'omr-3bhk', 'ref': 'CHN-OMR-301', 'name': 'Sunlit 3BHK near OMR IT corridor'})
        self.assertIsNone(result['tenant'])

    def test_existing_assignment_is_never_changed_silently(self):
        def get(table, params):
            if table == 'resident_onboardings':
                return [{'id': RECORD, 'property_id': 'omr-3bhk'}]
            return [details_row()]
        with patch('src.services.onboarding.db._get', side_effect=get), patch('src.services.onboarding.db._post') as write:
            with self.assertRaises(HTTPException) as error:
                ob.start(ob.Start(conversation_id=LEAD, property_id='adyar-2bhk'))
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn('omr-3bhk', error.exception.detail)
            # Same property again: returns the existing record, nothing new written.
            again = ob.start(ob.Start(conversation_id=LEAD, property_id='omr-3bhk'))
        self.assertEqual(again['id'], RECORD)
        write.assert_not_called()


class LinkedDetails(unittest.TestCase):

    def test_completed_record_shows_tenant_and_property_reference(self):
        row = details_row(status='completed', tenant_id=str(uuid4()), tenant_ref='TEN-000007', tenant_name='TEST Priya',
                          tenancy_id=str(uuid4()), tenancy_status='active')
        shown = ob.present(row)
        self.assertEqual(shown['tenant']['tenant_ref'], 'TEN-000007')
        self.assertEqual(shown['property']['id'], 'omr-3bhk')
        self.assertEqual(shown['property']['ref'], 'CHN-OMR-301')
        self.assertEqual(shown['tenancy']['status'], 'active')
        self.assertTrue(shown['tenant_linking_ready'])

    def test_refresh_reads_by_onboarding_id_only(self):
        with patch('src.services.onboarding.db.ENABLED', True), \
             patch('src.services.onboarding.db._get', return_value=[details_row()]) as get:
            ob.get_record(RECORD)
        table, params = get.call_args.args
        self.assertEqual(table, ob.DETAILS_VIEW)
        self.assertEqual(params['id'], f'eq.{RECORD}')
        self.assertNotIn('property_id', params)

    def test_works_before_migration_without_tenant_fields(self):
        plain = {k: v for k, v in details_row().items() if not k.startswith(('tenan', 'property_ref', 'property_name'))}
        def get(table, params):
            if table == ob.DETAILS_VIEW:
                raise missing_view_error()
            return [plain]
        with patch('src.services.onboarding.db.ENABLED', True), patch('src.services.onboarding.db._get', side_effect=get):
            rows = ob.list_records()
        self.assertEqual(rows[0]['property']['id'], 'omr-3bhk')
        self.assertEqual(rows[0]['property']['name'], 'Sunlit 3BHK')
        self.assertIsNone(rows[0]['tenant'])
        self.assertFalse(rows[0]['tenant_linking_ready'])

    def test_other_database_errors_are_not_hidden_by_the_fallback(self):
        error = Exception('boom')
        error.response = MagicMock(status_code=500, text='server error')
        with patch('src.services.onboarding.db.ENABLED', True), patch('src.services.onboarding.db._get', side_effect=error):
            with self.assertRaises(HTTPException) as caught:
                ob.list_records()
        self.assertEqual(caught.exception.status_code, 409)


class AccessProtection(unittest.TestCase):

    def test_onboarding_routes_are_not_public(self):
        import src.main  # noqa: F401  (registers the router on the app)
        from src.services import team_auth
        onboarding_paths = [r.path for r in ob.router.routes]
        self.assertIn('/onboarding/{record_id}', onboarding_paths)
        for path in onboarding_paths:
            self.assertTrue(path.startswith('/onboarding'))
            self.assertNotIn(path, team_auth.PUBLIC_PATHS, 'onboarding data needs the team login')
            # No route looks up onboarding or tenant data by property ID.
            self.assertNotIn('property', path)


if __name__ == '__main__':
    unittest.main()
