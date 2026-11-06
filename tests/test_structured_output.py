"""Structured (JSON) output: extraction, truncation, repair and typed failures."""

import json
import logging

import pytest
from pydantic import BaseModel

from app.utilities.apim_llm_utils import ApimLlmClient
from app.utilities.llm_errors import (ModelOutputTruncatedError, StructuredOutputParseError,
                                      StructuredOutputValidationError)
from app.utilities.structured_output import extract_json, structured_call


class Plan(BaseModel):
    title: str
    points: list[str]


class _FakeClient:
    """Scripted gateway: each reply is ``(text, finish_reason)``."""

    reply_text = staticmethod(ApimLlmClient.reply_text)
    usage_of = staticmethod(ApimLlmClient.usage_of)

    def __init__(self, replies: list[tuple[str, str]]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, messages, model, route=None, max_tokens=0, tracking=None, **kwargs):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, **kwargs})
        text, finish = self.replies.pop(0)
        return {"choices": [{"message": {"content": text}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20}}


_VALID = json.dumps({"title": "Deck", "points": ["a", "b"]})


def _call(client, **overrides):
    kwargs = dict(operation="deck_plan", schema=Plan, system="sys",
                  messages=[{"role": "user", "content": "plan it"}], model="gpt-4o",
                  max_tokens=1000, max_budget=4000, attempts=3)
    kwargs.update(overrides)
    return structured_call(client, **kwargs)


def test_a_code_fence_inside_a_json_string_no_longer_truncates_it():
    """Regression: the old extractor cut at the first ``` anywhere, mid-string."""
    reply = '```json\n{"title": "Use ``` fences", "points": ["x ```y```"]}\n```'
    client = _FakeClient([(reply, "stop")])

    plan = _call(client)

    assert plan.title == "Use ``` fences"
    assert plan.points == ["x ```y```"]


def test_extract_json_ignores_trailing_prose_and_honours_an_opener():
    assert extract_json('Here you go: {"a": 1} hope it helps {') == '{"a": 1}'
    assert extract_json('{"note": 1} then [1, 2]', opener="[") == "[1, 2]"


def test_malformed_json_is_regenerated_under_a_strict_instruction():
    client = _FakeClient([('{"title": "Deck", "points": ["a",', "stop"), (_VALID, "stop")])

    plan = _call(client)

    assert plan.title == "Deck"
    second = client.calls[1]["messages"]
    assert any("not valid JSON" in str(m.get("content")) for m in second)


def test_truncated_output_grows_the_budget_instead_of_resending_the_same_request():
    client = _FakeClient([('{"title": "Deck", "points": ["a', "length"), (_VALID, "stop")])

    _call(client)

    assert [c["max_tokens"] for c in client.calls] == [1000, 2000]


def test_truncation_at_the_budget_ceiling_asks_for_concision_then_gives_up():
    truncated = ('{"title": "Deck", "points": ["a', "length")
    client = _FakeClient([truncated, truncated, truncated])

    with pytest.raises(ModelOutputTruncatedError) as info:
        _call(client, max_tokens=4000, max_budget=4000)

    assert info.value.retryable
    assert len(client.calls) == 2            # the third identical request is never sent
    assert "cut off" in client.calls[1]["messages"][0]["content"]


def test_exhausted_parse_failures_raise_a_typed_retryable_error_chained_to_the_cause():
    client = _FakeClient([("not json at all", "stop")] * 3)

    with pytest.raises(StructuredOutputParseError) as info:
        _call(client)

    assert info.value.retryable
    assert isinstance(info.value.__cause__, json.JSONDecodeError)


def test_a_schema_mismatch_is_repaired_with_the_validation_errors():
    client = _FakeClient([('{"title": "Deck"}', "stop"), (_VALID, "stop")])

    plan = _call(client)

    assert plan.points == ["a", "b"]
    assert any("points" in str(m.get("content")) for m in client.calls[1]["messages"])


def test_exhausted_schema_failures_raise_a_validation_error():
    client = _FakeClient([('{"title": "Deck"}', "stop")] * 3)

    with pytest.raises(StructuredOutputValidationError):
        _call(client)


def test_a_failed_attempt_logs_finish_reason_and_a_bounded_snippet(caplog):
    huge = '{"title": "' + "x" * 5000
    client = _FakeClient([(huge, "stop"), (_VALID, "stop")])

    with caplog.at_level(logging.WARNING):
        _call(client)

    record = next(r for r in caplog.records if "structured output attempt" in r.getMessage())
    assert record.finish_reason == "stop"
    assert record.operation == "deck_plan"
    assert len(record.output_prefix) <= 300 and len(record.output_suffix) <= 300
    assert record.response_chars == len(huge)
