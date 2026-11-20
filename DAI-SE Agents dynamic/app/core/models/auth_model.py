"""Auth request/response models for Entra ID single sign-on.

The client never receives the signed JWT. It receives an opaque handle
(``<user_id>:<uuid4 hex>``) that the server exchanges for the JWT on every request,
which is what makes remote revocation — and the one-session-per-user rule — possible.
"""

from typing import Optional

from pydantic import BaseModel, Field


class SsoLoginRequest(BaseModel):
    """Tokens obtained by the front end from MSAL after an Entra sign-in."""
    access_token: str = Field(min_length=1)         # scoped to Microsoft Graph
    id_token: str = Field(min_length=1)             # audience = our app registration


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class AuthPrincipal(BaseModel):
    """Everything the service knows about the signed-in user, resolved at login."""
    user_id: int
    username: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    entra_oid: Optional[str] = None
    role: str                                       # the single strongest role held
    roles: list[str] = Field(default_factory=list)  # `role` plus everything it implies


class IssuedSession(BaseModel):
    """A freshly minted session. ``jwt`` stays server-side and is never serialised out."""
    session_token: str
    refresh_token: str
    session_id: str
    expires_in: int                                 # seconds until the session token dies
    expires_at: str                                 # ISO-8601, UTC
    refresh_expires_in: int                         # seconds until re-login is required
    principal: AuthPrincipal


class SessionUser(BaseModel):
    """The `data` block of an auth response — safe to render in the UI."""
    user_id: int
    username: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: str
    roles: list[str] = Field(default_factory=list)
    session_id: str


class AuthResponse(BaseModel):
    """Login and refresh share one shape so the front end can handle them identically."""
    status: str
    message: str
    data: SessionUser
    token: str                                      # opaque session handle
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    expires_at: str
    refresh_expires_in: int


class ValidationData(BaseModel):
    valid: bool
    expires_in: int
    should_refresh: bool                            # true once inside the refresh window
    user: SessionUser


class ValidateResponse(BaseModel):
    status: str
    message: str
    data: ValidationData


class LogoutResponse(BaseModel):
    status: str
    message: str
    logged_out: bool = True
