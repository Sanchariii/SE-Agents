"""Pre-initialise heavy Azure clients and imports at startup.

The first ingestion otherwise pays a large one-time cost (importing openai /
azure-search / langchain, constructing clients, the first TLS handshake and the
``ensure_index()`` round-trip). That work holds the GIL long enough to starve the
event loop, so a second upload started during the first one's cold start appears
to stall instead of running in parallel.

Warming once at boot means concurrent uploads are parallel from the first request.
Everything here is best-effort: an unconfigured or unreachable dependency is logged
and skipped, never fatal, and readiness is never delayed.
"""

from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def warmup() -> None:
    from app.utilities import clients_util

    for name, factory in (
        ("apim_gateway", lambda: __import__(
            "app.utilities.apim_llm_utils", fromlist=["get_apim_llm_client"]
        ).get_apim_llm_client().get_token()),
        ("azure_openai", clients_util.get_aoai_client),   # still used by Whisper + vision
        ("azure_search", clients_util.get_search_client),
        ("azure_search_index", clients_util.get_search_index_client),
        ("blob_storage", clients_util.get_blob_service),
    ):
        try:
            factory()
        except Exception as exc:                    # noqa: BLE001
            log.info("warmup: %s skipped (%s)", name, exc)

    try:                                            # index existence check + first round-trip
        from app.genai.retrieval import indexer
        if indexer.search_configured():
            indexer.ensure_index()
    except Exception as exc:                        # noqa: BLE001
        log.info("warmup: ensure_index skipped (%s)", exc)

    try:                                            # embeddings TLS path
        from app.genai.retrieval.embeddings import embed_texts
        embed_texts(["warmup"])
    except Exception as exc:                        # noqa: BLE001
        log.info("warmup: embeddings skipped (%s)", exc)

    try:                                            # compile the workflow graph once
        from app.genai.workflows.deck_workflow import get_graph
        get_graph()
    except Exception as exc:                        # noqa: BLE001
        log.info("warmup: workflow graph skipped (%s)", exc)

    log.info("warmup complete")
