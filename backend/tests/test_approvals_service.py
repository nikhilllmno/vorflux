"""Approval lifecycle contract: request, decide, resume, expire, bind to the exact proposal, use
once at the time of the write, and audit every outcome."""

import asyncio
import inspect
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from approval_world import DEFAULT_ARGS, World

from harness.approvals import (
    ActionDeniedError,
    ActionKind,
    ActorType,
    ApprovalNotRequiredError,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalStatus,
    ConflictError,
    Effect,
    InMemoryApprovalStore,
    InMemoryPolicyStore,
    ProposedCall,
    RiskLevel,
    Rule,
    Verdict,
)

pytestmark = pytest.mark.anyio

PMS_ARGS = DEFAULT_ARGS[ActionKind.PMS_CHANGE]


def test_service_satisfies_the_async_adapter_interface() -> None:
    assert isinstance(World().service, ApprovalPolicy)
    for name in ("evaluate", "request", "check", "consume"):
        assert inspect.iscoroutinefunction(getattr(ApprovalPolicy, name)), name


async def test_request_creates_a_pending_approval_with_a_harness_preview() -> None:
    world = World()

    call, request = await world.request(note="Guests asked for a higher rate on Christmas")

    assert request.status == ApprovalStatus.PENDING
    assert request.expires_at == world.clock.now + timedelta(hours=24)
    assert request.requester_id == world.requester.user_id
    assert (request.org_id, request.hotel_id) == (world.org_a, world.hotel_a1)
    assert (request.run_id, request.tool_call_id) == ("run-1", "call-1")
    assert request.preview.text() == (
        "Change rate of Deluxe King, 24 Dec 2026 in Beds24\n"
        "From: 9000\nTo: 9900\nGuests affected: 0"
    )
    assert request.note == "Guests asked for a higher rate on Christmas"
    assert world.audit_actions() == ["approval.requested"]
    assert world.audit.events[0].actor_type == ActorType.RUN
    assert (await world.service.check(request.id, call)).ok is False  # not approved yet


async def test_the_preview_shows_the_bound_value_not_the_agent_text() -> None:
    # Review probe: the agent says 9,900 but the arguments say 99. The approver sees 99.
    world = World()
    call = world.call(args={**PMS_ARGS, "new": 99})

    _, request = await world.request(call, note="Change DK rate to 9,900")

    assert "To: 99\n" in request.preview.text()
    assert "9,900" not in request.preview.text()


async def test_request_uses_the_hotel_ttl() -> None:
    world = World()
    await world.set_policy(approval_ttl=timedelta(minutes=30))

    _, request = await world.request()

    assert request.expires_at - request.created_at == timedelta(minutes=30)


async def test_request_refuses_denied_and_allowed_actions() -> None:
    world = World(platform_rules=[Rule(ActionKind.DELETE, Effect.DENY)])

    with pytest.raises(ActionDeniedError):
        await world.service.request(world.call(ActionKind.DELETE))
    with pytest.raises(ApprovalNotRequiredError):
        await world.service.request(world.call(ActionKind.READ, RiskLevel.LOW))
    with pytest.raises(ActionDeniedError):
        await world.service.request(world.call(toolset="nope", tool="nope"))
    assert world.audit_actions() == ["policy.denied", "policy.denied"]


# --- One approval per tool call ----------------------------------------------------------------


async def test_a_second_request_for_the_same_tool_call_returns_the_same_approval() -> None:
    world = World()
    call, first = await world.request()

    _, second = await world.request(call)

    assert second.id == first.id
    assert world.audit_actions() == ["approval.requested"]


async def test_a_tool_call_cannot_get_a_second_approval_for_other_arguments() -> None:
    world = World()
    await world.request()

    with pytest.raises(ConflictError, match="different proposal"):
        await world.request(world.call(args={**PMS_ARGS, "new": 1}))


async def test_a_used_or_rejected_tool_call_cannot_be_requested_again() -> None:
    world = World()
    call, request = await world.request()
    await world.approve(request, world.operator)
    assert (await world.service.consume(request.id, call)).ok is True

    with pytest.raises(ConflictError, match="used"):
        await world.request(call)

    other = world.call(tool_call_id="call-2")
    _, rejected = await world.request(other)
    await world.reject(rejected, world.operator)
    with pytest.raises(ConflictError, match="rejected"):
        await world.request(other)


async def test_two_racing_requests_for_one_tool_call_give_one_approval() -> None:
    world = World()
    call = world.call()

    first, second = await asyncio.gather(world.service.request(call), world.service.request(call))

    assert first.id == second.id
    assert len(await world.approvals.list(world.org_a, world.hotel_a1)) == 1


# --- Decide and use ----------------------------------------------------------------------------


async def test_approve_resumes_the_run_and_consume_returns_the_approved_arguments() -> None:
    world = World()
    call, request = await world.request()

    approved = await world.approve(request, world.operator)
    result = await world.service.consume(request.id, call)

    assert approved.status == ApprovalStatus.APPROVED
    assert approved.decided_by == world.operator.user_id
    assert approved.use_by == world.clock.now + timedelta(minutes=15)
    assert [r.outcome for r in world.resumer.resolutions] == [ApprovalStatus.APPROVED]
    assert result.ok is True
    assert dict(result.args or {}) == PMS_ARGS
    assert result.idempotency_key == str(request.id)
    assert world.audit_actions() == ["approval.requested", "approval.approved", "approval.consumed"]
    consumed = world.audit.events[-1]
    assert (consumed.actor_type, consumed.actor_id) == (ActorType.RUN, "run-1")
    assert consumed.details["requester_id"] == str(world.requester.user_id)


async def test_replay_of_a_used_approval_is_refused_and_audited() -> None:
    world = World()
    call, request = await world.request()
    await world.approve(request, world.operator)
    await world.service.consume(request.id, call)

    replay = await world.service.consume(request.id, call)

    assert (replay.ok, replay.reason) == (False, "approval was already used")
    assert (await world.service.check(request.id, call)).ok is False
    assert world.audit_actions()[-1] == "approval.use_refused"
    assert world.audit.events[-1].details["reason"] == "approval was already used"


async def test_concurrent_use_wins_only_once() -> None:
    world = World()
    call, request = await world.request()
    await world.approve(request, world.operator)

    results = await asyncio.gather(*(world.service.consume(request.id, call) for _ in range(5)))

    assert sorted(r.ok for r in results) == [False, False, False, False, True]
    assert world.audit_actions().count("approval.consumed") == 1


async def test_reject_resumes_the_run_and_the_action_cannot_run() -> None:
    world = World()
    call, request = await world.request()

    rejected = await world.service.reject(
        world.operator,
        world.tenant(world.org_a, world.hotel_a1, world.operator),
        request.id,
        reason="Too high for this week",
    )

    assert rejected.status == ApprovalStatus.REJECTED
    assert rejected.reason == "Too high for this week"
    assert [r.outcome for r in world.resumer.resolutions] == [ApprovalStatus.REJECTED]
    assert (await world.service.consume(request.id, call)).ok is False
    assert world.audit_actions() == [
        "approval.requested",
        "approval.rejected",
        "approval.use_refused",
    ]


async def test_a_decided_approval_cannot_be_decided_again() -> None:
    world = World()
    _, request = await world.request()
    await world.approve(request, world.operator)

    with pytest.raises(ConflictError):
        await world.reject(request, world.admin)
    with pytest.raises(ConflictError):
        await world.approve(request, world.admin)


class PausedRun:
    """A stand-in for the orchestrator and the control-plane executor."""

    def __init__(self, world: World, call: ProposedCall) -> None:
        self.world = world
        self.call = call
        self.approval_id: UUID | None = None
        self.executed: list[dict[str, Any]] = []
        self.tool_errors: list[str] = []

    async def gate(self) -> None:
        decision = await self.world.service.evaluate(self.call)
        assert decision.verdict == Verdict.ASK
        self.approval_id = (await self.world.service.request(self.call)).id

    async def resume(self, resolution: ApprovalResolution) -> None:
        assert (resolution.approval_id, resolution.tool_call_id) == (self.approval_id, "call-1")
        if resolution.outcome != ApprovalStatus.APPROVED:
            self.tool_errors.append(f"not run: {resolution.outcome}")
            return
        result = await self.world.service.consume(resolution.approval_id, self.call)
        if result.ok and result.args is not None:
            self.executed.append(dict(result.args))
        else:
            self.tool_errors.append(result.reason)


@pytest.mark.parametrize("approve", [True, False])
async def test_decision_resumes_the_paused_run(approve: bool) -> None:
    world = World()
    run = PausedRun(world, world.call())
    world.service._resumer = run
    await run.gate()
    assert run.approval_id is not None
    tenant = world.tenant(world.org_a, world.hotel_a1, world.operator)

    if approve:
        await world.service.approve(world.operator, tenant, run.approval_id)
        assert run.executed == [PMS_ARGS]
        assert run.tool_errors == []
    else:
        await world.service.reject(world.operator, tenant, run.approval_id)
        assert run.executed == []
        assert run.tool_errors == ["not run: rejected"]


# --- Re-check at the time of use ---------------------------------------------------------------


async def test_a_deny_rule_added_after_approval_wins_at_the_time_of_use() -> None:
    world = World()
    call, request = await world.request()
    await world.approve(request, world.operator)
    await world.set_policy(rules=(Rule(ActionKind.PMS_CHANGE, Effect.DENY),))

    result = await world.service.consume(request.id, call)

    assert result.ok is False
    assert result.reason.startswith("the policy now denies this action")
    assert world.audit_actions()[-1] == "approval.use_refused"


async def test_a_tool_removed_from_the_registry_cannot_use_its_approval() -> None:
    world = World()
    call, request = await world.request()
    await world.approve(request, world.operator)
    world.engine._catalog = type(world.catalog)()  # empty registry

    result = await world.service.consume(request.id, call)

    assert result.ok is False
    assert "not registered" in result.reason


async def test_approval_is_bound_to_its_tool_call_id() -> None:
    world = World()
    call, request = await world.request()
    await world.approve(request, world.operator)

    result = await world.service.consume(request.id, replace(call, tool_call_id="call-2"))

    assert (result.ok, result.reason) == (False, "approval belongs to another tool call")
    assert (await world.service.consume(request.id, call)).ok is True


@pytest.mark.parametrize(
    "change",
    [
        {"args": {**PMS_ARGS, "new": 99000}},
        {"args": {**PMS_ARGS, "target": "Suite, 24 Dec 2026"}},
        {"args": {**PMS_ARGS, "guests_affected": 3}},
        {"tool": "pms_change_low"},
        {"toolset": "reports"},
    ],
)
async def test_changed_proposal_needs_a_new_approval(change: dict[str, Any]) -> None:
    world = World()
    call, request = await world.request()
    await world.approve(request, world.operator)

    result = await world.service.consume(request.id, replace(call, **change))

    assert result.ok is False
    assert result.reason == "action changed after approval; a new approval is needed"
    assert (await world.service.consume(request.id, call)).ok is True  # the original is untouched


async def test_arguments_mutated_by_the_caller_after_the_request_do_not_change_the_call() -> None:
    world = World()
    args = dict(PMS_ARGS)
    call, request = await world.request(world.call(args=args))
    await world.approve(request, world.operator)

    args["new"] = 19900  # the call holds its own frozen copy

    result = await world.service.consume(request.id, call)
    assert result.ok is True
    assert dict(result.args or {})["new"] == 9900


async def test_approval_cannot_be_used_by_another_run_or_requester() -> None:
    world = World()
    call, request = await world.request()
    await world.approve(request, world.operator)
    someone_else = world.tenant(actor=world.operator)

    other_run = await world.service.consume(request.id, replace(call, run_id="run-2"))
    other_requester = await world.service.consume(request.id, replace(call, tenant=someone_else))

    assert other_run.reason == "approval belongs to another run"
    assert other_requester.reason == "approval belongs to another run"


async def test_approval_cannot_be_used_in_another_hotel_or_org() -> None:
    world = World()
    call, request = await world.request()
    await world.approve(request, world.operator)

    for org, hotel in [(world.org_a, world.hotel_a2), (world.org_b, world.hotel_b1)]:
        moved = replace(call, tenant=world.tenant(org, hotel))
        result = await world.service.consume(request.id, moved)
        assert (result.ok, result.reason) == (False, "approval not found")


async def test_check_and_consume_fail_closed_on_store_errors() -> None:
    class BrokenStore(InMemoryApprovalStore):
        async def get(self, org_id: Any, hotel_id: Any, approval_id: Any) -> ApprovalRequest | None:
            raise RuntimeError("database is down")

    policies = InMemoryPolicyStore()
    world = World(policies=policies, approvals=BrokenStore(policies))
    call = world.call()

    assert (await world.service.check(uuid4(), call)).ok is False
    assert (await world.service.consume(uuid4(), call)).ok is False
    assert world.audit_actions() == ["approval.use_refused"]


# --- Expiry ------------------------------------------------------------------------------------


async def test_pending_approval_expires_and_cannot_be_approved() -> None:
    world = World()
    call, request = await world.request()
    world.clock.advance(timedelta(hours=24))

    with pytest.raises(ConflictError):
        await world.approve(request, world.operator)

    stored = await world.approvals.get(world.org_a, world.hotel_a1, request.id)
    assert stored.status == ApprovalStatus.EXPIRED
    assert [r.outcome for r in world.resumer.resolutions] == [ApprovalStatus.EXPIRED]
    assert (await world.service.consume(request.id, call)).reason == "approval is expired"


async def test_pending_approval_is_still_valid_one_second_before_expiry() -> None:
    world = World()
    _, request = await world.request()
    world.clock.advance(timedelta(hours=24) - timedelta(seconds=1))

    assert (await world.approve(request, world.operator)).status == ApprovalStatus.APPROVED


async def test_expiry_sweep_expires_only_due_requests_of_one_hotel() -> None:
    world = World()
    _, old = await world.request()
    world.clock.advance(timedelta(hours=12))
    _, new = await world.request(world.call(tool_call_id="call-2"))
    other_hotel = world.call(tenant=world.tenant(world.org_a, world.hotel_a2))
    _, elsewhere = await world.request(other_hotel)
    world.clock.advance(timedelta(hours=12))

    expired = await world.service.expire_due(world.org_a, world.hotel_a1)

    assert [r.id for r in expired] == [old.id]
    assert (await world.approvals.get(world.org_a, world.hotel_a1, new.id)).status == "pending"
    assert (await world.approvals.get(world.org_a, world.hotel_a2, elsewhere.id)).status == (
        "pending"
    )


async def test_an_approval_given_just_before_the_deadline_is_usable_for_the_use_window() -> None:
    # Review probe: approved 5 s before the decide deadline, used 6 s later.
    world = World()
    call, request = await world.request()
    world.clock.advance(timedelta(hours=24) - timedelta(seconds=5))
    await world.approve(request, world.operator)
    world.clock.advance(timedelta(seconds=6))

    assert (await world.service.consume(request.id, call)).ok is True


async def test_an_approved_but_unused_approval_expires_after_the_use_window() -> None:
    world = World()
    await world.set_policy(use_window=timedelta(minutes=5))
    call, request = await world.request()
    await world.approve(request, world.operator)
    world.clock.advance(timedelta(minutes=5))

    result = await world.service.consume(request.id, call)

    assert (result.ok, result.reason) == (False, "approval expired")


# --- Cancel, resume recovery, and audit atomicity ----------------------------------------------


async def test_requester_run_can_cancel_a_pending_approval() -> None:
    world = World()
    _, request = await world.request()

    cancelled = await world.service.cancel(world.tenant(), request.id, reason="run cancelled")

    assert cancelled.status == ApprovalStatus.CANCELLED
    assert [r.outcome for r in world.resumer.resolutions] == [ApprovalStatus.CANCELLED]
    assert world.audit_actions() == ["approval.requested", "approval.cancelled"]


async def test_cancel_by_someone_else_or_after_a_decision_is_refused() -> None:
    from harness.approvals import NotFoundError

    world = World()
    _, request = await world.request()

    with pytest.raises(NotFoundError):
        await world.service.cancel(world.tenant(actor=world.operator), request.id)
    await world.approve(request, world.operator)
    with pytest.raises(ConflictError):
        await world.service.cancel(world.tenant(), request.id)


async def test_resumer_failure_does_not_undo_the_decision_and_can_be_recovered() -> None:
    class DownResumer:
        async def resume(self, resolution: ApprovalResolution) -> None:
            raise RuntimeError("orchestrator is down")

    world = World()
    world.service._resumer = DownResumer()
    call, request = await world.request()
    run = world.tenant()

    assert await world.service.resolution(run, request.id) is None  # still pending
    await world.approve(request, world.operator)

    resolution = await world.service.resolution(run, request.id)
    assert resolution is not None
    assert (resolution.outcome, resolution.run_id) == (ApprovalStatus.APPROVED, "run-1")
    other = world.tenant(actor=world.operator)
    assert await world.service.resolution(other, request.id) is None  # not their run
    assert (await world.service.consume(request.id, call)).ok is True


class FailingAudit:
    def __init__(self) -> None:
        self.fail = True

    def record_now(self, event: Any) -> None:
        if self.fail:
            raise RuntimeError("audit store is down")


async def test_failed_audit_write_leaves_no_usable_approval() -> None:
    world = World()
    call, request = await world.request()
    audit = FailingAudit()
    world.approvals.audit = audit

    with pytest.raises(RuntimeError, match="audit"):
        await world.approve(request, world.operator)

    assert await world.approvals.get(world.org_a, world.hotel_a1, request.id) == request
    assert (await world.service.check(request.id, call)).ok is False
    assert world.resumer.resolutions == []
    audit.fail = False
    assert (await world.approve(request, world.operator)).status == ApprovalStatus.APPROVED
    assert (await world.service.consume(request.id, call)).ok is True


async def test_failed_audit_write_blocks_consume() -> None:
    world = World()
    call, request = await world.request()
    await world.approve(request, world.operator)
    world.approvals.audit = FailingAudit()

    result = await world.service.consume(request.id, call)

    assert result.ok is False
    world.approvals.audit = world.audit
    assert (await world.service.consume(request.id, call)).ok is True
    assert world.audit_actions()[-1] == "approval.consumed"


async def test_failed_audit_of_a_denial_still_denies() -> None:
    class BrokenSink:
        async def record(self, event: Any) -> None:
            raise RuntimeError("audit store is down")

    world = World()
    world.service._audit = BrokenSink()

    decision = await world.service.evaluate(world.call(toolset="nope", tool="nope"))

    assert decision.verdict == Verdict.DENY
