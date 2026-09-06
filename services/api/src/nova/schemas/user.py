"""User payloads."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from nova.core.timezones import is_valid


class UserRead(BaseModel):
    """A user as returned to clients. Never includes the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str
    timezone: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class UserUpdate(BaseModel):
    """Fields a user may change about themselves."""

    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        """Reject a name the database cannot resolve.

        This has to fail here rather than at query time: the name goes into an
        ``AT TIME ZONE`` clause, so an unknown one would turn every later
        analytics request for this account into a 500.
        """
        if value is None:
            return None
        if not is_valid(value):
            raise ValueError("Unknown timezone. Use an IANA name such as 'Europe/Bucharest'.")
        return value
