-- M1 tenancy schema: plans, orgs, hotels, memberships, team roles (DEX-10).
--
-- Design: docs/architecture.md section 7.1 (tenant model) and 7.2 (isolation layers).
-- Reference: docs/schema.md.
--
-- Rules that every later tenant table follows:
--   * `org_id` on every tenant row, plus `hotel_id` when the row is hotel-scoped.
--   * Composite indexes start with `org_id`.
--   * `created_at` / `updated_at` (trigger-maintained). No soft delete: section 7.3 asks for
--     hard delete on contract exit and data subject requests.
--   * `org_id` and `hotel_id` never change after insert (trigger-enforced).
--   * Row level security is enabled in the same migration that creates the table, and the
--     `anon` and `authenticated` roles get no table privileges here. Read access is granted
--     only together with a membership-based policy (20261118090100_tenancy_rls.sql).
--
-- This file is applied to local and CI databases only. A hosted project gets it only through
-- docs/runbook-deploy.md section 3.1, with owner approval.

-- ---------------------------------------------------------------------------------------------
-- Deny by default for objects created later in `public`.
-- Supabase grants every new table, sequence, and function in `public` to `anon` and
-- `authenticated`. The harness grants each object explicitly instead.
--
-- Only two roles can create objects in `public`: `postgres` (migrations, SQL editor) and
-- `supabase_admin` (Supabase's own superuser, used for extension installs and platform work).
-- The defaults of `postgres` are revoked below. The defaults of `supabase_admin` cannot be
-- changed from a migration: `postgres` is not a member of `supabase_admin`, so Postgres refuses
-- ("permission denied to change default privileges"), and Supabase skips event triggers that
-- `postgres` owns when `supabase_admin` runs DDL. supabase/checks/public_exposure.sql detects
-- such objects instead; CI runs it and docs/runbook-deploy.md runs it after every push.
-- ---------------------------------------------------------------------------------------------
alter default privileges for role postgres in schema public
  revoke all on tables from anon, authenticated;
alter default privileges for role postgres in schema public
  revoke all on sequences from anon, authenticated;
alter default privileges for role postgres in schema public
  revoke all on functions from anon, authenticated;
-- Postgres grants EXECUTE on every new function to PUBLIC. That default is global (not per
-- schema), so it is revoked globally for functions that `postgres` (the migration role) creates.
alter default privileges for role postgres
  revoke execute on functions from public;

-- Helpers that the API must not expose live in `private`. PostgREST serves only the schemas in
-- supabase/config.toml `[api] schemas` (public, graphql_public).
create schema if not exists private;
revoke all on schema private from public, anon, authenticated;

-- ---------------------------------------------------------------------------------------------
-- Types
-- ---------------------------------------------------------------------------------------------

-- Hotel roles (section 7.1). Per org, optionally narrowed to one hotel per membership row.
create type public.hotel_role as enum ('owner', 'admin', 'operator', 'viewer');

-- Our team's roles (section 7.1). Global, not per org. They grant no tenant data access in the
-- database; support access goes through the control plane with an audit record.
create type public.team_role as enum ('code_reviewer', 'platform');

-- ---------------------------------------------------------------------------------------------
-- Triggers shared by every table
-- ---------------------------------------------------------------------------------------------

create function private.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- A row never moves to another tenant. Moving data between orgs or hotels is an explicit,
-- reviewed migration, not an UPDATE.
create function private.prevent_tenant_key_change()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  old_row jsonb := to_jsonb(old);
  new_row jsonb := to_jsonb(new);
begin
  if old_row ? 'org_id' and (old_row ->> 'org_id') is distinct from (new_row ->> 'org_id') then
    raise exception 'org_id is immutable on %.%', tg_table_schema, tg_table_name
      using errcode = 'check_violation';
  end if;
  if old_row ? 'hotel_id' and (old_row ->> 'hotel_id') is distinct from (new_row ->> 'hotel_id') then
    raise exception 'hotel_id is immutable on %.%', tg_table_schema, tg_table_name
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

-- A hotel's slug names its repo `hotel-<slug>` (D24) and its GitHub App allowlist entry, so
-- it never changes after insert. A rename is a new hotel row or a reviewed migration.
create function private.prevent_slug_change()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.slug is distinct from old.slug then
    raise exception 'slug is immutable on %.%', tg_table_schema, tg_table_name
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

revoke all on function private.set_updated_at() from public;
revoke all on function private.prevent_tenant_key_change() from public;
revoke all on function private.prevent_slug_change() from public;

-- True when every element is a non-blank string and no element repeats. Plan limit arrays use
-- it, so a NULL, empty, or duplicate id cannot reach the run orchestrator.
create function private.is_clean_id_list(ids text[])
returns boolean
language sql
immutable
set search_path = ''
as $$
  select ids is not null
    and array_position(ids, null) is null
    and not exists (select 1 from unnest(ids) as v (id) where btrim(v.id) = '')
    and cardinality(ids) = (select count(distinct v.id) from unnest(ids) as v (id));
$$;

revoke all on function private.is_clean_id_list(text[]) from public;
-- The check constraints on `plans` call it as the writing role.
grant usage on schema private to service_role;
grant execute on function private.is_clean_id_list(text[]) to service_role;

-- ---------------------------------------------------------------------------------------------
-- plans: global catalog of limits per org (section 7.1, "Plans"). Not tenant-owned.
-- ---------------------------------------------------------------------------------------------
create table public.plans (
  id uuid primary key default gen_random_uuid(),
  code text not null unique
    check (code ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
  name text not null
    check (char_length(name) between 1 and 200),
  max_hotels integer not null
    check (max_hotels > 0),
  max_concurrent_runs integer not null
    check (max_concurrent_runs > 0),
  monthly_budget_usd numeric(12, 2) not null
    check (monthly_budget_usd >= 0),
  -- Runtime ids of `RuntimeKind` in backend/src/harness/runtimes/base.py, without `fake`:
  -- the two runtimes of D2 (docs/architecture.md v0.6). No duplicates.
  -- TODO(https://github.com/nikhilllmno/vorflux/pull/4): until that PR is on main, `RuntimeKind`
  -- still lists `deep_agents` and `opencode`. backend/tests/rls/test_tenancy_constraints.py
  -- pins this list against `RuntimeKind`.
  allowed_runtimes text[] not null default array['claude_agent_sdk']
    check (
      cardinality(allowed_runtimes) > 0
      and allowed_runtimes <@ array['claude_agent_sdk', 'openai_agents_sdk']
      and private.is_clean_id_list(allowed_runtimes)
    ),
  -- Model ids the plan allows. A model that is not in the list is refused; an empty list
  -- allows no model. The per-hotel model policy (DEX-23) narrows this further. No NULL, blank,
  -- or duplicate ids. The database does not check that a model belongs to an allowed runtime
  -- (see docs/schema.md, "Plans").
  allowed_models text[] not null default '{}'
    check (private.is_clean_id_list(allowed_models)),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.plans is
  'Global plan catalog: per-org limits (hotels, runtimes, models, concurrent runs, monthly budget). Written by the control plane only.';

-- ---------------------------------------------------------------------------------------------
-- orgs: one B2B customer account (section 2.3). The org's `id` is the `org_id` of its rows.
-- ---------------------------------------------------------------------------------------------
create table public.orgs (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique
    check (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  name text not null
    check (char_length(name) between 1 and 200),
  plan_id uuid not null references public.plans (id) on delete restrict,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index orgs_plan_id_idx on public.orgs (plan_id);

comment on table public.orgs is
  'Organization (B2B customer). Hard delete cascades to hotels and memberships (section 7.3).';

-- ---------------------------------------------------------------------------------------------
-- hotels: one property inside an org.
-- ---------------------------------------------------------------------------------------------
create table public.hotels (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  -- Globally unique: the hotel repo is `hotel-<slug>` in one GitHub org (D24, Q31).
  slug text not null unique
    check (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  name text not null
    check (char_length(name) between 1 and 200),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  -- Target for composite foreign keys from hotel-scoped tables: a row's hotel must belong to
  -- the row's org.
  constraint hotels_org_id_id_key unique (org_id, id)
);

comment on table public.hotels is 'Hotel (property) inside an org.';

-- ---------------------------------------------------------------------------------------------
-- memberships: a user's hotel role in an org, optionally narrowed to one hotel.
--   hotel_id is null  -> the role applies to every hotel in the org.
--   hotel_id not null -> the role applies to that hotel only.
-- A user can hold one org-wide row and one row per hotel in the same org.
-- ---------------------------------------------------------------------------------------------
create table public.memberships (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.orgs (id) on delete cascade,
  hotel_id uuid,
  user_id uuid not null references auth.users (id) on delete cascade,
  role public.hotel_role not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint memberships_hotel_in_org_fkey
    foreign key (org_id, hotel_id) references public.hotels (org_id, id) on delete cascade,
  constraint memberships_scope_key unique nulls not distinct (org_id, user_id, hotel_id),
  -- The owner role is org-wide: an owner holds the org account, not one property.
  constraint memberships_owner_is_org_wide check (role <> 'owner' or hotel_id is null)
);

create index memberships_org_id_hotel_id_idx on public.memberships (org_id, hotel_id);
-- RLS helpers look up the caller's rows by user_id.
create index memberships_user_id_idx on public.memberships (user_id);

comment on table public.memberships is
  'Hotel role of a user in an org (owner, admin, operator, viewer). hotel_id null = all hotels of the org.';

-- ---------------------------------------------------------------------------------------------
-- team_roles: our team's global roles (section 7.1). Not tenant-owned.
-- ---------------------------------------------------------------------------------------------
create table public.team_roles (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  role public.team_role not null,
  -- Why the grant exists. Required for `platform` (break-glass support access).
  reason text
    check (reason is null or char_length(reason) between 1 and 500),
  -- `platform` is time-boxed (section 7.1). `code_reviewer` may be open-ended.
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint team_roles_user_role_key unique (user_id, role),
  constraint team_roles_platform_is_time_boxed
    check (role <> 'platform' or (expires_at is not null and reason is not null))
);

comment on table public.team_roles is
  'Our team''s global roles (code_reviewer, platform). Grants no tenant data access through RLS.';

-- ---------------------------------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------------------------------
create trigger plans_set_updated_at before update on public.plans
  for each row execute function private.set_updated_at();
create trigger orgs_set_updated_at before update on public.orgs
  for each row execute function private.set_updated_at();
create trigger hotels_set_updated_at before update on public.hotels
  for each row execute function private.set_updated_at();
create trigger memberships_set_updated_at before update on public.memberships
  for each row execute function private.set_updated_at();
create trigger team_roles_set_updated_at before update on public.team_roles
  for each row execute function private.set_updated_at();

create trigger hotels_prevent_tenant_key_change before update on public.hotels
  for each row execute function private.prevent_tenant_key_change();
create trigger memberships_prevent_tenant_key_change before update on public.memberships
  for each row execute function private.prevent_tenant_key_change();
create trigger hotels_prevent_slug_change before update on public.hotels
  for each row execute function private.prevent_slug_change();

-- ---------------------------------------------------------------------------------------------
-- Row level security on and forced, no client privileges (deny by default).
-- FORCE applies RLS to the table owner too, unless the owner has BYPASSRLS. Today the owner is
-- `postgres`, which bypasses RLS, so FORCE guards a later owner role without BYPASSRLS.
-- `service_role` (control plane only) bypasses RLS and keeps its privileges. The set of roles
-- with BYPASSRLS is pinned in backend/tests/rls/test_rls_catalog.py.
-- ---------------------------------------------------------------------------------------------
alter table public.plans enable row level security;
alter table public.orgs enable row level security;
alter table public.hotels enable row level security;
alter table public.memberships enable row level security;
alter table public.team_roles enable row level security;
alter table public.plans force row level security;
alter table public.orgs force row level security;
alter table public.hotels force row level security;
alter table public.memberships force row level security;
alter table public.team_roles force row level security;

revoke all on table public.plans, public.orgs, public.hotels, public.memberships, public.team_roles
  from public, anon, authenticated;
grant all on table public.plans, public.orgs, public.hotels, public.memberships, public.team_roles
  to service_role;
