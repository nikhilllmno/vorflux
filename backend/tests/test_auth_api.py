"""The tenant dependency at the HTTP edge: 401, 403, 503, and no leaks across orgs or hotels."""

from collections.abc import Iterator
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest
from auth_world import FakeJwksEndpoint, SigningKey, es256_key, mint, verifier
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from harness.auth import InMemoryMembershipProvider, require_tenant
from harness.config import Settings
from harness.main import create_app
from harness.tenancy import Action, HotelRole, TenantContext

USERS = ("viewer", "operator", "admin", "owner", "hotel_admin", "mixed", "outsider", "b_owner")


class World:
    """Two orgs. Org A has hotels a1 and a2; org B has hotel b1."""

    def __init__(self) -> None:
        self.key: SigningKey = es256_key()
        self.endpoint = FakeJwksEndpoint()
        self.endpoint.publish(self.key)
        self.org_a, self.org_b = uuid4(), uuid4()
        self.a1, self.a2, self.b1 = uuid4(), uuid4(), uuid4()
        self.user = {name: uuid4() for name in USERS}
        self.provider = InMemoryMembershipProvider()
        p = self.provider
        p.add_hotel(self.org_a, self.a1)
        p.add_hotel(self.org_a, self.a2)
        p.add_hotel(self.org_b, self.b1)
        p.add_membership(self.user["viewer"], self.org_a, HotelRole.VIEWER)
        p.add_membership(self.user["operator"], self.org_a, HotelRole.OPERATOR)
        p.add_membership(self.user["admin"], self.org_a, HotelRole.ADMIN)
        p.add_membership(self.user["owner"], self.org_a, HotelRole.OWNER)
        p.add_membership(self.user["hotel_admin"], self.org_a, HotelRole.ADMIN, hotel_id=self.a1)
        p.add_membership(self.user["mixed"], self.org_a, HotelRole.VIEWER)
        p.add_membership(self.user["mixed"], self.org_a, HotelRole.ADMIN, hotel_id=self.a2)
        p.add_membership(self.user["b_owner"], self.org_b, HotelRole.OWNER)

    def token(self, name: str, **overrides: Any) -> str:
        return mint(self.key, self.user[name], **overrides)

    def auth(self, name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(name)}"}


def add_test_routes(app: FastAPI) -> None:
    @app.get("/v1/orgs/{org_id}/hotels/{hotel_id}/test-admin")
    def admin_only(
        tenant: Annotated[TenantContext, Depends(require_tenant(Action.CONFIGURE_APPROVALS))],
    ) -> dict[str, str]:
        return {"role": str(tenant.role)}

    @app.get("/v1/orgs/{org_id}/hotels/{hotel_id}/test-run")
    def operator_only(
        tenant: Annotated[TenantContext, Depends(require_tenant(Action.START_RUN))],
    ) -> dict[str, str]:
        return {"role": str(tenant.role)}


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def client(world: World) -> Iterator[TestClient]:
    app = create_app(Settings(environment="test"))
    app.state.token_verifier = verifier(world.endpoint)
    app.state.membership_provider = world.provider
    add_test_routes(app)
    with TestClient(app) as client:
        yield client


def me(org: UUID, hotel: UUID) -> str:
    return f"/v1/orgs/{org}/hotels/{hotel}/me"


# --- 200 ---------------------------------------------------------------------------------------


def test_member_gets_their_tenant_context(client: TestClient, world: World) -> None:
    response = client.get(me(world.org_a, world.a1), headers=world.auth("operator"))

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(world.user["operator"]),
        "org_id": str(world.org_a),
        "hotel_id": str(world.a1),
        "role": "operator",
    }


# --- 401 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "Token abc",
        "Bearer not-a-jwt",
        "Bearer a.b.c",
    ],
)
def test_missing_or_malformed_credentials_get_401(
    client: TestClient, world: World, header: str | None
) -> None:
    headers = {} if header is None else {"Authorization": header}

    response = client.get(me(world.org_a, world.a1), headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_expired_token_gets_401(client: TestClient, world: World) -> None:
    token = world.token("owner", exp=1_000)

    response = client.get(me(world.org_a, world.a1), headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["www-authenticate"]


def test_bearer_scheme_is_case_insensitive(client: TestClient, world: World) -> None:
    headers = {"Authorization": f"bearer {world.token('viewer')}"}

    assert client.get(me(world.org_a, world.a1), headers=headers).status_code == 200


def test_bad_token_gets_401_before_any_membership_check(client: TestClient, world: World) -> None:
    """An invalid token never reaches the 403 path, so it cannot probe orgs or hotels."""
    forged = mint(es256_key(kid=world.key.kid), world.user["owner"])
    headers = {"Authorization": f"Bearer {forged}"}

    for org, hotel in [(world.org_a, world.a1), (uuid4(), uuid4()), (world.org_b, world.b1)]:
        assert client.get(me(org, hotel), headers=headers).status_code == 401


def test_bad_token_with_a_malformed_path_still_gets_401(client: TestClient) -> None:
    assert client.get("/v1/orgs/not-a-uuid/hotels/also-not/me").status_code == 401


# --- 403: membership ---------------------------------------------------------------------------


def test_user_with_no_membership_gets_403(client: TestClient, world: World) -> None:
    response = client.get(me(world.org_a, world.a1), headers=world.auth("outsider"))

    assert response.status_code == 403


def test_cross_org_access_is_denied(client: TestClient, world: World) -> None:
    owner_a = world.auth("owner")

    assert client.get(me(world.org_b, world.b1), headers=owner_a).status_code == 403
    # Org A's owner names org B's hotel under org A: the hotel is not in org A.
    assert client.get(me(world.org_a, world.b1), headers=owner_a).status_code == 403
    # Org B's owner names org A's hotel under org B.
    assert client.get(me(world.org_b, world.a1), headers=world.auth("b_owner")).status_code == 403


def test_hotel_scoped_member_cannot_reach_another_hotel(client: TestClient, world: World) -> None:
    headers = world.auth("hotel_admin")

    assert client.get(me(world.org_a, world.a1), headers=headers).status_code == 200
    assert client.get(me(world.org_a, world.a2), headers=headers).status_code == 403


def test_denials_do_not_reveal_whether_an_org_or_hotel_exists(
    client: TestClient, world: World
) -> None:
    headers = world.auth("owner")
    cases = [
        (world.org_b, world.b1),  # both exist, other org
        (world.org_a, world.b1),  # our org, hotel of another org
        (world.org_b, uuid4()),  # org exists, hotel does not
        (uuid4(), world.b1),  # org does not exist, hotel does
        (uuid4(), uuid4()),  # neither exists
    ]

    responses = [client.get(me(org, hotel), headers=headers) for org, hotel in cases]

    assert {r.status_code for r in responses} == {403}
    assert len({r.content for r in responses}) == 1


# --- 403: role ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "status"),
    [("viewer", 403), ("operator", 403), ("admin", 200), ("owner", 200), ("outsider", 403)],
)
def test_admin_action_needs_admin_or_higher(
    client: TestClient, world: World, name: str, status: int
) -> None:
    url = f"/v1/orgs/{world.org_a}/hotels/{world.a1}/test-admin"

    assert client.get(url, headers=world.auth(name)).status_code == status


def test_role_too_low_and_no_membership_look_the_same(client: TestClient, world: World) -> None:
    url = f"/v1/orgs/{world.org_a}/hotels/{world.a1}/test-admin"

    too_low = client.get(url, headers=world.auth("viewer"))
    outsider = client.get(url, headers=world.auth("outsider"))

    assert too_low.status_code == outsider.status_code == 403
    assert too_low.content == outsider.content


def test_hotel_admin_role_does_not_carry_to_other_hotels(client: TestClient, world: World) -> None:
    """`mixed` is an org-wide viewer and admin of a2 only."""
    headers = world.auth("mixed")

    def admin_url(hotel: UUID) -> str:
        return f"/v1/orgs/{world.org_a}/hotels/{hotel}/test-admin"

    assert client.get(admin_url(world.a2), headers=headers).status_code == 200
    assert client.get(admin_url(world.a1), headers=headers).status_code == 403
    assert client.get(me(world.org_a, world.a1), headers=headers).json()["role"] == "viewer"


def test_operator_can_start_runs_and_viewer_cannot(client: TestClient, world: World) -> None:
    url = f"/v1/orgs/{world.org_a}/hotels/{world.a1}/test-run"

    assert client.get(url, headers=world.auth("operator")).status_code == 200
    assert client.get(url, headers=world.auth("viewer")).status_code == 403


def test_removed_membership_takes_effect_on_the_next_request(
    client: TestClient, world: World
) -> None:
    headers = world.auth("admin")
    assert client.get(me(world.org_a, world.a1), headers=headers).status_code == 200

    world.provider.remove_membership(world.user["admin"], world.org_a)

    assert client.get(me(world.org_a, world.a1), headers=headers).status_code == 403


# --- 503: fail closed when not configured ------------------------------------------------------


def test_unconfigured_auth_fails_closed(world: World) -> None:
    app = create_app(Settings(environment="test"))
    app.state.membership_provider = world.provider

    with TestClient(app) as client:
        response = client.get(me(world.org_a, world.a1), headers=world.auth("owner"))

    assert response.status_code == 503


def test_missing_membership_provider_fails_closed(world: World) -> None:
    app = create_app(Settings(environment="test"))
    app.state.token_verifier = verifier(world.endpoint)

    with TestClient(app) as client:
        assert client.get(me(world.org_a, world.a1), headers=world.auth("owner")).status_code == 503
        # A bad token is still a 401, not a 503.
        assert client.get(me(world.org_a, world.a1)).status_code == 401


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_in_memory_provider_is_refused_outside_local_and_test(
    world: World, environment: str
) -> None:
    app = create_app(Settings(environment=environment))  # type: ignore[arg-type]
    app.state.token_verifier = verifier(world.endpoint)
    app.state.membership_provider = world.provider

    with TestClient(app) as client:
        response = client.get(me(world.org_a, world.a1), headers=world.auth("owner"))

    assert response.status_code == 503


def test_jwks_outage_gets_503_not_401(world: World) -> None:
    world.endpoint.fail = True
    app = create_app(Settings(environment="test"))
    app.state.token_verifier = verifier(world.endpoint)
    app.state.membership_provider = world.provider

    with TestClient(app) as client:
        assert client.get(me(world.org_a, world.a1), headers=world.auth("owner")).status_code == 503
