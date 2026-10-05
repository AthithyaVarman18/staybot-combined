-- One-time "welcome, you're an Existing Property Investor now" page.
-- Set to true when a New Property Investor account is moved to
-- existing_investor because they got a property
-- (investor_journey.graduate_to_existing_investor()); cleared when they
-- click through /existing-investor-welcome (POST /auth/existing-welcome-complete).
-- Accounts that registered as existing investors never get it.
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run. Safe to run again.

alter table accounts add column if not exists existing_welcome_pending boolean not null default false;
