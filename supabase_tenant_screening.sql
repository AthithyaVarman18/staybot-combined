-- Tenant screening: a one-time mandatory intake shown to tenant accounts
-- right after their first login, before they reach the dashboard/chat.
-- The structured answers (income, employment, EMI, ...) are merged into
-- the account's existing account_portfolios.details (see src/services/portfolio.py)
-- and shown automatically in "Your portfolio" on dashboard.html - no schema
-- change needed for those. This file only adds what that reuse can't cover:
-- the one-time gate itself, and storage for the uploaded proof-of-income file.

alter table accounts add column if not exists screening_seen boolean not null default false;

create table if not exists account_documents (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references accounts(id) on delete cascade,
  doc_key text not null,
  file_name text,
  content_type text,
  size_bytes integer,
  sha256 text,
  storage_path text not null,
  status text not null default 'awaiting_review'
    check (status in ('awaiting_review', 'accepted', 'changes_required')),
  review_note text,
  reviewed_by text,
  reviewed_at timestamptz,
  uploaded_at timestamptz not null default now()
);

create index if not exists account_documents_account_id_idx on account_documents (account_id);

-- Same as every other table here: only the backend (service_role key, which
-- bypasses RLS) ever talks to Supabase directly, so this is enabled with no
-- policies - it just blocks any other access path.
alter table account_documents enable row level security;
