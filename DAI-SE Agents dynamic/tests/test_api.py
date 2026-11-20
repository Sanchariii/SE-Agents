"""API surface: routing, probes, and that every route is actually protected."""

from app.config.constants import AppConstants


def test_health_is_public_and_dependency_free(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == AppConstants.APP_NAME


def test_health_echoes_the_correlation_id(client):
    r = client.get("/health", headers={"x-correlation-id": "abc-123"})
    assert r.headers["x-correlation-id"] == "abc-123"


def test_correlation_id_is_generated_when_absent(client):
    assert client.get("/health").headers.get("x-correlation-id")


def test_ready_reports_configuration(client):
    body = client.get("/ready").json()
    assert body["status"] in {"ready", "degraded"}
    assert "azure_openai" in body["configured"]


PROTECTED = [
    ("get", "/decks/sessions"),
    ("post", "/decks/sessions"),
    ("get", "/decks/s1/documents"),
    ("get", "/decks/s1/skill"),
    ("put", "/decks/s1/skill"),
    ("delete", "/decks/s1/skill"),
    ("post", "/decks/s1/skill/prepare"),
    ("post", "/decks/s1/skill/references/website"),
    ("post", "/decks/s1/documents/register"),
    ("post", "/decks/s1/documents/complete"),
    ("post", "/decks/s1/documents/cleanup-abandoned"),
    ("post", "/decks/s1/documents/doc1/abort"),
    ("post", "/decks/s1/invoke"),
    ("post", "/decks/s1/stop"),
    ("get", "/decks/s1/runs/current"),
    ("get", "/decks/s1/runs/r1"),
    ("get", "/decks/s1/runs/r1/events"),
    ("get", "/decks/s1/stream"),
    ("get", "/decks/s1/versions"),
    ("get", "/decks/s1/versions/1/feedback"),
    ("post", "/decks/s1/versions/1/feedback"),
    ("get", "/decks/s1/messages"),
    ("delete", "/decks/s1"),
    ("patch", "/decks/s1/title"),
    ("get", "/users"),
    ("get", "/approvals"),
    ("get", "/analytics/summary"),
    ("get", "/auth/me"),
    ("get", "/auth/validate"),
    ("post", "/auth/logout"),
]


def test_every_business_route_rejects_anonymous_callers(client):
    for method, path in PROTECTED:
        kwargs = {"json": {}} if method in {"post", "patch"} else {}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401, (
            f"{method.upper()} {path} returned {response.status_code}, not 401")


def test_a_malformed_token_is_rejected(client):
    r = client.get("/decks/sessions", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_skill_reference_upload_rejects_anonymous_callers(client):
    response = client.post(
        "/decks/s1/skill/references",
        files={"file": ("look.png", b"image bytes", "image/png")},
    )
    assert response.status_code == 401


def test_there_is_no_password_login_surface(client):
    """Sign-in is federated; no endpoint may accept a username and password."""
    paths = client.app.openapi()["paths"]
    assert "/api/users" not in paths or "post" not in paths["/api/users"]
    login = paths["/api/auth/login"]["post"]["requestBody"]
    schema_ref = login["content"]["application/json"]["schema"]["$ref"]
    assert schema_ref.endswith("SsoLoginRequest")


def test_the_api_exposes_only_the_intended_surface(client):
    """The internal agent routes were merged into /decks; they must not be reachable."""
    paths = set(client.app.openapi()["paths"])
    assert not any(p.startswith(("/extraction", "/chat-memory", "/ppt-versions",
                                 "/se-trainingdeck-agent")) for p in paths)


def test_cors_does_not_allow_a_wildcard_origin(app_instance):
    from starlette.middleware.cors import CORSMiddleware
    cors = [m for m in app_instance.user_middleware if m.cls is CORSMiddleware]
    assert cors, "CORS middleware is not installed"
    assert "*" not in cors[0].kwargs["allow_origins"]


def test_unhandled_errors_do_not_leak_internals(app_instance):
    """A crash must return a correlation id, never the exception text."""
    from fastapi.testclient import TestClient

    @app_instance.get("/_boom")
    async def _boom():
        raise RuntimeError("connection string s3cret")

    with TestClient(app_instance, raise_server_exceptions=False) as c:
        response = c.get("/_boom")

    assert response.status_code == 500
    assert "s3cret" not in response.text
    assert response.json()["correlation_id"]
