"""Single sign-on: session handles, rotation, revocation, roles and ownership."""

import json
import time

import pytest
from fastapi import HTTPException

from app.config.constants import AuthConstants, Roles
from app.config.exceptions import ForbiddenError, UnauthorizedError
from app.core.dependencies import auth
from app.core.handlers.auth_session_handler import AuthSessionHandler, effective_roles
from app.core.handlers.group_role_handler import GroupRoleHandler
from app.core.models.auth_model import AuthPrincipal
from app.utilities.redis_util import RedisUtil
from app.utilities.security_util import handle_user_id, hash_handle, new_handle


# --------------------------------------------------------------------------- #
# Test doubles                                                                 #
# --------------------------------------------------------------------------- #

class FakeJwt:
    """Stands in for JwtUtil so the suite needs no RS256 key pair."""

    def issue_access(self, *, subject, username, roles, session_id, extra=None):
        payload = {"sub": subject, "username": username, "roles": roles,
                   "sid": session_id, "exp": int(time.time()) + 1800, **(extra or {})}
        return json.dumps(payload), payload

    def decode(self, token: str) -> dict:
        claims = json.loads(token)
        if claims["exp"] <= time.time():
            raise ValueError("expired")
        return claims


def make_handler() -> AuthSessionHandler:
    """A session handler bound to a private in-memory Redis and a fake signer."""
    return AuthSessionHandler(redis=RedisUtil(url="redis://127.0.0.1:1/0"),
                              jwt_util=FakeJwt())


def principal(user_id: int = 7, role: str = Roles.MEMBER) -> AuthPrincipal:
    return AuthPrincipal(user_id=user_id, username="a.user@example.com",
                         display_name="A User", email="a.user@example.com",
                         entra_oid="oid-1", role=role, roles=effective_roles(role))


# --------------------------------------------------------------------------- #
# Opaque handles                                                               #
# --------------------------------------------------------------------------- #

def test_handle_carries_the_user_id_and_random_material():
    handle = new_handle(42)
    assert handle.startswith("42:")
    assert handle_user_id(handle) == 42
    assert len(handle.split(":")[1]) == 32


def test_handles_are_unique():
    assert new_handle(1) != new_handle(1)


def test_only_the_digest_is_ever_stored():
    handle = new_handle(1)
    digest = hash_handle(handle)
    assert digest != handle and handle not in digest
    assert hash_handle(handle) == digest              # stable for lookups


def test_a_malformed_handle_yields_no_user_id():
    assert handle_user_id("not-a-handle") is None


# --------------------------------------------------------------------------- #
# Session lifecycle                                                            #
# --------------------------------------------------------------------------- #

def test_a_new_session_resolves_to_its_claims():
    handler = make_handler()
    issued = handler.create(principal())
    claims = handler.resolve(issued.session_token)
    assert claims["sub"] == "7"
    assert claims["role"] == Roles.MEMBER
    assert claims["sid"] == issued.session_id


def test_the_signed_token_is_never_handed_to_the_client():
    issued = make_handler().create(principal())
    assert "jwt" not in issued.model_dump()
    assert issued.session_token != issued.refresh_token


def test_an_unknown_handle_resolves_to_nothing():
    assert make_handler().resolve(new_handle(7)) is None


def test_signing_in_again_terminates_the_earlier_session():
    """One active session per user: the older handle must stop working at once."""
    handler = make_handler()
    first = handler.create(principal())
    second = handler.create(principal())
    assert handler.resolve(first.session_token) is None
    assert handler.resolve(second.session_token) is not None


def test_an_expired_session_is_rejected_even_when_redis_keeps_it():
    """The in-memory fallback ignores TTLs, so expiry is also enforced in code."""
    handler = make_handler()
    issued = handler.create(principal())
    record = handler._r().get_json(handler._session_key(issued.session_token))
    record["expires_at"] = time.time() - 1
    handler._r().set_json(handler._session_key(issued.session_token), record)
    assert handler.resolve(issued.session_token) is None


def test_a_tampered_stored_token_is_rejected():
    handler = make_handler()
    issued = handler.create(principal())
    key = handler._session_key(issued.session_token)
    record = handler._r().get_json(key)
    record["jwt"] = "not-a-token"
    handler._r().set_json(key, record)
    assert handler.resolve(issued.session_token) is None


def test_logout_ends_the_session_and_its_refresh_handle():
    handler = make_handler()
    issued = handler.create(principal())
    assert handler.revoke(issued.session_token) is True
    assert handler.resolve(issued.session_token) is None
    assert handler.consume_refresh(issued.refresh_token) is None


# --------------------------------------------------------------------------- #
# Refresh                                                                      #
# --------------------------------------------------------------------------- #

def test_a_refresh_handle_is_single_use():
    handler = make_handler()
    issued = handler.create(principal())
    assert handler.consume_refresh(issued.refresh_token) is not None
    assert handler.consume_refresh(issued.refresh_token) is None


def test_refreshing_does_not_extend_the_absolute_deadline():
    handler = make_handler()
    first = handler.create(principal())
    record = handler.consume_refresh(first.refresh_token)
    second = handler.create(principal(), login_at=record["login_at"],
                            session_id=record["session_id"])
    assert second.session_id == first.session_id
    assert second.refresh_expires_in <= first.refresh_expires_in


def test_a_session_issued_late_cannot_outlive_the_refresh_window():
    """Near the deadline the session must be shortened, not run past re-login time."""
    handler = make_handler()
    nearly_spent = time.time() - AuthConstants.REFRESH_TTL_SECONDS + 120
    issued = handler.create(principal(), login_at=nearly_spent)
    assert issued.expires_in <= issued.refresh_expires_in
    assert issued.expires_in <= 120


def test_an_expired_refresh_handle_is_rejected():
    handler = make_handler()
    issued = handler.create(principal())
    key = handler._refresh_key(issued.refresh_token)
    record = handler._r().get_json(key)
    record["expires_at"] = time.time() - 1
    handler._r().set_json(key, record)
    assert handler.consume_refresh(issued.refresh_token) is None


def test_a_login_elsewhere_invalidates_the_old_refresh_handle():
    handler = make_handler()
    first = handler.create(principal())
    handler.create(principal())
    assert handler.consume_refresh(first.refresh_token) is None


def test_replaying_a_burned_handle_revokes_the_live_session_family():
    """Reuse of a spent handle within the still-active login is treated as theft."""
    handler = make_handler()
    issued = handler.create(principal())
    record = handler.consume_refresh(issued.refresh_token)          # spend it once
    rotated = handler.create(principal(), login_at=record["login_at"],
                             session_id=record["session_id"])
    assert handler.consume_refresh(issued.refresh_token) is None    # replay is refused
    assert handler.resolve(rotated.session_token) is None           # and the family dies


def test_replaying_a_handle_from_an_ended_login_spares_the_new_session():
    """A dead token from a previous login must not force-log-out a fresh one."""
    handler = make_handler()
    first = handler.create(principal())
    handler.consume_refresh(first.refresh_token)                    # burn within login A
    second = handler.create(principal())                           # unrelated login B
    assert handler.consume_refresh(first.refresh_token) is None    # dead token rejected
    assert handler.resolve(second.session_token) is not None       # login B untouched


# --------------------------------------------------------------------------- #
# Roles from directory groups                                                  #
# --------------------------------------------------------------------------- #

MAPPING = {"g-admin": Roles.ADMIN, "g-approver": Roles.APPROVER, "g-member": Roles.MEMBER}


def test_the_strongest_role_wins():
    assert GroupRoleHandler.highest_role(["g-member", "g-admin"], MAPPING) == Roles.ADMIN
    assert GroupRoleHandler.highest_role(["g-member", "g-approver"],
                                         MAPPING) == Roles.APPROVER


def test_no_mapped_group_grants_no_role():
    assert GroupRoleHandler.highest_role(["g-unrelated"], MAPPING) is None
    assert GroupRoleHandler.highest_role([], MAPPING) is None


def test_roles_are_cumulative():
    assert set(effective_roles(Roles.ADMIN)) == set(Roles.ALL)
    assert Roles.MEMBER in effective_roles(Roles.APPROVER)
    assert Roles.ADMIN not in effective_roles(Roles.APPROVER)


# --------------------------------------------------------------------------- #
# Bearer handling                                                              #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_missing_token_is_401():
    with pytest.raises(HTTPException) as exc:
        await auth.require_auth(authorization=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_non_bearer_scheme_is_401():
    with pytest.raises(HTTPException) as exc:
        await auth.require_auth(authorization="Basic abc123")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_an_unknown_session_is_401_without_leaking_details(monkeypatch):
    monkeypatch.setattr(auth.auth_session_handler, "resolve", lambda h: None)
    with pytest.raises(HTTPException) as exc:
        await auth.require_auth(authorization="Bearer 1:deadbeef")
    assert exc.value.status_code == 401
    assert exc.value.detail == "invalid or expired session"


@pytest.mark.asyncio
async def test_a_live_session_yields_its_claims(monkeypatch, claims_member):
    monkeypatch.setattr(auth.auth_session_handler, "resolve", lambda h: claims_member)
    assert await auth.require_auth(authorization="Bearer 2:abc") == claims_member


# --------------------------------------------------------------------------- #
# Role checks                                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_role_check_allows_a_held_role(claims_admin):
    checker = auth.require_role(Roles.ADMIN)
    assert await checker(claims=claims_admin) is claims_admin


@pytest.mark.asyncio
async def test_role_check_rejects_a_missing_role(claims_member):
    checker = auth.require_role(Roles.ADMIN)
    with pytest.raises(HTTPException) as exc:
        await checker(claims=claims_member)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_role_check_accepts_any_of_several_roles(claims_member):
    checker = auth.require_role(Roles.MEMBER, Roles.ADMIN)
    assert await checker(claims=claims_member) is claims_member


@pytest.mark.asyncio
async def test_an_admin_inherits_the_approver_gate(claims_admin):
    checker = auth.require_role(Roles.APPROVER)
    assert await checker(claims=claims_admin) is claims_admin


@pytest.mark.asyncio
async def test_a_member_cannot_approve(claims_member):
    checker = auth.require_role(Roles.APPROVER)
    with pytest.raises(HTTPException) as exc:
        await checker(claims=claims_member)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# Session ownership (the IDOR guard)                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_owner_may_access_their_session(monkeypatch, claims_member):
    monkeypatch.setattr(auth.session_registry_handler, "owns", lambda u, s: True)
    assert await auth.require_session_access("sess-1", claims_member) is claims_member


@pytest.mark.asyncio
async def test_non_owner_gets_404_not_403(monkeypatch, claims_member):
    """404 rather than 403: a 403 would confirm the session id exists."""
    monkeypatch.setattr(auth.session_registry_handler, "owns", lambda u, s: False)
    with pytest.raises(HTTPException) as exc:
        await auth.require_session_access("someone-elses-session", claims_member)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_admin_may_access_any_session(monkeypatch, claims_admin):
    monkeypatch.setattr(auth.session_registry_handler, "owns",
                        lambda u, s: pytest.fail("ownership must not be queried for admins"))
    assert await auth.require_session_access("any-session", claims_admin) is claims_admin


@pytest.mark.asyncio
async def test_strict_session_owner_allows_the_owner(monkeypatch, claims_member):
    """The feedback owner guard accepts a registered session owner."""
    monkeypatch.setattr(auth.session_registry_handler, "owns", lambda u, s: True)
    assert await auth.require_session_owner("sess-1", claims_member) is claims_member


@pytest.mark.asyncio
async def test_strict_session_owner_does_not_bypass_admin(monkeypatch, claims_admin):
    """An administrator cannot submit feedback for another user's session."""
    monkeypatch.setattr(auth.session_registry_handler, "owns", lambda u, s: False)
    with pytest.raises(HTTPException) as exc:
        await auth.require_session_owner("someone-elses-session", claims_admin)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_session_role_requires_both_ownership_and_role(monkeypatch, claims_member):
    monkeypatch.setattr(auth.session_registry_handler, "owns", lambda u, s: True)
    checker = auth.require_session_role(Roles.APPROVER)
    with pytest.raises(HTTPException) as exc:
        await checker(claims=claims_member)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_strict_owner_role_accepts_an_owner_with_an_implied_role(claims_admin):
    """Cumulative roles let an owning administrator use a member operation."""
    checker = auth.require_session_owner_role(Roles.MEMBER)
    assert await checker(claims=claims_admin) is claims_admin


# --------------------------------------------------------------------------- #
# The login flow                                                               #
# --------------------------------------------------------------------------- #

PROFILE = {"id": "oid-1", "displayName": "A User", "mail": "a.user@example.com",
           "userPrincipalName": "a.user@example.com"}


@pytest.fixture
def sso(monkeypatch):
    """Wire the login flow to fakes: Graph, the mapping table and the user store."""
    from app.core.handlers import auth_handler as module
    from app.core.models.user_model import UserRecord

    state = {"profile": PROFILE, "id_claims": {"oid": "oid-1"},
             "groups": ["g-admin", "g-member"], "is_active": True}

    async def fake_me(_token):
        return state["profile"]

    async def fake_groups(_token, _ids):
        return state["groups"]

    monkeypatch.setattr(module.graph, "verify_id_token", lambda t: state["id_claims"])
    monkeypatch.setattr(module.graph, "fetch_me", fake_me)
    monkeypatch.setattr(module.graph, "check_member_groups", fake_groups)
    monkeypatch.setattr(module.group_role_handler, "get_mapping", lambda: MAPPING)
    monkeypatch.setattr(
        module.user_handler, "upsert_sso_user",
        lambda **kw: UserRecord(id=7, username=kw["username"], email=kw["email"],
                                display_name=kw["display_name"],
                                entra_oid=kw["entra_oid"], roles=kw["roles"],
                                is_active=state["is_active"]))
    monkeypatch.setattr(
        module.user_handler, "get_by_id",
        lambda user_id: UserRecord(id=user_id, username=PROFILE["userPrincipalName"],
                                   email=PROFILE["mail"], entra_oid=PROFILE["id"],
                                   roles=effective_roles(Roles.ADMIN),
                                   is_active=state["is_active"]))
    monkeypatch.setattr(module, "auth_session_handler", make_handler())
    return state


@pytest.mark.asyncio
async def test_login_issues_a_session_with_the_strongest_role(sso):
    from app.core.handlers.auth_handler import auth_handler

    session = await auth_handler.login("graph-token", "id-token")
    assert session.principal.role == Roles.ADMIN
    assert session.principal.user_id == 7
    assert session.session_token and session.refresh_token
    assert session.expires_in > 0


@pytest.mark.asyncio
async def test_login_is_denied_without_a_mapped_group(sso):
    from app.core.handlers.auth_handler import auth_handler

    sso["groups"] = []
    with pytest.raises(ForbiddenError):
        await auth_handler.login("graph-token", "id-token")


@pytest.mark.asyncio
async def test_login_is_denied_when_the_two_tokens_disagree(sso):
    """An id token for one user must not be paired with another user's access token."""
    from app.core.handlers.auth_handler import auth_handler

    sso["id_claims"] = {"oid": "somebody-else"}
    with pytest.raises(UnauthorizedError):
        await auth_handler.login("graph-token", "id-token")


@pytest.mark.asyncio
async def test_login_is_denied_for_a_locally_disabled_account(sso):
    from app.core.handlers.auth_handler import auth_handler

    sso["is_active"] = False
    with pytest.raises(ForbiddenError):
        await auth_handler.login("graph-token", "id-token")


@pytest.mark.asyncio
async def test_a_replayed_refresh_handle_is_rejected(sso):
    from app.core.handlers.auth_handler import auth_handler

    session = await auth_handler.login("graph-token", "id-token")
    await auth_handler.refresh(session.refresh_token)
    with pytest.raises(UnauthorizedError):
        await auth_handler.refresh(session.refresh_token)
