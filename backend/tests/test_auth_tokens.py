"""Supabase access-token verification: signature, algorithm allow-list, and claims."""

import asyncio
import time
from typing import Any
from uuid import uuid4

import pytest
from auth_world import (
    AUDIENCE,
    ISSUER,
    FakeJwksEndpoint,
    SigningKey,
    claims,
    es256_key,
    hmac_token,
    mint,
    raw_token,
    rs256_key,
    verifier,
)

from harness.auth import AuthenticatedUser, InvalidTokenError, TokenVerifier


@pytest.fixture
def key() -> SigningKey:
    return es256_key()


@pytest.fixture
def endpoint(key: SigningKey) -> FakeJwksEndpoint:
    endpoint = FakeJwksEndpoint()
    endpoint.publish(key)
    return endpoint


@pytest.fixture
def check(endpoint: FakeJwksEndpoint) -> TokenVerifier:
    return verifier(endpoint)


def verify(check: TokenVerifier, token: str) -> AuthenticatedUser:
    return asyncio.run(check.verify(token))


def rejected(check: TokenVerifier, token: str) -> None:
    with pytest.raises(InvalidTokenError):
        verify(check, token)


# --- Valid tokens ------------------------------------------------------------------------------


def test_valid_es256_token_gives_the_user_and_session(
    check: TokenVerifier, key: SigningKey
) -> None:
    user_id, session_id = uuid4(), str(uuid4())

    user = verify(check, mint(key, user_id, session_id=session_id))

    assert user == AuthenticatedUser(user_id=user_id, session_id=session_id)


def test_valid_rs256_token_is_accepted(endpoint: FakeJwksEndpoint, key: SigningKey) -> None:
    rsa_key = rs256_key()
    endpoint.publish(key, rsa_key)
    user_id = uuid4()

    assert verify(verifier(endpoint), mint(rsa_key, user_id)).user_id == user_id


def test_expiry_inside_the_leeway_is_accepted(check: TokenVerifier, key: SigningKey) -> None:
    assert verify(check, mint(key, exp=int(time.time()) - 5)).user_id


# --- Time claims -------------------------------------------------------------------------------


def test_expired_token_is_rejected(check: TokenVerifier, key: SigningKey) -> None:
    rejected(check, mint(key, exp=int(time.time()) - 60))


def test_token_without_exp_is_rejected(check: TokenVerifier, key: SigningKey) -> None:
    rejected(check, mint(key, exp=None))


def test_token_not_valid_yet_is_rejected(check: TokenVerifier, key: SigningKey) -> None:
    rejected(check, mint(key, nbf=int(time.time()) + 300))


def test_token_issued_in_the_future_is_rejected(check: TokenVerifier, key: SigningKey) -> None:
    rejected(check, mint(key, iat=int(time.time()) + 300))


def test_token_without_iat_is_rejected(check: TokenVerifier, key: SigningKey) -> None:
    rejected(check, mint(key, iat=None))


# --- Issuer and audience -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "issuer",
    [
        "https://other-project.supabase.test/auth/v1",
        ISSUER + "/",
        ISSUER.upper(),
        None,
    ],
)
def test_wrong_or_missing_issuer_is_rejected(
    check: TokenVerifier, key: SigningKey, issuer: str | None
) -> None:
    rejected(check, mint(key, iss=issuer))


@pytest.mark.parametrize("audience", ["anon", "service_role", "", ["other"], None])
def test_wrong_or_missing_audience_is_rejected(
    check: TokenVerifier, key: SigningKey, audience: Any
) -> None:
    rejected(check, mint(key, aud=audience))


def test_audience_list_that_contains_ours_is_accepted(
    check: TokenVerifier, key: SigningKey
) -> None:
    assert verify(check, mint(key, aud=["other", AUDIENCE])).user_id


# --- Signature ---------------------------------------------------------------------------------


def test_token_signed_by_another_key_with_our_kid_is_rejected(
    check: TokenVerifier, key: SigningKey
) -> None:
    attacker = es256_key(kid=key.kid)

    rejected(check, mint(attacker))


def test_tampered_payload_is_rejected(check: TokenVerifier, key: SigningKey) -> None:
    header, _, signature = mint(key).split(".")
    _, forged_payload, _ = mint(es256_key(kid=key.kid), sub=str(uuid4())).split(".")

    rejected(check, f"{header}.{forged_payload}.{signature}")


def test_embedded_jwk_header_does_not_choose_the_key(check: TokenVerifier, key: SigningKey) -> None:
    attacker = es256_key(kid=key.kid)

    rejected(check, mint(attacker, headers={"jwk": dict(attacker.public_jwk)}))


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b", "a.b.c", "a.b.c.d.e", "x" * 20_000])
def test_malformed_token_is_rejected(check: TokenVerifier, token: str) -> None:
    rejected(check, token)


# --- Algorithm allow-list and confusion --------------------------------------------------------


@pytest.mark.parametrize("alg", ["none", "None", "NONE", "nOnE"])
def test_unsigned_alg_none_token_is_rejected(
    check: TokenVerifier, key: SigningKey, alg: str
) -> None:
    rejected(check, raw_token({"alg": alg, "typ": "JWT", "kid": key.kid}, claims(), b""))


def test_hs256_signed_with_the_public_key_is_rejected(endpoint: FakeJwksEndpoint) -> None:
    """Classic RS/HS confusion: the attacker uses the published public key as an HMAC secret."""
    rsa_key = rs256_key()
    endpoint.publish(rsa_key)
    header = {"alg": "HS256", "typ": "JWT", "kid": rsa_key.kid}

    rejected(verifier(endpoint), hmac_token(rsa_key.public_pem(), header, claims()))


def test_symmetric_key_in_the_jwks_is_never_used(endpoint: FakeJwksEndpoint) -> None:
    secret = b"a-shared-secret-that-must-never-verify-a-token"
    oct_jwk = {"kty": "oct", "kid": "legacy", "alg": "HS256", "k": "YS1zaGFyZWQtc2VjcmV0"}
    endpoint.publish(oct_jwk)

    token = hmac_token(secret, {"alg": "HS256", "typ": "JWT", "kid": "legacy"}, claims())

    rejected(verifier(endpoint), token)


def test_header_alg_that_does_not_match_the_key_type_is_rejected(
    endpoint: FakeJwksEndpoint, key: SigningKey
) -> None:
    """An RS256 header naming an EC key's kid never verifies against that key."""
    rsa_attacker = rs256_key(kid=key.kid)

    rejected(verifier(endpoint), mint(rsa_attacker))


def test_header_alg_that_differs_from_the_jwk_alg_is_rejected(endpoint: FakeJwksEndpoint) -> None:
    rsa_key = rs256_key()
    endpoint.publish({**rsa_key.public_jwk, "alg": "RS512"})

    rejected(verifier(endpoint), mint(rsa_key))


def test_key_published_for_encryption_is_not_used_to_verify(endpoint: FakeJwksEndpoint) -> None:
    ec_key = es256_key()
    endpoint.publish({**ec_key.public_jwk, "use": "enc"})

    rejected(verifier(endpoint), mint(ec_key))


def test_algorithm_outside_the_configured_allow_list_is_rejected(
    endpoint: FakeJwksEndpoint,
) -> None:
    rsa_key = rs256_key()
    endpoint.publish(rsa_key)

    rejected(verifier(endpoint, algorithms=("ES256",)), mint(rsa_key))


def test_token_without_kid_is_rejected(check: TokenVerifier, key: SigningKey) -> None:
    import jwt

    token = jwt.encode(claims(), key.private_key, "ES256")  # no kid header

    rejected(check, token)


@pytest.mark.parametrize("algorithms", [("HS256",), ("none",), ("ES256", "HS256"), ()])
def test_verifier_refuses_symmetric_none_or_empty_allow_lists(
    endpoint: FakeJwksEndpoint, algorithms: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        verifier(endpoint, algorithms=algorithms)


# --- Subject and Supabase role -----------------------------------------------------------------


@pytest.mark.parametrize("sub", ["", "not-a-uuid", None, 42])
def test_subject_must_be_a_user_uuid(check: TokenVerifier, key: SigningKey, sub: Any) -> None:
    rejected(check, mint(key, sub=sub))


@pytest.mark.parametrize("role", ["anon", "service_role", "supabase_admin", None])
def test_only_authenticated_user_tokens_are_accepted(
    check: TokenVerifier, key: SigningKey, role: str | None
) -> None:
    rejected(check, mint(key, role=role))


def test_anonymous_sign_in_tokens_are_rejected(check: TokenVerifier, key: SigningKey) -> None:
    rejected(check, mint(key, is_anonymous=True))
