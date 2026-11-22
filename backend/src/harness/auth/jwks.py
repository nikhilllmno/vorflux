"""Supabase Auth signing keys: fetch the JWKS, keep it in memory, refetch on rotation.

Supabase publishes the public half of each asymmetric signing key at
`<project URL>/auth/v1/.well-known/jwks.json`. A new key is published as a standby key before
Auth signs with it, so a token with an unknown `kid` usually means our copy is old.

Cache rules (docs/auth.md, "Signing keys"):

- Fetch on first use, then again when the copy is older than `ttl_seconds` (default 10 minutes,
  the same as Supabase's edge cache). That refresh runs in the background: requests whose `kid`
  is in the copy keep using it and never wait for the JWKS endpoint.
- An unknown `kid` causes at most one refetch per `min_refetch_seconds`, so forged tokens with
  random kids cannot turn the API into a JWKS request amplifier. Concurrent callers share a
  single fetch.
- Each fetch has a total deadline (`fetch_timeout_seconds`, default 10 s), not only a timeout per
  socket operation.
- If a refresh fails, or returns a document with no usable key, the last good keys stay in use
  for up to `max_stale_seconds` after they were fetched. With no usable keys (none fetched yet,
  or the last good ones too old), verification raises `JwksUnavailableError` (HTTP 503).
- Only asymmetric signature keys are kept: `RSA` (RS256, at least 2048 bits) and `EC` on P-256
  (ES256). A symmetric (`oct`) key is never used, even if the document lists one.
"""

import asyncio
import json
import logging
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from jwt import PyJWK, PyJWKError

logger = logging.getLogger(__name__)

# The algorithms this module can verify, and the key type (and curve) each one needs.
SUPPORTED_ALGORITHMS: Mapping[str, tuple[str, str | None]] = {
    "ES256": ("EC", "P-256"),
    "RS256": ("RSA", None),
}
_DEFAULT_ALG_FOR_KTY = {"EC": "ES256", "RSA": "RS256"}
_MIN_RSA_BITS = 2048
_MAX_KID_LENGTH = 200


class JwksUnavailableError(Exception):
    """No usable signing keys: the JWKS could not be fetched and no recent copy exists."""


class JwksFetcher(Protocol):
    async def fetch(self) -> object:
        """Return the parsed JWKS JSON document. Raise on any transport or parse error."""
        ...


@dataclass(frozen=True, slots=True)
class VerificationKey:
    """One public key and the single algorithm it may verify."""

    kid: str
    alg: str
    jwk: PyJWK


def parse_jwks(document: object) -> dict[str, VerificationKey]:
    """The usable keys of a JWKS document, by `kid`. Raises ValueError for a malformed document.

    Unusable entries (symmetric, unknown type, wrong curve, short RSA, not for signatures) are
    skipped. A `kid` listed twice is dropped, because the key it names is ambiguous.
    """
    if not isinstance(document, Mapping):
        raise ValueError("JWKS document is not a JSON object")
    entries = document.get("keys")
    if not isinstance(entries, list):
        raise ValueError("JWKS document has no `keys` list")
    keys: dict[str, VerificationKey] = {}
    duplicates: set[str] = set()
    for entry in entries:
        key = _parse_key(entry)
        if key is None:
            continue
        if key.kid in keys:
            duplicates.add(key.kid)
        keys[key.kid] = key
    for kid in duplicates:
        logger.warning("JWKS lists kid %r more than once; ignoring it", kid)
        del keys[kid]
    return keys


def _parse_key(entry: object) -> VerificationKey | None:
    if not isinstance(entry, Mapping):
        return None
    kid, kty = entry.get("kid"), entry.get("kty")
    if not isinstance(kid, str) or not kid or len(kid) > _MAX_KID_LENGTH:
        return None
    if not isinstance(kty, str) or kty not in _DEFAULT_ALG_FOR_KTY:
        return None  # `oct` (shared secret) and unknown types are never used
    alg = entry.get("alg", _DEFAULT_ALG_FOR_KTY[kty])
    if alg not in SUPPORTED_ALGORITHMS:
        return None
    want_kty, want_curve = SUPPORTED_ALGORITHMS[alg]
    if kty != want_kty or (want_curve is not None and entry.get("crv") != want_curve):
        return None
    if entry.get("use", "sig") != "sig":
        return None
    key_ops = entry.get("key_ops")
    if key_ops is not None and (not isinstance(key_ops, list) or "verify" not in key_ops):
        return None
    try:
        jwk = PyJWK(dict(entry), algorithm=alg)
    except (PyJWKError, ValueError, TypeError, KeyError):
        return None
    if kty == "RSA" and getattr(jwk.key, "key_size", 0) < _MIN_RSA_BITS:
        return None
    return VerificationKey(kid=kid, alg=alg, jwk=jwk)


class JwksCache:
    """In-memory copy of the signing keys with TTL refresh and rate-limited refetch."""

    def __init__(
        self,
        fetcher: JwksFetcher,
        *,
        ttl_seconds: float = 600,
        min_refetch_seconds: float = 30,
        max_stale_seconds: float = 3600,
        fetch_timeout_seconds: float = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or min_refetch_seconds <= 0 or fetch_timeout_seconds <= 0:
            raise ValueError("JWKS ttl, refetch interval and fetch timeout must be positive")
        if max_stale_seconds < ttl_seconds:
            raise ValueError("JWKS max_stale_seconds must be at least ttl_seconds")
        self._fetcher = fetcher
        self._ttl = ttl_seconds
        self._min_refetch = min_refetch_seconds
        self._max_stale = max_stale_seconds
        self._fetch_timeout = fetch_timeout_seconds
        self._clock = clock
        self._keys: dict[str, VerificationKey] | None = None
        self._fetched_at = 0.0  # last successful fetch
        self._attempted_at: float | None = None  # last fetch attempt, successful or not
        self._attempts = 0  # bumps on every attempt; lets waiting callers skip a duplicate fetch
        self._lock = asyncio.Lock()
        self._background: asyncio.Task[None] | None = None

    async def get(self, kid: str) -> VerificationKey | None:
        """The key for `kid`, or None if the published set does not have it.

        Waits for a fetch only when there are no usable keys or `kid` is unknown. Raises
        JwksUnavailableError when there are no usable keys at all.
        """
        if self._has_usable_keys():
            if self._age() >= self._ttl and self._may_attempt():
                self._start_background_refresh()
        elif self._may_fetch():
            await self._refresh(self._attempts)
        key = self._usable_keys().get(kid)
        if key is None and self._may_fetch():
            await self._refresh(self._attempts)
            key = self._usable_keys().get(kid)
        return key

    async def wait_for_background_refresh(self) -> None:
        """Wait until a background refresh (if one runs) has finished. For tests and shutdown."""
        if self._background is not None:
            await self._background

    def _start_background_refresh(self) -> None:
        if self._background is None or self._background.done():
            self._background = asyncio.create_task(self._refresh(self._attempts))

    def _has_usable_keys(self) -> bool:
        return bool(self._keys) and self._age() <= self._max_stale

    def _may_fetch(self) -> bool:
        """True if a fetch may start now, or one is in flight (the caller then waits for it)."""
        return self._lock.locked() or self._may_attempt()

    def _age(self) -> float:
        return self._clock() - self._fetched_at

    def _may_attempt(self) -> bool:
        return self._attempted_at is None or self._clock() - self._attempted_at >= self._min_refetch

    def _usable_keys(self) -> dict[str, VerificationKey]:
        if not self._keys:
            raise JwksUnavailableError("no usable signing key has been fetched")
        if self._age() > self._max_stale:
            raise JwksUnavailableError("signing keys are too old and cannot be refreshed")
        return self._keys

    async def _refresh(self, seen_attempts: int) -> None:
        async with self._lock:
            if self._attempts != seen_attempts:
                return  # another caller fetched while we waited
            started = self._clock()
            self._attempted_at = started
            try:
                document = await asyncio.wait_for(self._fetcher.fetch(), self._fetch_timeout)
                keys = parse_jwks(document)
            except Exception as error:  # any failure (timeout included) keeps the old keys
                logger.warning("JWKS refresh failed: %s", type(error).__name__)
            else:
                if keys:
                    self._keys = keys
                    self._fetched_at = started
                elif self._keys:
                    logger.error("JWKS lists no usable signing key; keeping the last good keys")
                else:
                    logger.error("JWKS lists no usable signing key")
            finally:
                self._attempts += 1


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a redirect is an error: never follow it to another host or scheme


class HttpJwksFetcher:
    """Fetches the JWKS over HTTPS (or plain HTTP for a local stack; config.py checks which)."""

    def __init__(self, url: str, *, timeout_seconds: float = 5.0, max_bytes: int = 64 * 1024):
        if urlsplit(url).scheme not in ("https", "http"):
            raise ValueError("JWKS URL must be http(s)")
        self._url = url
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._opener = urllib.request.build_opener(_NoRedirect)

    async def fetch(self) -> object:
        return await asyncio.to_thread(self._fetch_sync)

    def _fetch_sync(self) -> object:
        request = urllib.request.Request(  # noqa: S310 - the scheme is checked in __init__
            self._url, headers={"Accept": "application/json", "User-Agent": "harness-auth"}
        )
        with self._opener.open(request, timeout=self._timeout) as response:
            if response.status != 200:
                raise OSError(f"JWKS endpoint returned HTTP {response.status}")
            body: bytes = response.read(self._max_bytes + 1)
        if len(body) > self._max_bytes:
            raise ValueError("JWKS document is too large")
        return json.loads(body)
