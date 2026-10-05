-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Tenant journey tracker (src/services/tenant_journeys.py, "Tenant journeys" tab in /ui).
-- Needs supabase_accounts.sql and supabase_tenant_screening.sql first. Safe to run again.
--
-- The tracker builds each tenant's journey from data the app already keeps
-- (account, screening, income documents, chats, rental applications, owner
-- approval, maintenance, owner messages). These columns add the two moments
-- that weren't timestamped yet. The tracker works without them - it just
-- shows "last login" as unknown and dates screening from the portfolio.

alter table accounts add column if not exists last_login_at timestamptz;
alter table accounts add column if not exists login_count integer not null default 0;
alter table accounts add column if not exists screening_completed_at timestamptz;

-- Tenants who already finished screening before this column existed:
-- best available date is when their screening answers were last saved.
update accounts a
   set screening_completed_at = coalesce(p.updated_at, a.updated_at)
  from account_portfolios p
 where p.account_id = a.id
   and a.role = 'tenant'
   and a.screening_seen
   and a.screening_completed_at is null;

create index if not exists accounts_role_created_idx on accounts (role, created_at desc);
