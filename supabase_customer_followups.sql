-- Run this once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- The staff member's own tracking for each customer on the Inquiries page
-- ("All chats" tab and the customer pop-up - src/services/customers.py):
-- where they are with that person, their notes, and when to follow up.
-- One row per customer (their chat session_id). Safe to run again.

create table if not exists customer_followups (
  session_id    text primary key,           -- conversations.session_id: "wa:+1919..." or an account's session
  status        text not null default 'new'
                check (status in ('new', 'contacted', 'follow_up', 'won', 'lost')),
  notes         text,
  follow_up_at  timestamptz,                -- when to get back to them
  contacted_at  timestamptz,                -- first time they were marked contacted
  updated_by    text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index if not exists customer_followups_due_idx on customer_followups (follow_up_at)
  where status in ('new', 'contacted', 'follow_up');

drop trigger if exists customer_followups_set_updated_at on customer_followups;
create trigger customer_followups_set_updated_at
before update on customer_followups
for each row execute function set_updated_at();

-- Only the backend (service_role key) can read or write it.
alter table customer_followups enable row level security;
revoke all on customer_followups from anon, authenticated;
