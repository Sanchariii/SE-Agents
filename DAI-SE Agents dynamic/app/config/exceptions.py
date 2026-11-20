"""Application exceptions and the FastAPI handlers that turn them into responses.

Handlers exist so business code can raise a domain error without importing
FastAPI, and so an unexpected exception never leaks a stack trace to a client.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.utilities.logger_util import get_logger, request_id_ctx

log = get_logger(__name__)


def _auth_challenge(status_code: int) -> dict[str, str]:
    """Return the RFC 7235 challenge header a 401 must carry.

    Args:
        status_code: Status code of the response being built.

    Returns:
        The ``WWW-Authenticate`` header for a 401, otherwise an empty mapping.
    """
    return {"WWW-Authenticate": "Bearer"} if status_code == 401 else {}


class AppError(Exception):
    """Base for expected, client-reportable failures."""
    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnauthorizedError(AppError):
    """No usable credential: unauthenticated, expired or revoked session."""
    status_code = 401


class ForbiddenError(AppError):
    """Authenticated, but not entitled to the application or the action."""
    status_code = 403


class NotFoundError(AppError):
    status_code = 404


class ConflictError(AppError):
    status_code = 409


class UnsupportedMediaTypeError(AppError):
    status_code = 415

    def __init__(self, detail: str, supported: list[str] | None = None) -> None:
        super().__init__(detail)
        self.supported = supported or []


class UpstreamError(AppError):
    """A dependency (Azure OpenAI, Search, Blob, Postgres) failed."""
    status_code = 502


class TlsConfigurationError(RuntimeError):
    """Configured CA bundle path does not exist."""


class SlideRenderError(RuntimeError):
    """LibreOffice could not render a slide to an image. Internal; never sent to clients."""


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        body: dict = {"detail": exc.detail, "correlation_id": request_id_ctx.get()}
        if isinstance(exc, UnsupportedMediaTypeError) and exc.supported:
            body["supported"] = exc.supported
        return JSONResponse(status_code=exc.status_code, content=body,
                            headers=_auth_challenge(exc.status_code))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Dependencies raise HTTPException, domain code raises AppError; both must
        # reach the client in one shape so the front end has a single error path.
        headers = dict(exc.headers or {})
        headers.update(_auth_challenge(exc.status_code))
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "correlation_id": request_id_ctx.get()},
            headers=headers)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # This outermost handler runs after the correlation middleware's finally has
        # reset the context var, so recover the id from the request scope and stamp it
        # explicitly onto both the log record and the response. Return only that id so
        # internals never leak.
        cid = getattr(request.state, "correlation_id", None) or request_id_ctx.get()
        log.exception("unhandled error: %s", exc, extra={"request_id": cid})
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error",
                     "correlation_id": cid})
