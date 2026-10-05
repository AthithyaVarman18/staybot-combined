-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Direct chat between a tenant and the investor who listed a home
-- (src/services/rental_chat.py, "Messages" tab in /ui).
-- Needs supabase_accounts.sql, supabase_properties.sql, supabase_owner_listings.sql
-- and supabase_rental_applications.sql to have been run first. Safe to run again.
--
-- One thread per (home, tenant). Started by the tenant from a home they're
-- interested in, or by the owner from an application on their Tenants tab.

create table if not exists rental_threads (
  id                    uuid primary key default gen_random_uuid(),
  property_id           text not null references properties(id) on delete cascade,
  property_title        text,
  tenant_account_id     uuid not null references accounts(id) on delete cascade,
  tenant_name           text,
  owner_session_id      text not null,     -- the investor account (accounts.session_id) that listed the home
  owner_name            text,
  tenant_last_read_at   timestamptz,
  owner_last_read_at    timestamptz,
  last_message_at       timestamptz,
  last_message_preview  text,
  last_sender           text check (last_sender in ('tenant', 'owner')),
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  unique (property_id, tenant_account_id)
);

create index if not exists rental_threads_tenant_idx on rental_threads (tenant_account_id, last_message_at desc);
create index if not exists rental_threads_owner_idx on rental_threads (owner_session_id, last_message_at desc);

create table if not exists rental_messages (
  id                 uuid primary key default gen_random_uuid(),
  thread_id          uuid not null references rental_threads(id) on delete cascade,
  sender             text not null check (sender in ('tenant', 'owner')),
  sender_account_id  uuid references accounts(id) on delete set null,
  body               text not null check (char_length(body) between 1 and 2000),
  created_at         timestamptz not null default now()
);

create index if not exists rental_messages_thread_idx on rental_messages (thread_id, created_at);

drop trigger if exists rental_threads_set_updated_at on rental_threads;
create trigger rental_threads_set_updated_at
before update on rental_threads
for each row execute function set_updated_at();   -- defined in supabase_schema.sql

-- Server-only, like every other table.
alter table rental_threads enable row level security;
alter table rental_messages enable row level security;
revoke all on rental_threads, rental_messages from anon, authenticated;
