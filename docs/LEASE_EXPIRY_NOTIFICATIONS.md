# Lease expiry in-app notifications

This change adds customer-facing lease-expiry notifications for both sides of an active rental:

- **Tenant**: the logged-in tenant account receives the notification.
- **Existing property owner**: the logged-in `existing_investor` account that owns/listed the rental receives the same lease-expiry event in their own notification feed.

## Notification schedule

- Lease start day: one notice.
- After that: one notice every 30 days while more than 15 days remain.
- From 15 days before the lease end: one notice every day through the due date.
- After the due date: one "lease date exceeded" notice.

Each notice is created once per account (deduplicated), so refreshing or polling the app does not create duplicates.

## Database setup

Run this once in Supabase after the accounts and rental-application tables exist:

```text
supabase_lease_notifications.sql
```

It creates the server-only `account_notifications` table.

## How the lease end is determined

For customer rental applications, the notification service uses an explicit `details.lease_end` when present. Otherwise it calculates the lease end from `details.move_in_date + details.lease_months`, with the lease ending on the day before the matching calendar date.

Purchase applications are ignored.

## UI behavior

Customer accounts get a **Notifications** tab (API: `/me/account-notifications`; the separate `/me/notifications` routes are the owners' "someone asked about your house" pop-ups). The tab shows unread count, notification history, and a Mark all read action.

When a new unread notification appears, Staybot also shows a small pop-up toast at the bottom of the app. The popup is shown once per notification per browser session history; the notification remains unread until the user opens it or marks all as read.

The frontend polls every 30 seconds while the page is visible, so an approaching lease-expiry notice can appear without a full page refresh.

## Automated verification

The added test file is:

```text
tests/test_lease_notifications.py
```

It covers calendar-safe lease calculations, the 30-day / 15-day-daily schedule, offer countdowns, tenant and owner delivery, deduplication, read-state protection, scoped maintenance and offer reminders, and that the two notification APIs don't clash.
