# Staybot customer notifications

Two separate notification systems run side by side:

| What | Who gets it | API | UI |
|---|---|---|---|
| Lease / maintenance / purchase-offer reminders | tenants and investors | `/me/account-notifications` | **Notifications** tab in `/ui` (badge + pop-up toast) |
| "Someone asked about your house" | owners (investor accounts) | `/me/notifications` | bell + pop-up cards (`static/notifications.js`) |

## Reminder schedule (`src/services/lease_notifications.py`)

- **Lease** (tenant and the owner who listed the home): on the lease start day, then once every 30 days while more than 15 days remain. From 15 days before the end date, one reminder every day through the due date. After the due date, a single "lease date exceeded" notice.
- **Maintenance** (tenant and owner): one reminder per open ticket for each day it stays open (needs_review / open / in_progress). Owners only see tickets from tenants they approved for their homes, the same rule as their Maintenance list.
- **Purchase offers** (the investor on the offer): 3 days, 2 days and "tomorrow" before `expires_at`, then 12, 6, 3 and 1 hours, plus an "expired" notice. The exact expiry time is in the message. Closing reminders use the same countdown when the optional `closing_time` is filled in on the offer.

Reminders are created when the account's dashboard asks for them (on load and every 30 seconds while the page is visible). Each one has a `dedupe_key`, so polling never creates duplicates. Each account only ever sees its own reminders.

## Supabase

Run `supabase_lease_notifications.sql` after `supabase_accounts.sql` and `supabase_rental_applications.sql`. It's safe to rerun and upgrades the earlier lease-only `account_notifications` table. The owner pop-ups need `supabase_owner_notifications.sql`.

Maintenance and offer reminders are best-effort: if `maintenance_tickets` or the acquisition workflow tables aren't set up yet, lease reminders still work.

## Tests

`tests/test_lease_notifications.py` and `tests/test_customer_notification_rules.py`.

## Automated high-priority enquiry email

- Reuses the existing `src/services/lead_scoring.py` result. No scoring thresholds were changed.
- Sends automatically when an enquiry crosses into `hot`, or when its score crosses from `<=90` to `>90`. Exactly 90 does not trigger the `>90` rule.
- Recipients are resolved only through stored property/enquiry relationships: the enquiry tenant account, the property owner account, rental-application ownership, and linked investor portfolio records. No global role broadcast and no name/phone/email matching is used.
- Uses `src/services/email_sender.py` and the existing SMTP variables. `EMAIL_DRY_RUN=true` keeps delivery in the existing test outbox.
- Delivery is idempotent through `high_priority_inquiry_email_notifications` with a unique `(inquiry_id, recipient_account_id, trigger_type)` constraint. Failed deliveries are recorded without breaking scoring or the enquiry workflow.
- Email contains Staybot branding, property title/location/price, current score/status, the high-priority reason, and a Staybot application link.

Run `supabase_high_priority_inquiry_email.sql` for the high-priority enquiry email delivery log.
