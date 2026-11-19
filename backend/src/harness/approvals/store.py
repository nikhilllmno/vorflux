"""Persistence interfaces for approvals and hotel policies, with in-memory implementations.

Every read and write is scoped by `org_id` and `hotel_id`. A lookup with the wrong tenant finds
nothing, which the API reports as 404. The Postgres implementation comes after the tenancy schema
(PR #5) merges; docs/approvals.md has the proposed tables.

Contract for every implementation (tests/test_approvals_store.py checks it):

- Every write takes the audit event of that change. The store writes the state and the event
  together or not at all (in Postgres: one transaction). A failed audit write leaves no state
  change, so no approval can be used without its audit event.
- Updates are compare-and-set on `version`.
- A guarded update (`TransitionGuard`) also checks, in the same atomic step as the write, that the
  hotel policy still has the version the caller assessed and that a deadline has not passed by
  the store's own clock. A policy write and a guarded approval write of one hotel never overlap.
  This keeps the time-of-use checks valid at the moment of the write (docs/approvals.md,
  "Atomic transitions").
- One tool call has at most one approval request: `add` raises `DuplicateToolCallError` for a
  second request with the same (org_id, hotel_id, run_id, tool_call_id).
- Values read back may be plain strings; the model types parse them (see model.py).
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol
from uuid import UUID

from harness.approvals.audit import AuditEvent, InMemoryAuditLog
from harness.approvals.model import ApprovalRequest, ApprovalStatus
from harness.approvals.policy import NO_STORED_POLICY, HotelPolicy


class DuplicateToolCallError(ValueError):
    """The tool call already has an approval request (unique key in Postgres)."""


@dataclass(frozen=True, slots=True)
class TransitionGuard:
    """Conditions that the store checks atomically with an approval write.

    - `policy_version`: the stored policy version that the caller assessed, or
      `NO_STORED_POLICY`. The write fails if the hotel policy changed since.
    - `deadline`: the write fails if the store's clock is at or after it.
    """

    policy_version: int
    deadline: datetime | None = None


class WriteResult(Enum):
    APPLIED = "applied"
    STALE = "stale"  # another writer changed the row first
    POLICY_CHANGED = "policy_changed"
    DEADLINE_PASSED = "deadline_passed"

    def __bool__(self) -> bool:
        raise TypeError("compare a WriteResult with WriteResult.APPLIED")


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
        self,
        current: ApprovalRequest,
        new: ApprovalRequest,
        event: AuditEvent,
        guard: TransitionGuard | None = None,
    ) -> WriteResult:
        """Compare-and-set: store `new` and `event` only if the row still has `current.version`.

        `new.version` must be `current.version + 1`. With a `guard`, the policy version and the
        deadline are checked in the same atomic step (see `TransitionGuard`).
        In Postgres: one transaction that takes the hotel's shared policy lock, then
        `UPDATE ... WHERE id = $1 AND org_id = $2 AND hotel_id = $3 AND version = $4` plus the
        guard conditions (docs/approvals.md, "Atomic transitions").
        """
        ...


class PolicyStore(Protocol):
    async def get(self, org_id: UUID, hotel_id: UUID) -> HotelPolicy | None: ...

    async def put(self, policy: HotelPolicy, event: AuditEvent) -> bool:
        """Store `policy` and `event` if the stored version is `policy.version - 1`.

        Version 0 inserts and fails when a row exists. Returns False when another writer won.
        In Postgres the transaction takes the hotel's exclusive policy lock first.
        """
        ...


class SyncAuditLog(Protocol):
    def record_now(self, event: AuditEvent) -> None: ...


class SharedPolicyVersions(Protocol):
    """An in-memory policy store that an in-memory approval store shares its lock with."""

    @property
    def lock(self) -> threading.Lock: ...

    def stored_version_locked(self, org_id: UUID, hotel_id: UUID) -> int:
        """The stored policy version or NO_STORED_POLICY. The caller holds `lock`."""
        ...


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def guard_failure(
    guard: TransitionGuard | None, policy_version: int, now: datetime
) -> WriteResult | None:
    """Why `guard` blocks a write now, or None. The caller holds the lock."""
    if guard is None:
        return None
    if policy_version != guard.policy_version:
        return WriteResult.POLICY_CHANGED
    if guard.deadline is not None and now >= guard.deadline:
        return WriteResult.DEADLINE_PASSED
    return None


class InMemoryApprovalStore:
    """Thread-safe store for tests and local development. Not for production.

    It shares the lock of `policies`, so a guarded write and a policy write never overlap.
    `clock` is the store's own clock for guard deadlines.
    The audit event is written first, under the same lock; if it raises, the state is unchanged.
    """

    def __init__(
        self,
        policies: SharedPolicyVersions,
        audit: SyncAuditLog | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._policies = policies
        self._lock = policies.lock
        self._clock = clock
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
        self,
        current: ApprovalRequest,
        new: ApprovalRequest,
        event: AuditEvent,
        guard: TransitionGuard | None = None,
    ) -> WriteResult:
        key = (current.org_id, current.hotel_id, current.id)
        if (new.org_id, new.hotel_id, new.id) != key or new.version != current.version + 1:
            raise ValueError("replace must keep the id and tenant and bump the version by one")
        if _call_key(new) != _call_key(current):
            raise ValueError("replace must keep the run and the tool call")
        with self._lock:
            stored = self._rows.get(key)
            if stored is None or stored.version != current.version:
                return WriteResult.STALE
            policy_version = self._policies.stored_version_locked(key[0], key[1])
            failure = guard_failure(guard, policy_version, self._clock())
            if failure is not None:
                return failure
            self.audit.record_now(event)
            self._rows[key] = new
            return WriteResult.APPLIED


def _call_key(row: ApprovalRequest) -> tuple[UUID, UUID, str, str]:
    return (row.org_id, row.hotel_id, row.run_id, row.tool_call_id)


class InMemoryPolicyStore:
    """Thread-safe store for tests and local development. Not for production."""

    def __init__(self, audit: SyncAuditLog | None = None) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[UUID, UUID], HotelPolicy] = {}
        self.audit: SyncAuditLog = audit if audit is not None else InMemoryAuditLog()

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def stored_version_locked(self, org_id: UUID, hotel_id: UUID) -> int:
        stored = self._rows.get((org_id, hotel_id))
        return NO_STORED_POLICY if stored is None else stored.version

    async def get(self, org_id: UUID, hotel_id: UUID) -> HotelPolicy | None:
        with self._lock:
            return self._rows.get((org_id, hotel_id))

    async def put(self, policy: HotelPolicy, event: AuditEvent) -> bool:
        key = (policy.org_id, policy.hotel_id)
        with self._lock:
            stored = self._rows.get(key)
            stored_version = NO_STORED_POLICY if stored is None else stored.version
            if policy.version != stored_version + 1:
                return False
            self.audit.record_now(event)
            self._rows[key] = policy
            return True
