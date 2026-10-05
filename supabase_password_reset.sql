-- Forgot password / email password reset for customer accounts (tenants and
-- investors; see src/services/password_reset.py). Completely separate from the
-- staff team password in team_auth.py. Run once in Supabase: SQL Editor -> New
-- query -> paste -> Run. Needs supabase_accounts.sql first (account_id points at
-- accounts). Safe to run again.
--
-- Only a SHA-256 hash of each reset token is stored - the raw token exists only
-- in the email link - so a leaked database dump can't be used to reset anyone's
-- password. No password (plain or hashed) is ever stored in this table.

create table if not exists password_reset_tokens (
  id            uuid primary key default gen_random_uuid(),
  account_id    uuid not null references accounts(id) on delete cascade,
  token_hash    text not null unique,      -- sha256 of the raw token from the email link
  expires_at    timestamptz not null,      -- link stops working after this (PASSWORD_RESET_TTL_MINUTES)
  used_at       timestamptz,               -- set when the token is spent OR replaced by a newer one; null = still usable
  created_at    timestamptz not null default now()
);
create index if not exists password_reset_tokens_account_idx on password_reset_tokens (account_id);
create index if not exists password_reset_tokens_expiry_idx on password_reset_tokens (expires_at);

-- Same lock-down as accounts / account_sessions: only the backend's service_role
-- key (which bypasses RLS) can read or write it.
alter table password_reset_tokens enable row level security;
revoke all on password_reset_tokens from anon, authenticated;
