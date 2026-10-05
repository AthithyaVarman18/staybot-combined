"""Team-managed resident onboarding. No customer-facing record access.

Tenant/property linking (supabase_tenancy.sql): an onboarding keeps the
existing property ID chosen at start. Verified completion creates one tenant
(unique tenant_ref such as TEN-000001) and one tenancy linking that tenant to
the same property, so a property keeps its full tenancy history.
"""
import re
from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.services import db, properties

router = APIRouter(prefix='/onboarding', tags=['Onboarding'])
DOCS = {'identity': 'Identity document', 'signed_lease': 'Signed lease'}


class OnboardingData(BaseModel):
    model_config = ConfigDict(extra='forbid')
    resident_name: str = Field(default='', max_length=200)
    phone: str = Field(default='', max_length=40)
    move_in_date: date | None = None
    occupants: int | None = Field(default=None, ge=1, le=100)
    rent: int | None = Field(default=None, ge=0)
    deposit: int | None = Field(default=None, ge=0)
    lease_start: date | None = None
    lease_end: date | None = None
    documents: dict[str, Literal['pending', 'received', 'verified']] = Field(
        default_factory=lambda: dict.fromkeys(DOCS, 'pending'))
    lease_signed: bool = False
    notes: str = Field(default='', max_length=4000)

    @model_validator(mode='after')
    def validate_dates(self):
        if set(self.documents) != set(DOCS):
            raise ValueError('Both identity and signed lease document statuses are required.')
        if self.lease_start and self.lease_end and self.lease_end <= self.lease_start:
            raise ValueError('Lease end must be after lease start.')
        if self.move_in_date and self.lease_start and self.move_in_date < self.lease_start:
            raise ValueError('Move-in date cannot be before lease start.')
        if self.move_in_date and self.lease_end and self.move_in_date > self.lease_end:
            raise ValueError('Move-in date cannot be after lease end.')
        return self


def missing(data):
    absent = [name.replace('_', ' ') for name in (
        'resident_name', 'phone', 'move_in_date', 'occupants', 'lease_start', 'lease_end'
    ) if not data.get(name) or (isinstance(data[name], str) and not data[name].strip())]
    absent += [name for name in ('rent', 'deposit') if data.get(name) is None]
    absent += [label + ' verification' for key, label in DOCS.items()
               if data.get('documents', {}).get(key) != 'verified']
    if not data.get('lease_signed'):
        absent.append('signed lease confirmation')
    return absent


def configured():
    if not db.ENABLED:
        raise HTTPException(503, 'Connect Supabase and run supabase_onboarding.sql to save onboarding records.')


def call(fn, *args, **kwargs):
    configured()
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as error:
        text = getattr(getattr(error, 'response', None), 'text', '') or str(error)
        if 'Property unavailable' in text:
            raise HTTPException(409, 'This property is no longer available: it is already occupied or not an active rental. Nothing was changed.')
        if 'Record changed or already completed' in text:
            raise HTTPException(409, 'This record was changed elsewhere or is already completed. Refresh it and try again.')
        if 'Record missing' in text:
            raise HTTPException(404, 'Onboarding record not found.')
        raise HTTPException(409, 'Could not save or load onboarding. Check the database migration, refresh the record, and confirm the property is still available.')


DETAILS_VIEW = 'resident_onboarding_details'


def _is_missing_relation(error):
    response = getattr(error, 'response', None)
    text = getattr(response, 'text', '') or ''
    return getattr(response, 'status_code', None) == 404 or 'PGRST205' in text or 'does not exist' in text


def fetch(params):
    """Onboarding rows with their property reference and linked tenant.
    Before supabase_tenancy.sql is run, falls back to the plain table so the
    existing onboarding flow keeps working (without tenant fields)."""
    configured()
    try:
        return db._get(DETAILS_VIEW, {'select': '*', **params})
    except Exception as error:
        if not _is_missing_relation(error):
            raise HTTPException(409, 'Could not load onboarding. Refresh and try again.')
    return call(db._get, 'resident_onboardings', {'select': '*', **params})


def present(row):
    return {
        **row,
        'missing': missing(row['data']),
        # Shown in the onboarding details and the completed resident view.
        'property': {
            'id': row['property_id'],
            'ref': row.get('property_ref'),
            'name': row.get('property_name') or row.get('property_title'),
        },
        'tenant': {
            'id': row['tenant_id'],
            'tenant_ref': row.get('tenant_ref'),
            'name': row.get('tenant_name'),
        } if row.get('tenant_id') else None,
        'tenancy': {
            'id': row['tenancy_id'],
            'status': row.get('tenancy_status'),
            'started_at': row.get('tenancy_started_at'),
            'ended_at': row.get('tenancy_ended_at'),
        } if row.get('tenancy_id') else None,
        'tenant_linking_ready': 'tenant_id' in row,
    }


def fetch_one(record_id):
    rows = fetch({'id': f'eq.{record_id}'})
    if not rows:
        raise HTTPException(404, 'Onboarding record not found.')
    return present(rows[0])


# Existing property IDs are short slugs such as "omr-3bhk" or "owner-anna-nagar-58ec39".
PROPERTY_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$')


class Start(BaseModel):
    conversation_id: UUID
    property_id: str = Field(min_length=1, max_length=200, pattern=PROPERTY_ID.pattern)


@router.get('')
def list_records():
    return [present(row) for row in fetch({'order': 'updated_at.desc'})]


@router.get('/{record_id}')
def get_record(record_id: UUID):
    """Refresh one record (team-only, by onboarding ID - never by property ID)."""
    return fetch_one(record_id)


@router.post('')
def start(body: Start):
    lead = call(db.get_lead, str(body.conversation_id))
    if not lead:
        raise HTTPException(404, 'Lead not found.')
    existing = call(db._get, 'resident_onboardings', {'conversation_id': f'eq.{body.conversation_id}', 'select': 'id,property_id'})
    if existing:
        # Never silently switch an existing assignment to another property.
        if existing[0]['property_id'] != body.property_id:
            raise HTTPException(409, f"This lead's onboarding is already assigned to property {existing[0]['property_id']}. "
                                     'The property assignment cannot be changed.')
        return fetch_one(existing[0]['id'])
    rows = call(db._get, 'properties', {'id': f'eq.{body.property_id}', 'status': 'eq.active', 'listing_type': 'eq.rent'})
    if not rows:
        raise HTTPException(400, 'Select an available rental property stored in Supabase.')
    data = OnboardingData().model_dump(mode='json')
    # Reuse only explicit resident details, never inferred listing prices or dates.
    for message in call(db.list_messages, str(body.conversation_id)):
        analysis = message.get('analysis') or {}
        details = analysis.get('viewing_request') or {}
        for source, target in [('name', 'resident_name'), ('phone', 'phone')]:
            if details.get(source):
                data[target] = str(details[source])[:200 if target == 'resident_name' else 40]
    if (lead.get('session_id') or '').startswith('wa:'):
        data['phone'] = lead['session_id'][3:][:40]
    row = call(db._post, 'resident_onboardings', {
        'conversation_id': str(body.conversation_id), 'property_id': rows[0]['id'],
        'property_title': rows[0]['title'], 'data': data,
    })
    return fetch_one(row['id'])


class Save(OnboardingData):
    version: int = Field(ge=1)
    complete: bool = False


@router.put('/{record_id}')
def save(record_id: UUID, body: Save):
    data = body.model_dump(mode='json', exclude={'version', 'complete'})
    if body.complete and missing(data):
        raise HTTPException(400, 'Complete these items first: ' + ', '.join(missing(data)))
    row = call(db._post, 'rpc/save_resident_onboarding', {
        'record_id': str(record_id), 'expected_version': body.version,
        'new_data': data, 'finish': body.complete,
    })
    if body.complete:
        properties.clear_cache()
    # Reload with the property reference and (after completion) the linked tenant.
    return fetch_one(row['id'])
