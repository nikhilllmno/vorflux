"""Offline contracts for `supabase/` (docs/schema.md).

These run in every backend job, with no database. The database job (`tests/rls/`) checks the
applied schema; these checks catch unsafe config and migration files before that.
"""

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


def test_web_never_references_the_service_role() -> None:
    # Only the control plane (backend) may hold the service role key (section 7.2).
    offenders = [
        path.relative_to(REPO)
        for path in (REPO / "web").rglob("*")
        if path.is_file()
        and "node_modules" not in path.parts
        and ".next" not in path.parts
        and path.suffix in {".ts", ".tsx", ".js", ".mjs", ".json"}
        and re.search(r"service[_-]?role", path.read_text(errors="ignore"), re.I)
    ]
    assert offenders == []
