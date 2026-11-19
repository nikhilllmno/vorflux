"""Policy evaluation: allow, ask (with approver roles), or deny.

Rules (docs/approvals.md, "Evaluation order"):

1. A call without a tenant, a run id, a tool call id, a toolset, or a tool: deny.
2. A tool that is not in the registry: deny. The registry sets the kind and the risk.
3. Explicit rules (platform rules, then the hotel's rules) that match the kind and the toolset
   (or every toolset) combine as "strictest wins": deny > ask > allow.
4. With no matching rule, the default applies: `read` is allowed, every other kind asks.
5. No "yolo": only a read-only action below `high` risk can be allowed. A hotel operation (D19),
   a code action, or a `high` risk action asks, whatever the rules say.
6. Asking needs a preview that the harness renders from the arguments. Arguments that do not
   fit the kind's proposal: deny.
7. Code actions are approved only by our team's `code_reviewer`; hotel roles never.
   Other actions are approved by hotel roles at or above the hotel's minimum role for that kind
   (default `operator`, Section 7.1).
8. Any error: deny.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import UUID

from harness.approvals.catalog import ToolCatalog, ToolSpec
from harness.approvals.model import (
    ACTION_CLASSES,
    ActionClass,
    ActionKind,
    ApproverRole,
    Decision,
    HotelRole,
    Preview,
    ProposedCall,
    RiskLevel,
    TeamRole,
    Verdict,
    proposal_hash,
)
from harness.approvals.preview import render_preview
from harness.tenancy import TenantContext

if TYPE_CHECKING:
    from harness.approvals.store import PolicyStore

logger = logging.getLogger(__name__)

DEFAULT_APPROVAL_TTL = timedelta(hours=24)
MIN_APPROVAL_TTL = timedelta(minutes=1)
MAX_APPROVAL_TTL = timedelta(days=7)
DEFAULT_USE_WINDOW = timedelta(minutes=15)
MIN_USE_WINDOW = timedelta(minutes=1)
MAX_USE_WINDOW = timedelta(hours=24)
DEFAULT_APPROVER_MIN_ROLE = HotelRole.OPERATOR
CODE_APPROVERS: frozenset[ApproverRole] = frozenset({TeamRole.CODE_REVIEWER})


class Effect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


_STRICTNESS: Mapping[Effect, int] = MappingProxyType(
    {Effect.ALLOW: 0, Effect.ASK: 1, Effect.DENY: 2}
)

DEFAULT_EFFECTS: Mapping[ActionKind, Effect] = MappingProxyType(
    {kind: Effect.ALLOW if kind == ActionKind.READ else Effect.ASK for kind in ActionKind}
)


@dataclass(frozen=True, slots=True)
class Rule:
    """An explicit effect for one action kind, in one toolset or (toolset=None) in every toolset.

    `kind` and `effect` are parsed on construction, so a rule loaded as plain strings works the
    same as a typed one. An unknown value raises.
    """

    kind: ActionKind
    effect: Effect
    toolset: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ActionKind(self.kind))
        object.__setattr__(self, "effect", Effect(self.effect))

    def matches(self, kind: ActionKind, toolset: str) -> bool:
        return self.kind == kind and (self.toolset is None or self.toolset == toolset)


@dataclass(frozen=True, slots=True)
class HotelPolicy:
    """Per-hotel settings. Hotel admins set `approver_min_roles`; see ApprovalService."""

    org_id: UUID
    hotel_id: UUID
    rules: tuple[Rule, ...] = ()
    approver_min_roles: Mapping[ActionKind, HotelRole] = field(
        default_factory=lambda: MappingProxyType({})
    )
    approval_ttl: timedelta = DEFAULT_APPROVAL_TTL  # deadline to decide
    use_window: timedelta = DEFAULT_USE_WINDOW  # deadline to use, from the decision
    # Decision to confirm: the requester cannot approve their own `high` risk action by default.
    forbid_self_approval_of_high_risk: bool = True
    version: int = 0

    def __post_init__(self) -> None:
        roles = {ActionKind(k): HotelRole(v) for k, v in self.approver_min_roles.items()}
        for kind, role in roles.items():
            validate_approver_min_role(kind, role)
        object.__setattr__(self, "approver_min_roles", MappingProxyType(roles))
        rules = tuple(r if isinstance(r, Rule) else Rule(**r) for r in self.rules)
        for rule in rules:
            if ACTION_CLASSES[rule.kind] == ActionClass.CODE:
                raise ValueError("hotel rules cannot target code actions")
        object.__setattr__(self, "rules", rules)
        if not MIN_APPROVAL_TTL <= self.approval_ttl <= MAX_APPROVAL_TTL:
            raise ValueError(
                f"approval_ttl must be between {MIN_APPROVAL_TTL} and {MAX_APPROVAL_TTL}"
            )
        if not MIN_USE_WINDOW <= self.use_window <= MAX_USE_WINDOW:
            raise ValueError(f"use_window must be between {MIN_USE_WINDOW} and {MAX_USE_WINDOW}")
        if not isinstance(self.forbid_self_approval_of_high_risk, bool):
            raise ValueError("forbid_self_approval_of_high_risk must be a bool")

    def min_role_for(self, kind: ActionKind) -> HotelRole:
        return self.approver_min_roles.get(ActionKind(kind), DEFAULT_APPROVER_MIN_ROLE)


def validate_approver_min_role(kind: ActionKind, role: HotelRole) -> None:
    """Raise ValueError for a setting that the design does not allow."""
    action_class = ACTION_CLASSES[ActionKind(kind)]
    if action_class == ActionClass.CODE:
        raise ValueError("hotel roles can never approve code actions")
    if action_class == ActionClass.READ_ONLY:
        raise ValueError("read-only actions need no approval")
    if HotelRole(role) == HotelRole.VIEWER:
        raise ValueError("viewers cannot approve; the minimum role is operator")


def hotel_roles_at_least(min_role: HotelRole) -> frozenset[ApproverRole]:
    return frozenset(role for role in HotelRole if role.at_least(min_role))


def approver_roles_for(policy: HotelPolicy, kind: ActionKind) -> frozenset[ApproverRole]:
    """The roles that may approve `kind` in this hotel now."""
    if ACTION_CLASSES[ActionKind(kind)] == ActionClass.CODE:
        return CODE_APPROVERS
    return hotel_roles_at_least(policy.min_role_for(kind))


@dataclass(frozen=True, slots=True)
class Assessment:
    """The decision plus what the service needs to bind an approval (absent on deny)."""

    decision: Decision
    spec: ToolSpec | None = None
    preview: Preview | None = None
    proposal_hash: str | None = None


def _deny(call: object, reason: str) -> Assessment:
    tool_call_id = getattr(call, "tool_call_id", "")
    return Assessment(
        Decision(tool_call_id if isinstance(tool_call_id, str) else "", Verdict.DENY, reason)
    )


class PolicyEngine:
    """Pure policy decisions. `assess` and `evaluate` never raise: every failure is a deny."""

    def __init__(
        self,
        policies: "PolicyStore",
        catalog: ToolCatalog,
        platform_rules: Sequence[Rule] = (),
    ) -> None:
        self._policies = policies
        self._catalog = catalog
        self._platform_rules = tuple(platform_rules)

    async def hotel_policy(self, org_id: UUID, hotel_id: UUID) -> HotelPolicy:
        stored = await self._policies.get(org_id, hotel_id)
        return stored or HotelPolicy(org_id=org_id, hotel_id=hotel_id)

    async def evaluate(self, call: ProposedCall) -> Decision:
        return (await self.assess(call)).decision

    async def assess(self, call: ProposedCall) -> Assessment:
        try:
            return await self._assess(call)
        except Exception:
            logger.exception("approval policy evaluation failed; denying the action")
            return _deny(call, "policy evaluation failed")

    async def _assess(self, call: ProposedCall) -> Assessment:
        if not isinstance(call, ProposedCall) or not isinstance(call.tenant, TenantContext):
            return _deny(call, "tool call has no tenant context")
        names = (call.run_id, call.tool_call_id, call.toolset, call.tool)
        if not all(isinstance(name, str) and name for name in names):
            return _deny(call, "tool call needs a run id, a tool call id, a toolset, and a tool")
        spec = self._catalog.get(call.toolset, call.tool)
        if spec is None:
            return _deny(call, f"tool {call.toolset}/{call.tool} is not registered")

        policy = await self.hotel_policy(call.org_id, call.hotel_id)
        matching = [
            rule.effect
            for rule in (*self._platform_rules, *policy.rules)
            if rule.matches(spec.kind, call.toolset)
        ]
        effect = (
            max(matching, key=_STRICTNESS.__getitem__) if matching else DEFAULT_EFFECTS[spec.kind]
        )
        if effect == Effect.DENY:
            return _deny(call, f"{spec.kind} is denied in toolset {call.toolset}")
        allowed = (
            effect == Effect.ALLOW
            and ACTION_CLASSES[spec.kind] == ActionClass.READ_ONLY
            and spec.risk != RiskLevel.HIGH
        )

        try:
            preview = render_preview(spec.kind, call.args)
        except ValueError:
            return _deny(call, f"arguments do not match the {spec.kind} proposal")
        bound = proposal_hash(call, spec.kind, spec.risk, preview)

        if allowed:
            decision = Decision(call.tool_call_id, Verdict.ALLOW, f"{spec.kind} is allowed")
        else:
            roles = approver_roles_for(policy, spec.kind)
            needs = (
                "a code reviewer"
                if roles == CODE_APPROVERS
                else f"{policy.min_role_for(spec.kind)} or higher"
            )
            decision = Decision(
                call.tool_call_id, Verdict.ASK, f"{spec.kind} needs approval by {needs}", roles
            )
        return Assessment(decision, spec, preview, bound)
