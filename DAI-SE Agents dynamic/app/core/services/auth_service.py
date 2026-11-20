"""Single-sign-on endpoints: login, refresh, validate, logout, me.

The front end exchanges its Entra tokens here once and then works with an opaque
session handle. ``/validate`` is built to be polled every few seconds: it costs one
cached lookup and reports how long the session has left, so the client can refresh
before it lapses instead of discovering the expiry mid-request.
"""

import asyncio
import time

from fastapi import APIRouter, Depends

from app.config.constants import AuthConstants, RouteTags
from app.config.exceptions import NotFoundError
from app.core.dependencies.auth import current_handle, require_auth
from app.core.handlers.auth_handler import auth_handler, to_session_user
from app.core.handlers.user_handler import user_handler
from app.core.models.auth_model import (
    AuthResponse, IssuedSession, LogoutResponse, RefreshRequest, SessionUser,
    SsoLoginRequest, ValidateResponse, ValidationData,
)
from app.core.models.user_model import User
from app.utilities.logger_util import get_logger

log = get_logger(__name__)
auth_router = APIRouter(prefix="/auth", tags=[RouteTags.AUTH])


def _auth_response(session: IssuedSession, message: str) -> AuthResponse:
    """Shape a login or refresh result for the client.

    Only the opaque handles are returned; the signed token they stand for stays in
    Redis so the session can be revoked at any moment.

    Args:
        session: The freshly issued session.
        message: Human-readable outcome for the UI.

    Returns:
        The response body.
    """
    return AuthResponse(
        status=AuthConstants.STATUS_SUCCESS,
        message=message,
        data=to_session_user(session),
        token=session.session_token,
        refresh_token=session.refresh_token,
        expires_in=session.expires_in,
        expires_at=session.expires_at,
        refresh_expires_in=session.refresh_expires_in,
    )


@auth_router.post("/login", response_model=AuthResponse)
async def login(body: SsoLoginRequest) -> AuthResponse:
    """Exchange Entra ID tokens for an application session.

    Args:
        body: The access token (scoped to Graph) and the id token from MSAL.

    Returns:
        The session handles plus the resolved user and role.
    """
    log.info("login requested")
    session = await auth_handler.login(body.access_token, body.id_token)
    log.info("login issued session %s for user %s",
             session.session_id, session.principal.user_id)
    return _auth_response(session, AuthConstants.LOGIN_MESSAGE)


@auth_router.post("/refresh", response_model=AuthResponse)
async def refresh(body: RefreshRequest) -> AuthResponse:
    """Rotate an about-to-expire session using its single-use refresh handle.

    Args:
        body: The refresh handle issued with the current session.

    Returns:
        A new pair of handles; the previous pair is invalidated.
    """
    log.info("refresh requested")
    session = await auth_handler.refresh(body.refresh_token)
    log.info("refresh rotated session %s for user %s",
             session.session_id, session.principal.user_id)
    return _auth_response(session, AuthConstants.REFRESH_MESSAGE)


@auth_router.get("/validate", response_model=ValidateResponse)
async def validate(claims: dict = Depends(require_auth)) -> ValidateResponse:
    """Cheap liveness probe for the client's session, safe to poll every few seconds.

    Args:
        claims: Resolved by the auth dependency, which already rejects a dead session.

    Returns:
        Remaining lifetime and whether the client should refresh now. A 401 means the
        session is gone and the client should try ``/auth/refresh``.
    """
    expires_at = claims.get("exp") or (time.time() + AuthConstants.SESSION_TTL_SECONDS)
    remaining = max(0, int(float(expires_at) - time.time()))
    should_refresh = remaining <= AuthConstants.REFRESH_THRESHOLD_SECONDS
    # Debug, not info: the client polls this every few seconds per open tab.
    log.debug("session %s validated (expires_in=%ss, should_refresh=%s)",
              claims.get("sid"), remaining, should_refresh)
    user = SessionUser(
        user_id=int(claims["sub"]), username=claims.get("username", ""),
        display_name=claims.get("display_name"), email=claims.get("email"),
        role=claims.get("role", ""), roles=list(claims.get("roles") or []),
        session_id=claims.get("sid", ""),
    )
    return ValidateResponse(
        status=AuthConstants.STATUS_SUCCESS,
        message=AuthConstants.VALID_MESSAGE,
        data=ValidationData(
            valid=True, expires_in=remaining,
            should_refresh=should_refresh,
            user=user),
    )


@auth_router.post("/logout", response_model=LogoutResponse)
async def logout(handle: str = Depends(current_handle)) -> LogoutResponse:
    """End the caller's session everywhere.

    Args:
        handle: The opaque session handle taken from the Authorization header.

    Returns:
        Confirmation. Logging out an already-dead session is not an error, so a client
        can always reach a signed-out state.
    """
    await auth_handler.logout(handle)
    return LogoutResponse(status=AuthConstants.STATUS_SUCCESS,
                          message=AuthConstants.LOGOUT_MESSAGE)


@auth_router.get("/me", response_model=User)
async def me(claims: dict = Depends(require_auth)) -> User:
    """Return the caller's stored profile.

    Args:
        claims: Resolved by the auth dependency.

    Returns:
        The user row backing the session.
    """
    record = await asyncio.to_thread(user_handler.get_by_id, int(claims["sub"]))
    if not record:
        log.warning("profile requested for user %s, which no longer exists",
                    claims.get("sub"))
        raise NotFoundError("user not found")
    return User(**record.model_dump())
