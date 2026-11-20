"""Opaque session handles: minting and one-way hashing.

Sign-in is federated (Entra ID), so this service stores no passwords. What it does
store are login sessions, and those are looked up by the SHA-256 digest of the
handle rather than the handle itself — a leaked Redis snapshot then contains
nothing that can be replayed against the API.
"""

import hashlib
import hmac
import uuid

from app.config.constants import AuthConstants


def new_handle(user_id: int) -> str:
    """Mint an opaque handle of the form ``<user_id>:<uuid4 hex>``.

    The user id prefix is a routing/telemetry hint only; the 128 bits of
    ``uuid.uuid4`` (os.urandom-backed) are what make the handle unguessable.

    Args:
        user_id: Internal id of the user the handle is being issued to.

    Returns:
        The handle to hand to the client. Only its digest is ever stored.
    """
    return f"{user_id}{AuthConstants.TOKEN_SEPARATOR}{uuid.uuid4().hex}"


def hash_handle(handle: str) -> str:
    """Return the SHA-256 hex digest used as the server-side lookup key.

    Args:
        handle: The opaque handle presented by the client.

    Returns:
        Hex digest of the handle.
    """
    return hashlib.sha256(handle.encode("utf-8")).hexdigest()


def handle_user_id(handle: str) -> int | None:
    """Best-effort read of the user id embedded in a handle, for logging only.

    Args:
        handle: The opaque handle presented by the client.

    Returns:
        The embedded user id, or ``None`` when the handle is malformed. Never treat
        this as authenticated — only a successful store lookup proves anything.
    """
    prefix, _, _ = handle.partition(AuthConstants.TOKEN_SEPARATOR)
    return int(prefix) if prefix.isdigit() else None


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two secrets without leaking their contents through timing.

    Args:
        left: First value.
        right: Second value.

    Returns:
        True when the two values are identical.
    """
    return hmac.compare_digest(left, right)
