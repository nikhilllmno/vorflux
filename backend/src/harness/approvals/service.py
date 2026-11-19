"""Approval service: evaluate, request, decide, cancel, expire, check, and consume approvals.

It implements `ApprovalPolicy` (the adapter interface) and the staff-facing operations behind the
API.

- Every state change goes to the store together with its audit event (one transaction).
- A decision and a use pass a `TransitionGuard` to the store: the write fails if the hotel
  policy changed after the service read it, or if the deadline passed while the service waited
  for I/O. The service then reads again and re-checks (at most `MAX_TRANSITION_ATTEMPTS` times).
- `consume` is idempotent per tool call: a repeat for the same approval and the same tool call
  returns the same arguments and idempotency key (`replayed=True`), so a caller that was
  cancelled after the store write can retry. The executor dedupes on the idempotency key.
- A denied tool call and a refused use write an audit event too (best effort; the denial or
  refusal stands if the audit write fails).
- Every exit from `pending` notifies the `RunResumer`, so the paused run continues
  (docs/architecture.md Section 4.1). If a notification fails, the orchestrator reads the stored
  outcome with `resolution()`.
- Staff-facing operations take the caller's `Principal` (for role checks) and an explicit
  `TenantContext` (for storage scope). The context's `actor_id` must be the principal.
  Only a human principal can approve or reject.
- `list_for` and `get_for` return an `ApprovalView`. Only a principal who can decide the request
  sees its sensitive preview values, its arguments, and the agent's note.
"""

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from harness.approvals.audit import ActorType, AuditEvent, AuditSink
from harness.approvals.model import (
    ActionKind,
    ApprovalCheck,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalStatus,
    ApprovalView,
    ApproverRole,
    Decision,
    HotelRole,
    Principal,
    ProposedCall,
    RiskLevel,
    TeamRole,
    Verdict,
)
from harness.approvals.policy import (
    Assessment,
    HotelPolicy,
    PolicyEngine,
    approver_roles_for,
    validate_approver_min_role,
)
from harness.approvals.store import (
    ApprovalStore,
    DuplicateToolCallError,
    PolicyStore,
    TransitionGuard,
    WriteResult,
)
from harness.tenancy import TenantContext

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
MAX_NOTE_LENGTH = 2000
MAX_REASON_LENGTH = 1000
MAX_TRANSITION_ATTEMPTS = 3


class ApprovalError(Exception):
    """Base class. The API maps each subclass to one HTTP status."""


class NotFoundError(ApprovalError):
    """Unknown id, or the principal cannot see this hotel (404, never 403, Section 7.4)."""


class ForbiddenError(ApprovalError):
    """The principal can see the request but cannot act on it."""


class ConflictError(ApprovalError):
    """The request is no longer pending, another writer changed it first, or the tool call
    already has an approval for a different proposal."""


class InvalidPolicyError(ApprovalError, ValueError):
    """A policy setting that the design does not allow."""


class ActionDeniedError(ApprovalError):
    """The policy denies the action, so no approval can be requested."""


class ApprovalNotRequiredError(ApprovalError):
    """The policy allows the action without an approval."""


class RunResumer(Protocol):
    """Implemented by the run orchestrator (DEX-16). Must be idempotent per approval id."""

    async def resume(self, resolution: ApprovalResolution) -> None: ...


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

    async def evaluate(self, call: ProposedCall) -> Decision:
        decision = (await self._engine.assess(call)).decision
        if decision.verdict == Verdict.DENY:
            await self._record_tool_call_event(call, "policy.denied", decision.reason)
        return decision

    async def request(self, call: ProposedCall, *, note: str | None = None) -> ApprovalRequest:
        assessment = await self._engine.assess(call)
        decision = assessment.decision
        if decision.verdict == Verdict.DENY:
            await self._record_tool_call_event(call, "policy.denied", decision.reason)
            raise ActionDeniedError(decision.reason)
        if decision.verdict == Verdict.ALLOW:
            raise ApprovalNotRequiredError(decision.reason)
        spec, preview, bound = assessment.spec, assessment.preview, assessment.proposal_hash
        if spec is None or preview is None or bound is None:  # an ask always carries all three
            raise ActionDeniedError("policy evaluation failed")
        note = _clean_text(note, MAX_NOTE_LENGTH, "note")

        existing = await self._approvals.get_by_tool_call(
            call.org_id, call.hotel_id, call.run_id, call.tool_call_id
        )
        if existing is not None:
            return await self._reuse(existing, bound)

        policy = await self._engine.hotel_policy(call.org_id, call.hotel_id)
        now = self._clock()
        request = ApprovalRequest(
            id=uuid4(),
            org_id=call.org_id,
            hotel_id=call.hotel_id,
            run_id=call.run_id,
            tool_call_id=call.tool_call_id,
            requester_id=call.requester_id,
            toolset=call.toolset,
            tool=call.tool,
            kind=spec.kind,
            risk=spec.risk,
            args=call.args,
            preview=preview,
            proposal_hash=bound,
            approver_roles=decision.approver_roles,
            status=ApprovalStatus.PENDING,
            created_at=now,
            expires_at=now + policy.approval_ttl,
            note=note,
        )
        event = _event(request, ActorType.RUN, request.run_id, "approval.requested", now)
        try:
            await self._approvals.add(request, event)
        except DuplicateToolCallError:
            # Another request for this tool call won the race; reuse it if it is the same proposal.
            existing = await self._approvals.get_by_tool_call(
                call.org_id, call.hotel_id, call.run_id, call.tool_call_id
            )
            if existing is None:
                raise ConflictError("tool call already has an approval") from None
            return await self._reuse(existing, bound)
        return request

    async def check(self, approval_id: UUID, call: ProposedCall) -> ApprovalCheck:
        try:
            request, failure, _ = await self._verify(approval_id, call, self._clock())
            if request is None or failure is not None:
                return ApprovalCheck(False, failure or "approval not found", _status(request))
            return ApprovalCheck(True, "approval is valid", request.status)
        except Exception:
            logger.exception("approval check failed; treating it as not approved")
            return ApprovalCheck(False, "approval check failed")

    async def consume(self, approval_id: UUID, call: ProposedCall) -> ApprovalCheck:
        try:
            request: ApprovalRequest | None = None
            reason = "approval changed while it was being used; try again"
            for _ in range(MAX_TRANSITION_ATTEMPTS):
                request, failure, assessment = await self._verify(
                    approval_id, call, self._clock(), allow_used=True
                )
                if failure is not None or request is None:
                    reason = failure or "approval not found"
                    break
                if assessment is None or assessment.policy_version is None:
                    reason = "policy evaluation failed"
                    break
                if request.consumed_at is not None:
                    # This tool call already used the approval (for example, the first call
                    # was cancelled after the store write). Return the same grant; nothing is
                    # written, so the executor's idempotency key prevents a second write.
                    await self._record_tool_call_event(
                        call,
                        "approval.use_replayed",
                        "the same tool call used this approval before",
                        approval_id=approval_id,
                    )
                    return _grant(request, "approval already used by this tool call", True)
                # The store checks the policy version and `use_by` again, atomically with the
                # write, so a change while this call awaited I/O cannot slip through.
                now = self._clock()
                used = replace(request, consumed_at=now, version=request.version + 1)
                event = _event(used, ActorType.RUN, used.run_id, "approval.consumed", now)
                guard = TransitionGuard(assessment.policy_version, deadline=request.use_by)
                result = await self._approvals.replace(request, used, event, guard)
                if result == WriteResult.APPLIED:
                    return _grant(used, "approval used", False)
                if result == WriteResult.DEADLINE_PASSED:
                    reason = "approval expired"
                    break
                # STALE or POLICY_CHANGED: verify again against the current row and policy.
                # If another call for this tool call won, the next pass returns its grant.
            await self._record_tool_call_event(
                call, "approval.use_refused", reason, approval_id=approval_id
            )
            return ApprovalCheck(False, reason, _status(request))
        except Exception:
            logger.exception("approval consume failed; treating it as not approved")
            await self._record_tool_call_event(
                call, "approval.use_refused", "approval consume failed", approval_id=approval_id
            )
            return ApprovalCheck(False, "approval consume failed")

    # --- Staff-facing operations (API) ------------------------------------------------------

    async def list_for(
        self,
        principal: Principal,
        tenant: TenantContext,
        status: ApprovalStatus | None = None,
    ) -> list[ApprovalView]:
        _check_actor(principal, tenant)
        if not self._can_view_hotel(principal, tenant.org_id, tenant.hotel_id):
            raise NotFoundError("hotel not found")
        await self.expire_due(tenant.org_id, tenant.hotel_id)
        rows = await self._approvals.list(tenant.org_id, tenant.hotel_id, status=status)
        policy = await self._engine.hotel_policy(tenant.org_id, tenant.hotel_id)
        return [
            ApprovalView.of(row, full=self._can_decide(principal, row, policy))
            for row in rows
            if self._can_view(principal, row)
        ]

    async def get_for(
        self, principal: Principal, tenant: TenantContext, approval_id: UUID
    ) -> ApprovalView:
        _check_actor(principal, tenant)
        request = await self._approvals.get(tenant.org_id, tenant.hotel_id, approval_id)
        if request is None or not self._can_view(principal, request):
            raise NotFoundError("approval not found")
        request = await self._expire_if_due(request, self._clock())
        policy = await self._engine.hotel_policy(request.org_id, request.hotel_id)
        return ApprovalView.of(request, full=self._can_decide(principal, request, policy))

    async def approve(
        self,
        principal: Principal,
        tenant: TenantContext,
        approval_id: UUID,
        reason: str | None = None,
    ) -> ApprovalRequest:
        return await self._decide(principal, tenant, approval_id, True, reason)

    async def reject(
        self,
        principal: Principal,
        tenant: TenantContext,
        approval_id: UUID,
        reason: str | None = None,
    ) -> ApprovalRequest:
        return await self._decide(principal, tenant, approval_id, False, reason)

    async def resolution(
        self, tenant: TenantContext, approval_id: UUID
    ) -> ApprovalResolution | None:
        """The stored outcome for the requester's run, or None while pending or unknown.

        The orchestrator uses this to resume a paused run when a `RunResumer` call failed.
        """
        request = await self._approvals.get(tenant.org_id, tenant.hotel_id, approval_id)
        if request is None or request.requester_id != tenant.actor_id:
            return None
        request = await self._expire_if_due(request, self._clock())
        if request.status == ApprovalStatus.PENDING:
            return None
        return _resolution(request)

    async def cancel(
        self, tenant: TenantContext, approval_id: UUID, reason: str | None = None
    ) -> ApprovalRequest:
        """Cancel a pending request for the run's requester (for example, the run was cancelled)."""
        request = await self._approvals.get(tenant.org_id, tenant.hotel_id, approval_id)
        if request is None or request.requester_id != tenant.actor_id:
            raise NotFoundError("approval not found")
        now = self._clock()
        request = await self._expire_if_due(request, now)
        if request.status != ApprovalStatus.PENDING:
            raise ConflictError(f"approval is {request.status}")
        cancelled = replace(
            request,
            status=ApprovalStatus.CANCELLED,
            decided_at=now,
            reason=_clean_text(reason, MAX_REASON_LENGTH, "reason"),
            version=request.version + 1,
        )
        event = _event(cancelled, ActorType.RUN, cancelled.run_id, "approval.cancelled", now)
        if await self._approvals.replace(request, cancelled, event) != WriteResult.APPLIED:
            raise ConflictError("approval changed; reload it")
        await self._notify(cancelled)
        return cancelled

    async def expire_due(self, org_id: UUID, hotel_id: UUID) -> list[ApprovalRequest]:
        """Expire every pending request of one hotel whose deadline has passed."""
        now = self._clock()
        expired = []
        for request in await self._approvals.list(org_id, hotel_id, status=ApprovalStatus.PENDING):
            updated = await self._expire_if_due(request, now)
            if updated.status == ApprovalStatus.EXPIRED:
                expired.append(updated)
        return expired

    async def set_approver_min_role(
        self,
        principal: Principal,
        tenant: TenantContext,
        kind: ActionKind,
        min_role: HotelRole,
    ) -> HotelPolicy:
        """A hotel `admin` or `owner` sets which roles may approve one action kind.

        The new minimum also applies to pending requests: a decision needs a role that both the
        request's snapshot and the current policy allow.
        """
        _check_actor(principal, tenant)
        org_id, hotel_id = tenant.org_id, tenant.hotel_id
        roles = principal.hotel_roles(org_id, hotel_id)
        if not roles:
            raise NotFoundError("hotel not found")
        if not principal.is_human:
            raise ForbiddenError("only a person can change approver roles")
        if not any(role.at_least(HotelRole.ADMIN) for role in roles):
            raise ForbiddenError("only a hotel admin or owner can change approver roles")
        try:
            validate_approver_min_role(kind, min_role)
        except ValueError as error:
            raise InvalidPolicyError(str(error)) from None

        # One read: the base settings and the next version come from the same snapshot. With no
        # stored policy, the write is an insert (version 0) that fails if another admin won.
        stored = await self._policies.get(org_id, hotel_id)
        base = stored if stored is not None else HotelPolicy(org_id=org_id, hotel_id=hotel_id)
        updated = replace(
            base,
            approver_min_roles={**base.approver_min_roles, kind: min_role},
            version=0 if stored is None else stored.version + 1,
        )
        event = AuditEvent(
            org_id=org_id,
            hotel_id=hotel_id,
            actor_type=ActorType.USER,
            actor_id=str(principal.user_id),
            action="approval_policy.updated",
            subject_type="approval_policy",
            subject_id=f"{hotel_id}:{kind}",
            at=self._clock(),
            details={
                "kind": str(kind),
                "min_role": str(min_role),
                "previous_min_role": str(base.min_role_for(kind)),
            },
        )
        if not await self._policies.put(updated, event):
            raise ConflictError("policy changed; reload it")
        return updated

    # --- Internals ---------------------------------------------------------------------------

    async def _reuse(self, existing: ApprovalRequest, bound: str) -> ApprovalRequest:
        """One approval per tool call: return it for the same proposal, refuse anything else."""
        if existing.proposal_hash != bound:
            raise ConflictError(
                "tool call already has an approval for a different proposal; use a new tool call"
            )
        existing = await self._expire_if_due(existing, self._clock())
        usable = existing.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
        if not usable or existing.consumed_at is not None:
            state = "used" if existing.consumed_at is not None else existing.status
            raise ConflictError(f"tool call already has an approval that is {state}")
        return existing

    async def _decide(
        self,
        principal: Principal,
        tenant: TenantContext,
        approval_id: UUID,
        approve: bool,
        reason: str | None,
    ) -> ApprovalRequest:
        _check_actor(principal, tenant)
        reason = _clean_text(reason, MAX_REASON_LENGTH, "reason")
        status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        for _ in range(MAX_TRANSITION_ATTEMPTS):
            request = await self._approvals.get(tenant.org_id, tenant.hotel_id, approval_id)
            if request is None or not self._can_view(principal, request):
                raise NotFoundError("approval not found")
            if not principal.is_human:
                raise ForbiddenError("only a person can decide an approval")
            request = await self._expire_if_due(request, self._clock())
            if request.status != ApprovalStatus.PENDING:
                raise ConflictError(f"approval is {request.status}")
            policy, policy_version = await self._engine.load_policy(
                request.org_id, request.hotel_id
            )
            if not self._can_decide(principal, request, policy):
                raise ForbiddenError("you do not have a role that can decide this approval")
            if approve and _self_approval_blocked(principal, request, policy):
                raise ForbiddenError("the requester cannot approve their own high-risk action")

            # The store checks the policy version and `expires_at` again, atomically with the
            # write, so a change while this call awaited I/O cannot slip through.
            now = self._clock()
            decided = replace(
                request,
                status=status,
                decided_by=principal.user_id,
                decided_at=now,
                use_by=now + policy.use_window if approve else None,
                reason=reason,
                version=request.version + 1,
            )
            event = _event(
                decided, ActorType.USER, str(principal.user_id), f"approval.{status}", now
            )
            guard = TransitionGuard(policy_version, deadline=request.expires_at)
            result = await self._approvals.replace(request, decided, event, guard)
            if result == WriteResult.APPLIED:
                await self._notify(decided)
                return decided
            if result == WriteResult.DEADLINE_PASSED:
                # The store's clock is authoritative: expire now, even if this clock trails it.
                await self._expire(request, max(self._clock(), request.expires_at))
                raise ConflictError(f"approval is {ApprovalStatus.EXPIRED}")
            # STALE or POLICY_CHANGED: read the request and the policy again.
        raise ConflictError("approval changed; reload it")

    async def _verify(
        self, approval_id: UUID, call: ProposedCall, now: datetime, *, allow_used: bool = False
    ) -> tuple[ApprovalRequest | None, str | None, Assessment | None]:
        """The request, why it cannot authorise this exact tool call now (None if it can), and
        the current policy assessment that the answer used.

        With `allow_used`, a used approval passes if every other check passes: the same run,
        requester, and tool call, the same proposal, the current policy, and `use_by`. Only
        `consume` sets it, to return the same grant again; `check` refuses a used approval.
        """
        if not isinstance(call, ProposedCall) or not isinstance(call.tenant, TenantContext):
            return None, "tool call has no tenant context", None
        request = await self._approvals.get(call.org_id, call.hotel_id, approval_id)
        if request is None:
            return None, "approval not found", None
        if request.status != ApprovalStatus.APPROVED:
            if _is_due(request, now):
                return request, "approval expired", None
            return request, f"approval is {request.status}", None
        if request.consumed_at is not None and not allow_used:
            return request, "approval was already used", None
        if request.use_by is None or now >= request.use_by:
            return request, "approval expired", None
        if request.run_id != call.run_id or request.requester_id != call.requester_id:
            return request, "approval belongs to another run", None
        if request.tool_call_id != call.tool_call_id:
            return request, "approval belongs to another tool call", None
        assessment = await self._engine.assess(call)  # the current policy, at the time of use
        if assessment.decision.verdict == Verdict.DENY:
            reason = f"the policy now denies this action: {assessment.decision.reason}"
            return request, reason, assessment
        if assessment.proposal_hash != request.proposal_hash:
            return request, "action changed after approval; a new approval is needed", assessment
        return request, None, assessment

    async def _expire_if_due(self, request: ApprovalRequest, now: datetime) -> ApprovalRequest:
        if not _is_due(request, now):
            return request
        return await self._expire(request, now)

    async def _expire(self, request: ApprovalRequest, now: datetime) -> ApprovalRequest:
        """Mark a pending request expired (compare-and-set), audit it, and resume the run."""
        expired = replace(
            request, status=ApprovalStatus.EXPIRED, decided_at=now, version=request.version + 1
        )
        event = _event(expired, ActorType.SYSTEM, None, "approval.expired", now)
        if await self._approvals.replace(request, expired, event) != WriteResult.APPLIED:
            current = await self._approvals.get(request.org_id, request.hotel_id, request.id)
            return current or request
        await self._notify(expired)
        return expired

    def _can_view_hotel(self, principal: Principal, org_id: UUID, hotel_id: UUID) -> bool:
        return principal.is_member(org_id, hotel_id) or _is_code_reviewer(principal)

    def _can_view(self, principal: Principal, request: ApprovalRequest) -> bool:
        if principal.is_member(request.org_id, request.hotel_id):
            return True
        return request.is_code and _is_code_reviewer(principal)

    def _can_decide(
        self, principal: Principal, request: ApprovalRequest, policy: HotelPolicy
    ) -> bool:
        # The stricter of the request's snapshot and the current policy (a raised minimum role
        # also protects pending requests).
        allowed: frozenset[ApproverRole] = request.approver_roles & approver_roles_for(
            policy, request.kind
        )
        if request.is_code:
            return TeamRole.CODE_REVIEWER in allowed and _is_code_reviewer(principal)
        return bool(principal.hotel_roles(request.org_id, request.hotel_id) & allowed)

    async def _notify(self, request: ApprovalRequest) -> None:
        if self._resumer is None:
            return
        try:
            await self._resumer.resume(_resolution(request))
        except Exception:
            # The decision is stored; the orchestrator recovers it with `resolution()`.
            logger.exception("run resume failed for approval %s", request.id)

    async def _record_tool_call_event(
        self,
        call: ProposedCall,
        action: str,
        reason: str,
        *,
        approval_id: UUID | None = None,
    ) -> None:
        """Best-effort audit of a denial or refused use. Never raises."""
        try:
            if not isinstance(call, ProposedCall) or not isinstance(call.tenant, TenantContext):
                logger.warning("%s without a tenant: %s", action, reason)
                return
            details = {
                "run_id": call.run_id,
                "tool_call_id": call.tool_call_id,
                "toolset": call.toolset,
                "tool": call.tool,
                "requester_id": str(call.requester_id),
                "reason": reason,
            }
            if approval_id is not None:
                details["approval_id"] = str(approval_id)
            await self._audit.record(
                AuditEvent(
                    org_id=call.org_id,
                    hotel_id=call.hotel_id,
                    actor_type=ActorType.RUN,
                    actor_id=call.run_id,
                    action=action,
                    subject_type="tool_call",
                    subject_id=call.tool_call_id,
                    at=self._clock(),
                    details=details,
                )
            )
        except Exception:
            logger.exception("audit write failed for %s", action)


def _is_code_reviewer(principal: Principal) -> bool:
    return principal.is_human and TeamRole.CODE_REVIEWER in principal.team_roles


def _self_approval_blocked(
    principal: Principal, request: ApprovalRequest, policy: HotelPolicy
) -> bool:
    return (
        principal.user_id == request.requester_id
        and request.risk == RiskLevel.HIGH
        and policy.forbid_self_approval_of_high_risk
    )


def _is_due(request: ApprovalRequest, now: datetime) -> bool:
    return request.status == ApprovalStatus.PENDING and now >= request.expires_at


def _status(request: ApprovalRequest | None) -> ApprovalStatus | None:
    return None if request is None else request.status


def _clean_text(text: str | None, limit: int, name: str) -> str | None:
    if text is None or not text.strip():
        return None
    text = text.strip()
    if len(text) > limit:
        raise ValueError(f"{name} must have at most {limit} characters")
    return text


def _check_actor(principal: Principal, tenant: TenantContext) -> None:
    if tenant.actor_id != principal.user_id:
        raise ValueError("tenant.actor_id must be the principal's user id")


def _grant(request: ApprovalRequest, reason: str, replayed: bool) -> ApprovalCheck:
    """A successful use: the approved arguments and the idempotency key for the write."""
    return ApprovalCheck(
        True,
        reason,
        request.status,
        args=request.args,
        idempotency_key=str(request.id),
        replayed=replayed,
    )


def _resolution(request: ApprovalRequest) -> ApprovalResolution:
    return ApprovalResolution(
        approval_id=request.id,
        org_id=request.org_id,
        hotel_id=request.hotel_id,
        run_id=request.run_id,
        tool_call_id=request.tool_call_id,
        outcome=request.status,
        reason=request.reason,
    )


def _event(
    request: ApprovalRequest,
    actor_type: ActorType,
    actor_id: str | None,
    action: str,
    at: datetime,
) -> AuditEvent:
    details = {
        "kind": str(request.kind),
        "risk": str(request.risk),
        "status": str(request.status),
        "run_id": request.run_id,
        "tool_call_id": request.tool_call_id,
        "requester_id": str(request.requester_id),
        "proposal_hash": request.proposal_hash,
    }
    if request.reason:
        details["reason"] = request.reason
    return AuditEvent(
        org_id=request.org_id,
        hotel_id=request.hotel_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        subject_type="approval_request",
        subject_id=str(request.id),
        at=at,
        details=details,
    )
