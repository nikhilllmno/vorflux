"""Who may see and decide approvals: tenant scope, hotel and team roles, people versus runs,
self-approval, and the approver roles that hotel admins set."""

from typing import Any
from uuid import UUID

import pytest
from approval_world import World

from harness.approvals import (
    ActionKind,
    ApprovalStatus,
    ConflictError,
    Effect,
    ForbiddenError,
    HotelPolicy,
    HotelRole,
    InMemoryPolicyStore,
    InvalidPolicyError,
    NotFoundError,
    Principal,
    RiskLevel,
    Rule,
)

pytestmark = pytest.mark.anyio


# --- Tenant scope ------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["hotel_a2_owner", "org_b_owner"])
async def test_another_tenant_gets_not_found_for_every_operation(who: str) -> None:
    world = World()
    _, request = await world.request()
    outsider: Principal = getattr(world, who)
    tenant = world.tenant(world.org_a, world.hotel_a1, outsider)

    with pytest.raises(NotFoundError):
        await world.service.list_for(outsider, tenant)
    with pytest.raises(NotFoundError):
        await world.service.get_for(outsider, tenant, request.id)
    with pytest.raises(NotFoundError):
        await world.service.approve(outsider, tenant, request.id)
    with pytest.raises(NotFoundError):
        await world.service.reject(outsider, tenant, request.id)
    assert world.resumer.resolutions == []


async def test_an_approval_id_from_another_hotel_is_not_found_in_your_hotel() -> None:
    world = World()
    _, request = await world.request()
    owner_a2 = world.hotel_a2_owner

    with pytest.raises(NotFoundError):
        await world.service.approve(
            owner_a2, world.tenant(world.org_a, world.hotel_a2, owner_a2), request.id
        )


async def test_tenant_context_must_belong_to_the_principal() -> None:
    world = World()
    _, request = await world.request()
    borrowed = world.tenant(world.org_a, world.hotel_a1, world.operator)

    with pytest.raises(ValueError, match="actor_id"):
        await world.service.approve(world.viewer, borrowed, request.id)
    assert await world.approvals.get(world.org_a, world.hotel_a1, request.id) == request


# --- Hotel roles -------------------------------------------------------------------------------


async def test_org_wide_admin_approves_in_every_hotel_of_the_org() -> None:
    world = World()
    _, request = await world.request(world.call(tenant=world.tenant(world.org_a, world.hotel_a2)))

    approved = await world.approve(request, world.org_a_admin)

    assert approved.status == ApprovalStatus.APPROVED


async def test_viewer_can_see_but_cannot_decide() -> None:
    world = World()
    _, request = await world.request()
    tenant = world.tenant(actor=world.viewer)

    assert [r.id for r in await world.service.list_for(world.viewer, tenant)] == [request.id]
    with pytest.raises(ForbiddenError):
        await world.approve(request, world.viewer)
    with pytest.raises(ForbiddenError):
        await world.reject(request, world.viewer)


async def test_operator_cannot_approve_when_the_hotel_requires_admin() -> None:
    world = World()
    await world.set_policy(approver_min_roles={ActionKind.PMS_CHANGE: HotelRole.ADMIN})
    _, request = await world.request()

    with pytest.raises(ForbiddenError):
        await world.approve(request, world.operator)
    assert (await world.approve(request, world.owner)).status == ApprovalStatus.APPROVED


async def test_a_raised_minimum_role_also_protects_pending_requests() -> None:
    # Decision to confirm: a decision needs a role that both the snapshot and the current
    # policy allow (the stricter of the two).
    world = World()
    _, request = await world.request()
    await world.set_policy(approver_min_roles={ActionKind.PMS_CHANGE: HotelRole.ADMIN})

    with pytest.raises(ForbiddenError):
        await world.approve(request, world.operator)
    assert (await world.approve(request, world.admin)).status == ApprovalStatus.APPROVED


async def test_a_lowered_minimum_role_does_not_widen_a_pending_request() -> None:
    world = World()
    await world.set_policy(approver_min_roles={ActionKind.PMS_CHANGE: HotelRole.OWNER})
    _, request = await world.request()
    await world.set_policy(approver_min_roles={ActionKind.PMS_CHANGE: HotelRole.OPERATOR})

    with pytest.raises(ForbiddenError):
        await world.approve(request, world.admin)


@pytest.mark.parametrize("kind", [ActionKind.GIT_PUSH, ActionKind.CODE_CHANGE])
async def test_hotel_roles_can_never_approve_code(kind: ActionKind) -> None:
    world = World()
    _, request = await world.request(world.call(kind))

    for hotel_person in (world.owner, world.admin, world.operator, world.org_a_admin):
        with pytest.raises(ForbiddenError):
            await world.approve(request, hotel_person)
    assert (await world.approve(request, world.code_reviewer)).status == ApprovalStatus.APPROVED


async def test_code_reviewer_sees_only_code_requests_of_a_hotel() -> None:
    world = World()
    _, pms = await world.request()
    _, code = await world.request(world.call(ActionKind.GIT_PUSH, tool_call_id="call-2"))
    tenant = world.tenant(actor=world.code_reviewer)

    visible = await world.service.list_for(world.code_reviewer, tenant)

    assert [r.id for r in visible] == [code.id]
    with pytest.raises(NotFoundError):
        await world.service.get_for(world.code_reviewer, tenant, pms.id)


async def test_platform_team_role_sees_and_decides_nothing() -> None:
    world = World()
    _, request = await world.request(world.call(ActionKind.GIT_PUSH))
    tenant = world.tenant(actor=world.platform)

    with pytest.raises(NotFoundError):
        await world.service.list_for(world.platform, tenant)
    with pytest.raises(NotFoundError):
        await world.service.approve(world.platform, tenant, request.id)


async def test_list_filters_by_status() -> None:
    world = World()
    _, first = await world.request()
    _, second = await world.request(world.call(tool_call_id="call-2"))
    await world.approve(first, world.operator)
    tenant = world.tenant(actor=world.operator)

    approved = await world.service.list_for(world.operator, tenant, ApprovalStatus.APPROVED)
    pending = await world.service.list_for(world.operator, tenant, ApprovalStatus.PENDING)

    assert [r.id for r in approved] == [first.id]
    assert [r.id for r in pending] == [second.id]


# --- People versus runs, and self-approval -----------------------------------------------------


async def test_the_run_can_never_decide_its_own_request() -> None:
    # Review probe: a principal built from a run token has the requester's id and roles.
    world = World()
    _, request = await world.request(world.call(risk=RiskLevel.HIGH))

    for decide in (world.approve, world.reject):
        with pytest.raises(ForbiddenError, match="only a person"):
            await decide(request, world.run)
    stored = await world.approvals.get(world.org_a, world.hotel_a1, request.id)
    assert stored.status == ApprovalStatus.PENDING


async def test_a_run_principal_cannot_change_approver_roles() -> None:
    world = World()
    run_admin = Principal(world.admin.user_id, "run", world.admin.memberships)  # type: ignore[arg-type]

    with pytest.raises(ForbiddenError):
        await world.service.set_approver_min_role(
            run_admin, world.tenant(actor=run_admin), ActionKind.PMS_CHANGE, HotelRole.ADMIN
        )


def test_a_principal_must_say_if_it_is_a_person_or_a_run() -> None:
    with pytest.raises(TypeError):
        Principal(UUID(int=1))  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        Principal(UUID(int=1), "robot")  # type: ignore[arg-type]


async def test_requester_may_approve_their_own_medium_risk_action() -> None:
    world = World()
    _, request = await world.request()

    assert (await world.approve(request, world.requester)).status == ApprovalStatus.APPROVED


async def test_requester_cannot_approve_their_own_high_risk_action_by_default() -> None:
    # Decision to confirm: forbidden by default; a hotel can turn it off.
    world = World()
    _, request = await world.request(world.call(risk=RiskLevel.HIGH))

    with pytest.raises(ForbiddenError, match="their own high-risk"):
        await world.approve(request, world.requester)
    assert (await world.approve(request, world.operator)).status == ApprovalStatus.APPROVED


async def test_hotel_can_allow_self_approval_of_high_risk() -> None:
    world = World()
    await world.set_policy(forbid_self_approval_of_high_risk=False)
    _, request = await world.request(world.call(risk=RiskLevel.HIGH))

    assert (await world.approve(request, world.requester)).status == ApprovalStatus.APPROVED


async def test_requester_may_still_reject_their_own_high_risk_action() -> None:
    world = World()
    _, request = await world.request(world.call(risk=RiskLevel.HIGH))

    assert (await world.reject(request, world.requester)).status == ApprovalStatus.REJECTED


async def test_a_high_risk_value_stored_as_a_string_still_blocks_self_approval() -> None:
    # Review probe: a row read back from a database holds plain strings.
    world = World()
    _, request = await world.request(world.call(risk=RiskLevel.HIGH))
    row = {f: getattr(request, f) for f in request.__dataclass_fields__}
    row.update(risk="high", kind="pms_change", status="pending", approver_roles=["operator"])
    stored = type(request)(**row)
    world.approvals._rows[(request.org_id, request.hotel_id, request.id)] = stored

    with pytest.raises(ForbiddenError, match="their own high-risk"):
        await world.approve(stored, world.requester)


@pytest.mark.parametrize("risk", ["extreme", "", None])
async def test_an_unknown_stored_risk_is_read_as_high(risk: Any) -> None:
    world = World()
    _, request = await world.request()
    row = {f: getattr(request, f) for f in request.__dataclass_fields__}
    row["risk"] = risk

    assert type(request)(**row).risk == RiskLevel.HIGH


# --- Approver roles set by hotel admins --------------------------------------------------------


async def test_hotel_admin_sets_approver_role_with_an_audit_event() -> None:
    world = World()
    tenant = world.tenant(actor=world.admin)

    policy = await world.service.set_approver_min_role(
        world.admin, tenant, ActionKind.MESSAGE_SEND, HotelRole.ADMIN
    )

    assert policy.min_role_for(ActionKind.MESSAGE_SEND) == HotelRole.ADMIN
    event = world.audit.events[-1]
    assert event.action == "approval_policy.updated"
    assert dict(event.details) == {
        "kind": "message_send",
        "min_role": "admin",
        "previous_min_role": "operator",
    }


async def test_only_admins_of_the_hotel_set_approver_roles() -> None:
    world = World()

    async def attempt(who: Principal) -> None:
        tenant = world.tenant(world.org_a, world.hotel_a1, who)
        await world.service.set_approver_min_role(
            who, tenant, ActionKind.PMS_CHANGE, HotelRole.ADMIN
        )

    with pytest.raises(ForbiddenError):
        await attempt(world.operator)
    for outsider in (world.hotel_a2_owner, world.org_b_owner, world.code_reviewer):
        with pytest.raises(NotFoundError):
            await attempt(outsider)
    assert world.audit.events == ()


@pytest.mark.parametrize(
    ("kind", "role"),
    [
        (ActionKind.PMS_CHANGE, HotelRole.VIEWER),
        (ActionKind.GIT_PUSH, HotelRole.OWNER),
        (ActionKind.READ, HotelRole.ADMIN),
    ],
)
async def test_admin_cannot_set_a_forbidden_approver_role(
    kind: ActionKind, role: HotelRole
) -> None:
    world = World()

    with pytest.raises(InvalidPolicyError):
        await world.service.set_approver_min_role(
            world.admin, world.tenant(actor=world.admin), kind, role
        )


async def test_first_policy_writes_from_two_admins_never_lose_a_setting() -> None:
    # Another admin inserts the first policy between this admin's read and write.
    world: World

    class RacingStore(InMemoryPolicyStore):
        raced = False

        async def get(self, org_id: UUID, hotel_id: UUID) -> HotelPolicy | None:
            seen = await super().get(org_id, hotel_id)
            if not self.raced:
                self.raced = True
                other = world.tenant(org_id, hotel_id, world.owner)
                await world.service.set_approver_min_role(
                    world.owner, other, ActionKind.PMS_CHANGE, HotelRole.OWNER
                )
            return seen

    world = World(policies=RacingStore())
    world.policies.audit = world.audit
    tenant = world.tenant(actor=world.admin)

    with pytest.raises(ConflictError):
        await world.service.set_approver_min_role(
            world.admin, tenant, ActionKind.MESSAGE_SEND, HotelRole.ADMIN
        )
    stored = await world.policies.get(world.org_a, world.hotel_a1)
    assert stored is not None
    assert stored.min_role_for(ActionKind.PMS_CHANGE) == HotelRole.OWNER
    assert stored.min_role_for(ActionKind.MESSAGE_SEND) == HotelRole.OPERATOR
    assert world.audit_actions() == ["approval_policy.updated"]


# --- Who sees the sensitive parts of a request (review 2, N4) -----------------------------------

HIDDEN = "(hidden: only approvers see this)"


async def test_a_member_who_cannot_decide_sees_a_redacted_card() -> None:
    world = World()
    _, request = await world.request(world.call(ActionKind.MESSAGE_SEND), note="VIP guest")

    view = await world.service.get_for(world.viewer, world.tenant(actor=world.viewer), request.id)
    listed = await world.service.list_for(world.viewer, world.tenant(actor=world.viewer))

    assert view.full is False
    assert listed == [view]
    assert view.request.preview.text() == (
        "Send an email to 1 recipient via Beds24\n"
        f"Recipients: {HIDDEN}\nSubject: {HIDDEN}\nMessage: {HIDDEN}\n"
        "Tool: ops/message_send_medium"
    )
    assert view.request.note is None
    assert dict(view.request.args) == {}
    assert "guest@example.com" not in repr(view)
    assert "VIP guest" not in repr(view)
    # Not sensitive: the card still says what happens, where, and its state.
    assert (view.id, view.request.status, view.request.kind) == (
        request.id,
        ApprovalStatus.PENDING,
        ActionKind.MESSAGE_SEND,
    )


async def test_a_principal_who_can_decide_sees_the_full_card() -> None:
    world = World()
    _, request = await world.request(world.call(ActionKind.MESSAGE_SEND), note="VIP guest")

    view = await world.service.get_for(
        world.operator, world.tenant(actor=world.operator), request.id
    )

    assert view.full is True
    assert view.request == request


@pytest.mark.parametrize("kind", [ActionKind.EXTERNAL_WRITE, ActionKind.READ])
async def test_written_values_and_read_arguments_are_sensitive(kind: ActionKind) -> None:
    world = World()
    if kind == ActionKind.READ:
        await world.set_policy(rules=(Rule(ActionKind.READ, Effect.ASK),))
    _, request = await world.request(world.call(kind))

    view = await world.service.get_for(world.viewer, world.tenant(actor=world.viewer), request.id)

    hidden = {f.label for f in view.request.preview.fields if f.value == HIDDEN}
    assert hidden == {f.label for f in request.preview.fields if f.sensitive}
    assert hidden and "Tool" not in hidden


async def test_a_raised_minimum_role_also_hides_the_card_from_operators() -> None:
    world = World()
    _, request = await world.request(world.call(ActionKind.MESSAGE_SEND))
    await world.set_policy(approver_min_roles={ActionKind.MESSAGE_SEND: HotelRole.ADMIN})

    operator = await world.service.get_for(
        world.operator, world.tenant(actor=world.operator), request.id
    )
    admin = await world.service.get_for(world.admin, world.tenant(actor=world.admin), request.id)

    assert (operator.full, admin.full) == (False, True)


async def test_only_a_code_reviewer_sees_the_full_card_of_a_code_request() -> None:
    world = World()
    _, request = await world.request(world.call(ActionKind.GIT_PUSH), note="fix rates")

    owner = await world.service.get_for(world.owner, world.tenant(actor=world.owner), request.id)
    reviewer = await world.service.get_for(
        world.code_reviewer, world.tenant(actor=world.code_reviewer), request.id
    )

    assert (owner.full, owner.request.note) == (False, None)
    assert (reviewer.full, reviewer.request.note) == (True, "fix rates")
