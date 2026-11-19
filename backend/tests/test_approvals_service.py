"""Approval lifecycle contract: request, decide, resume, expire, bind to the payload, use once."""

from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from approval_world import World

from harness.approvals import (
    Action,
    ActionDeniedError,
    ActionKind,
    ApprovalNotRequiredError,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalStatus,
    AuditEvent,
    ConflictError,
    Effect,
    ForbiddenError,
    HotelPolicy,
    HotelRole,
    InMemoryApprovalStore,
    InMemoryAuditLog,
    InMemoryPolicyStore,
    InvalidPolicyError,
    NotFoundError,
    Principal,
    RiskLevel,
    Rule,
)


def _request(world: World, action: Action | None = None) -> tuple[Action, ApprovalRequest]:
    action = action or world.action()
    request = world.service.request_approval(
        action, call_id="call-1", summary="Change Deluxe King on 24 Dec from 9000 to 9900"
    )
    return action, request


def _approve(world: World, request: ApprovalRequest, who: Principal) -> ApprovalRequest:
    return world.service.approve(
        who, world.tenant(request.org_id, request.hotel_id, who), request.id
    )


def test_service_satisfies_the_adapter_interface() -> None:
    assert isinstance(World().service, ApprovalPolicy)


def test_request_creates_a_pending_approval_with_an_expiry_and_an_audit_event() -> None:
    world = World()

    action, request = _request(world)

    assert request.status is ApprovalStatus.PENDING
    assert request.expires_at == world.clock.now + timedelta(hours=24)
    assert request.requester_id == world.requester.user_id
    assert (request.org_id, request.hotel_id) == (world.org_a, world.hotel_a1)
    assert world.audit_actions() == ["approval.requested"]
    assert world.service.check(request.id, action).ok is False  # not approved yet


def test_request_uses_the_hotel_ttl() -> None:
    world = World()
    world.set_policy(approval_ttl=timedelta(minutes=30))

    _, request = _request(world)

    assert request.expires_at - request.created_at == timedelta(minutes=30)


def test_request_refuses_denied_and_allowed_actions() -> None:
    world = World(platform_rules=[Rule(ActionKind.DELETE, Effect.DENY)])

    with pytest.raises(ActionDeniedError):
        _request(world, world.action(ActionKind.DELETE))
    with pytest.raises(ActionDeniedError):
        _request(world, world.action("unknown_kind"))
    with pytest.raises(ApprovalNotRequiredError):
        _request(world, world.action(ActionKind.READ, RiskLevel.LOW))
    assert world.audit_actions() == []


# --- Approve, reject, and resume ---------------------------------------------------------------


def test_approve_resumes_the_run_and_the_approval_can_be_used_once() -> None:
    world = World()
    action, request = _request(world)

    approved = _approve(world, request, world.operator)

    assert approved.status is ApprovalStatus.APPROVED
    assert approved.decided_by == world.operator.user_id
    assert world.resumer.resolutions == [
        ApprovalResolution(
            approval_id=request.id,
            org_id=world.org_a,
            hotel_id=world.hotel_a1,
            run_id="run-1",
            call_id="call-1",
            outcome=ApprovalStatus.APPROVED,
            reason=None,
        )
    ]
    assert world.service.check(request.id, action).ok is True
    first = world.service.consume(request.id, action)
    assert first.ok is True
    assert world.audit_actions() == ["approval.requested", "approval.approved", "approval.consumed"]


def test_replay_a_used_approval_is_refused() -> None:
    world = World()
    action, request = _request(world)
    _approve(world, request, world.operator)
    assert world.service.consume(request.id, action).ok

    replay = world.service.consume(request.id, action)
    check = world.service.check(request.id, action)

    assert (replay.ok, replay.reason) == (False, "approval was already used")
    assert check.ok is False


def test_concurrent_use_wins_only_once() -> None:
    world = World()
    _, request = _request(world)
    approved = _approve(world, request, world.operator)
    used = replace(approved, consumed_at=world.clock.now, version=approved.version + 1)
    event = AuditEvent(
        org_id=used.org_id,
        hotel_id=used.hotel_id,
        actor_id=used.requester_id,
        action="approval.consumed",
        subject_type="approval_request",
        subject_id=str(used.id),
        at=world.clock.now,
    )

    assert world.approvals.replace(approved, used, event) is True
    assert world.approvals.replace(approved, used, event) is False  # stale version loses
    assert world.audit_actions().count("approval.consumed") == 1  # the loser writes no audit


def test_reject_resumes_the_run_and_the_action_cannot_run() -> None:
    world = World()
    action, request = _request(world)

    rejected = world.service.reject(
        world.operator,
        world.tenant(world.org_a, world.hotel_a1, world.operator),
        request.id,
        "Rate too high",
    )

    assert rejected.status is ApprovalStatus.REJECTED
    assert rejected.reason == "Rate too high"
    assert [r.outcome for r in world.resumer.resolutions] == [ApprovalStatus.REJECTED]
    assert world.service.consume(request.id, action).ok is False
    assert world.audit_actions()[-1] == "approval.rejected"


def test_a_decided_approval_cannot_be_decided_again() -> None:
    world = World()
    _, request = _request(world)
    _approve(world, request, world.operator)

    with pytest.raises(ConflictError):
        world.service.reject(
            world.admin, world.tenant(world.org_a, world.hotel_a1, world.admin), request.id
        )
    with pytest.raises(ConflictError):
        _approve(world, request, world.admin)


def test_resumer_failure_does_not_undo_the_decision() -> None:
    world = World()

    class BrokenResumer:
        def resume(self, resolution: ApprovalResolution) -> None:
            raise RuntimeError("orchestrator is down")

    world.service._resumer = BrokenResumer()
    action, request = _request(world)

    _approve(world, request, world.operator)

    assert world.service.consume(request.id, action).ok is True


class PausedRun:
    """A stand-in for the orchestrator: it holds the gated tool call and resumes it."""

    def __init__(self, world: World, action: Action) -> None:
        self.world = world
        self.action = action
        self.approval_id: UUID | None = None
        self.tool_calls: list[dict[str, Any]] = []
        self.tool_errors: list[str] = []

    def gate(self) -> None:
        decision = self.world.service.evaluate(self.action)
        assert decision.__class__.__name__ == "RequireApproval"
        self.approval_id = self.world.service.request_approval(
            self.action, call_id="call-1", summary="Raise the rate"
        ).id

    def resume(self, resolution: ApprovalResolution) -> None:
        assert resolution.approval_id == self.approval_id
        if resolution.outcome is not ApprovalStatus.APPROVED:
            self.tool_errors.append(f"not run: {resolution.outcome}")
            return
        check = self.world.service.consume(resolution.approval_id, self.action)
        if check.ok:
            self.tool_calls.append(dict(self.action.payload))
        else:
            self.tool_errors.append(check.reason)


@pytest.mark.parametrize("approve", [True, False])
def test_decision_resumes_the_paused_run(approve: bool) -> None:
    world = World()
    run = PausedRun(world, world.action())
    world.service._resumer = run
    run.gate()
    assert run.approval_id is not None

    if approve:
        world.service.approve(
            world.operator,
            world.tenant(world.org_a, world.hotel_a1, world.operator),
            run.approval_id,
        )
        assert run.tool_calls == [dict(run.action.payload)]
        assert run.tool_errors == []
    else:
        world.service.reject(
            world.operator,
            world.tenant(world.org_a, world.hotel_a1, world.operator),
            run.approval_id,
        )
        assert run.tool_calls == []
        assert run.tool_errors == ["not run: rejected"]


# --- Expiry --------------------------------------------------------------------------------


def test_pending_approval_expires_and_cannot_be_approved() -> None:
    world = World()
    action, request = _request(world)
    world.clock.advance(timedelta(hours=24))

    with pytest.raises(ConflictError, match="expired"):
        _approve(world, request, world.operator)

    stored = world.approvals.get(world.org_a, world.hotel_a1, request.id)
    assert stored is not None and stored.status is ApprovalStatus.EXPIRED
    assert [r.outcome for r in world.resumer.resolutions] == [ApprovalStatus.EXPIRED]
    assert world.audit_actions() == ["approval.requested", "approval.expired"]
    assert world.service.consume(request.id, action).ok is False


def test_pending_approval_is_still_valid_one_second_before_expiry() -> None:
    world = World()
    _, request = _request(world)
    world.clock.advance(timedelta(hours=24) - timedelta(seconds=1))

    assert _approve(world, request, world.operator).status is ApprovalStatus.APPROVED


def test_expiry_sweep_expires_only_due_requests_of_one_hotel() -> None:
    world = World()
    _, old = _request(world)
    world.clock.advance(timedelta(hours=23))
    _, fresh = _request(world, world.action(run_id="run-2"))
    _, other_hotel = _request(
        world, world.action(tenant=world.tenant(hotel_id=world.hotel_a2), run_id="run-3")
    )
    world.clock.advance(timedelta(hours=1))

    expired = world.service.expire_due(world.org_a, world.hotel_a1)

    assert [r.id for r in expired] == [old.id]
    statuses = {r.id: r.status for r in world.approvals.list(world.org_a, world.hotel_a1)}
    assert statuses == {old.id: ApprovalStatus.EXPIRED, fresh.id: ApprovalStatus.PENDING}
    still = world.approvals.get(world.org_a, world.hotel_a2, other_hotel.id)
    assert still is not None and still.status is ApprovalStatus.PENDING


def test_approved_but_unused_approval_cannot_be_used_after_expiry() -> None:
    world = World()
    action, request = _request(world)
    _approve(world, request, world.operator)
    world.clock.advance(timedelta(hours=24))

    result = world.service.consume(request.id, action)

    assert (result.ok, result.reason) == (False, "approval expired")


# --- Binding to the exact proposal ------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"payload": {"room": "Deluxe King", "date": "2026-12-24", "old": 9000, "new": 99000}},
        {"payload": {"room": "Suite", "date": "2026-12-24", "old": 9000, "new": 9900}},
        {"kind": ActionKind.EXTERNAL_WRITE},
        {"risk": RiskLevel.LOW},
        {"tool": "pms_set_inventory"},
        {"toolset": "coding"},
    ],
)
def test_changed_proposal_needs_a_new_approval(change: dict[str, Any]) -> None:
    world = World()
    action, request = _request(world)
    _approve(world, request, world.operator)
    changed = replace(action, **change)

    result = world.service.consume(request.id, changed)

    assert result.ok is False
    assert result.reason == "action changed after approval; a new approval is needed"
    assert world.service.consume(request.id, action).ok is True  # the original is untouched


def test_payload_mutated_in_place_after_approval_is_refused() -> None:
    world = World()
    payload = {"room": "Deluxe King", "new": 9900}
    action, request = _request(world, world.action(payload=payload))
    _approve(world, request, world.operator)

    payload["new"] = 19900

    assert world.service.consume(request.id, action).ok is False


def test_approval_cannot_be_used_by_another_run_or_requester() -> None:
    world = World()
    action, request = _request(world)
    _approve(world, request, world.operator)

    other_run = replace(action, run_id="run-2")
    other_requester = replace(action, tenant=world.tenant(actor=world.operator))

    assert world.service.consume(request.id, other_run).reason == "approval belongs to another run"
    assert world.service.consume(request.id, other_requester).ok is False


def test_approval_cannot_be_used_in_another_hotel_or_org() -> None:
    world = World()
    action, request = _request(world)
    _approve(world, request, world.operator)

    other_hotel = replace(action, tenant=world.tenant(hotel_id=world.hotel_a2))
    other_org = replace(action, tenant=world.tenant(org_id=world.org_b, hotel_id=world.hotel_b1))

    for wrong in (other_hotel, other_org):
        result = world.service.consume(request.id, wrong)
        assert (result.ok, result.reason) == (False, "approval not found")


def test_check_and_consume_fail_closed_on_store_errors() -> None:
    class BrokenStore(InMemoryApprovalStore):
        def get(self, org_id: Any, hotel_id: Any, approval_id: Any) -> ApprovalRequest | None:
            raise RuntimeError("database is down")

    world = World(approvals=BrokenStore())
    action = world.action()

    assert world.service.check(uuid4(), action).ok is False
    assert world.service.consume(uuid4(), action).ok is False


# --- Tenant scope ----------------------------------------------------------------------------


def test_cross_hotel_approval_is_not_found() -> None:
    world = World()
    _, request = _request(world)

    # An owner of hotel A2 (same org) neither sees nor approves a hotel A1 request.
    with pytest.raises(NotFoundError):
        _approve(world, request, world.hotel_a2_owner)
    with pytest.raises(NotFoundError):
        world.service.approve(
            world.hotel_a2_owner,
            world.tenant(world.org_a, world.hotel_a2, world.hotel_a2_owner),
            request.id,
        )
    with pytest.raises(NotFoundError):
        world.service.list_for(
            world.hotel_a2_owner, world.tenant(world.org_a, world.hotel_a1, world.hotel_a2_owner)
        )


def test_cross_org_approval_is_not_found() -> None:
    world = World()
    _, request = _request(world)

    with pytest.raises(NotFoundError):
        _approve(world, request, world.org_b_owner)
    with pytest.raises(NotFoundError):
        world.service.approve(
            world.org_b_owner,
            world.tenant(world.org_b, world.hotel_b1, world.org_b_owner),
            request.id,
        )
    with pytest.raises(NotFoundError):
        world.service.get_for(
            world.org_b_owner,
            world.tenant(world.org_a, world.hotel_a1, world.org_b_owner),
            request.id,
        )
    assert world.resumer.resolutions == []


def test_org_wide_admin_approves_in_every_hotel_of_the_org() -> None:
    world = World()
    _, request = _request(world, world.action(tenant=world.tenant(hotel_id=world.hotel_a2)))

    assert _approve(world, request, world.org_a_admin).status is ApprovalStatus.APPROVED


# --- Roles -------------------------------------------------------------------------------------


def test_viewer_can_see_but_cannot_decide() -> None:
    world = World()
    _, request = _request(world)

    assert [
        r.id
        for r in world.service.list_for(
            world.viewer, world.tenant(world.org_a, world.hotel_a1, world.viewer)
        )
    ] == [request.id]
    with pytest.raises(ForbiddenError):
        _approve(world, request, world.viewer)
    with pytest.raises(ForbiddenError):
        world.service.reject(
            world.viewer, world.tenant(world.org_a, world.hotel_a1, world.viewer), request.id
        )


def test_operator_cannot_approve_when_the_hotel_requires_admin() -> None:
    world = World()
    world.set_policy(approver_min_roles={ActionKind.PMS_CHANGE: HotelRole.ADMIN})
    _, request = _request(world)

    with pytest.raises(ForbiddenError):
        _approve(world, request, world.operator)
    assert _approve(world, request, world.admin).status is ApprovalStatus.APPROVED


def test_owner_can_approve_when_the_hotel_requires_admin() -> None:
    world = World()
    world.set_policy(approver_min_roles={ActionKind.PMS_CHANGE: HotelRole.ADMIN})
    _, request = _request(world)

    assert _approve(world, request, world.owner).status is ApprovalStatus.APPROVED


def test_pending_request_keeps_the_approver_roles_it_was_created_with() -> None:
    world = World()
    _, request = _request(world)
    world.service.set_approver_min_role(
        world.admin,
        world.tenant(world.org_a, world.hotel_a1, world.admin),
        ActionKind.PMS_CHANGE,
        HotelRole.OWNER,
    )

    assert _approve(world, request, world.operator).status is ApprovalStatus.APPROVED


@pytest.mark.parametrize("kind", [ActionKind.GIT_PUSH, ActionKind.CODE_CHANGE])
def test_hotel_roles_can_never_approve_code(kind: ActionKind) -> None:
    world = World()
    _, request = _request(world, world.action(kind, toolset="coding", tool="git"))

    for hotel_person in (world.owner, world.admin, world.operator, world.org_a_admin):
        with pytest.raises(ForbiddenError):
            _approve(world, request, hotel_person)
    with pytest.raises(NotFoundError):
        _approve(world, request, world.platform)  # break-glass is not an approver

    assert _approve(world, request, world.code_reviewer).status is ApprovalStatus.APPROVED


def test_code_reviewer_sees_only_code_requests_of_a_hotel() -> None:
    world = World()
    _, ops_request = _request(world)
    _, code_request = _request(
        world, world.action(ActionKind.CODE_CHANGE, toolset="coding", tool="git", run_id="run-2")
    )

    seen = world.service.list_for(
        world.code_reviewer, world.tenant(world.org_a, world.hotel_a1, world.code_reviewer)
    )

    assert [r.id for r in seen] == [code_request.id]
    with pytest.raises(NotFoundError):
        _approve(world, ops_request, world.code_reviewer)


def test_list_filters_by_status() -> None:
    world = World()
    _, first = _request(world)
    _, second = _request(world, world.action(run_id="run-2"))
    _approve(world, first, world.operator)

    pending = world.service.list_for(
        world.operator,
        world.tenant(world.org_a, world.hotel_a1, world.operator),
        ApprovalStatus.PENDING,
    )

    assert [r.id for r in pending] == [second.id]


# --- Self-approval ---------------------------------------------------------------------------


def test_requester_may_approve_their_own_action_by_default() -> None:
    world = World()
    _, request = _request(world, world.action(risk=RiskLevel.HIGH))

    assert _approve(world, request, world.requester).status is ApprovalStatus.APPROVED


def test_hotel_can_forbid_self_approval_of_high_risk_actions() -> None:
    world = World()
    world.set_policy(forbid_self_approval_of_high_risk=True)
    _, high = _request(world, world.action(risk=RiskLevel.HIGH))
    _, medium = _request(world, world.action(risk=RiskLevel.MEDIUM, run_id="run-2"))

    with pytest.raises(ForbiddenError, match="own high-risk"):
        _approve(world, high, world.requester)
    assert _approve(world, medium, world.requester).status is ApprovalStatus.APPROVED
    assert _approve(world, high, world.operator).status is ApprovalStatus.APPROVED


def test_requester_may_still_reject_their_own_high_risk_action() -> None:
    world = World()
    world.set_policy(forbid_self_approval_of_high_risk=True)
    _, request = _request(world, world.action(risk=RiskLevel.HIGH))

    rejected = world.service.reject(
        world.requester, world.tenant(world.org_a, world.hotel_a1, world.requester), request.id
    )

    assert rejected.status is ApprovalStatus.REJECTED


# --- Cancel ------------------------------------------------------------------------------------


def test_requester_run_can_cancel_a_pending_approval() -> None:
    world = World()
    action, request = _request(world)

    cancelled = world.service.cancel(world.tenant(), request.id, "run cancelled")

    assert cancelled.status is ApprovalStatus.CANCELLED
    assert [r.outcome for r in world.resumer.resolutions] == [ApprovalStatus.CANCELLED]
    assert world.audit_actions()[-1] == "approval.cancelled"
    assert world.service.consume(request.id, action).ok is False
    with pytest.raises(ConflictError):
        _approve(world, request, world.operator)


def test_cancel_by_someone_else_or_after_a_decision_is_refused() -> None:
    world = World()
    _, request = _request(world)

    with pytest.raises(NotFoundError):
        world.service.cancel(world.tenant(actor=world.operator), request.id)
    _approve(world, request, world.operator)
    with pytest.raises(ConflictError):
        world.service.cancel(world.tenant(), request.id)


# --- Hotel admins set approver roles -------------------------------------------------------


def test_hotel_admin_sets_approver_role_with_an_audit_event() -> None:
    world = World()

    policy = world.service.set_approver_min_role(
        world.admin,
        world.tenant(world.org_a, world.hotel_a1, world.admin),
        ActionKind.PMS_CHANGE,
        HotelRole.ADMIN,
    )
    again = world.service.set_approver_min_role(
        world.owner,
        world.tenant(world.org_a, world.hotel_a1, world.owner),
        ActionKind.MESSAGE_SEND,
        HotelRole.OWNER,
    )

    assert policy.min_role_for(ActionKind.PMS_CHANGE) is HotelRole.ADMIN
    assert again.min_role_for(ActionKind.PMS_CHANGE) is HotelRole.ADMIN
    assert again.min_role_for(ActionKind.MESSAGE_SEND) is HotelRole.OWNER
    assert world.audit_actions() == ["approval_policy.updated", "approval_policy.updated"]
    assert world.audit.events[0].actor_id == world.admin.user_id


def test_only_admins_of_the_hotel_set_approver_roles() -> None:
    world = World()

    def attempt(who: Principal) -> None:
        tenant = world.tenant(world.org_a, world.hotel_a1, who)
        world.service.set_approver_min_role(who, tenant, ActionKind.PMS_CHANGE, HotelRole.ADMIN)

    with pytest.raises(ForbiddenError):
        attempt(world.operator)
    for outsider in (world.hotel_a2_owner, world.org_b_owner, world.code_reviewer):
        with pytest.raises(NotFoundError):
            attempt(outsider)
    assert world.audit.events == ()


def test_tenant_context_must_belong_to_the_principal() -> None:
    world = World()
    _, request = _request(world)
    borrowed = world.tenant(world.org_a, world.hotel_a1, world.operator)

    with pytest.raises(ValueError, match="actor_id"):
        world.service.approve(world.viewer, borrowed, request.id)
    assert world.approvals.get(world.org_a, world.hotel_a1, request.id) == request


def test_first_policy_writes_from_two_admins_never_lose_a_setting() -> None:
    # Another admin inserts the first policy between this admin's read and write.
    class RacingStore(InMemoryPolicyStore):
        raced = False

        def get(self, org_id: UUID, hotel_id: UUID) -> HotelPolicy | None:
            seen = super().get(org_id, hotel_id)
            if not self.raced:
                self.raced = True
                other = world.tenant(org_id, hotel_id, world.owner)
                world.service.set_approver_min_role(
                    world.owner, other, ActionKind.PMS_CHANGE, HotelRole.OWNER
                )
            return seen

    audit = InMemoryAuditLog()
    world = World(policies=RacingStore(audit), audit=audit)
    tenant = world.tenant(world.org_a, world.hotel_a1, world.admin)

    with pytest.raises(ConflictError):
        world.service.set_approver_min_role(
            world.admin, tenant, ActionKind.MESSAGE_SEND, HotelRole.ADMIN
        )
    stored = world.policies.get(world.org_a, world.hotel_a1)
    assert stored is not None
    assert stored.min_role_for(ActionKind.PMS_CHANGE) is HotelRole.OWNER
    assert stored.min_role_for(ActionKind.MESSAGE_SEND) is HotelRole.OPERATOR
    assert world.audit_actions() == ["approval_policy.updated"]


@pytest.mark.parametrize(
    ("kind", "role"),
    [
        (ActionKind.CODE_CHANGE, HotelRole.OWNER),
        (ActionKind.GIT_PUSH, HotelRole.ADMIN),
        (ActionKind.PMS_CHANGE, HotelRole.VIEWER),
        (ActionKind.READ, HotelRole.ADMIN),
    ],
)
def test_admin_cannot_set_a_forbidden_approver_role(kind: ActionKind, role: HotelRole) -> None:
    world = World()

    with pytest.raises(InvalidPolicyError):
        world.service.set_approver_min_role(
            world.admin, world.tenant(world.org_a, world.hotel_a1, world.admin), kind, role
        )
    assert world.audit.events == ()


# --- Audit and state change are atomic -----------------------------------------------------


class FailingAudit:
    def __init__(self) -> None:
        self.fail = True

    def record(self, event: Any) -> None:
        if self.fail:
            raise RuntimeError("audit store is down")


def test_failed_audit_write_leaves_no_usable_approval() -> None:
    world = World()
    action, request = _request(world)
    audit = FailingAudit()
    world.approvals.audit = audit

    with pytest.raises(RuntimeError, match="audit"):
        _approve(world, request, world.operator)

    assert world.approvals.get(world.org_a, world.hotel_a1, request.id) == request
    assert world.service.check(request.id, action).ok is False
    assert world.resumer.resolutions == []
    audit.fail = False
    assert _approve(world, request, world.operator).status is ApprovalStatus.APPROVED
    assert world.service.consume(request.id, action).ok is True


def test_failed_audit_write_blocks_consume() -> None:
    world = World()
    action, request = _request(world)
    _approve(world, request, world.operator)
    world.approvals.audit = FailingAudit()

    result = world.service.consume(request.id, action)

    assert result.ok is False
    world.approvals.audit = world.audit
    assert world.service.consume(request.id, action).ok is True
    assert world.audit_actions()[-1] == "approval.consumed"


def test_orchestrator_recovers_a_decision_when_resume_failed() -> None:
    class DownResumer:
        def resume(self, resolution: ApprovalResolution) -> None:
            raise RuntimeError("orchestrator is down")

    world = World()
    action, request = _request(world)
    world.service._resumer = DownResumer()
    run = world.tenant(world.org_a, world.hotel_a1, world.requester)

    assert world.service.resolution(run, request.id) is None  # still pending
    _approve(world, request, world.operator)

    resolution = world.service.resolution(run, request.id)
    assert resolution is not None
    assert (resolution.outcome, resolution.run_id) == (ApprovalStatus.APPROVED, action.run_id)
    other = world.tenant(world.org_a, world.hotel_a1, world.operator)
    assert world.service.resolution(other, request.id) is None  # not their run
