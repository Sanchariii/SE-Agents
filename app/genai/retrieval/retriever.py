"""Hybrid (keyword + vector) search filtered to the session's latest versions."""

import json

from azure.search.documents.models import VectorizedQuery

from app.config.constants import SearchConstants
from app.core.handlers.session_store import session_store
from app.core.models.document_model import RetrievedChunk
from app.genai.guardrails import check_content
from app.genai.retrieval.embeddings import embed_texts
from app.genai.retrieval.filters import odata_str
from app.utilities.clients_util import get_search_client
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def _latest_filter(session_id: str) -> str | None:
    latest = session_store.latest_docs(session_id)
    if not latest:
        return None
    clauses = " or ".join(
        f"(doc_id eq {odata_str(r.doc_id)} and version eq {int(r.version)})" for r in latest)
    return f"session_id eq {odata_str(session_id)} and ({clauses})"


def retrieve(session_id: str, query: str, top_k: int = SearchConstants.DEFAULT_TOP_K,
             kinds: list[str] | None = None) -> list[RetrievedChunk]:

    filter_expr = _latest_filter(session_id)
    if filter_expr is None:
        return []
    if kinds:
        filter_expr += " and (" + " or ".join(f"kind eq {odata_str(k)}" for k in kinds) + ")"

    vector = embed_texts([query])[0]
    results = get_search_client().search(
        search_text=query,
        vector_queries=[VectorizedQuery(vector=vector, k_nearest_neighbors=top_k,
                                        fields="content_vector")],
        filter=filter_expr,
        select=["chunk_id", "doc_id", "filename", "section", "kind", "text", "meta_json"],
        top=top_k)
    chunks = [RetrievedChunk(
        chunk_id=r["chunk_id"], doc_id=r["doc_id"], filename=r.get("filename") or "",
        section=r.get("section") or "", kind=r.get("kind") or "text", text=r["text"],
        score=r.get("@search.score", 0.0),
        meta=json.loads(r["meta_json"]) if r.get("meta_json") else {}) for r in results]
    if chunks:
        check_content(session_id, json.dumps([c.model_dump() for c in chunks]), "sources")
    return chunks