"""Shared test world for approval tests: two orgs, three hotels, people with roles, a fake clock."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from harness.approvals import (
    Action,
    ActionKind,
    ApprovalResolution,
    ApprovalService,
    AuditEvent,
    HotelPolicy,
    HotelRole,
    InMemoryApprovalStore,
    InMemoryAuditLog,
    InMemoryPolicyStore,
    Membership,
    PolicyEngine,
    Principal,
    RiskLevel,
    Rule,
    TeamRole,
)
from harness.tenancy import TenantContext


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 19, 6, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class RecordingResumer:
    def __init__(self) -> None:
        self.resolutions: list[ApprovalResolution] = []

    def resume(self, resolution: ApprovalResolution) -> None:
        self.resolutions.append(resolution)


@dataclass
class World:
    org_a: UUID = field(default_factory=uuid4)
    org_b: UUID = field(default_factory=uuid4)
    hotel_a1: UUID = field(default_factory=uuid4)
    hotel_a2: UUID = field(default_factory=uuid4)
    hotel_b1: UUID = field(default_factory=uuid4)
    clock: FakeClock = field(default_factory=FakeClock)
    # Stores default to ones that write to `audit`; a test can pass its own store.
    approvals: InMemoryApprovalStore = None  # type: ignore[assignment]
    policies: InMemoryPolicyStore = None  # type: ignore[assignment]
    audit: InMemoryAuditLog = field(default_factory=InMemoryAuditLog)
    resumer: RecordingResumer = field(default_factory=RecordingResumer)
    platform_rules: Sequence[Rule] = ()

    def __post_init__(self) -> None:
        if self.approvals is None:
            self.approvals = InMemoryApprovalStore(self.audit)
        if self.policies is None:
            self.policies = InMemoryPolicyStore(self.audit)
        self.engine = PolicyEngine(self.policies, self.platform_rules)
        self.service = ApprovalService(
            self.engine, self.approvals, self.policies, self.resumer, self.clock
        )
        # Requester: an operator of hotel A1 who starts runs.
        self.requester = self.person(Membership(self.org_a, HotelRole.OPERATOR, self.hotel_a1))
        self.operator = self.person(Membership(self.org_a, HotelRole.OPERATOR, self.hotel_a1))
        self.admin = self.person(Membership(self.org_a, HotelRole.ADMIN, self.hotel_a1))
        self.owner = self.person(Membership(self.org_a, HotelRole.OWNER, self.hotel_a1))
        self.viewer = self.person(Membership(self.org_a, HotelRole.VIEWER, self.hotel_a1))
        self.org_a_admin = self.person(Membership(self.org_a, HotelRole.ADMIN))  # whole org
        self.hotel_a2_owner = self.person(Membership(self.org_a, HotelRole.OWNER, self.hotel_a2))
        self.org_b_owner = self.person(Membership(self.org_b, HotelRole.OWNER))
        self.code_reviewer = Principal(uuid4(), team_roles=frozenset({TeamRole.CODE_REVIEWER}))
        self.platform = Principal(uuid4(), team_roles=frozenset({TeamRole.PLATFORM}))

    @staticmethod
    def person(*memberships: Membership) -> Principal:
        return Principal(uuid4(), memberships=frozenset(memberships))

    def tenant(
        self,
        org_id: UUID | None = None,
        hotel_id: UUID | None = None,
        actor: Principal | None = None,
    ) -> TenantContext:
        return TenantContext(
            org_id=org_id or self.org_a,
            hotel_id=hotel_id or self.hotel_a1,
            actor_id=(actor or self.requester).user_id,
        )

    def action(
        self,
        kind: Any = ActionKind.PMS_CHANGE,
        risk: Any = RiskLevel.MEDIUM,
        payload: Mapping[str, Any] | None = None,
        *,
        tenant: TenantContext | None = None,
        toolset: str = "ops",
        tool: str = "pms_set_rate",
        run_id: str = "run-1",
    ) -> Action:
        return Action(
            tenant=tenant or self.tenant(),
            run_id=run_id,
            toolset=toolset,
            tool=tool,
            kind=kind,
            risk=risk,
            payload=(
                payload
                if payload is not None
                else {"room": "Deluxe King", "date": "2026-12-24", "old": 9000, "new": 9900}
            ),
        )

    def set_policy(self, **kwargs: Any) -> HotelPolicy:
        """Seed the hotel A1 policy. The seed's audit event goes to a scratch log, not `audit`."""
        policy = HotelPolicy(org_id=self.org_a, hotel_id=self.hotel_a1, **kwargs)
        seed = AuditEvent(
            org_id=self.org_a,
            hotel_id=self.hotel_a1,
            actor_id=None,
            action="test.seed",
            subject_type="approval_policy",
            subject_id=str(self.hotel_a1),
            at=self.clock.now,
        )
        audit, self.policies.audit = self.policies.audit, InMemoryAuditLog()
        try:
            assert self.policies.put(policy, seed)
        finally:
            self.policies.audit = audit
        return policy

    def audit_actions(self) -> list[str]:
        return [event.action for event in self.audit.events]
