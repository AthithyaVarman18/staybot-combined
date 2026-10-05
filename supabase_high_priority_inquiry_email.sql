-- Staybot high-priority enquiry email delivery tracking.
-- Run once in Supabase SQL Editor after supabase_property_inquiries.sql
-- and supabase_accounts.sql.
--
-- The table is intentionally separate from account_notifications:
-- account_notifications powers the in-app notification center, while this
-- table is the durable/idempotent SMTP delivery ledger for high-priority
-- enquiry emails.

create table if not exists high_priority_inquiry_email_notifications (
  id                    uuid primary key default gen_random_uuid(),
  inquiry_id            uuid not null references property_inquiries(id) on delete cascade,
  property_id           text,
  recipient_account_id  uuid not null references accounts(id) on delete cascade,
  recipient_role        text not null check (recipient_role in ('tenant', 'investor')),
  recipient_email       text,
  trigger_type          text not null check (trigger_type in ('hot', 'score_gt_90')),
  lead_score            integer not null,
  lead_status           text,
  delivery_status       text not null default 'pending'
                        check (delivery_status in ('pending', 'sent', 'failed')),
  sent_at               timestamptz,
  error                 text,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  unique (inquiry_id, recipient_account_id, trigger_type)
);

create index if not exists high_priority_inquiry_email_inquiry_idx
  on high_priority_inquiry_email_notifications (inquiry_id, created_at desc);

create index if not exists high_priority_inquiry_email_recipient_idx
  on high_priority_inquiry_email_notifications (recipient_account_id, created_at desc);

create index if not exists high_priority_inquiry_email_status_idx
  on high_priority_inquiry_email_notifications (delivery_status, created_at desc);

drop trigger if exists high_priority_inquiry_email_set_updated_at
  on high_priority_inquiry_email_notifications;

create trigger high_priority_inquiry_email_set_updated_at
before update on high_priority_inquiry_email_notifications
for each row execute function set_updated_at();

alter table high_priority_inquiry_email_notifications enable row level security;

-- Only the backend service_role reads/writes delivery records.
revoke all on high_priority_inquiry_email_notifications from anon, authenticated;
