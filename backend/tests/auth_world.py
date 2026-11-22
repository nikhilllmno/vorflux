"""Test keys, tokens and a fake JWKS endpoint for the auth tests.

Every key is generated in memory when a test runs. No key material is stored in the repository.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes

from harness.auth import JwksCache, TokenVerifier

ISSUER = "https://project-ref.supabase.test/auth/v1"
AUDIENCE = "authenticated"


@dataclass(frozen=True)
class SigningKey:
    kid: str
    alg: str
    private_key: PrivateKeyTypes
    public_jwk: Mapping[str, Any]

    def public_pem(self) -> bytes:
        public_key = self.private_key.public_key()  # type: ignore[union-attr]
        pem: bytes = public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return pem


def es256_key(kid: str | None = None) -> SigningKey:
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    kid = kid or str(uuid4())
    return SigningKey(kid, "ES256", private_key, {**jwk, "kid": kid, "alg": "ES256", "use": "sig"})


def rs256_key(kid: str | None = None) -> SigningKey:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    kid = kid or str(uuid4())
    return SigningKey(kid, "RS256", private_key, {**jwk, "kid": kid, "alg": "RS256", "use": "sig"})


def claims(user_id: UUID | None = None, **overrides: Any) -> dict[str, Any]:
    """Claims shaped like a Supabase user access token. Pass `name=None` to drop a claim."""
    now = int(time.time())
    base: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id or uuid4()),
        "iat": now,
        "exp": now + 3600,
        "role": "authenticated",
        "session_id": str(uuid4()),
        "is_anonymous": False,
        "aal": "aal1",
    }
    base.update(overrides)
    return {name: value for name, value in base.items() if value is not None}


def mint(
    key: SigningKey,
    user_id: UUID | None = None,
    *,
    headers: Mapping[str, Any] | None = None,
    **overrides: Any,
) -> str:
    header = {"kid": key.kid, **(headers or {})}
    return jwt.encode(claims(user_id, **overrides), key.private_key, key.alg, headers=header)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def raw_token(header: Mapping[str, Any], payload: Mapping[str, Any], signature: bytes) -> str:
    return ".".join(
        [_b64(json.dumps(header).encode()), _b64(json.dumps(payload).encode()), _b64(signature)]
    )


def hmac_token(secret: bytes, header: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    """An HS256 token signed by hand, so that any bytes (even a public key) can be the secret."""
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


@dataclass
class FakeJwksEndpoint:
    """Serves a JWKS document. Counts fetches; can fail; can hold a fetch open."""

    keys: list[Mapping[str, Any]] = field(default_factory=list)
    calls: int = 0
    fail: bool = False
    hold: asyncio.Event | None = None

    def publish(self, *keys: SigningKey | Mapping[str, Any]) -> None:
        self.keys = [k.public_jwk if isinstance(k, SigningKey) else k for k in keys]

    async def fetch(self) -> Mapping[str, object]:
        self.calls += 1
        if self.hold is not None:
            await self.hold.wait()
        if self.fail:
            raise OSError("JWKS endpoint unreachable")
        return {"keys": list(self.keys)}


@dataclass
class FakeClock:
    now: float = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def verifier(
    endpoint: FakeJwksEndpoint,
    clock: FakeClock | None = None,
    *,
    algorithms: Sequence[str] = ("ES256", "RS256"),
    ttl_seconds: float = 600,
    min_refetch_seconds: float = 30,
    max_stale_seconds: float = 3600,
) -> TokenVerifier:
    cache = JwksCache(
        endpoint,
        ttl_seconds=ttl_seconds,
        min_refetch_seconds=min_refetch_seconds,
        max_stale_seconds=max_stale_seconds,
        clock=clock or FakeClock(),
    )
    return TokenVerifier(
        cache,
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=algorithms,
        leeway_seconds=10,
    )
