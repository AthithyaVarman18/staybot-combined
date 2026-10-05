-- Investors: people who want to BUY property (to rent out or resell).
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run. Safe to run again.
-- Needs supabase_schema.sql, supabase_lease_onboarding.sql (onboarding_people) and
-- supabase_mls_listings.sql / supabase_deals.sql for matching.

create table if not exists investor_profiles (
  id                 uuid primary key default gen_random_uuid(),
  person_id          uuid unique references onboarding_people(id),
  conversation_id    uuid unique references conversations(id) on delete set null,
  full_name          text not null,
  whatsapp           text,
  email              text,
  cash_available     numeric(14, 2),
  financing          text check (financing is null or financing in ('cash', 'mortgage', 'unsure')),
  pre_approved       text check (pre_approved is null or pre_approved in ('yes', 'no', 'unsure')),
  goal               text check (goal is null or goal in ('income', 'growth', 'both')),
  areas              text[] not null default '{}',
  property_type      text,
  min_bedrooms       integer,
  timeline           text check (timeline is null or timeline in ('now', '3_months', '6_months', '12_months', 'unsure')),
  experience         text check (experience is null or experience in ('first_time', 'owns_some', 'owns_many')),
  notes              text,
  score              integer not null default 0,
  score_parts        jsonb not null default '{}'::jsonb,
  tier               text not null default 'nurture' check (tier in ('hot', 'warm', 'nurture', 'unqualified')),
  status             text not null default 'new'
                     check (status in ('new', 'qualified', 'advisory', 'buying', 'bought', 'not_now')),
  source             text not null default 'chat',
  created_by         text not null default 'ai_chat',
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);
create index if not exists investor_profiles_tier_idx on investor_profiles (tier, updated_at desc);

create table if not exists investor_activity (
  id           bigint generated always as identity primary key,
  investor_id  uuid not null references investor_profiles(id),
  actor        text not null,
  action       text not null,
  details      jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now()
);
create index if not exists investor_activity_idx on investor_activity (investor_id, id desc);

drop trigger if exists investor_profiles_set_updated_at on investor_profiles;
create trigger investor_profiles_set_updated_at before update on investor_profiles
for each row execute function set_updated_at();

alter table investor_profiles enable row level security;
alter table investor_activity enable row level security;
revoke all on investor_profiles, investor_activity from anon, authenticated;

-- Deals can point at the investor they were prepared for.
alter table investment_deals add column if not exists investor_id uuid references investor_profiles(id);

-- More of the investor's brief, collected by the AI over the conversation.
alter table investor_profiles add column if not exists target_cash_flow numeric(12, 2);
alter table investor_profiles add column if not exists max_loan numeric(14, 2);
alter table investor_profiles add column if not exists management_preference text
  check (management_preference is null or management_preference in ('self', 'company', 'unsure'));
alter table investor_profiles add column if not exists condition_preference text
  check (condition_preference is null or condition_preference in ('turnkey', 'light_work', 'heavy_work', 'unsure'));
alter table investor_profiles add column if not exists hold_years integer;
alter table investor_profiles add column if not exists ownership text
  check (ownership is null or ownership in ('personal', 'company', 'unsure'));
alter table investor_profiles add column if not exists last_matched_at timestamptz;

-- This project already had an older `investor_activity` table whose foreign key
-- points at a different `investors` table, so every activity insert failed with
-- a 409. The chat log lives in its own table instead.
create table if not exists investor_profile_activity (
  id           bigint generated always as identity primary key,
  investor_id  uuid not null references investor_profiles(id) on delete cascade,
  actor        text not null,
  action       text not null,
  details      jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now()
);
create index if not exists investor_profile_activity_idx on investor_profile_activity (investor_id, id desc);
alter table investor_profile_activity enable row level security;
revoke all on investor_profile_activity from anon, authenticated;

-- Remembers which brief the last chat match list was built from, so the same
-- homes are not repeated on every following turn.
alter table investor_profiles add column if not exists last_match_signature text;

-- How much risk the investor is comfortable with (asked in chat, also used by
-- the guided journey in src/services/investor_journey.py).
alter table investor_profiles add column if not exists risk_tolerance text
  check (risk_tolerance is null or risk_tolerance in ('low', 'moderate', 'high'));

-- New build vs older home, and how they plan to make money from it (asked in
-- chat alongside risk_tolerance - see INVESTORS in src/prompts/system_prompt.py).
alter table investor_profiles add column if not exists home_age_preference text
  check (home_age_preference is null or home_age_preference in ('new', 'older', 'any'));
alter table investor_profiles add column if not exists strategy text
  check (strategy is null or strategy in ('long_term_rental', 'short_term_rental', 'fix_and_flip', 'buy_and_hold', 'unsure'));
-- "Not sure" is a real answer to the risk question (the chat asks it for every investor).
alter table investor_profiles drop constraint if exists investor_profiles_risk_tolerance_check;
alter table investor_profiles add constraint investor_profiles_risk_tolerance_check
  check (risk_tolerance is null or risk_tolerance in ('low', 'moderate', 'high', 'unsure'));
