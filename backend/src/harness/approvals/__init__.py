"""Approval policy engine and approval service (DEX-20, Hermes parity HP-21).

- `model`: actions, roles, decisions, approval requests (immutable values).
- `policy`: `PolicyEngine.evaluate` -> allow, require approval, or deny. Fails closed.
- `interface`: `ApprovalPolicy`, the small interface that runtime adapters call.
- `service`: `ApprovalService`, which implements `ApprovalPolicy` and the staff operations.
- `store`, `audit`: persistence and audit interfaces with in-memory implementations.
- `api`: FastAPI routes to list, approve, and reject, and to set approver roles.

Design: docs/approvals.md and docs/architecture.md (D19, Sections 4.2, 5.2, 7.1, 10.10).
"""

from harness.approvals.audit import AuditEvent, AuditSink, InMemoryAuditLog
from harness.approvals.interface import ApprovalPolicy
from harness.approvals.model import (
    Action,
    ActionClass,
    ActionKind,
    Allow,
    ApprovalCheck,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalStatus,
    ApproverRole,
    Decision,
    Deny,
    HotelRole,
    Membership,
    Principal,
    RequireApproval,
    RiskLevel,
    TeamRole,
    payload_hash,
)
from harness.approvals.policy import Effect, HotelPolicy, PolicyEngine, Rule
from harness.approvals.service import (
    ActionDeniedError,
    ApprovalError,
    ApprovalNotRequiredError,
    ApprovalService,
    ConflictError,
    ForbiddenError,
    InvalidPolicyError,
    NotFoundError,
    RunResumer,
)
from harness.approvals.store import (
    ApprovalStore,
    InMemoryApprovalStore,
    InMemoryPolicyStore,
    PolicyStore,
)

__all__ = [
    "Action",
    "ActionClass",
    "ActionDeniedError",
    "ActionKind",
    "Allow",
    "ApprovalCheck",
    "ApprovalError",
    "ApprovalNotRequiredError",
    "ApprovalPolicy",
    "ApprovalRequest",
    "ApprovalResolution",
    "ApprovalService",
    "ApprovalStatus",
    "ApprovalStore",
    "ApproverRole",
    "AuditEvent",
    "AuditSink",
    "ConflictError",
    "Decision",
    "Deny",
    "Effect",
    "ForbiddenError",
    "HotelPolicy",
    "HotelRole",
    "InMemoryApprovalStore",
    "InMemoryAuditLog",
    "InMemoryPolicyStore",
    "InvalidPolicyError",
    "Membership",
    "NotFoundError",
    "PolicyEngine",
    "PolicyStore",
    "Principal",
    "RequireApproval",
    "RiskLevel",
    "Rule",
    "RunResumer",
    "TeamRole",
    "payload_hash",
]
