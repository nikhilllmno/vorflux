"""Policy contract: per-toolset and per-hotel allow/ask/deny, no yolo, the tool registry, typed
previews, and deny by default (docs/approvals.md, "Evaluation order")."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from approval_world import DEFAULT_ARGS, World, preview_context, tool_name

from harness.approvals import (
    ActionKind,
    Decision,
    Effect,
    HotelPolicy,
    HotelRole,
    InMemoryPolicyStore,
    PolicyEngine,
    ProposedCall,
    RiskLevel,
    Rule,
    TeamRole,
    ToolCatalog,
    ToolSpec,
    Verdict,
    proposal_hash,
    render_preview,
)

pytestmark = pytest.mark.anyio

HOTEL_OPERATIONS = [
    ActionKind.MESSAGE_SEND,
    ActionKind.PMS_CHANGE,
    ActionKind.EXTERNAL_WRITE,
    ActionKind.DELETE,
]
CODE_KINDS = [ActionKind.GIT_PUSH, ActionKind.CODE_CHANGE]
HOTEL_ROLES_FROM_OPERATOR = frozenset({HotelRole.OPERATOR, HotelRole.ADMIN, HotelRole.OWNER})


async def _evaluate(world: World, call: ProposedCall | None = None) -> Decision:
    return await world.service.evaluate(call or world.call())


async def test_read_only_action_is_allowed_without_approval() -> None:
    world = World()

    decision = await _evaluate(world, world.call(ActionKind.READ, RiskLevel.LOW))

    assert decision.verdict == Verdict.ALLOW
    assert decision.tool_call_id == "call-1"


@pytest.mark.parametrize("kind", HOTEL_OPERATIONS)
async def test_hotel_operation_requires_operator_or_higher_by_default(kind: ActionKind) -> None:
    world = World()

    decision = await _evaluate(world, world.call(kind, RiskLevel.LOW))

    assert decision.verdict == Verdict.ASK
    assert decision.approver_roles == HOTEL_ROLES_FROM_OPERATOR


@pytest.mark.parametrize("kind", CODE_KINDS)
async def test_code_action_requires_a_code_reviewer_and_no_hotel_role(kind: ActionKind) -> None:
    world = World()

    decision = await _evaluate(world, world.call(kind))

    assert decision.verdict == Verdict.ASK
    assert decision.approver_roles == frozenset({TeamRole.CODE_REVIEWER})


async def test_hotel_admin_setting_narrows_the_approver_roles() -> None:
    world = World()
    await world.set_policy(approver_min_roles={ActionKind.PMS_CHANGE: HotelRole.ADMIN})

    decision = await _evaluate(world)

    assert decision.approver_roles == frozenset({HotelRole.ADMIN, HotelRole.OWNER})


async def test_platform_rule_denies_a_kind_in_one_toolset_only() -> None:
    world = World(platform_rules=[Rule(ActionKind.MESSAGE_SEND, Effect.DENY, toolset="reports")])

    in_reports = await _evaluate(world, world.call(ActionKind.MESSAGE_SEND, toolset="reports"))
    in_ops = await _evaluate(world, world.call(ActionKind.MESSAGE_SEND, toolset="ops"))

    assert in_reports.verdict == Verdict.DENY
    assert in_ops.verdict == Verdict.ASK


async def test_hotel_rule_can_deny_and_the_strictest_matching_rule_wins() -> None:
    world = World(platform_rules=[Rule(ActionKind.READ, Effect.ALLOW)])
    await world.set_policy(rules=(Rule(ActionKind.READ, Effect.DENY, toolset="reports"),))

    denied = await _evaluate(world, world.call(ActionKind.READ, toolset="reports"))
    allowed = await _evaluate(world, world.call(ActionKind.READ, toolset="ops"))

    assert denied.verdict == Verdict.DENY
    assert allowed.verdict == Verdict.ALLOW


async def test_hotel_rules_are_per_hotel() -> None:
    world = World()
    await world.set_policy(rules=(Rule(ActionKind.READ, Effect.DENY),))
    other_hotel = world.tenant(world.org_a, world.hotel_a2)

    here = await _evaluate(world, world.call(ActionKind.READ))
    there = await _evaluate(world, world.call(ActionKind.READ, tenant=other_hotel))

    assert here.verdict == Verdict.DENY
    assert there.verdict == Verdict.ALLOW


async def test_read_can_be_made_to_ask_and_its_preview_lists_every_argument() -> None:
    world = World()
    await world.set_policy(rules=(Rule(ActionKind.READ, Effect.ASK),))

    _, request = await world.request(world.call(ActionKind.READ))

    assert request.approver_roles == HOTEL_ROLES_FROM_OPERATOR
    assert request.preview.text() == (
        "Read data\ndate: 2026-12-24\nreport: occupancy\nTool: ops/read_medium"
    )


# --- No yolo -----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", HOTEL_OPERATIONS)
async def test_no_yolo_an_allow_rule_never_skips_approval_of_a_hotel_operation(
    kind: ActionKind,
) -> None:
    world = World(platform_rules=[Rule(kind, Effect.ALLOW)])
    await world.set_policy(rules=(Rule(kind, Effect.ALLOW),))

    decision = await _evaluate(world, world.call(kind, RiskLevel.LOW))

    assert decision.verdict == Verdict.ASK


@pytest.mark.parametrize("kind", CODE_KINDS)
async def test_no_yolo_a_platform_allow_rule_never_skips_approval_of_code(
    kind: ActionKind,
) -> None:
    # Decision to confirm: code kinds always ask, even under a platform allow rule.
    world = World(platform_rules=[Rule(kind, Effect.ALLOW)])

    decision = await _evaluate(world, world.call(kind, RiskLevel.LOW))

    assert decision.verdict == Verdict.ASK
    assert decision.approver_roles == frozenset({TeamRole.CODE_REVIEWER})


async def test_no_yolo_an_allow_rule_never_skips_approval_of_a_high_risk_action() -> None:
    world = World(platform_rules=[Rule(ActionKind.READ, Effect.ALLOW)])

    decision = await _evaluate(world, world.call(ActionKind.READ, RiskLevel.HIGH))

    assert decision.verdict == Verdict.ASK


# --- The tool registry -------------------------------------------------------------------------


async def test_unregistered_tool_is_denied() -> None:
    world = World()

    decision = await _evaluate(world, world.call(toolset="totally_new", tool="mystery_tool"))

    assert decision.verdict == Verdict.DENY
    assert decision.reason == "tool totally_new/mystery_tool is not registered"
    assert world.audit_actions() == ["policy.denied"]


async def test_registered_tool_in_another_toolset_is_denied() -> None:
    world = World(catalog=ToolCatalog([ToolSpec("ops", "read_low", "read", "low")]))

    allowed = await _evaluate(world, world.call(toolset="ops", tool="read_low"))
    elsewhere = await _evaluate(world, world.call(toolset="reports", tool="read_low"))

    assert allowed.verdict == Verdict.ALLOW
    assert elsewhere.verdict == Verdict.DENY


async def test_the_registry_sets_kind_and_risk_not_the_caller() -> None:
    # The tool's name says "read" but the registry says it changes the PMS at high risk.
    catalog = ToolCatalog(
        [ToolSpec("ops", "read_rates", ActionKind.PMS_CHANGE, RiskLevel.HIGH, system="Beds24")]
    )
    world = World(catalog=catalog)

    _, request = await world.request(world.call(toolset="ops", tool="read_rates"))

    assert (request.kind, request.risk) == (ActionKind.PMS_CHANGE, RiskLevel.HIGH)


def test_registry_refuses_a_duplicate_or_an_unknown_kind_or_risk() -> None:
    spec = ToolSpec("ops", "t", ActionKind.READ, RiskLevel.LOW)
    with pytest.raises(ValueError, match="twice"):
        ToolCatalog([spec, spec])
    with pytest.raises(ValueError):
        ToolSpec("ops", "t", "wire_transfer", RiskLevel.LOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ToolSpec("ops", "t", ActionKind.READ, "extreme")  # type: ignore[arg-type]


# --- Typed previews ----------------------------------------------------------------------------


def test_pms_change_preview_is_rendered_from_the_arguments() -> None:
    context = preview_context(ActionKind.PMS_CHANGE)
    preview = render_preview(ActionKind.PMS_CHANGE, DEFAULT_ARGS[ActionKind.PMS_CHANGE], context)

    assert preview.text() == (
        "Change rate of Deluxe King, 24 Dec 2026 in Beds24\n"
        "From (agent reported, not checked): 9000\n"
        "To: 9900\n"
        "Guests affected (agent reported, not checked): 0\n"
        "Tool: ops/pms_change_medium"
    )


@pytest.mark.parametrize("kind", [*HOTEL_OPERATIONS, *CODE_KINDS])
def test_every_kind_that_can_ask_has_a_typed_preview(kind: ActionKind) -> None:
    preview = render_preview(kind, DEFAULT_ARGS[kind], preview_context(kind))

    assert preview.title
    assert preview.fields


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"note_to_self": "x"}, "unknown field"),
        ({"guests_affected": "3"}, "a string is not coerced to a number"),
        ({"guests_affected": -1}, "negative count"),
        ({"setting": ""}, "empty text"),
        ({"system": "Sandbox (test copy, no guests)"}, "the system comes from the registry"),
        ({"target": "Deluxe King"}, "free-text targets are not accepted"),
        ({"date": "2026-13-40"}, "not a real date"),
        ({"date": "24 Dec 2026"}, "not an ISO date"),
    ],
)
async def test_arguments_that_do_not_fit_the_kind_are_denied(
    change: dict[str, Any], why: str
) -> None:
    world = World()
    args = {**DEFAULT_ARGS[ActionKind.PMS_CHANGE], **change}

    decision = await _evaluate(world, world.call(args=args))

    assert decision.verdict == Verdict.DENY, why
    assert decision.reason == "arguments do not match the pms_change proposal"


# Review 2 (N2): the system and the target come from the registry and the hotel config, never
# from the agent. Values that only the agent knows are marked as not checked.


async def test_the_system_in_the_preview_comes_from_the_registry() -> None:
    world = World()

    _, ops = await world.request()
    _, reports = await world.request(world.call(toolset="reports", tool_call_id="call-2"))

    assert ops.preview.title == "Change rate of Deluxe King, 24 Dec 2026 in Beds24"
    assert reports.preview.title == "Change rate of Deluxe King, 24 Dec 2026 in Channel manager"


async def test_the_target_label_comes_from_the_hotel_config() -> None:
    world = World()
    world.directory.set_label(
        world.org_a, world.hotel_a1, "ops", "room-type:dk", "Deluxe King (sea view)"
    )

    _, request = await world.request()

    assert request.preview.title == "Change rate of Deluxe King (sea view), 24 Dec 2026 in Beds24"


@pytest.mark.parametrize(
    "kind", [ActionKind.PMS_CHANGE, ActionKind.EXTERNAL_WRITE, ActionKind.DELETE]
)
async def test_a_target_that_the_hotel_config_does_not_know_is_denied(kind: ActionKind) -> None:
    world = World()
    args = {**DEFAULT_ARGS[kind], "target_id": "room-type:penthouse"}

    decision = await _evaluate(world, world.call(kind, args=args))

    assert decision.verdict == Verdict.DENY
    assert decision.reason == "target is not in the hotel config for ops"


async def test_target_labels_are_scoped_to_the_hotel() -> None:
    world = World()
    world.directory.set_label(world.org_a, world.hotel_a2, "ops", "room-type:villa", "Villa")
    args = {**DEFAULT_ARGS[ActionKind.PMS_CHANGE], "target_id": "room-type:villa"}

    here = await _evaluate(world, world.call(args=args))
    there = await _evaluate(
        world, world.call(args=args, tenant=world.tenant(world.org_a, world.hotel_a2))
    )

    assert here.verdict == Verdict.DENY
    assert there.verdict == Verdict.ASK


async def test_values_that_only_the_agent_reports_are_marked_as_not_checked() -> None:
    world = World()

    _, request = await world.request()

    checked = {f.label: f.verified for f in request.preview.fields}
    assert checked == {"From": False, "To": True, "Guests affected": False, "Tool": True}
    assert request.preview.as_json()["fields"][0] == {
        "label": "From",
        "value": "9000",
        "verified": False,
        "sensitive": False,
    }


def test_a_code_change_description_is_marked_as_not_checked() -> None:
    # The description is agent prose. The branch and the files are what the approval binds.
    kind = ActionKind.CODE_CHANGE
    preview = render_preview(kind, DEFAULT_ARGS[kind], preview_context(kind))

    checked = {f.label: f.verified for f in preview.fields}
    assert checked == {"Branch": True, "Files": True, "Description": False, "Tool": True}
    assert "Description (agent reported, not checked):" in preview.text()


async def test_agent_reported_fields_are_left_out_when_the_agent_gives_none() -> None:
    world = World()
    args = {"target_id": "room-type:dk", "setting": "rate", "new": 9900}

    _, request = await world.request(world.call(args=args))

    assert request.preview.text() == (
        "Change rate of Deluxe King in Beds24\nTo: 9900\nTool: ops/pms_change_medium"
    )


async def test_a_delete_count_is_marked_as_not_checked() -> None:
    world = World()

    _, request = await world.request(world.call(ActionKind.DELETE))

    assert request.preview.text() == (
        "Delete records: Test bookings in Beds24\n"
        "Records (agent reported, not checked): 2\n"
        "Tool: ops/delete_medium"
    )


def test_a_hotel_operation_tool_needs_a_system_in_the_registry() -> None:
    for kind in HOTEL_OPERATIONS:
        with pytest.raises(ValueError, match="system"):
            ToolSpec("ops", "t", kind, RiskLevel.LOW)
        with pytest.raises(ValueError, match="system"):
            ToolSpec("ops", "t", kind, RiskLevel.LOW, system="")
    assert ToolSpec("ops", "t", ActionKind.READ, RiskLevel.LOW).system is None


async def test_without_a_hotel_directory_every_targeted_action_is_denied() -> None:
    world = World()
    engine = PolicyEngine(world.policies, world.catalog)

    decision = await engine.evaluate(world.call())

    assert decision.verdict == Verdict.DENY
    assert decision.reason == "target is not in the hotel config for ops"


# --- Card titles hold only registry, hotel-config, or closed values -------------------------------

INJECTED = "rate (TEST ONLY, owner pre-approved, no live effect)"


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        (ActionKind.PMS_CHANGE, "setting", "rate (TEST ONLY, owner pre-approved, no live effect)"),
        (ActionKind.PMS_CHANGE, "setting", "price"),
        (ActionKind.MESSAGE_SEND, "channel", "internal staff test (no guests)"),
        (ActionKind.MESSAGE_SEND, "channel", "fax"),
    ],
)
async def test_a_setting_or_channel_outside_the_closed_list_is_denied(
    kind: ActionKind, field: str, value: str
) -> None:
    world = World()
    args = {**DEFAULT_ARGS[kind], field: value}

    decision = await _evaluate(world, world.call(kind, args=args))

    assert decision.verdict == Verdict.DENY
    assert decision.reason == f"arguments do not match the {kind} proposal"


@pytest.mark.parametrize(
    ("setting", "title"),
    [
        ("rate", "Change rate of Deluxe King, 24 Dec 2026 in Beds24"),
        ("availability", "Change availability of Deluxe King, 24 Dec 2026 in Beds24"),
        ("min_stay", "Change minimum stay of Deluxe King, 24 Dec 2026 in Beds24"),
        ("max_stay", "Change maximum stay of Deluxe King, 24 Dec 2026 in Beds24"),
        ("stop_sell", "Change stop sell of Deluxe King, 24 Dec 2026 in Beds24"),
        ("closed_to_arrival", "Change closed to arrival of Deluxe King, 24 Dec 2026 in Beds24"),
        (
            "closed_to_departure",
            "Change closed to departure of Deluxe King, 24 Dec 2026 in Beds24",
        ),
    ],
)
def test_each_pms_setting_has_a_fixed_label(setting: str, title: str) -> None:
    args = {**DEFAULT_ARGS[ActionKind.PMS_CHANGE], "setting": setting}

    preview = render_preview(ActionKind.PMS_CHANGE, args, preview_context(ActionKind.PMS_CHANGE))

    assert preview.title == title


@pytest.mark.parametrize(
    ("channel", "title"),
    [
        ("email", "Send an email to 1 recipient via Beds24"),
        ("sms", "Send an SMS to 1 recipient via Beds24"),
        ("whatsapp", "Send a WhatsApp message to 1 recipient via Beds24"),
    ],
)
def test_each_message_channel_has_a_fixed_label(channel: str, title: str) -> None:
    args = {**DEFAULT_ARGS[ActionKind.MESSAGE_SEND], "channel": channel}

    preview = render_preview(
        ActionKind.MESSAGE_SEND, args, preview_context(ActionKind.MESSAGE_SEND)
    )

    assert preview.title == title


def _with_text(value: Any, text: str) -> Any:
    """`value` with every string in it replaced by `text` (keys of objects stay)."""
    if isinstance(value, str):
        return text
    if isinstance(value, list):
        return [_with_text(item, text) for item in value]
    if isinstance(value, dict):
        return {key: _with_text(item, text) for key, item in value.items()}
    return value


@pytest.mark.parametrize("kind", [*HOTEL_OPERATIONS, *CODE_KINDS])
async def test_agent_text_never_reaches_a_card_title(kind: ActionKind) -> None:
    # Put the injected text into each string argument in turn. Each call is either denied, or
    # its card title does not contain the text (the value is a field, not the title).
    world = World()
    for name, value in DEFAULT_ARGS[kind].items():
        args = {**DEFAULT_ARGS[kind], name: _with_text(value, INJECTED)}
        if args == dict(DEFAULT_ARGS[kind]):
            continue  # no string in this argument

        decision = await _evaluate(world, world.call(kind, args=args))

        if decision.verdict == Verdict.DENY:
            continue
        preview = render_preview(kind, args, preview_context(kind))
        assert "TEST ONLY" not in preview.title, name


async def test_an_unknown_target_id_is_not_copied_into_the_reason_or_the_audit_log() -> None:
    world = World()
    args = {**DEFAULT_ARGS[ActionKind.PMS_CHANGE], "target_id": "guest:jane.doe@example.com"}

    decision = await _evaluate(world, world.call(args=args))
    again = await _evaluate(world, world.call(args=args, tool_call_id="call-2"))
    other = {**args, "target_id": "guest:john@example.com"}
    await _evaluate(world, world.call(args=other, tool_call_id="call-3"))

    assert decision.reason == "target is not in the hotel config for ops"
    assert again.reason == decision.reason
    details = [dict(event.details) for event in world.audit.events]
    assert all("jane" not in v and "john" not in v for d in details for v in d.values())
    # A hash of the id lets support match two denials of the same id without logging it.
    hashes = [d["target_sha256"] for d in details]
    assert all(h.startswith("sha256:") for h in hashes)
    assert hashes[0] == hashes[1] != hashes[2]


# --- Deny by default ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{"run_id": ""}, {"tool_call_id": ""}, {"toolset": ""}, {"tool": ""}],
)
async def test_missing_identifiers_are_denied(overrides: dict[str, str]) -> None:
    world = World()
    fields: dict[str, Any] = {
        "tenant": world.tenant(),
        "run_id": "run-1",
        "tool_call_id": "call-1",
        "toolset": "ops",
        "tool": tool_name(ActionKind.READ, RiskLevel.LOW),
        **overrides,
    }

    decision = await _evaluate(world, ProposedCall(**fields))

    assert decision.verdict == Verdict.DENY


async def test_call_without_tenant_context_is_denied() -> None:
    world = World()
    call = ProposedCall(
        tenant=None,  # type: ignore[arg-type]
        run_id="run-1",
        tool_call_id="call-1",
        toolset="ops",
        tool=tool_name(ActionKind.READ, RiskLevel.LOW),
    )

    decision = await _evaluate(world, call)

    assert decision.verdict == Verdict.DENY
    assert decision.reason == "tool call has no tenant context"


@pytest.mark.parametrize(
    "args",
    [
        {"when": object()},
        {"rate": float("nan")},
        {1: "a"},
        {"nested": {2: "b"}},
        {"tags": {"a", "b"}},
    ],
)
def test_arguments_that_are_not_plain_json_cannot_build_a_call(args: Any) -> None:
    world = World()

    with pytest.raises((TypeError, ValueError)):
        world.call(args=args)


async def test_policy_store_error_becomes_deny_not_allow() -> None:
    class BrokenStore(InMemoryPolicyStore):
        async def get(self, org_id: Any, hotel_id: Any) -> HotelPolicy | None:
            raise RuntimeError("database is down")

    world = World(policies=BrokenStore())

    for kind in ActionKind:
        decision = await _evaluate(world, world.call(kind, RiskLevel.LOW))
        assert decision.verdict == Verdict.DENY
        assert decision.reason == "policy evaluation failed"


# --- Values loaded as plain strings (a database row) -------------------------------------------


async def test_a_stored_deny_rule_given_as_strings_stays_deny() -> None:
    world = World()
    await world.set_policy(rules=({"kind": "message_send", "effect": "deny"},))

    decision = await _evaluate(world, world.call(ActionKind.MESSAGE_SEND))

    assert decision.verdict == Verdict.DENY


async def test_a_stored_read_deny_rule_given_as_strings_applies() -> None:
    world = World()
    await world.set_policy(rules=(Rule("read", "deny"),))  # type: ignore[arg-type]

    decision = await _evaluate(world, world.call(ActionKind.READ, RiskLevel.LOW))

    assert decision.verdict == Verdict.DENY


async def test_stored_min_roles_given_as_strings_apply() -> None:
    world = World()
    await world.set_policy(approver_min_roles={"pms_change": "owner"})

    decision = await _evaluate(world)

    assert decision.approver_roles == frozenset({HotelRole.OWNER})


@pytest.mark.parametrize(
    "rule",
    [{"kind": "message_send", "effect": "maybe"}, {"kind": "wire_transfer", "effect": "deny"}],
)
def test_a_stored_rule_with_an_unknown_value_cannot_load(rule: dict[str, str]) -> None:
    # The store cannot build the policy, so `evaluate` gets an error and denies.
    with pytest.raises(ValueError):
        HotelPolicy(org_id=uuid4(), hotel_id=uuid4(), rules=(rule,))  # type: ignore[arg-type]


# --- Policy validation and the proposal hash ---------------------------------------------------


@pytest.mark.parametrize(
    "settings",
    [
        {"approver_min_roles": {ActionKind.PMS_CHANGE: HotelRole.VIEWER}},
        {"approver_min_roles": {ActionKind.GIT_PUSH: HotelRole.OWNER}},
        {"approver_min_roles": {ActionKind.READ: HotelRole.OWNER}},
        {"rules": (Rule(ActionKind.CODE_CHANGE, Effect.ALLOW),)},
        {"approval_ttl": timedelta(seconds=10)},
        {"approval_ttl": timedelta(days=30)},
        {"use_window": timedelta(seconds=10)},
        {"use_window": timedelta(days=2)},
    ],
)
def test_hotel_policy_rejects_settings_the_design_forbids(settings: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        HotelPolicy(org_id=uuid4(), hotel_id=uuid4(), **settings)


def test_hotel_policy_defaults_are_the_safe_ones() -> None:
    policy = HotelPolicy(org_id=uuid4(), hotel_id=uuid4())

    assert policy.forbid_self_approval_of_high_risk is True
    assert policy.use_window == timedelta(minutes=15)


def test_proposal_hash_ignores_key_order_but_binds_args_and_preview() -> None:
    world = World()
    args = DEFAULT_ARGS[ActionKind.PMS_CHANGE]
    call = world.call(args=args)
    reordered = world.call(args=dict(reversed(list(args.items()))))
    context = preview_context(ActionKind.PMS_CHANGE)
    preview = render_preview(ActionKind.PMS_CHANGE, args, context)
    kind, risk = ActionKind.PMS_CHANGE, RiskLevel.MEDIUM
    base = proposal_hash(call, kind, risk, preview)

    assert proposal_hash(reordered, kind, risk, preview) == base
    assert proposal_hash(world.call(args={**args, "new": 99}), kind, risk, preview) != base
    assert proposal_hash(call, kind, RiskLevel.HIGH, preview) != base
    other_preview = render_preview(ActionKind.PMS_CHANGE, {**args, "new": 99}, context)
    assert proposal_hash(call, kind, risk, other_preview) != base
    other_hotel = world.call(args=args, tenant=world.tenant(world.org_a, world.hotel_a2))
    assert proposal_hash(other_hotel, kind, risk, preview) != base
