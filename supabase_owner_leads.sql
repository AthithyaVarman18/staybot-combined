-- Owner leads imported from a provider's CSV file (e.g. a listings data provider),
-- plus the staff outreach log. Run once in Supabase: SQL Editor -> New query -> paste -> Run.
-- Needs supabase_schema.sql and supabase_properties.sql first. Only adds new tables; safe to run again.

create table if not exists owner_lead_imports (
  id           uuid primary key default gen_random_uuid(),
  file_name    text not null,
  source       text not null,
  row_count    integer not null default 0,
  imported     integer not null default 0,
  duplicates   integer not null default 0,
  skipped      integer not null default 0,
  created_by   text not null,
  created_at   timestamptz not null default now()
);

create table if not exists owner_leads (
  id                 uuid primary key default gen_random_uuid(),
  import_id          uuid references owner_lead_imports(id),
  dedupe_key         text not null unique,          -- phone, else email, else address: stops the same owner being imported twice
  owner_name         text not null,
  phone              text check (phone is null or phone ~ '^\+[1-9][0-9]{7,14}$'),
  email              text,
  property_title     text,
  address            text,
  area               text,
  city               text,
  pincode            text,
  listing_type       text check (listing_type is null or listing_type in ('rent', 'sale')),
  price              numeric(14, 2),
  deposit            numeric(14, 2),
  currency           text not null default 'INR' check (currency in ('INR', 'USD')),
  bedrooms           integer,
  bathrooms          integer,
  sqft               integer,
  furnished          text,
  listing_url        text,
  listed_on          date,
  source             text not null,
  -- What the provider says the owner agreed to. WhatsApp is only allowed with 'whatsapp'.
  consent            text not null default 'none' check (consent in ('whatsapp', 'call', 'email', 'none')),
  consent_source     text not null default 'provider_file',
  do_not_call        boolean not null default false,
  status             text not null default 'new'
                     check (status in ('new', 'contacted', 'interested', 'call_back', 'not_interested', 'do_not_contact', 'converted')),
  notes              text,
  provider_notes     text,
  conversation_id    uuid references conversations(id) on delete set null,
  property_id        text references properties(id),
  last_contacted_at  timestamptz,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);
create index if not exists owner_leads_status_idx on owner_leads (status, updated_at desc);

create table if not exists owner_lead_activity (
  id          bigint generated always as identity primary key,
  lead_id     uuid not null references owner_leads(id),
  actor       text not null,
  action      text not null,
  details     jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);
create index if not exists owner_lead_activity_lead_idx on owner_lead_activity (lead_id, id desc);

drop trigger if exists owner_leads_set_updated_at on owner_leads;
create trigger owner_leads_set_updated_at before update on owner_leads
for each row execute function set_updated_at();

-- Server-only, like every other table.
alter table owner_lead_imports enable row level security;
alter table owner_leads enable row level security;
alter table owner_lead_activity enable row level security;
revoke all on owner_lead_imports, owner_leads, owner_lead_activity from anon, authenticated;

-- Email outreach: every imported owner can be emailed once; the email has a
-- personal link where the owner says yes (WhatsApp / email), no, or unsubscribes.
alter table owner_leads add column if not exists email_status text not null default 'not_sent'
  check (email_status in ('not_sent', 'sending', 'sent', 'test_sent', 'failed'));
alter table owner_leads add column if not exists email_sent_at timestamptz;
alter table owner_leads add column if not exists email_error text;
alter table owner_leads add column if not exists confirm_token_hash text unique;
alter table owner_leads add column if not exists response text
  check (response is null or response in ('whatsapp', 'email', 'not_interested', 'unsubscribed'));
alter table owner_leads add column if not exists responded_at timestamptz;
