"""FastAPI dependencies that authenticate a request and build its `TenantContext`.

Use `require_tenant(action)` on any route under `/v1/orgs/{org_id}/hotels/{hotel_id}/...`:

    @router.post("/runs")
    async def start_run(
        tenant: Annotated[TenantContext, Depends(require_tenant(Action.START_RUN))],
    ) -> ...

Responses (docs/auth.md, "HTTP behaviour"):

- 401 with `WWW-Authenticate: Bearer`: no bearer token, or the token fails a check. The token is
  checked before the path ids, so a caller without a valid token learns nothing about them.
- 403 `forbidden`: no membership that covers the hotel, the hotel is not in the org, either does
  not exist, or the role is below the action's minimum. All of these give the same response.
- 503: auth or the membership provider is not configured, or the signing keys cannot be loaded.
  The routes fail closed.
"""

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Path, Request, status

from harness.auth.jwks import JwksUnavailableError
from harness.auth.memberships import InMemoryMembershipProvider, MembershipProvider
from harness.auth.tokens import AuthenticatedUser, InvalidTokenError, TokenVerifier
from harness.config import Settings
from harness.tenancy import Action, TenantContext

logger = logging.getLogger(__name__)


def _unauthorized(*, invalid_token: bool) -> HTTPException:
    challenge = 'Bearer error="invalid_token"' if invalid_token else "Bearer"
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, "not authenticated", headers={"WWW-Authenticate": challenge}
    )


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail)


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, credentials = authorization.partition(" ")
    token = credentials.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def get_token_verifier(request: Request) -> TokenVerifier:
    """The app's verifier: `app.state.token_verifier`, else one built once from the settings."""
    state = request.app.state
    verifier = getattr(state, "token_verifier", None)
    if isinstance(verifier, TokenVerifier):
        return verifier
    settings: Settings = state.settings
    built = TokenVerifier.from_settings(settings)
    if built is None:
        raise _unavailable("authentication is not configured")
    state.token_verifier = built
    return built


async def get_authenticated_user(request: Request) -> AuthenticatedUser:
    token = _bearer_token(request.headers.get("authorization"))
    if token is None:
        raise _unauthorized(invalid_token=False)
    verifier = get_token_verifier(request)
    try:
        return await verifier.verify(token)
    except InvalidTokenError as error:
        logger.info("rejected access token: %s", error.reason)
        raise _unauthorized(invalid_token=True) from None
    except JwksUnavailableError:
        logger.error("cannot verify access tokens: signing keys are unavailable")
        raise _unavailable("authentication is temporarily unavailable") from None


def get_membership_provider(request: Request) -> MembershipProvider:
    provider = getattr(request.app.state, "membership_provider", None)
    if not isinstance(provider, MembershipProvider):
        raise _unavailable("tenancy is not configured")
    settings: Settings = request.app.state.settings
    if isinstance(provider, InMemoryMembershipProvider) and settings.environment not in (
        "local",
        "test",
    ):
        logger.error("the in-memory membership provider is refused in %s", settings.environment)
        raise _unavailable("tenancy is not configured")
    return provider


AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]
MembershipProviderDep = Annotated[MembershipProvider, Depends(get_membership_provider)]


@functools.cache
def require_tenant(action: Action) -> Callable[..., Awaitable[TenantContext]]:
    """A dependency that returns the caller's `TenantContext` if they may perform `action`.

    The org and hotel come from the `org_id` and `hotel_id` path parameters. The same callable
    is returned for the same action, so `app.dependency_overrides` can target it.
    """

    async def tenant_for_request(
        user: AuthenticatedUserDep,
        provider: MembershipProviderDep,
        org_id: Annotated[UUID, Path()],
        hotel_id: Annotated[UUID, Path()],
    ) -> TenantContext:
        role = await provider.role_for(user.user_id, org_id, hotel_id)
        tenant = TenantContext(org_id=org_id, hotel_id=hotel_id, actor_id=user.user_id, role=role)
        if not tenant.allows(action):  # no role, or below the action's minimum
            logger.info("denied %s for user %s (role %s)", action, user.user_id, role)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        return tenant

    tenant_for_request.__name__ = f"require_tenant_{action.value}"
    return tenant_for_request
