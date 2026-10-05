-- Staybot structured requirement/profile layer.
-- Additive: does not alter conversations, enquiries, properties, offers,
-- negotiations or inspections. Run after supabase_accounts.sql.

create table if not exists user_requirements (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references accounts(id) on delete cascade,
  role             text not null check (role in ('tenant','buyer','new_investor','existing_investor','owner')),
  profile_type     text not null check (profile_type in ('tenant','buyer','new_investor','existing_investor','owner')),
  requirements_json jsonb not null default '{}'::jsonb,
  version          integer not null default 1 check (version > 0),
  created_by       text not null default 'user',
  is_active        boolean not null default true,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (user_id, profile_type, is_active) deferrable initially immediate
);

-- The unique constraint above intentionally allows only one active profile per
-- user/profile_type; inactive history remains available.
create index if not exists user_requirements_user_idx on user_requirements(user_id, updated_at desc);
create index if not exists user_requirements_role_idx on user_requirements(role, updated_at desc);

create table if not exists requirement_versions (
  id                uuid primary key default gen_random_uuid(),
  requirement_id    uuid not null references user_requirements(id) on delete cascade,
  version           integer not null check (version > 0),
  requirements_json jsonb not null default '{}'::jsonb,
  created_by        text not null default 'user',
  created_at        timestamptz not null default now(),
  unique (requirement_id, version)
);

create index if not exists requirement_versions_req_idx on requirement_versions(requirement_id, version desc);

alter table user_requirements enable row level security;
alter table requirement_versions enable row level security;
revoke all on user_requirements, requirement_versions from anon, authenticated;

drop trigger if exists user_requirements_set_updated_at on user_requirements;
create trigger user_requirements_set_updated_at before update on user_requirements
for each row execute function set_updated_at();

-- If an earlier draft of this feature was installed, expand its profile
-- constraints without touching any stored JSON or version history.
alter table user_requirements drop constraint if exists user_requirements_role_check;
alter table user_requirements add constraint user_requirements_role_check
  check (role in ('tenant','buyer','new_investor','existing_investor','owner'));
alter table user_requirements drop constraint if exists user_requirements_profile_type_check;
alter table user_requirements add constraint user_requirements_profile_type_check
  check (profile_type in ('tenant','buyer','new_investor','existing_investor','owner'));
