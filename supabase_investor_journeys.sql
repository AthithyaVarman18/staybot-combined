-- Investor workflows: Existing Property Investor and New Property Investor.
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run.
-- Needs supabase_schema.sql first (conversations table, set_updated_at()). Safe to run again.

create table if not exists investors (
  id                        uuid primary key default gen_random_uuid(),
  conversation_id           uuid references conversations(id) on delete set null,
  session_id                text,
  name                      text,
  phone                     text,
  email                     text,

  -- Classification, set automatically once enough is known (see src/services/investors.py).
  investor_type             text check (investor_type is null or investor_type in ('new', 'existing')),
  journey                   text check (journey is null or journey in ('new_investor', 'existing_investor')),
  stage                     text not null default 'lead_generation',
  stage_index               integer not null default 0,
  status                    text not null default 'active' check (status in ('active', 'paused', 'converted', 'lost')),

  -- Stored investor profile.
  investment_goals          text,
  budget                    numeric(14, 2),
  budget_currency           text not null default 'INR' check (budget_currency in ('INR', 'USD')),
  location                  text,
  investment_strategy       text,     -- e.g. buy_and_hold, rental_income, fix_and_flip, short_term_rental, appreciation, commercial
  risk_tolerance            text check (risk_tolerance is null or risk_tolerance in ('low', 'moderate', 'high')),
  timeline                  text,     -- e.g. immediate, 3_months, 6_months, 1_year, exploring
  existing_property_count   integer,
  financing_requirements    text,     -- e.g. cash, mortgage, hard_money, heloc, partner_funded, undecided

  notes                     text,
  assigned_staff            text,
  crm_synced_at             timestamptz,
  created_at                timestamptz not null default now(),
  updated_at                timestamptz not null default now()
);

create index if not exists investors_conversation_idx on investors (conversation_id);
create index if not exists investors_session_idx on investors (session_id);
create index if not exists investors_pipeline_idx on investors (journey, stage, status, updated_at desc);
create index if not exists investors_phone_idx on investors (phone);

drop trigger if exists investors_set_updated_at on investors;
create trigger investors_set_updated_at before update on investors
for each row execute function set_updated_at();

-- One property row per property the investor already owns (Existing Property
-- Investor journey's "portfolio information collection" stage) or is acquiring
-- (both journeys, from "property search & investment analysis" / "property
-- identification" onward). Kept separate from the shared `properties` listings
-- table since these describe the investor's own holdings, not marketplace listings.
create table if not exists investor_portfolio_properties (
  id                uuid primary key default gen_random_uuid(),
  investor_id       uuid not null references investors(id) on delete cascade,
  relationship      text not null default 'owned' check (relationship in ('owned', 'target', 'under_contract', 'acquired')),
  address           text,
  property_type     text,
  bedrooms          integer,
  bathrooms         integer,
  estimated_value   numeric(14, 2),
  purchase_price     numeric(14, 2),
  outstanding_mortgage numeric(14, 2),
  monthly_rent      numeric(14, 2),
  monthly_expenses  numeric(14, 2),
  condition_notes   text,
  property_id       text references properties(id),   -- linked marketplace listing, once one is chosen
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create index if not exists investor_portfolio_investor_idx on investor_portfolio_properties (investor_id, relationship);

drop trigger if exists investor_portfolio_set_updated_at on investor_portfolio_properties;
create trigger investor_portfolio_set_updated_at before update on investor_portfolio_properties
for each row execute function set_updated_at();

create table if not exists investor_stage_history (
  id            bigint generated always as identity primary key,
  investor_id   uuid not null references investors(id) on delete cascade,
  from_stage    text,
  to_stage      text not null,
  actor         text not null,
  note          text,
  created_at    timestamptz not null default now()
);
create index if not exists investor_stage_history_investor_idx on investor_stage_history (investor_id, id desc);

create table if not exists investor_activity (
  id            bigint generated always as identity primary key,
  investor_id   uuid not null references investors(id) on delete cascade,
  actor         text not null,
  action        text not null,
  details       jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now()
);
create index if not exists investor_activity_investor_idx on investor_activity (investor_id, id desc);

-- Server-only, like every other table.
alter table investors enable row level security;
alter table investor_portfolio_properties enable row level security;
alter table investor_stage_history enable row level security;
alter table investor_activity enable row level security;
revoke all on investors, investor_portfolio_properties, investor_stage_history, investor_activity from anon, authenticated;
