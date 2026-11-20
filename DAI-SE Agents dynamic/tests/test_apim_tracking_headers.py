"""APIM tracking headers attribute each model call to its user, sub-agent and version."""

import pytest

from app.config.constants import AgentNames, AppConstants
from app.utilities import apim_llm_utils
from app.utilities.analytics.capture import agent_run
from app.utilities.apim_llm_utils import ApimLlmClient
from app.utilities.logger_util import user_email_ctx, user_id_ctx


@pytest.fixture
def caller():
    """Bind a signed-in caller for one test, then restore the previous context."""
    id_token, email_token = user_id_ctx.set("42"), user_email_ctx.set("jane@example.com")
    yield
    user_email_ctx.reset(email_token)
    user_id_ctx.reset(id_token)


def test_a_sub_agent_call_carries_user_agent_and_version(caller):
    """Inside an agent span the headers name the caller and the sub-agent."""
    with agent_run(AgentNames.PLANNER):
        headers = ApimLlmClient()._tracking_headers(None)

    assert headers["x-rok-user-id"] == "42"
    assert headers["x-rok-user-email"] == "jane@example.com"
    assert headers["x-rok-agent-name"] == AgentNames.PLANNER
    assert headers["x-rok-agent-version"] == AppConstants.AGENT_VERSION
    assert headers["x-rok-agent-id"] == AppConstants.APP_NAME


def test_outside_any_span_and_user_only_the_service_is_named():
    """Warmup-style calls fall back to the service name and send no user headers."""
    headers = ApimLlmClient()._tracking_headers(None)

    assert headers["x-rok-agent-name"] == AppConstants.APP_NAME
    assert headers["x-rok-agent-version"] == AppConstants.AGENT_VERSION
    assert "x-rok-user-id" not in headers
    assert "x-rok-user-email" not in headers


def test_the_span_name_is_cleared_when_the_span_ends(caller):
    """A finished span must not leak its name onto the next call."""
    with agent_run(AgentNames.TRIAGE):
        pass

    assert ApimLlmClient()._tracking_headers(None)["x-rok-agent-name"] == AppConstants.APP_NAME


def test_a_per_call_override_beats_the_enclosing_span(caller):
    """Shared helpers (guardrails, translation) name themselves inside another agent's span."""
    with agent_run(AgentNames.PLANNER):
        headers = ApimLlmClient()._tracking_headers(
            {"x-rok-agent-name": AgentNames.GUARDRAILS})

    assert headers["x-rok-agent-name"] == AgentNames.GUARDRAILS
    assert headers["x-rok-user-email"] == "jane@example.com"


def test_the_headers_reach_the_gateway_request(caller, monkeypatch):
    """The tracking headers are on the actual HTTP request sent to APIM."""
    sent = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    def fake_post(url, headers=None, **kwargs):
        sent.update(headers or {})
        return _Response()

    client = ApimLlmClient()
    monkeypatch.setattr(client, "get_token", lambda force_refresh=False: "token")
    monkeypatch.setattr(client, "_base_url", lambda: "https://gateway.example")
    monkeypatch.setattr(apim_llm_utils.requests, "post", fake_post)

    with agent_run(AgentNames.SLIDE_CONTENT):
        assert client.chat_text("system", [{"role": "user", "content": "hi"}],
                                model="gpt-4o") == "ok"

    assert sent["x-rok-user-id"] == "42"
    assert sent["x-rok-user-email"] == "jane@example.com"
    assert sent["x-rok-agent-name"] == AgentNames.SLIDE_CONTENT
    assert sent["x-rok-agent-version"] == AppConstants.AGENT_VERSION
