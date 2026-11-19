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


def as_database_user(url: str, user: str) -> str:
    """Return `url` with another login role, for catalog probes that need that role.

    The local Supabase stack uses one password for its built-in roles. The result goes through
    the local-only guard again before any connection.
    """
    return make_conninfo(url, user=user)
