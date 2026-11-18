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


def test_plan_runtimes_are_known_runtime_ids(conn: Conn) -> None:
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(
            "insert into public.plans (code, name, max_hotels, max_concurrent_runs,"
            " monthly_budget_usd, allowed_runtimes) values ('bad', 'Bad', 1, 1, 0,"
            " array['claude_agent_sdk', 'my_runtime'])"
        )


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
