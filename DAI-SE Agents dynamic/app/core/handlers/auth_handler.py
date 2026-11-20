"""Single sign-on orchestration: Entra tokens in, application session out.

The login path is deliberately fan-out shaped. Verifying the id token, reading the
Graph profile and checking group membership are independent, so all three run
concurrently and the sign-in costs roughly one network round trip rather than three.
Every blocking call (JWKS fetch, Postgres, Redis) is pushed off the event loop.
"""

import asyncio

from app.config.constants import Roles
from app.config.exceptions import ForbiddenError, UnauthorizedError
from app.core.handlers.auth_session_handler import auth_session_handler, effective_roles
from app.core.handlers.group_role_handler import group_role_handler
from app.core.handlers.user_handler import user_handler
from app.core.models.auth_model import AuthPrincipal, IssuedSession, SessionUser
from app.utilities import graph_util as graph
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def strongest_role(roles: list[str]) -> str | None:
    """Pick the highest-precedence role out of a stored role list.

    Args:
        roles: Roles previously granted to the user.

    Returns:
        The strongest recognised role, or ``None`` when the list grants nothing.
    """
    held = set(roles or [])
    return next((r for r in Roles.PRECEDENCE if r in held), None)


def to_session_user(session: IssuedSession) -> SessionUser:
    """Shape the client-facing user block of an auth response.

    Args:
        session: The freshly issued session.

    Returns:
        The safe-to-render user summary.
    """
    p = session.principal
    return SessionUser(user_id=p.user_id, username=p.username,
                       display_name=p.display_name, email=p.email,
                       role=p.role, roles=p.roles, session_id=session.session_id)


class AuthHandler:
    """Turns Entra ID tokens into an application session, and keeps it alive."""

    async def _assigned_groups(self, access_token: str) -> tuple[list[str], dict[str, str]]:
        """Read the group->role mapping and ask Graph which of those groups apply.

        Args:
            access_token: Graph access token supplied by the front end.

        Returns:
            Tuple of the group ids the user belongs to and the full mapping.
        """
        # Cached in Redis, so this is normally a sub-millisecond read, not a query.
        mapping = await asyncio.to_thread(group_role_handler.get_mapping)
        assigned = await graph.check_member_groups(access_token, list(mapping))
        return assigned, mapping

    async def login(self, access_token: str, id_token: str) -> IssuedSession:
        """Authenticate an Entra sign-in and open an application session.

        Args:
            access_token: Access token scoped to Microsoft Graph.
            id_token: Id token whose audience is this application.

        Returns:
            The issued session, including the opaque handles for the client.

        Raises:
            UnauthorizedError: A token is invalid, or the two tokens describe
                different principals.
            ForbiddenError: The user is in no mapped group, or is deactivated here.
            UpstreamError: The identity provider could not be reached.
        """
        log.info("sso login started")
        # JWKS verification, the profile read and the group check are independent.
        # return_exceptions keeps a fast failure from orphaning the other two calls;
        # the first error is then re-raised, so token problems outrank Graph outages.
        results = await asyncio.gather(
            asyncio.to_thread(graph.verify_id_token, id_token),
            graph.fetch_me(access_token),
            self._assigned_groups(access_token),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        id_claims, profile, (assigned, mapping) = results

        oid = profile.get("id")
        if not oid:
            log.error("graph profile carried no object id")
            raise UnauthorizedError("could not identify the signed-in user")

        # Bind the two tokens together: an id token for one user must never be paired
        # with an access token for another.
        claimed_oid = id_claims.get("oid") or id_claims.get("sub")
        if id_claims.get("oid") and claimed_oid != oid:
            log.warning("id token oid '%s' does not match graph oid '%s'",
                        claimed_oid, oid)
            raise UnauthorizedError("identity tokens do not match")

        username = profile.get("userPrincipalName") or profile.get("mail") or oid
        email = profile.get("mail") or profile.get("userPrincipalName")

        role = group_role_handler.highest_role(assigned, mapping)
        if not role:
            # No mapped group means no entitlement: the directory is the gate.
            log.warning("login denied for '%s': no mapped group membership", username)
            raise ForbiddenError("you are not authorised to use this application")

        roles = effective_roles(role)
        record = await asyncio.to_thread(
            user_handler.upsert_sso_user, entra_oid=oid, username=username,
            email=email, display_name=profile.get("displayName"), roles=roles)
        if not record.is_active:
            log.warning("login denied for '%s': account deactivated locally", username)
            raise ForbiddenError("your account is disabled")

        principal = AuthPrincipal(user_id=record.id, username=record.username,
                                  display_name=record.display_name, email=record.email,
                                  entra_oid=oid, role=role, roles=roles)
        session = await asyncio.to_thread(auth_session_handler.create, principal)
        log.info("sso login succeeded for user %s ('%s') as '%s'",
                 record.id, username, role)
        return session

    async def refresh(self, refresh_handle: str) -> IssuedSession:
        """Rotate a session using its single-use refresh handle.

        Entitlements are re-read from the local user row, so an administrator's change
        takes effect on the next refresh rather than only on the next sign-in.

        Args:
            refresh_handle: The opaque refresh handle held by the client.

        Returns:
            A new session with fresh handles and the original absolute deadline.

        Raises:
            UnauthorizedError: The handle is unknown, expired, replayed or superseded.
            ForbiddenError: The account has since been disabled or stripped of roles.
        """
        record = await asyncio.to_thread(auth_session_handler.consume_refresh,
                                         refresh_handle)
        if not record:
            log.info("refresh rejected: handle is not valid")
            raise UnauthorizedError("invalid or expired refresh token")

        user = await asyncio.to_thread(user_handler.get_by_id, int(record["user_id"]))
        if not user or not user.is_active:
            log.warning("refresh denied for user %s: account inactive",
                        record.get("user_id"))
            raise ForbiddenError("your account is disabled")

        role = strongest_role(user.roles)
        if not role:
            log.warning("refresh denied for user %s: no roles remain", user.id)
            raise ForbiddenError("you are not authorised to use this application")

        principal = AuthPrincipal(user_id=user.id, username=user.username,
                                  display_name=user.display_name, email=user.email,
                                  entra_oid=user.entra_oid, role=role,
                                  roles=effective_roles(role))
        # login_at and session_id are carried over: rotating handles must not extend
        # the absolute re-login deadline, nor split one login across two identities.
        session = await asyncio.to_thread(
            auth_session_handler.create, principal,
            login_at=float(record["login_at"]), session_id=record["session_id"])
        log.info("session %s refreshed for user %s", session.session_id, user.id)
        return session

    async def logout(self, session_handle: str) -> bool:
        """End the caller's session.

        Args:
            session_handle: The opaque session handle presented as the bearer token.

        Returns:
            True when a live session was terminated.
        """
        ended = await asyncio.to_thread(auth_session_handler.revoke, session_handle)
        log.info("logout processed (session ended=%s)", ended)
        return ended


auth_handler = AuthHandler()
