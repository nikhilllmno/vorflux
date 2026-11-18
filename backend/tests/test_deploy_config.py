"""Contracts for the deploy config files (docs/runbook-deploy.md).

The files are never deployed from CI. These tests catch config that would fail or be unsafe
on the first human-approved deploy: a wrong port, a health check the app does not serve,
production as the default target, secrets in plain config, or Vercel auto-deploys.
`flyctl config validate` needs a Fly API token, so these checks run offline instead.
"""

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harness.config import Settings
from harness.main import create_app

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
FLY_CONFIGS = {"staging": BACKEND / "fly.toml", "production": BACKEND / "fly.production.toml"}
SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PRIVATE|CREDENTIAL|DB_URL", re.IGNORECASE)
# India pilot region (architecture D25): Fly runs in `bom` if Fly offers it, else `sin`; the web
# functions run in Vercel `bom1`, the same AWS region as Supabase `ap-south-1` (docs/infra-map.md).
PILOT_FLY_REGIONS = {"bom", "sin"}
PILOT_VERCEL_REGION = "bom1"


def load_fly(environment: str) -> dict[str, Any]:
    with FLY_CONFIGS[environment].open("rb") as f:
        return tomllib.load(f)


def env_example_names() -> set[str]:
    lines = (REPO / ".env.example").read_text().splitlines()
    return {line.split("=", 1)[0] for line in lines if "=" in line and not line.startswith("#")}


def dockerfile_port() -> int:
    exposed = re.findall(r"^EXPOSE (\d+)$", (BACKEND / "Dockerfile").read_text(), re.MULTILINE)
    assert len(exposed) == 1, "Dockerfile must EXPOSE exactly one port"
    return int(exposed[0])


def test_bare_fly_deploy_targets_staging_not_production() -> None:
    staging, production = load_fly("staging"), load_fly("production")

    assert staging["app"].endswith("-staging")
    assert staging["env"]["HARNESS_ENVIRONMENT"] == "staging"
    assert production["env"]["HARNESS_ENVIRONMENT"] == "production"
    assert staging["app"] != production["app"]


@pytest.mark.parametrize("environment", sorted(FLY_CONFIGS))
def test_fly_service_port_matches_the_image(environment: str) -> None:
    config = load_fly(environment)

    assert config["http_service"]["internal_port"] == dockerfile_port()
    assert int(config["env"]["PORT"]) == dockerfile_port()
    assert config["http_service"]["force_https"] is True
    assert config["build"]["dockerfile"] == "Dockerfile"


@pytest.mark.parametrize("environment", sorted(FLY_CONFIGS))
def test_fly_health_check_is_served_by_the_app_built_from_fly_env(
    environment: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_fly(environment)
    monkeypatch.chdir(tmp_path)  # no local .env file can leak into Settings
    for name, value in config["env"].items():
        monkeypatch.setenv(name, value)
    app = create_app(Settings())

    for check in config["http_service"]["checks"]:
        response = TestClient(app).request(check["method"], check["path"])
        assert response.status_code == 200
        assert response.json()["environment"] == environment


@pytest.mark.parametrize("environment", sorted(FLY_CONFIGS))
def test_fly_env_holds_only_documented_non_secret_names(environment: str) -> None:
    names = set(load_fly(environment)["env"])

    assert not {n for n in names if SECRET_NAME.search(n)}, "secrets go in `fly secrets`"
    assert names - {"PORT"} <= env_example_names()


@pytest.mark.parametrize("environment", sorted(FLY_CONFIGS))
def test_fly_deploy_never_runs_migrations(environment: str) -> None:
    # Migrations are a separate, owner-approved step with the Supabase CLI.
    assert "release_command" not in load_fly(environment).get("deploy", {})


def test_production_keeps_a_machine_running() -> None:
    service = load_fly("production")["http_service"]

    assert service["min_machines_running"] >= 1
    assert service["auto_stop_machines"] == "off"


def test_staging_and_production_share_region_and_vm_shape() -> None:
    staging, production = load_fly("staging"), load_fly("production")

    assert staging["primary_region"] == production["primary_region"]
    assert staging["vm"] == production["vm"]


def test_api_runs_in_the_india_pilot_region() -> None:
    for environment in FLY_CONFIGS:
        assert load_fly(environment)["primary_region"] in PILOT_FLY_REGIONS


def test_vercel_never_deploys_on_git_push_and_runs_next_to_the_data() -> None:
    vercel = json.loads((REPO / "web" / "vercel.json").read_text())

    assert vercel["git"]["deploymentEnabled"] is False
    assert vercel["framework"] == "nextjs"
    assert vercel["regions"] == [PILOT_VERCEL_REGION]
