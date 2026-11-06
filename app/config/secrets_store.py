"""Hosted-mode secret source: the Kubernetes Secrets Store CSI driver.

In hosted mode the pod mounts a ``SecretProviderClass`` (``sea-secrets``) volume at
``/mnt/secrets-store``. The Azure provider projects every Key Vault secret named in
that class as a file at the mount root: the file name is the secret's ``objectName``
(``UPPER-HYPHEN``, e.g. ``PG-HOST``) and the file body is the secret value. This
module reads those files into a plain ``dict`` so ``Settings`` can be populated
without the Azure SDK.

Two on-disk layouts are supported so the loader keeps working however the volume is
provisioned:
- one file per secret (the standard CSI layout described above), and
- a single file whose body is a JSON object of ``{"NAME": "value"}`` pairs.
"""

import json
import logging
import os

# Config bootstrap must not import app.utilities (pgsql_util -> settings would be a
# circular import); the stdlib logger still routes through the configured root handler.
log = logging.getLogger(__name__)


def _normalize(name: str) -> str:
    """Return the canonical ``UPPER-HYPHEN`` form of a secret name.

    Args:
        name: A secret/file/JSON-key name in any casing (``pg_host``, ``PG-HOST``).

    Returns:
        The name upper-cased with underscores turned into hyphens (``PG-HOST``).
    """
    return name.strip().upper().replace("_", "-")


def _parse_json_object(body: str) -> dict | None:
    """Return the parsed mapping if ``body`` is a JSON object, else ``None``.

    Individual secret values (hosts, ports, keys, connection strings) never start
    with ``{``, so this only matches a genuine ``{name: value}`` blob and never
    misreads a single-secret file.

    Args:
        body: Raw file contents.

    Returns:
        The decoded ``dict`` when ``body`` is a JSON object, otherwise ``None``.
    """
    text = body.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def load_mounted_secrets(mount_path: str) -> dict[str, str]:
    """Read every secret projected by the CSI driver at ``mount_path``.

    Args:
        mount_path: Directory where the SecretProviderClass volume is mounted
            (e.g. ``/mnt/secrets-store``).

    Returns:
        Mapping of ``UPPER-HYPHEN`` secret name -> secret value. Empty when the
        path is absent or unreadable, so callers can still fall back to env vars.
    """
    # Missing mount is non-fatal: hosted pods can still boot on env-var fallback.
    if not os.path.isdir(mount_path):
        log.warning("secrets store mount not found at %s; relying on env vars", mount_path)
        return {}

    try:
        entries = os.listdir(mount_path)
    except OSError:
        log.exception("failed to list secrets store mount %s", mount_path)
        return {}

    secrets: dict[str, str] = {}
    for name in entries:
        # Skip the CSI/projected-volume bookkeeping entries (..data, ..2026_...).
        if name.startswith("."):
            continue
        full = os.path.join(mount_path, name)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            log.exception("failed to read mounted secret file %s", full)
            continue

        # A JSON-object file carries many secrets at once; anything else is a
        # single secret keyed by its file name.
        blob = _parse_json_object(body)
        if blob is not None:
            for key, value in blob.items():
                secrets[_normalize(key)] = "" if value is None else str(value)
        else:
            secrets[_normalize(name)] = body.strip()

    log.info("loaded %d secret(s) from %s", len(secrets), mount_path)
    return secrets
