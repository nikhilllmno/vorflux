"""Tenant (hotel) scope that every request, run, and job carries.

The design doc (docs/architecture.md, section "Multi-tenancy and isolation") is the authority.
This module holds the value type, the hotel roles, the minimum role per action, and the one
invariant the rest of the code relies on: a run may touch a repository only if the tenant's
allowlist names it. docs/auth.md describes how the API builds a `TenantContext` per request.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID


class HotelRole(StrEnum):
    """Hotel roles (docs/architecture.md Section 7.1).

    The values are the labels of the `public.hotel_role` enum in the tenancy migration
    (DEX-10, https://github.com/nikhilllmno/vorflux/pull/5), so a database row parses directly.
    """

    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def at_least(self, other: "HotelRole") -> bool:
        return self.rank >= HotelRole(other).rank


_ROLE_RANK: Mapping[HotelRole, int] = MappingProxyType(
    {HotelRole.VIEWER: 0, HotelRole.OPERATOR: 1, HotelRole.ADMIN: 2, HotelRole.OWNER: 3}
)


class Action(StrEnum):
    """What a staff user does inside one hotel. Each action has one minimum role.

    Approving a hotel operation is not here: its approver roles come from the hotel's approval
    policy (DEX-20), which admins configure per operation kind.
    """

    VIEW = "view"
    START_RUN = "start_run"
    REQUEST_CHANGE = "request_change"
    CONFIRM_CHANGE = "confirm_change"
    CONFIGURE_APPROVALS = "configure_approvals"
    MANAGE_MEMBERS = "manage_members"
    MANAGE_ORG = "manage_org"


_MIN_ROLE: Mapping[Action, HotelRole] = MappingProxyType(
    {
        Action.VIEW: HotelRole.VIEWER,
        Action.START_RUN: HotelRole.OPERATOR,
        Action.REQUEST_CHANGE: HotelRole.OPERATOR,
        Action.CONFIRM_CHANGE: HotelRole.OPERATOR,
        Action.CONFIGURE_APPROVALS: HotelRole.ADMIN,
        Action.MANAGE_MEMBERS: HotelRole.ADMIN,
        Action.MANAGE_ORG: HotelRole.OWNER,
    }
)


def min_role_for(action: Action) -> HotelRole:
    """The lowest hotel role that may perform `action`."""
    return _MIN_ROLE[Action(action)]


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Who a unit of work runs for. Built once at the edge, never mutated.

    `actor_id` is the user id: the JWT `sub` claim, which is also `auth.uid()` in Postgres.
    `role` is the caller's effective hotel role in this hotel, set by the auth edge
    (`harness.auth.require_tenant`). A context built without a role allows no action.
    """

    org_id: UUID
    hotel_id: UUID
    actor_id: UUID
    allowed_repos: frozenset[str] = field(default_factory=frozenset)
    role: HotelRole | None = None

    def can_access_repo(self, repo: str) -> bool:
        """True only for an exact `owner/name` match in the allowlist (case-insensitive)."""
        wanted = repo.strip().lower()
        return any(wanted == allowed.lower() for allowed in self.allowed_repos)

    def allows(self, action: Action) -> bool:
        """True when this context's role is at least the minimum role for `action`."""
        return self.role is not None and self.role.at_least(min_role_for(action))
