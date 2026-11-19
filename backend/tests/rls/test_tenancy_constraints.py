"""Data rules of the tenancy schema (DEX-10), checked as the database owner.

The control plane writes with the service role, which bypasses RLS. These constraints still
hold for it, so a bug in the API cannot put a row in the wrong tenant.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from psycopg import errors

from harness.runtimes.base import RuntimeKind

from .world import World

Conn = psycopg.Connection[Any]


def new_user(conn: Conn) -> uuid.UUID:
    user_id = uuid.uuid4()
    conn.execute(
        "insert into auth.users (id, email, aud, role)"
        " values (%s, %s, 'authenticated', 'authenticated')",
        (user_id, f"c-{user_id.hex[:8]}@example.test"),
    )
    return user_id


def test_membership_hotel_must_belong_to_the_membership_org(conn: Conn, world: World) -> None:
    hotel_b = world.org("B").hotels[0]
    with pytest.raises(errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            "insert into public.memberships (org_id, hotel_id, user_id, role)"
            " values (%s, %s, %s, 'viewer')",
            (world.org("A").id, hotel_b.id, new_user(conn)),
        )


def test_owner_role_is_org_wide(conn: Conn, world: World) -> None:
    org = world.org("A")
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(
            "insert into public.memberships (org_id, hotel_id, user_id, role)"
            " values (%s, %s, %s, 'owner')",
            (org.id, org.hotels[0].id, new_user(conn)),
        )


def test_one_org_wide_membership_per_user_and_org(conn: Conn, world: World) -> None:
    user_id = world.users["A_viewer"]
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        conn.execute(
            "insert into public.memberships (org_id, user_id, role) values (%s, %s, 'admin')",
            (world.org("A").id, user_id),
        )


def test_hotel_cannot_move_to_another_org(conn: Conn, world: World) -> None:
    with pytest.raises(errors.CheckViolation, match="org_id is immutable"), conn.transaction():
        conn.execute(
            "update public.hotels set org_id = %s where id = %s",
            (world.org("B").id, world.org("A").hotels[0].id),
        )


def test_membership_cannot_change_tenant(conn: Conn, world: World) -> None:
    org = world.org("A")
    user_id = world.users["A_hotel_operator"]
    with pytest.raises(errors.CheckViolation, match="hotel_id is immutable"), conn.transaction():
        conn.execute(
            "update public.memberships set hotel_id = %s where user_id = %s",
            (org.hotels[1].id, user_id),
        )
    with pytest.raises(errors.CheckViolation, match="org_id is immutable"), conn.transaction():
        conn.execute(
            "update public.memberships set org_id = %s where user_id = %s",
            (world.org("B").id, user_id),
        )


def test_role_change_keeps_the_row_and_bumps_updated_at(conn: Conn, world: World) -> None:
    user_id = world.users["A_viewer"]
    conn.execute(
        "update public.memberships set created_at = now() - interval '1 day',"
        " updated_at = now() - interval '1 day' where user_id = %s",
        (user_id,),
    )
    conn.execute("update public.memberships set role = 'operator' where user_id = %s", (user_id,))
    row = conn.execute(
        "select role::text, updated_at = now(), created_at < now()"
        " from public.memberships where user_id = %s",
        (user_id,),
    ).fetchone()
    assert row == ("operator", True, True)


def test_platform_team_role_is_time_boxed_with_a_reason(conn: Conn) -> None:
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(
            "insert into public.team_roles (user_id, role, reason) values (%s, 'platform', 'x')",
            (new_user(conn),),
        )
    conn.execute(
        "insert into public.team_roles (user_id, role) values (%s, 'code_reviewer')",
        (new_user(conn),),
    )


# The runtimes a plan may allow. On main, `RuntimeKind` also has `deep_agents`, `opencode`, and
# the test-only `fake`; the database refuses all three.
# TODO(https://github.com/nikhilllmno/vorflux/pull/4): that PR leaves only these two runtimes in
# `RuntimeKind` and removes `FAKE`. When it is on main, replace this constant with
# `frozenset(kind.value for kind in RuntimeKind)`.
PLAN_RUNTIMES = frozenset({"claude_agent_sdk", "openai_agents_sdk"})


def insert_plan(conn: Conn, **limits: object) -> None:
    columns = ["code", "name", "max_hotels", "max_concurrent_runs", "monthly_budget_usd", *limits]
    values = [f"p-{uuid.uuid4().hex[:8]}", "Plan", 1, 1, 0, *limits.values()]
    placeholders = ", ".join(["%s"] * len(values))
    conn.execute(
        f"insert into public.plans ({', '.join(columns)}) values ({placeholders})",  # noqa: S608
        values,
    )


def plan_is_accepted(conn: Conn, **limits: object) -> bool:
    try:
        with conn.transaction():
            insert_plan(conn, **limits)
    except errors.CheckViolation:
        return False
    return True


def test_plan_runtime_list_matches_runtime_kind(conn: Conn) -> None:
    # Every runtime id that the code knows, plus the pinned list, tried one by one. The database
    # must accept exactly the pinned list, and the pinned list must exist in the code.
    assert {kind.value for kind in RuntimeKind} >= PLAN_RUNTIMES
    candidates = {kind.value for kind in RuntimeKind} | PLAN_RUNTIMES
    accepted = {r for r in candidates if plan_is_accepted(conn, allowed_runtimes=[r])}
    assert accepted == PLAN_RUNTIMES


@pytest.mark.parametrize(
    "runtimes",
    [
        [],
        ["my_runtime"],
        ["deep_agents"],
        ["opencode"],
        ["fake"],
        ["claude_agent_sdk", "my_runtime"],
        ["claude_agent_sdk", "claude_agent_sdk"],
        ["claude_agent_sdk", None],
        None,
    ],
)
def test_plan_rejects_bad_runtime_lists(conn: Conn, runtimes: list[str | None] | None) -> None:
    with pytest.raises((errors.CheckViolation, errors.NotNullViolation)), conn.transaction():
        insert_plan(conn, allowed_runtimes=runtimes)


@pytest.mark.parametrize(
    "models",
    [
        [""],
        ["  "],
        [None],
        ["claude-sonnet-4-5", None],
        ["claude-sonnet-4-5", "claude-sonnet-4-5"],
        None,
    ],
)
def test_plan_rejects_bad_model_lists(conn: Conn, models: list[str | None] | None) -> None:
    with pytest.raises((errors.CheckViolation, errors.NotNullViolation)), conn.transaction():
        insert_plan(conn, allowed_models=models)


def test_service_role_writes_a_valid_plan(conn: Conn) -> None:
    # The check constraints call a helper in `private`; the control plane role must reach it.
    conn.execute("set local role service_role")
    insert_plan(
        conn,
        allowed_runtimes=["claude_agent_sdk", "openai_agents_sdk"],
        allowed_models=["claude-sonnet-4-5", "gpt-5"],
    )
    insert_plan(conn, allowed_models=[])
    with pytest.raises(errors.CheckViolation), conn.transaction():
        insert_plan(conn, allowed_models=["gpt-5", "gpt-5"])


def test_hotel_slug_cannot_change(conn: Conn, world: World) -> None:
    # The slug names the repo `hotel-<slug>` (D24); a rename would break the repo mapping.
    hotel = world.org("A").hotels[0]
    with pytest.raises(errors.CheckViolation, match="slug is immutable"), conn.transaction():
        conn.execute(
            "update public.hotels set slug = %s where id = %s", (f"{hotel.slug}-new", hotel.id)
        )
    # Other columns still change.
    conn.execute("update public.hotels set name = 'Renamed' where id = %s", (hotel.id,))


def test_hotel_slug_is_globally_unique(conn: Conn, world: World) -> None:
    # The hotel repo is `hotel-<slug>` in one GitHub org, so two orgs cannot share a slug.
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        conn.execute(
            "insert into public.hotels (org_id, slug, name) values (%s, %s, 'Copy')",
            (world.org("B").id, world.org("A").hotels[0].slug),
        )


def test_deleting_an_org_hard_deletes_its_hotels_and_memberships(conn: Conn, world: World) -> None:
    org = world.org("B")
    conn.execute("delete from public.orgs where id = %s", (org.id,))
    for table in ("hotels", "memberships"):
        row = conn.execute(
            f"select count(*) from public.{table} where org_id = %s",  # noqa: S608
            (org.id,),
        ).fetchone()
        assert row == (0,), table
    # Org A is untouched.
    row = conn.execute(
        "select count(*) from public.hotels where org_id = %s", (world.org("A").id,)
    ).fetchone()
    assert row == (2,)


def test_plan_in_use_cannot_be_deleted(conn: Conn, world: World) -> None:
    with pytest.raises(errors.ForeignKeyViolation), conn.transaction():
        conn.execute("delete from public.plans where id = %s", (world.org("A").plan_id,))
