"""Policy evaluation: allow, require approval (with approver roles), or deny.

Rules (docs/approvals.md, "Evaluation order"):

1. Unknown action kind or risk level, a payload that is not plain JSON, or any error: deny.
2. Explicit rules (platform rules, then the hotel's rules) that match the kind and the toolset
   (or every toolset) combine as "strictest wins": deny > ask > allow.
3. With no matching rule, the default applies: `read` is allowed, every other kind asks.
4. No "yolo": a hotel operation (D19) or a `high` risk action is never allowed; it asks.
5. Code actions are approved only by our team's `code_reviewer`; hotel roles never.
   Other actions are approved by hotel roles at or above the hotel's minimum role for that kind
   (default `operator`, Section 7.1).
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import UUID

from harness.approvals.model import (
    ACTION_CLASSES,
    Action,
    ActionClass,
    ActionKind,
    Allow,
    ApproverRole,
    Decision,
    Deny,
    HotelRole,
    RequireApproval,
    RiskLevel,
    TeamRole,
    payload_hash,
)
from harness.tenancy import TenantContext

if TYPE_CHECKING:
    from harness.approvals.store import PolicyStore

logger = logging.getLogger(__name__)

DEFAULT_APPROVAL_TTL = timedelta(hours=24)
MIN_APPROVAL_TTL = timedelta(minutes=1)
MAX_APPROVAL_TTL = timedelta(days=7)
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
    {kind: Effect.ALLOW if kind is ActionKind.READ else Effect.ASK for kind in ActionKind}
)


@dataclass(frozen=True, slots=True)
class Rule:
    """An explicit effect for one action kind, in one toolset or (toolset=None) in every toolset."""

    kind: ActionKind
    effect: Effect
    toolset: str | None = None

    def matches(self, kind: ActionKind, toolset: str) -> bool:
        return self.kind is kind and (self.toolset is None or self.toolset == toolset)


@dataclass(frozen=True, slots=True)
class HotelPolicy:
    """Per-hotel settings. Hotel admins set `approver_min_roles`; see ApprovalService."""

    org_id: UUID
    hotel_id: UUID
    rules: tuple[Rule, ...] = ()
    approver_min_roles: Mapping[ActionKind, HotelRole] = field(
        default_factory=lambda: MappingProxyType({})
    )
    approval_ttl: timedelta = DEFAULT_APPROVAL_TTL
    # The design has no two-person rule (Q8, D16), so the requester may approve their own
    # action by default. A hotel can turn this on for `high` risk actions.
    forbid_self_approval_of_high_risk: bool = False
    version: int = 0

    def __post_init__(self) -> None:
        roles = dict(self.approver_min_roles)
        for kind, role in roles.items():
            validate_approver_min_role(kind, role)
        object.__setattr__(self, "approver_min_roles", MappingProxyType(roles))
        if not MIN_APPROVAL_TTL <= self.approval_ttl <= MAX_APPROVAL_TTL:
            raise ValueError(
                f"approval_ttl must be between {MIN_APPROVAL_TTL} and {MAX_APPROVAL_TTL}"
            )
        for rule in self.rules:
            if ACTION_CLASSES[rule.kind] is ActionClass.CODE:
                raise ValueError("hotel rules cannot target code actions")

    def min_role_for(self, kind: ActionKind) -> HotelRole:
        return self.approver_min_roles.get(kind, DEFAULT_APPROVER_MIN_ROLE)


def validate_approver_min_role(kind: ActionKind, role: HotelRole) -> None:
    """Raise ValueError for a setting that the design does not allow."""
    action_class = ACTION_CLASSES[ActionKind(kind)]
    if action_class is ActionClass.CODE:
        raise ValueError("hotel roles can never approve code actions")
    if action_class is ActionClass.READ_ONLY:
        raise ValueError("read-only actions need no approval")
    if HotelRole(role) is HotelRole.VIEWER:
        raise ValueError("viewers cannot approve; the minimum role is operator")


def hotel_roles_at_least(min_role: HotelRole) -> frozenset[ApproverRole]:
    return frozenset(role for role in HotelRole if role.at_least(min_role))


class PolicyEngine:
    """Pure policy decisions. `evaluate` never raises: every failure becomes `Deny`."""

    def __init__(self, policies: "PolicyStore", platform_rules: Sequence[Rule] = ()) -> None:
        self._policies = policies
        self._platform_rules = tuple(platform_rules)

    def hotel_policy(self, org_id: UUID, hotel_id: UUID) -> HotelPolicy:
        return self._policies.get(org_id, hotel_id) or HotelPolicy(org_id=org_id, hotel_id=hotel_id)

    def evaluate(self, action: Action) -> Decision:
        try:
            return self._evaluate(action)
        except Exception:
            logger.exception("approval policy evaluation failed; denying the action")
            return Deny("policy evaluation failed")

    def _evaluate(self, action: Action) -> Decision:
        if not isinstance(action.tenant, TenantContext):
            return Deny("action has no tenant context")
        try:
            kind = ActionKind(action.kind)
        except ValueError:
            return Deny(f"unknown action kind: {action.kind!r}")
        try:
            risk = RiskLevel(action.risk)
        except ValueError:
            return Deny(f"unknown risk level: {action.risk!r}")
        if not action.toolset or not action.tool or not action.run_id:
            return Deny("action needs a toolset, a tool, and a run id")
        try:
            payload_hash(action)
        except (TypeError, ValueError):
            return Deny("action payload is not plain JSON")

        policy = self.hotel_policy(action.org_id, action.hotel_id)
        matching = [
            rule.effect
            for rule in (*self._platform_rules, *policy.rules)
            if rule.matches(kind, action.toolset)
        ]
        effect = max(matching, key=_STRICTNESS.__getitem__) if matching else DEFAULT_EFFECTS[kind]
        action_class = ACTION_CLASSES[kind]

        if effect is Effect.DENY:
            return Deny(f"{kind} is denied in toolset {action.toolset}")
        if effect is Effect.ALLOW:
            if action_class is ActionClass.HOTEL_OPERATION:
                effect = Effect.ASK  # D19: every hotel operation needs an approval
            elif risk is RiskLevel.HIGH:
                effect = Effect.ASK  # no auto-allow for high risk
            else:
                return Allow(f"{kind} is allowed in toolset {action.toolset}")

        if action_class is ActionClass.CODE:
            return RequireApproval(CODE_APPROVERS, f"{kind} needs a code reviewer")
        min_role = policy.min_role_for(kind)
        return RequireApproval(
            hotel_roles_at_least(min_role), f"{kind} needs approval by {min_role} or higher"
        )
