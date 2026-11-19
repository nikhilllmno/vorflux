"""Value types for the approval policy engine: tool calls, roles, decisions, approval requests.

Design authority: docs/architecture.md (D19, Sections 4.2, 5.2, 7.1, 10.10) and docs/approvals.md.
Everything here is immutable. State changes produce a new value with `dataclasses.replace`.

Values that can come back from a store as plain strings (enums, roles) are parsed in
`__post_init__`, so a row that a database driver returns as text cannot bypass a check. An
unknown value raises, except an unknown stored risk, which is read as `high` (fail closed).
"""

import hashlib
import json
import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from harness.tenancy import TenantContext

logger = logging.getLogger(__name__)


class ActionKind(StrEnum):
    """What a registered tool does to the world. The tool registry sets it, never the caller."""

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


def stored_risk(value: object) -> RiskLevel:
    """Parse a risk read back from a store. An unknown value is treated as `high`."""
    try:
        return RiskLevel(str(value))
    except ValueError:
        logger.warning("unknown stored risk %r; treating it as high", value)
        return RiskLevel.HIGH


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
        return self.rank >= HotelRole(other).rank


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


def parse_approver_role(value: object) -> ApproverRole:
    if not isinstance(value, str):
        raise ValueError(f"unknown approver role: {value!r}")
    if value in HotelRole:
        return HotelRole(value)
    if value in TeamRole:
        return TeamRole(value)
    raise ValueError(f"unknown approver role: {value!r}")


def parse_approver_roles(values: Iterable[object]) -> frozenset[ApproverRole]:
    return frozenset(parse_approver_role(value) for value in values)


class PrincipalKind(StrEnum):
    """Who is behind a principal. Only a `human` with a user session can decide an approval."""

    HUMAN = "human"  # a person signed in with a user session (DEX-12)
    RUN = "run"  # an agent run, an MCP tool, or any credential that acts for a requester


@dataclass(frozen=True, slots=True)
class Membership:
    """A hotel role in one org, optionally narrowed to one hotel (`hotel_id=None` = whole org)."""

    org_id: UUID
    role: HotelRole
    hotel_id: UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", HotelRole(self.role))

    def covers(self, org_id: UUID, hotel_id: UUID) -> bool:
        return self.org_id == org_id and (self.hotel_id is None or self.hotel_id == hotel_id)


@dataclass(frozen=True, slots=True)
class Principal:
    """The caller of a staff operation. Built at the edge by auth (DEX-12).

    `kind` has no default: the edge must say if this is a person or a run.
    """

    user_id: UUID
    kind: PrincipalKind
    memberships: frozenset[Membership] = field(default_factory=frozenset)
    team_roles: frozenset[TeamRole] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", PrincipalKind(self.kind))
        object.__setattr__(self, "team_roles", frozenset(TeamRole(r) for r in self.team_roles))

    @property
    def is_human(self) -> bool:
        return self.kind == PrincipalKind.HUMAN

    def hotel_roles(self, org_id: UUID, hotel_id: UUID) -> frozenset[HotelRole]:
        return frozenset(m.role for m in self.memberships if m.covers(org_id, hotel_id))

    def is_member(self, org_id: UUID, hotel_id: UUID) -> bool:
        return bool(self.hotel_roles(org_id, hotel_id))


# --- JSON values -------------------------------------------------------------------------------

JsonObject = Mapping[str, Any]
MAX_JSON_DEPTH = 32


def freeze_json(value: object, _depth: int = 0) -> Any:
    """A deep, immutable copy of a JSON value (dicts become read-only mappings, lists tuples).

    Raises TypeError or ValueError for anything that is not plain JSON: a key that is not a
    string, NaN or infinity, a set, bytes, or any other type.
    """
    if _depth > MAX_JSON_DEPTH:
        raise ValueError("JSON value is nested too deeply")
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        items: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object keys must be strings, not {type(key).__name__}")
            items[key] = freeze_json(item, _depth + 1)
        return MappingProxyType(items)
    if isinstance(value, list | tuple):
        return tuple(freeze_json(item, _depth + 1) for item in value)
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def thaw_json(value: Any) -> Any:
    """A plain, mutable copy (dicts and lists) of a value from `freeze_json`."""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [thaw_json(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    """Sorted keys, no whitespace, no NaN. Raises for anything that is not plain JSON."""
    return json.dumps(
        thaw_json(freeze_json(value)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _frozen_object(value: object, name: str) -> JsonObject:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    return frozen


# --- Tool calls and previews -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposedCall:
    """One tool call that a run wants to make, as the adapter reports it.

    The caller gives no kind or risk: the tool registry sets both. Tenant scope and the
    requester come only from `tenant` (the run's TenantContext), never from `args`.
    `args` is frozen on construction; building a call with non-JSON args raises.
    """

    tenant: TenantContext
    run_id: str
    tool_call_id: str
    toolset: str
    tool: str
    args: JsonObject = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", _frozen_object(self.args, "args"))

    @property
    def org_id(self) -> UUID:
        return self.tenant.org_id

    @property
    def hotel_id(self) -> UUID:
        return self.tenant.hotel_id

    @property
    def requester_id(self) -> UUID:
        return self.tenant.actor_id


@dataclass(frozen=True, slots=True)
class PreviewField:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class Preview:
    """The approval card that the harness renders from the arguments (Section 10.10 step 2)."""

    title: str
    fields: tuple[PreviewField, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "fields": [{"label": f.label, "value": f.value} for f in self.fields],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "Preview":
        return cls(
            title=str(value["title"]),
            fields=tuple(PreviewField(str(f["label"]), str(f["value"])) for f in value["fields"]),
        )

    def text(self) -> str:
        return "\n".join([self.title, *(f"{f.label}: {f.value}" for f in self.fields)])


def proposal_hash(call: ProposedCall, kind: ActionKind, risk: RiskLevel, preview: Preview) -> str:
    """Hash of the exact proposal: tenant, tool, kind, risk, arguments, and the rendered preview.

    An approval binds to this hash. Any change to the arguments, or to what the approver saw,
    needs a new approval.
    """
    document = {
        "v": 2,
        "org_id": str(call.org_id),
        "hotel_id": str(call.hotel_id),
        "toolset": call.toolset,
        "tool": call.tool,
        "kind": str(kind),
        "risk": str(risk),
        "args": call.args,
        "preview": preview.as_json(),
    }
    return "sha256:" + hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


# --- Decisions ---------------------------------------------------------------------------------


class Verdict(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Decision:
    """The policy's answer for one tool call. `ask` carries the roles that may approve."""

    tool_call_id: str
    verdict: Verdict
    reason: str
    approver_roles: frozenset[ApproverRole] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", Verdict(self.verdict))
        if (self.verdict == Verdict.ASK) != bool(self.approver_roles):
            raise ValueError("an ask decision needs approver roles; other decisions have none")


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A pending or decided approval for one tool call and one exact proposal."""

    id: UUID
    org_id: UUID
    hotel_id: UUID
    run_id: str
    tool_call_id: str
    requester_id: UUID
    toolset: str
    tool: str
    kind: ActionKind
    risk: RiskLevel
    args: JsonObject  # the approved arguments; `consume` returns them to the executor
    preview: Preview  # rendered by the harness from `args`; what the approver sees
    proposal_hash: str
    approver_roles: frozenset[ApproverRole]  # snapshot of the policy at request time
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime  # deadline to decide
    note: str | None = None  # optional agent text, shown as context only; not bound
    decided_by: UUID | None = None
    decided_at: datetime | None = None
    use_by: datetime | None = None  # deadline to use an approved request
    reason: str | None = None
    consumed_at: datetime | None = None  # set once; an approval is single use
    version: int = 0  # optimistic concurrency for store updates

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ActionKind(self.kind))
        object.__setattr__(self, "risk", stored_risk(self.risk))
        object.__setattr__(self, "status", ApprovalStatus(self.status))
        object.__setattr__(self, "approver_roles", parse_approver_roles(self.approver_roles))
        object.__setattr__(self, "args", _frozen_object(self.args, "args"))
        if not isinstance(self.preview, Preview):
            object.__setattr__(self, "preview", Preview.from_json(self.preview))

    @property
    def is_code(self) -> bool:
        return ACTION_CLASSES[self.kind] == ActionClass.CODE


@dataclass(frozen=True, slots=True)
class ApprovalCheck:
    """Result of `check` and `consume`. `ok` is False for every failure, with a reason.

    A successful `consume` also returns the approved arguments and an idempotency key. The
    executor runs the tool with exactly these arguments.
    """

    ok: bool
    reason: str
    status: ApprovalStatus | None = None
    args: JsonObject | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalResolution:
    """Sent to the run orchestrator when a request leaves `pending`, so the paused run resumes."""

    approval_id: UUID
    org_id: UUID
    hotel_id: UUID
    run_id: str
    tool_call_id: str
    outcome: ApprovalStatus
    reason: str | None
