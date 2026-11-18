"""Tenant (hotel) scope that every request, run, and job carries.

The design doc (docs/architecture.md, section "Multi-tenancy and isolation") is the authority.
This module holds only the value type and the one invariant the rest of the code relies on:
a run may touch a repository only if the tenant's allowlist names it.
"""

from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Who a unit of work runs for. Built once at the edge, never mutated."""

    org_id: UUID
    hotel_id: UUID
    actor_id: UUID
    allowed_repos: frozenset[str] = field(default_factory=frozenset)

    def can_access_repo(self, repo: str) -> bool:
        """True only for an exact `owner/name` match in the allowlist (case-insensitive)."""
        wanted = repo.strip().lower()
        return any(wanted == allowed.lower() for allowed in self.allowed_repos)
