"""Auth settings from the environment and the HTTP JWKS fetcher."""

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest
from auth_world import ISSUER, es256_key, mint
from fastapi.testclient import TestClient
from pydantic import ValidationError

from harness.auth import HttpJwksFetcher, InMemoryMembershipProvider, TokenVerifier
from harness.config import Settings
from harness.main import create_app
from harness.tenancy import HotelRole


def test_auth_is_off_by_default() -> None:
    settings = Settings(environment="test")

    assert settings.auth_issuer is None
    assert settings.auth_jwks_url is None
    assert TokenVerifier.from_settings(settings) is None


def test_defaults_match_supabase_user_tokens() -> None:
    settings = Settings(environment="test")

    assert settings.auth_audience == "authenticated"
    assert settings.auth_algorithms == ["ES256", "RS256"]


def test_settings_read_the_documented_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("HARNESS_AUTH_JWKS_URL", f"{ISSUER}/.well-known/jwks.json")
    monkeypatch.setenv("HARNESS_AUTH_ALGORITHMS", '["ES256"]')
    monkeypatch.setenv("HARNESS_AUTH_JWKS_MIN_REFETCH_SECONDS", "60")

    settings = Settings()

    assert settings.auth_issuer == ISSUER
    assert settings.auth_algorithms == ["ES256"]
    assert settings.auth_jwks_min_refetch_seconds == 60
    assert isinstance(TokenVerifier.from_settings(settings), TokenVerifier)


def test_empty_variables_from_env_example_keep_auth_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ISSUER",
        "JWKS_URL",
        "AUDIENCE",
        "ALGORITHMS",
        "LEEWAY_SECONDS",
        "JWKS_CACHE_SECONDS",
        "JWKS_MIN_REFETCH_SECONDS",
    ):
        monkeypatch.setenv(f"HARNESS_AUTH_{name}", "")

    settings = Settings()

    assert TokenVerifier.from_settings(settings) is None
    assert settings.auth_audience == "authenticated"


@pytest.mark.parametrize("algorithms", [["HS256"], ["none"], ["ES256", "HS512"], []])
def test_settings_refuse_symmetric_none_or_empty_algorithms(algorithms: list[str]) -> None:
    with pytest.raises(ValidationError):
        Settings(auth_algorithms=algorithms)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["auth_audience", "auth_issuer"])
def test_blank_audience_or_issuer_is_refused_at_startup(field: str) -> None:
    values = {"auth_issuer": ISSUER, "auth_jwks_url": f"{ISSUER}/.well-known/jwks.json"}

    with pytest.raises(ValidationError):
        Settings(**{**values, field: "   "})


def test_issuer_and_jwks_url_are_set_together() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_issuer=ISSUER)
    with pytest.raises(ValidationError):
        Settings(auth_jwks_url=f"{ISSUER}/.well-known/jwks.json")


@pytest.mark.parametrize(
    "url",
    [
        "http://project-ref.supabase.test/auth/v1/.well-known/jwks.json",
        "ftp://example.test/jwks.json",
        "file:///etc/passwd",
        "not a url",
    ],
)
def test_jwks_url_must_be_https_unless_local(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(auth_issuer=ISSUER, auth_jwks_url=url)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:54321/auth/v1/.well-known/jwks.json", "http://localhost:54321/jwks"],
)
def test_local_supabase_stack_may_use_http(url: str) -> None:
    assert Settings(auth_issuer=ISSUER, auth_jwks_url=url).auth_jwks_url == url


@pytest.mark.parametrize("environment", ["local", "test"])
def test_local_or_test_environment_is_refused_on_fly(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """A Fly machine that forgets HARNESS_ENVIRONMENT must not start as `local`."""
    monkeypatch.setenv("FLY_APP_NAME", "harness-api")

    with pytest.raises(ValidationError):
        Settings(environment=environment)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings()


def test_deployed_environments_start_on_fly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLY_APP_NAME", "harness-api")

    assert Settings(environment="staging").environment == "staging"


def test_local_http_jwks_is_refused_in_production() -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            auth_issuer=ISSUER,
            auth_jwks_url="http://127.0.0.1:54321/auth/v1/.well-known/jwks.json",
        )


# --- HTTP fetcher against a local server -------------------------------------------------------


class _Server:
    def __init__(self) -> None:
        self.body: bytes = b'{"keys": []}'
        self.redirect_to: str | None = None
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.requests += 1
                if outer.redirect_to is not None:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/auth/v1/.well-known/jwks.json"


@pytest.fixture
def server() -> Iterator[_Server]:
    server = _Server()
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def test_http_fetcher_reads_the_jwks(server: _Server) -> None:
    key = es256_key()
    server.body = json.dumps({"keys": [dict(key.public_jwk)]}).encode()

    document = asyncio.run(HttpJwksFetcher(server.url).fetch())

    assert document == {"keys": [dict(key.public_jwk)]}


def test_http_fetcher_refuses_an_oversized_document(server: _Server) -> None:
    server.body = b'{"keys": [' + b" " * 70_000 + b"]}"

    with pytest.raises(ValueError):
        asyncio.run(HttpJwksFetcher(server.url, max_bytes=64 * 1024).fetch())


def test_http_fetcher_does_not_follow_redirects(server: _Server) -> None:
    server.redirect_to = "http://127.0.0.1:1/elsewhere.json"

    with pytest.raises(OSError):
        asyncio.run(HttpJwksFetcher(server.url).fetch())


def test_app_builds_a_verifier_from_settings_end_to_end(server: _Server) -> None:
    key = es256_key()
    server.body = json.dumps({"keys": [dict(key.public_jwk)]}).encode()
    settings = Settings(environment="test", auth_issuer=ISSUER, auth_jwks_url=server.url)
    org, hotel, user = uuid4(), uuid4(), uuid4()
    provider = InMemoryMembershipProvider()
    provider.add_hotel(org, hotel)
    provider.add_membership(user, org, HotelRole.VIEWER)
    app = create_app(settings)
    app.state.membership_provider = provider

    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {mint(key, user)}"}
        first = client.get(f"/v1/orgs/{org}/hotels/{hotel}/me", headers=headers)
        second = client.get(f"/v1/orgs/{org}/hotels/{hotel}/me", headers=headers)

    assert first.status_code == second.status_code == 200
    assert server.requests == 1  # the verifier is built once per app and caches the keys
