-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- A tenant buying the home they rent (src/services/home_purchase.py).
-- Needs supabase_rental_applications.sql first. Safe to run again.
--
-- Flow (home_buy_requests.status):
--   requested       tenant pressed "Own this house" on their home      -> waits for the owner
--   owner_declined  owner said no (owner_note is shown to the tenant)  -> the button stays disabled
--   negotiating     owner agreed; the tenant now has an investor        -> price / terms go back and forth
--                   profile on the new-investor journey                   as tenant_offers on the rental application
--   sale_agreed     one side accepted the other's offer                 -> our team runs the buyer checks
--                                                                          and the property checks
--   completed       both checks passed: the home is the tenant's. Their tenancy ends, the home leaves the
--                   seller's portfolio and joins theirs, and their account becomes an existing investor.
--                   They then tell us whether they live there or rent it out (plan, list_home, want_rentals).
--   cancelled       a check failed, or the tenancy ended first - they keep renting as before.

create table if not exists home_buy_requests (
  id                   uuid primary key default gen_random_uuid(),
  application_id       uuid not null unique references rental_applications(id) on delete cascade,  -- one request per tenancy
  property_id          text not null,
  property_title       text,
  tenant_account_id    uuid not null references accounts(id) on delete cascade,
  owner_session_id     text not null,
  tenant_message       text,
  status               text not null default 'requested'
                       check (status in ('requested', 'owner_declined', 'negotiating', 'sale_agreed', 'completed', 'cancelled')),
  owner_note           text,
  owner_decided_at     timestamptz,
  investor_id          uuid references investors(id) on delete set null,
  agreed_offer_id      uuid references tenant_offers(id) on delete set null,
  agreed_price         numeric,
  buyer_check          text check (buyer_check in ('passed', 'failed')),
  buyer_check_note     text,
  property_check       text check (property_check in ('passed', 'failed')),
  property_check_note  text,
  checked_by           text,
  completed_at         timestamptz,
  plan                 text check (plan in ('live_in', 'rent_out')),
  list_home            boolean,
  want_rentals         boolean,
  plan_answered_at     timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);
create index if not exists home_buy_requests_owner_idx on home_buy_requests (owner_session_id, status);
create index if not exists home_buy_requests_tenant_idx on home_buy_requests (tenant_account_id, status);

drop trigger if exists home_buy_requests_set_updated_at on home_buy_requests;
create trigger home_buy_requests_set_updated_at
before update on home_buy_requests
for each row execute function set_updated_at();   -- defined in supabase_schema.sql

-- Server-only, like every other table.
alter table home_buy_requests enable row level security;
revoke all on home_buy_requests from anon, authenticated;

-- The property check uses the same inspection flow as an investor buying a
-- home: once the price is agreed, the purchase gets an acquisition_offers row
-- (supabase_acquisition_workflows.sql), so the team books the inspector,
-- uploads the report and records findings / repairs in the Inspections tab.
-- The buyer check isn't repeated - the tenant was screened when they rented.
alter table home_buy_requests add column if not exists acquisition_offer_id uuid;
