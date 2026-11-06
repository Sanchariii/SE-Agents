"""Context-local token accumulation for the currently active agent run.

Every LLM call in the service flows through the gateway client; on each response it
calls :func:`record_llm_usage`, which folds the token counts into whichever agent
run is active on the current context. Slide work is fanned out with
``ContextThreadPoolExecutor`` (which copies the context per task), so each worker
thread carries its own accumulator and totals never race across threads.

Usage is bucketed by model so one agent node that happens to call more than one
model still yields correct per-model tokens and cost.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field

from app.utilities.logger_util import get_logger

log = get_logger(__name__)


@dataclass
class ModelUsage:
    """Aggregated token usage for one model within a single agent run."""

    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def add(self, prompt: int, completion: int, cached: int) -> None:
        """Fold one LLM call's usage into this bucket.

        Args:
            prompt: Input/prompt tokens for the call.
            completion: Output/completion tokens for the call.
            cached: Cached input tokens reported for the call (0 when unknown).

        Returns:
            None.
        """
        self.llm_calls += 1
        self.input_tokens += prompt
        self.output_tokens += completion
        self.cached_input_tokens += cached


@dataclass
class AgentRunAccumulator:
    """Per-agent-run token totals, keyed by the model that produced them."""

    by_model: dict[str, ModelUsage] = field(default_factory=dict)

    def add(self, model: str | None, prompt: int, completion: int, cached: int) -> None:
        """Record one LLM call against its model bucket.

        Args:
            model: Model/route name used for the call; empty string when unknown.
            prompt: Input/prompt tokens.
            completion: Output/completion tokens.
            cached: Cached input tokens (0 when unknown).

        Returns:
            None.
        """
        self.by_model.setdefault(model or "", ModelUsage()).add(prompt, completion, cached)

    def total_calls(self) -> int:
        """Return the total number of LLM calls across every model bucket."""
        return sum(bucket.llm_calls for bucket in self.by_model.values())


#: The agent run active on the current context, or ``None`` outside any agent span.
_current_run: ContextVar[AgentRunAccumulator | None] = ContextVar(
    "analytics_agent_run", default=None)
#: The workflow run id active on the current context, bound so agent rows can point
#: at their parent even from a fanned-out worker thread.
_workflow_run_id: ContextVar[str | None] = ContextVar(
    "analytics_workflow_run_id", default=None)


def bind_agent_run(accumulator: AgentRunAccumulator) -> object:
    """Make ``accumulator`` the active agent run for the current context.

    Args:
        accumulator: The accumulator to receive subsequent LLM usage.

    Returns:
        A reset token to pass to :func:`unbind_agent_run`.
    """
    return _current_run.set(accumulator)


def unbind_agent_run(token: object) -> None:
    """Restore the previously active agent run.

    Args:
        token: The token returned by :func:`bind_agent_run`.

    Returns:
        None.
    """
    _current_run.reset(token)


def bind_workflow_run_id(run_id: str) -> object:
    """Bind the workflow run id for the current context (and inheriting workers).

    Args:
        run_id: The parent workflow run id.

    Returns:
        A reset token to pass to :func:`unbind_workflow_run_id`.
    """
    return _workflow_run_id.set(run_id)


def unbind_workflow_run_id(token: object) -> None:
    """Restore the previously bound workflow run id.

    Args:
        token: The token returned by :func:`bind_workflow_run_id`.

    Returns:
        None.
    """
    _workflow_run_id.reset(token)


def current_workflow_run_id() -> str | None:
    """Return the workflow run id active on the current context, if any."""
    return _workflow_run_id.get()


def record_llm_usage(model: str | None, prompt_tokens: int, completion_tokens: int,
                     cached_tokens: int = 0) -> None:
    """Fold one LLM call's token usage into the active agent run.

    Called by the gateway client on every chat/embedding response. A no-op when no
    agent run is active (for example, a warmup call), and never raises — telemetry
    must not disturb the model call it is measuring.

    Args:
        model: Model/route name used for the call.
        prompt_tokens: Input/prompt tokens.
        completion_tokens: Output/completion tokens.
        cached_tokens: Cached input tokens (0 when unknown).

    Returns:
        None.
    """
    try:
        accumulator = _current_run.get()
        if accumulator is None:
            return
        accumulator.add(model, prompt_tokens or 0, completion_tokens or 0, cached_tokens or 0)
    except Exception as exc:                        # noqa: BLE001
        # Capture is best effort: a bad usage payload must never break the model call.
        log.warning("failed to record llm usage for model %s: %s", model, exc)
