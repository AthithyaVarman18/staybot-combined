import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi import HTTPException
from pydantic import ValidationError
from src.services.onboarding import OnboardingData, Save, missing, save


class OnboardingTests(unittest.TestCase):
    def complete_data(self):
        return dict(resident_name='Resident', phone='+919876543210', move_in_date='2026-10-01',
                    occupants=2, rent=25000, deposit=0, lease_start='2026-10-01', lease_end='2027-09-30',
                    documents={'identity':'verified','signed_lease':'verified'}, lease_signed=True)

    def test_completion_accepts_zero_deposit(self):
        self.assertEqual(missing(OnboardingData(**self.complete_data()).model_dump(mode='json')), [])

    def test_incomplete_does_not_write(self):
        with patch('src.services.onboarding.db._post') as write:
            with self.assertRaises(HTTPException) as error:
                save(uuid4(), Save(version=1, complete=True))
            self.assertEqual(error.exception.status_code, 400)
            write.assert_not_called()

    def test_received_is_not_verified(self):
        data=self.complete_data(); data['documents']['identity']='received'
        self.assertIn('Identity document verification', missing(data))

    def test_invalid_lease_dates(self):
        data=self.complete_data(); data['lease_end']='2026-09-01'
        with self.assertRaises(ValidationError): OnboardingData(**data)

    def test_move_in_outside_lease(self):
        data=self.complete_data(); data['move_in_date']='2027-10-01'
        with self.assertRaises(ValidationError): OnboardingData(**data)

    def test_missing_document_cannot_bypass(self):
        with self.assertRaises(ValidationError): OnboardingData(documents={})

    def test_complete_uses_atomic_rpc(self):
        body=Save(version=2, complete=True, **self.complete_data())
        record_id = uuid4()
        with patch('src.services.onboarding.db.ENABLED', True), patch('src.services.onboarding.db._post', return_value={'id': str(record_id), 'data':self.complete_data()}) as write, patch('src.services.onboarding.properties.clear_cache') as clear, patch('src.services.onboarding.fetch_one', return_value={}) as reload:
            save(record_id, body)
            reload.assert_called_once_with(str(record_id))
            self.assertEqual(write.call_args.args[0], 'rpc/save_resident_onboarding')
            self.assertEqual(write.call_args.args[1]['expected_version'], 2)
            self.assertTrue(write.call_args.args[1]['finish'])
            clear.assert_called_once()

    def test_unconfigured_db(self):
        with patch('src.services.onboarding.db.ENABLED', False):
            with self.assertRaises(HTTPException) as error:
                save(uuid4(), Save(version=1))
            self.assertEqual(error.exception.status_code, 503)
