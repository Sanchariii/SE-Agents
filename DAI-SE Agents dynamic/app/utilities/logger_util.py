"""Logging with request, session and build correlation.

These pieces of identity can end up on a log line:

* ``request_id`` / ``user_id`` - set once per HTTP request by the server middleware.
* ``session_id`` - the deck session the work belongs to.
* ``run_id`` / ``stage`` / ``attempt`` - which workflow run, which graph node inside
  it, and which internal recovery attempt of that node wrote the line.
* ``build_id`` / ``slide`` - which deck build, and which slide inside it, wrote the
  line.

The last three matter because slide work is fanned out across a worker pool. Two decks
building at once both log "slide 9 tier 1 attempt 1/3", and without a session and a
build on the line those two streams are indistinguishable - which is exactly what made
a real blank-slide incident un-debuggable.

All of them are :class:`~contextvars.ContextVar`, so a worker thread only inherits them if
the submitting thread's context is copied into it. A plain ``ThreadPoolExecutor`` does
not do that copy; :class:`ContextThreadPoolExecutor` does. Use it anywhere deck work is
fanned out.

A call site may still pass ``extra={"session_id": ...}`` explicitly; an explicit value
always wins over the ambient one.
"""

import contextvars
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterable, Iterator

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
user_id_ctx: ContextVar[str] = ContextVar("user_id", default="-")
#: Caller's email, sent to the gateway for usage attribution. Never logged.
user_email_ctx: ContextVar[str] = ContextVar("user_email", default="")
#: Sub-agent making the model call; bound by analytics agent_run / ingestion_run.
agent_name_ctx: ContextVar[str] = ContextVar("agent_name", default="")
#: The deck session. Set for the whole of a build, inherited by every worker.
session_id_ctx: ContextVar[str] = ContextVar("session_id", default="")
#: Which build of that session: the slide working directory's unique suffix.
build_id_ctx: ContextVar[str] = ContextVar("build_id", default="")
#: Which slide inside the build. Empty outside per-slide work.
slide_ctx: ContextVar[str] = ContextVar("slide", default="")
#: The top-level workflow run (one user operation). Separates concurrent runs of a session.
run_id_ctx: ContextVar[str] = ContextVar("run_id", default="")
#: The workflow stage (graph node) currently executing inside the run.
stage_ctx: ContextVar[str] = ContextVar("stage", default="")
#: The internal recovery attempt of that stage; empty on the first attempt.
attempt_ctx: ContextVar[str] = ContextVar("attempt", default="")

#: Bindable name -> the variable holding it, for `log_context`.
_BINDABLE: dict[str, ContextVar[str]] = {
    "request_id": request_id_ctx,
    "user_id": user_id_ctx,
    "session_id": session_id_ctx,
    "build_id": build_id_ctx,
    "slide": slide_ctx,
    "run_id": run_id_ctx,
    "stage": stage_ctx,
    "attempt": attempt_ctx,
}

#: `scope` carries session/build/slide and collapses to "" when none are set, so lines
#: outside a deck build read exactly as they did before.
_FORMAT_STR = (
    "%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s] "
    "[request_id=%(request_id)s] [user_id=%(user_id)s]%(scope)s "
    "[%(filename)s]:[%(funcName)s]:[%(lineno)d] %(message)s"
)


class RequestIdFilter(logging.Filter):
    """Stamp the ambient correlation identity onto every record.

    Runs on the handler, so it sees records from every logger in the process -
    including third-party ones that know nothing about these fields. Every attribute
    the formatter references is therefore always set here, never conditionally.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = getattr(record, "request_id", None) or request_id_ctx.get()
        record.user_id = getattr(record, "user_id", None) or user_id_ctx.get()
        # An explicit extra={"session_id": ...} beats the ambient value: the caller
        # knows which session it means, and hundreds of call sites already pass it.
        session = getattr(record, "session_id", None) or session_id_ctx.get()
        build = getattr(record, "build_id", None) or build_id_ctx.get()
        slide = getattr(record, "slide", None) or slide_ctx.get()
        run = getattr(record, "run_id", None) or run_id_ctx.get()
        stage = getattr(record, "stage", None) or stage_ctx.get()
        attempt = getattr(record, "attempt", None) or attempt_ctx.get()
        record.session_id, record.build_id, record.slide = session, build, slide
        record.run_id, record.stage, record.attempt = run, stage, attempt
        parts = [f"{name}={value}" for name, value in
                 (("session", session), ("run", run), ("stage", stage),
                  ("attempt", attempt), ("build", build), ("slide", slide)) if value]
        record.scope = f" [{' '.join(parts)}]" if parts else ""
        return True


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Bind correlation fields for the duration of the block.

    Args:
        **fields: Any of `request_id`, `user_id`, `session_id`, `run_id`, `stage`,
            `attempt`, `build_id`, `slide`.
            An empty or None value is ignored rather than binding a blank, so a caller
            can pass an optional id without having to branch on it. Unknown names are
            ignored too - this is logging, and it must never raise into real work.

    Yields:
        None. Every binding is reset on the way out, including on an exception.
    """
    tokens = []
    for name, value in fields.items():
        variable = _BINDABLE.get(name)
        if variable is None or value is None or value == "":
            continue
        tokens.append((variable, variable.set(str(value))))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    """A pool whose workers inherit the submitting thread's context.

    `ContextVar` values are per-thread, so work handed to a plain pool runs with the
    defaults and logs `request_id=-` with no session attached. Copying the context at
    submit time is the whole fix: a fresh copy per task, so tasks stay isolated from
    one another and anything a task binds cannot leak back to the caller.

    `Executor.map` is built on `submit`, so overriding `submit` covers both.
    """

    def submit(self, fn, /, *args, **kwargs):
        """Submit `fn`, running it inside a copy of the caller's context."""
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


_MASK = "***REDACTED***"
#: Shortest known secret value that is redacted by exact match; shorter values would
#: mangle ordinary log text more than they protect.
_MIN_SECRET_LEN = 4

#: Credential shapes masked in every formatted line (message, args and traceback),
#: regardless of which logger or library produced them.
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*"), rf"\1 {_MASK}"),
    # Bare JWTs (access / id / refresh tokens) that appear without a Bearer prefix.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), _MASK),
    # key=value / "key": "value" pairs, incl. HTTP headers and Azure connection strings.
    (re.compile(
        r"(?i)((?<![\w-])(?:client[_-]?secret|ocp-apim-subscription-key|subscription[_-]?key"
        r"|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|sas[_-]?token"
        r"|password|passwd|pwd|accountkey|sharedaccesskey|sharedaccesssignature|sig)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\"'\s&;,}]+"), rf"\1{_MASK}"),
    # Credentials embedded in URLs: scheme://user:password@host (password may hold '@').
    (re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]*:)[^\s/]+(@)"), rf"\1{_MASK}\2"),
)

#: Exact secret values registered at startup (see ``setup_logging``), longest first so
#: a value containing another is masked whole.
_known_secrets: tuple[str, ...] = ()


def redact(text: str) -> str:
    """Mask credentials in a piece of log text.

    Args:
        text: A fully formatted log line (may include a traceback).

    Returns:
        ``text`` with registered secret values and known credential shapes replaced by
        a mask. Never raises - on any failure the original text is masked wholesale.
    """
    try:
        for secret in _known_secrets:
            if secret in text:
                text = text.replace(secret, _MASK)
        for pattern, replacement in _REDACTION_PATTERNS:
            text = pattern.sub(replacement, text)
        return text
    except Exception:  # noqa: BLE001 - logging must never raise into real work
        return _MASK


#: Attributes every LogRecord has, plus the ones `RequestIdFilter` stamps; anything else
#: on a record came from a call site's ``extra={...}`` and is rendered as ``k=v``.
_RESERVED_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "scope", "color_message", *_BINDABLE}
#: Bound on the rendered ``extra`` suffix so one call site can't flood a line.
_EXTRA_MAX_CHARS = 1000


def _extra_suffix(record: logging.LogRecord) -> str:
    """Render a record's call-site ``extra`` fields as a bounded `` | k=v ...`` suffix.

    Args:
        record: The record being formatted.

    Returns:
        The suffix, or ``""`` when the call site passed no extra fields.
    """
    pairs = [f"{k}={v}" for k, v in record.__dict__.items()
             if k not in _RESERVED_ATTRS and not k.startswith("_")]
    if not pairs:
        return ""
    text = " ".join(pairs)
    if len(text) > _EXTRA_MAX_CHARS:
        text = text[:_EXTRA_MAX_CHARS] + "..."
    return f" | {text}"


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts secrets from the final rendered line.

    Redacting after formatting covers the message, its ``%s`` args, ``extra`` fields
    and any exception traceback in one place. Call-site ``extra`` fields the format
    string does not name are appended as ``k=v`` so they are never silently dropped.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format ``record`` and mask any credentials in the result.

        Args:
            record: The log record to render.

        Returns:
            The rendered, redacted log line.
        """
        rendered = super().format(record)
        suffix = _extra_suffix(record)
        if suffix:
            # Keep the suffix on the message line, ahead of any traceback.
            head, sep, tail = rendered.partition("\n")
            rendered = f"{head}{suffix}{sep}{tail}"
        return redact(rendered)


_FORMAT = RedactingFormatter(_FORMAT_STR)


def register_secrets(values: Iterable[str]) -> None:
    """Register exact secret values to be masked in every log line.

    Args:
        values: Credential values (keys, passwords, connection strings, tokens).
            Empty or very short values are ignored.

    Returns:
        None.
    """
    global _known_secrets
    merged = set(_known_secrets) | {v for v in values
                                    if isinstance(v, str) and len(v) >= _MIN_SECRET_LEN}
    _known_secrets = tuple(sorted(merged, key=len, reverse=True))


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    """Configure the root logger with correlation stamping and secret redaction.

    Args:
        level: Root log level name (e.g. ``"INFO"``).
        secrets: Exact credential values to mask in every log line, typically
            ``get_settings().secret_values()``.

    Returns:
        None.
    """
    register_secrets(secrets)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_FORMAT)
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # HTTP client / SDK loggers can dump request headers (Authorization, api-key,
    # Ocp-Apim-Subscription-Key) at DEBUG/INFO; keep them at WARNING.
    for noisy in ("azure", "azure.core.pipeline.policies.http_logging_policy", "httpx",
                  "httpcore", "urllib3", "requests", "openai", "msal", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Uvicorn installs its own handlers; route them through the redacting root handler.
    for name in ("uvicorn", "uvicorn.error"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
