"""Manual, step-by-step test of retrieval against a document already indexed
by test_indexing_work.py. Run from repo root:
    python -m scripts.manual_testing.test_retrieval_work
Comment out steps at the bottom to isolate just the one you're debugging.
"""

import mimetypes

from app.core.handlers.session_store import session_store
from app.core.models.document_model import DocumentRecord
from app.genai.ingestion.chunking import build_ckm
from app.genai.ingestion.registry import get_extractor
from app.genai.retrieval import indexer
from app.genai.retrieval.embeddings import embed_texts
from app.genai.retrieval.filters import odata_str
from app.genai.retrieval.retriever import retrieve

FILE_PATH = "C:\\projects\\SE-Agents-project\\dev\\deployment\\DAI-SE%20Agents\\app\\data\\sample_docs\\01_smt_ppm_kpivs.txt"
SESSION_ID = "manual-retrieval-session"
QUERY = "what is the target PPM for SMT?"   # <- change to something your doc can answer


def setup_indexed_doc() -> DocumentRecord:
    """Register + extract + index one real file so retrieve() has something to find."""
    with open(FILE_PATH, "rb") as f:
        raw = f.read()
    mime_type, _ = mimetypes.guess_type(FILE_PATH)
    filename = FILE_PATH.split("\\")[-1]

    record = session_store.register_upload(SESSION_ID, filename, mime_type, "test-sha", FILE_PATH)
    units = get_extractor(record.mime_type).extract(raw, record)
    ckm = build_ckm(record, units)
    session_store.save_ckm(ckm)
    indexer.index_ckm(SESSION_ID, ckm)
    record.status = "indexed"
    session_store.update_doc(record)
    print(f"Indexed {len(ckm.chunks)} chunk(s) for doc_id={record.doc_id} v{record.version}")
    return record


def step1_embed_query(query: str):
    vectors = embed_texts([query])
    print(f"Embedded query into vector of length {len(vectors[0])}")
    return vectors[0]


def step2_build_filter():
    docs = session_store.latest_docs(SESSION_ID)
    print(f"Latest docs in session: {[(d.doc_id, d.version) for d in docs]}")
    clauses = " or ".join(
        f"(doc_id eq {odata_str(d.doc_id)} and version eq {d.version})" for d in docs)
    filter_expr = f"session_id eq {odata_str(SESSION_ID)} and ({clauses})"
    print(f"OData filter: {filter_expr}")
    return filter_expr


def step3_retrieve(query: str, top_k: int = 5):
    chunks = retrieve(SESSION_ID, query, top_k=top_k)
    print(f"STEP3 : Retrieved {len(chunks)} chunk(s) for query={query!r}")
    for c in chunks:
        print(f" {c.model_dump_json(indent=2)}")
        print(f"  score={c.score:.4f} [{c.kind}] {c.filename} section={c.section!r}: "
              f"{c.text[:100]!r}")
    return chunks


def step4_retrieve_filtered_by_kind(query: str, kinds: list[str]):
    chunks = retrieve(SESSION_ID, query, kinds=kinds)
    print(f" STEP4 : Retrieved {len(chunks)} chunk(s) filtered to kinds={kinds}")
    for c in chunks:
        print(f"  score={c.score:.4f} [{c.kind}] {c.text[:100]!r}")
    return chunks


def cleanup():
    """Remove this session's chunks from Search and its Redis state."""
    indexer.purge_session(SESSION_ID)
    session_store.delete_session(SESSION_ID)
    print(f"Purged Search chunks and Redis state for session {SESSION_ID!r}")


if __name__ == "__main__":
    setup_indexed_doc()

    step1_embed_query(QUERY)
    step2_build_filter()
    step3_retrieve(QUERY)
    step4_retrieve_filtered_by_kind(QUERY, kinds=["text"])

    cleanup()