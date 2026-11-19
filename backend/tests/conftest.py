import pytest
from fakes import FakeRuntime
from fastapi.testclient import TestClient

from harness.config import Settings
from harness.main import create_app
from harness.runtimes import RuntimeRegistry


@pytest.fixture
def client() -> TestClient:
    registry = RuntimeRegistry()
    registry.register(FakeRuntime())
    return TestClient(create_app(Settings(environment="test"), runtimes=registry))
