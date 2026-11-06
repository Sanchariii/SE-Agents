"""Structured (JSON) model output: extraction, truncation handling, repair, typing.

Every structured call used to do ``json.loads(reply)`` inline. This module keeps the
steps apart so each failure is detected and handled for what it is:

1. **transport** - ``client.complete``; typed gateway errors propagate unchanged.
2. **extraction** - reply text plus a normalised finish reason.
3. **truncation** - detected *before* parsing; the budget grows, then the model is
   told to be concise. An identical request is never resent.
4. **JSON extraction** - only a wrapping code fence is stripped and the first JSON
   value is decoded, so a fence or prose *inside* the JSON can no longer cut it
   mid-string (the cause of "Unterminated string starting at ...").
5. **parse + schema validation.**
6. **repair** - a parse failure regenerates under a strict instruction (a large broken
   output is not echoed back); a schema failure echoes the output with the compact
   validation errors, since those fixes are small.
7. **classification** - exhausted attempts raise a typed, retryable
   :class:`StructuredOutputError` chained to the last parser error.
"""

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.config.constants import ApimConstants, LlmConstants
from app.utilities import run_control
from app.utilities.llm_errors import (ModelOutputTruncatedError, StructuredOutputError,
                                      StructuredOutputParseError,
                                      StructuredOutputValidationError)
from app.utilities.logger_util import get_logger
from app.utilities.metrics_util import Metrics, incr

log = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

#: Finish/stop reasons that mean "the output-token limit cut the reply off", across the
#: OpenAI (finish_reason), Anthropic (stop_reason) and Bedrock (stopReason) shapes.
_TRUNCATED_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})
_LEADING_FENCE = re.compile(r"^```[A-Za-z0-9_+-]*[ \t]*\r?\n?")

_CONCISE_NOTE = ("IMPORTANT: your previous answer was cut off by the output limit. "
                 "Answer again with the same JSON structure but keep every text field "
                 "short (key points: a few words each) so the complete JSON fits.")
_STRICT_NOTE = ("IMPORTANT: your previous answer was not valid JSON ({error}). Reply with "
                "ONLY one complete, valid JSON value matching the required structure - "
                "no prose, no code fences, every string closed and properly escaped.")


def finish_reason(response: dict) -> str:
    """Return the provider's finish/stop reason for a gateway response, or ``""``."""
    if response.get("stop_reason"):
        return str(response["stop_reason"])
    choices = response.get("choices")
    if choices and isinstance(choices, list):
        return str(choices[0].get("finish_reason") or "")
    return str(response.get("stopReason") or "")


def is_truncated(response: dict) -> bool:
    """True when the reply was cut off by the output-token limit."""
    return finish_reason(response).lower() in _TRUNCATED_REASONS


def extract_json(text: str, opener: str | None = None) -> str:
    """Isolate the JSON value in a model reply.

    Strips one wrapping code fence, starts at the first ``{`` or ``[`` (or at
    ``opener`` when given) and decodes exactly one JSON value, ignoring trailing
    prose. Never cuts at a fence or bracket *inside* the JSON.

    Args:
        text: The raw reply text.
        opener: Force the value to start at the first occurrence of this character.

    Returns:
        The JSON text, or the best-effort remainder when it does not decode (so the
        caller's parser reports the real error position).
    """
    body = text.strip()
    fence = _LEADING_FENCE.match(body)
    if fence:
        body = body[fence.end():]
        stripped = body.rstrip()
        if stripped.endswith("```"):
            body = stripped[:-3]
    if opener:
        start = body.find(opener)
    else:
        starts = [i for i in (body.find("{"), body.find("[")) if i != -1]
        start = min(starts) if starts else -1
    if start > 0:
        body = body[start:]
    try:
        _, end = json.JSONDecoder().raw_decode(body)
        return body[:end]
    except json.JSONDecodeError:
        return body.strip()


def parse_json(text: str, opener: str | None = None) -> Any:
    """Extract and decode the JSON value in a model reply.

    Raises:
        json.JSONDecodeError: The reply holds no complete JSON value.
    """
    return json.loads(extract_json(text, opener))


def _snippets(text: str) -> tuple[str, str]:
    n = LlmConstants.DIAG_SNIPPET_CHARS
    return text[:n], (text[-n:] if len(text) > n else "")


def _log_failure(*, operation: str, schema: str, model: str, route: str | None,
                 attempt: int, attempts: int, response: dict, usage: dict,
                 max_tokens: int, text: str, error: BaseException) -> None:
    prefix, suffix = _snippets(text)
    log.warning(
        "structured output attempt %d/%d failed for %s: %s",
        attempt, attempts, operation, type(error).__name__,
        extra={"operation": operation, "schema": schema, "model": model,
               "route": route or "-", "finish_reason": finish_reason(response) or "-",
               "input_tokens": usage.get("prompt"), "output_tokens": usage.get("completion"),
               "max_tokens": max_tokens, "response_chars": len(text),
               "error": str(error)[:300], "output_prefix": prefix,
               "output_suffix": suffix})


def structured_call(client, *, operation: str, schema: type[T], system: str,
                    messages: list[dict], model: str, route: str | None = None,
                    max_tokens: int = LlmConstants.JSON_MAX_TOKENS,
                    max_budget: int = ApimConstants.JSON_MAX_BUDGET,
                    attempts: int = LlmConstants.STRUCTURED_ATTEMPTS,
                    tracking: dict | None = None) -> T:
    """Generate, parse and validate one structured model answer with bounded repair.

    Args:
        client: The ``ApimLlmClient`` used for transport.
        operation: Stable name of the call for logs (e.g. ``deck_plan``).
        schema: Pydantic model the answer must validate against.
        system: System prompt.
        messages: Conversation after the system prompt.
        model: Model name; the route is inferred when ``route`` is None.
        route: Explicit gateway route.
        max_tokens: Initial output budget.
        max_budget: Ceiling the budget may grow to on truncation.
        attempts: Generations before giving up.
        tracking: Optional usage-attribution headers.

    Returns:
        The validated schema instance.

    Raises:
        StructuredOutputError: A typed, retryable failure after ``attempts``
            generations (parse, validation or truncation), chained to the last cause.
        LlmError: A transport failure from the gateway, unchanged.
    """
    from app.utilities.apim_llm_utils import route_for_model

    schema_name = schema.__name__
    resolved_route = route or route_for_model(model)
    extra_body = ({"response_format": {"type": "json_object"}}
                  if resolved_route in LlmConstants.JSON_MODE_ROUTES else {})
    notes: list[str] = []
    history: list[dict] = list(messages)
    budget = max_tokens
    last_error: StructuredOutputError | None = None
    last_cause: BaseException | None = None

    for attempt in range(1, attempts + 1):
        system_text = "\n\n".join([system, *notes]) if notes else system
        response = client.complete([{"role": "system", "content": system_text}, *history],
                                   model=model, route=route, max_tokens=budget,
                                   tracking=tracking, **extra_body)
        text = client.reply_text(response).strip()
        usage = client.usage_of(response)
        failure = dict(operation=operation, schema=schema_name, model=model, route=route,
                       attempt=attempt, attempts=attempts, response=response, usage=usage,
                       max_tokens=budget, text=text)

        if is_truncated(response):
            last_cause = None
            last_error = ModelOutputTruncatedError(
                f"{operation}: output truncated at {budget} tokens "
                f"(finish_reason={finish_reason(response)}, {len(text)} chars)", model=model,
                route=resolved_route)
            _log_failure(**failure, error=last_error)
            incr(Metrics.STRUCTURED_PARSE_FAILURES, operation=operation, reason="truncated")
            if budget < max_budget:
                budget = min(budget * 2, max_budget)
            elif _CONCISE_NOTE not in notes:
                notes.append(_CONCISE_NOTE)
            else:
                break                    # same limit, same instruction: retrying is futile
            run_control.note_retry(last_error.error_code, attempt)
            continue

        raw = extract_json(text)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_cause = exc
            last_error = StructuredOutputParseError(
                f"{operation}: invalid JSON ({exc.msg} at char {exc.pos} of {len(raw)})",
                model=model, route=resolved_route)
            _log_failure(**failure, error=exc)
            incr(Metrics.STRUCTURED_PARSE_FAILURES, operation=operation, reason="parse")
            strict = _STRICT_NOTE.format(error=f"{exc.msg} at char {exc.pos}")
            if len(raw) <= LlmConstants.REPAIR_ECHO_MAX_CHARS:
                history = [*messages, {"role": "assistant", "content": raw},
                           {"role": "user", "content": strict}]
            else:
                # Echoing tens of KB of broken JSON doubles the input for little gain;
                # regenerate from the original conversation under a strict instruction.
                history = list(messages)
                notes = [n for n in notes if not n.startswith("IMPORTANT: your previous answer was not")]
                notes.append(strict)
            run_control.note_retry(last_error.error_code, attempt)
            continue

        try:
            result = schema.model_validate(data)
        except ValidationError as exc:
            last_cause = exc
            errors = "; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}"
                for e in exc.errors()[:10])
            last_error = StructuredOutputValidationError(
                f"{operation}: output does not match {schema_name} ({errors[:300]})",
                model=model, route=resolved_route)
            _log_failure(**failure, error=exc)
            incr(Metrics.STRUCTURED_PARSE_FAILURES, operation=operation, reason="schema")
            correction = (f"That JSON does not match the required structure: {errors}. "
                          "Reply with ONLY the corrected JSON.")
            if len(raw) <= LlmConstants.REPAIR_ECHO_MAX_CHARS:
                history = [*messages, {"role": "assistant", "content": raw},
                           {"role": "user", "content": correction}]
            else:
                history = list(messages)
                notes.append(f"IMPORTANT: {correction}")
            run_control.note_retry(last_error.error_code, attempt)
            continue

        if attempt > 1:
            log.info("structured output recovered for %s on attempt %d", operation, attempt)
            incr(Metrics.STRUCTURED_RECOVERIES, operation=operation, attempts=attempt)
            run_control.note_recovered()
        return result

    assert last_error is not None
    raise last_error from last_cause
