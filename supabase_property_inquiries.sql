-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Creates the property_inquiries table: "Enquire about this" clicks from the
-- listing cards in the chat, confirmed by the customer in the enquiry panel.
-- Needs supabase_schema.sql to have been run first (conversations + set_updated_at).

create table if not exists property_inquiries (
  id                uuid primary key default gen_random_uuid(),
  conversation_id   uuid references conversations(id) on delete set null,  -- kept if the chat is reset
  session_id        text,
  property_id       text,                   -- properties.id, e.g. 'omr-3bhk'
  property_title    text not null,
  price_label       text,
  area              text,
  owner_name        text,                   -- copied in at save time; never sent to the customer
  owner_phone       text,
  customer_name     text,
  customer_phone    text,
  status            text not null default 'new'
                    check (status in ('new', 'contacted', 'closed')),
  notes             text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index if not exists property_inquiries_created_idx on property_inquiries (created_at desc);
create index if not exists property_inquiries_status_idx on property_inquiries (status);

drop trigger if exists property_inquiries_set_updated_at on property_inquiries;
create trigger property_inquiries_set_updated_at
before update on property_inquiries
for each row execute function set_updated_at();

-- Only the backend (service_role key) can read or write inquiries - owner
-- contact details live on this table, same rule as properties.owner_phone.
alter table property_inquiries enable row level security;

-- Snapshot of the linked conversation's lead score/status/summary, taken at
-- the moment the inquiry is saved - not a live view of conversation_id.
-- "General enquiry" is one long-running conversation reused for whatever
-- the customer asks about next, so its live summary keeps changing; without
-- a snapshot, an old inquiry about one property would silently start
-- showing whatever the customer is chatting about right now instead of
-- what that enquiry was actually about.
alter table property_inquiries add column if not exists lead_summary text;
alter table property_inquiries add column if not exists lead_score integer;
alter table property_inquiries add column if not exists lead_status text;

-- When the home's owner (the investor who listed it) first saw this enquiry
-- on their Tenants tab -> Enquiries. Null = still counts in their badge.
alter table property_inquiries add column if not exists owner_seen_at timestamptz;
