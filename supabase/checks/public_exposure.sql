-- Objects in `public` that a client role (`anon`, `authenticated`) can reach outside the harness
-- rules (docs/schema.md). Expect zero rows.
--
-- Read-only. Run it as `postgres`:
--   * CI runs it on the local stack after `supabase db reset`
--     (backend/tests/rls/test_rls_catalog.py).
--   * docs/runbook-deploy.md runs it on a hosted project after every `supabase db push`.
--
-- Why a check and not only default privileges: a migration can revoke the default grants of
-- `postgres` only. Objects that `supabase_admin` creates in `public` (for example an extension
-- installed into `public`) still get every privilege for `anon` and `authenticated`, with RLS
-- off. This query finds them.
with rel as (
  select
    c.oid, 'public.' || quote_ident(c.relname) as name, c.relkind,
    c.relrowsecurity, c.relforcerowsecurity, c.relowner
  from pg_catalog.pg_class c
  where c.relnamespace = 'public'::regnamespace
    and c.relkind in ('r', 'p', 'v', 'm', 'f', 'S')
),
problems as (
  select name, relowner, 'rls disabled' as problem
  from rel where relkind in ('r', 'p') and not relrowsecurity
  union all
  select name, relowner, 'rls not forced'
  from rel where relkind in ('r', 'p') and not relforcerowsecurity
  union all
  select name, relowner, 'rls table without a policy'
  from rel
  where relkind in ('r', 'p')
    and not exists (select 1 from pg_catalog.pg_policy p where p.polrelid = rel.oid)
  union all
  select name, relowner, 'anon has a privilege'
  from rel
  where relkind <> 'S'
    and has_table_privilege('anon', oid, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
  union all
  select name, relowner, 'authenticated has a write privilege'
  from rel
  where relkind <> 'S'
    and has_table_privilege('authenticated', oid, 'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
  union all
  select name, relowner, 'client role can use the sequence'
  from rel
  where relkind = 'S'
    and (has_sequence_privilege('anon', oid, 'USAGE, SELECT, UPDATE')
      or has_sequence_privilege('authenticated', oid, 'USAGE, SELECT, UPDATE'))
  union all
  select
    'public.' || quote_ident(p.proname)
      || '(' || pg_catalog.pg_get_function_identity_arguments(p.oid) || ')',
    p.proowner, 'client role can execute the function'
  from pg_catalog.pg_proc p
  where p.pronamespace = 'public'::regnamespace
    and (has_function_privilege('anon', p.oid, 'EXECUTE')
      or has_function_privilege('authenticated', p.oid, 'EXECUTE'))
)
select name as object, pg_catalog.pg_get_userbyid(relowner) as owner, problem
from problems
order by object, problem;
