"""User domain models. Identities are federated, so nothing here holds a credential."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class User(BaseModel):
    """A locally mirrored directory user, as exposed by the API."""
    id: int
    username: str                                   # userPrincipalName from Entra
    email: Optional[str] = None
    display_name: Optional[str] = None
    roles: list[str] = Field(default_factory=list)  # last roles granted at sign-in
    is_active: bool = True
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None


class UserRecord(User):
    """Internal row. ``entra_oid`` lives here rather than on ``User`` so the directory
    object id is available to the login flow but never serialised into a response."""
    entra_oid: Optional[str] = None
