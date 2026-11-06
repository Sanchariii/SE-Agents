"""Typed, classified failures for gateway model calls and structured output.

Every error carries whether it is worth retrying and a stable ``error_code``, so the
transport, the structured-output layer and the stage-recovery wrapper can each decide
"retry or give up" without string-matching messages. :func:`to_user_message` turns
any exception into a code and a message that is safe to show a user; the raw detail
stays in the server log.
"""

from app.config.constants import RunErrorCodes
from app.config.exceptions import UpstreamError
from app.utilities.run_control import RunCancelledError, RunFencedError


class LlmError(UpstreamError):
    """A model call through the gateway failed.

    Attributes:
        retryable: Whether repeating the same operation could succeed.
        error_code: Stable code from :class:`RunErrorCodes`.
        status: Gateway HTTP status, when there was a response.
        route: Gateway route (azure-openai | foundry | bedrock | embeddings).
        model: Model or deployment name.
        apim_request_id: Correlation id returned by the gateway, when present.
        attempts: Transport attempts made before giving up.
        body_excerpt: Redacted, bounded excerpt of the response body.
    """

    retryable = False
    error_code = RunErrorCodes.INTERNAL_ERROR

    def __init__(self, detail: str, *, status: int | None = None, route: str = "",
                 model: str = "", apim_request_id: str = "", attempts: int = 0,
                 body_excerpt: str = "") -> None:
        super().__init__(detail)
        self.status = status
        self.route = route
        self.model = model
        self.apim_request_id = apim_request_id
        self.attempts = attempts
        self.body_excerpt = body_excerpt


class TransientGatewayError(LlmError):
    """5xx or a dropped connection from the gateway or its backend."""
    retryable = True
    error_code = RunErrorCodes.LLM_GATEWAY_UNAVAILABLE


class ModelRateLimitError(LlmError):
    """429 from the gateway."""
    retryable = True
    error_code = RunErrorCodes.LLM_RATE_LIMITED


class ModelTimeoutError(LlmError):
    """The call did not complete within its timeout or overall deadline."""
    retryable = True
    error_code = RunErrorCodes.LLM_TIMEOUT


class ModelAuthError(LlmError):
    """401/403: the service principal or subscription key was rejected."""
    retryable = False
    error_code = RunErrorCodes.LLM_AUTH_FAILED


class PermanentModelRequestError(LlmError):
    """A 4xx the request itself caused (bad model, deployment or payload)."""
    retryable = False
    error_code = RunErrorCodes.LLM_REQUEST_INVALID


class StructuredOutputError(LlmError):
    """The model answered, but not with usable structured output."""
    retryable = True
    error_code = RunErrorCodes.LLM_OUTPUT_INVALID


class StructuredOutputParseError(StructuredOutputError):
    """The reply was not valid JSON."""


class StructuredOutputValidationError(StructuredOutputError):
    """The reply was JSON but did not match the target schema."""


class ModelOutputTruncatedError(StructuredOutputError):
    """The reply hit the output-token limit, even after growing the budget."""


#: User-facing copy per error code. Never includes exception text.
_USER_MESSAGES = {
    RunErrorCodes.LLM_GATEWAY_UNAVAILABLE:
        "The AI service is temporarily unavailable. Please try again in a few minutes.",
    RunErrorCodes.LLM_RATE_LIMITED:
        "The AI service is busy right now. Please try again in a few minutes.",
    RunErrorCodes.LLM_TIMEOUT:
        "The AI service took too long to respond. Please try again.",
    RunErrorCodes.LLM_AUTH_FAILED:
        "The AI service rejected this application's credentials. Please contact support.",
    RunErrorCodes.LLM_REQUEST_INVALID:
        "The AI service could not process this request. Please contact support.",
    RunErrorCodes.LLM_OUTPUT_INVALID:
        "The AI returned an incomplete answer while preparing the deck. Please try again.",
    RunErrorCodes.WORKER_LOST:
        "The deck generation was interrupted. Please try again.",
    RunErrorCodes.WORKER_SHUTDOWN:
        "The service restarted while generating the deck. Please try again.",
    RunErrorCodes.CANCELLED_BY_USER:
        "Stopped. Nothing from this request was saved.",
    RunErrorCodes.SERVICE_UNAVAILABLE:
        "The service is temporarily unavailable. Please try again shortly.",
    RunErrorCodes.INTERNAL_ERROR:
        "Something went wrong while generating the deck. Please try again.",
}

#: Codes a user can sensibly retry by sending the request again.
_RETRYABLE_CODES = frozenset({
    RunErrorCodes.LLM_GATEWAY_UNAVAILABLE, RunErrorCodes.LLM_RATE_LIMITED,
    RunErrorCodes.LLM_TIMEOUT, RunErrorCodes.LLM_OUTPUT_INVALID,
    RunErrorCodes.WORKER_LOST, RunErrorCodes.WORKER_SHUTDOWN,
    RunErrorCodes.SERVICE_UNAVAILABLE, RunErrorCodes.INTERNAL_ERROR,
})


def message_for(error_code: str) -> str:
    """Return the user-safe message for ``error_code``.

    Args:
        error_code: A :class:`RunErrorCodes` value.

    Returns:
        The message; unknown codes get the generic internal-error copy.
    """
    return _USER_MESSAGES.get(error_code, _USER_MESSAGES[RunErrorCodes.INTERNAL_ERROR])


def is_retryable_code(error_code: str | None) -> bool:
    """True when a run that ended with ``error_code`` is worth resubmitting."""
    return bool(error_code) and error_code in _RETRYABLE_CODES


def to_user_message(exc: BaseException) -> tuple[str, str, bool]:
    """Classify any exception into ``(error_code, safe_message, retryable)``.

    Args:
        exc: The exception that ended a run or a stage.

    Returns:
        The stable code, a message safe to show a user, and whether resending the
        request is worth it.
    """
    if isinstance(exc, RunCancelledError) or (
            isinstance(exc, RunFencedError) and exc.analytics_cancelled):
        code = RunErrorCodes.CANCELLED_BY_USER
    elif isinstance(exc, RunFencedError):
        code = RunErrorCodes.WORKER_LOST
    elif isinstance(exc, LlmError):
        code = exc.error_code
    else:
        code = RunErrorCodes.INTERNAL_ERROR
    return code, message_for(code), code in _RETRYABLE_CODES


def is_retryable(exc: BaseException) -> bool:
    """True when ``exc`` is a model failure worth retrying at a higher layer."""
    return isinstance(exc, LlmError) and exc.retryable
