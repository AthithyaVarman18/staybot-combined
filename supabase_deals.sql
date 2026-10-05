-- Investment deal analysis: default assumptions the team can edit, and saved deals.
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run. Safe to run again.
-- Needs supabase_mls_listings.sql (deals point at an MLS listing) and supabase_schema.sql.

create table if not exists deal_assumptions (
  id           integer primary key default 1 check (id = 1),   -- one shared set of defaults
  values       jsonb not null,
  updated_by   text not null default 'system',
  updated_at   timestamptz not null default now()
);

insert into deal_assumptions (id, values, updated_by)
values (1, '{
  "down_payment_percent": 25,
  "interest_rate_percent": 6.5,
  "loan_years": 30,
  "closing_costs_percent": 3,
  "management_percent": 8,
  "maintenance_percent": 5,
  "vacancy_percent": 5,
  "insurance_annual": 1200,
  "other_monthly": 0
}'::jsonb, 'defaults (edit in the Deals tab)')
on conflict (id) do nothing;

create table if not exists investment_deals (
  id            uuid primary key default gen_random_uuid(),
  list_number   text references mls_listings(list_number),
  person_id     uuid references onboarding_people(id),          -- the investor, once they exist
  label         text not null,
  inputs        jsonb not null,
  results       jsonb not null,
  status        text not null default 'shortlist'
                check (status in ('shortlist', 'offer_made', 'bought', 'rejected')),
  notes         text,
  created_by    text not null,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists investment_deals_status_idx on investment_deals (status, updated_at desc);

drop trigger if exists investment_deals_set_updated_at on investment_deals;
create trigger investment_deals_set_updated_at before update on investment_deals
for each row execute function set_updated_at();

alter table deal_assumptions enable row level security;
alter table investment_deals enable row level security;
revoke all on deal_assumptions, investment_deals from anon, authenticated;
