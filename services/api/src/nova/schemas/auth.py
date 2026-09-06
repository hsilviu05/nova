"""Authentication payloads."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator

from nova.schemas.user import UserRead

# Long enough to resist offline guessing, capped because Argon2 hashing cost
# grows with input and an unbounded password is a cheap DoS vector.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128


class RegisterRequest(BaseModel):
    """New-account payload."""

    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("password")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Password must not be blank.")
        return value

    @field_validator("display_name")
    @classmethod
    def _strip_display_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Display name must not be blank.")
        return stripped


class LoginRequest(BaseModel):
    """Credential payload."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class RefreshRequest(BaseModel):
    """Rotation payload."""

    refresh_token: str = Field(min_length=1, max_length=512)


class LogoutRequest(BaseModel):
    """Revocation payload."""

    refresh_token: str = Field(min_length=1, max_length=512)


class TokenPair(BaseModel):
    """Issued credentials.

    ``refresh_token`` is shown exactly once; the server keeps only a digest.
    """

    access_token: str
    refresh_token: str
    # S105 is a false positive: "bearer" is the OAuth 2.0 token_type
    # literal, not a credential.
    token_type: str = "bearer"  # noqa: S105
    expires_in: int = Field(description="Access-token lifetime in seconds.")


class AuthenticatedUser(BaseModel):
    """Token pair plus the account it belongs to."""

    user: UserRead
    tokens: TokenPair
