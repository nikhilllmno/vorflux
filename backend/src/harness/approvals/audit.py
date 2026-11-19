"""Audit events for approvals, policy changes, denials, and refused use.

The real append-only `audit_events` table comes with DEX-24. State changes reach the audit log
through the store, in the same write as the change (see store.py). Events without a state change
(a denied tool call, a refused use) go straight to an `AuditSink`.
"""

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from uuid import UUID, uuid4


class ActorType(StrEnum):
    USER = "user"  # a person; actor_id is the user id
    RUN = "run"  # an agent run; actor_id is the run id
    SYSTEM = "system"  # for example the expiry sweep; actor_id is None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    org_id: UUID
    hotel_id: UUID
    actor_type: ActorType
    actor_id: str | None
    action: str  # for example "approval.approved" or "policy.denied"
    subject_type: str  # "approval_request", "approval_policy", or "tool_call"
    subject_id: str
    at: datetime
    details: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_type", ActorType(self.actor_type))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


class AuditSink(Protocol):
    async def record(self, event: AuditEvent) -> None: ...


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[AuditEvent] = []

    def record_now(self, event: AuditEvent) -> None:
        """Synchronous write, for stores that hold a lock across the state change."""
        with self._lock:
            self._events.append(event)

    async def record(self, event: AuditEvent) -> None:
        self.record_now(event)

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._events)
