-- M1 row level security for the tenancy tables (DEX-11).
--
-- Design: docs/architecture.md section 7.2 ("RLS policies on every tenant table, keyed on JWT
-- claims, as a second guard under the API"). Reference: docs/schema.md.
--
-- Model:
--   * Deny by default. A table with RLS on and no policy returns no rows and accepts no writes.
--   * The caller is the JWT `sub` claim (`auth.uid()`). Access comes only from the caller's rows
--     in `public.memberships`, read at query time, so a removed membership takes effect at once.
--   * `anon` gets nothing. `authenticated` gets SELECT only. There are no INSERT, UPDATE, or
--     DELETE policies: every write goes through the control plane, which holds the service role
--     (section 4.1) and checks the caller's role in code.
--   * Our team's roles (`team_roles`) grant no tenant data access here.

-- ---------------------------------------------------------------------------------------------
-- Helpers. SECURITY DEFINER so that a policy can read `memberships` without recursing into the
-- `memberships` policy. Each helper answers a question about the caller only.
-- ---------------------------------------------------------------------------------------------

-- True when the caller has any membership in the org (org-wide or hotel-scoped).
create function private.is_org_member(target_org_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.memberships m
    where m.user_id = (select auth.uid())
      and m.org_id = target_org_id
  );
$$;

-- True when the caller may read rows of this hotel: an org-wide membership in the org, or a
-- membership for exactly this hotel. With target_hotel_id null (an org-level row), only an
-- org-wide membership matches.
create function private.can_read_hotel(target_org_id uuid, target_hotel_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.memberships m
    where m.user_id = (select auth.uid())
      and m.org_id = target_org_id
      and (m.hotel_id is null or m.hotel_id = target_hotel_id)
  );
$$;

-- True when the caller holds one of `roles` with a scope that covers the hotel (org-wide, or
-- this exact hotel). With target_hotel_id null, only an org-wide membership matches.
create function private.has_hotel_role(
  target_org_id uuid,
  target_hotel_id uuid,
  roles public.hotel_role[]
)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.memberships m
    where m.user_id = (select auth.uid())
      and m.org_id = target_org_id
      and m.role = any (roles)
      and (m.hotel_id is null or m.hotel_id = target_hotel_id)
  );
$$;

-- True when the caller is an org-wide owner or admin of an org on this plan.
create function private.can_read_plan(target_plan_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.orgs o
    join public.memberships m on m.org_id = o.id
    where o.plan_id = target_plan_id
      and m.user_id = (select auth.uid())
      and m.hotel_id is null
      and m.role in ('owner', 'admin')
  );
$$;

revoke all on function private.is_org_member(uuid) from public;
revoke all on function private.can_read_hotel(uuid, uuid) from public;
revoke all on function private.has_hotel_role(uuid, uuid, public.hotel_role[]) from public;
revoke all on function private.can_read_plan(uuid) from public;

grant usage on schema private to authenticated, service_role;
grant execute on function private.is_org_member(uuid) to authenticated, service_role;
grant execute on function private.can_read_hotel(uuid, uuid) to authenticated, service_role;
grant execute on function private.has_hotel_role(uuid, uuid, public.hotel_role[])
  to authenticated, service_role;
grant execute on function private.can_read_plan(uuid) to authenticated, service_role;

-- ---------------------------------------------------------------------------------------------
-- Read access for signed-in users. No policy targets `anon`.
-- ---------------------------------------------------------------------------------------------
grant select on table public.plans, public.orgs, public.hotels, public.memberships, public.team_roles
  to authenticated;

-- Plan limits are billing data: org-wide owners and admins of an org on the plan.
create policy plans_select_org_admins on public.plans
  for select to authenticated
  using (private.can_read_plan(id));

-- Any member of the org, including a hotel-scoped member, sees the org row.
create policy orgs_select_members on public.orgs
  for select to authenticated
  using (private.is_org_member(id));

-- Org-wide members see every hotel of the org; hotel-scoped members see their hotel only.
create policy hotels_select_members on public.hotels
  for select to authenticated
  using (private.can_read_hotel(org_id, id));

-- Each user sees their own memberships. Owners and admins also see the memberships inside
-- their scope: an org-wide admin sees the whole org, a hotel admin sees that hotel's rows.
create policy memberships_select_own_or_admin on public.memberships
  for select to authenticated
  using (
    user_id = (select auth.uid())
    or private.has_hotel_role(org_id, hotel_id, array['owner', 'admin']::public.hotel_role[])
  );

-- Our team members see their own team roles only.
create policy team_roles_select_own on public.team_roles
  for select to authenticated
  using (user_id = (select auth.uid()));
