"""`GET /v1/orgs/{org_id}/hotels/{hotel_id}/me`: who the caller is in one hotel.

The web app's org and hotel switcher (DEX-13) calls it to show the caller's role.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from harness.auth.dependencies import require_tenant
from harness.tenancy import Action, HotelRole, TenantContext

router = APIRouter(prefix="/v1/orgs/{org_id}/hotels/{hotel_id}", tags=["auth"])


class MeOut(BaseModel):
    user_id: UUID
    org_id: UUID
    hotel_id: UUID
    role: HotelRole


@router.get("/me")
def me(tenant: Annotated[TenantContext, Depends(require_tenant(Action.VIEW))]) -> MeOut:
    assert tenant.role is not None  # noqa: S101 - require_tenant always sets the role
    return MeOut(
        user_id=tenant.actor_id, org_id=tenant.org_id, hotel_id=tenant.hotel_id, role=tenant.role
    )
