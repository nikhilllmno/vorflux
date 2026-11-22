"""Process settings, read once from the environment (variable names are in `.env.example`)."""

import os
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hosts of a local Supabase stack (`supabase start`), the only ones that may serve JWKS over http.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

AuthAlgorithm = Literal["ES256", "RS256"]


def _default_auth_algorithms() -> list[AuthAlgorithm]:
    return ["ES256", "RS256"]


class Settings(BaseSettings):
    """Non-secret behaviour plus references to secrets. Secret values never get defaults."""

    # `env_ignore_empty`: a variable copied empty from `.env.example` means "use the default".
    model_config = SettingsConfigDict(
        env_prefix="HARNESS_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    environment: Literal["local", "test", "staging", "production"] = "local"
    log_level: str = "INFO"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- Request authentication: Supabase Auth access tokens (docs/auth.md) ---
    # Auth is off until both the issuer and the JWKS URL are set; protected routes then return 503.
    auth_issuer: str | None = None
    auth_jwks_url: str | None = None
    auth_audience: str = Field(default="authenticated", min_length=1)
    # Asymmetric algorithms only. HS256 (the legacy shared secret) and `none` cannot be configured.
    auth_algorithms: list[AuthAlgorithm] = Field(
        default_factory=_default_auth_algorithms, min_length=1
    )
    auth_leeway_seconds: int = Field(default=10, ge=0, le=60)
    auth_jwks_cache_seconds: int = Field(default=600, ge=30, le=86_400)
    auth_jwks_min_refetch_seconds: int = Field(default=30, ge=1, le=3_600)

    @model_validator(mode="after")
    def _check_environment(self) -> Self:
        # Fly.io sets FLY_APP_NAME on every machine. `local` and `test` allow the in-memory
        # membership provider and an http JWKS URL, so a deployed machine must name its stage.
        if self.environment in ("local", "test") and os.environ.get("FLY_APP_NAME"):
            raise ValueError("set HARNESS_ENVIRONMENT to staging or production on Fly.io")
        return self

    @model_validator(mode="after")
    def _check_auth(self) -> Self:
        if (self.auth_issuer is None) != (self.auth_jwks_url is None):
            raise ValueError("set HARNESS_AUTH_ISSUER and HARNESS_AUTH_JWKS_URL together")
        if self.auth_issuer is not None and not self.auth_issuer.strip():
            raise ValueError("HARNESS_AUTH_ISSUER cannot be blank")
        if not self.auth_audience.strip():
            raise ValueError("HARNESS_AUTH_AUDIENCE cannot be blank")
        if self.auth_jwks_url is not None:
            _check_jwks_url(
                self.auth_jwks_url, allow_local_http=self.environment in ("local", "test")
            )
        return self


def _check_jwks_url(url: str, *, allow_local_http: bool) -> None:
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError("HARNESS_AUTH_JWKS_URL must be an absolute https URL")
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and allow_local_http and parts.hostname in _LOCAL_HOSTS:
        return
    raise ValueError(
        "HARNESS_AUTH_JWKS_URL must use https (http only for a local stack in local or test)"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
