"""Shared test world for approval tests: two orgs, three hotels, people with roles, a fake clock,
and a tool registry with one tool per (kind, risk) in two toolsets."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from harness.approvals import (
    ActionKind,
    ActorType,
    ApprovalRequest,
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
    PrincipalKind,
    ProposedCall,
    RiskLevel,
    Rule,
    TeamRole,
    ToolCatalog,
    ToolSpec,
)
from harness.tenancy import TenantContext

TOOLSETS = ("ops", "reports")

# Valid arguments for each kind's proposal (see harness.approvals.preview).
DEFAULT_ARGS: Mapping[ActionKind, Mapping[str, Any]] = {
    ActionKind.READ: {"report": "occupancy", "date": "2026-12-24"},
    ActionKind.PMS_CHANGE: {
        "system": "Beds24",
        "target": "Deluxe King, 24 Dec 2026",
        "setting": "rate",
        "old": 9000,
        "new": 9900,
        "guests_affected": 0,
    },
    ActionKind.MESSAGE_SEND: {
        "channel": "email",
        "recipients": ["guest@example.com"],
        "subject": "Your stay",
        "body": "Welcome!",
    },
    ActionKind.EXTERNAL_WRITE: {
        "system": "Channel manager",
        "target": "Deluxe King",
        "changes": {"min_stay": 2},
    },
    ActionKind.DELETE: {"system": "Beds24", "target": "Test bookings", "count": 2},
    ActionKind.GIT_PUSH: {
        "repository": "dextr/hotel-a1",
        "branch": "harness/fix-rates",
        "commit": "0123abc",
    },
    ActionKind.CODE_CHANGE: {
        "repository": "dextr/hotel-a1",
        "branch": "harness/fix-rates",
        "paths": ["prompts/front_desk.md"],
        "description": "Fix the rate wording",
    },
}


def tool_name(kind: ActionKind, risk: RiskLevel) -> str:
    return f"{kind}_{risk}"


def default_catalog() -> ToolCatalog:
    return ToolCatalog(
        ToolSpec(toolset, tool_name(kind, risk), kind, risk)
        for toolset in TOOLSETS
        for kind in ActionKind
        for risk in RiskLevel
    )


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

    async def resume(self, resolution: ApprovalResolution) -> None:
        self.resolutions.append(resolution)


def human(*memberships: Membership, team_roles: frozenset[TeamRole] = frozenset()) -> Principal:
    return Principal(uuid4(), PrincipalKind.HUMAN, frozenset(memberships), team_roles)


@dataclass
class World:
    org_a: UUID = field(default_factory=uuid4)
    org_b: UUID = field(default_factory=uuid4)
    hotel_a1: UUID = field(default_factory=uuid4)
    hotel_a2: UUID = field(default_factory=uuid4)
    hotel_b1: UUID = field(default_factory=uuid4)
    clock: FakeClock = field(default_factory=FakeClock)
    audit: InMemoryAuditLog = field(default_factory=InMemoryAuditLog)
    # Stores default to ones that write to `audit`; a test can pass its own store.
    approvals: Any = None
    policies: Any = None
    catalog: ToolCatalog = field(default_factory=default_catalog)
    resumer: RecordingResumer = field(default_factory=RecordingResumer)
    platform_rules: Sequence[Rule] = ()

    def __post_init__(self) -> None:
        if self.policies is None:
            self.policies = InMemoryPolicyStore(self.audit)
        if self.approvals is None:
            # Shares the policy store's lock and the fake clock (guarded writes).
            self.approvals = InMemoryApprovalStore(self.policies, self.audit, self.clock)
        self.engine = PolicyEngine(self.policies, self.catalog, self.platform_rules)
        self.service = ApprovalService(
            self.engine, self.approvals, self.policies, self.audit, self.resumer, self.clock
        )
        # Requester: an operator of hotel A1 who starts runs.
        self.requester = human(Membership(self.org_a, HotelRole.OPERATOR, self.hotel_a1))
        self.operator = human(Membership(self.org_a, HotelRole.OPERATOR, self.hotel_a1))
        self.admin = human(Membership(self.org_a, HotelRole.ADMIN, self.hotel_a1))
        self.owner = human(Membership(self.org_a, HotelRole.OWNER, self.hotel_a1))
        self.viewer = human(Membership(self.org_a, HotelRole.VIEWER, self.hotel_a1))
        self.org_a_admin = human(Membership(self.org_a, HotelRole.ADMIN))  # whole org
        self.hotel_a2_owner = human(Membership(self.org_a, HotelRole.OWNER, self.hotel_a2))
        self.org_b_owner = human(Membership(self.org_b, HotelRole.OWNER))
        self.code_reviewer = human(team_roles=frozenset({TeamRole.CODE_REVIEWER}))
        self.platform = human(team_roles=frozenset({TeamRole.PLATFORM}))
        # The requester's run: same user id and memberships, but not a person.
        self.run = Principal(self.requester.user_id, PrincipalKind.RUN, self.requester.memberships)

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

    def call(
        self,
        kind: ActionKind = ActionKind.PMS_CHANGE,
        risk: RiskLevel = RiskLevel.MEDIUM,
        args: Mapping[str, Any] | None = None,
        *,
        tenant: TenantContext | None = None,
        toolset: str = "ops",
        tool: str | None = None,
        run_id: str = "run-1",
        tool_call_id: str = "call-1",
    ) -> ProposedCall:
        return ProposedCall(
            tenant=tenant or self.tenant(),
            run_id=run_id,
            tool_call_id=tool_call_id,
            toolset=toolset,
            tool=tool or tool_name(kind, risk),
            args=DEFAULT_ARGS[kind] if args is None else args,
        )

    async def set_policy(self, **kwargs: Any) -> HotelPolicy:
        """Seed the hotel A1 policy. The seed's audit event goes to a scratch log, not `audit`."""
        stored = await self.policies.get(self.org_a, self.hotel_a1)
        version = 0 if stored is None else stored.version + 1
        policy = HotelPolicy(org_id=self.org_a, hotel_id=self.hotel_a1, version=version, **kwargs)
        seed = AuditEvent(
            org_id=self.org_a,
            hotel_id=self.hotel_a1,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action="test.seed",
            subject_type="approval_policy",
            subject_id=str(self.hotel_a1),
            at=self.clock.now,
        )
        audit, self.policies.audit = self.policies.audit, InMemoryAuditLog()
        try:
            assert await self.policies.put(policy, seed)
        finally:
            self.policies.audit = audit
        return policy

    async def request(
        self, call: ProposedCall | None = None, note: str | None = None
    ) -> tuple[ProposedCall, ApprovalRequest]:
        call = call or self.call()
        return call, await self.service.request(call, note=note)

    async def approve(self, request: ApprovalRequest, who: Principal) -> ApprovalRequest:
        tenant = self.tenant(request.org_id, request.hotel_id, who)
        return await self.service.approve(who, tenant, request.id)

    async def reject(self, request: ApprovalRequest, who: Principal) -> ApprovalRequest:
        tenant = self.tenant(request.org_id, request.hotel_id, who)
        return await self.service.reject(who, tenant, request.id)

    def audit_actions(self) -> list[str]:
        return [event.action for event in self.audit.events]
