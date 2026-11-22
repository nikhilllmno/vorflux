"""Request authentication (Supabase Auth JWTs) and the per-request `TenantContext`.

See docs/auth.md. Routes depend on `require_tenant(action)`; tests and local development set
`app.state.token_verifier` and `app.state.membership_provider`.
"""

from harness.auth.dependencies import (
    AuthenticatedUserDep,
    get_authenticated_user,
    get_membership_provider,
    get_token_verifier,
    require_tenant,
)
from harness.auth.jwks import (
    HttpJwksFetcher,
    JwksCache,
    JwksFetcher,
    JwksUnavailableError,
    VerificationKey,
    parse_jwks,
)
from harness.auth.memberships import InMemoryMembershipProvider, MembershipProvider
from harness.auth.tokens import AuthenticatedUser, InvalidTokenError, TokenVerifier

__all__ = [
    "AuthenticatedUser",
    "AuthenticatedUserDep",
    "HttpJwksFetcher",
    "InMemoryMembershipProvider",
    "InvalidTokenError",
    "JwksCache",
    "JwksFetcher",
    "JwksUnavailableError",
    "MembershipProvider",
    "TokenVerifier",
    "VerificationKey",
    "get_authenticated_user",
    "get_membership_provider",
    "get_token_verifier",
    "parse_jwks",
    "require_tenant",
]
