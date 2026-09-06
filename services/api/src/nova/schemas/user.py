"""User payloads."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserRead(BaseModel):
    """A user as returned to clients. Never includes the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class UserUpdate(BaseModel):
    """Fields a user may change about themselves."""

    display_name: str | None = Field(default=None, min_length=1, max_length=80)
