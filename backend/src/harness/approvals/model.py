"""Value types for the approval policy engine: actions, roles, decisions, and approval requests.

Design authority: docs/architecture.md (D19, Sections 4.2, 5.2, 7.1, 10.10) and docs/approvals.md.
Everything here is immutable. State changes produce a new value with `dataclasses.replace`.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from harness.tenancy import TenantContext


class ActionKind(StrEnum):
    """What a tool call does to the world. The adapter or MCP tool definition sets it."""

    READ = "read"  # lookups and reports; changes nothing
    GIT_PUSH = "git_push"  # push a harness/* branch
    CODE_CHANGE = "code_change"  # a code or protected-path change that needs a human
    MESSAGE_SEND = "message_send"  # guest message or campaign; cannot be unsent
    PMS_CHANGE = "pms_change"  # rate, inventory, or booking change in a PMS
    EXTERNAL_WRITE = "external_write"  # any other write to a system outside the harness
    DELETE = "delete"  # delete data in a hotel or external system


class ActionClass(StrEnum):
    READ_ONLY = "read_only"
    HOTEL_OPERATION = "hotel_operation"  # D19: always needs a hotel staff approval
    CODE = "code"  # hotel roles can never approve (Section 7.1)


ACTION_CLASSES: Mapping[ActionKind, ActionClass] = MappingProxyType(
    {
        ActionKind.READ: ActionClass.READ_ONLY,
        ActionKind.GIT_PUSH: ActionClass.CODE,
        ActionKind.CODE_CHANGE: ActionClass.CODE,
        ActionKind.MESSAGE_SEND: ActionClass.HOTEL_OPERATION,
        ActionKind.PMS_CHANGE: ActionClass.HOTEL_OPERATION,
        ActionKind.EXTERNAL_WRITE: ActionClass.HOTEL_OPERATION,
        ActionKind.DELETE: ActionClass.HOTEL_OPERATION,
    }
)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class HotelRole(StrEnum):
    """Hotel roles from docs/architecture.md Section 7.1, highest first.

    TODO(DEX-10, PR #5): align with the `public.hotel_role` enum once the tenancy schema merges.
    """

    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"

    @property
    def rank(self) -> int:
        return _HOTEL_ROLE_RANK[self]

    def at_least(self, other: "HotelRole") -> bool:
        return self.rank >= other.rank


_HOTEL_ROLE_RANK: Mapping[HotelRole, int] = MappingProxyType(
    {HotelRole.VIEWER: 0, HotelRole.OPERATOR: 1, HotelRole.ADMIN: 2, HotelRole.OWNER: 3}
)


class TeamRole(StrEnum):
    """Our team's global roles (Section 7.1).

    `platform` is time-boxed break-glass support access. It never approves anything.
    """

    CODE_REVIEWER = "code_reviewer"
    PLATFORM = "platform"


ApproverRole = HotelRole | TeamRole


@dataclass(frozen=True, slots=True)
class Membership:
    """A hotel role in one org, optionally narrowed to one hotel (`hotel_id=None` = whole org)."""

    org_id: UUID
    role: HotelRole
    hotel_id: UUID | None = None

    def covers(self, org_id: UUID, hotel_id: UUID) -> bool:
        return self.org_id == org_id and (self.hotel_id is None or self.hotel_id == hotel_id)


@dataclass(frozen=True, slots=True)
class Principal:
    """A signed-in human who views or decides approvals. Built at the edge by auth (DEX-12)."""

    user_id: UUID
    memberships: frozenset[Membership] = field(default_factory=frozenset)
    team_roles: frozenset[TeamRole] = field(default_factory=frozenset)

    def hotel_roles(self, org_id: UUID, hotel_id: UUID) -> frozenset[HotelRole]:
        return frozenset(m.role for m in self.memberships if m.covers(org_id, hotel_id))

    def is_member(self, org_id: UUID, hotel_id: UUID) -> bool:
        return bool(self.hotel_roles(org_id, hotel_id))


@dataclass(frozen=True, slots=True)
class Action:
    """One proposed tool call, as the policy sees it.

    Tenant scope and the requester come only from `tenant` (the run's TenantContext), never from
    tool arguments. `payload` is the exact proposal (tool arguments) that an approval binds to.
    """

    tenant: TenantContext
    run_id: str
    toolset: str
    tool: str
    kind: ActionKind
    risk: RiskLevel
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def org_id(self) -> UUID:
        return self.tenant.org_id

    @property
    def hotel_id(self) -> UUID:
        return self.tenant.hotel_id

    @property
    def requester_id(self) -> UUID:
        return self.tenant.actor_id


def payload_hash(action: Action) -> str:
    """Hash of the exact proposal: tenant, kind, risk, toolset, tool, and payload.

    Canonical JSON (sorted keys, no whitespace, no NaN). Raises TypeError or ValueError when the
    payload is not plain JSON; callers treat that as a reason to deny.
    """
    document = {
        "v": 1,
        "org_id": str(action.org_id),
        "hotel_id": str(action.hotel_id),
        "kind": str(action.kind),
        "risk": str(action.risk),
        "toolset": action.toolset,
        "tool": action.tool,
        "payload": action.payload,
    }
    blob = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Allow:
    reason: str


@dataclass(frozen=True, slots=True)
class RequireApproval:
    approver_roles: frozenset[ApproverRole]
    reason: str

    def __post_init__(self) -> None:
        if not self.approver_roles:
            raise ValueError("RequireApproval needs at least one approver role")


@dataclass(frozen=True, slots=True)
class Deny:
    reason: str


Decision = Allow | RequireApproval | Deny


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A pending or decided approval for one exact proposal (bound by `payload_hash`)."""

    id: UUID
    org_id: UUID
    hotel_id: UUID
    run_id: str
    call_id: str
    requester_id: UUID
    toolset: str
    tool: str
    kind: ActionKind
    risk: RiskLevel
    payload_hash: str
    summary: str  # plain-language preview shown to the approver
    approver_roles: frozenset[ApproverRole]  # snapshot of the policy at request time
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime  # deadline to decide and to use the approval
    decided_by: UUID | None = None
    decided_at: datetime | None = None
    reason: str | None = None
    consumed_at: datetime | None = None  # set once; an approval is single use
    version: int = 0  # optimistic concurrency for store updates

    @property
    def is_code(self) -> bool:
        return ACTION_CLASSES.get(self.kind) is ActionClass.CODE


@dataclass(frozen=True, slots=True)
class ApprovalCheck:
    """Result of `check` and `consume`. `ok` is False for every failure, with a reason."""

    ok: bool
    reason: str
    status: ApprovalStatus | None = None


@dataclass(frozen=True, slots=True)
class ApprovalResolution:
    """Sent to the run orchestrator when a request leaves `pending`, so the paused run resumes."""

    approval_id: UUID
    org_id: UUID
    hotel_id: UUID
    run_id: str
    call_id: str
    outcome: ApprovalStatus
    reason: str | None
