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
