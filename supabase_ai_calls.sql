-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Performance log: one row per customer message (speed, tokens, model).
-- Powers the Stats tab. Without it, stats only cover the time since the server started.

create table if not exists ai_calls (
  id                 bigint generated always as identity primary key,
  created_at         timestamptz not null default now(),
  channel            text,                 -- 'web' | 'whatsapp'
  outcome            text not null,        -- 'ai' | 'quick_reply' | 'error'
  quick_kind         text,                 -- greeting | thanks | acknowledgement | goodbye
  total_ms           int,                  -- whole reply, including search and saving
  ai_ms              int,                  -- time spent waiting for the AI
  model              text,
  prompt_tokens      int,
  completion_tokens  int,
  total_tokens       int,
  prompt_chars       int,
  attempts           int,                  -- models tried for this reply
  skipped_models     jsonb default '[]'::jsonb,
  session_id         text,
  error              text
);

create index if not exists ai_calls_created_idx on ai_calls (created_at desc);

alter table ai_calls enable row level security;
