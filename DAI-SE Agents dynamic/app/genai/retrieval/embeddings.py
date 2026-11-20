"""Batch texts through the APIM embeddings deployment."""

from app.config.constants import ModelConstants, SearchConstants
from app.utilities.apim_llm_utils import get_apim_llm_client
from app.utilities.logger_util import get_logger
log = get_logger(__name__)


def embed_texts(texts: list[str]) -> list[list[float]]:
    try:
        if not texts:
            return []
        
        return get_apim_llm_client().embeddings(
            texts, model=ModelConstants.EMBEDDING,
            batch_size=SearchConstants.EMBEDDING_BATCH_SIZE)
    except Exception as exc:
        log.error("embedding failed: %s", exc)
        return [[0.0] * SearchConstants.EMBEDDING_DIMENSIONS] * len(texts)  # fallback to zero vector