"""Offline contracts for `supabase/` (docs/schema.md).

These run in every backend job, with no database. The database job (`tests/rls/`) checks the
applied schema; these checks catch unsafe config and migration files before that.
"""

import base64
import json
import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SUPABASE = REPO / "supabase"
MIGRATIONS = sorted((SUPABASE / "migrations").glob("*.sql"))
CREATE_TABLE = re.compile(r"create\s+table\s+(?:if\s+not\s+exists\s+)?public\.(\w+)", re.I)


def test_migrations_exist_with_timestamped_names() -> None:
    assert MIGRATIONS
    for path in MIGRATIONS:
        assert re.fullmatch(r"\d{14}_[a-z0-9_]+\.sql", path.name), path.name
    stamps = [path.name[:14] for path in MIGRATIONS]
    assert len(set(stamps)) == len(stamps), "two migrations share a timestamp"


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda path: path.name)
def test_every_new_public_table_enables_rls_in_the_same_migration(path: Path) -> None:
    text = path.read_text()
    for table in CREATE_TABLE.findall(text):
        enable = rf"alter\s+table\s+public\.{table}\s+enable\s+row\s+level\s+security"
        assert re.search(enable, text, re.I), f"{path.name}: public.{table} without RLS"


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda path: path.name)
def test_migrations_do_not_grant_writes_or_access_to_anon(path: Path) -> None:
    text = re.sub(r"--[^\n]*", "", path.read_text())
    for statement in text.split(";"):
        if re.match(r"\s*grant\b", statement, re.I):
            assert not re.search(r"\banon\b", statement, re.I), statement.strip()
            if re.search(r"\bauthenticated\b", statement, re.I):
                assert not re.search(r"\b(insert|update|delete|truncate|all)\b", statement, re.I), (
                    statement.strip()
                )


def test_local_config_keeps_the_api_closed() -> None:
    config = tomllib.loads((SUPABASE / "config.toml").read_text())
    api = config["api"]
    # The private schema holds SECURITY DEFINER helpers; it must never be exposed by PostgREST.
    assert "private" not in api["schemas"]
    assert api["auto_expose_new_tables"] is False
    # The repository holds no seed data (synthetic rows live in the tests only).
    assert config["db"]["seed"]["enabled"] is False


# The web app must never ship a service-role key (docs/architecture.md, section 7.2): only the
# control plane (backend) may hold it. The scan looks for key material and the server-side env var
# name, not for the plain words "service_role", because the web app's own key guard names the role
# on purpose when it refuses such a key.
WEB = REPO / "web"
WEB_SKIP_DIRS = {"node_modules", ".next"}
WEB_SUFFIXES = {".ts", ".tsx", ".js", ".mjs", ".cjs", ".mts", ".cts", ".json"}
SECRET_KEY = re.compile(r"\bsb_secret_[A-Za-z0-9_-]{8,}")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]*\.(eyJ[A-Za-z0-9_-]*)\.[A-Za-z0-9_-]*")
SERVICE_ROLE_ENV = re.compile(r"\b[A-Z0-9_]*SERVICE_ROLE_KEY\b")


def _jwt_role(payload: str) -> object:
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    return claims.get("role") if isinstance(claims, dict) else None


def service_role_findings(text: str) -> list[str]:
    """What in `text` would put a service-role key in the web app. Never holds the value."""
    findings = []
    if SECRET_KEY.search(text):
        findings.append("a Supabase secret key (sb_secret_...)")
    if any(_jwt_role(match.group(1)) == "service_role" for match in JWT.finditer(text)):
        findings.append("a JWT with role service_role")
    if SERVICE_ROLE_ENV.search(text):
        findings.append("a *SERVICE_ROLE_KEY env var reference")
    return findings


def web_offenders(web: Path) -> dict[Path, list[str]]:
    offenders = {}
    for path in web.rglob("*"):
        if (
            path.is_file()
            and path.suffix in WEB_SUFFIXES
            and not WEB_SKIP_DIRS.intersection(path.relative_to(web).parts)
        ):
            findings = service_role_findings(path.read_text(errors="ignore"))
            if findings:
                offenders[path.relative_to(web)] = findings
    return offenders


def test_web_never_ships_a_service_role_key() -> None:
    assert web_offenders(WEB) == {}


# Key-shaped values are built at run time so that no key-shaped literal sits in the repository
# (gitleaks scans the full history).
def _jwt(role: str) -> str:
    def part(value: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return ".".join([part({"alg": "HS256", "typ": "JWT"}), part({"role": role}), "signature"])


_SECRET_KEY = "_".join(["sb", "secret", "test" * 8])
_PUBLISHABLE_KEY = "_".join(["sb", "publishable", "test" * 8])


@pytest.mark.parametrize(
    "text",
    [
        f'const key = "{_SECRET_KEY}";',
        f'{{"supabaseKey": "{_SECRET_KEY}"}}',
        f'createClient(url, "{_jwt("service_role")}");',
        f"NEXT_PUBLIC_SUPABASE_ANON_KEY={_jwt('service_role')}",
        "const key = process.env.SUPABASE_SERVICE_ROLE_KEY!;",
    ],
    ids=["secret-key", "secret-key-json", "service-role-jwt", "service-role-jwt-env", "env-var"],
)
def test_scan_flags_a_service_role_key(text: str) -> None:
    assert service_role_findings(text)


# Verbatim lines from the web app's key guard (src/lib/supabase-key.ts, src/lib/config.ts) and its
# tests. They name the role on purpose, to refuse the key.
GUARD_TEXT = [
    '"the browser bundle, so the build stops. Use the publishable key, never a secret or " +',
    '        "service_role key.",',
    '      "NEXT_PUBLIC_SUPABASE_ANON_KEY must be the publishable (or anon) key, never a secret or '
    'service_role key.",',
    " * A secret key or a `service_role` JWT bypasses RLS.",
    " * Only a publishable key (`sb_publishable_...`) or a legacy JWT with role `anon` is "
    "browser-safe.",
    '    ["a service_role JWT", jwtWithRole("service_role")],',
    '  it("next.config.ts refuses to load with a service_role key, so nothing is bundled", '
    "async () => {",
    '    vi.stubEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY", jwtWithRole("service_role"));',
    'const SECRET_KEY = ["sb", "secret", "test0000"].join("_");',
    "Never put a `sb_secret_...` key here.",
]


@pytest.mark.parametrize(
    "text",
    [
        *GUARD_TEXT,
        f'const key = "{_PUBLISHABLE_KEY}";',
        f'const key = "{_jwt("anon")}";',
        f'const token = "{_jwt("authenticated")}";',
    ],
)
def test_scan_allows_guard_text_and_browser_safe_keys(text: str) -> None:
    assert service_role_findings(text) == []


def test_scan_walks_web_sources_and_skips_build_output(tmp_path: Path) -> None:
    web = tmp_path / "web"
    (web / "src" / "lib").mkdir(parents=True)
    (web / "src" / "lib" / "supabase-key.ts").write_text("\n".join(GUARD_TEXT))
    for skipped in ("node_modules/pkg/index.js", ".next/static/chunk.js"):
        (web / skipped).parent.mkdir(parents=True)
        (web / skipped).write_text(f'"{_SECRET_KEY}"')
    assert web_offenders(web) == {}

    (web / "src" / "lib" / "admin.ts").write_text(f'export const key = "{_jwt("service_role")}";')
    assert web_offenders(web) == {Path("src/lib/admin.ts"): ["a JWT with role service_role"]}
