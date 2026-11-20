"""Shared fixtures.

The suite runs entirely in-process: Redis falls back to its in-memory mode and the
Postgres bootstrap failure is tolerated by the lifespan, so no infrastructure,
credentials or network access are needed.
"""

import os

os.environ.setdefault("APP_TYPE", "local")
os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("REDIS_URI", "redis://127.0.0.1:1/0")   # forces the in-memory fallback
# A dev machine with LibreOffice installed would otherwise send HIGH slides to the real
# renderer and judge; max-mode tests opt in by patching.
os.environ.setdefault("SLIDE_MAX_MODE_ENABLED", "false")
# A dev.env with DECK_ENGINE=skill_deck would otherwise send every workflow test to the
# real gateway; the skill deck agent's own tests switch it on by patching.
os.environ.setdefault("DECK_ENGINE", "classic")
# Same for DECK_POLISH: the deck polish agent's own tests drive it directly.
os.environ.setdefault("DECK_POLISH", "off")

import pytest
from fastapi.testclient import TestClient

from app.config.constants import Roles


@pytest.fixture
def claims_admin() -> dict:
    return {"sub": "1", "username": "admin@example.com", "role": Roles.ADMIN,
            "roles": list(Roles.IMPLIED[Roles.ADMIN])}


@pytest.fixture
def claims_approver() -> dict:
    return {"sub": "2", "username": "approver@example.com", "role": Roles.APPROVER,
            "roles": list(Roles.IMPLIED[Roles.APPROVER])}


@pytest.fixture
def claims_member() -> dict:
    return {"sub": "3", "username": "member@example.com", "role": Roles.MEMBER,
            "roles": list(Roles.IMPLIED[Roles.MEMBER])}


@pytest.fixture
def app_instance():
    from app.core.services.server import app
    return app


@pytest.fixture
def client(app_instance) -> TestClient:
    # base_url carries the global /api prefix; httpx appends the relative request
    # paths onto it, so tests keep addressing routes as "/health", "/decks/...".
    return TestClient(app_instance, base_url="http://testserver/api")


@pytest.fixture
def session_store(monkeypatch):
    """A store bound to a fresh in-memory Redis so tests never share state."""
    from app.core.handlers import session_store as module
    from app.core.handlers.session_store import SessionStore
    from app.utilities.redis_util import RedisUtil

    # A reachable dev database would otherwise leak rows between runs; tests override these.
    monkeypatch.setattr(module.chat_message_handler, "latest_session_memory", lambda *a, **k: None)
    monkeypatch.setattr(module.chat_message_handler, "list_messages", lambda *a, **k: [])
    monkeypatch.setattr(module.document_registry_handler, "list_documents", lambda *a, **k: [])
    monkeypatch.setattr(module.document_registry_handler, "record", lambda *a, **k: None)
    monkeypatch.setattr(module.document_registry_handler, "delete", lambda *a, **k: 0)
    monkeypatch.setattr(module.deck_version_handler, "list_versions", lambda *a, **k: [])
    return SessionStore(RedisUtil(url="redis://127.0.0.1:1/0"))
