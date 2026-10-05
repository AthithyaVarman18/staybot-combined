-- "Someone asked about your house" notifications for owners
-- (src/services/owner_notifications.py, src/static/notifications.js).
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run. Needs
-- supabase_schema.sql (set_updated_at) and supabase_accounts.sql first.
-- Safe to run again.

create table if not exists owner_notifications (
  id                uuid primary key default gen_random_uuid(),
  owner_account_id  uuid not null references accounts(id) on delete cascade,   -- the investor who listed the home
  property_id       text not null,
  property_title    text,
  asker_account_id  uuid references accounts(id) on delete set null,          -- tenant / new investor who asked
  asker_role        text,
  asker             jsonb not null default '{}'::jsonb,   -- snapshot: name, email, phone, role label, portfolio facts
  question          text,
  source            text check (source in ('chat', 'whatsapp', 'enquiry', 'message')),
  ask_count         integer not null default 1,           -- questions folded into this one (same person + home, 30 min)
  read_at           timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index if not exists owner_notifications_owner_idx
  on owner_notifications (owner_account_id, updated_at desc);
create index if not exists owner_notifications_merge_idx
  on owner_notifications (owner_account_id, asker_account_id, property_id, updated_at desc);

drop trigger if exists owner_notifications_set_updated_at on owner_notifications;
create trigger owner_notifications_set_updated_at before update on owner_notifications
for each row execute function set_updated_at();

-- Backend only (service_role key): the asker's contact details live here.
alter table owner_notifications enable row level security;
