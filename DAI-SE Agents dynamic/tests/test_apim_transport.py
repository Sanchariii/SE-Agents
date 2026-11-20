"""Gateway transport: classified, bounded retries and safe diagnostics.

`requests.post` and the backoff sleep are patched, so no network or waiting happens.
"""

import json
import logging

import pytest
import requests

from app.config.constants import ApimConstants
from app.utilities import apim_llm_utils as mod
from app.utilities import run_control
from app.utilities.llm_errors import (ModelAuthError, ModelRateLimitError, ModelTimeoutError,
                                      TransientGatewayError)
from app.utilities.logger_util import log_context

_URL = "https://gateway.example.com/foundry/models/chat/completions"
_SECRET = "Bearer secret-token-xyz"


class _Resp:
    """Minimal stand-in for `requests.Response`."""

    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)
        self.headers = {"content-type": "application/json", **(headers or {})}

    def json(self) -> dict:
        return self._body


@pytest.fixture
def client(monkeypatch):
    c = mod.ApimLlmClient()
    monkeypatch.setattr(c, "get_token",
                        lambda force_refresh=False: "tok-new" if force_refresh else "tok")
    sleeps: list[float] = []
    monkeypatch.setattr(mod.run_control, "sleep", sleeps.append)
    c.sleeps = sleeps
    return c


def _script(monkeypatch, responses: list) -> list[dict]:
    calls: list[dict] = []

    def post(url, headers=None, json=None, timeout=None, verify=None):
        calls.append({"headers": dict(headers or {}), "json": dict(json or {})})
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(mod.requests, "post", post)
    return calls


def _post(client, body: dict | None = None):
    return client._post(_URL, body or {"messages": [{"role": "user", "content": "hi"}],
                                        "max_tokens": 10},
                        {"Authorization": _SECRET, "api-key": "subkey-123456"},
                        route="foundry", model="claude-sonnet-4-6")


def test_a_transient_500_is_retried_and_recovers(client, monkeypatch):
    calls = _script(monkeypatch, [_Resp(500, {"error": "boom"}), _Resp(200, {"ok": 1})])

    assert _post(client) == {"ok": 1}
    assert len(calls) == 2
    assert len(client.sleeps) == 1


def test_exhausted_5xx_raises_a_typed_error_with_diagnostics_and_no_final_sleep(client,
                                                                              monkeypatch):
    _script(monkeypatch, [_Resp(503, {"error": "down"}, {"apim-request-id": "apim-42"})
                          for _ in range(ApimConstants.MAX_ATTEMPTS)])

    with pytest.raises(TransientGatewayError) as info:
        _post(client)

    err = info.value
    assert err.status == 503 and err.retryable
    assert err.apim_request_id == "apim-42"
    assert err.route == "foundry" and err.model == "claude-sonnet-4-6"
    assert "503 from gateway" in str(err) and "apim-42" in str(err)
    assert len(client.sleeps) == ApimConstants.MAX_ATTEMPTS - 1


def test_429_honours_retry_after(client, monkeypatch):
    _script(monkeypatch, [_Resp(429, headers={"retry-after": "7"}), _Resp(200, {"ok": 1})])

    _post(client)

    assert client.sleeps == [7.0]


def test_429_exhaustion_is_a_rate_limit_error(client, monkeypatch):
    _script(monkeypatch, [_Resp(429) for _ in range(ApimConstants.MAX_ATTEMPTS)])

    with pytest.raises(ModelRateLimitError):
        _post(client)


def test_401_refreshes_the_token_once_without_counting_as_a_retry(client, monkeypatch):
    calls = _script(monkeypatch, [_Resp(401), _Resp(200, {"ok": 1})])

    _post(client)

    assert calls[1]["headers"]["Authorization"] == "Bearer tok-new"
    assert client.sleeps == []


def test_403_is_not_retried(client, monkeypatch):
    calls = _script(monkeypatch, [_Resp(403)])

    with pytest.raises(ModelAuthError) as info:
        _post(client)

    assert not info.value.retryable
    assert len(calls) == 1


def test_timeouts_and_dropped_connections_are_retried(client, monkeypatch):
    _script(monkeypatch, [requests.ReadTimeout("slow"), requests.ConnectionError("reset"),
                          _Resp(200, {"ok": 1})])

    assert _post(client) == {"ok": 1}
    assert len(client.sleeps) == 2


def test_exhausted_timeouts_raise_a_timeout_error(client, monkeypatch):
    _script(monkeypatch, [requests.ConnectTimeout("no route")
                          for _ in range(ApimConstants.MAX_ATTEMPTS)])

    with pytest.raises(ModelTimeoutError):
        _post(client)


def test_a_rejected_param_is_shed_without_using_the_retry_budget(client, monkeypatch):
    calls = _script(monkeypatch, [
        _Resp(400, {"error": {"param": "temperature", "message": "unsupported"}}),
        _Resp(200, {"ok": 1})])

    _post(client, {"messages": [], "temperature": 0.2})

    assert "temperature" not in calls[1]["json"]
    assert client.sleeps == []


def test_logs_never_contain_request_credentials(client, monkeypatch, caplog):
    _script(monkeypatch, [_Resp(500, {"error": "boom"}), _Resp(200, {"ok": 1})])

    with caplog.at_level(logging.DEBUG):
        _post(client)

    text = caplog.text
    assert "secret-token-xyz" not in text
    assert "subkey-123456" not in text
    assert "gateway attempt failed" in text


def test_correlation_headers_come_from_the_log_context(client):
    with log_context(request_id="req-1", run_id="run-1", stage="planning", user_id="7"):
        headers = client._tracking_headers(None)

    assert headers["x-correlation-id"] == "req-1"
    assert headers["x-ms-client-request-id"] == "run-1:planning:1"
    assert headers["x-rok-user-id"] == "7"


def test_a_stopped_run_never_starts_another_gateway_call(client, monkeypatch):
    calls = _script(monkeypatch, [_Resp(200, {"ok": 1})])
    control = run_control.RunControl("run-1")
    control.request_cancel()

    with run_control.bind(control), pytest.raises(run_control.RunCancelledError):
        _post(client)

    assert calls == []


def test_a_shed_param_is_remembered_per_model(client, monkeypatch):
    calls = _script(monkeypatch, [
        _Resp(400, {"error": {"message": "`temperature` is deprecated for this model."}}),
        _Resp(200, {"ok": 1}), _Resp(200, {"ok": 2})])

    _post(client, {"messages": [], "temperature": 0.2})
    _post(client, {"messages": [], "temperature": 0.2})

    # the second call never sends temperature, so it costs one round trip, not two
    assert len(calls) == 3
    assert "temperature" in calls[0]["json"]
    assert "temperature" not in calls[1]["json"] and "temperature" not in calls[2]["json"]
