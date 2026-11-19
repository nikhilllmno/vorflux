"""Audit events for approval requests and policy changes.

The real append-only `audit_events` table comes with DEX-24. Until then, the service writes to an
`AuditSink`. In Postgres, the state change and its audit event go in one transaction.
"""

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Protocol
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class AuditEvent:
    org_id: UUID
    hotel_id: UUID
    actor_id: UUID | None  # None = the system (for example, the expiry sweep)
    action: str  # for example "approval.approved"
    subject_type: str  # "approval_request" or "approval_policy"
    subject_id: str
    at: datetime
    details: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    id: UUID = field(default_factory=uuid4)


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._events)
