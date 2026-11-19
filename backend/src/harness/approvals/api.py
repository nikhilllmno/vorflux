"""HTTP routes for the approval inbox (DEX-20): list, read, approve, reject, set approver roles.

The routes fail closed until their two dependencies exist:

- `get_principal` returns 401 until Supabase JWT auth lands (DEX-12), which will override it.
  DEX-12 must build a `human` principal only from a user session JWT. A run token or an MCP
  tool credential gives a `run` principal, which can never approve or reject.
- `get_approval_service` returns 503 until the app has an `approval_service` with a Postgres
  store. Tests set both.

Another tenant's ids return 404, never 403 (docs/architecture.md Section 7.4).

A card is `redacted` for a principal who cannot decide the request: sensitive preview values
are hidden and the note is left out (docs/approvals.md, "Who sees what").
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
    ApprovalStatus,
    ApprovalView,
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
from harness.tenancy import TenantContext


def get_principal() -> Principal:
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication is not configured")


def get_approval_service(request: Request) -> ApprovalService:
    service = getattr(request.app.state, "approval_service", None)
    if not isinstance(service, ApprovalService):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "approvals are not configured")
    return service


PrincipalDep = Annotated[Principal, Depends(get_principal)]
ServiceDep = Annotated[ApprovalService, Depends(get_approval_service)]


class PreviewFieldOut(BaseModel):
    label: str
    value: str
    verified: bool  # False: the agent reported this value; the harness did not check it
    sensitive: bool  # True: hidden on a redacted card


class PreviewOut(BaseModel):
    title: str
    fields: list[PreviewFieldOut]


class ApprovalOut(BaseModel):
    """The approval card.

    `preview` is rendered by the harness from the bound arguments, the tool registry, and the
    hotel config, so it shows every argument that the approval binds to. `note` is optional
    agent text, shown as context only. On a `redacted` card the sensitive values are hidden and
    `note` is null.
    """

    id: UUID
    run_id: str
    tool_call_id: str
    requester_id: UUID
    kind: ActionKind
    risk: RiskLevel
    toolset: str
    tool: str
    preview: PreviewOut
    note: str | None
    proposal_hash: str
    approver_roles: list[str]
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    decided_by: UUID | None
    decided_at: datetime | None
    use_by: datetime | None
    reason: str | None
    used: bool
    redacted: bool

    @classmethod
    def of(cls, view: ApprovalView) -> "ApprovalOut":
        request = view.request
        return cls(
            id=request.id,
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
            requester_id=request.requester_id,
            kind=request.kind,
            risk=request.risk,
            toolset=request.toolset,
            tool=request.tool,
            preview=PreviewOut.model_validate(request.preview.as_json()),
            note=request.note,
            proposal_hash=request.proposal_hash,
            approver_roles=sorted(str(role) for role in request.approver_roles),
            status=request.status,
            created_at=request.created_at,
            expires_at=request.expires_at,
            decided_by=request.decided_by,
            decided_at=request.decided_at,
            use_by=request.use_by,
            reason=request.reason,
            used=request.consumed_at is not None,
            redacted=not view.full,
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


def _tenant(principal: Principal, org_id: UUID, hotel_id: UUID) -> TenantContext:
    """The request's tenant scope, built once at the edge. Membership is checked by the service."""
    return TenantContext(org_id=org_id, hotel_id=hotel_id, actor_id=principal.user_id)


@router.get("/approvals")
async def list_approvals(
    org_id: UUID,
    hotel_id: UUID,
    principal: PrincipalDep,
    service: ServiceDep,
    status_filter: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
) -> ApprovalList:
    with _errors():
        tenant = _tenant(principal, org_id, hotel_id)
        rows = await service.list_for(principal, tenant, status_filter)
    return ApprovalList(approvals=[ApprovalOut.of(view) for view in rows])


@router.get("/approvals/{approval_id}")
async def get_approval(
    org_id: UUID, hotel_id: UUID, approval_id: UUID, principal: PrincipalDep, service: ServiceDep
) -> ApprovalOut:
    with _errors():
        tenant = _tenant(principal, org_id, hotel_id)
        view = await service.get_for(principal, tenant, approval_id)
    return ApprovalOut.of(view)


@router.post("/approvals/{approval_id}/approve")
async def approve(
    org_id: UUID,
    hotel_id: UUID,
    approval_id: UUID,
    body: DecisionIn,
    principal: PrincipalDep,
    service: ServiceDep,
) -> ApprovalOut:
    with _errors():
        decided = await service.approve(
            principal, _tenant(principal, org_id, hotel_id), approval_id, body.reason
        )
    # Only a principal who can decide gets here, so the card is full.
    return ApprovalOut.of(ApprovalView.of(decided, full=True))


@router.post("/approvals/{approval_id}/reject")
async def reject(
    org_id: UUID,
    hotel_id: UUID,
    approval_id: UUID,
    body: DecisionIn,
    principal: PrincipalDep,
    service: ServiceDep,
) -> ApprovalOut:
    with _errors():
        decided = await service.reject(
            principal, _tenant(principal, org_id, hotel_id), approval_id, body.reason
        )
    # Only a principal who can decide gets here, so the card is full.
    return ApprovalOut.of(ApprovalView.of(decided, full=True))


@router.put("/approval-policy/approvers/{kind}")
async def set_approver_role(
    org_id: UUID,
    hotel_id: UUID,
    kind: ActionKind,
    body: ApproverRoleIn,
    principal: PrincipalDep,
    service: ServiceDep,
) -> ApproverRoleOut:
    with _errors():
        policy = await service.set_approver_min_role(
            principal, _tenant(principal, org_id, hotel_id), kind, body.min_role
        )
    return ApproverRoleOut(kind=kind, min_role=policy.min_role_for(kind))
