"""Approval policy engine and approval service (DEX-20, Hermes parity HP-21).

- `model`: tool calls, roles, decisions, approval requests, previews (immutable values).
- `catalog`: the tool registry; the only source of an action's kind and risk.
- `preview`: typed proposals per action kind and the preview the harness renders from them.
- `policy`: `PolicyEngine` -> allow, ask, or deny. Fails closed.
- `interface`: `ApprovalPolicy`, the small async interface that runtime adapters call.
- `service`: `ApprovalService`, which implements `ApprovalPolicy` and the staff operations.
- `store`, `audit`: persistence and audit interfaces with in-memory implementations.
- `api`: FastAPI routes to list, approve, and reject, and to set approver roles.

Design: docs/approvals.md and docs/architecture.md (D19, Sections 4.2, 5.2, 7.1, 10.10).
"""

from harness.approvals.audit import ActorType, AuditEvent, AuditSink, InMemoryAuditLog
from harness.approvals.catalog import ToolCatalog, ToolSpec
from harness.approvals.interface import ApprovalPolicy
from harness.approvals.model import (
    ActionClass,
    ActionKind,
    ApprovalCheck,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalStatus,
    ApproverRole,
    Decision,
    HotelRole,
    Membership,
    Preview,
    PreviewField,
    Principal,
    PrincipalKind,
    ProposedCall,
    RiskLevel,
    TeamRole,
    Verdict,
    canonical_json,
    proposal_hash,
)
from harness.approvals.policy import (
    NO_STORED_POLICY,
    Effect,
    HotelPolicy,
    PolicyEngine,
    Rule,
)
from harness.approvals.preview import PROPOSAL_TYPES, render_preview
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
    DuplicateToolCallError,
    InMemoryApprovalStore,
    InMemoryPolicyStore,
    PolicyStore,
    TransitionGuard,
    WriteResult,
)

__all__ = [
    "NO_STORED_POLICY",
    "PROPOSAL_TYPES",
    "ActionClass",
    "ActionDeniedError",
    "ActionKind",
    "ActorType",
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
    "DuplicateToolCallError",
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
    "Preview",
    "PreviewField",
    "Principal",
    "PrincipalKind",
    "ProposedCall",
    "RiskLevel",
    "Rule",
    "RunResumer",
    "TeamRole",
    "ToolCatalog",
    "ToolSpec",
    "TransitionGuard",
    "Verdict",
    "WriteResult",
    "canonical_json",
    "proposal_hash",
    "render_preview",
]
