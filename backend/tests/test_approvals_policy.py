"""Policy evaluation contract: allow, require approval with approver roles, or deny; fail closed."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from approval_world import World

from harness.approvals import (
    ActionKind,
    Allow,
    Deny,
    Effect,
    HotelPolicy,
    HotelRole,
    InMemoryPolicyStore,
    PolicyEngine,
    RequireApproval,
    RiskLevel,
    Rule,
    TeamRole,
    payload_hash,
)

HOTEL_OPERATIONS = [
    ActionKind.MESSAGE_SEND,
    ActionKind.PMS_CHANGE,
    ActionKind.EXTERNAL_WRITE,
    ActionKind.DELETE,
]
CODE_KINDS = [ActionKind.GIT_PUSH, ActionKind.CODE_CHANGE]
OPERATOR_AND_UP = frozenset({HotelRole.OPERATOR, HotelRole.ADMIN, HotelRole.OWNER})


def test_read_only_action_is_allowed_without_approval() -> None:
    world = World()

    decision = world.engine.evaluate(world.action(ActionKind.READ, RiskLevel.LOW))

    assert isinstance(decision, Allow)


@pytest.mark.parametrize("kind", HOTEL_OPERATIONS)
def test_hotel_operation_requires_operator_or_higher_by_default(kind: ActionKind) -> None:
    world = World()

    decision = world.engine.evaluate(world.action(kind))

    assert isinstance(decision, RequireApproval)
    assert decision.approver_roles == OPERATOR_AND_UP


@pytest.mark.parametrize("kind", CODE_KINDS)
def test_code_action_requires_a_code_reviewer_and_no_hotel_role(kind: ActionKind) -> None:
    world = World()

    decision = world.engine.evaluate(world.action(kind, toolset="coding", tool="git"))

    assert isinstance(decision, RequireApproval)
    assert decision.approver_roles == frozenset({TeamRole.CODE_REVIEWER})


def test_hotel_admin_setting_narrows_the_approver_roles() -> None:
    world = World()
    world.set_policy(approver_min_roles={ActionKind.PMS_CHANGE: HotelRole.ADMIN})

    decision = world.engine.evaluate(world.action(ActionKind.PMS_CHANGE))

    assert isinstance(decision, RequireApproval)
    assert decision.approver_roles == frozenset({HotelRole.ADMIN, HotelRole.OWNER})


def test_platform_rule_denies_a_kind_in_one_toolset_only() -> None:
    world = World(platform_rules=[Rule(ActionKind.DELETE, Effect.DENY, toolset="connector-safe")])

    denied = world.engine.evaluate(world.action(ActionKind.DELETE, toolset="connector-safe"))
    elsewhere = world.engine.evaluate(world.action(ActionKind.DELETE, toolset="ops"))

    assert isinstance(denied, Deny)
    assert isinstance(elsewhere, RequireApproval)


def test_hotel_rule_can_deny_and_the_strictest_matching_rule_wins() -> None:
    world = World(platform_rules=[Rule(ActionKind.READ, Effect.ALLOW)])
    world.set_policy(rules=(Rule(ActionKind.READ, Effect.DENY, toolset="ops"),))

    assert isinstance(world.engine.evaluate(world.action(ActionKind.READ, toolset="ops")), Deny)
    assert isinstance(world.engine.evaluate(world.action(ActionKind.READ, toolset="coding")), Allow)


def test_hotel_rules_are_per_hotel() -> None:
    world = World()
    world.set_policy(rules=(Rule(ActionKind.MESSAGE_SEND, Effect.DENY),))
    other_hotel = world.tenant(hotel_id=world.hotel_a2)

    here = world.engine.evaluate(world.action(ActionKind.MESSAGE_SEND))
    there = world.engine.evaluate(world.action(ActionKind.MESSAGE_SEND, tenant=other_hotel))

    assert isinstance(here, Deny)
    assert isinstance(there, RequireApproval)


def test_platform_allow_rule_allows_a_low_risk_code_push() -> None:
    world = World(platform_rules=[Rule(ActionKind.GIT_PUSH, Effect.ALLOW, toolset="coding")])

    decision = world.engine.evaluate(
        world.action(ActionKind.GIT_PUSH, RiskLevel.LOW, toolset="coding", tool="git")
    )

    assert isinstance(decision, Allow)


@pytest.mark.parametrize("kind", HOTEL_OPERATIONS)
def test_no_yolo_an_allow_rule_never_skips_approval_of_a_hotel_operation(kind: ActionKind) -> None:
    world = World(platform_rules=[Rule(kind, Effect.ALLOW)])
    world.set_policy(rules=(Rule(kind, Effect.ALLOW),))

    decision = world.engine.evaluate(world.action(kind, RiskLevel.LOW))

    assert isinstance(decision, RequireApproval)


def test_no_yolo_an_allow_rule_never_skips_approval_of_a_high_risk_action() -> None:
    world = World(platform_rules=[Rule(ActionKind.GIT_PUSH, Effect.ALLOW)])

    decision = world.engine.evaluate(
        world.action(ActionKind.GIT_PUSH, RiskLevel.HIGH, toolset="coding", tool="git")
    )

    assert isinstance(decision, RequireApproval)
    assert decision.approver_roles == frozenset({TeamRole.CODE_REVIEWER})


# --- Deny by default -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"kind": "wire_transfer"}, "unknown action kind"),
        ({"risk": "extreme"}, "unknown risk level"),
        ({"payload": {"when": object()}}, "not plain JSON"),
        ({"payload": {"rate": float("nan")}}, "not plain JSON"),
        ({"toolset": ""}, "needs a toolset"),
        ({"run_id": ""}, "needs a toolset"),
    ],
)
def test_bad_or_unknown_input_is_denied(overrides: dict[str, Any], reason: str) -> None:
    world = World()

    decision = world.engine.evaluate(world.action(**overrides))

    assert isinstance(decision, Deny)
    assert reason in decision.reason


def test_known_kind_given_as_plain_string_is_still_evaluated() -> None:
    world = World()

    decision = world.engine.evaluate(world.action("pms_change", "medium"))

    assert isinstance(decision, RequireApproval)


def test_action_without_tenant_context_is_denied() -> None:
    world = World()
    action = world.action()
    object.__setattr__(action, "tenant", {"org_id": str(world.org_a)})

    assert isinstance(world.engine.evaluate(action), Deny)


def test_policy_store_error_becomes_deny_not_allow() -> None:
    class BrokenStore(InMemoryPolicyStore):
        def get(self, org_id: Any, hotel_id: Any) -> HotelPolicy | None:
            raise RuntimeError("database is down")

    world = World()
    engine = PolicyEngine(BrokenStore())

    for kind in ActionKind:
        decision = engine.evaluate(world.action(kind, RiskLevel.LOW))
        assert isinstance(decision, Deny), kind
        assert decision.reason == "policy evaluation failed"


# --- Hotel policy settings -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"approver_min_roles": {ActionKind.PMS_CHANGE: HotelRole.VIEWER}}, "viewers"),
        ({"approver_min_roles": {ActionKind.GIT_PUSH: HotelRole.OWNER}}, "never approve code"),
        ({"approver_min_roles": {ActionKind.READ: HotelRole.ADMIN}}, "need no approval"),
        ({"rules": (Rule(ActionKind.CODE_CHANGE, Effect.ALLOW),)}, "code actions"),
        ({"approval_ttl": timedelta(seconds=10)}, "approval_ttl"),
        ({"approval_ttl": timedelta(days=30)}, "approval_ttl"),
    ],
)
def test_hotel_policy_rejects_settings_the_design_forbids(
    kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        HotelPolicy(org_id=uuid4(), hotel_id=uuid4(), **kwargs)


# --- Payload hash ------------------------------------------------------------------------------


def test_payload_hash_ignores_key_order_but_binds_every_field_of_the_proposal() -> None:
    world = World()
    base = world.action(payload={"a": 1, "b": [1, 2]})

    same = world.action(payload={"b": [1, 2], "a": 1})
    variants = [
        world.action(payload={"a": 2, "b": [1, 2]}),
        world.action(payload={"a": 1.0, "b": [1, 2]}),
        world.action(ActionKind.EXTERNAL_WRITE, payload={"a": 1, "b": [1, 2]}),
        world.action(risk=RiskLevel.LOW, payload={"a": 1, "b": [1, 2]}),
        world.action(tool="pms_other", payload={"a": 1, "b": [1, 2]}),
        world.action(toolset="coding", payload={"a": 1, "b": [1, 2]}),
        world.action(tenant=world.tenant(hotel_id=world.hotel_a2), payload={"a": 1, "b": [1, 2]}),
    ]

    assert payload_hash(base) == payload_hash(same)
    assert payload_hash(base).startswith("sha256:")
    assert len({payload_hash(base), *(payload_hash(v) for v in variants)}) == len(variants) + 1
