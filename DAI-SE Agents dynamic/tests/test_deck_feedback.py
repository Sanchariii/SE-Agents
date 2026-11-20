"""Deck-version feedback validation, persistence, routes, and API contract."""

from collections.abc import Generator
from typing import NoReturn

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config.constants import DeckFeedbackConstants
from app.config.exceptions import ConflictError, NotFoundError
from app.core.dependencies import auth
from app.core.handlers.deck_version_handler import DeckVersionHandler
from app.core.models.deck_version_model import SubmitDeckVersionFeedbackRequest
from app.core.services import deck_service


class FakeFeedbackDb:
    """Minimal Postgres test double for deck-version feedback statements."""

    def __init__(
        self,
        *,
        version_exists: bool = True,
        thumbs_up: bool | None = None,
        comment: str | None = None,
    ) -> None:
        self.version_exists = version_exists
        self.thumbs_up = thumbs_up
        self.comment = comment
        self.executed: list[tuple[str, dict]] = []
        self.returning: list[tuple[str, dict]] = []
        self.queries: list[tuple[str, dict]] = []

    def execute(self, sql: str, params: dict | None = None) -> None:
        """Record a schema statement.

        Args:
            sql: SQL statement being executed.
            params: Optional bound parameters.

        Returns:
            None.
        """
        self.executed.append((sql, params or {}))

    def execute_returning(
        self,
        sql: str,
        params: dict | None = None,
    ) -> list[dict]:
        """Apply the conditional feedback update and return its row.

        Args:
            sql: Parameterized update statement.
            params: Bound feedback values.

        Returns:
            The updated row, or an empty list when the condition did not match.
        """
        values = params or {}
        self.returning.append((sql, values))
        if not self.version_exists or self.thumbs_up is not None:
            return []
        self.thumbs_up = values["thumbs_up"]
        self.comment = values["comment"]
        return [self._row()]

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        """Return the current fake version state.

        Args:
            sql: Parameterized select statement.
            params: Bound version identifiers.

        Returns:
            No rows for a missing version, otherwise the requested feedback state.
        """
        self.queries.append((sql, params or {}))
        if not self.version_exists:
            return []
        if "SELECT thumbs_up FROM" in sql:
            return [{"thumbs_up": self.thumbs_up}]
        return [self._row()]

    def _row(self) -> dict:
        """Build the current fake feedback row.

        Returns:
            A row matching the deck feedback query projection.
        """
        return {
            "session_id": "s1",
            "deck_version": 2,
            "storage_path": "s1/deck-v2.pptx",
            "theme": None,
            "plan_title": None,
            "files": "{}",
            "status": "generated",
            "created_at": None,
            "thumbs_up": self.thumbs_up,
            "user_comment": self.comment,
        }


def _handler(db: FakeFeedbackDb) -> DeckVersionHandler:
    """Create a handler using the supplied database test double.

    Args:
        db: Fake database backing the handler.

    Returns:
        A deck-version handler isolated from real Postgres.
    """
    handler = DeckVersionHandler()
    handler._db = db
    return handler


@pytest.fixture
def authenticated_owner_client(
    client: TestClient,
    app_instance: FastAPI,
    claims_member: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient, None, None]:
    """Provide an API client authenticated as the owner of every test session.

    Args:
        client: Shared FastAPI test client.
        app_instance: Application whose auth dependency is overridden.
        claims_member: Member claims fixture.
        monkeypatch: Pytest patch helper.

    Yields:
        Authenticated FastAPI test client.
    """
    def claims_override() -> dict:
        """Return member claims without resolving a real session handle."""
        return claims_member

    app_instance.dependency_overrides[auth.require_auth] = claims_override
    monkeypatch.setattr(auth.session_registry_handler, "owns", lambda _uid, _sid: True)
    try:
        yield client
    finally:
        app_instance.dependency_overrides.pop(auth.require_auth, None)


@pytest.mark.parametrize("thumbs_up", [True, False])
def test_feedback_request_requires_a_strict_boolean(thumbs_up: bool) -> None:
    """Accept both boolean ratings, including a valid false value."""
    body = SubmitDeckVersionFeedbackRequest(thumbs_up=thumbs_up)
    assert body.thumbs_up is thumbs_up


@pytest.mark.parametrize("value", [1, 0, "true", "false", None])
def test_feedback_request_rejects_non_boolean_ratings(value: object) -> None:
    """Reject coerced, missing-like, and string rating values."""
    with pytest.raises(ValidationError):
        SubmitDeckVersionFeedbackRequest(thumbs_up=value)


def test_feedback_request_requires_a_rating() -> None:
    """Reject a request that omits its required rating."""
    with pytest.raises(ValidationError):
        SubmitDeckVersionFeedbackRequest()


def test_feedback_comment_accepts_exact_limit_and_rejects_overflow() -> None:
    """Enforce the shared 1,000-character feedback comment limit."""
    limit = DeckFeedbackConstants.COMMENT_MAX_LENGTH
    accepted = SubmitDeckVersionFeedbackRequest(thumbs_up=True, comment="a" * limit)
    assert len(accepted.comment or "") == limit
    with pytest.raises(ValidationError):
        SubmitDeckVersionFeedbackRequest(thumbs_up=True, comment="a" * (limit + 1))


def test_ensure_table_adds_feedback_columns_to_existing_schema() -> None:
    """Run additive feedback migrations after creating the base version table."""
    db = FakeFeedbackDb()
    _handler(db).ensure_table()
    statements = "\n".join(sql for sql, _ in db.executed)
    assert len(db.executed) == 3
    assert "ADD COLUMN IF NOT EXISTS thumbs_up BOOLEAN" in statements
    assert "ADD COLUMN IF NOT EXISTS user_comment" in statements
    assert f"VARCHAR({DeckFeedbackConstants.COMMENT_MAX_LENGTH})" in statements


@pytest.mark.parametrize(
    ("thumbs_up", "comment"),
    [(True, "Useful deck"), (False, None)],
)
def test_submit_feedback_atomically_stores_positive_and_negative_ratings(
    thumbs_up: bool,
    comment: str | None,
) -> None:
    """Store either rating through the conditional one-time update."""
    db = FakeFeedbackDb()
    result = _handler(db).submit_feedback("s1", 2, thumbs_up, comment)
    sql, params = db.returning[0]
    assert "thumbs_up IS NULL" in sql
    assert params == {
        "sid": "s1",
        "ver": 2,
        "thumbs_up": thumbs_up,
        "comment": comment,
    }
    assert result.thumbs_up is thumbs_up
    assert result.comment == comment


def test_submit_feedback_rejects_a_duplicate() -> None:
    """Return a conflict when the version already has either rating."""
    handler = _handler(FakeFeedbackDb(thumbs_up=False, comment="Needs changes"))
    with pytest.raises(ConflictError, match="already submitted"):
        handler.submit_feedback("s1", 2, True, "Changed my mind")


def test_submit_feedback_rejects_a_missing_deck_version() -> None:
    """Return not found when the conditional update targets no deck version."""
    handler = _handler(FakeFeedbackDb(version_exists=False))
    with pytest.raises(NotFoundError, match="deck version not found"):
        handler.submit_feedback("s1", 2, True, None)


def test_get_feedback_preserves_a_false_rating() -> None:
    """Distinguish thumbs-down from the null not-submitted state."""
    result = _handler(FakeFeedbackDb(thumbs_up=False)).get_feedback("s1", 2)
    assert result.thumbs_up is False


@pytest.mark.parametrize(
    "db",
    [FakeFeedbackDb(thumbs_up=None), FakeFeedbackDb(version_exists=False)],
)
def test_get_feedback_returns_not_found_until_feedback_exists(
    db: FakeFeedbackDb,
) -> None:
    """Hide both a missing version and an unsubmitted feedback resource."""
    with pytest.raises(NotFoundError, match="deck feedback not found"):
        _handler(db).get_feedback("s1", 2)


def test_deleting_a_version_removes_its_embedded_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete feedback with its owning ppt_versions row without extra cleanup."""
    db = FakeFeedbackDb(thumbs_up=True, comment="Useful deck")
    deleted_paths: list[str] = []

    def delete_file(path: str, **_kwargs: object) -> None:
        """Record a blob deletion without contacting storage."""
        deleted_paths.append(path)

    monkeypatch.setattr(
        "app.core.handlers.deck_version_handler.storage.delete_file", delete_file)

    assert _handler(db).delete_version("s1", 2) is True
    assert deleted_paths == ["s1/deck-v2.pptx"]
    assert any("DELETE FROM ppt_versions" in sql for sql, _ in db.executed)


def test_submit_feedback_endpoint_returns_created_feedback(
    authenticated_owner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose the saved handler result with HTTP 201."""
    def submit(
        session_id: str,
        deck_version: int,
        thumbs_up: bool,
        comment: str | None,
    ) -> dict:
        """Return feedback without contacting Postgres."""
        return {
            "session_id": session_id,
            "deck_version": deck_version,
            "thumbs_up": thumbs_up,
            "comment": comment,
        }

    monkeypatch.setattr(deck_service.deck_version_handler, "submit_feedback", submit)
    response = authenticated_owner_client.post(
        "/decks/s1/versions/2/feedback",
        json={"thumbs_up": False, "comment": "Needs more detail"},
    )
    assert response.status_code == 201
    assert response.json() == {
        "session_id": "s1",
        "deck_version": 2,
        "thumbs_up": False,
        "comment": "Needs more detail",
    }


def test_get_feedback_endpoint_returns_saved_feedback(
    authenticated_owner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose persisted feedback through the nested GET route."""
    def get(session_id: str, deck_version: int) -> dict:
        """Return feedback without contacting Postgres."""
        return {
            "session_id": session_id,
            "deck_version": deck_version,
            "thumbs_up": True,
            "comment": None,
        }

    monkeypatch.setattr(deck_service.deck_version_handler, "get_feedback", get)
    response = authenticated_owner_client.get("/decks/s1/versions/2/feedback")
    assert response.status_code == 200
    assert response.json()["thumbs_up"] is True


def test_duplicate_feedback_endpoint_returns_conflict(
    authenticated_owner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Map the immutable submission error to HTTP 409."""
    def duplicate(*_args: object) -> NoReturn:
        """Raise the duplicate domain error."""
        raise ConflictError("feedback already submitted for deck version")

    monkeypatch.setattr(deck_service.deck_version_handler, "submit_feedback", duplicate)
    response = authenticated_owner_client.post(
        "/decks/s1/versions/2/feedback", json={"thumbs_up": True})
    assert response.status_code == 409


def test_feedback_endpoint_rejects_invalid_payload(
    authenticated_owner_client: TestClient,
) -> None:
    """Return HTTP 422 for a non-boolean rating."""
    response = authenticated_owner_client.post(
        "/decks/s1/versions/2/feedback", json={"thumbs_up": 1})
    assert response.status_code == 422


def test_openapi_documents_feedback_request_and_responses(
    app_instance: FastAPI,
) -> None:
    """Publish both nested feedback operations in the generated API contract."""
    openapi = app_instance.openapi()
    path = "/api/decks/{session_id}/versions/{deck_version}/feedback"
    operations = openapi["paths"][path]
    assert set(operations) >= {"get", "post"}
    assert "201" in operations["post"]["responses"]
    schema = operations["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("SubmitDeckVersionFeedbackRequest")
    request_schema = openapi["components"]["schemas"]["SubmitDeckVersionFeedbackRequest"]
    assert request_schema["required"] == ["thumbs_up"]
    assert request_schema["properties"]["thumbs_up"]["type"] == "boolean"
    comment_schema = request_schema["properties"]["comment"]["anyOf"][0]
    assert comment_schema["maxLength"] == DeckFeedbackConstants.COMMENT_MAX_LENGTH
