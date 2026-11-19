"""Fixtures for the database isolation tests (docs/architecture.md section 7.4).

The tests need a local Supabase database with the migrations in `supabase/migrations/` applied
(`supabase db start` then `supabase db reset`; see docs/schema.md). Set
`HARNESS_TEST_DATABASE_URL` to its URL. Never point it at a hosted database: the tests write
synthetic rows (always rolled back) and switch roles.

Without the URL the tests are skipped, so `uv run pytest` still works on a laptop with no
Docker. The CI database job sets `HARNESS_TEST_REQUIRE_DB=1`, which turns a missing URL into
a failure instead of a silent skip.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest

from .local_db import (
    MissingRoleSettingsError,
    NonLocalDatabaseError,
    as_database_user,
    check_local_database_url,
)
from .world import World, seed


def _database_url() -> str:
    url = os.environ.get("HARNESS_TEST_DATABASE_URL", "")
    if not url:
        if os.environ.get("HARNESS_TEST_REQUIRE_DB") == "1":
            pytest.fail("HARNESS_TEST_REQUIRE_DB=1 but HARNESS_TEST_DATABASE_URL is not set")
        pytest.skip("HARNESS_TEST_DATABASE_URL is not set (local Supabase database)")
    try:
        return check_local_database_url(url, os.environ)
    except NonLocalDatabaseError as exc:
        pytest.fail(f"Refusing to run the isolation tests: {exc}")


@pytest.fixture
def connect_as() -> Callable[[str], psycopg.Connection[Any]]:
    """Open a separate connection to the same local database as another built-in role."""

    def connect(user: str) -> psycopg.Connection[Any]:
        try:
            url = as_database_user(_database_url(), user, os.environ)
            check_local_database_url(url, os.environ)
        except (MissingRoleSettingsError, NonLocalDatabaseError) as exc:
            pytest.fail(f"Refusing to connect as {user}: {exc}")
        return psycopg.connect(url)

    return connect


@pytest.fixture(scope="session")
def db_session() -> Iterator[tuple[psycopg.Connection[Any], World]]:
    """One connection and one seeded world for the whole run, rolled back at the end."""
    with psycopg.connect(_database_url(), autocommit=True) as conn:
        with conn.transaction(force_rollback=True):
            world = seed(conn)
            yield conn, world


@pytest.fixture
def conn(db_session: tuple[psycopg.Connection[Any], World]) -> Iterator[psycopg.Connection[Any]]:
    """The shared connection inside a per-test savepoint that is always rolled back."""
    connection, _ = db_session
    with connection.transaction(force_rollback=True):
        yield connection
    connection.execute("reset role")


@pytest.fixture
def world(db_session: tuple[psycopg.Connection[Any], World]) -> World:
    return db_session[1]
