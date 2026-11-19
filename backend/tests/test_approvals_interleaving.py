"""Interleavings between a policy change or the clock and an approval write (review round 2).

A policy change or a deadline that passes while `consume` or a decision awaits I/O must not let
the write through. Each test runs the change inside the policy read, so it commits after the
service has read the policy and before the service writes the approval.
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

import pytest
from approval_world import FakeClock, World

from harness.approvals import (
    ActionKind,
    ApprovalStatus,
    ConflictError,
    Effect,
    ForbiddenError,
    HotelPolicy,
    HotelRole,
    InMemoryApprovalStore,
    InMemoryPolicyStore,
    Rule,
)

pytestmark = pytest.mark.anyio

Hook = Callable[[], Awaitable[None]]


class HookedPolicyStore(InMemoryPolicyStore):
    """Runs `hook` once, after the next read and before that read returns."""

    def __init__(self) -> None:
        super().__init__()
        self.hook: Hook | None = None

    async def get(self, org_id: UUID, hotel_id: UUID) -> HotelPolicy | None:
        policy = await super().get(org_id, hotel_id)
        hook, self.hook = self.hook, None
        if hook is not None:
            await hook()
        return policy


def _world() -> tuple[World, HookedPolicyStore]:
    policies = HookedPolicyStore()
    world = World(policies=policies)
    policies.audit = world.audit
    return world, policies


@pytest.mark.parametrize("seeded", [False, True], ids=["no_stored_policy", "stored_policy"])
async def test_a_deny_rule_committed_during_consume_refuses_the_use(seeded: bool) -> None:
    world, policies = _world()
    if seeded:
        await world.set_policy()
    call, request = await world.request()
    await world.approve(request, world.operator)

    async def add_deny_rule() -> None:
        await world.set_policy(rules=(Rule(ActionKind.PMS_CHANGE, Effect.DENY),))

    policies.hook = add_deny_rule
    result = await world.service.consume(request.id, call)

    assert result.ok is False
    assert result.reason.startswith("the policy now denies this action")
    assert "approval.consumed" not in world.audit_actions()


async def test_a_raised_min_role_committed_during_a_decision_blocks_it() -> None:
    world, policies = _world()
    call, request = await world.request()

    async def raise_min_role() -> None:
        await world.set_policy(approver_min_roles={ActionKind.PMS_CHANGE: HotelRole.ADMIN})

    policies.hook = raise_min_role
    with pytest.raises(ForbiddenError):
        await world.approve(request, world.operator)

    stored = await world.approvals.get(request.org_id, request.hotel_id, request.id)
    assert stored is not None and stored.status == ApprovalStatus.PENDING
    assert (await world.service.consume(request.id, call)).ok is False


async def test_consume_that_reaches_the_use_deadline_while_waiting_is_refused() -> None:
    world, policies = _world()
    call, request = await world.request()
    approved = await world.approve(request, world.operator)
    assert approved.use_by is not None
    world.clock.now = approved.use_by - timedelta(seconds=1)

    async def slow_read() -> None:
        world.clock.advance(timedelta(seconds=2))

    policies.hook = slow_read
    result = await world.service.consume(request.id, call)

    assert (result.ok, result.reason) == (False, "approval expired")
    assert "approval.consumed" not in world.audit_actions()


async def test_a_decision_that_reaches_the_decide_deadline_while_waiting_is_refused() -> None:
    world, policies = _world()
    call, request = await world.request()
    world.clock.now = request.expires_at - timedelta(seconds=1)

    async def slow_read() -> None:
        world.clock.advance(timedelta(seconds=2))

    policies.hook = slow_read
    with pytest.raises(ConflictError, match="expired"):
        await world.approve(request, world.operator)

    stored = await world.approvals.get(request.org_id, request.hotel_id, request.id)
    assert stored is not None and stored.status == ApprovalStatus.EXPIRED
    assert "approval.approved" not in world.audit_actions()
    assert (await world.service.consume(request.id, call)).ok is False


async def test_a_deadline_seen_only_by_the_store_still_expires_the_request() -> None:
    # The store's clock is authoritative. Here it runs 2 s ahead of the service clock.
    store_clock = FakeClock()
    policies = InMemoryPolicyStore()
    world = World(policies=policies)
    policies.audit = world.audit
    world.approvals = InMemoryApprovalStore(policies, world.audit, store_clock)
    world.service._approvals = world.approvals
    _, request = await world.request()
    world.clock.now = request.expires_at - timedelta(seconds=1)
    store_clock.now = request.expires_at + timedelta(seconds=1)

    with pytest.raises(ConflictError, match="expired"):
        await world.approve(request, world.operator)

    stored = await world.approvals.get(request.org_id, request.hotel_id, request.id)
    assert stored is not None and stored.status == ApprovalStatus.EXPIRED
    assert world.audit_actions()[-1] == "approval.expired"
    assert [r.outcome for r in world.resumer.resolutions] == [ApprovalStatus.EXPIRED]
