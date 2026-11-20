"""Best-effort document summary (enrichment; never fails ingestion)."""

from app.config.constants import IngestionConstants, ModelConstants
from app.core.models.document_model import CkM
from app.genai.guardrails import TRUST_BOUNDARY
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def summarize(ckm: CkM) -> str | None:
    try:
        body = "\n\n".join(c.text for c in ckm.chunks)[:IngestionConstants.MAX_SUMMARY_INPUT_CHARS]
        if not body.strip():
            return None
        return get_apim_llm_client().chat_text(
            system="You summarise source documents for a training-deck builder. "
                   "Write 3-5 sentences capturing the key topics and takeaways.\n\n"
                   + TRUST_BOUNDARY,
            messages=[{"role": "user", "content": body}],
            max_tokens=IngestionConstants.SUMMARY_MAX_TOKENS,
            model=ModelConstants.SUMMARIZER)
    except Exception as exc:                       
        log.warning("summarise failed: %s", exc)
        return None