"""Version-aware AI Search indexer; purges superseded versions on upload."""

import json
from concurrent.futures import as_completed

from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration, SearchableField, SearchField,
    SearchFieldDataType, SearchIndex, SimpleField, VectorSearch, VectorSearchProfile,
)

from app.config.constants import SearchConstants
from app.config.settings import get_settings
from app.core.models.document_model import CkM
from app.genai.retrieval.embeddings import embed_texts
from app.genai.retrieval.filters import odata_str
from app.utilities.logger_util import (ContextThreadPoolExecutor, get_logger)

log = get_logger(__name__)


def search_configured() -> bool:
    setting = get_settings()
    # Log only whether each value is present — never the endpoint value or the API key.
    log.info("search_configured: endpoint=%s, api_key=%s",
             bool(setting.azure_search_endpoint), bool(setting.azure_search_api_key))
    return bool(setting.azure_search_endpoint and setting.azure_search_api_key)


def _index_client():
    from app.utilities.clients_util import get_search_index_client
    return get_search_index_client()


def build_index_definition(name: str) -> SearchIndex:
    return SearchIndex(
        name=name,
        fields=[
            SimpleField(name="chunk_id", type=SearchFieldDataType.String, key=True),
            SimpleField(name="session_id", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="doc_id", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="version", type=SearchFieldDataType.Int32, filterable=True),
            SimpleField(name="filename", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="kind", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="page", type=SearchFieldDataType.Int32, filterable=True),
            SearchableField(name="section", type=SearchFieldDataType.String),
            SearchableField(name="text", type=SearchFieldDataType.String),
            SimpleField(name="meta_json", type=SearchFieldDataType.String),
            SearchField(name="content_vector",
                        type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                        searchable=True,
                        vector_search_dimensions=SearchConstants.EMBEDDING_DIMENSIONS,
                        vector_search_profile_name=SearchConstants.VECTOR_PROFILE),
        ],
        vector_search=VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name=SearchConstants.VECTOR_ALGORITHM)],
            profiles=[VectorSearchProfile(
                name=SearchConstants.VECTOR_PROFILE,
                algorithm_configuration_name=SearchConstants.VECTOR_ALGORITHM)]),
    )


def ensure_index() -> None:
    setting = get_settings()
    client = _index_client()
    if setting.azure_search_index not in set(client.list_index_names()):
        client.create_index(build_index_definition(setting.azure_search_index))
        log.info("created search index", extra={"index": setting.azure_search_index})


def _to_doc(session_id: str, filename: str, chunk, vector: list[float]) -> dict:
    return {"chunk_id": chunk.chunk_id, "session_id": session_id, "doc_id": chunk.doc_id,
            "version": chunk.version, "filename": filename, "kind": chunk.kind,
            "page": chunk.page, "section": chunk.section, "text": chunk.text,
            "meta_json": json.dumps(chunk.meta) if chunk.meta else "",
            "content_vector": vector}


def _delete_where(odata_filter: str) -> None:
    from app.utilities.clients_util import get_search_client
    client = get_search_client()
    stale = [{"chunk_id": r["chunk_id"]}
             for r in client.search(search_text="*", select=["chunk_id"], filter=odata_filter)]
    if stale:
        client.delete_documents(stale)


def purge_old_versions(session_id: str, doc_id: str, keep_version: int) -> None:
    _delete_where(f"session_id eq {odata_str(session_id)} and doc_id eq {odata_str(doc_id)} "
                  f"and version lt {int(keep_version)}")


def purge_document(session_id: str, doc_id: str) -> None:
    """Delete ALL versions of one document's chunks (used when a source is deleted)."""
    _delete_where(f"session_id eq {odata_str(session_id)} and doc_id eq {odata_str(doc_id)}")


def purge_session(session_id: str) -> None:
    """Delete every chunk belonging to a session (used on full session delete)."""
    _delete_where(f"session_id eq {odata_str(session_id)}")


def _upload_batch(client, batch: list[dict]) -> int:
    failed = [r.key for r in client.merge_or_upload_documents(batch) if not r.succeeded]
    if failed:
        raise RuntimeError(f"failed to index {len(failed)} chunks: {failed[:5]}")
    return len(batch)


def index_ckm(session_id: str, ckm: CkM) -> int:
    from app.utilities.clients_util import get_search_client
    if not ckm.chunks:
        return 0
    ensure_index()
    vectors = embed_texts([f"{c.section}\n{c.text}" if c.section else c.text for c in ckm.chunks])
    docs = [_to_doc(session_id, ckm.filename, c, v) for c, v in zip(ckm.chunks, vectors)]
    client = get_search_client()
    batch_size = SearchConstants.UPLOAD_BATCH_SIZE
    batches = [docs[i:i + batch_size] for i in range(0, len(docs), batch_size)]
    if len(batches) == 1:
        _upload_batch(client, batches[0])
    else:
        with ContextThreadPoolExecutor(
                max_workers=min(SearchConstants.UPLOAD_MAX_WORKERS, len(batches))) as pool:
            for fut in as_completed([pool.submit(_upload_batch, client, b) for b in batches]):
                fut.result()
    purge_old_versions(session_id, ckm.doc_id, ckm.version)
    return len(docs)