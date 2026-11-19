"""Schema-wide guarantees read from the Postgres catalog (DEX-10, DEX-11).

These tests fail when a new table or migration breaks a tenancy rule, before any data exists:
a table without RLS (enabled and forced) or without a policy, a table that the isolation tests
do not cover, a client write grant, a tenant table without `org_id`, an index that does not
start with `org_id`, a new role that bypasses RLS, or an object in `public` that
supabase/checks/public_exposure.sql reports.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from .world import TABLES

Conn = psycopg.Connection[Any]

# Tables without `org_id`. `orgs` is the tenant root (its `id` is the org id). The others are
# global and written by the control plane only. Adding a table here needs a review of why it
# has no tenant.
TENANT_ROOT = "orgs"
GLOBAL_TABLES = frozenset({"plans", "team_roles"})
CLIENT_ROLES = ("anon", "authenticated")
ALL_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
EXPOSURE_CHECK = Path(__file__).resolve().parents[3] / "supabase" / "checks" / "public_exposure.sql"

# Roles that skip every RLS policy, on the pinned local stack (Supabase CLI and Postgres image in
# .github/workflows/ci.yml). `postgres` owns the tables and the RLS helpers; `service_role` is
# the control plane; the `supabase_*` roles are Supabase's own. A change to this set needs a
# security review: a role here reads every tenant's rows.
BYPASSRLS_ROLES = frozenset(
    {"postgres", "service_role", "supabase_admin", "supabase_etl_admin", "supabase_read_only_user"}
)
# Roles that can create objects in `public`. Each one needs a default-privileges answer in
# docs/schema.md: `postgres` defaults are revoked by the migrations; `supabase_admin` defaults
# cannot be changed by `postgres`, so the exposure check covers its objects.
PUBLIC_CREATOR_ROLES = frozenset({"postgres", "supabase_admin"})


def rows(conn: Conn, query: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    return conn.execute(query, params).fetchall()


def public_tables(conn: Conn) -> set[str]:
    found = rows(
        conn,
        "select c.relname from pg_class c where c.relnamespace = 'public'::regnamespace"
        " and c.relkind in ('r', 'p')",
    )
    return {name for (name,) in found}


def columns(conn: Conn, table: str) -> dict[str, tuple[str, bool]]:
    found = rows(
        conn,
        "select a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull"
        " from pg_attribute a where a.attrelid = %s::regclass and a.attnum > 0"
        " and not a.attisdropped",
        (f"public.{table}",),
    )
    return {name: (type_, notnull) for name, type_, notnull in found}


def test_every_public_table_is_covered_by_the_isolation_tests(conn: Conn) -> None:
    # A new table must be added to world.TABLES with its expected access rules.
    assert public_tables(conn) == set(TABLES)


def test_no_views_or_materialized_views_bypass_rls(conn: Conn) -> None:
    # Views run with the owner's rights unless security_invoker is set. Add one only with a test.
    found = rows(
        conn,
        "select relname from pg_class where relnamespace = 'public'::regnamespace"
        " and relkind in ('v', 'm')",
    )
    assert found == []


@pytest.mark.parametrize("table", TABLES)
def test_rls_is_enabled_with_at_least_one_policy(conn: Conn, table: str) -> None:
    (enabled,) = rows(
        conn, "select relrowsecurity from pg_class where oid = %s::regclass", (f"public.{table}",)
    )[0]
    assert enabled, f"RLS is off on public.{table}"
    policies = rows(
        conn, "select polname from pg_policy where polrelid = %s::regclass", (f"public.{table}",)
    )
    assert policies, f"public.{table} has no RLS policy"


@pytest.mark.parametrize("table", TABLES)
def test_rls_is_forced(conn: Conn, table: str) -> None:
    # FORCE applies RLS to a table owner without BYPASSRLS as well.
    (forced,) = rows(
        conn,
        "select relforcerowsecurity from pg_class where oid = %s::regclass",
        (f"public.{table}",),
    )[0]
    assert forced, f"RLS is not forced on public.{table}"


def test_only_the_pinned_roles_bypass_rls(conn: Conn) -> None:
    found = {name for (name,) in rows(conn, "select rolname from pg_roles where rolbypassrls")}
    assert found == BYPASSRLS_ROLES


def test_only_the_pinned_roles_can_create_in_public(conn: Conn) -> None:
    found = {
        name
        for (name,) in rows(
            conn,
            "select rolname from pg_roles where left(rolname, 3) <> 'pg_'"
            " and has_schema_privilege(oid, 'public', 'CREATE')",
        )
    }
    assert found == PUBLIC_CREATOR_ROLES


def exposure_report(conn: Conn) -> list[tuple[str, str, str]]:
    return [
        (str(obj), str(owner), str(problem))
        for obj, owner, problem in conn.execute(EXPOSURE_CHECK.read_text()).fetchall()
    ]


def test_public_exposure_check_is_clean(conn: Conn) -> None:
    # The same query runs on a hosted project after every push (docs/runbook-deploy.md).
    assert exposure_report(conn) == []


def test_exposure_check_reports_objects_that_supabase_admin_creates_in_public(
    connect_as: Callable[[str], Conn],
) -> None:
    # `postgres` cannot change the default privileges of `supabase_admin`, so a table that
    # `supabase_admin` creates in `public` still gets client grants with RLS off. Pin that
    # behaviour, and prove that the exposure check reports the table and the sequence.
    with connect_as("supabase_admin") as admin:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connect_as("postgres") as other:
                other.execute(
                    "alter default privileges for role supabase_admin in schema public"
                    " revoke all on tables from anon, authenticated"
                )
        with admin.transaction(force_rollback=True):
            admin.execute("create table public.zz_admin_probe (id int)")
            admin.execute("create sequence public.zz_admin_seq")
            admin.execute("set local role postgres")
            report = exposure_report(admin)
    assert ("public.zz_admin_probe", "supabase_admin", "rls disabled") in report
    assert ("public.zz_admin_probe", "supabase_admin", "anon has a privilege") in report
    assert (
        "public.zz_admin_probe",
        "supabase_admin",
        "authenticated has a write privilege",
    ) in report
    assert (
        "public.zz_admin_seq",
        "supabase_admin",
        "client role can use the sequence",
    ) in report


def test_policies_are_select_only_for_authenticated(conn: Conn) -> None:
    # Writes go through the control plane (service role). A write policy, or a policy for
    # `anon` or PUBLIC, needs a design change and a new test.
    found = rows(
        conn,
        "select c.relname, p.polname, p.polcmd, p.polpermissive,"
        " array(select r.rolname from pg_roles r where r.oid = any(p.polroles) order by 1)"
        " from pg_policy p join pg_class c on c.oid = p.polrelid"
        " where c.relnamespace = 'public'::regnamespace",
    )
    assert found
    for table, name, cmd, permissive, roles in found:
        assert cmd == "r", f"{table}.{name} is not a SELECT policy"
        assert permissive, f"{table}.{name}"
        assert roles == ["authenticated"], f"{table}.{name} applies to {roles}"


@pytest.mark.parametrize("table", TABLES)
def test_client_roles_get_select_only(conn: Conn, table: str) -> None:
    for role in CLIENT_ROLES:
        granted = {
            privilege
            for privilege in ALL_TABLE_PRIVILEGES
            if rows(
                conn, "select has_table_privilege(%s, %s, %s)", (role, f"public.{table}", privilege)
            )[0][0]
        }
        expected = {"SELECT"} if role == "authenticated" else set()
        assert granted == expected, f"{role} on public.{table}: {sorted(granted)}"


def test_new_objects_in_public_are_not_granted_to_client_roles(conn: Conn) -> None:
    # Default privileges: a table or function created later starts with no client access.
    # The migrations revoke them for `postgres`, the role that runs migrations.
    (user,) = rows(conn, "select current_user")[0]
    assert user == "postgres"
    conn.execute("create table public.zz_probe (id int)")
    conn.execute("create sequence public.zz_probe_seq")
    conn.execute("create function public.zz_probe_fn() returns int language sql as 'select 1'")
    assert ("public.zz_probe", "postgres", "rls disabled") in exposure_report(conn)
    for role in CLIENT_ROLES:
        for privilege in ALL_TABLE_PRIVILEGES:
            assert not rows(
                conn, "select has_table_privilege(%s, 'public.zz_probe', %s)", (role, privilege)
            )[0][0], (role, privilege)
        assert not rows(
            conn, "select has_function_privilege(%s, 'public.zz_probe_fn()', 'EXECUTE')", (role,)
        )[0][0], role
        assert not rows(
            conn,
            "select has_sequence_privilege(%s, 'public.zz_probe_seq', 'USAGE, SELECT, UPDATE')",
            (role,),
        )[0][0], role


def test_private_schema_is_closed_to_anon(conn: Conn) -> None:
    assert not rows(conn, "select has_schema_privilege('anon', 'private', 'USAGE')")[0][0]


def test_security_definer_functions_pin_search_path(conn: Conn) -> None:
    found = rows(
        conn,
        "select n.nspname || '.' || p.proname, p.proconfig from pg_proc p"
        " join pg_namespace n on n.oid = p.pronamespace"
        " where p.prosecdef and n.nspname in ('public', 'private')",
    )
    assert found, "expected the RLS helper functions"
    for name, config in found:
        assert config and 'search_path=""' in config, f"{name} must set search_path = ''"
    in_public = [name for name, _ in found if name.startswith("public.")]
    assert in_public == [], "SECURITY DEFINER functions belong in the private schema"


@pytest.mark.parametrize("table", sorted(set(TABLES) - GLOBAL_TABLES - {TENANT_ROOT}))
def test_tenant_table_carries_org_id_with_org_first_indexes(conn: Conn, table: str) -> None:
    cols = columns(conn, table)
    assert cols.get("org_id") == ("uuid", True), f"public.{table} needs org_id uuid not null"

    fk_targets = rows(
        conn,
        "select confrelid::regclass::text,"
        " array(select attname from pg_attribute where attrelid = conrelid"
        "   and attnum = any(conkey) order by array_position(conkey, attnum)),"
        " array(select attname from pg_attribute where attrelid = confrelid"
        "   and attnum = any(confkey) order by array_position(confkey, attnum))"
        " from pg_constraint where conrelid = %s::regclass and contype = 'f'",
        (f"public.{table}",),
    )
    assert ("orgs", ["org_id"], ["id"]) in fk_targets, f"public.{table}.org_id -> orgs(id)"
    if "hotel_id" in cols:
        # The hotel must belong to the row's org.
        assert ("hotels", ["org_id", "hotel_id"], ["org_id", "id"]) in fk_targets, table

    indexes = rows(
        conn,
        "select i.indexrelid::regclass::text,"
        " array(select a.attname from unnest(i.indkey) with ordinality k(attnum, n)"
        "   join pg_attribute a on a.attrelid = i.indrelid and a.attnum = k.attnum order by k.n)"
        " from pg_index i where i.indrelid = %s::regclass",
        (f"public.{table}",),
    )
    assert any(cols_[0] == "org_id" for _, cols_ in indexes), f"no org_id index on {table}"
    for name, cols_ in indexes:
        if len(cols_) > 1:
            assert cols_[0] == "org_id", f"composite index {name} must start with org_id"


@pytest.mark.parametrize("table", sorted(set(TABLES) - GLOBAL_TABLES - {TENANT_ROOT}))
def test_tenant_keys_are_immutable(conn: Conn, table: str) -> None:
    triggers = rows(
        conn,
        "select t.tgname from pg_trigger t join pg_proc p on p.oid = t.tgfoid"
        " where t.tgrelid = %s::regclass and p.proname = 'prevent_tenant_key_change'",
        (f"public.{table}",),
    )
    assert triggers, f"public.{table} needs the prevent_tenant_key_change trigger"


@pytest.mark.parametrize("table", TABLES)
def test_audit_timestamps(conn: Conn, table: str) -> None:
    cols = columns(conn, table)
    for name in ("created_at", "updated_at"):
        assert cols.get(name) == ("timestamp with time zone", True), f"{table}.{name}"
    triggers = rows(
        conn,
        "select t.tgname from pg_trigger t join pg_proc p on p.oid = t.tgfoid"
        " where t.tgrelid = %s::regclass and p.proname = 'set_updated_at'",
        (f"public.{table}",),
    )
    assert triggers, f"public.{table} needs the set_updated_at trigger"
    assert "deleted_at" not in cols, "no soft delete: section 7.3 asks for hard delete"
