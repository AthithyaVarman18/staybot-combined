-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Tenant -> team -> owner rental approval (src/services/rentals.py).
-- Needs supabase_accounts.sql, supabase_properties.sql and
-- supabase_owner_listings.sql to have been run first. Safe to run again.
--
-- Flow:
--   submitted       tenant applied for a home (Homes tab)          -> waits for the team
--   team_approved   team approved the tenant                        -> shows on the owner's Tenants tab
--   approved        owner approved (or the team, for a home no      -> tenant rents the home; maintenance
--                   investor owns)                                     requests are open for it
--   team_declined / owner_declined / withdrawn / closed (home let to someone else) / ended (tenancy over)

create table if not exists rental_applications (
  id                 uuid primary key default gen_random_uuid(),
  property_id        text not null references properties(id) on delete cascade,
  property_title     text,
  tenant_account_id  uuid not null references accounts(id) on delete cascade,
  tenant_session_id  text not null,
  tenant_name        text,
  tenant_email       text,
  tenant_phone       text,
  owner_session_id   text,          -- session_id of the investor account that listed the home; null = team-managed home
  move_in_date       text,
  message            text,
  status             text not null default 'submitted'
                     check (status in ('submitted', 'team_approved', 'approved', 'team_declined',
                                       'owner_declined', 'withdrawn', 'closed', 'ended')),
  team_note          text,
  team_reviewed_by   text,
  team_reviewed_at   timestamptz,
  owner_note         text,
  owner_decided_at   timestamptz,
  ended_at           timestamptz,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

-- One open application per tenant per home, and one approved tenant per home.
create unique index if not exists rental_applications_one_open
  on rental_applications (tenant_account_id, property_id)
  where status in ('submitted', 'team_approved', 'approved');
create unique index if not exists rental_applications_one_tenant_per_home
  on rental_applications (property_id) where status = 'approved';

create index if not exists rental_applications_status_idx on rental_applications (status, created_at desc);
create index if not exists rental_applications_owner_idx on rental_applications (owner_session_id, status);
create index if not exists rental_applications_tenant_idx on rental_applications (tenant_account_id, status);

drop trigger if exists rental_applications_set_updated_at on rental_applications;
create trigger rental_applications_set_updated_at
before update on rental_applications
for each row execute function set_updated_at();   -- defined in supabase_schema.sql

-- Server-only, like every other table.
alter table rental_applications enable row level security;
revoke all on rental_applications from anon, authenticated;

-- Tenants' own tickets and owners' tickets are looked up by session_id and
-- property_id (both already on maintenance_tickets) - no change needed there.
create index if not exists maintenance_tickets_session_idx on maintenance_tickets (session_id);

-- Tenant onboarding checklist from the "Apply to rent" dialog: household,
-- move-in, work and income, rental history, emergency contact and consent,
-- plus the listing's rent / pet / parking rules when they applied.
-- Shown to the owner as a summary (contact details only once the team approves).
alter table rental_applications add column if not exists details jsonb;

-- Owner -> tenant purchase offers (src/services/rentals.py). The owner sends
-- the same terms as the staff Purchase offers page (price, earnest money, due
-- diligence, financing, closing date, expiry, what's included) from their
-- Tenants tab; the tenant sees it on their Homes tab and accepts or declines.
-- Staybot only records the terms - it never creates a purchase contract.
create table if not exists tenant_offers (
  id                 uuid primary key default gen_random_uuid(),
  application_id     uuid not null references rental_applications(id) on delete cascade,
  property_id        text not null,
  property_title     text,
  owner_session_id   text not null,
  tenant_account_id  uuid not null references accounts(id) on delete cascade,
  terms              jsonb not null,
  status             text not null default 'sent'
                     check (status in ('sent', 'accepted', 'declined', 'withdrawn', 'replaced')),
  tenant_note        text,
  responded_at       timestamptz,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);
create index if not exists tenant_offers_app_idx on tenant_offers (application_id, created_at desc);
create unique index if not exists tenant_offers_one_open on tenant_offers (application_id) where status = 'sent';

drop trigger if exists tenant_offers_set_updated_at on tenant_offers;
create trigger tenant_offers_set_updated_at
before update on tenant_offers
for each row execute function set_updated_at();

alter table tenant_offers enable row level security;
revoke all on tenant_offers from anon, authenticated;

-- Counter-offers: the tenant can modify the owner's purchase offer and send
-- it back; the owner accepts, declines or modifies again. Each version is a
-- row; from_party says who sent it, message is their note with it, and
-- tenant_note / owner_note is the other side's note when they answer.
alter table tenant_offers add column if not exists from_party text not null default 'owner'
  check (from_party in ('owner', 'tenant'));
alter table tenant_offers add column if not exists message text;
alter table tenant_offers add column if not exists owner_note text;
