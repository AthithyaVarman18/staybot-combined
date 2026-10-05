-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Safe to run more than once. Includes supabase_lead_scoring.sql, so you only need this one.

-- ---------------------------------------------------------------
-- AI Lead Score: 9-part breakdown + team verification
-- ---------------------------------------------------------------
alter table conversations add column if not exists lead_components jsonb;
alter table conversations add column if not exists human_verification text not null default 'unverified';

alter table conversations drop constraint if exists conversations_human_verification_check;
alter table conversations add constraint conversations_human_verification_check
  check (human_verification in ('unverified', 'verified', 'not_genuine'));

update conversations set lead_status = 'hot' where lead_status = 'very_hot';
update conversations set lead_status = 'nurture' where lead_status = 'cold';
alter table conversations alter column lead_status set default 'nurture';

-- ---------------------------------------------------------------
-- Outcomes: what really happened with each lead
-- ---------------------------------------------------------------
alter table conversations add column if not exists outcome text not null default 'open';
alter table conversations add column if not exists outcome_note text;
alter table conversations add column if not exists outcome_at timestamptz;
-- The score and tier at the moment the outcome was marked, so later
-- messages can't change what the report compares.
alter table conversations add column if not exists score_at_outcome int;
alter table conversations add column if not exists tier_at_outcome text;
alter table conversations add column if not exists components_at_outcome jsonb;

alter table conversations drop constraint if exists conversations_outcome_check;
alter table conversations add constraint conversations_outcome_check
  check (outcome in ('open', 'rented', 'bought', 'listed', 'lost', 'no_response'));

create index if not exists conversations_outcome_idx on conversations (outcome);
