-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Creates the maintenance_tickets table: every issue a tenant reports in the
-- chat/WhatsApp conversation (the AI detects it and asks for their unit
-- number first - see MAINTENANCE REPORTS in src/prompts/system_prompt.py),
-- with the AI's photo/text classification and the team's own workflow status.
-- Needs supabase_schema.sql to have been run first (set_updated_at).

create table if not exists maintenance_tickets (
  id                    uuid primary key default gen_random_uuid(),
  conversation_id       uuid references conversations(id) on delete set null,  -- kept if the chat is reset
  session_id            text,
  property_id           text,                 -- properties.id, e.g. 'omr-3bhk'
  property_title        text,
  tenant_id             text not null,        -- the tenant's own unit/apartment number, e.g. 'Unit 302' - asked for before anything else
  tenant_name           text,
  tenant_phone          text,
  message               text not null default '',
  has_image             boolean not null default false,
  image_filename        text,

  -- What the AI decided
  issue_type            text,                 -- plumbing | electrical | hvac | appliance |
                                                -- structural | water_damage | gas | pest |
                                                -- security | door_window | heating | cooling | other
  urgency               text,                 -- urgent | normal | low
  confidence            numeric,              -- 0.0 - 1.0
  photo_text_match      boolean,              -- null = no photo, or not enough evidence either way
  summary               text,
  recommended_action    text,
  classification_status text not null default 'needs_review'
                        check (classification_status in ('classified', 'rejected', 'needs_review')),

  -- What the team is doing about it
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

-- Only the backend (service_role key) can read or write tickets.
alter table maintenance_tickets enable row level security;
