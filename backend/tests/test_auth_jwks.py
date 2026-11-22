"""JWKS caching and key rotation: cached fetches, one rate-limited refetch for an unknown kid."""

import asyncio

import jwt
import pytest
from auth_world import FakeClock, FakeJwksEndpoint, es256_key, mint, rs256_key, verifier
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from harness.auth import InvalidTokenError, JwksUnavailableError, TokenVerifier, parse_jwks


def accepts(check: TokenVerifier, token: str) -> bool:
    try:
        asyncio.run(check.verify(token))
    except InvalidTokenError:
        return False
    return True


def test_keys_are_fetched_once_and_cached() -> None:
    key = es256_key()
    endpoint = FakeJwksEndpoint()
    endpoint.publish(key)
    check = verifier(endpoint)

    for _ in range(5):
        assert accepts(check, mint(key))

    assert endpoint.calls == 1


def test_keys_are_refetched_after_the_cache_ttl() -> None:
    key = es256_key()
    endpoint, clock = FakeJwksEndpoint(), FakeClock()
    endpoint.publish(key)
    check = verifier(endpoint, clock, ttl_seconds=600)

    assert accepts(check, mint(key))
    clock.advance(599)
    assert accepts(check, mint(key))
    assert endpoint.calls == 1

    clock.advance(2)
    assert accepts(check, mint(key))
    assert endpoint.calls == 2


def test_rotation_unknown_kid_triggers_one_refetch_and_then_verifies() -> None:
    old, new = es256_key(), es256_key()
    endpoint, clock = FakeJwksEndpoint(), FakeClock()
    endpoint.publish(old)
    check = verifier(endpoint, clock, min_refetch_seconds=30)
    assert accepts(check, mint(old))
    clock.advance(31)

    endpoint.publish(old, new)  # Supabase publishes the standby key, then rotates to it

    assert accepts(check, mint(new))
    assert endpoint.calls == 2
    assert accepts(check, mint(old))  # the previous key stays trusted while it is published
    assert endpoint.calls == 2


def test_unknown_kid_refetch_is_rate_limited() -> None:
    key = es256_key()
    endpoint, clock = FakeJwksEndpoint(), FakeClock()
    endpoint.publish(key)
    check = verifier(endpoint, clock, min_refetch_seconds=30)
    assert accepts(check, mint(key))
    clock.advance(31)

    # Forged tokens with random kids: one refetch, then no more until the interval passes.
    for _ in range(20):
        assert not accepts(check, mint(es256_key()))
    assert endpoint.calls == 2

    clock.advance(29)
    assert not accepts(check, mint(es256_key()))
    assert endpoint.calls == 2

    clock.advance(2)
    assert not accepts(check, mint(es256_key()))
    assert endpoint.calls == 3


def test_unknown_kid_right_after_a_fetch_does_not_refetch() -> None:
    key = es256_key()
    endpoint = FakeJwksEndpoint()
    endpoint.publish(key)
    check = verifier(endpoint, FakeClock(), min_refetch_seconds=30)

    assert not accepts(check, mint(es256_key()))  # first fetch happens here

    assert endpoint.calls == 1


def test_concurrent_unknown_kids_share_one_refetch() -> None:
    key, new = es256_key(), es256_key()
    endpoint, clock = FakeJwksEndpoint(), FakeClock()
    endpoint.publish(key)
    check = verifier(endpoint, clock, min_refetch_seconds=30)
    assert accepts(check, mint(key))
    clock.advance(31)
    endpoint.publish(key, new)

    async def burst() -> list[bool]:
        endpoint.hold = asyncio.Event()

        async def one() -> bool:
            try:
                await check.verify(mint(new))
            except InvalidTokenError:
                return False
            return True

        tasks = [asyncio.create_task(one()) for _ in range(10)]
        await asyncio.sleep(0)
        endpoint.hold.set()
        return await asyncio.gather(*tasks)

    assert asyncio.run(burst()) == [True] * 10
    assert endpoint.calls == 2


def test_revoked_key_is_dropped_on_the_next_fetch() -> None:
    old, new = es256_key(), es256_key()
    endpoint, clock = FakeJwksEndpoint(), FakeClock()
    endpoint.publish(old, new)
    check = verifier(endpoint, clock, ttl_seconds=600)
    assert accepts(check, mint(old))

    endpoint.publish(new)
    clock.advance(601)

    assert not accepts(check, mint(old))
    assert accepts(check, mint(new))


def test_first_fetch_failure_is_unavailable_not_invalid() -> None:
    endpoint = FakeJwksEndpoint(fail=True)
    check = verifier(endpoint)

    with pytest.raises(JwksUnavailableError):
        asyncio.run(check.verify(mint(es256_key())))


def test_refresh_failure_keeps_cached_keys_until_max_stale() -> None:
    key = es256_key()
    endpoint, clock = FakeJwksEndpoint(), FakeClock()
    endpoint.publish(key)
    check = verifier(endpoint, clock, ttl_seconds=600, max_stale_seconds=3600)
    assert accepts(check, mint(key))

    endpoint.fail = True
    clock.advance(601)
    assert accepts(check, mint(key))  # stale but inside max_stale
    clock.advance(10)
    assert accepts(check, mint(key))
    calls_after_failures = endpoint.calls

    clock.advance(3600)
    with pytest.raises(JwksUnavailableError):
        asyncio.run(check.verify(mint(key)))
    assert endpoint.calls > calls_after_failures


def test_failed_refresh_is_retried_no_more_than_the_refetch_interval() -> None:
    key = es256_key()
    endpoint, clock = FakeJwksEndpoint(), FakeClock()
    endpoint.publish(key)
    check = verifier(endpoint, clock, ttl_seconds=600, min_refetch_seconds=30)
    assert accepts(check, mint(key))
    endpoint.fail = True
    clock.advance(601)

    for _ in range(10):
        assert accepts(check, mint(key))

    assert endpoint.calls == 2


@pytest.mark.parametrize("document", [{}, {"keys": "not-a-list"}, [], "text"])
def test_malformed_jwks_document_is_refused(document: object) -> None:
    with pytest.raises(ValueError):
        parse_jwks(document)


@pytest.mark.parametrize(
    "keys",
    [
        [],
        [{"kty": "oct", "kid": "secret", "alg": "HS256", "k": "c2VjcmV0"}],
        [{"kty": "EC", "kid": "no-curve"}],
    ],
)
def test_jwks_with_no_usable_key_is_unavailable_not_invalid(keys: list[object]) -> None:
    """No usable key is a server-side problem (503), not a bad user token (401)."""
    endpoint = FakeJwksEndpoint()
    endpoint.publish(*keys)  # type: ignore[arg-type]

    with pytest.raises(JwksUnavailableError):
        asyncio.run(verifier(endpoint).verify(mint(es256_key())))


def test_unknown_kid_with_usable_keys_is_invalid() -> None:
    endpoint = FakeJwksEndpoint()
    endpoint.publish(es256_key())

    with pytest.raises(InvalidTokenError):
        asyncio.run(verifier(endpoint).verify(mint(es256_key())))


def test_only_asymmetric_signature_keys_are_kept() -> None:
    good_ec, good_rsa = es256_key("ec"), rs256_key("rsa")
    p384 = jwt.algorithms.ECAlgorithm.to_jwk(
        ec.generate_private_key(ec.SECP384R1()).public_key(), as_dict=True
    )
    short_rsa = jwt.algorithms.RSAAlgorithm.to_jwk(
        rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key(),  # noqa: S505
        as_dict=True,
    )
    document = {
        "keys": [
            good_ec.public_jwk,
            good_rsa.public_jwk,
            {"kty": "oct", "kid": "secret", "alg": "HS256", "k": "c2VjcmV0"},
            {**p384, "kid": "p384", "alg": "ES256"},
            {**short_rsa, "kid": "short", "alg": "RS256"},
            {**good_ec.public_jwk, "kid": "enc", "use": "enc"},
            {**good_ec.public_jwk, "kid": "ops", "key_ops": ["encrypt"]},
            {**good_ec.public_jwk, "kid": "rs-on-ec", "alg": "RS256"},
            {**good_ec.public_jwk, "kid": "dup"},
            {**good_rsa.public_jwk, "kid": "dup"},
            "not-a-key",
        ]
    }

    keys = parse_jwks(document)

    assert sorted(keys) == ["ec", "rsa"]
    assert keys["ec"].alg == "ES256"
    assert keys["rsa"].alg == "RS256"
