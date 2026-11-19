"""Approval service: request, decide, cancel, expire, check, and consume approvals.

It implements `ApprovalPolicy` (the adapter interface) and the staff-facing operations behind the
API. Every state change writes an audit event, and every exit from `pending` notifies the
`RunResumer` so that the paused run continues (docs/architecture.md Section 4.1).
"""

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from harness.approvals.audit import AuditEvent, AuditSink
from harness.approvals.model import (
    Action,
    ActionKind,
    Allow,
    ApprovalCheck,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalStatus,
    Decision,
    Deny,
    HotelRole,
    Principal,
    RiskLevel,
    TeamRole,
    payload_hash,
)
from harness.approvals.policy import HotelPolicy, PolicyEngine, validate_approver_min_role
from harness.approvals.store import ApprovalStore, PolicyStore
from harness.tenancy import TenantContext

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
MAX_SUMMARY_LENGTH = 2000
MAX_REASON_LENGTH = 1000


class ApprovalError(Exception):
    """Base class. The API maps each subclass to one HTTP status."""


class NotFoundError(ApprovalError):
    """Unknown id, or the principal cannot see this hotel (404, never 403, Section 7.4)."""


class ForbiddenError(ApprovalError):
    """The principal can see the request but lacks the role to act on it."""


class ConflictError(ApprovalError):
    """The request is no longer pending, or another writer changed it first."""


class InvalidPolicyError(ApprovalError, ValueError):
    """A policy setting that the design does not allow."""


class ActionDeniedError(ApprovalError):
    """The policy denies the action, so no approval can be requested."""


class ApprovalNotRequiredError(ApprovalError):
    """The policy allows the action without an approval."""


class RunResumer(Protocol):
    """Implemented by the run orchestrator (DEX-16). Must be idempotent per approval id."""

    def resume(self, resolution: ApprovalResolution) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ApprovalService:
    def __init__(
        self,
        engine: PolicyEngine,
        approvals: ApprovalStore,
        policies: PolicyStore,
        audit: AuditSink,
        resumer: RunResumer | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._engine = engine
        self._approvals = approvals
        self._policies = policies
        self._audit = audit
        self._resumer = resumer
        self._clock = clock

    # --- ApprovalPolicy (adapter interface) -------------------------------------------------

    def evaluate(self, action: Action) -> Decision:
        return self._engine.evaluate(action)

    def request_approval(self, action: Action, *, call_id: str, summary: str) -> ApprovalRequest:
        decision = self.evaluate(action)
        if isinstance(decision, Deny):
            raise ActionDeniedError(decision.reason)
        if isinstance(decision, Allow):
            raise ApprovalNotRequiredError(decision.reason)
        if not call_id:
            raise ValueError("call_id is required")
        summary = summary.strip()
        if not summary or len(summary) > MAX_SUMMARY_LENGTH:
            raise ValueError(f"summary must have 1 to {MAX_SUMMARY_LENGTH} characters")

        policy = self._engine.hotel_policy(action.org_id, action.hotel_id)
        now = self._clock()
        request = ApprovalRequest(
            id=uuid4(),
            org_id=action.org_id,
            hotel_id=action.hotel_id,
            run_id=action.run_id,
            call_id=call_id,
            requester_id=action.requester_id,
            toolset=action.toolset,
            tool=action.tool,
            kind=ActionKind(action.kind),
            risk=RiskLevel(action.risk),
            payload_hash=payload_hash(action),
            summary=summary,
            approver_roles=decision.approver_roles,
            status=ApprovalStatus.PENDING,
            created_at=now,
            expires_at=now + policy.approval_ttl,
        )
        self._approvals.add(request)
        self._record(request, action.requester_id, "approval.requested", now)
        return request

    def check(self, approval_id: UUID, action: Action) -> ApprovalCheck:
        try:
            request = self._approvals.get(action.org_id, action.hotel_id, approval_id)
            failure = self._binding_failure(request, action, self._clock())
            if request is None or failure is not None:
                return ApprovalCheck(False, failure or "approval not found", _status(request))
            return ApprovalCheck(True, "approval is valid", request.status)
        except Exception:
            logger.exception("approval check failed; treating it as not approved")
            return ApprovalCheck(False, "approval check failed")

    def consume(self, approval_id: UUID, action: Action) -> ApprovalCheck:
        try:
            now = self._clock()
            request = self._approvals.get(action.org_id, action.hotel_id, approval_id)
            failure = self._binding_failure(request, action, now)
            if request is None or failure is not None:
                return ApprovalCheck(False, failure or "approval not found", _status(request))
            used = replace(request, consumed_at=now, version=request.version + 1)
            if not self._approvals.replace(request, used):
                return ApprovalCheck(False, "approval was already used", request.status)
            self._record(used, action.requester_id, "approval.consumed", now)
            return ApprovalCheck(True, "approval used", used.status)
        except Exception:
            logger.exception("approval consume failed; treating it as not approved")
            return ApprovalCheck(False, "approval consume failed")

    # --- Staff-facing operations (API) ------------------------------------------------------

    def list_for(
        self,
        principal: Principal,
        org_id: UUID,
        hotel_id: UUID,
        status: ApprovalStatus | None = None,
    ) -> list[ApprovalRequest]:
        if not self._can_view_hotel(principal, org_id, hotel_id):
            raise NotFoundError("hotel not found")
        self.expire_due(org_id, hotel_id)
        rows = self._approvals.list(org_id, hotel_id, status=status)
        return [row for row in rows if self._can_view(principal, row)]

    def get_for(
        self, principal: Principal, org_id: UUID, hotel_id: UUID, approval_id: UUID
    ) -> ApprovalRequest:
        request = self._approvals.get(org_id, hotel_id, approval_id)
        if request is None or not self._can_view(principal, request):
            raise NotFoundError("approval not found")
        return self._expire_if_due(request, self._clock())

    def approve(
        self,
        principal: Principal,
        org_id: UUID,
        hotel_id: UUID,
        approval_id: UUID,
        reason: str | None = None,
    ) -> ApprovalRequest:
        return self._decide(principal, org_id, hotel_id, approval_id, True, reason)

    def reject(
        self,
        principal: Principal,
        org_id: UUID,
        hotel_id: UUID,
        approval_id: UUID,
        reason: str | None = None,
    ) -> ApprovalRequest:
        return self._decide(principal, org_id, hotel_id, approval_id, False, reason)

    def cancel(
        self, tenant: TenantContext, approval_id: UUID, reason: str | None = None
    ) -> ApprovalRequest:
        """Cancel a pending request for the run's requester (for example, the run was cancelled)."""
        request = self._approvals.get(tenant.org_id, tenant.hotel_id, approval_id)
        if request is None or request.requester_id != tenant.actor_id:
            raise NotFoundError("approval not found")
        now = self._clock()
        request = self._expire_if_due(request, now)
        if request.status is not ApprovalStatus.PENDING:
            raise ConflictError(f"approval is {request.status}")
        cancelled = replace(
            request,
            status=ApprovalStatus.CANCELLED,
            decided_at=now,
            reason=_clean_reason(reason),
            version=request.version + 1,
        )
        if not self._approvals.replace(request, cancelled):
            raise ConflictError("approval changed; reload it")
        self._record(cancelled, tenant.actor_id, "approval.cancelled", now)
        self._notify(cancelled)
        return cancelled

    def expire_due(self, org_id: UUID, hotel_id: UUID) -> list[ApprovalRequest]:
        """Expire every pending request of one hotel whose deadline has passed."""
        now = self._clock()
        expired = []
        for request in self._approvals.list(org_id, hotel_id, status=ApprovalStatus.PENDING):
            updated = self._expire_if_due(request, now)
            if updated.status is ApprovalStatus.EXPIRED:
                expired.append(updated)
        return expired

    def set_approver_min_role(
        self,
        principal: Principal,
        org_id: UUID,
        hotel_id: UUID,
        kind: ActionKind,
        min_role: HotelRole,
    ) -> HotelPolicy:
        """A hotel `admin` or `owner` sets which roles may approve one action kind."""
        roles = principal.hotel_roles(org_id, hotel_id)
        if not roles:
            raise NotFoundError("hotel not found")
        if not any(role.at_least(HotelRole.ADMIN) for role in roles):
            raise ForbiddenError("only a hotel admin or owner can change approver roles")
        try:
            validate_approver_min_role(kind, min_role)
        except ValueError as error:
            raise InvalidPolicyError(str(error)) from None

        current = self._engine.hotel_policy(org_id, hotel_id)
        stored = self._policies.get(org_id, hotel_id)
        updated = replace(
            current,
            approver_min_roles={**current.approver_min_roles, kind: min_role},
            version=0 if stored is None else current.version + 1,
        )
        if not self._policies.put(updated):
            raise ConflictError("policy changed; reload it")
        self._audit.record(
            AuditEvent(
                org_id=org_id,
                hotel_id=hotel_id,
                actor_id=principal.user_id,
                action="approval_policy.updated",
                subject_type="approval_policy",
                subject_id=f"{hotel_id}:{kind}",
                at=self._clock(),
                details={"kind": str(kind), "min_role": str(min_role)},
            )
        )
        return updated

    # --- Internals ---------------------------------------------------------------------------

    def _decide(
        self,
        principal: Principal,
        org_id: UUID,
        hotel_id: UUID,
        approval_id: UUID,
        approve: bool,
        reason: str | None,
    ) -> ApprovalRequest:
        request = self._approvals.get(org_id, hotel_id, approval_id)
        if request is None or not self._can_view(principal, request):
            raise NotFoundError("approval not found")
        now = self._clock()
        request = self._expire_if_due(request, now)
        if request.status is not ApprovalStatus.PENDING:
            raise ConflictError(f"approval is {request.status}")
        if not self._can_decide(principal, request):
            raise ForbiddenError("you do not have a role that can decide this approval")
        if approve and self._self_approval_blocked(principal, request):
            raise ForbiddenError("the requester cannot approve their own high-risk action")

        status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        decided = replace(
            request,
            status=status,
            decided_by=principal.user_id,
            decided_at=now,
            reason=_clean_reason(reason),
            version=request.version + 1,
        )
        if not self._approvals.replace(request, decided):
            raise ConflictError("approval changed; reload it")
        self._record(decided, principal.user_id, f"approval.{status}", now)
        self._notify(decided)
        return decided

    def _binding_failure(
        self, request: ApprovalRequest | None, action: Action, now: datetime
    ) -> str | None:
        """Why this approval cannot authorise this exact action now, or None if it can."""
        if request is None:
            return "approval not found"
        if request.status is not ApprovalStatus.APPROVED:
            return "approval expired" if _is_due(request, now) else f"approval is {request.status}"
        if request.consumed_at is not None:
            return "approval was already used"
        if now >= request.expires_at:
            return "approval expired"
        if request.run_id != action.run_id or request.requester_id != action.requester_id:
            return "approval belongs to another run"
        if payload_hash(action) != request.payload_hash:
            return "action changed after approval; a new approval is needed"
        return None

    def _expire_if_due(self, request: ApprovalRequest, now: datetime) -> ApprovalRequest:
        if not _is_due(request, now):
            return request
        expired = replace(
            request, status=ApprovalStatus.EXPIRED, decided_at=now, version=request.version + 1
        )
        if not self._approvals.replace(request, expired):
            current = self._approvals.get(request.org_id, request.hotel_id, request.id)
            return current or request
        self._record(expired, None, "approval.expired", now)
        self._notify(expired)
        return expired

    def _can_view_hotel(self, principal: Principal, org_id: UUID, hotel_id: UUID) -> bool:
        return principal.is_member(org_id, hotel_id) or self._is_code_reviewer(principal)

    def _can_view(self, principal: Principal, request: ApprovalRequest) -> bool:
        if principal.is_member(request.org_id, request.hotel_id):
            return True
        return request.is_code and self._is_code_reviewer(principal)

    def _can_decide(self, principal: Principal, request: ApprovalRequest) -> bool:
        if request.is_code:
            return self._is_code_reviewer(principal)  # hotel roles never approve code
        roles = principal.hotel_roles(request.org_id, request.hotel_id)
        return bool(roles & request.approver_roles)

    def _self_approval_blocked(self, principal: Principal, request: ApprovalRequest) -> bool:
        if principal.user_id != request.requester_id or request.risk is not RiskLevel.HIGH:
            return False
        policy = self._engine.hotel_policy(request.org_id, request.hotel_id)
        return policy.forbid_self_approval_of_high_risk

    @staticmethod
    def _is_code_reviewer(principal: Principal) -> bool:
        return TeamRole.CODE_REVIEWER in principal.team_roles

    def _record(
        self, request: ApprovalRequest, actor_id: UUID | None, action: str, at: datetime
    ) -> None:
        details = {
            "kind": str(request.kind),
            "risk": str(request.risk),
            "status": str(request.status),
            "run_id": request.run_id,
            "call_id": request.call_id,
            "payload_hash": request.payload_hash,
        }
        if request.reason:
            details["reason"] = request.reason
        self._audit.record(
            AuditEvent(
                org_id=request.org_id,
                hotel_id=request.hotel_id,
                actor_id=actor_id,
                action=action,
                subject_type="approval_request",
                subject_id=str(request.id),
                at=at,
                details=details,
            )
        )

    def _notify(self, request: ApprovalRequest) -> None:
        if self._resumer is None:
            return
        resolution = ApprovalResolution(
            approval_id=request.id,
            org_id=request.org_id,
            hotel_id=request.hotel_id,
            run_id=request.run_id,
            call_id=request.call_id,
            outcome=request.status,
            reason=request.reason,
        )
        try:
            self._resumer.resume(resolution)
        except Exception:
            # The decision is stored; the orchestrator can resume from the stored state.
            logger.exception("run resume failed for approval %s", request.id)


def _is_due(request: ApprovalRequest, now: datetime) -> bool:
    return request.status is ApprovalStatus.PENDING and now >= request.expires_at


def _status(request: ApprovalRequest | None) -> ApprovalStatus | None:
    return None if request is None else request.status


def _clean_reason(reason: str | None) -> str | None:
    if reason is None or not reason.strip():
        return None
    reason = reason.strip()
    if len(reason) > MAX_REASON_LENGTH:
        raise ValueError(f"reason must have at most {MAX_REASON_LENGTH} characters")
    return reason
