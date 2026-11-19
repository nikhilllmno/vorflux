"""Persistence interfaces for approvals and hotel policies, with in-memory implementations.

Every read and write is scoped by `org_id` and `hotel_id`. A lookup with the wrong tenant finds
nothing, which the API reports as 404. The Postgres implementation comes after the tenancy schema
(PR #5) merges; docs/approvals.md has the proposed tables.

Contract for every implementation (tests/test_approvals_store.py checks it):

- Every write takes the audit event of that change. The store writes the state and the event
  together or not at all (in Postgres: one transaction). A failed audit write leaves no state
  change, so no approval can be used without its audit event.
- Updates are compare-and-set on `version`.
- One tool call has at most one approval request: `add` raises `DuplicateToolCallError` for a
  second request with the same (org_id, hotel_id, run_id, tool_call_id).
- Values read back may be plain strings; the model types parse them (see model.py).
"""

import threading
from typing import Protocol
from uuid import UUID

from harness.approvals.audit import AuditEvent, InMemoryAuditLog
from harness.approvals.model import ApprovalRequest, ApprovalStatus
from harness.approvals.policy import HotelPolicy


class DuplicateToolCallError(ValueError):
    """The tool call already has an approval request (unique key in Postgres)."""


class ApprovalStore(Protocol):
    async def add(self, request: ApprovalRequest, event: AuditEvent) -> None:
        """Insert a new request and its audit event.

        Raises DuplicateToolCallError if the tool call already has a request, and ValueError if
        the id exists.
        """
        ...

    async def get(
        self, org_id: UUID, hotel_id: UUID, approval_id: UUID
    ) -> ApprovalRequest | None: ...

    async def get_by_tool_call(
        self, org_id: UUID, hotel_id: UUID, run_id: str, tool_call_id: str
    ) -> ApprovalRequest | None: ...

    async def list(
        self, org_id: UUID, hotel_id: UUID, *, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]:
        """Requests of one hotel, oldest first."""
        ...

    async def replace(
        self, current: ApprovalRequest, new: ApprovalRequest, event: AuditEvent
    ) -> bool:
        """Compare-and-set: store `new` and `event` only if the row still has `current.version`.

        `new.version` must be `current.version + 1`. Returns False when another writer won.
        In Postgres: `UPDATE ... WHERE id = $1 AND org_id = $2 AND hotel_id = $3 AND version = $4`.
        """
        ...


class PolicyStore(Protocol):
    async def get(self, org_id: UUID, hotel_id: UUID) -> HotelPolicy | None: ...

    async def put(self, policy: HotelPolicy, event: AuditEvent) -> bool:
        """Store `policy` and `event` if the stored version is `policy.version - 1`.

        Version 0 inserts and fails when a row exists. Returns False when another writer won.
        """
        ...


class SyncAuditLog(Protocol):
    def record_now(self, event: AuditEvent) -> None: ...


class InMemoryApprovalStore:
    """Thread-safe store for tests and local development. Not for production.

    The audit event is written first, under the same lock; if it raises, the state is unchanged.
    """

    def __init__(self, audit: SyncAuditLog | None = None) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[UUID, UUID, UUID], ApprovalRequest] = {}
        self.audit: SyncAuditLog = audit if audit is not None else InMemoryAuditLog()

    async def add(self, request: ApprovalRequest, event: AuditEvent) -> None:
        key = (request.org_id, request.hotel_id, request.id)
        call = (request.org_id, request.hotel_id, request.run_id, request.tool_call_id)
        with self._lock:
            if any(row_id == request.id for (_, _, row_id) in self._rows):
                raise ValueError(f"approval already exists: {request.id}")
            if any(_call_key(row) == call for row in self._rows.values()):
                raise DuplicateToolCallError(f"tool call already has an approval: {call[3]}")
            self.audit.record_now(event)
            self._rows[key] = request

    async def get(self, org_id: UUID, hotel_id: UUID, approval_id: UUID) -> ApprovalRequest | None:
        with self._lock:
            return self._rows.get((org_id, hotel_id, approval_id))

    async def get_by_tool_call(
        self, org_id: UUID, hotel_id: UUID, run_id: str, tool_call_id: str
    ) -> ApprovalRequest | None:
        wanted = (org_id, hotel_id, run_id, tool_call_id)
        with self._lock:
            return next((row for row in self._rows.values() if _call_key(row) == wanted), None)

    async def list(
        self, org_id: UUID, hotel_id: UUID, *, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]:
        with self._lock:
            rows = [
                row
                for (row_org, row_hotel, _), row in self._rows.items()
                if row_org == org_id
                and row_hotel == hotel_id
                and (status is None or row.status == status)
            ]
        return sorted(rows, key=lambda row: row.created_at)

    async def replace(
        self, current: ApprovalRequest, new: ApprovalRequest, event: AuditEvent
    ) -> bool:
        key = (current.org_id, current.hotel_id, current.id)
        if (new.org_id, new.hotel_id, new.id) != key or new.version != current.version + 1:
            raise ValueError("replace must keep the id and tenant and bump the version by one")
        if _call_key(new) != _call_key(current):
            raise ValueError("replace must keep the run and the tool call")
        with self._lock:
            stored = self._rows.get(key)
            if stored is None or stored.version != current.version:
                return False
            self.audit.record_now(event)
            self._rows[key] = new
            return True


def _call_key(row: ApprovalRequest) -> tuple[UUID, UUID, str, str]:
    return (row.org_id, row.hotel_id, row.run_id, row.tool_call_id)


class InMemoryPolicyStore:
    """Thread-safe store for tests and local development. Not for production."""

    def __init__(self, audit: SyncAuditLog | None = None) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[UUID, UUID], HotelPolicy] = {}
        self.audit: SyncAuditLog = audit if audit is not None else InMemoryAuditLog()

    async def get(self, org_id: UUID, hotel_id: UUID) -> HotelPolicy | None:
        with self._lock:
            return self._rows.get((org_id, hotel_id))

    async def put(self, policy: HotelPolicy, event: AuditEvent) -> bool:
        key = (policy.org_id, policy.hotel_id)
        with self._lock:
            stored = self._rows.get(key)
            stored_version = -1 if stored is None else stored.version
            if policy.version != stored_version + 1:
                return False
            self.audit.record_now(event)
            self._rows[key] = policy
            return True
