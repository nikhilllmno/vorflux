"""Store contract that every ApprovalStore and PolicyStore must pass (a Postgres store later too).

`JsonRowApprovalStore` and `JsonRowPolicyStore` keep each row as a JSON string and rebuild it with
plain strings for every enum, the way a database driver returns text columns. The service must
behave the same on them as on the typed in-memory stores.
"""

import json
import threading
from collections.abc import Callable
from dataclasses import fields, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from approval_world import FakeClock, World

from harness.approvals import (
    NO_STORED_POLICY,
    ActionKind,
    ActorType,
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    DuplicateToolCallError,
    ForbiddenError,
    HotelPolicy,
    InMemoryApprovalStore,
    InMemoryAuditLog,
    InMemoryPolicyStore,
    RiskLevel,
    TransitionGuard,
    Verdict,
    WriteResult,
)
from harness.approvals.model import thaw_json
from harness.approvals.store import Clock, SyncAuditLog, guard_failure

pytestmark = pytest.mark.anyio


def _to_row(request: ApprovalRequest) -> str:
    row: dict[str, Any] = {}
    for f in fields(request):
        value = getattr(request, f.name)
        if isinstance(value, UUID):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        elif f.name == "approver_roles":
            value = sorted(str(role) for role in value)
        elif f.name == "args":
            value = thaw_json(value)
        elif f.name == "preview":
            value = value.as_json()
        row[f.name] = value
    return json.dumps(row)


def _from_row(text: str) -> ApprovalRequest:
    row = json.loads(text)
    for name in ("id", "org_id", "hotel_id", "requester_id", "decided_by"):
        row[name] = UUID(row[name]) if row[name] else None
    for name in ("created_at", "expires_at", "decided_at", "use_by", "consumed_at"):
        row[name] = datetime.fromisoformat(row[name]) if row[name] else None
    return ApprovalRequest(**row)  # kind, risk, status, roles, preview stay plain JSON


class JsonRowApprovalStore:
    def __init__(self, policies: "JsonRowPolicyStore", audit: SyncAuditLog, clock: Clock) -> None:
        self._policies = policies
        self._lock = policies.lock
        self._clock = clock
        self._rows: dict[UUID, str] = {}
        self.audit: SyncAuditLog = audit

    def _all(self) -> list[ApprovalRequest]:
        return [_from_row(row) for row in self._rows.values()]

    async def add(self, request: ApprovalRequest, event: AuditEvent) -> None:
        with self._lock:
            if request.id in self._rows:
                raise ValueError("approval already exists")
            key = (request.org_id, request.hotel_id, request.run_id, request.tool_call_id)
            if any((r.org_id, r.hotel_id, r.run_id, r.tool_call_id) == key for r in self._all()):
                raise DuplicateToolCallError("tool call already has an approval")
            self.audit.record_now(event)
            self._rows[request.id] = _to_row(request)

    async def get(self, org_id: UUID, hotel_id: UUID, approval_id: UUID) -> ApprovalRequest | None:
        with self._lock:
            row = self._rows.get(approval_id)
        request = _from_row(row) if row else None
        if request is None or (request.org_id, request.hotel_id) != (org_id, hotel_id):
            return None
        return request

    async def get_by_tool_call(
        self, org_id: UUID, hotel_id: UUID, run_id: str, tool_call_id: str
    ) -> ApprovalRequest | None:
        with self._lock:
            rows = self._all()
        key = (org_id, hotel_id, run_id, tool_call_id)
        return next(
            (r for r in rows if (r.org_id, r.hotel_id, r.run_id, r.tool_call_id) == key), None
        )

    async def list(
        self, org_id: UUID, hotel_id: UUID, *, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]:
        with self._lock:
            rows = self._all()
        rows = [
            r
            for r in rows
            if (r.org_id, r.hotel_id) == (org_id, hotel_id) and status in (None, r.status)
        ]
        return sorted(rows, key=lambda r: r.created_at)

    async def replace(
        self,
        current: ApprovalRequest,
        new: ApprovalRequest,
        event: AuditEvent,
        guard: TransitionGuard | None = None,
    ) -> WriteResult:
        if new.version != current.version + 1 or new.id != current.id:
            raise ValueError("replace must keep the id and bump the version by one")
        with self._lock:
            row = self._rows.get(current.id)
            if row is None or _from_row(row).version != current.version:
                return WriteResult.STALE
            policy_version = self._policies.stored_version_locked(current.org_id, current.hotel_id)
            failure = guard_failure(guard, policy_version, self._clock())
            if failure is not None:
                return failure
            self.audit.record_now(event)
            self._rows[current.id] = _to_row(new)
            return WriteResult.APPLIED


class JsonRowPolicyStore:
    def __init__(self, audit: SyncAuditLog | None = None) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[UUID, UUID], str] = {}
        self.audit: SyncAuditLog = audit or InMemoryAuditLog()

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def stored_version_locked(self, org_id: UUID, hotel_id: UUID) -> int:
        text = self._rows.get((org_id, hotel_id))
        return NO_STORED_POLICY if text is None else int(json.loads(text)["version"])

    async def get(self, org_id: UUID, hotel_id: UUID) -> HotelPolicy | None:
        with self._lock:
            text = self._rows.get((org_id, hotel_id))
        if text is None:
            return None
        row = json.loads(text)
        return HotelPolicy(
            org_id=org_id,
            hotel_id=hotel_id,
            rules=tuple(row["rules"]),  # plain dicts of strings
            approver_min_roles=row["approver_min_roles"],  # plain strings
            approval_ttl=timedelta(seconds=row["approval_ttl"]),
            use_window=timedelta(seconds=row["use_window"]),
            forbid_self_approval_of_high_risk=row["forbid_self_approval_of_high_risk"],
            version=row["version"],
        )

    async def put(self, policy: HotelPolicy, event: AuditEvent) -> bool:
        key = (policy.org_id, policy.hotel_id)
        with self._lock:
            stored_version = self.stored_version_locked(*key)
            if policy.version != stored_version + 1:
                return False
            self.audit.record_now(event)
            self._rows[key] = json.dumps(
                {
                    "rules": [
                        {"kind": str(r.kind), "effect": str(r.effect), "toolset": r.toolset}
                        for r in policy.rules
                    ],
                    "approver_min_roles": {
                        str(k): str(v) for k, v in policy.approver_min_roles.items()
                    },
                    "approval_ttl": policy.approval_ttl.total_seconds(),
                    "use_window": policy.use_window.total_seconds(),
                    "forbid_self_approval_of_high_risk": policy.forbid_self_approval_of_high_risk,
                    "version": policy.version,
                }
            )
            return True


def _typed_world() -> World:
    return World()


def _json_world() -> World:
    audit = InMemoryAuditLog()
    clock = FakeClock()
    policies = JsonRowPolicyStore(audit)
    approvals = JsonRowApprovalStore(policies, audit, clock)
    return World(clock=clock, audit=audit, approvals=approvals, policies=policies)


@pytest.fixture(params=[_typed_world, _json_world], ids=["typed", "json_rows"])
def world(request: pytest.FixtureRequest) -> World:
    factory: Callable[[], World] = request.param
    return factory()


def _event(request: ApprovalRequest, action: str) -> AuditEvent:
    return AuditEvent(
        org_id=request.org_id,
        hotel_id=request.hotel_id,
        actor_type=ActorType.SYSTEM,
        actor_id=None,
        action=action,
        subject_type="approval_request",
        subject_id=str(request.id),
        at=request.created_at,
    )


async def test_rows_round_trip_with_typed_values(world: World) -> None:
    _, request = await world.request()

    stored = await world.approvals.get(world.org_a, world.hotel_a1, request.id)

    assert stored == request
    assert type(stored.kind) is ActionKind
    assert type(stored.risk) is RiskLevel
    assert type(stored.status) is ApprovalStatus


async def test_reads_are_scoped_by_tenant(world: World) -> None:
    _, request = await world.request()

    assert await world.approvals.get(world.org_a, world.hotel_a2, request.id) is None
    assert await world.approvals.get(world.org_b, world.hotel_a1, request.id) is None
    assert await world.approvals.list(world.org_a, world.hotel_a2) == []


async def test_replace_is_compare_and_set(world: World) -> None:
    _, request = await world.request()
    newer = replace(request, version=request.version + 1)

    assert await world.approvals.replace(request, newer, _event(newer, "a")) == WriteResult.APPLIED
    assert await world.approvals.replace(request, newer, _event(newer, "b")) == WriteResult.STALE
    assert world.audit_actions() == ["approval.requested", "a"]


def test_a_write_result_cannot_be_used_as_a_bool() -> None:
    with pytest.raises(TypeError):
        bool(WriteResult.STALE)


@pytest.mark.parametrize("seeded", [False, True], ids=["no_stored_policy", "stored_policy"])
async def test_a_guarded_write_fails_if_the_policy_changed(world: World, seeded: bool) -> None:
    if seeded:
        await world.set_policy()
    _, request = await world.request()
    assessed = 0 if seeded else NO_STORED_POLICY
    await world.set_policy(use_window=timedelta(minutes=5))  # a policy write after the read
    newer = replace(request, version=request.version + 1)

    result = await world.approvals.replace(
        request, newer, _event(newer, "a"), TransitionGuard(assessed)
    )

    assert result == WriteResult.POLICY_CHANGED
    assert world.audit_actions() == ["approval.requested"]
    current = TransitionGuard(assessed + 1)
    assert await world.approvals.replace(request, newer, _event(newer, "b"), current) == (
        WriteResult.APPLIED
    )


async def test_a_guarded_write_fails_at_or_after_its_deadline(world: World) -> None:
    _, request = await world.request()
    newer = replace(request, version=request.version + 1)
    at_deadline = TransitionGuard(NO_STORED_POLICY, deadline=world.clock.now)
    before_deadline = TransitionGuard(NO_STORED_POLICY, deadline=world.clock.now + timedelta(0, 1))

    late = await world.approvals.replace(request, newer, _event(newer, "a"), at_deadline)
    in_time = await world.approvals.replace(request, newer, _event(newer, "b"), before_deadline)

    assert (late, in_time) == (WriteResult.DEADLINE_PASSED, WriteResult.APPLIED)
    assert world.audit_actions() == ["approval.requested", "b"]


async def test_one_request_per_tool_call(world: World) -> None:
    _, request = await world.request()
    twin = replace(request, id=UUID(int=7))

    with pytest.raises(DuplicateToolCallError):
        await world.approvals.add(twin, _event(twin, "approval.requested"))


async def test_a_lost_request_race_returns_the_winning_approval(world: World) -> None:
    # The first lookup misses; the insert then hits the unique key and the service reuses
    # the approval that won.
    call, winner = await world.request()
    real = world.approvals.get_by_tool_call
    misses = iter([True])

    async def stale_then_real(*args: Any) -> ApprovalRequest | None:
        return None if next(misses, False) else await real(*args)

    world.approvals.get_by_tool_call = stale_then_real

    assert (await world.service.request(call)).id == winner.id


async def test_service_flow_works_on_every_store(world: World) -> None:
    call, request = await world.request()
    await world.approve(request, world.operator)

    result = await world.service.consume(request.id, call)
    again = await world.service.consume(request.id, call)

    assert (result.ok, result.replayed) == (True, False)
    assert (again.ok, again.replayed, again.idempotency_key) == (True, True, str(request.id))
    assert (await world.service.check(request.id, call)).ok is False


async def test_stored_string_rules_and_roles_keep_their_meaning(world: World) -> None:
    await world.set_policy(
        rules=({"kind": "message_send", "effect": "deny"}, {"kind": "read", "effect": "deny"}),
        approver_min_roles={"pms_change": "admin"},
    )

    send = await world.service.evaluate(world.call(ActionKind.MESSAGE_SEND))
    read = await world.service.evaluate(world.call(ActionKind.READ, RiskLevel.LOW))
    pms = await world.service.evaluate(world.call())

    assert (send.verdict, read.verdict) == (Verdict.DENY, Verdict.DENY)
    assert pms.verdict == Verdict.ASK
    assert {str(r) for r in pms.approver_roles} == {"admin", "owner"}


async def test_stored_high_risk_blocks_self_approval(world: World) -> None:
    _, request = await world.request(world.call(risk=RiskLevel.HIGH))

    with pytest.raises(ForbiddenError, match="their own high-risk"):
        await world.approve(request, world.requester)


def test_typed_stores_are_the_in_memory_ones() -> None:
    world = _typed_world()
    assert isinstance(world.approvals, InMemoryApprovalStore)
    assert isinstance(world.policies, InMemoryPolicyStore)
