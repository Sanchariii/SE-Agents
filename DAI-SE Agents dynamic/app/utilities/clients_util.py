"""Lazy Azure SDK singletons, all wired to the configured TLS trust.

Every client is cached: constructing them is expensive (TLS handshake, token
discovery) and they are thread-safe and designed to be long-lived.
"""

from functools import lru_cache

from app.config.constants import AzureConstants
from app.config.settings import get_settings
from app.utilities import tls_util as tls
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


@lru_cache
def get_aoai_client():
    from openai import AzureOpenAI
    s = get_settings()
    if not s.azure_openai_endpoint:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT is not configured")
    return AzureOpenAI(
        azure_endpoint=s.azure_openai_endpoint,
        api_key=s.azure_openai_api_key,
        api_version=s.azure_openai_api_version,
        http_client=tls.http_client(),
        max_retries=AzureConstants.MAX_RETRIES,
    )


@lru_cache
def get_search_client():
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents import SearchClient
    s = get_settings()
    if not s.azure_search_endpoint:
        raise RuntimeError("AZURE_SEARCH_ENDPOINT is not configured")
    return SearchClient(
        endpoint=s.azure_search_endpoint, index_name=s.azure_search_index,
        credential=AzureKeyCredential(s.azure_search_api_key),
        connection_verify=tls.verify_setting(), retry_total=AzureConstants.MAX_RETRIES)


@lru_cache
def get_search_index_client():
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents.indexes import SearchIndexClient
    s = get_settings()
    if not s.azure_search_endpoint:
        raise RuntimeError("AZURE_SEARCH_ENDPOINT is not configured")
    return SearchIndexClient(
        endpoint=s.azure_search_endpoint,
        credential=AzureKeyCredential(s.azure_search_api_key),
        connection_verify=tls.verify_setting(), retry_total=AzureConstants.MAX_RETRIES)


@lru_cache
def get_blob_service():
    from azure.storage.blob import BlobServiceClient
    from app.config.constants import StorageConstants
    s = get_settings()
    conn = s.blob_connection_str
    if not conn:
        raise RuntimeError("BLOB_CONNECTION_STR is not configured")
    # Bound the per-chunk download size + socket waits so large-file (video) downloads
    # stream in progress-reportable chunks and a stalled socket fails loudly, not silently.
    tuning = dict(
        connection_verify=tls.verify_setting(),
        retry_total=AzureConstants.MAX_RETRIES,
        max_single_get_size=StorageConstants.DOWNLOAD_CHUNK_BYTES,
        max_chunk_get_size=StorageConstants.DOWNLOAD_CHUNK_BYTES,
        connection_timeout=AzureConstants.BLOB_CONNECT_TIMEOUT_SECONDS,
        read_timeout=AzureConstants.BLOB_READ_TIMEOUT_SECONDS,
    )
    # Accept either an account connection string or an account SAS URL.
    if conn.startswith(("http://", "https://")):
        return BlobServiceClient(account_url=conn, **tuning)
    return BlobServiceClient.from_connection_string(conn, **tuning)

@lru_cache
def get_doc_intelligence_client():
    from azure.core.credentials import AzureKeyCredential
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    s = get_settings()
    if not s.azure_document_intelligence_endpoint:
        raise RuntimeError("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT is not configured")
    return DocumentIntelligenceClient(
        endpoint=s.azure_document_intelligence_endpoint,
        credential=AzureKeyCredential(s.azure_document_intelligence_key),
        connection_verify=tls.verify_setting())


def reset() -> None:
    for factory in (get_aoai_client, get_search_client, get_search_index_client,
                    get_blob_service, get_doc_intelligence_client):
        factory.cache_clear()
    tls.reset()