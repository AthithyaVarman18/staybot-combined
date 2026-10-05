-- End-user accounts: real login/register for tenants and investors (separate from
-- the staff team password in team_auth.py). Run once in Supabase: SQL Editor ->
-- New query -> paste -> Run. Needs supabase_schema.sql and supabase_investors.sql
-- first (accounts.session_id ties into conversations/investors by the same key
-- the anonymous web chat already uses, and existing_investor/new_investor
-- accounts are linked straight to an investors row). Safe to run again.

create table if not exists accounts (
  id                uuid primary key default gen_random_uuid(),
  name              text not null,
  email             text not null unique,
  password_hash     text not null,
  password_salt     text not null,
  role              text not null check (role in ('tenant', 'new_investor', 'existing_investor')),
  session_id        text not null unique,      -- the chat session_id this account owns (see src/services/accounts.py)
  investor_id       uuid references investors(id),
  -- New Property Investor accounts see a one-time "real estate basics" page
  -- right after register/login, before landing on their dashboard (see
  -- POST_AUTH_REDIRECT / education_redirect() in src/services/accounts.py).
  -- Irrelevant for tenant / existing_investor accounts, who skip it.
  education_seen    boolean not null default false,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create index if not exists accounts_email_idx on accounts (lower(email));

-- Safe to run again on a table created before this column existed.
alter table accounts add column if not exists education_seen boolean not null default false;

drop trigger if exists accounts_set_updated_at on accounts;
create trigger accounts_set_updated_at before update on accounts
for each row execute function set_updated_at();

create table if not exists account_sessions (
  id            uuid primary key default gen_random_uuid(),
  account_id    uuid not null references accounts(id) on delete cascade,
  token_hash    text not null unique,     -- sha256 of the raw cookie token; the raw token is never stored
  created_at    timestamptz not null default now(),
  expires_at    timestamptz not null
);
create index if not exists account_sessions_account_idx on account_sessions (account_id);
create index if not exists account_sessions_expiry_idx on account_sessions (expires_at);

alter table accounts enable row level security;
alter table account_sessions enable row level security;
revoke all on accounts, account_sessions from anon, authenticated;
