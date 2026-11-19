"""Persistence interfaces for approvals and hotel policies, with in-memory implementations.

Every read and write is scoped by `org_id` and `hotel_id`. A lookup with the wrong tenant finds
nothing, which the API reports as 404. The Postgres implementation comes after the tenancy schema
(PR #5) merges; docs/approvals.md has the proposed tables.

Every write takes the audit event of that change. A store writes the state and the event
together or not at all (in Postgres: one transaction). A failed audit write leaves no state
change, so no approval can be used without its audit event.
"""

import threading
from typing import Protocol
from uuid import UUID

from harness.approvals.audit import AuditEvent, AuditSink, InMemoryAuditLog
from harness.approvals.model import ApprovalRequest, ApprovalStatus
from harness.approvals.policy import HotelPolicy


class ApprovalStore(Protocol):
    def add(self, request: ApprovalRequest, event: AuditEvent) -> None:
        """Insert a new request and its audit event. Raises ValueError if the id exists."""
        ...

    def get(self, org_id: UUID, hotel_id: UUID, approval_id: UUID) -> ApprovalRequest | None: ...

    def list(
        self, org_id: UUID, hotel_id: UUID, *, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]:
        """Requests of one hotel, oldest first."""
        ...

    def replace(self, current: ApprovalRequest, new: ApprovalRequest, event: AuditEvent) -> bool:
        """Compare-and-set: store `new` and `event` only if the row still has `current.version`.

        `new.version` must be `current.version + 1`. Returns False when another writer won.
        In Postgres: `UPDATE ... WHERE id = $1 AND org_id = $2 AND hotel_id = $3 AND version = $4`.
        """
        ...


class PolicyStore(Protocol):
    def get(self, org_id: UUID, hotel_id: UUID) -> HotelPolicy | None: ...

    def put(self, policy: HotelPolicy, event: AuditEvent) -> bool:
        """Store `policy` and `event` if the stored version is `policy.version - 1`.

        Version 0 inserts and fails when a row exists. Returns False when another writer won.
        """
        ...


class InMemoryApprovalStore:
    """Thread-safe store for tests and local development. Not for production.

    The audit event is written first, under the same lock; if it raises, the state is unchanged.
    """

    def __init__(self, audit: AuditSink | None = None) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[UUID, UUID, UUID], ApprovalRequest] = {}
        self.audit: AuditSink = audit if audit is not None else InMemoryAuditLog()

    def add(self, request: ApprovalRequest, event: AuditEvent) -> None:
        key = (request.org_id, request.hotel_id, request.id)
        with self._lock:
            if any(row_id == request.id for (_, _, row_id) in self._rows):
                raise ValueError(f"approval already exists: {request.id}")
            self.audit.record(event)
            self._rows[key] = request

    def get(self, org_id: UUID, hotel_id: UUID, approval_id: UUID) -> ApprovalRequest | None:
        with self._lock:
            return self._rows.get((org_id, hotel_id, approval_id))

    def list(
        self, org_id: UUID, hotel_id: UUID, *, status: ApprovalStatus | None = None
    ) -> list[ApprovalRequest]:
        with self._lock:
            rows = [
                row
                for (row_org, row_hotel, _), row in self._rows.items()
                if row_org == org_id
                and row_hotel == hotel_id
                and (status is None or row.status is status)
            ]
        return sorted(rows, key=lambda row: row.created_at)

    def replace(self, current: ApprovalRequest, new: ApprovalRequest, event: AuditEvent) -> bool:
        key = (current.org_id, current.hotel_id, current.id)
        if (new.org_id, new.hotel_id, new.id) != key or new.version != current.version + 1:
            raise ValueError("replace must keep the id and tenant and bump the version by one")
        with self._lock:
            stored = self._rows.get(key)
            if stored is None or stored.version != current.version:
                return False
            self.audit.record(event)
            self._rows[key] = new
            return True


class InMemoryPolicyStore:
    """Thread-safe store for tests and local development. Not for production."""

    def __init__(self, audit: AuditSink | None = None) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[UUID, UUID], HotelPolicy] = {}
        self.audit: AuditSink = audit if audit is not None else InMemoryAuditLog()

    def get(self, org_id: UUID, hotel_id: UUID) -> HotelPolicy | None:
        with self._lock:
            return self._rows.get((org_id, hotel_id))

    def put(self, policy: HotelPolicy, event: AuditEvent) -> bool:
        key = (policy.org_id, policy.hotel_id)
        with self._lock:
            stored = self._rows.get(key)
            stored_version = -1 if stored is None else stored.version
            if policy.version != stored_version + 1:
                return False
            self.audit.record(event)
            self._rows[key] = policy
            return True
