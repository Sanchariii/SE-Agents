"""Single place that decides TLS trust (corporate CA bundles / proxies)."""

import os
import ssl
import tempfile
from functools import lru_cache
from pathlib import Path

from app.config.constants import (
    AppConstants,
    AppType,
    HttpConstants,
    SecretsStoreConstants,
    TlsConstants,
)
from app.config.exceptions import TlsConfigurationError
from app.config.settings import get_settings
from app.utilities.logger_util import get_logger

log = get_logger(__name__)


def _bundle_has_certs(path: Path) -> bool:
    """Whether OpenSSL can load at least one certificate from ``path``.

    An existing-but-empty CA bundle (e.g. an unpopulated secrets-store mount) is the
    dangerous case: exporting it via SSL_CERT_FILE/REQUESTS_CA_BUNDLE replaces the
    whole system trust store with nothing, so every TLS handshake then fails with
    "X509: NO_CERTIFICATE_OR_CRL_FOUND". We must reject such a bundle up front.

    Logs the file size and the number of certificates found so an empty vs a
    malformed vs a valid bundle is distinguishable from logs alone.

    Args:
        path: CA bundle PEM file, or a capath directory of hashed certs.

    Returns:
        True if the bundle yields one or more X.509 certificates, else False.
    """
    is_dir = path.is_dir()
    try:                                            # -1 = directory / stat unavailable
        size = -1 if is_dir else path.stat().st_size
    except OSError:
        size = -1
    if size == 0:
        log.warning("CA bundle %s is EMPTY (0 bytes); no certificate to load", path)
        return False
    try:
        # Start from an empty store so cert_store_stats reflects only this bundle;
        # this loads the file exactly as the SDKs' TLS stacks do, so it catches the
        # same empty/malformed content that would otherwise fail at handshake time.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if is_dir:
            ctx.load_verify_locations(capath=str(path))
        else:
            ctx.load_verify_locations(cafile=str(path))
        count = ctx.cert_store_stats().get("x509", 0)
        if count > 0:
            log.info("CA bundle %s is valid: %d certificate(s) loaded (size=%s bytes)",
                     path, count, size)
            return True
        log.warning("CA bundle %s loaded but contains NO certificate (size=%s bytes)",
                    path, size)
        return False
    except (ssl.SSLError, OSError) as exc:          # unreadable or no PEM certificate
        log.warning("CA bundle %s could not be loaded (size=%s bytes): %s", path, size, exc)
        return False


def _combined_bundle(corporate_pem: str) -> str:
    """Merge the public root CAs (certifi) with the corporate CA into one bundle.

    Pointing SSL_CERT_FILE / REQUESTS_CA_BUNDLE at a corporate-only PEM *replaces* the
    system trust store, so public endpoints (login.microsoftonline.com, *.blob, AOAI)
    then fail with 'unable to get local issuer certificate'. Concatenating certifi's
    public roots with the corporate PEM trusts both. Best-effort: on any failure the
    corporate PEM is returned unchanged.

    Args:
        corporate_pem: path to the resolved corporate CA PEM (or CSI mount).

    Returns:
        Path to the combined bundle, or ``corporate_pem`` if the merge can't be done.
    """
    try:
        import certifi
        public = Path(certifi.where()).read_text(encoding="utf-8")
    except Exception as exc:                        # noqa: BLE001 - certifi missing/unreadable
        log.warning("TLS: public roots (certifi) unavailable (%s); using the corporate "
                    "CA alone — public endpoints may fail to verify", exc)
        return corporate_pem
    try:
        corporate = Path(corporate_pem).read_text(encoding="utf-8")
        out = Path(tempfile.gettempdir()) / TlsConstants.COMBINED_BUNDLE_NAME
        out.write_text(f"{public}\n{corporate}\n", encoding="utf-8")
    except OSError as exc:
        log.warning("TLS: could not write combined CA bundle (%s); using the corporate "
                    "CA alone", exc)
        return corporate_pem
    log.info("TLS: merged public roots (certifi) + %s into %s", corporate_pem, out)
    return str(out)


def _resolve_corporate_bundle() -> str | None:
    """Locate the corporate CA PEM to trust (before merging in the public roots).

    Resolution order — best-effort, so the default sources never abort startup:
      1. an explicit TLS_CA_BUNDLE / CA-bundle env var (a configured path that does
         not exist is a misconfiguration and raises);
      2. the Key Vault CSI secrets-store mount (hosted);
      3. the corporate PEM shipped in app/config (TlsConstants.CA_BUNDLE_PATH);
      4. otherwise None.

    A bundle that exists but yields no certificate (e.g. an empty CSI mount) is skipped
    so it neither shadows a later valid source nor replaces the trust store with nothing.
    Emits an INFO/WARNING trail (source, presence, size, cert count).

    Returns:
        Path to a usable corporate CA PEM, or None when none is found.

    Raises:
        TlsConfigurationError: an explicitly configured bundle path does not exist.
    """
    s = get_settings()
    hosted = AppConstants.APP_TYPE == AppType.HOSTED
    log.info("TLS: resolving CA trust (app_type=%s, tls_verify=%s)",
             AppConstants.APP_TYPE, s.tls_verify)

    # 1) An explicit TLS_CA_BUNDLE / env var wins; a configured-but-missing path is fatal.
    candidate = None
    if s.tls_ca_bundle:
        candidate = s.tls_ca_bundle
        log.info("TLS: TLS_CA_BUNDLE is explicitly set: %s", candidate)
    else:
        for var in TlsConstants.BUNDLE_ENV_VARS:
            val = os.environ.get(var)
            if val:
                candidate = val
                log.info("TLS: CA bundle taken from environment variable %s=%s", var, val)
                break
    if candidate:
        path = Path(candidate).expanduser()
        if not path.exists():
            log.error("TLS: configured CA bundle path does NOT exist: %s", path)
            raise TlsConfigurationError(f"TLS_CA_BUNDLE path does not exist: {path}")
        if _bundle_has_certs(path):
            log.info("TLS: using configured CA bundle: %s", path)
            return str(path)
        log.warning("TLS: configured CA bundle %s has no certificate; falling back to "
                    "the default sources", path)

    # 2+3) Best-effort defaults: the CSI Key Vault mount, then the shipped app/config PEM.
    csi_label = (f"Key Vault secret '{SecretsStoreConstants.CA_BUNDLE_OBJECT}' via the CSI "
                 f"secrets-store mount ({SecretsStoreConstants.MOUNT_PATH})"
                 if hosted else "bundled app/config PEM")
    sources: list[tuple[str, str]] = [(csi_label, s.tls_ca_bundle_file)]
    if TlsConstants.CA_BUNDLE_PATH != s.tls_ca_bundle_file:      # add the shipped PEM (hosted)
        sources.append(("bundled corporate PEM in app/config", TlsConstants.CA_BUNDLE_PATH))
    log.info("TLS: no explicit bundle; trying default sources in order: %s",
             ", ".join(f"{label} [{p}]" for label, p in sources))
    for source, raw in sources:
        path = Path(raw)
        if not path.exists():
            log.warning("TLS: CA bundle ABSENT at %s (%s); trying next source", path, source)
            continue
        if _bundle_has_certs(path):
            log.info("TLS: using CA bundle from %s: %s", source, path)
            return str(path)
        log.warning("TLS: CA bundle at %s (%s) is present but has no certificate; "
                    "trying next source", path, source)

    # 4) Nothing usable.
    log.warning("TLS: no usable corporate CA bundle found; using the system trust store")
    return None


@lru_cache
def ca_bundle() -> str | None:
    """Return a CA bundle path trusting BOTH the public roots and the corporate CA.

    Resolves the corporate CA (explicit TLS_CA_BUNDLE / env var / CSI Key Vault mount /
    shipped app/config PEM) then merges it with certifi's public roots into a single
    file, so public Azure/Microsoft endpoints AND the corporate proxy both verify.
    Returns None only when no corporate CA is found (the system trust store is then used
    unchanged). Cached, so resolution and the merge happen once per process.

    Returns:
        Path to the combined CA bundle, or None to use the system trust store.

    Raises:
        TlsConfigurationError: an explicitly configured bundle path does not exist.
    """
    raw = _resolve_corporate_bundle()
    if not raw:
        return None
    return _combined_bundle(raw)


def verify_setting() -> bool | str:
    if not get_settings().tls_verify:
        return False
    return ca_bundle() or True


def apply_process_trust() -> None:
    """Export the resolved CA bundle into the env vars child libraries read.

    Best-effort: an unavailable bundle falls back to the system trust store rather than
    blocking startup. Logs the final decision and every env var it sets or leaves
    untouched, so the process-wide TLS trust is visible in the startup logs.
    """
    if not get_settings().tls_verify:
        # TLS_VERIFY=false is a deliberate opt-out (corporate TLS-inspection proxy);
        # silence urllib3's per-request InsecureRequestWarning so it doesn't flood logs.
        log.warning("TLS: verification is DISABLED (TLS_VERIFY=false) — expected for local "
                    "dev only, never in production")
        import warnings
        try:
            from urllib3.exceptions import InsecureRequestWarning
            warnings.filterwarnings("ignore", category=InsecureRequestWarning)
        except ImportError:
            pass
        return
    bundle = ca_bundle()
    if not bundle:
        log.info("TLS: no custom CA bundle applied; child libraries will use the system "
                 "trust store")
        return
    for var in TlsConstants.BUNDLE_ENV_VARS:
        existing = os.environ.get(var)
        if existing:                                # keep any operator-provided value
            log.info("TLS: %s already set to %s; leaving it unchanged", var, existing)
        else:
            os.environ[var] = bundle
            log.info("TLS: exported %s=%s for child libraries", var, bundle)
    log.info("TLS: custom CA bundle in use: %s", bundle, extra={"ca_bundle": bundle})


def ssl_context() -> ssl.SSLContext:
    bundle = ca_bundle()
    if bundle and Path(bundle).is_dir():
        return ssl.create_default_context(capath=bundle)
    return ssl.create_default_context(cafile=bundle)


@lru_cache
def http_client():
    """Shared sync httpx client for the Azure SDKs (pooling + keep-alive)."""
    import httpx
    verify = verify_setting()
    if verify is not False:
        log.info("TLS verification is ENABLED.")
        verify = ssl_context() if ca_bundle() else True
    else:
        log.warning("TLS verification is DISABLED — never in production")
    return httpx.Client(
        verify=verify,
        timeout=httpx.Timeout(HttpConstants.TIMEOUT_SECONDS,
                              connect=HttpConstants.CONNECT_TIMEOUT_SECONDS),
        limits=httpx.Limits(max_connections=HttpConstants.MAX_CONNECTIONS,
                            max_keepalive_connections=HttpConstants.MAX_KEEPALIVE_CONNECTIONS),
    )


def reset() -> None:
    ca_bundle.cache_clear()
    http_client.cache_clear()