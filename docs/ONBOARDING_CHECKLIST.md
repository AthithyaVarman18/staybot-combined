# Resident onboarding: requirement checklist

Status labels: **Tested** (implemented and covered by an automated test or a
browser/PDF check), **Implemented** (built, not separately tested),
**Needs configuration** (built, but needs external credentials or setup),
**Not implemented**.

Tests: `tests/test_lease_onboarding.py` (run with `bash tests/run_tenancy_db_tests.sh`).
`T01`–`T16` below are its `test_01`–`test_16`.

## Staff journey and interface

| Requirement | Status | Where / evidence |
|---|---|---|
| Guided 7-step story with numbered steps, progress, complete/remaining, missing info, who acts next, Back / Save and continue | Tested | `src/static/onboarding.js`; checked in browser at desktop width. T03, T04 |
| Ask staff for help throughout | Tested | Help panel (staff), "Ask staff for help" (tenant/owner page), HELP on WhatsApp. T14 |
| Mobile-friendly, Airbnb-style, no JSON or technical fields for customers | Tested | `src/static/party.html`; checked in browser at 375 px. The staff page reuses Staybot styles. |
| Start from a lead or the Onboarding tab; works without chat messages | Tested | Lead drawer → Start onboarding. T02 |
| Select or create property/unit/address, tenant, owner, staff; reuse records; prefill | Tested | Start dialog; people are reused by WhatsApp number; the owner is prefilled from the listing. T01 |
| Prevent duplicate cases | Tested | Unique active case per home + unit (DB index and API). T01 |
| Case list shows tenant, owner, property, step, missing info, both approvals, staff, next action, PDF, WhatsApp status | Tested | Case cards; checked in browser |
| Configurable document checklist per jurisdiction/property, with reasons | Tested | `onboarding_document_requirements`; Templates & checklist dialog |
| Upload ≠ verification; statuses: awaiting review / accepted / changes required | Tested | DB constraint requires a recorded reviewer. T05 |
| Progress saved in DB after every step; resume from another browser | Tested | No localStorage source of truth (only the "acting as" name). T03 |
| Currency, schedule, charges, occupants, lease/account setup, language, communication preferences | Tested | Steps 2, 3, 5. T03, T04 |

## Agreement, approvals, PDF

| Requirement | Status | Where / evidence |
|---|---|---|
| Draft generated from validated details and a configured template | Tested | `onboarding_create_agreement_version`, stored snapshot + SHA-256 |
| Both parties see the actual draft before approving | Tested | Tenant/owner page: terms + draft PDF; approval requires "I have read". Browser-checked |
| Separate tenant/owner approval: pending / approved / changes requested, who, when, exact version | Tested | `onboarding_approvals` (append-only). T06, T07, T12 |
| Staff cannot approve on a party's behalf | Tested | Approvals come only from the party's secure link, checked against the case's person. T08 |
| Any change creates a new version and invalidates previous approvals | Tested | Automatic re-versioning when details change. T07 |
| Backend-enforced finalization: complete info, accepted documents, same current version approved by both, staff review, signing | Tested | API + `onboarding_finalize_case` (tested by bypassing the API). T06, T07, T13 |
| Approval distinguished from signing; never labelled "signed" | Tested | PDF: "An approval is not a signature"; blank signature lines. T10 |
| Formal e-signatures | **Not implemented** | No provider integrated. Choosing "Electronic signatures required" blocks finalization with a clear message. T13 |
| Final deliverable is a readable PDF with all required content, reference and version | Tested | `src/services/lease_pdf.py`; pdfinfo/pdftotext checks, visual inspection. T10. Example: `docs/example-final-lease-DEMO.pdf` |
| DRAFT watermark before approval; DEMO labelling without an approved template | Tested | Visual check of draft and final; T10 checks the DEMO label |
| Staff-approved template setup | Tested / **Needs configuration** | "Add approved template" (requires an approver name + confirmation). T15. A real lawyer-approved template must be added |
| Final PDF stored privately, never overwritten, fingerprint checked, controlled downloads | Tested | Private bucket, `x-upsert: false`, SHA-256 checked on every download/send, team auth or party link only. T09, T10 |
| Preview agreement / Download final lease PDF / Send final PDF through WhatsApp | Tested | Buttons on steps 6 and 7; browser-checked |

## WhatsApp

| Requirement | Status | Where / evidence |
|---|---|---|
| Personalized invitations with secure, expiring, person-scoped links (no accounts) | Tested | `onboarding_access_links` (hash only, 7 days, revoked on reissue or person change). T08 |
| Step-by-step WhatsApp self-service; no repeated questions; resume | Tested | Role → name → email → language → communication, then a status summary. T14 |
| Final PDF sent to tenant and owner as a document, same version, personalized caption | Tested (test mode) / **Needs configuration** (live) | Media upload + document or template message per the official Cloud API docs. T11 |
| Separate per-recipient status: queued, accepted, sent, delivered, read, failed; no delivery claimed from request acceptance | Tested | Status only from webhooks; forward-only; test mode labelled "Simulated". T11 |
| Safe retries, duplicate requests and webhooks | Tested | Idempotency keys, claim-by-status, unique status events. T11, T12 |
| Webhook signature verification | Tested | Live webhooks require `WHATSAPP_APP_SECRET`; bad signatures are rejected. T13 |
| Consent and template requirements | Tested / **Needs configuration** | Consent is recorded before any message. Outside the 24-hour window an approved template is required, otherwise the send is marked failed with the reason. T13. Templates must be created and approved in Meta |
| Honest test mode when credentials are missing | Tested | Setup banners; `test_mode` rows; test cases never message real numbers (unless listed in `WHATSAPP_TEST_RECIPIENTS`). T13 |

## Specification onboarding criteria

| Criterion | Status | Notes |
|---|---|---|
| Greeting and role confirmation (tenant/owner/staff) | Tested | The WhatsApp greeting confirms the role; "not me" escalates. Staff are identified by the team login. T14 |
| Property identifier and contact capture | Tested | T01 |
| TurboTenant onboarding link generation | **Not implemented** | No public TurboTenant API documentation. Status "Not connected"; staff can paste a link they created themselves (validated to turbotenant.com). T15 |
| Lease and account setup preferences | Tested | Step 5 |
| Document checklist and escalation needs | Tested | T05, T14 |
| Personalized welcome messages | Tested | Invitation and final-PDF caption use the person's name, the home and the reference. A separate post-move-in welcome series is **Not implemented** |
| WhatsApp self-service | Tested (test mode) / **Needs configuration** (live) | T14 |
| Internal staff visibility and human handoff | Tested | Escalations with handoff summaries (role, advisor, urgency, understood, context, next action, follow-up, reason, summary, confidence). T14 |
| Communication preferences | Tested | Language, channel, best time |
| Role-based access controls | Tested | Staff: team password. Tenant/owner: scoped link tokens. Supabase tables: RLS on, public roles revoked. T08, T09. Limitation: staff share one password, so staff names in the audit trail are self-selected, not individually authenticated |
| Audit logging | Tested | `onboarding_audit_log` (append-only trigger). T12 |
| AI must not invent facts, grant approvals, bypass checks, claim signatures or delivery | Tested | Handoff summaries are rule-based (quote the message plus stored facts). No AI step can write approvals or statuses. T10, T11, T14 |
| 80% WhatsApp self-service completion target | Implemented (tracking only) | `GET /onboarding/cases/metrics` counts real (non-test) cases only. The target has **not** been measured; it needs real usage |
| TurboTenant as system of record | **Not implemented** | Workflow state is in Supabase; this does not replace a TurboTenant integration |

## External dependencies still required

1. Run `supabase_lease_onboarding.sql` in the live Supabase project, then restart the app.
2. A lawyer-approved North Carolina lease template (replace the DEMO template).
3. WhatsApp Cloud API credentials, app secret, webhook subscription (messages and statuses), and two approved templates.
4. A public HTTPS address (`PUBLIC_BASE_URL`) so tenant/owner links work outside this computer.
5. TurboTenant API access and documentation, if automated sync is wanted.
6. An e-signature provider, if formal signatures are required.
7. Individual staff logins, if audit entries must be tied to authenticated staff members.

## Not part of this work

Lead scoring, property search, viewings, owner listings, maintenance triage
and the rest of the broader specification are separate modules and are not
claimed as complete by this onboarding work. Existing chat, leads, viewings and
earlier onboarding records still load (T16), and the existing unit tests pass.
