"""RS256 JWTs: issue with the private key, validate with the public key.

One service now issues and validates, so both keys live here. No ``aud`` claim is
set, which keeps validation a bare ``jwt.decode(token, public_key, algorithms=...)``.
"""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

from app.config.constants import JwtConstants
from app.config.settings import get_settings
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


class JwtUtil:
    def __init__(self) -> None:
        self.algorithm = JwtConstants.ALGORITHM
        self.issuer = JwtConstants.ISSUER
        self.access_ttl = timedelta(minutes=JwtConstants.ACCESS_TOKEN_TTL_MINUTES)
        # Keys come from the CSI secrets-store mount when hosted, app/config locally.
        settings = get_settings()
        self._private = self._load(settings.jwt_private_key_path)
        self._public = self._load(settings.jwt_public_key_path)

    @staticmethod
    def _load(path: str) -> str | None:
        p = Path(path)
        return p.read_text(encoding="utf-8") if p.exists() else None

    @property
    def configured(self) -> bool:
        return bool(self._private and self._public)

    def issue_access(self, *, subject: str, username: str, roles: list[str],
                     session_id: str, extra: dict | None = None) -> tuple[str, dict]:
        """Sign a short-lived access token for one login session.

        The token is stored server-side and exchanged for claims on every request, so
        ``session_id`` is what lets a specific login be revoked without a deny list.

        Args:
            subject: Internal user id as a string.
            username: The user's principal name.
            roles: Effective roles, including everything the strongest role implies.
            session_id: Id of the login session this token belongs to.
            extra: Additional non-sensitive claims (display name, email, role, oid).

        Returns:
            Tuple of the encoded token and the payload that was signed.

        Raises:
            RuntimeError: The signing key is not configured.
        """
        if not self._private:
            raise RuntimeError("JWT private key not configured")
        now = datetime.now(timezone.utc)
        payload = {
            "sub": subject,
            "username": username,
            "roles": roles,
            "sid": session_id,
            "iss": self.issuer,
            "iat": int(now.timestamp()),
            "exp": int((now + self.access_ttl).timestamp()),
            "jti": uuid.uuid4().hex,
            **(extra or {}),
        }
        return jwt.encode(payload, self._private, algorithm=self.algorithm), payload

    def decode(self, token: str) -> dict:
        if not self._public:
            raise RuntimeError("JWT public key not configured")
        return jwt.decode(token, self._public, algorithms=[self.algorithm],
                          issuer=self.issuer, options={"verify_iss": True})
