# Item 3 + Item 4 implementation and validation

## Implemented

### Item 3 — Offer to Purchase
- Staff-entered offer terms: price, earnest money, due-diligence fee, due-diligence period/end, financing contingency/deadline, included items, closing date, and offer expiration.
- Plain review PDF headed **Summary for review — not a contract**.
- Versioned offer terms and approval: a changed term set creates a new version, supersedes the prior approval, revokes the prior access link, and rebuilds active deadlines.
- Investor approval through an expiring, version-bound private link.
- Broker-platform handoff state and link with configurable platform: zipForm, Dotloop, SkySlope, or Other. No contract form is recreated in Staybot.
- Chronological negotiation timeline with actor, amount, timestamp, note, and terminal states.
- Deadline records for due diligence, financing, closing, and offer expiration.
- Background deadline worker creates staff alerts and attempts the existing WhatsApp channel; offer expiration is also guarded in request handling.
- No price/term recommendations by AI and no money collection for earnest money or due-diligence money.
- Deal view shows the latest linked offer, broker handoff state, and next active deadline.

### Item 4 — Inspection Assistance
- Staff-maintained inspector list with NC licence number, service areas, price, turnaround, and contact.
- Two-or-three-slot booking reusing the existing viewing date/time validation helpers.
- Listing-agent access confirmation and inspection-fee-payer field.
- Private PDF report storage using the existing private storage pattern, with unique non-overwriting paths.
- PDF text extraction with a manual-entry fallback.
- Findings require major/minor severity and an exact report quote entered from the report; the report remains the source of truth.
- WhatsApp summary uses only stored findings; it does not infer defects or severity.
- Repair costs only accept a real staff quote or a client-approved cost table.
- Repair negotiation is linked to the offer's due-diligence deadline and cannot be changed after that deadline.

## Required client configuration

1. Confirm the broker's contract platform and the actual handoff method/link workflow.
2. Provide the broker's trusted inspector list.
3. Confirm who pays the inspection fee.
4. Run `supabase_acquisition_workflows.sql` after the existing base, properties, investors, deals, and viewings migrations.
5. Keep `BROKER_CONTRACT_PLATFORM=unselected` until the broker confirms the platform.
6. For live WhatsApp messaging outside Meta's allowed messaging window, configure the approved WhatsApp template/provider workflow used by the client.

## Validation completed locally

- Python compilation: passed.
- Test suite: **103 passed, 31 skipped, 45 subtests passed**.
- JavaScript syntax checks: passed for purchase offers, investor approval, inspections, and deals UI scripts.
- SQL structural safety checks: passed; the new workflow migration contains no `DROP TABLE`, `TRUNCATE`, or `DELETE FROM` statements.
- FastAPI startup smoke test: passed; application startup completed and team/public workflow pages returned successfully.
- Database-backed live workflow: not executed in this environment because the configured Supabase host was not resolvable from the test environment. The app now returns a clear HTTP 503 instead of misreporting that condition as a successful workflow.

## Security / delivery note

The real `.env` file is intentionally excluded from the delivery ZIP. Use `.env.example` and the client's own secrets when configuring the environment.
