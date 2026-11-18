from fastapi.testclient import TestClient

from harness import __version__
from harness.config import Settings
from harness.main import create_app


def test_healthz_reports_ok_and_version(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__, "environment": "test"}


def test_runtimes_lists_fake_runtime_in_test_env(client: TestClient) -> None:
    body = client.get("/v1/runtimes").json()

    assert [r["kind"] for r in body["runtimes"]] == ["fake"]
    assert body["runtimes"][0]["capabilities"]["streaming"] is True


def test_production_does_not_register_fake_runtime() -> None:
    client = TestClient(create_app(Settings(environment="production")))

    assert client.get("/v1/runtimes").json() == {"runtimes": []}
