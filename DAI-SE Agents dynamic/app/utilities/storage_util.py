"""Blob when configured, local filesystem otherwise."""

from app.config.constants import StorageConstants
from app.config.settings import get_settings
from app.utilities.logger_util import get_logger

log = get_logger(__name__)
LOCAL_ROOT = StorageConstants.LOCAL_ROOT


def _use_blob() -> bool:
    return bool(get_settings().blob_connection_str)


def _account_name_key() -> tuple[str | None, str | None]:
    """AccountName/AccountKey from the connection string; (None, None) for a SAS-URL
    connection, which has no key to sign fresh SAS tokens with."""
    conn = get_settings().blob_connection_str
    if not conn or conn.startswith(("http://", "https://")):
        return None, None
    parts = dict(p.split("=", 1) for p in conn.split(";") if "=" in p)
    return parts.get("AccountName"), parts.get("AccountKey")


def _ensure_container(container: str) -> None:
    """Create the container on first use; a no-op if it already exists."""
    from azure.core.exceptions import ResourceExistsError
    from app.utilities.clients_util import get_blob_service
    try:
        get_blob_service().create_container(container)
    except ResourceExistsError:
        pass


def upload_blob_path(session_id: str, doc_id: str, version: int, filename: str) -> str:
    """Canonical blob key for an uploaded source file — the single formula shared by
    the server-side save path and the client-side presigned upload path."""
    return f"{session_id}/{doc_id}/v{version}/{filename}"


def save_upload(session_id: str, doc_id: str, version: int, filename: str, data: bytes) -> str:
    return save_bytes(upload_blob_path(session_id, doc_id, version, filename), data)


def save_bytes(rel_path: str, data: bytes, container: str | None = None) -> str:
    if _use_blob():
        from app.utilities.clients_util import get_blob_service
        container = container or StorageConstants.CONTAINER_UPLOADS
        log.info("saving %s bytes to blob '%s' in container '%s'", len(data), rel_path, container)
        _ensure_container(container)               # create the container on first use
        svc = get_blob_service()
        svc.get_blob_client(container=container, blob=rel_path).upload_blob(
            data, overwrite=True)
        log.info("saved %s bytes to blob '%s' in container '%s'", len(data), rel_path, container)
        return rel_path
    target = LOCAL_ROOT / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return rel_path


def read_file(storage_path: str, container: str | None = None) -> bytes:
    """Return a stored file's bytes: streamed from Blob, or read from local disk.

    The Blob path downloads in bounded chunks and logs progress, so a large file
    (e.g. a video) shows movement in the logs instead of appearing to hang, and a
    stalled socket surfaces as an error (bounded read timeout) rather than silence.

    Args:
        storage_path: blob key / relative path of the file.
        container: source container (defaults to the uploads container).

    Returns:
        The full file contents as bytes.
    """
    if not _use_blob():
        log.info("reading '%s' from local storage", storage_path)
        return (LOCAL_ROOT / storage_path).read_bytes()

    from app.utilities.clients_util import get_blob_service
    container = container or StorageConstants.CONTAINER_UPLOADS
    blob = get_blob_service().get_blob_client(container=container, blob=storage_path)
    log.info("downloading blob '%s' from container '%s'…", storage_path, container)
    downloader = blob.download_blob()
    total = downloader.size or 0
    log.info("blob '%s' is %.1f MB; streaming in %s MB chunk(s)",
             storage_path, total / (1024 * 1024),
             StorageConstants.DOWNLOAD_CHUNK_BYTES // (1024 * 1024))

    buffer = bytearray()
    # Stream chunk-by-chunk so progress is visible and a stalled read can't hang silently.
    for chunk in downloader.chunks():
        buffer.extend(chunk)
        pct = (len(buffer) / total * 100) if total else 100.0
        log.info("downloading '%s': %.1f / %.1f MB (%.0f%%)", storage_path,
                 len(buffer) / (1024 * 1024), total / (1024 * 1024), pct)
    log.info("download complete for blob '%s' (%s bytes)", storage_path, len(buffer))
    return bytes(buffer)


def read_sas_url(storage_path: str, container: str | None = None,
                 expiry_minutes: int = 60) -> str | None:
    """A short-lived, read-only SAS URL for a blob, or None when Blob storage isn't
    configured (local fallback) or the connection has no account key to sign with."""
    if not _use_blob():
        return None
    account_name, account_key = _account_name_key()
    if not (account_name and account_key):
        return None
    from datetime import datetime, timedelta, timezone
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas
    from app.utilities.clients_util import get_blob_service
    container = container or StorageConstants.CONTAINER_UPLOADS
    blob_client = get_blob_service().get_blob_client(container=container, blob=storage_path)
    sas = generate_blob_sas(
        account_name=account_name, container_name=container, blob_name=storage_path,
        account_key=account_key, permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes))
    return f"{blob_client.url}?{sas}"


def upload_sas_url(storage_path: str, container: str | None = None,
                   expiry_minutes: int | None = None) -> str | None:
    """A short-lived write SAS URL the client PUTs bytes to, uploading straight into
    Blob and bypassing the API (so a large file never hits the ingress body-size
    limit that causes HTTP 413).

    Args:
        storage_path: blob key to grant write access to.
        container: target container (defaults to the uploads container).
        expiry_minutes: URL lifetime; defaults to StorageConstants.UPLOAD_SAS_TTL_MINUTES.

    Returns:
        The full ``https://…?<sas>`` URL, or None when Blob isn't configured (local
        fallback) or the connection string has no account key to sign with.
    """
    if not _use_blob():
        return None
    account_name, account_key = _account_name_key()
    if not (account_name and account_key):
        return None
    from datetime import datetime, timedelta, timezone
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas
    from app.utilities.clients_util import get_blob_service
    container = container or StorageConstants.CONTAINER_UPLOADS
    minutes = expiry_minutes or StorageConstants.UPLOAD_SAS_TTL_MINUTES
    _ensure_container(container)                   # so the client's PUT can't 404 on a missing container
    blob_client = get_blob_service().get_blob_client(container=container, blob=storage_path)
    # create + write let the client PUT a new block blob (single shot or chunked).
    sas = generate_blob_sas(
        account_name=account_name, container_name=container, blob_name=storage_path,
        account_key=account_key,
        permission=BlobSasPermissions(create=True, write=True),
        expiry=datetime.now(timezone.utc) + timedelta(minutes=minutes))
    log.info("issued %s-min upload SAS for blob '%s' in container '%s'",
             minutes, storage_path, container)
    return f"{blob_client.url}?{sas}"


def blob_size(storage_path: str, container: str | None = None) -> int | None:
    """Size in bytes of a stored object, or None if it does not exist. Used to
    confirm a client's direct-to-blob upload actually landed before marking it
    uploaded (an unfinished/abandoned upload leaves no blob, so this returns None)."""
    if _use_blob():
        from azure.core.exceptions import ResourceNotFoundError
        from app.utilities.clients_util import get_blob_service
        container = container or StorageConstants.CONTAINER_UPLOADS
        try:
            props = get_blob_service().get_blob_client(
                container=container, blob=storage_path).get_blob_properties()
            return props.size
        except ResourceNotFoundError:
            return None
    path = LOCAL_ROOT / storage_path
    return path.stat().st_size if path.exists() else None


def delete_file(storage_path: str, container: str | None = None) -> None:
    """Idempotent: no error if the blob/file is already gone."""
    if _use_blob():
        from azure.core.exceptions import ResourceNotFoundError
        from app.utilities.clients_util import get_blob_service
        container = container or StorageConstants.CONTAINER_UPLOADS
        try:
            get_blob_service().get_blob_client(container=container, blob=storage_path).delete_blob()
        except ResourceNotFoundError:
            pass
        return
    path = LOCAL_ROOT / storage_path
    if path.exists():
        path.unlink()


def delete_prefix(prefix: str, container: str | None = None) -> int:
    """Delete every blob/local file under `prefix` (e.g. a session folder); returns count removed."""
    if _use_blob():
        from app.utilities.clients_util import get_blob_service
        container = container or StorageConstants.CONTAINER_UPLOADS
        client = get_blob_service().get_container_client(container)
        names = [b.name for b in client.list_blobs(name_starts_with=prefix)]
        for name in names:
            client.delete_blob(name)
        return len(names)
    import shutil
    root = LOCAL_ROOT / prefix
    if not root.exists():
        return 0
    count = sum(1 for p in root.rglob("*") if p.is_file())
    shutil.rmtree(root)
    return count