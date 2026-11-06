"""Renaming a project: PATCH /decks/{session_id}/title."""

import pytest
from pydantic import ValidationError

from app.config.constants import SessionTitleConstants
from app.core.dependencies import auth
from app.core.handlers.session_registry_handler import SessionRegistryHandler
from app.core.models.session_model import UpdateSessionTitleRequest


class FakeDb:
    """Records the last write and returns canned RETURNING rows."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def execute_returning(self, sql: str, params: dict | None = None) -> list[dict]:
        self.calls.append((sql, params or {}))
        return self.rows


@pytest.fixture
def owner_client(client, app_instance, claims_member, monkeypatch):
    """Client authenticated as a member who owns every session."""
    app_instance.dependency_overrides[auth.require_auth] = lambda: claims_member
    monkeypatch.setattr(auth.session_registry_handler, "owns", lambda _u, _s: True)
    try:
        yield client
    finally:
        app_instance.dependency_overrides.pop(auth.require_auth, None)


def test_update_title_matches_on_user_and_session() -> None:
    handler = SessionRegistryHandler()
    db = FakeDb([{"session_id": "s1"}])
    handler._db = db
    assert handler.update_title(7, "s1", "New") is True
    sql, params = db.calls[0]
    assert "user_id = :u AND session_id = :s" in sql
    assert params == {"u": 7, "s": "s1", "t": "New"}


def test_update_title_reports_missing_project() -> None:
    handler = SessionRegistryHandler()
    handler._db = FakeDb([])
    assert handler.update_title(7, "other", "New") is False


def test_title_is_trimmed_and_validated() -> None:
    assert UpdateSessionTitleRequest(title="  Hello  ").title == "Hello"
    for bad in ("", "   ", "a" * (SessionTitleConstants.MAX_LENGTH + 1)):
        with pytest.raises(ValidationError):
            UpdateSessionTitleRequest(title=bad)


def test_title_accepts_exact_maximum_length() -> None:
    """Accept a title whose length equals the configured limit."""
    title = "a" * SessionTitleConstants.MAX_LENGTH
    assert UpdateSessionTitleRequest(title=title).title == title


def test_route_updates_title(owner_client, claims_member, monkeypatch) -> None:
    """Trim and persist a valid title for the authenticated owner."""
    seen: dict = {}

    def fake_update(user_id: int, session_id: str, title: str) -> bool:
        seen.update(user_id=user_id, session_id=session_id, title=title)
        return True

    monkeypatch.setattr(auth.session_registry_handler, "update_title", fake_update)
    r = owner_client.patch("/decks/s1/title", json={"title": " Renamed "})
    assert r.status_code == 200
    assert r.json() == {"session_id": "s1", "title": "Renamed"}
    assert seen == {"user_id": int(claims_member["sub"]), "session_id": "s1", "title": "Renamed"}


def test_route_accepts_title_at_maximum_length(owner_client, monkeypatch) -> None:
    """Return the complete title when the request is at the configured limit."""
    title = "a" * SessionTitleConstants.MAX_LENGTH
    monkeypatch.setattr(auth.session_registry_handler, "update_title", lambda *_: True)

    response = owner_client.patch("/decks/s1/title", json={"title": title})

    assert response.status_code == 200
    assert response.json() == {"session_id": "s1", "title": title}


def test_route_returns_404_when_nothing_updated(owner_client, monkeypatch) -> None:
    """Hide a session that was not found by the owner's update."""
    monkeypatch.setattr(auth.session_registry_handler, "update_title", lambda *_: False)
    assert owner_client.patch("/decks/s1/title", json={"title": "x"}).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": ""},
        {"title": "  "},
        {"title": None},
        {"title": "a" * (SessionTitleConstants.MAX_LENGTH + 1)},
    ],
)
def test_route_rejects_invalid_title_payloads(owner_client, payload: dict) -> None:
    """Reject missing, blank, null, and over-limit title values."""
    response = owner_client.patch("/decks/s1/title", json=payload)
    assert response.status_code == 422


def test_route_returns_404_for_a_session_not_owned(
    owner_client, monkeypatch
) -> None:
    """Do not disclose or update a session that belongs to another user."""
    monkeypatch.setattr(auth.session_registry_handler, "owns", lambda _u, _s: False)

    response = owner_client.patch("/decks/s1/title", json={"title": "Renamed"})

    assert response.status_code == 404
    assert response.json()["detail"] == "session not found"


def test_route_rejects_a_caller_without_member_role(
    owner_client, app_instance, claims_approver
) -> None:
    """Require the member role even when the caller is authenticated."""
    claims = {**claims_approver, "roles": ["approver"]}
    app_instance.dependency_overrides[auth.require_auth] = lambda: claims

    response = owner_client.patch("/decks/s1/title", json={"title": "Renamed"})

    assert response.status_code == 403
