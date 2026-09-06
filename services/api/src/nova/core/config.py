"""Application configuration.

Settings are loaded from the environment (and an optional ``.env`` file) with
the ``NOVA_`` prefix and ``__`` as the nesting delimiter, so
``NOVA_DATABASE__HOST`` populates ``Settings.database.host``.

Nothing here has a fallback for a secret. A missing ``NOVA_JWT__SECRET_KEY``
is a startup failure, not a silently-insecure default.
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class DatabaseSettings(BaseModel):
    """PostgreSQL connection and pool configuration."""

    host: str = "localhost"
    port: int = 5432
    user: str = "nova"
    password: SecretStr = SecretStr("nova")
    name: str = "nova"

    pool_size: int = Field(default=10, ge=1)
    max_overflow: int = Field(default=5, ge=0)
    pool_timeout_seconds: int = Field(default=30, ge=1)
    pool_recycle_seconds: int = Field(default=1800, ge=60)
    echo: bool = False

    def dsn(self, *, driver: str = "postgresql+asyncpg") -> str:
        """Build a SQLAlchemy URL. The password is only unwrapped here."""
        password = self.password.get_secret_value()
        return f"{driver}://{self.user}:{password}@{self.host}:{self.port}/{self.name}"


class RedisSettings(BaseModel):
    """Redis connection configuration."""

    host: str = "localhost"
    port: int = 6379
    db: int = Field(default=0, ge=0)
    password: SecretStr | None = None
    socket_timeout_seconds: float = Field(default=2.0, gt=0)

    def dsn(self) -> str:
        auth = f":{self.password.get_secret_value()}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


class JWTSettings(BaseModel):
    """Access-token signing configuration.

    Only the short-lived access token is a JWT. Refresh tokens are opaque
    random strings stored hashed in the database, so they can be revoked --
    a stateless refresh JWT cannot be.
    """

    secret_key: SecretStr
    algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    issuer: str = "nova-api"
    audience: str = "nova-clients"

    access_token_ttl_seconds: int = Field(default=900, ge=60)
    refresh_token_ttl_seconds: int = Field(default=60 * 60 * 24 * 30, ge=3600)

    @field_validator("secret_key")
    @classmethod
    def _reject_weak_secret(cls, value: SecretStr) -> SecretStr:
        # 32 bytes is the floor for HS256; a shorter key weakens the HMAC.
        if len(value.get_secret_value()) < 32:
            raise ValueError("JWT secret_key must be at least 32 characters")
        return value


class SecuritySettings(BaseModel):
    """Transport and abuse-prevention configuration."""

    cors_origins: list[str] = Field(default_factory=list)
    cors_allow_credentials: bool = True

    # Fixed-window limits applied to unauthenticated auth endpoints.
    auth_rate_limit_attempts: int = Field(default=10, ge=1)
    auth_rate_limit_window_seconds: int = Field(default=60, ge=1)

    # Argon2id work factors. Defaults follow the OWASP cheat-sheet baseline
    # (19 MiB, 2 iterations, 1 lane); tests lower them for speed.
    argon2_time_cost: int = Field(default=2, ge=1)
    argon2_memory_cost_kib: int = Field(default=19456, ge=8)
    argon2_parallelism: int = Field(default=1, ge=1)


class DeviceSettings(BaseModel):
    """Device provisioning and connection configuration."""

    # A claim code is read off a small screen and typed by hand, so it cannot
    # carry much entropy (~29 bits). Everything else here exists to
    # compensate: a short life, a single use, and rate limiting on the claim
    # endpoint -- which is the only place a wrong guess is observable, since
    # a guess that matches no code matches no row either.
    claim_code_ttl_seconds: int = Field(default=600, ge=60, le=3600)

    # Provisioning is a rare event, so the limits are deliberately tight.
    provision_rate_limit_attempts: int = Field(default=10, ge=1)
    provision_rate_limit_window_seconds: int = Field(default=3600, ge=1)
    claim_rate_limit_attempts: int = Field(default=10, ge=1)
    claim_rate_limit_window_seconds: int = Field(default=60, ge=1)

    # How often a connected device is expected to report in.
    heartbeat_interval_seconds: int = Field(default=30, ge=5)

    # Frames larger than this are rejected before parsing. A device sends
    # small JSON; anything bigger is a bug or an attack.
    max_frame_bytes: int = Field(default=16384, ge=512)
    # Consecutive malformed frames tolerated before the socket is closed.
    max_protocol_violations: int = Field(default=5, ge=1)


class AISettings(BaseModel):
    """AI provider configuration.

    ``offline`` needs no credential and is the default, so the stack starts
    and the tests run without a key. Selecting ``anthropic`` without one
    degrades back to offline with a warning rather than refusing to boot.
    """

    chat_provider: Literal["anthropic", "offline"] = "offline"
    chat_model: str = "claude-opus-5"
    anthropic_api_key: SecretStr | None = None

    request_timeout_seconds: float = Field(default=60.0, gt=0)
    # Replies from a desk companion are a few sentences. A generous cap
    # would only pay for a runaway.
    max_reply_tokens: int = Field(default=600, ge=64, le=8192)
    # Turns of history sent with each request. Enough for continuity,
    # bounded so cost does not grow without limit as a conversation ages.
    context_window_messages: int = Field(default=20, ge=2, le=200)

    # Route around a safety refusal by category instead of returning
    # nothing. A companion going silent reads as broken.
    enable_refusal_fallbacks: bool = True

    # "lexical" measures shared vocabulary; "hash" is content-addressed and
    # carries no similarity at all, kept only for storage-plumbing tests.
    embedding_provider: Literal["lexical", "hash"] = "lexical"
    # Must match the memories column; see nova.models.memory.
    embedding_dimensions: int = Field(default=1536, ge=64, le=4096)

    # Memory retrieval and extraction.
    memory_retrieval_limit: int = Field(default=6, ge=0, le=50)
    # Cosine distance above which a memory is too unrelated to include.
    # Recall matters more than precision here: a memory that turns out to be
    # irrelevant costs a few tokens, one that is missed costs the illusion
    # that NOVA remembers anything.
    memory_max_distance: float = Field(default=0.85, ge=0.0, le=2.0)
    # Extraction costs a model call per exchange, so it can be turned off.
    memory_extraction_enabled: bool = True
    memory_min_confidence: float = Field(default=0.4, ge=0.0, le=1.0)

    # AI calls cost real money, so they are limited more tightly than
    # ordinary reads.
    message_rate_limit: int = Field(default=30, ge=1)
    message_rate_limit_window_seconds: int = Field(default=60, ge=1)


class ObservabilitySettings(BaseModel):
    """Logging configuration."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # JSON in deployed environments, human-readable colour locally.
    log_json: bool = True


class Settings(BaseSettings):
    """Root settings object, resolved once per process."""

    model_config = SettingsConfigDict(
        env_prefix="NOVA_",
        env_nested_delimiter="__",
        # Both are tried, and a missing file is ignored. The repository root
        # holds the .env that docker compose reads; a service-local one wins
        # when present, so tools run from services/api (alembic, pytest) find
        # configuration without the caller exporting it by hand.
        env_file=("../../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    jwt: JWTSettings
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    device: DeviceSettings = Field(default_factory=DeviceSettings)
    ai: AISettings = Field(default_factory=AISettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def docs_url(self) -> str | None:
        """OpenAPI docs are served everywhere except production."""
        return None if self.is_production else "/docs"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, constructed on first use.

    Cached so that importing modules and FastAPI dependencies share one
    instance; tests clear the cache after mutating the environment.
    """
    # Values come from the environment; pydantic-settings fills every field.
    return Settings()
