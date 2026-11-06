"""Smoke test: probes, login, and that anonymous callers are refused."""

import sys

from _client import Api, run


def main() -> int:
    api = Api()

    def health() -> bool:
        r = api._client.get("/health")
        return r.status_code == 200 and r.json()["status"] == "ok"

    def ready() -> bool:
        r = api._client.get("/ready")
        return r.status_code in (200, 503) and "configured" in r.json()

    def anonymous_is_refused() -> bool:
        return api._client.get("/decks/sessions").status_code == 401

    def login_works() -> bool:
        return bool(api.login().get("token"))

    def me_matches_the_login() -> bool:
        return api.get("/auth/me").status_code == 200

    def correlation_id_is_echoed() -> bool:
        r = api._client.get("/health", headers={"x-correlation-id": "smoke-1"})
        return r.headers.get("x-correlation-id") == "smoke-1"

    return run("00 smoke", [
        ("GET /health is ok", health),
        ("GET /ready reports configuration", ready),
        ("anonymous access is refused", anonymous_is_refused),
        ("login returns an access token", login_works),
        ("GET /auth/me works with the token", me_matches_the_login),
        ("correlation id is echoed back", correlation_id_is_echoed),
    ])


if __name__ == "__main__":
    sys.exit(main())
