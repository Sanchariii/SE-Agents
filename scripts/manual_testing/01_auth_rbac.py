"""Auth and RBAC over single sign-on: validation, rotation, single-session, IDOR.

Accounts cannot be created here any more — identities come from Entra ID — so the
script exercises everything reachable with the one signed-in identity plus an
anonymous client.
"""

import sys

from _client import Api, run


def main() -> int:
    user = Api()
    tokens = user.login()

    def validate_reports_a_live_session() -> bool:
        r = user.get("/auth/validate")
        return r.status_code == 200 and r.json()["data"]["valid"] is True

    def me_returns_the_signed_in_user() -> bool:
        r = user.get("/auth/me")
        return r.status_code == 200 and bool(r.json()["username"])

    def an_unknown_handle_is_rejected() -> bool:
        anon = Api()
        anon.token = "1:0000000000000000000000000000dead"
        return anon.get("/auth/validate").status_code == 401

    def anonymous_callers_are_rejected() -> bool:
        return Api().get("/decks/sessions").status_code == 401

    def refresh_rotates_and_burns_the_old_handle() -> bool:
        if not tokens.get("refresh_token"):
            print("    skipped: no refresh handle (SE_SESSION_TOKEN mode)")
            return True
        rotated = user._client.post("/auth/refresh",
                                    json={"refresh_token": tokens["refresh_token"]})
        if rotated.status_code != 200:
            print("   ", rotated.status_code, rotated.text)
            return False
        payload = rotated.json()
        # A refresh handle is single use: replaying it must fail.
        replay = user._client.post("/auth/refresh",
                                   json={"refresh_token": tokens["refresh_token"]})
        old_handle = user.token
        user.token, tokens["refresh_token"] = payload["token"], payload["refresh_token"]
        # Rotation must also retire the previous session handle.
        stale = Api()
        stale.token = old_handle
        return replay.status_code == 401 and stale.get("/auth/validate").status_code == 401

    session = {"id": None}

    def a_session_can_be_created() -> bool:
        session["id"] = user.create_session("rbac test")
        return bool(session["id"])

    def a_stranger_cannot_read_that_session() -> bool:
        """The core IDOR check: no token at all must not help."""
        return Api().get(f"/decks/{session['id']}/versions").status_code == 401

    def logout_ends_the_session() -> bool:
        handle = user.token
        user.logout()
        dead = Api()
        dead.token = handle
        ended = dead.get("/auth/validate").status_code == 401
        user.login()                                  # restore for any later scripts
        return ended

    return run("01 auth + rbac (sso)", [
        ("validate reports a live session", validate_reports_a_live_session),
        ("/auth/me returns the signed-in user", me_returns_the_signed_in_user),
        ("an unknown session handle returns 401", an_unknown_handle_is_rejected),
        ("anonymous callers are rejected", anonymous_callers_are_rejected),
        ("refresh rotates and burns the old handle", refresh_rotates_and_burns_the_old_handle),
        ("a session can be created", a_session_can_be_created),
        ("a stranger cannot read it", a_stranger_cannot_read_that_session),
        ("logout ends the session", logout_ends_the_session),
    ])


if __name__ == "__main__":
    sys.exit(main())
