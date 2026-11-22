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
  socket operation. `HttpJwksFetcher` also stops its own worker thread at a shorter deadline
  (8 s), so a slow endpoint cannot leave fetch threads running.
- If a refresh fails, or returns a document with no usable key, the last good keys stay in use
  for up to `max_stale_seconds` after they were fetched. With no usable keys (none fetched yet,
  or the last good ones too old), verification raises `JwksUnavailableError` (HTTP 503).
- Only asymmetric signature keys are kept: `RSA` (RS256, at least 2048 bits) and `EC` on P-256
  (ES256). A symmetric (`oct`) key is never used, even if the document lists one.
"""

import asyncio
import contextlib
import http.client
import json
import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
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

    @property
    def fetch_timeout_seconds(self) -> float:
        return self._fetch_timeout

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


class HttpJwksFetcher:
    """Fetches the JWKS over HTTPS (or plain HTTP for a local stack; config.py checks which).

    Bounds on one fetch:

    - `timeout_seconds` per socket operation (connect, each read).
    - `total_seconds` for the whole fetch. At the deadline a timer shuts the socket down, so the
      worker thread ends even when the server sends one byte just inside each socket timeout.
      Keep it below `JwksCache.fetch_timeout_seconds`, so that a thread never outlives the wait
      for it and fetch threads cannot pile up.
    - `max_bytes` for the body. Redirects are not followed (`http.client` never follows them).
    """

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 5.0,
        total_seconds: float = 8.0,
        max_bytes: int = 64 * 1024,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("JWKS URL must be an absolute http(s) URL")
        if timeout_seconds <= 0 or total_seconds <= 0 or max_bytes <= 0:
            raise ValueError("JWKS fetch limits must be positive")
        self._url = url
        self._timeout = timeout_seconds
        self._total = total_seconds
        self._max_bytes = max_bytes

    @property
    def total_seconds(self) -> float:
        return self._total

    async def fetch(self) -> object:
        return await asyncio.to_thread(self._fetch_sync)

    def _connection(self) -> http.client.HTTPConnection:
        parts = urlsplit(self._url)
        host, port = parts.hostname or "", parts.port
        if parts.scheme == "https":
            context = ssl.create_default_context()  # verifies the certificate and host name
            return http.client.HTTPSConnection(host, port, timeout=self._timeout, context=context)
        return http.client.HTTPConnection(host, port, timeout=self._timeout)

    def _fetch_sync(self) -> object:
        parts = urlsplit(self._url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        connection = self._connection()
        expired = threading.Event()
        # `getresponse()` can drop `connection.sock` while the response still reads from it,
        # so the timer keeps its own reference to the socket.
        opened: list[socket.socket] = []

        def stop() -> None:
            expired.set()
            for sock in opened:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)  # wakes a read that is blocked right now

        timer = threading.Timer(self._total, stop)
        timer.daemon = True
        timer.start()
        try:
            connection.connect()
            if connection.sock is not None:
                opened.append(connection.sock)
            if expired.is_set():  # the deadline passed while the socket was being opened
                raise TimeoutError("JWKS fetch exceeded its total deadline")
            connection.request(
                "GET", path, headers={"Accept": "application/json", "User-Agent": "harness-auth"}
            )
            response = connection.getresponse()
            if response.status != 200:
                raise OSError(f"JWKS endpoint returned HTTP {response.status}")
            body = response.read(self._max_bytes + 1)
        except Exception:
            if expired.is_set():
                raise TimeoutError("JWKS fetch exceeded its total deadline") from None
            raise
        finally:
            timer.cancel()
            connection.close()
        if expired.is_set():
            raise TimeoutError("JWKS fetch exceeded its total deadline")
        if len(body) > self._max_bytes:
            raise ValueError("JWKS document is too large")
        return json.loads(body)
