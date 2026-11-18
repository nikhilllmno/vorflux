import pytest
from fastapi.testclient import TestClient

from harness.config import Settings
from harness.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(environment="test")))
