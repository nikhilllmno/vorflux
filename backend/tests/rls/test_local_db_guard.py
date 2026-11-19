"""The local-only guard rejects every way to point a local-looking URL at another server.

These tests need no database, so they run in the backend job as well as the database job.
"""

from __future__ import annotations

import pytest
from psycopg.conninfo import conninfo_to_dict

from .local_db import (
    MissingRoleSettingsError,
    NonLocalDatabaseError,
    as_database_user,
    check_local_database_url,
)

LOCAL_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"


@pytest.mark.parametrize(
    "url",
    [
        LOCAL_URL,
        "postgresql://postgres:postgres@localhost:54322/postgres",
        "postgresql://postgres:postgres@[::1]:54322/postgres",
        "postgresql://postgres:postgres@supabase_db:5432/postgres?sslmode=disable",
        "host=127.0.0.1 port=54322 user=postgres dbname=postgres",
    ],
)
def test_accepts_local_hosts(url: str) -> None:
    assert check_local_database_url(url, {}) == url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://postgres:x@db.abcd.supabase.co:5432/postgres",
        # The URL host looks local, but libpq would connect elsewhere.
        "postgresql://postgres:x@127.0.0.1:54322/postgres?host=remote.example",
        "postgresql://postgres:x@127.0.0.1:54322/postgres?hostaddr=192.0.2.1",
        "postgresql://postgres:x@127.0.0.1:54322/postgres?service=prod",
        "postgresql://postgres:x@127.0.0.1:54322/postgres?servicefile=/x/pg_service.conf",
        "host=127.0.0.1 hostaddr=192.0.2.1 dbname=postgres",
        # Several hosts: libpq tries the next one when the first fails.
        "postgresql://postgres:x@127.0.0.1:54322,remote.example:5432/postgres",
        # No host: libpq falls back to PGHOST or a socket path.
        "postgresql:///postgres",
        "dbname=postgres",
        "host=/var/run/postgresql dbname=postgres",
        "not a url",
    ],
)
def test_rejects_urls_that_can_reach_another_server(url: str) -> None:
    with pytest.raises(NonLocalDatabaseError):
        check_local_database_url(url, {})


@pytest.mark.parametrize("name", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"])
def test_rejects_environment_redirects(name: str) -> None:
    with pytest.raises(NonLocalDatabaseError):
        check_local_database_url(LOCAL_URL, {name: "x"})


def test_db_fixture_checks_before_it_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    from . import conftest

    def fail_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("connected before the guard rejected the URL")

    monkeypatch.setattr(conftest.psycopg, "connect", fail_connect)
    monkeypatch.setenv("HARNESS_TEST_DATABASE_URL", LOCAL_URL + "?host=remote.example")
    with pytest.raises(pytest.fail.Exception, match="Refusing"):
        conftest._database_url()


def test_role_url_uses_the_local_default_password() -> None:
    params = conninfo_to_dict(as_database_user(LOCAL_URL, "supabase_admin", {}))
    assert (params["user"], params["password"], params["host"]) == (
        "supabase_admin",
        "postgres",
        "127.0.0.1",
    )


def test_role_url_takes_the_password_from_the_role_variable() -> None:
    environ = {"HARNESS_TEST_SUPABASE_ADMIN_PASSWORD": "not-the-default"}
    params = conninfo_to_dict(as_database_user(LOCAL_URL, "supabase_admin", environ))
    assert params["password"] == environ["HARNESS_TEST_SUPABASE_ADMIN_PASSWORD"]


def test_role_url_refuses_a_role_without_settings() -> None:
    with pytest.raises(MissingRoleSettingsError, match=r"'service_role'.*ROLE_PASSWORDS"):
        as_database_user(LOCAL_URL, "service_role", {})


def test_role_url_refuses_an_empty_password_variable() -> None:
    with pytest.raises(MissingRoleSettingsError, match="HARNESS_TEST_POSTGRES_PASSWORD"):
        as_database_user(LOCAL_URL, "postgres", {"HARNESS_TEST_POSTGRES_PASSWORD": ""})
