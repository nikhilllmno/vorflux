"""HTTP contract of the approval inbox: fail closed, 404 across tenants, and role checks."""

import asyncio
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from approval_world import World
from fastapi.testclient import TestClient

from harness.approvals import ActionKind, ApprovalRequest, ApprovalStatus, Principal, RiskLevel
from harness.approvals.api import get_principal
from harness.config import Settings
from harness.main import create_app


class Api:
    def __init__(self, world: World) -> None:
        self.world = world
        self.app = create_app(Settings(environment="test"))
        self.app.state.approval_service = world.service
        self.client = TestClient(self.app)
        self.as_user(world.operator)

    def as_user(self, principal: Principal) -> None:
        self.app.dependency_overrides[get_principal] = lambda: principal

    def base(self, org_id: object = None, hotel_id: object = None) -> str:
        return f"/v1/orgs/{org_id or self.world.org_a}/hotels/{hotel_id or self.world.hotel_a1}"


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def api(world: World) -> Iterator[Api]:
    yield Api(world)


def _pending(world: World, **kwargs: Any) -> ApprovalRequest:
    call = world.call(**kwargs)
    return asyncio.run(world.service.request(call, note="Christmas demand is high"))


def test_routes_fail_closed_without_auth(world: World) -> None:
    client = TestClient(create_app(Settings(environment="test")))
    base = f"/v1/orgs/{world.org_a}/hotels/{world.hotel_a1}"

    assert client.get(f"{base}/approvals").status_code == 401
    assert client.post(f"{base}/approvals/{uuid4()}/approve", json={}).status_code == 401


def test_routes_fail_closed_without_an_approval_service(world: World) -> None:
    app = create_app(Settings(environment="test"))
    app.dependency_overrides[get_principal] = lambda: world.operator
    client = TestClient(app)

    response = client.get(f"/v1/orgs/{world.org_a}/hotels/{world.hotel_a1}/approvals")

    assert response.status_code == 503


def test_list_and_get_show_the_card_without_the_raw_payload(api: Api, world: World) -> None:
    request = _pending(world)

    listed = api.client.get(f"{api.base()}/approvals").json()["approvals"]
    one = api.client.get(f"{api.base()}/approvals/{request.id}").json()

    assert [card["id"] for card in listed] == [str(request.id)]
    assert one["preview"] == {
        "title": "Change rate of Deluxe King, 24 Dec 2026 in Beds24",
        "fields": [
            {"label": "From", "value": "9000"},
            {"label": "To", "value": "9900"},
            {"label": "Guests affected", "value": "0"},
        ],
    }
    assert one["note"] == "Christmas demand is high"
    assert one["tool_call_id"] == "call-1"
    assert one["status"] == "pending"
    assert one["approver_roles"] == ["admin", "operator", "owner"]
    assert one["used"] is False
    assert "payload" not in one


def test_list_filters_by_status(api: Api, world: World) -> None:
    first = _pending(world)
    _pending(world, run_id="run-2")
    api.client.post(f"{api.base()}/approvals/{first.id}/approve", json={})

    approved = api.client.get(f"{api.base()}/approvals", params={"status": "approved"}).json()

    assert [card["id"] for card in approved["approvals"]] == [str(first.id)]
    bad = api.client.get(f"{api.base()}/approvals", params={"status": "maybe"})
    assert bad.status_code == 422


def test_approve_then_conflict_on_a_second_decision(api: Api, world: World) -> None:
    request = _pending(world)

    approved = api.client.post(
        f"{api.base()}/approvals/{request.id}/approve", json={"reason": "OK for Christmas"}
    )
    again = api.client.post(f"{api.base()}/approvals/{request.id}/reject", json={})

    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["reason"] == "OK for Christmas"
    assert approved.json()["decided_by"] == str(world.operator.user_id)
    assert again.status_code == 409
    assert [r.outcome for r in world.resumer.resolutions] == [ApprovalStatus.APPROVED]


def test_reject(api: Api, world: World) -> None:
    request = _pending(world)

    response = api.client.post(f"{api.base()}/approvals/{request.id}/reject", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_viewer_gets_403(api: Api, world: World) -> None:
    request = _pending(world)
    api.as_user(world.viewer)

    response = api.client.post(f"{api.base()}/approvals/{request.id}/approve", json={})

    assert response.status_code == 403


def test_hotel_owner_gets_403_for_code(api: Api, world: World) -> None:
    request = _pending(world, kind=ActionKind.CODE_CHANGE)
    api.as_user(world.owner)

    response = api.client.post(f"{api.base()}/approvals/{request.id}/approve", json={})

    assert response.status_code == 403


@pytest.mark.parametrize("who", ["hotel_a2_owner", "org_b_owner"])
def test_other_tenant_gets_404_never_403(api: Api, world: World, who: str) -> None:
    request = _pending(world)
    api.as_user(getattr(world, who))

    paths = [
        ("get", f"{api.base()}/approvals"),
        ("get", f"{api.base()}/approvals/{request.id}"),
        ("post", f"{api.base()}/approvals/{request.id}/approve"),
        ("post", f"{api.base()}/approvals/{request.id}/reject"),
        ("post", f"{api.base(world.org_b, world.hotel_b1)}/approvals/{request.id}/approve"),
    ]
    for method, path in paths:
        response = api.client.request(method, path, json={} if method == "post" else None)
        assert response.status_code == 404, (method, path, response.status_code)
    assert world.resumer.resolutions == []


def test_unknown_approval_is_404(api: Api) -> None:
    assert api.client.get(f"{api.base()}/approvals/{uuid4()}").status_code == 404


def test_decision_body_is_validated(api: Api, world: World) -> None:
    request = _pending(world)
    url = f"{api.base()}/approvals/{request.id}/approve"

    assert api.client.post(url, json={"reason": "x" * 1001}).status_code == 422
    assert api.client.post(url, json={"approved_by": "someone"}).status_code == 422


def test_admin_sets_approver_role(api: Api, world: World) -> None:
    api.as_user(world.admin)

    response = api.client.put(
        f"{api.base()}/approval-policy/approvers/pms_change", json={"min_role": "admin"}
    )

    assert response.status_code == 200
    assert response.json() == {"kind": "pms_change", "min_role": "admin"}
    request = _pending(world)
    api.as_user(world.operator)
    denied = api.client.post(f"{api.base()}/approvals/{request.id}/approve", json={})
    assert denied.status_code == 403


@pytest.mark.parametrize(
    ("who", "kind", "body", "code"),
    [
        ("operator", "pms_change", {"min_role": "admin"}, 403),
        ("org_b_owner", "pms_change", {"min_role": "admin"}, 404),
        ("admin", "code_change", {"min_role": "owner"}, 422),
        ("admin", "pms_change", {"min_role": "viewer"}, 422),
        ("admin", "wire_transfer", {"min_role": "admin"}, 422),
        ("admin", "pms_change", {"min_role": "superuser"}, 422),
    ],
)
def test_approver_role_setting_is_checked(
    api: Api, world: World, who: str, kind: str, body: dict[str, str], code: int
) -> None:
    api.as_user(getattr(world, who))

    response = api.client.put(f"{api.base()}/approval-policy/approvers/{kind}", json=body)

    assert response.status_code == code


def test_a_run_principal_gets_403_on_its_own_request(api: Api, world: World) -> None:
    request = _pending(world, risk=RiskLevel.HIGH)
    api.as_user(world.run)

    approve = api.client.post(f"{api.base()}/approvals/{request.id}/approve", json={})
    reject = api.client.post(f"{api.base()}/approvals/{request.id}/reject", json={})

    assert (approve.status_code, reject.status_code) == (403, 403)
