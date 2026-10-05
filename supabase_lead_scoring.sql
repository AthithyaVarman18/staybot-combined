-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- AI Lead Score: stores the 9-part score breakdown and the team's human
-- verification on each conversation, and moves old statuses to the new tiers.

alter table conversations add column if not exists lead_components jsonb;
alter table conversations add column if not exists human_verification text not null default 'unverified';

alter table conversations drop constraint if exists conversations_human_verification_check;
alter table conversations add constraint conversations_human_verification_check
  check (human_verification in ('unverified', 'verified', 'not_genuine'));

-- Old tiers -> new tiers (very_hot -> hot, cold -> nurture).
update conversations set lead_status = 'hot' where lead_status = 'very_hot';
update conversations set lead_status = 'nurture' where lead_status = 'cold';
alter table conversations alter column lead_status set default 'nurture';
