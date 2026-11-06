"""Manual, step-by-step test of the ingestion pipeline against one real file.

Run from repo root: python -m scripts.manual_testing.test_indexing_work
Comment out steps at the bottom to isolate just the one you're debugging.
"""

import mimetypes

from app.core.models.document_model import DocumentRecord
from app.genai.ingestion.chunking import build_ckm
from app.genai.ingestion.registry import get_extractor
from app.genai.ingestion.summarizer import summarize
from app.genai.retrieval import indexer

FILE_PATH = "C:\\projects\\SE-Agents-project\\dev\\deployment\\DAI-SE%20Agents\\app\\data\\sample_docs\\MobileDemo.MP4"   # <- point at your real doc
# FILE_PATH = "C:\\projects\\SE-Agents-project\\dev\\deployment\\DAI-SE%20Agents\\app\\data\\sample_docs\\ROK AI Gateway Engineering Guide 7 1.pdf"
SESSION_ID = "manual-test-session"

def make_record(path: str) -> DocumentRecord:
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type is None:
        raise ValueError(f"Could not guess MIME type for {path}")
    return DocumentRecord(
        session_id=SESSION_ID,
        doc_id="manual-doc-001",
        version=1,
        filename=path.split("\\")[-1],
        mime_type=mime_type,
        sha256="test",
        storage_path=path,
    )

def step1_extract(record: DocumentRecord, data: bytes):
    extractor = get_extractor(record.mime_type)
    print(f"Using extractor: {extractor.__class__.__name__}")
    units = extractor.extract(data, record)
    print(f"Extracted {len(units)} unit(s)")
    for u in units:
        # print(f"  [{u.kind}] section={u.section!r} page={u.page} text[:80]={u.text[:80]!r}")
        print(f" complete unit details :\n {u.model_dump_json(indent=2)} \n -------------------")
    return units


def step2_build_ckm(record: DocumentRecord, units):
    ckm = build_ckm(record, units)
    print(f"Built CkM with {len(ckm.chunks)} chunk(s)\n")
    print(f"All chunks : {ckm.model_dump_json(indent=2)} \n \n")
    for c in ckm.chunks[:3]:
        print(f"  {c.chunk_id} ({c.kind}, page={c.page}): {c.text[:80]!r}")
    return ckm

def step3_summarize(ckm):
    summary = summarize(ckm)
    print(f"Summary: {summary}")
    ckm.summary = summary
    return ckm

def step4_index(ckm):
    if not indexer.search_configured():
        print("AI Search not configured — skipping indexing")
        return
    count = indexer.index_ckm(SESSION_ID, ckm)
    print(f"Indexed {count} chunk(s)")

def cleanup():
    """Remove this session's chunks from Search and its Redis state."""
    indexer.purge_session(SESSION_ID)
    # session_store.delete_session(SESSION_ID)
    print(f"Purged Search chunks and Redis state for session {SESSION_ID!r}")


if __name__ == "__main__":
    record = make_record(FILE_PATH)
    print("--------------------------printing the record-------------\n")
    print(f"Record: {record.model_dump_json(indent=2)}\n")
    print("-----------------------------------------------------------\n")

    with open(FILE_PATH, "rb") as f:
        raw = f.read()
    print("--------------------------STEP1 processing---------------------------------\n")
    units = step1_extract(record, raw)
    print("--------------------------STEP1 done---------------------------------\n")

    print("--------------------------STEP2 processing---------------------------------\n")
    ckm = step2_build_ckm(record, units)
    print("--------------------------STEP2 done---------------------------------\n")

    print("--------------------------STEP3 processing---------------------------------\n")
    ckm = step3_summarize(ckm)
    print("--------------------------STEP3 done---------------------------------\n")
    #
    print("--------------------------STEP4 processing---------------------------------\n")
    step4_index(ckm)
    print("--------------------------STEP4 done---------------------------------\n")

    print(f'----------------CLEAN UP --------------')
    cleanup()