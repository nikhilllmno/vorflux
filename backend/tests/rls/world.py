"""A synthetic two-org world for the RLS isolation tests, and the expected access rules.

Two orgs (A and B), each on its own plan, with two hotels each (A1, A2, B1, B2). Each org has
one user per hotel role with an org-wide membership, one user per non-owner role with a
membership narrowed to hotel 1, and one operator narrowed to hotel 2. Two users mix roles and
scopes: `A_mixed_hotels` is admin of hotel A1 and viewer of hotel A2, and `AB_mixed_orgs` is an
org-wide admin of A and a viewer of hotel B1. Their admin rights must not carry into the scope
where they are only viewers. Three users have no membership: an outsider, a code reviewer, and
a platform (break-glass) user from our team.

`expected_visible` is the behaviour contract of the policies, written without SQL: the tests
compare what Postgres returns for a user against it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

# Every table in `public`, in foreign-key order. A new table must be added here (and to the
# expected rules below), or test_rls_catalog fails: no table ships without isolation tests.
TABLES = ("plans", "orgs", "hotels", "memberships", "team_roles")

ORG_WIDE_ROLES = ("owner", "admin", "operator", "viewer")
HOTEL_SCOPED_ROLES = ("admin", "operator", "viewer")  # an owner is always org-wide
MANAGER_ROLES = frozenset({"owner", "admin"})


@dataclass(frozen=True, slots=True)
class Membership:
    id: uuid.UUID
    org_id: uuid.UUID
    hotel_id: uuid.UUID | None
    user_id: uuid.UUID
    role: str


@dataclass(frozen=True, slots=True)
class Hotel:
    id: uuid.UUID
    org_id: uuid.UUID
    slug: str


@dataclass(frozen=True, slots=True)
class Org:
    key: str
    id: uuid.UUID
    plan_id: uuid.UUID
    hotels: tuple[Hotel, Hotel]


@dataclass(frozen=True, slots=True)
class TeamRole:
    id: uuid.UUID
    user_id: uuid.UUID
    role: str


@dataclass(slots=True)
class World:
    orgs: dict[str, Org] = field(default_factory=dict)
    users: dict[str, uuid.UUID] = field(default_factory=dict)
    memberships: list[Membership] = field(default_factory=list)
    team_roles: list[TeamRole] = field(default_factory=list)

    def org(self, key: str) -> Org:
        return self.orgs[key]

    def memberships_of(self, user_id: uuid.UUID) -> list[Membership]:
        return [m for m in self.memberships if m.user_id == user_id]

    def all_ids(self, table: str) -> set[uuid.UUID]:
        if table == "plans":
            return {o.plan_id for o in self.orgs.values()}
        if table == "orgs":
            return {o.id for o in self.orgs.values()}
        if table == "hotels":
            return {h.id for o in self.orgs.values() for h in o.hotels}
        if table == "memberships":
            return {m.id for m in self.memberships}
        if table == "team_roles":
            return {t.id for t in self.team_roles}
        raise KeyError(table)

    def ids_of_org(self, table: str, org_key: str) -> set[uuid.UUID]:
        """Rows that belong to one org (empty for global tables without an org)."""
        org = self.orgs[org_key]
        if table == "plans":
            return {org.plan_id}
        if table == "orgs":
            return {org.id}
        if table == "hotels":
            return {h.id for h in org.hotels}
        if table == "memberships":
            return {m.id for m in self.memberships if m.org_id == org.id}
        if table == "team_roles":
            return set()
        raise KeyError(table)

    def expected_visible(self, table: str, user_id: uuid.UUID | None) -> set[uuid.UUID]:
        """What a signed-in user may read. `None` means a JWT without a `sub` claim."""
        if user_id is None:
            return set()
        mine = self.memberships_of(user_id)
        if table == "orgs":
            return {m.org_id for m in mine}
        if table == "hotels":
            return {
                h.id
                for m in mine
                for h in self._org_by_id(m.org_id).hotels
                if m.hotel_id is None or m.hotel_id == h.id
            }
        if table == "plans":
            return {
                self._org_by_id(m.org_id).plan_id
                for m in mine
                if m.hotel_id is None and m.role in MANAGER_ROLES
            }
        if table == "memberships":
            visible = {m.id for m in mine}
            for row in self.memberships:
                for m in mine:
                    if (
                        m.role in MANAGER_ROLES
                        and m.org_id == row.org_id
                        and (m.hotel_id is None or m.hotel_id == row.hotel_id)
                    ):
                        visible.add(row.id)
            return visible
        if table == "team_roles":
            return {t.id for t in self.team_roles if t.user_id == user_id}
        raise KeyError(table)

    def _org_by_id(self, org_id: uuid.UUID) -> Org:
        return next(o for o in self.orgs.values() if o.id == org_id)


NO_MEMBERSHIP_USERS = ("outsider", "code_reviewer", "platform")
# Users with several memberships that mix roles and scopes.
MIXED_MEMBERSHIP_USERS = ("A_mixed_hotels", "AB_mixed_orgs")


def member_names(org_key: str) -> list[str]:
    """Every user with a membership in one org."""
    names = [f"{org_key}_{role}" for role in ORG_WIDE_ROLES]
    names += [f"{org_key}_hotel_{role}" for role in HOTEL_SCOPED_ROLES]
    return [*names, f"{org_key}_hotel2_operator"]


def seed(conn: psycopg.Connection) -> World:
    """Insert the world as the database owner (RLS does not apply). The caller rolls back."""
    world = World()
    with conn.cursor() as cur:
        for key in ("A", "B"):
            plan_id, org_id = uuid.uuid4(), uuid.uuid4()
            suffix = uuid.uuid4().hex[:8]
            cur.execute(
                "insert into public.plans (id, code, name, max_hotels, max_concurrent_runs,"
                " monthly_budget_usd) values (%s, %s, %s, 5, 2, 100)",
                (plan_id, f"test-{key.lower()}-{suffix}", f"Test plan {key}"),
            )
            cur.execute(
                "insert into public.orgs (id, slug, name, plan_id) values (%s, %s, %s, %s)",
                (org_id, f"org-{key.lower()}-{suffix}", f"Org {key}", plan_id),
            )
            hotels = []
            for n in (1, 2):
                hotel = Hotel(uuid.uuid4(), org_id, f"{key.lower()}{n}-{suffix}")
                cur.execute(
                    "insert into public.hotels (id, org_id, slug, name) values (%s, %s, %s, %s)",
                    (hotel.id, org_id, hotel.slug, f"Hotel {key}{n}"),
                )
                hotels.append(hotel)
            org = Org(key, org_id, plan_id, (hotels[0], hotels[1]))
            world.orgs[key] = org

            for role in ORG_WIDE_ROLES:
                user = _add_user(cur, world, f"{key}_{role}")
                _add_membership(cur, world, org_id, None, user, role)
            for role in HOTEL_SCOPED_ROLES:
                user = _add_user(cur, world, f"{key}_hotel_{role}")
                _add_membership(cur, world, org_id, org.hotels[0].id, user, role)
            user = _add_user(cur, world, f"{key}_hotel2_operator")
            _add_membership(cur, world, org_id, org.hotels[1].id, user, "operator")

        org_a, org_b = world.org("A"), world.org("B")
        user = _add_user(cur, world, "A_mixed_hotels")
        _add_membership(cur, world, org_a.id, org_a.hotels[0].id, user, "admin")
        _add_membership(cur, world, org_a.id, org_a.hotels[1].id, user, "viewer")
        user = _add_user(cur, world, "AB_mixed_orgs")
        _add_membership(cur, world, org_a.id, None, user, "admin")
        _add_membership(cur, world, org_b.id, org_b.hotels[0].id, user, "viewer")

        _add_user(cur, world, "outsider")
        for role in ("code_reviewer", "platform"):  # our team, no membership
            user = _add_user(cur, world, role)
            team_role = TeamRole(uuid.uuid4(), user, role)
            cur.execute(
                "insert into public.team_roles (id, user_id, role, reason, expires_at)"
                " values (%s, %s, %s, 'isolation test', now() + interval '1 day')",
                (team_role.id, user, role),
            )
            world.team_roles.append(team_role)
    return world


def _add_user(cur: psycopg.Cursor[tuple[object, ...]], world: World, name: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    cur.execute(
        "insert into auth.users (id, email, aud, role) values (%s, %s, 'authenticated',"
        " 'authenticated')",
        (user_id, f"{name.lower()}-{user_id.hex[:8]}@example.test"),
    )
    world.users[name] = user_id
    return user_id


def _add_membership(
    cur: psycopg.Cursor[tuple[object, ...]],
    world: World,
    org_id: uuid.UUID,
    hotel_id: uuid.UUID | None,
    user_id: uuid.UUID,
    role: str,
) -> None:
    membership = Membership(uuid.uuid4(), org_id, hotel_id, user_id, role)
    cur.execute(
        "insert into public.memberships (id, org_id, hotel_id, user_id, role)"
        " values (%s, %s, %s, %s, %s)",
        (membership.id, org_id, hotel_id, user_id, role),
    )
    world.memberships.append(membership)


@contextmanager
def as_role(
    conn: psycopg.Connection, role: str, claims: dict[str, str] | None = None
) -> Iterator[None]:
    """Run the block as a Supabase API role with the given JWT claims, inside a savepoint.

    This is what PostgREST does for each request: `set local role` plus the
    `request.jwt.claims` setting that `auth.uid()` reads. The savepoint is rolled back, so the
    role and claims never leak into the next block.
    """
    if role not in {"anon", "authenticated", "service_role"}:
        raise ValueError(role)
    try:
        with conn.transaction(force_rollback=True), conn.cursor() as cur:
            cur.execute(
                "select set_config('request.jwt.claims', %s, true)", (json.dumps(claims or {}),)
            )
            cur.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
            yield
    finally:
        # Savepoint rollback restores the role; reset again in case the block left it changed.
        conn.execute("reset role")


@contextmanager
def as_user(conn: psycopg.Connection, user_id: uuid.UUID | None) -> Iterator[None]:
    """Signed-in user. `None` gives an `authenticated` JWT without a `sub` claim."""
    claims = {"role": "authenticated", "aud": "authenticated"}
    if user_id is not None:
        claims["sub"] = str(user_id)
    with as_role(conn, "authenticated", claims):
        yield
