"""Two-org isolation through row level security (DEX-11, docs/architecture.md section 7.4).

Every test runs as the Supabase API role that a client request gets (`authenticated` with the
JWT claims set, or `anon`), on the synthetic world from `world.py`, and is rolled back.

Contract:
* A user reads only rows that a membership gives them (`World.expected_visible`), so a user
  in org A never reads a row of org B, and a hotel-scoped member reads only their hotel.
* No client role writes any tenancy row, in its own org or another org. Two layers are tested
  separately: table privileges (the client has SELECT only), and RLS on its own (the test
  grants write privileges inside the rolled-back transaction, and RLS must still deny).
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from psycopg import errors, sql

from .world import (
    HOTEL_SCOPED_ROLES,
    NO_MEMBERSHIP_USERS,
    TABLES,
    World,
    as_role,
    as_user,
    member_names,
)

Conn = psycopg.Connection[Any]

READ_ACTORS = [*member_names("A"), *member_names("B"), *NO_MEMBERSHIP_USERS]
# Writers come from org A; target org B is "another org", target org A is "own org".
WRITE_ACTORS = [*member_names("A"), *NO_MEMBERSHIP_USERS]
WRITE_OPS = ("insert", "update", "delete")
TARGET_ORGS = ("A", "B")


def select_ids(conn: Conn, table: str, where_id: uuid.UUID | None = None) -> set[uuid.UUID]:
    query = sql.SQL("select id from public.{}").format(sql.Identifier(table))
    params: tuple[object, ...] = ()
    if where_id is not None:
        query += sql.SQL(" where id = %s")
        params = (where_id,)
    with conn.cursor() as cur:
        cur.execute(query, params)
        return {row[0] for row in cur.fetchall()}


# --- Reads ---------------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("actor", READ_ACTORS)
def test_user_reads_exactly_the_rows_their_memberships_allow(
    conn: Conn, world: World, actor: str, table: str
) -> None:
    user_id = world.users[actor]
    with as_user(conn, user_id):
        visible = select_ids(conn, table)
    assert visible == world.expected_visible(table, user_id)


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("actor", member_names("A"))
def test_org_a_user_cannot_read_org_b_rows_even_by_id(
    conn: Conn, world: World, actor: str, table: str
) -> None:
    org_b_ids = world.ids_of_org(table, "B")
    with as_user(conn, world.users[actor]):
        assert select_ids(conn, table).isdisjoint(org_b_ids)
        for row_id in org_b_ids:
            assert select_ids(conn, table, where_id=row_id) == set()


@pytest.mark.parametrize("role", HOTEL_SCOPED_ROLES)
def test_hotel_scoped_member_sees_only_their_hotel(conn: Conn, world: World, role: str) -> None:
    org = world.org("A")
    hotel_1, hotel_2 = org.hotels
    with as_user(conn, world.users[f"A_hotel_{role}"]):
        assert select_ids(conn, "hotels") == {hotel_1.id}
        assert select_ids(conn, "hotels", where_id=hotel_2.id) == set()
        assert select_ids(conn, "orgs") == {org.id}
        # Plan limits are for org-wide owners and admins only.
        assert select_ids(conn, "plans") == set()


def test_hotel_admin_sees_memberships_of_their_hotel_only(conn: Conn, world: World) -> None:
    org = world.org("A")
    hotel_1 = org.hotels[0]
    with as_user(conn, world.users["A_hotel_admin"]):
        visible = select_ids(conn, "memberships")
    expected = {m.id for m in world.memberships if m.hotel_id == hotel_1.id}
    assert visible == expected
    assert all(m.hotel_id == hotel_1.id for m in world.memberships if m.id in visible)


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_non_admin_sees_only_their_own_membership(conn: Conn, world: World, role: str) -> None:
    user_id = world.users[f"A_{role}"]
    with as_user(conn, user_id):
        visible = select_ids(conn, "memberships")
    assert visible == {m.id for m in world.memberships_of(user_id)}


def test_org_wide_admin_sees_every_membership_of_their_org(conn: Conn, world: World) -> None:
    with as_user(conn, world.users["A_admin"]):
        visible = select_ids(conn, "memberships")
    assert visible == world.ids_of_org("memberships", "A")


@pytest.mark.parametrize("table", TABLES)
def test_jwt_without_sub_claim_reads_nothing(conn: Conn, table: str) -> None:
    with as_user(conn, None):
        assert select_ids(conn, table) == set()


@pytest.mark.parametrize("table", TABLES)
def test_anon_has_no_access(conn: Conn, table: str) -> None:
    with as_role(conn, "anon"), pytest.raises(errors.InsufficientPrivilege):
        select_ids(conn, table)


def test_removed_membership_takes_effect_at_once(conn: Conn, world: World) -> None:
    user_id = world.users["A_admin"]
    conn.execute("delete from public.memberships where user_id = %s", (user_id,))
    for table in TABLES:
        with as_user(conn, user_id):
            assert select_ids(conn, table) == set(), table


@pytest.mark.parametrize("table", TABLES)
def test_service_role_bypasses_rls_for_the_control_plane(
    conn: Conn, world: World, table: str
) -> None:
    with as_role(conn, "service_role", {"role": "service_role"}):
        assert world.all_ids(table) <= select_ids(conn, table)


# --- Writes --------------------------------------------------------------------------------


def write_statement(
    world: World, table: str, op: str, actor: str, target_org: str
) -> tuple[sql.Composed, tuple[object, ...]]:
    """A valid write that targets `target_org`'s rows, so only access control can stop it."""
    org = world.org(target_org)
    user_id = world.users[actor]
    name = sql.Identifier(table)
    if op == "insert":
        suffix = uuid.uuid4().hex[:8]
        inserts: dict[str, tuple[str, tuple[object, ...]]] = {
            "plans": (
                "insert into public.{} (code, name, max_hotels, max_concurrent_runs,"
                " monthly_budget_usd) values (%s, 'Stolen', 1, 1, 0)",
                (f"x-{suffix}",),
            ),
            "orgs": (
                "insert into public.{} (slug, name, plan_id) values (%s, 'Stolen', %s)",
                (f"x-{suffix}", org.plan_id),
            ),
            "hotels": (
                "insert into public.{} (org_id, slug, name) values (%s, %s, 'Stolen')",
                (org.id, f"x-{suffix}"),
            ),
            # Self-escalation: make myself owner of the target org.
            "memberships": (
                "insert into public.{} (org_id, user_id, role) values (%s, %s, 'owner')",
                (org.id, user_id),
            ),
            # Self-escalation: give myself a team role I do not hold.
            "team_roles": (
                "insert into public.{} (user_id, role, reason, expires_at)"
                " values (%s, %s, 'self-grant', now() + interval '1 day')",
                (user_id, "platform" if actor == "code_reviewer" else "code_reviewer"),
            ),
        }
        text, params = inserts[table]
        return sql.SQL(text).format(name), params

    where, params = target_filter(world, table, actor, target_org)
    text = (
        "update public.{} set updated_at = now() where {}"
        if op == "update"
        else ("delete from public.{} where {}")
    )
    return sql.SQL(text).format(name, where), params


def target_filter(
    world: World, table: str, actor: str, target_org: str
) -> tuple[sql.SQL, tuple[object, ...]]:
    """The rows of `target_org` in `table` that an update or delete aims at."""
    org = world.org(target_org)
    user_id = world.users[actor]
    filters: dict[str, tuple[str, tuple[object, ...]]] = {
        "plans": ("id = %s", (org.plan_id,)),
        "orgs": ("id = %s", (org.id,)),
        "hotels": ("org_id = %s", (org.id,)),
        "memberships": ("org_id = %s", (org.id,)),
        # Team roles have no org: own rows for target A, other users' rows for target B.
        "team_roles": ("user_id = %s" if target_org == "A" else "user_id <> %s", (user_id,)),
    }
    where, params = filters[table]
    return sql.SQL(where), params


def count_target_rows(conn: Conn, world: World, table: str, actor: str, target: str) -> int:
    where, params = target_filter(world, table, actor, target)
    query = sql.SQL("select count(*) from public.{} where {}").format(sql.Identifier(table), where)
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize("target", TARGET_ORGS)
@pytest.mark.parametrize("op", WRITE_OPS)
@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("actor", WRITE_ACTORS)
def test_client_role_has_no_write_privilege(
    conn: Conn, world: World, actor: str, table: str, op: str, target: str
) -> None:
    statement, params = write_statement(world, table, op, actor, target)
    with as_user(conn, world.users[actor]), pytest.raises(errors.InsufficientPrivilege):
        conn.execute(statement, params)


@pytest.mark.parametrize("target", TARGET_ORGS)
@pytest.mark.parametrize("op", WRITE_OPS)
@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("actor", WRITE_ACTORS)
def test_rls_alone_denies_writes_if_privileges_were_granted(
    conn: Conn, world: World, actor: str, table: str, op: str, target: str
) -> None:
    # Simulate a future mistake: a migration grants write privileges to `authenticated`.
    # The grant is rolled back with the test. RLS must still deny every write.
    conn.execute(
        sql.SQL("grant insert, update, delete on public.{} to authenticated").format(
            sql.Identifier(table)
        )
    )
    before = count_target_rows(conn, world, table, actor, target) if op != "insert" else 0
    statement, params = write_statement(world, table, op, actor, target)
    with as_user(conn, world.users[actor]):
        if op == "insert":
            with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
                conn.execute(statement, params)
        else:
            assert conn.execute(statement, params).rowcount == 0
    if op != "insert":
        assert count_target_rows(conn, world, table, actor, target) == before


def test_member_cannot_promote_their_own_membership(conn: Conn, world: World) -> None:
    user_id = world.users["A_hotel_viewer"]
    conn.execute("grant update on public.memberships to authenticated")
    with as_user(conn, user_id):
        updated = conn.execute(
            "update public.memberships set role = 'admin' where user_id = %s", (user_id,)
        ).rowcount
    assert updated == 0
    row = conn.execute("select role::text from public.memberships where user_id = %s", (user_id,))
    assert row.fetchall() == [("viewer",)]
