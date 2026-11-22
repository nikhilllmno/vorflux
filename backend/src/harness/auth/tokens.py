"""Verify a Supabase Auth user access token (docs/auth.md, "Token checks").

A token is accepted only when all of these hold:

- It is a compact JWS (three parts) of at most `MAX_TOKEN_LENGTH` characters.
- The header `alg` is in the configured allow-list, which may hold only ES256 and RS256.
  `none`, HS256 (the legacy shared secret) and every other value are refused before any key
  is looked up.
- The header `kid` names a key in the published JWKS, and that key's own algorithm equals the
  header `alg` (so an RS256 header can never use an EC key, and no header can use a
  symmetric key). The token never chooses its key in any other way (`jwk`, `jku`, `x5u` and
  `x5c` headers are ignored).
- The signature verifies; `exp`, `iat`, `iss`, `aud` and `sub` are present; `exp` is in the
  future and `nbf` (if present) and `iat` are not, each within `leeway_seconds`; `iss` equals
  the configured issuer exactly; `aud` is or contains the configured audience.
- `sub` is a canonical user UUID, `role` is `authenticated`, and `is_anonymous` is not true.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import jwt

from harness.auth.jwks import SUPPORTED_ALGORITHMS, HttpJwksFetcher, JwksCache

if TYPE_CHECKING:
    from harness.config import Settings

MAX_TOKEN_LENGTH = 8192
_REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub"]


class InvalidTokenError(Exception):
    """The token is missing, malformed, expired, or not ours. Maps to HTTP 401.

    `reason` is for server logs only; it is never sent to the client.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """A person signed in with a Supabase user session."""

    user_id: UUID  # JWT `sub`, the same value as `auth.uid()` in Postgres
    session_id: str | None = None


class TokenVerifier:
    def __init__(
        self,
        jwks: JwksCache,
        *,
        issuer: str,
        audience: str,
        algorithms: Collection[str] = ("ES256", "RS256"),
        leeway_seconds: float = 10,
    ) -> None:
        allowed = frozenset(algorithms)
        if not allowed or not allowed <= SUPPORTED_ALGORITHMS.keys():
            raise ValueError("allowed algorithms must be a non-empty subset of ES256 and RS256")
        if not issuer.strip() or not audience.strip():
            raise ValueError("issuer and audience are required")
        self._jwks = jwks
        self._issuer = issuer
        self._audience = audience
        self._algorithms = allowed
        self._leeway = leeway_seconds

    @classmethod
    def from_settings(cls, settings: "Settings") -> "TokenVerifier | None":
        """A verifier for the configured Supabase project, or None when auth is not configured."""
        if settings.auth_issuer is None or settings.auth_jwks_url is None:
            return None
        cache = JwksCache(
            HttpJwksFetcher(settings.auth_jwks_url),
            ttl_seconds=settings.auth_jwks_cache_seconds,
            min_refetch_seconds=settings.auth_jwks_min_refetch_seconds,
            max_stale_seconds=max(3600, settings.auth_jwks_cache_seconds),
        )
        return cls(
            cache,
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            algorithms=settings.auth_algorithms,
            leeway_seconds=settings.auth_leeway_seconds,
        )

    async def verify(self, token: str) -> AuthenticatedUser:
        """Raises InvalidTokenError, or JwksUnavailableError when no keys can be loaded."""
        if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH or token.count(".") != 2:
            raise InvalidTokenError("malformed token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise InvalidTokenError("malformed token header") from None
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in self._algorithms:
            raise InvalidTokenError(f"algorithm {alg!r} is not allowed")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidTokenError("token has no kid")

        key = await self._jwks.get(kid)
        if key is None:
            raise InvalidTokenError("unknown signing key")
        if key.alg != alg:
            raise InvalidTokenError("token algorithm does not match its key")

        try:
            claims = jwt.decode(
                token,
                key.jwk,
                algorithms=[key.alg],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={
                    "require": _REQUIRED_CLAIMS,
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.PyJWTError as error:
            raise InvalidTokenError(type(error).__name__) from None
        return _user_from(claims)


def _user_from(claims: dict[str, Any]) -> AuthenticatedUser:
    sub = claims.get("sub")
    try:
        user_id = UUID(sub) if isinstance(sub, str) else None
    except ValueError:
        user_id = None
    if user_id is None or str(user_id) != sub:
        raise InvalidTokenError("sub is not a user id")
    if claims.get("role") != "authenticated":
        raise InvalidTokenError("not a user session token")
    if claims.get("is_anonymous", False) is not False:
        raise InvalidTokenError("anonymous sessions are not accepted")
    session_id = claims.get("session_id")
    return AuthenticatedUser(
        user_id=user_id, session_id=session_id if isinstance(session_id, str) else None
    )
