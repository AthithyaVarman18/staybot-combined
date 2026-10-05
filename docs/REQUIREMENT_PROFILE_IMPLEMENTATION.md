# Staybot Requirement/Profile Layer

This feature is additive. It sits between account login and the existing chatbot/property workflows.

## Flow

`Login -> existing mandatory onboarding (if any) -> Requirement Profile -> deterministic property matching -> existing Staybot chat`

Returning users can edit the saved profile from the persistent preferences button in the dashboard.

## Database setup

Run `supabase_requirements.sql` in the Supabase SQL Editor after the existing account schema. It creates:

- `user_requirements` — one active structured profile per user/profile type
- `requirement_versions` — update history

The backend uses the existing service-role Supabase client; no browser credentials are exposed.

## API

Customer:

- `GET /auth/requirements?profile_type=...`
- `POST /auth/requirements`
- `GET /auth/requirements/matches`
- `DELETE /auth/requirements?profile_type=...`

Staff:

- `GET /staff/requirements/clients`
- `GET /staff/requirements`
- `GET /staff/requirements/{user_id}`
- `PUT /staff/requirements/{user_id}`
- `/team/client-requirements` staff UI

All customer reads/writes derive the user from the existing authenticated session. A frontend-supplied `user_id` is never trusted.

## Role mapping in the existing Staybot architecture

The current authentication schema has three customer roles: `tenant`, `new_investor`, and `existing_investor`. It does not have a separate customer `owner` account role; owners are represented by investor accounts in the existing listing/rental workflows.

Therefore the implementation keeps authentication unchanged and supports a separate `owner` requirement profile for investor accounts. It is available from the persistent **Property Details** link and to staff when creating a seller/owner profile. This avoids introducing a fourth authentication role that would unnecessarily change existing owner/listing/lease behavior.

## Matching

Matching is deterministic and runs in `src/services/requirements.py` before an LLM call. It filters by listing type, location, property type, bedroom/bathroom minimums, budget, furnishing and required features using fields that actually exist in the property data. Match cards contain structured reasons from those fields.

The existing chatbot receives the saved profile as context and is explicitly told not to repeat questions for known fields. Existing chatbot/property/enquiry/offer/inspection logic remains in place.

## Tests

The full existing suite was run after the implementation:

`397 passed, 31 skipped, 45 subtests passed`

Additional tests are in `tests/test_requirements.py`.
