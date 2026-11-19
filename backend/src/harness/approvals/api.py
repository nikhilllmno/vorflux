"""HTTP routes for the approval inbox (DEX-20): list, read, approve, reject, set approver roles.

The routes fail closed until their two dependencies exist:

- `get_principal` returns 401 until Supabase JWT auth lands (DEX-12), which will override it.
- `get_approval_service` returns 503 until the app has an `approval_service` with a Postgres
  store. Tests set both.

Another tenant's ids return 404, never 403 (docs/architecture.md Section 7.4).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from harness.approvals.model import (
    ActionKind,
    ApprovalRequest,
    ApprovalStatus,
    HotelRole,
    Principal,
    RiskLevel,
)
from harness.approvals.service import (
    ApprovalService,
    ConflictError,
    ForbiddenError,
    InvalidPolicyError,
    NotFoundError,
)


def get_principal() -> Principal:
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication is not configured")


def get_approval_service(request: Request) -> ApprovalService:
    service = getattr(request.app.state, "approval_service", None)
    if not isinstance(service, ApprovalService):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "approvals are not configured")
    return service


PrincipalDep = Annotated[Principal, Depends(get_principal)]
ServiceDep = Annotated[ApprovalService, Depends(get_approval_service)]


class ApprovalOut(BaseModel):
    """The approval card. The raw payload is not exposed; `summary` is the staff preview."""

    id: UUID
    run_id: str
    call_id: str
    requester_id: UUID
    kind: ActionKind
    risk: RiskLevel
    toolset: str
    tool: str
    summary: str
    payload_hash: str
    approver_roles: list[str]
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    decided_by: UUID | None
    decided_at: datetime | None
    reason: str | None
    used: bool

    @classmethod
    def of(cls, request: ApprovalRequest) -> "ApprovalOut":
        return cls(
            id=request.id,
            run_id=request.run_id,
            call_id=request.call_id,
            requester_id=request.requester_id,
            kind=request.kind,
            risk=request.risk,
            toolset=request.toolset,
            tool=request.tool,
            summary=request.summary,
            payload_hash=request.payload_hash,
            approver_roles=sorted(str(role) for role in request.approver_roles),
            status=request.status,
            created_at=request.created_at,
            expires_at=request.expires_at,
            decided_by=request.decided_by,
            decided_at=request.decided_at,
            reason=request.reason,
            used=request.consumed_at is not None,
        )


class ApprovalList(BaseModel):
    approvals: list[ApprovalOut]


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)


class ApproverRoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_role: HotelRole


class ApproverRoleOut(BaseModel):
    kind: ActionKind
    min_role: HotelRole


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except NotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
    except ForbiddenError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from None
    except ConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    except InvalidPolicyError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None


router = APIRouter(prefix="/v1/orgs/{org_id}/hotels/{hotel_id}", tags=["approvals"])


@router.get("/approvals")
def list_approvals(
    org_id: UUID,
    hotel_id: UUID,
    principal: PrincipalDep,
    service: ServiceDep,
    status_filter: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
) -> ApprovalList:
    with _errors():
        rows = service.list_for(principal, org_id, hotel_id, status_filter)
    return ApprovalList(approvals=[ApprovalOut.of(row) for row in rows])


@router.get("/approvals/{approval_id}")
def get_approval(
    org_id: UUID, hotel_id: UUID, approval_id: UUID, principal: PrincipalDep, service: ServiceDep
) -> ApprovalOut:
    with _errors():
        return ApprovalOut.of(service.get_for(principal, org_id, hotel_id, approval_id))


@router.post("/approvals/{approval_id}/approve")
def approve(
    org_id: UUID,
    hotel_id: UUID,
    approval_id: UUID,
    body: DecisionIn,
    principal: PrincipalDep,
    service: ServiceDep,
) -> ApprovalOut:
    with _errors():
        decided = service.approve(principal, org_id, hotel_id, approval_id, body.reason)
    return ApprovalOut.of(decided)


@router.post("/approvals/{approval_id}/reject")
def reject(
    org_id: UUID,
    hotel_id: UUID,
    approval_id: UUID,
    body: DecisionIn,
    principal: PrincipalDep,
    service: ServiceDep,
) -> ApprovalOut:
    with _errors():
        decided = service.reject(principal, org_id, hotel_id, approval_id, body.reason)
    return ApprovalOut.of(decided)


@router.put("/approval-policy/approvers/{kind}")
def set_approver_role(
    org_id: UUID,
    hotel_id: UUID,
    kind: ActionKind,
    body: ApproverRoleIn,
    principal: PrincipalDep,
    service: ServiceDep,
) -> ApproverRoleOut:
    with _errors():
        policy = service.set_approver_min_role(principal, org_id, hotel_id, kind, body.min_role)
    return ApproverRoleOut(kind=kind, min_role=policy.min_role_for(kind))
