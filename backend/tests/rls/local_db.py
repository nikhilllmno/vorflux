"""The local-only guard for the database tests.

The isolation tests write synthetic rows and switch roles, so they must never reach a hosted
database. The guard checks the connection parameters that libpq will use, not only the host
part of the URL: a query string such as `?host=...` or `?hostaddr=...`, a service file, or a
`PG*` environment variable can send a URL that looks local to another server.
"""

from __future__ import annotations

from collections.abc import Mapping

from psycopg.conninfo import conninfo_to_dict, make_conninfo

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "db", "postgres", "supabase_db"})

# Connection parameters that can change the server libpq connects to, apart from `host`.
REDIRECT_PARAMS = ("hostaddr", "service", "servicefile")
# Environment variables that libpq reads for those parameters when the conninfo omits them.
REDIRECT_ENV = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")


class NonLocalDatabaseError(ValueError):
    """The URL could connect to something other than a local test database."""


def check_local_database_url(url: str, environ: Mapping[str, str]) -> str:
    """Return `url` if libpq would connect only to a local host; raise otherwise.

    This never opens a connection.
    """
    try:
        params = conninfo_to_dict(url)
    except Exception as exc:
        raise NonLocalDatabaseError("HARNESS_TEST_DATABASE_URL is not a valid conninfo") from exc

    redirects = [name for name in REDIRECT_PARAMS if params.get(name)]
    if redirects:
        raise NonLocalDatabaseError(f"Refusing connection parameters {redirects}")
    env_redirects = [name for name in REDIRECT_ENV if environ.get(name)]
    if env_redirects:
        raise NonLocalDatabaseError(f"Refusing while {env_redirects} is set in the environment")

    host = str(params.get("host") or "")
    # An empty host falls back to PGHOST or a Unix socket; a list tries several servers.
    if not host or "," in host or host not in LOCAL_HOSTS:
        raise NonLocalDatabaseError(f"Refusing to run against a non-local host: {host!r}")
    return url


# Login roles that the catalog probes connect as. For each role: the environment variable that
# holds its password, and the password that a local `supabase start` stack uses for it.
ROLE_PASSWORDS: Mapping[str, tuple[str, str]] = {
    "postgres": ("HARNESS_TEST_POSTGRES_PASSWORD", "postgres"),
    "supabase_admin": ("HARNESS_TEST_SUPABASE_ADMIN_PASSWORD", "postgres"),
}


class MissingRoleSettingsError(LookupError):
    """A probe asked for a role that has no explicit connection settings."""


def as_database_user(url: str, user: str, environ: Mapping[str, str]) -> str:
    """Return `url` with the login role and password of `user`, for catalog probes.

    The password comes from the role's variable in ROLE_PASSWORDS, or the local stack default
    when that variable is unset. The result goes through the local-only guard again before any
    connection.
    """
    if user not in ROLE_PASSWORDS:
        raise MissingRoleSettingsError(
            f"No connection settings for role {user!r}. Add it to ROLE_PASSWORDS in"
            " backend/tests/rls/local_db.py with a password variable and its local default."
        )
    variable, default = ROLE_PASSWORDS[user]
    password = environ.get(variable, default)
    if not password:
        raise MissingRoleSettingsError(
            f"{variable} is set but empty. Set it to the password of {user!r} on the local"
            f" database, or unset it to use the local stack default."
        )
    return make_conninfo(url, user=user, password=password)
