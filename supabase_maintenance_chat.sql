-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Adds the Maintenance-tab chat assistant's output to maintenance_tickets
-- (src/services/maintenance_chat.py): an AI summary of the tenant's chat,
-- the transcript itself, and why the AI handed it to the owner and team.
-- Needs supabase_maintenance_tickets.sql first. Until this is run, the
-- summary and transcript are folded into the ticket's summary/message.

alter table maintenance_tickets add column if not exists chat_summary text;
alter table maintenance_tickets add column if not exists chat_transcript jsonb;
alter table maintenance_tickets add column if not exists escalation_reason text;
