# Database schema and row level security

This page describes the Supabase Postgres schema in `supabase/migrations/` and the rules that
every new table must follow. The design authority is
[`architecture.md` §7](architecture.md#7-multi-tenancy-and-isolation-b2b) (tenant model,
isolation, and tests). Linear: [DEX-10](https://linear.app/dextr-ai/issue/DEX-10/m1-supabase-schema-migrations-for-orgs-hotels-memberships-and-roles)
(schema) and [DEX-11](https://linear.app/dextr-ai/issue/DEX-11/m1-rls-policies-and-two-org-isolation-tests-in-ci)
(RLS and isolation tests).

| | |
|---|---|
| Migrations | `supabase/migrations/<UTC timestamp>_<name>.sql`, forward-only, reviewed by PR |
| Applied by | A human, with owner approval, per [`runbook-deploy.md` §3.1](runbook-deploy.md). Never from CI and never by an agent. |
| Tested by | The CI job **Database (Supabase migrations, RLS isolation)** on a local Supabase stack |
| Schemas | `public` (tables, exposed to the Data API), `private` (RLS helper functions, not exposed) |

## Access model

- **Deny by default.** RLS is on for every table in `public`. A table with RLS on and no policy
  returns no rows to a client.
- **Clients read, the control plane writes.** The `anon` role has no privileges. The
  `authenticated` role has `SELECT` only, filtered by the policies below. There are no `INSERT`,
  `UPDATE`, or `DELETE` policies. Every write goes through the control-plane API, which uses the
  service role (it bypasses RLS) and checks the caller's role in code (§4.1, §7.2).
- **Access comes from membership.** The caller is the JWT `sub` claim (`auth.uid()`). Policies
  look up the caller's rows in `memberships` at query time, so a removed membership takes effect
  on the next query. Policies do not read custom JWT claims.
- **Our team's roles grant no data access through RLS.** `code_reviewer` and `platform` rows in
  `team_roles` only let the user see their own role. Break-glass access goes through the control
  plane with an audit event (DEX-24), never through a hidden RLS bypass.
- **New objects start closed.** The first migration revokes the default grants that Supabase
  gives `anon` and `authenticated` on new tables, sequences, and functions in `public`, and
  `supabase/config.toml` sets `auto_expose_new_tables = false`. Each table grants what it needs
  explicitly.

## Tables

All ids are `uuid` (`gen_random_uuid()`). Every table has `created_at` and `updated_at`
(`timestamptz not null`; a trigger sets `updated_at` on every update). Deletes are hard deletes
(§7.3); there is no `deleted_at`.

| Table | Scope | Columns (besides id and timestamps) | Key rules |
|---|---|---|---|
| `plans` | Global | `code` (unique), `name`, `max_hotels`, `max_concurrent_runs`, `monthly_budget_usd`, `allowed_runtimes text[]`, `allowed_models text[]` | Limits are positive. `allowed_runtimes` is non-empty and holds only `claude_agent_sdk`, `openai_agents_sdk`, `deep_agents`, `opencode` (the `RuntimeName` ids). An empty `allowed_models` allows no model. |
| `orgs` | Tenant root (`id` is the org id) | `slug` (unique), `name`, `plan_id` | `plan_id` is required. A plan in use cannot be deleted. |
| `hotels` | Org | `org_id`, `slug` (globally unique), `name` | `slug` is unique across orgs because the repo is `hotel-<slug>` in one GitHub org (D24). `unique (org_id, id)` is the target of composite foreign keys. `org_id` cannot change. |
| `memberships` | Org, optionally one hotel | `org_id`, `hotel_id` (null = org-wide), `user_id` (`auth.users`), `role` (`owner`, `admin`, `operator`, `viewer`) | `(org_id, hotel_id)` references `hotels (org_id, id)`, so the hotel must belong to the org. One row per `(org_id, user_id, hotel_id)`, nulls not distinct. `owner` is org-wide only. `org_id` and `hotel_id` cannot change. |
| `team_roles` | Global (our team) | `user_id` (`auth.users`), `role` (`code_reviewer`, `platform`), `reason`, `expires_at` | One row per user and role. `platform` needs `expires_at` and `reason` (time-boxed break-glass, §7.1). |

Deleting an org deletes its hotels and memberships. Deleting a hotel deletes its hotel-scoped
memberships. Deleting a user in `auth.users` deletes their memberships and team roles.

Indexes: every index on an org-scoped table that has more than one column starts with `org_id`
(§7.2). `memberships (org_id, hotel_id)` serves the policy lookups and `memberships (user_id)`
serves "my memberships".

## Policies

All policies are `FOR SELECT TO authenticated`. The helpers live in `private`, run as
`SECURITY DEFINER` with `search_path = ''`, and answer questions about the caller only.

| Table | Policy | A row is visible when the caller |
|---|---|---|
| `plans` | `plans_select_org_admins` | is an org-wide `owner` or `admin` of an org on the plan (`private.can_read_plan`) |
| `orgs` | `orgs_select_members` | has any membership in the org, including a hotel-scoped one (`private.is_org_member`) |
| `hotels` | `hotels_select_members` | has an org-wide membership in the org, or a membership for this hotel (`private.can_read_hotel`) |
| `memberships` | `memberships_select_own_or_admin` | is the member, or is an `owner` or `admin` whose scope covers the row: org-wide sees the org, a hotel admin sees that hotel's rows (`private.has_hotel_role`) |
| `team_roles` | `team_roles_select_own` | is the user of the row |

Result by role, inside the caller's own org:

| Role (scope) | `plans` | `orgs` | `hotels` | `memberships` | `team_roles` |
|---|---|---|---|---|---|
| `owner` (org-wide) | own org's plan | own org | all hotels | all in the org | own only |
| `admin` (org-wide) | own org's plan | own org | all hotels | all in the org | own only |
| `operator`, `viewer` (org-wide) | none | own org | all hotels | own only | own only |
| `admin` (one hotel) | none | own org | that hotel | that hotel's rows and own | own only |
| `operator`, `viewer` (one hotel) | none | own org | that hotel | own only | own only |
| no membership, `code_reviewer`, `platform` | none | none | none | none | own only |
| `anon` | permission denied | permission denied | permission denied | permission denied | permission denied |

Nobody sees any row of another org.

## Adding a table

1. Put it in `public` with `org_id uuid not null references public.orgs (id) on delete cascade`.
   For a hotel-scoped table add `hotel_id uuid not null` and
   `foreign key (org_id, hotel_id) references public.hotels (org_id, id) on delete cascade`.
2. Start every composite index with `org_id`. Add an index that starts with `org_id` if none does.
3. Add `created_at`, `updated_at`, and the triggers `private.set_updated_at()` and
   `private.prevent_tenant_key_change()`.
4. In the same migration: `enable row level security`, `revoke all ... from public, anon,
   authenticated`, `grant all ... to service_role`, `grant select ... to authenticated`, and a
   `for select to authenticated` policy. For a hotel-scoped table the usual policy is
   `using (private.can_read_hotel(org_id, hotel_id))`.
5. Add the table to `TABLES` in `backend/tests/rls/world.py` and its rule to
   `World.expected_visible`, with seed rows for both orgs.

The tests fail until each step is done: the catalog tests compare the set of tables in `public`
with `TABLES`, and check RLS, policies, grants, `org_id`, indexes, foreign keys, and triggers.
A write policy for clients, or a policy for `anon`, also fails them; that needs a design change.

## Tests

| File | What it proves |
|---|---|
| `backend/tests/rls/test_rls_isolation.py` | Two orgs (A and B), each with two hotels, every hotel role org-wide and hotel-scoped, plus users with no membership and with each team role. For every user and table, the visible rows equal the expected set. No user of org A reads, inserts, updates, or deletes a row of org B, and no client role can write its own org either (by privilege, and by RLS alone if privileges were granted by mistake). A hotel-scoped member sees only their hotel. Also: no `sub` claim reads nothing, `anon` is denied, a removed membership takes effect at once, self-promotion fails. |
| `backend/tests/rls/test_rls_catalog.py` | Schema-wide rules from the catalog: every `public` table is covered, RLS is on with a policy, policies are select-only for `authenticated`, grants, default privileges for new objects, tenant columns, foreign keys, `org_id`-first indexes, triggers, `SECURITY DEFINER` functions pin `search_path`. |
| `backend/tests/rls/test_tenancy_constraints.py` | Data rules that hold for the service role too: cross-org hotel in a membership, hotel-scoped owner, duplicate membership, tenant key changes, time-boxed `platform`, unknown runtime ids, global hotel slugs, cascade on org delete, plan in use. |
| `backend/tests/test_supabase_migrations.py` | Offline (runs in the backend job): migration file names, RLS enabled in the same file as each `create table`, no client write grants, `supabase/config.toml` keeps `private` unexposed and seeds off, `web/` never names the service role. |

All rows are synthetic (`@example.test`) and every test runs in a transaction that is rolled back.

## Run locally

Needs Docker and the Supabase CLI 3.2.x (same as CI). These commands target the **local** stack
only. Never add `--remote`, `--linked`, or `--db-url` with a hosted URL.

```bash
# from the repo root
supabase db start                 # local Postgres on 127.0.0.1:54322
supabase db reset                 # re-apply every migration from scratch
supabase db lint --schema public,private --level warning --fail-on warning

cd backend
export HARNESS_TEST_DATABASE_URL="$(supabase status -o env | sed -n 's/^DB_URL="\(.*\)"$/\1/p')"
HARNESS_TEST_REQUIRE_DB=1 uv run pytest tests/rls

supabase stop --no-backup         # from the repo root, when done
```

Without `HARNESS_TEST_DATABASE_URL`, `uv run pytest` skips the database tests. The tests refuse
any host other than a local one (`127.0.0.1`, `localhost`, `::1`, or a local container name).

## Hosted projects

Applying migrations to staging or production is a human step with owner approval
([`runbook-deploy.md`](runbook-deploy.md) S5 and §3.1). Before the first push, match the local
API setting in the hosted project: Project settings → Data API → turn off automatic exposure of
new tables (the hosted match for `auto_expose_new_tables = false`), and keep `private` out of the exposed schemas.
