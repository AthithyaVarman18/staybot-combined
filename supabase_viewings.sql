-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Creates the viewings table: every viewing a customer asks for in the chat.
-- Needs supabase_schema.sql to have been run first (conversations + set_updated_at).

create table if not exists viewings (
  id                uuid primary key default gen_random_uuid(),
  conversation_id   uuid references conversations(id) on delete set null,  -- kept if the chat is reset
  session_id        text,
  property_id       text,                   -- properties.id, e.g. 'omr-3bhk'
  property_title    text,
  viewing_date      date not null,
  viewing_time      time not null,
  customer_name     text,
  customer_phone    text,
  status            text not null default 'requested'
                    check (status in ('requested', 'confirmed', 'cancelled', 'completed')),
  notes             text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index if not exists viewings_when_idx on viewings (viewing_date, viewing_time);
create index if not exists viewings_open_idx on viewings (session_id, property_id, status);

drop trigger if exists viewings_set_updated_at on viewings;
create trigger viewings_set_updated_at
before update on viewings
for each row execute function set_updated_at();

-- Only the backend (service_role key) can read or write viewings.
alter table viewings enable row level security;
