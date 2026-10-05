-- ============================================================================
-- StayBot: Maintenance classification ONLY - Supabase setup
-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
--
-- This intentionally sets up ONLY what the maintenance ticket feature needs:
--   1) the "conversations" table + set_updated_at() trigger fn (prerequisites,
--      since maintenance_tickets has a foreign key to conversations and reuses
--      the same trigger function)
--   2) the "maintenance_tickets" table itself
--
-- It deliberately does NOT create: messages, properties, viewings, outcomes,
-- lead scoring extras, or ai_calls - those power other StayBot features
-- (leads dashboard, listings, viewings) that are out of scope here.
-- ============================================================================

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- 1) Prerequisites: conversations table + shared trigger function
-- ---------------------------------------------------------------------------

create table if not exists conversations (
  id                uuid primary key default gen_random_uuid(),
  session_id        text not null,
  listing_id        text,
  listing_title     text,
  persona           text default 'tenant',
  property_context  jsonb default '{}'::jsonb,
  role              text,
  intent            text,
  intent_score      int default 0,
  lead_status       text default 'cold',
  summary           text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index if not exists conversations_session_idx on conversations (session_id, listing_id);

create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists conversations_set_updated_at on conversations;
create trigger conversations_set_updated_at
before update on conversations
for each row execute function set_updated_at();

alter table conversations enable row level security;

-- ---------------------------------------------------------------------------
-- 2) maintenance_tickets - the actual maintenance classification table
-- ---------------------------------------------------------------------------

create table if not exists maintenance_tickets (
  id                    uuid primary key default gen_random_uuid(),
  conversation_id       uuid references conversations(id) on delete set null,
  session_id            text,
  property_id           text,
  property_title        text,
  tenant_id             text not null,
  tenant_name           text,
  tenant_phone          text,
  message               text not null default '',
  has_image             boolean not null default false,
  image_filename        text,

  issue_type            text,
  urgency               text,
  confidence            numeric,
  photo_text_match      boolean,
  summary               text,
  recommended_action    text,
  classification_status text not null default 'needs_review'
                        check (classification_status in ('classified', 'rejected', 'needs_review')),

  ticket_status         text not null default 'needs_review'
                        check (ticket_status in ('needs_review', 'open', 'in_progress', 'resolved', 'dismissed')),
  notes                 text,

  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

create index if not exists maintenance_tickets_status_idx on maintenance_tickets (ticket_status, created_at desc);
create index if not exists maintenance_tickets_property_idx on maintenance_tickets (property_id);
create index if not exists maintenance_tickets_open_idx on maintenance_tickets (conversation_id, ticket_status);
create index if not exists maintenance_tickets_tenant_idx on maintenance_tickets (tenant_id);

drop trigger if exists maintenance_tickets_set_updated_at on maintenance_tickets;
create trigger maintenance_tickets_set_updated_at
before update on maintenance_tickets
for each row execute function set_updated_at();

-- Only the backend (service_role / secret key) can read or write tickets.
alter table maintenance_tickets enable row level security;
