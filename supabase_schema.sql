-- Run this once in your Supabase project: Dashboard -> SQL Editor -> New query -> Run.
-- Creates the two tables the app needs: conversations (one row per
-- browser+listing chat thread) and messages (every turn in that thread).

create extension if not exists pgcrypto;

create table if not exists conversations (
  id                uuid primary key default gen_random_uuid(),
  session_id        text not null,          -- random id generated in the browser, stored in localStorage
  listing_id        text,                   -- null = "general enquiry" (no property selected)
  listing_title     text,
  persona           text default 'tenant',
  property_context  jsonb default '{}'::jsonb,
  role              text,                   -- 'tenant' | 'owner', set from the latest AI analysis
  intent            text,
  intent_score      int default 0,
  lead_status       text default 'cold',    -- cold | warm | hot | very_hot
  summary           text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index if not exists conversations_session_idx on conversations (session_id, listing_id);
create index if not exists conversations_score_idx on conversations (intent_score desc, updated_at desc);

create table if not exists messages (
  id                uuid primary key default gen_random_uuid(),
  conversation_id   uuid not null references conversations(id) on delete cascade,
  role              text not null,          -- 'user' | 'assistant'
  content           text not null,
  analysis          jsonb,                  -- full /analyze response, for assistant messages
  created_at        timestamptz not null default now()
);

create index if not exists messages_conversation_idx on messages (conversation_id, created_at);

-- Keep updated_at current automatically whenever a conversation row changes.
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists conversations_set_updated_at on conversations;
create trigger conversations_set_updated_at
before update on conversations
for each row execute function set_updated_at();

-- Lock the tables down from the public/anon key. The backend uses the
-- service_role key (set as SUPABASE_SERVICE_KEY in .env), which bypasses
-- RLS entirely, so no policies need to be added for the app to work.
alter table conversations enable row level security;
alter table messages enable row level security;
