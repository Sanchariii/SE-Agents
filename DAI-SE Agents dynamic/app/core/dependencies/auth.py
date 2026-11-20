"""Auth dependencies: a live session, role checks, and per-session ownership.

The bearer credential is an opaque session handle, not a JWT. Every request exchanges
it for the signed claims held in Redis, which costs one cached lookup but means a
revoked or superseded session stops working immediately instead of at token expiry.
"""

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import Depends, Header, HTTPException, Path

from app.config.constants import JwtConstants, Roles
from app.config.settings import get_settings
from app.core.handlers.auth_session_handler import auth_session_handler
from app.core.handlers.session_registry_handler import session_registry_handler
from app.utilities.logger_util import get_logger, user_email_ctx, user_id_ctx

log = get_logger(__name__)


def bearer_handle(authorization: str | None) -> str | None:
    """Extract the opaque session handle from an Authorization header.

    Args:
        authorization: Raw header value, if the client sent one.

    Returns:
        The handle, or ``None`` when the header is missing or not a bearer scheme.
    """
    if not authorization or not authorization.lower().startswith(JwtConstants.BEARER_PREFIX):
        return None
    return authorization.split(" ", 1)[1].strip() or None


async def require_auth(authorization: str | None = Header(default=None)) -> dict:
    """Resolve the caller's session handle into verified claims.

    Disabling auth is only possible in an explicitly configured non-production setup
    (``JWT_REQUIRED=false``).

    Args:
        authorization: The ``Authorization: Bearer <session handle>`` header.

    Returns:
        The claims of the live session.

    Raises:
        HTTPException: 401 when no live session backs the presented handle.
    """
    if not get_settings().jwt_required:
        claims = {"sub": "0", "username": "anonymous", "role": Roles.ADMIN,
                  "roles": list(Roles.ALL)}
        user_id_ctx.set(claims["sub"])
        user_email_ctx.set("")
        return claims

    handle = bearer_handle(authorization)
    if not handle:
        raise HTTPException(status_code=401, detail="missing bearer token")

    # Redis and JWT verification are synchronous, so they run off the event loop.
    claims = await asyncio.to_thread(auth_session_handler.resolve, handle)
    if not claims:
        # One message for unknown, expired, revoked and superseded sessions alike.
        log.info("session token rejected")
        raise HTTPException(status_code=401, detail="invalid or expired session")

    user_id_ctx.set(str(claims.get("sub", "-")))
    user_email_ctx.set(claims.get("email") or "")
    return claims


async def current_handle(authorization: str | None = Header(default=None)) -> str:
    """Return the raw session handle, for the routes that must revoke it.

    Args:
        authorization: The ``Authorization: Bearer <session handle>`` header.

    Returns:
        The presented handle.

    Raises:
        HTTPException: 401 when no bearer credential was sent.
    """
    handle = bearer_handle(authorization)
    if not handle:
        raise HTTPException(status_code=401, detail="missing bearer token")
    return handle


def require_role(*roles: str):
    """Dependency factory: caller must hold at least one of `roles`."""
    async def checker(claims: dict = Depends(require_auth)) -> dict:
        if not set(claims.get("roles") or []).intersection(roles):
            raise HTTPException(status_code=403,
                                detail=f"requires role: {', '.join(roles)}")
        return claims
    return checker


async def require_session_access(session_id: str = Path(...),
                                 claims: dict = Depends(require_auth)) -> dict:
    """Session ids are guessable, so every per-session route must prove ownership.
    Admins may act on any session; everyone else only on their own."""
    if Roles.ADMIN in (claims.get("roles") or []):
        return claims
    owns = await asyncio.to_thread(session_registry_handler.owns,
                                   int(claims["sub"]), session_id)
    if not owns:
        raise HTTPException(status_code=404, detail="session not found")
    return claims


async def require_session_owner(session_id: str = Path(...),
                                claims: dict = Depends(require_auth)) -> dict:
    """Require the caller to own a session, without an administrator bypass.

    Args:
        session_id: Session id supplied by the route path.
        claims: Authenticated caller claims.

    Returns:
        The authenticated claims when the caller owns the session.

    Raises:
        HTTPException: 404 when the caller does not own the session.
    """
    owns = await asyncio.to_thread(
        session_registry_handler.owns, int(claims["sub"]), session_id)
    if not owns:
        raise HTTPException(status_code=404, detail="session not found")
    return claims


def require_session_role(*roles: str):
    """Ownership plus a role — used by the mutating deck routes."""
    async def checker(claims: dict = Depends(require_session_access)) -> dict:
        if not set(claims.get("roles") or []).intersection(roles):
            raise HTTPException(status_code=403,
                                detail=f"requires role: {', '.join(roles)}")
        return claims
    return checker


def require_session_owner_role(
    *roles: str,
) -> Callable[..., Awaitable[dict]]:
    """Build a dependency requiring strict ownership and one allowed role.

    Args:
        roles: Any application role that permits the operation.

    Returns:
        A FastAPI dependency that validates strict ownership and role membership.
    """
    async def checker(claims: dict = Depends(require_session_owner)) -> dict:
        """Validate the role after strict session ownership succeeds.

        Args:
            claims: Claims already validated by ``require_session_owner``.

        Returns:
            The authenticated caller claims.

        Raises:
            HTTPException: 403 when the caller lacks every allowed role.
        """
        if not set(claims.get("roles") or []).intersection(roles):
            raise HTTPException(status_code=403,
                                detail=f"requires role: {', '.join(roles)}")
        return claims
    return checker
