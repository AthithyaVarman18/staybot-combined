-- Staff admin account. Run once in Supabase: SQL Editor -> New query -> paste -> Run.
-- Needs supabase_accounts.sql first. Safe to run again.
--
-- The admin logs in at the hidden page /admin/login (not linked anywhere on the
-- site). The public /login page refuses this account, and /register can never
-- create an admin (it only accepts tenant / new_investor / existing_investor).
--
-- Email:    akileshkumar123@gmail.com
-- Password: 12345678
-- The password is stored the same way as every other account: PBKDF2-HMAC-SHA256
-- (260,000 rounds) with its own salt - the plain password is not in the database.

-- 1) Allow the 'admin' role on the accounts table.
alter table accounts drop constraint if exists accounts_role_check;
alter table accounts add constraint accounts_role_check
  check (role in ('tenant', 'new_investor', 'existing_investor', 'admin'));

-- 2) Create (or reset) the admin account.
insert into accounts (name, email, password_hash, password_salt, role, session_id, education_seen)
values (
  'Admin',
  'akileshkumar123@gmail.com',
  '1efb0573772ab310866dfffada52b9205e3cea9e85c3bcb72f451cf826693f6d',
  '5579e8de3b1d23bc564b203f9a018731',
  'admin',
  'admin-1b12b96257608851b35389f4',
  true
)
on conflict (email) do update set
  password_hash = excluded.password_hash,
  password_salt = excluded.password_salt,
  role          = 'admin';

-- 3) Sign out any old sessions on this email (matters only if it was a customer account before).
delete from account_sessions
where account_id = (select id from accounts where email = 'akileshkumar123@gmail.com');
