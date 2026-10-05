-- Per-account portfolio: the structured details the AI has picked up about
-- this person across every chat turn (see src/services/portfolio.py), shown
-- back to them on their dashboard. Run once in Supabase: SQL Editor -> New
-- query -> paste -> Run. Needs supabase_accounts.sql first (account_id
-- references accounts). Safe to run again.

create table if not exists account_portfolios (
  account_id  uuid primary key references accounts(id) on delete cascade,
  -- Tenant accounts: requirements (location, property_type, bedrooms,
  -- budget, ...). Investor/owner accounts: property_details (what they're
  -- listing/renting out). Whatever the AI last extracted, merged over time
  -- so a later turn that only answers one question never erases what was
  -- already known.
  details     jsonb not null default '{}'::jsonb,
  updated_at  timestamptz not null default now()
);

drop trigger if exists account_portfolios_set_updated_at on account_portfolios;
create trigger account_portfolios_set_updated_at before update on account_portfolios
for each row execute function set_updated_at();

alter table account_portfolios enable row level security;
revoke all on account_portfolios from anon, authenticated;
