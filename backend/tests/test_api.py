import pytest
from fastapi.testclient import TestClient

from harness import __version__
from harness.config import Settings
from harness.main import create_app


def test_healthz_reports_ok_and_version(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__, "environment": "test"}


def test_runtimes_lists_injected_runtimes(client: TestClient) -> None:
    body = client.get("/v1/runtimes").json()

    assert [r["kind"] for r in body["runtimes"]] == ["fake"]
    assert body["runtimes"][0]["capabilities"]["streaming"] is True


@pytest.mark.parametrize("environment", ["local", "test", "production"])
def test_app_registers_no_test_double_in_any_environment(environment: str) -> None:
    client = TestClient(create_app(Settings(environment=environment)))

    assert client.get("/v1/runtimes").json() == {"runtimes": []}
