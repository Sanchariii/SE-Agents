"""Server-side login sessions in Redis, addressed by opaque handles.

Why not hand the JWT to the browser: a signed token is valid until it expires and
cannot be withdrawn. Here the browser holds only an opaque handle; the JWT lives in
Redis behind the SHA-256 of that handle. Deleting the Redis entry therefore ends the
session immediately, which is what makes "one active session per user" and remote
logout work. Expiry is enforced twice — by the Redis TTL and by the JWT's own ``exp``
— so the local in-memory Redis fallback (which ignores TTLs) cannot extend a session.
"""

import time
import uuid
from datetime import datetime, timezone

from app.config.constants import AuthConstants, RedisKeys, Roles
from app.core.models.auth_model import AuthPrincipal, IssuedSession
from app.utilities.jwt_util import JwtUtil
from app.utilities.logger_util import get_logger
from app.utilities.redis_util import RedisUtil, get_redis
from app.utilities.security_util import hash_handle, new_handle

log = get_logger(__name__)


def effective_roles(role: str) -> list[str]:
    """Expand a single role into every role it implies.

    Args:
        role: The strongest role the user holds.

    Returns:
        All roles granted, so existing ``require_role`` checks keep working unchanged.
    """
    return list(Roles.IMPLIED.get(role, (role,)))


class AuthSessionHandler:
    """Creates, resolves, refreshes and revokes login sessions."""

    def __init__(self, redis: RedisUtil | None = None,
                 jwt_util: JwtUtil | None = None) -> None:
        self._redis = redis
        self._jwt = jwt_util

    # ------------------------------------------------------------------ wiring #

    def _r(self) -> RedisUtil:
        if self._redis is None:
            self._redis = get_redis()
        return self._redis

    def _j(self) -> JwtUtil:
        if self._jwt is None:
            self._jwt = JwtUtil()
        return self._jwt

    @staticmethod
    def _session_key(handle: str) -> str:
        return RedisKeys.AUTH_SESSION.format(token_hash=hash_handle(handle))

    @staticmethod
    def _refresh_key(handle: str) -> str:
        return RedisKeys.AUTH_REFRESH.format(token_hash=hash_handle(handle))

    @staticmethod
    def _used_key(handle: str) -> str:
        return RedisKeys.AUTH_REFRESH_USED.format(token_hash=hash_handle(handle))

    @staticmethod
    def _active_key(user_id: int) -> str:
        return RedisKeys.AUTH_USER_ACTIVE.format(user_id=user_id)

    # ------------------------------------------------------------------- create #

    def create(self, principal: AuthPrincipal, *, login_at: float | None = None,
               session_id: str | None = None) -> IssuedSession:
        """Issue a new session, replacing whatever session the user held before.

        Args:
            principal: The authenticated user and their effective roles.
            login_at: Epoch seconds of the original sign-in. Passed on refresh so the
                absolute re-login deadline is not extended by rotating handles.
            session_id: Reused on refresh so a login keeps one identity in the logs.

        Returns:
            The issued session, including the handles to return to the client.
        """
        now = time.time()
        login_at = login_at or now
        session_id = session_id or uuid.uuid4().hex

        # Absolute cap: refreshing may never push the deadline past the original login.
        refresh_deadline = login_at + AuthConstants.REFRESH_TTL_SECONDS
        refresh_ttl = int(refresh_deadline - now)
        if refresh_ttl <= 0:
            raise ValueError("refresh window has already elapsed")

        token, payload = self._j().issue_access(
            subject=str(principal.user_id),
            username=principal.username,
            roles=principal.roles,
            session_id=session_id,
            extra={"role": principal.role,
                   "email": principal.email,
                   "display_name": principal.display_name,
                   "oid": principal.entra_oid},
        )

        session_handle = new_handle(principal.user_id)
        refresh_handle = new_handle(principal.user_id)
        # Never let a rotated session outlive the absolute deadline: near the end of the
        # window the remaining refresh time is shorter than a full access-token life.
        session_ttl = max(1, min(int(payload["exp"]) - int(now), refresh_ttl))
        expires_at = int(now) + session_ttl

        record = {
            "session_id": session_id,
            "user_id": principal.user_id,
            "username": principal.username,
            "display_name": principal.display_name,
            "email": principal.email,
            "entra_oid": principal.entra_oid,
            "role": principal.role,
            "roles": principal.roles,
            "jwt": token,
            "login_at": login_at,
            "issued_at": now,
            "expires_at": expires_at,
            "refresh_hash": hash_handle(refresh_handle),
        }
        refresh_record = {
            "session_id": session_id,
            "user_id": principal.user_id,
            "login_at": login_at,
            "expires_at": refresh_deadline,
            "session_hash": hash_handle(session_handle),
        }

        # Terminate the previous login first: a user may hold exactly one session, so
        # signing in elsewhere must invalidate the older handles immediately.
        self.revoke_user(principal.user_id, reason="superseded by a new sign-in")

        self._r().set_json_ex(self._session_key(session_handle), record, session_ttl)
        self._r().set_json_ex(self._refresh_key(refresh_handle), refresh_record,
                              refresh_ttl)
        # Written last so the pointer never names a session that does not exist yet.
        self._r().set_json_ex(self._active_key(principal.user_id),
                              {"session_hash": hash_handle(session_handle),
                               "refresh_hash": hash_handle(refresh_handle),
                               "session_id": session_id},
                              refresh_ttl)

        log.info("session issued for user %s (session_id=%s, role=%s, ttl=%ss)",
                 principal.user_id, session_id, principal.role, session_ttl)
        return IssuedSession(
            session_token=session_handle,
            refresh_token=refresh_handle,
            session_id=session_id,
            expires_in=session_ttl,
            expires_at=datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
            refresh_expires_in=refresh_ttl,
            principal=principal,
        )

    # ------------------------------------------------------------------ resolve #

    def resolve(self, session_handle: str) -> dict | None:
        """Exchange a session handle for verified JWT claims.

        Args:
            session_handle: The opaque handle presented as the bearer token.

        Returns:
            The token claims, or ``None`` when the session is unknown, revoked or
            expired. The reason is deliberately not reported to the caller.
        """
        record = self._r().get_json(self._session_key(session_handle))
        if not record:
            return None

        # Second expiry gate: the in-memory Redis fallback ignores TTLs.
        if float(record.get("expires_at", 0)) <= time.time():
            self._r().delete(self._session_key(session_handle))
            log.info("session %s expired", record.get("session_id"))
            return None

        try:
            # Verifies signature, issuer and exp — a tampered Redis value is rejected.
            claims = self._j().decode(record["jwt"])
        except Exception as exc:                    # noqa: BLE001
            log.warning("stored token for session %s failed verification: %s",
                        record.get("session_id"), exc)
            self._r().delete(self._session_key(session_handle))
            return None

        # Revocation deletes this key outright, so its mere presence proves the session
        # is current. Re-reading the user's active pointer here would add a second Redis
        # round trip to every request and would sign a user out if that pointer were
        # evicted while their session key survived.
        return claims

    # ------------------------------------------------------------------ refresh #

    def consume_refresh(self, refresh_handle: str) -> dict | None:
        """Validate a refresh handle, burn it, and detect replay of a burned one.

        Refresh handles are single use. After a handle is spent it is remembered as a
        session-scoped tombstone: presenting it again while that same login is still
        active is treated as theft (RFC 6819 refresh-token reuse) and terminates the
        whole session family. A replay of a handle whose login has already ended is
        merely rejected, so a dead token cannot be used to force-log-out a new session.

        Args:
            refresh_handle: The opaque refresh handle presented by the client.

        Returns:
            The refresh record when it was valid and unused, otherwise ``None``.
        """
        # Reuse detection first: only the real random handle can hash to this key.
        tombstone = self._r().get_json(self._used_key(refresh_handle))
        if tombstone:
            user_id = int(tombstone["user_id"])
            active = self._r().get_json(self._active_key(user_id)) or {}
            if active.get("session_id") == tombstone.get("session_id"):
                # The compromised handle belongs to the live login: revoke everything.
                log.warning("refresh token reuse detected for user %s; "
                            "revoking session family", user_id)
                self.revoke_user(user_id, reason="refresh token reuse detected")
            else:
                # Handle from an already-ended login; reject without collateral damage.
                log.info("replay of a refresh handle from an ended session; ignoring")
            return None

        key = self._refresh_key(refresh_handle)
        record = self._r().get_json(key)
        if not record:
            return None
        if float(record.get("expires_at", 0)) <= time.time():
            self._r().delete(key)
            log.info("refresh handle for session %s expired", record.get("session_id"))
            return None

        active = self._r().get_json(self._active_key(record["user_id"])) or {}
        if active.get("refresh_hash") != hash_handle(refresh_handle):
            # Belongs to a login that has already been superseded elsewhere.
            self._r().delete(key)
            log.warning("refresh handle for user %s is no longer the active one",
                        record.get("user_id"))
            return None

        # Single use: burn the handle and leave a session-scoped tombstone so a later
        # replay trips the reuse check above. The tombstone lives no longer than the
        # refresh window it came from, then expires on its own.
        ttl = max(1, int(float(record["expires_at"]) - time.time()))
        self._r().delete(key)
        self._r().set_json_ex(self._used_key(refresh_handle),
                              {"user_id": record["user_id"],
                               "session_id": record.get("session_id")}, ttl)
        return record

    # ------------------------------------------------------------------- revoke #

    def revoke(self, session_handle: str) -> bool:
        """End the session identified by a handle.

        Args:
            session_handle: The opaque handle presented as the bearer token.

        Returns:
            True when a live session was removed.
        """
        record = self._r().get_json(self._session_key(session_handle))
        self._r().delete(self._session_key(session_handle))
        if not record:
            return False
        self.revoke_user(int(record["user_id"]), reason="logout")
        return True

    def revoke_user(self, user_id: int, *, reason: str = "revoked") -> None:
        """Drop a user's active session and its refresh handle.

        Args:
            user_id: Internal id of the user whose session should end.
            reason: Logged explanation (logout, superseded, deactivated, ...).
        """
        active = self._r().get_json(self._active_key(user_id))
        if not active:
            return
        if active.get("session_hash"):
            self._r().delete(
                RedisKeys.AUTH_SESSION.format(token_hash=active["session_hash"]))
        if active.get("refresh_hash"):
            self._r().delete(
                RedisKeys.AUTH_REFRESH.format(token_hash=active["refresh_hash"]))
        self._r().delete(self._active_key(user_id))
        log.info("session %s for user %s terminated (%s)",
                 active.get("session_id"), user_id, reason)


auth_session_handler = AuthSessionHandler()
