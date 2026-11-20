"""The FastAPI application: lifespan, middleware, probes and routers."""

import asyncio
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.config.constants import AppConstants, HttpConstants, RouteTags
from app.config.exceptions import TlsConfigurationError, register_exception_handlers
from app.config.settings import get_settings
from app.core.services.analytics_service import analytics_router
from app.core.services.approval_service import approval_router
from app.core.services.auth_service import auth_router
from app.core.services.deck_service import deck_router
from app.core.services.skill_service import skill_router
from app.core.services.user_service import user_router
from app.utilities import get_logger, get_redis, setup_logging
from app.utilities import events_util as events, tls_util as tls
from app.utilities.logger_util import request_id_ctx, user_id_ctx

settings = get_settings()
setup_logging(settings.log_level, secrets=settings.secret_values())
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tls.apply_process_trust()
    events.bind_loop(asyncio.get_running_loop())
    app.state.REDIS = get_redis()

    try:
        from app.core.handlers.approval_handler import approval_handler
        from app.core.handlers.chat_message_handler import chat_message_handler
        from app.core.handlers.document_registry_handler import document_registry_handler
        from app.core.handlers.deck_version_handler import deck_version_handler
        from app.core.handlers.group_role_handler import group_role_handler
        from app.core.handlers.session_registry_handler import session_registry_handler
        from app.core.handlers.user_handler import user_handler
        from app.core.handlers.workflow_event_handler import workflow_event_handler
        from app.core.handlers.workflow_run_handler import workflow_run_handler
        user_handler.ensure_table()
        approval_handler.ensure_table()
        session_registry_handler.ensure_table()
        deck_version_handler.ensure_table()
        # Sign-in reads this mapping, so make sure the table exists before traffic.
        group_role_handler.ensure_table()
        chat_message_handler.ensure_table()
        workflow_event_handler.ensure_table()
        # Run registry: the one-active-run-per-session invariant lives in its indexes.
        workflow_run_handler.ensure_table()
        document_registry_handler.ensure_table()
        # Analytics fact tables + pricing dimension; seed prices from the repo JSON.
        from app.utilities.analytics import analytics_store, start_writer
        analytics_store.ensure_tables()
        analytics_store.seed_pricing()
        start_writer()
    except Exception as exc:                        # noqa: BLE001
        log.warning("database not ready: %s", exc)

    # Non-blocking: pre-pays the heavy client/import cold start so the first
    # concurrent uploads run in parallel instead of serialising on first use.
    from app.core.services.warmup import warmup
    asyncio.get_running_loop().run_in_executor(None, warmup)

    # Periodic housekeeping (reaps abandoned direct-to-blob uploads); best-effort.
    from app.core.services import scheduler
    scheduler.start()

    log.info("%s starting", AppConstants.APP_NAME,
             extra={"environment": AppConstants.APP_ENV})
    yield
    # Release this pod's runs first, so a rolling deploy never leaves a session locked
    # until its lease goes stale; their workers are fenced and stop without publishing.
    from app.config.constants import RunErrorCodes
    from app.core.handlers import run_supervisor
    await asyncio.to_thread(run_supervisor.shutdown, RunErrorCodes.WORKER_SHUTDOWN)
    scheduler.shutdown()
    # Drain any buffered analytics rows before the process exits.
    try:
        from app.utilities.analytics import stop_writer
        stop_writer()
    except Exception as exc:                          # noqa: BLE001
        log.warning("analytics writer shutdown failed: %s", exc)
    from app.utilities import graph_util
    await graph_util.close_client()
    log.info("%s shutting down", AppConstants.APP_NAME)


app = FastAPI(title=AppConstants.APP_NAME, version=AppConstants.API_VERSION,
              lifespan=lifespan,
              docs_url=f"{AppConstants.API_PREFIX}/docs")

register_exception_handlers(app)

# Credentialed CORS cannot use a wildcard origin — browsers reject the combination
# and it would expose the API to any site. Origins come from configuration.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=list(HttpConstants.CORS_ALLOW_METHODS),
    allow_headers=list(HttpConstants.CORS_ALLOW_HEADERS),
    expose_headers=list(HttpConstants.CORS_EXPOSE_HEADERS),
)


@app.middleware("http")
async def correlation(request: Request, call_next):
    cid = request.headers.get(HttpConstants.CORRELATION_HEADER, str(uuid.uuid4()))
    # Stash the id on request.state too: this middleware's finally resets the context
    # var before an unhandled error reaches the outermost 500 handler, so that handler
    # recovers the correlation id from the shared request scope instead.
    request.state.correlation_id = cid
    token = request_id_ctx.set(cid)
    user_token = user_id_ctx.set("-")
    try:
        response = await call_next(request)
    finally:
        user_id_ctx.reset(user_token)
        request_id_ctx.reset(token)
    response.headers[HttpConstants.CORRELATION_HEADER] = cid
    return response


ops_router = APIRouter(tags=[RouteTags.OPS])


@ops_router.get("/health")
async def health():
    """Liveness: the process is up. Must stay dependency-free."""
    log.info("health check successful", extra={"status": "ok"})
    return {"status": "ok", "service": AppConstants.APP_NAME,
            "version": AppConstants.API_VERSION}


@ops_router.get("/ready")
async def ready():
    """Readiness: configuration is sane enough to serve traffic."""
    try:
        bundle, tls_ok, tls_err = tls.ca_bundle(), True, None
    except TlsConfigurationError as exc:
        bundle, tls_ok, tls_err = None, False, str(exc)

    from app.utilities.jwt_util import JwtUtil
    jwt_ok = JwtUtil().configured or not settings.jwt_required

    body = {
        "status": "ready" if (tls_ok and jwt_ok) else "degraded",
        "tls": {"verify": settings.tls_verify, "ca_bundle": bundle, "error": tls_err},
        "configured": {
            "jwt_keys": jwt_ok,
            # "azure_openai": bool(settings.azure_openai_endpoint),
            "azure_search": bool(settings.azure_search_endpoint),
            "blob_storage": bool(settings.blob_connection_str),
            "doc_intelligence": bool(settings.azure_document_intelligence_endpoint),
        },
    }
    if body["status"] != "ready":
        log.warning("readiness check failed", extra={"body": body})
        return JSONResponse(status_code=503, content=body)
    log.info("readiness check successful", extra={"body": body})
    return body


# Every route lives under a single global prefix (AppConstants.API_PREFIX),
# so the public surface is localhost:<port>/api/* — probes and docs included.
app.include_router(ops_router, prefix=AppConstants.API_PREFIX)
app.include_router(auth_router, prefix=AppConstants.API_PREFIX)
app.include_router(user_router, prefix=AppConstants.API_PREFIX)
app.include_router(deck_router, prefix=AppConstants.API_PREFIX)
app.include_router(skill_router, prefix=AppConstants.API_PREFIX)
app.include_router(approval_router, prefix=AppConstants.API_PREFIX)
app.include_router(analytics_router, prefix=AppConstants.API_PREFIX)
