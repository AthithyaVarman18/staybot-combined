-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Lets owners add listings through the chat. Their listings are saved as
-- 'pending' drafts and only shown to tenants after you approve them.
-- Needs supabase_properties.sql to have been run first.

alter table properties add column if not exists owner_name      text;
alter table properties add column if not exists owner_phone     text;
alter table properties add column if not exists source          text not null default 'admin';  -- 'admin' | 'owner_chat'
alter table properties add column if not exists conversation_id uuid references conversations(id) on delete set null;
alter table properties add column if not exists session_id      text;

-- Allow the new 'pending' status (drafts waiting for review).
alter table properties drop constraint if exists properties_status_check;
alter table properties add constraint properties_status_check
  check (status in ('pending', 'active', 'let', 'sold', 'hidden'));

create index if not exists properties_owner_draft_idx on properties (session_id, status);
