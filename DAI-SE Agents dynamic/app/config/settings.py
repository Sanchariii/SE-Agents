"""Environment-driven secrets and per-deployment configuration.

Bootstrap order in ``get_settings()``:
- ``AppConstants.APP_TYPE == "local"``  → load ``app/config/<APP_ENV>.env`` into
  ``os.environ`` and copy each ``UPPER_SNAKE`` env var into the matching
  ``snake_case`` ``Settings`` field.
- ``AppConstants.APP_TYPE == "hosted"`` → read each secret from the Secrets Store
  CSI mount (the ``sea-secrets`` SecretProviderClass) via
  ``app.config.secrets_store``, with an env-var fallback.

Only secrets and per-deployment values live here; everything static belongs in
``app.config.constants``. Nothing in this file may carry a real credential.
"""

import os
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

from app.config.constants import (
    AppConstants, AppType, AzureConstants, JwtConstants, SecretsStoreConstants, TlsConstants)
from app.config.secrets_store import load_mounted_secrets


# --------------------------------------------------------------------------- #
# Settings                                                                     #
# --------------------------------------------------------------------------- #

def _secret() -> str:
    """A sensitive string field: excluded from ``repr`` so the settings object can
    never dump credentials into logs, tracebacks or debugger output.

    Returns:
        A dataclass field defaulting to ``""`` with ``repr=False``.
    """
    return field(default="", repr=False, metadata={"sensitive": True})


@dataclass
class Settings:
    """Secrets and per-deployment configuration. Field names match env var names
    (in ``UPPER_SNAKE``) and Key Vault secret names (in ``kebab-case``).

    Credential fields are declared with ``_secret()``: they are hidden from ``repr``
    and returned by ``secret_values()`` so the log formatter can redact them."""

    log_level: str = "INFO"

    # --- HTTP / CORS ---
    cors_allowed_origins: str = "*"

    # --- TLS ---
    tls_ca_bundle: str = ""
    tls_verify: bool = True

    # --- Postgres (discrete parts; pg_dsn is assembled below) ---
    pg_database: str = ""
    pg_host: str = ""
    pg_password: str = _secret()
    pg_port: int = 5432
    pg_schema: str = ""
    pg_username: str = ""
    pg_sslmode: str = ""

    # --- Redis ---
    redis_uri: str = _secret()                      # hosted/cluster-internal instance
    redis_uri_local: str = _secret()                # used instead when APP_TYPE=local

    # --- Azure OpenAI ---
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = _secret()
    azure_openai_api_version: str = ""
    azure_openai_chat_deployment: str = ""
    azure_openai_embedding_deployment: str = ""
    azure_openai_whisper_deployment: str = ""

    # --- APIM AI Gateway (chat/agent models) ---
    apim_tenant_id: str = ""
    apim_client_id: str = ""
    apim_client_secret: str = _secret()
    apim_subscription_key: str = _secret()
    apim_scope: str = ""
    apim_openai_endpoint: str = ""

    # --- Azure AI Search ---
    acs_name: str = ""                              # endpoint is derived from this when unset
    azure_search_endpoint: str = ""
    azure_search_api_key: str = _secret()
    azure_search_index: str = ""

    # --- Azure Blob Storage ---
    blob_connection_str: str = _secret()
    blob_sas_token: str = _secret()
    blob_download_path: str = ""

    # --- Azure Document Intelligence ---
    azure_document_intelligence_endpoint: str = ""
    azure_document_intelligence_key: str = _secret()
    use_doc_intelligence: bool = True

    # --- Azure Video Indexer ---
    video_indexer_account_id: str = ""
    video_indexer_region: str = ""
    video_indexer_access_token: str = _secret()
    use_video_indexer: bool = False

    # --- Auth (Entra ID single sign-on) ---
    jwt_required: bool = True
    entra_tenant_id: str = ""                       # verifies the id token's issuer
    entra_client_id: str = ""                       # expected id-token audience
    entra_verify_id_token: bool = True              # off only for an isolated test rig

    # --- Transcription ---
    whisper_backend: str = "azure"                  # azure | local (faster-whisper)
    whisper_local_model: str = "base"

    # --- Max mode (render-and-review of HIGH-creativity slides) ---
    slide_max_mode_enabled: bool = True             # kill switch; off = today's ladder
    soffice_path: str = ""                          # LibreOffice binary; "" = autodetect

    # --- Deck engine ---
    deck_engine: str = "classic"                    # classic | skill_deck (DeckEngines)
    # off | on (follows the deck's creativity level) | low | medium | high (fixed level)
    deck_polish: str = "off"

    # ---------------- derived helpers ---------------- #

    def secret_values(self) -> list[str]:
        """Every populated credential value (plus the assembled Postgres DSN), for
        exact-match redaction by the log formatter.

        Returns:
            Non-empty values of all fields declared with ``_secret()``.
        """
        values = [getattr(self, f.name) for f in fields(self) if f.metadata.get("sensitive")]
        if self.pg_password:
            values.append(self.pg_dsn)
        return [v for v in values if isinstance(v, str) and v]

    @property
    def pg_dsn(self) -> str:
        base = (f"postgresql+psycopg2://{self.pg_username}:{self.pg_password}"
                f"@{self.pg_host}:{self.pg_port}/{self.pg_database}")
        params = []
        if self.pg_sslmode:
            params.append(f"sslmode={self.pg_sslmode}")
        if self.pg_schema:
            params.append(f"options={quote_plus(f'-csearch_path={self.pg_schema}')}")
        return f"{base}?{'&'.join(params)}" if params else base

    @property
    def redis_url(self) -> str:
        """Local mode prefers REDIS_URI_LOCAL: the hosted Redis is cluster-internal only."""
        if AppConstants.APP_TYPE == AppType.LOCAL and self.redis_uri_local:
            return self.redis_uri_local
        return self.redis_uri

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def dynamic_skills_enabled(self):
        """Whether session-scoped dynamic skills may be selected."""
        value = os.getenv("DYNAMIC_SKILLS_ENABLED", "true").strip().lower()
        if value not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError("DYNAMIC_SKILLS_ENABLED must be a boolean")
        return value in {"true", "1", "yes", "on"}

    @property
    def skill_reference_hosts(self):
        """Explicit HTTPS host allowlist for website style references."""
        return [host.strip() for host in
                os.getenv("SKILL_REFERENCE_ALLOWED_HOSTS", "").split(",")
                if host.strip()]

    @property
    def id_token_verification_enabled(self) -> bool:
        """True when the id token can actually be verified (tenant + client id known).

        Returns:
            Whether ``graph_util`` should cryptographically validate the id token.
        """
        return bool(self.entra_verify_id_token and self.entra_tenant_id
                    and self.entra_client_id)

    # --- content secrets resolved to a file path (CSI mount hosted, file local) ---

    def _secret_file(self, object_name: str, local_path: str) -> str:
        """Path to a content secret: the CSI-projected file in hosted mode, the given
        local file otherwise.

        Args:
            object_name: CSI/Key Vault object name projected as a file under the mount.
            local_path: File used in local mode.

        Returns:
            Absolute path to read the secret from for the active ``APP_TYPE``.
        """
        if AppConstants.APP_TYPE == AppType.HOSTED:
            return str(Path(SecretsStoreConstants.MOUNT_PATH) / object_name)
        return local_path

    @property
    def jwt_private_key_path(self) -> str:
        """RS256 private-key file: CSI mount in hosted mode, app/config locally."""
        return self._secret_file(SecretsStoreConstants.JWT_PRIVATE_KEY_OBJECT,
                                 JwtConstants.PRIVATE_KEY_PATH)

    @property
    def jwt_public_key_path(self) -> str:
        """RS256 public-key file: CSI mount in hosted mode, app/config locally."""
        return self._secret_file(SecretsStoreConstants.JWT_PUBLIC_KEY_OBJECT,
                                 JwtConstants.PUBLIC_KEY_PATH)

    @property
    def tls_ca_bundle_file(self) -> str:
        """Best-effort CA bundle file used when TLS_CA_BUNDLE is unset: CSI mount in
        hosted mode, the bundled PEM locally. May be absent — callers check."""
        return self._secret_file(SecretsStoreConstants.CA_BUNDLE_OBJECT,
                                 TlsConstants.CA_BUNDLE_PATH)


# --------------------------------------------------------------------------- #
# Field population helpers                                                     #
# --------------------------------------------------------------------------- #

def _coerce(current, raw: str):
    """Coerce a raw env/KV string into the type of the field's current value."""
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError:
            return current
    return raw


def _apply_env_vars(settings: Settings) -> None:
    """Copy each ``UPPER_SNAKE`` env var into the matching ``snake_case`` field."""
    for f in fields(settings):
        raw = os.environ.get(f.name.upper())
        if raw is not None and raw != "":
            setattr(settings, f.name, _coerce(getattr(settings, f.name), raw))


# Fields whose CSI/Key Vault secret name isn't simply ``FIELD_NAME.upper()`` with
# underscores turned into hyphens. First matching name wins.
_SECRET_ALIASES: dict[str, tuple[str, ...]] = {
    "azure_search_api_key": ("ACS-ADMIN-KEY",),
}


def _apply_secrets_store(settings: Settings) -> None:
    """Populate each field from the mounted CSI secrets, falling back to env vars.

    Secret/file names are ``UPPER-HYPHEN`` (``PG-HOST``) while ``Settings`` fields
    are ``snake_case`` (``pg_host``); ``_SECRET_ALIASES`` covers the few names that
    don't follow that mapping. Values still missing fall back to an equally-named
    ``UPPER_SNAKE`` env var so Helm-injected config keeps working.

    Args:
        settings: The ``Settings`` instance to populate in place.

    Returns:
        None. ``settings`` is mutated directly.
    """
    secrets = load_mounted_secrets(SecretsStoreConstants.MOUNT_PATH)
    missing: list[str] = []

    for f in fields(settings):
        # Try the field's own object name first, then any known alias.
        candidates = (f.name.upper().replace("_", "-"), *_SECRET_ALIASES.get(f.name, ()))
        raw = next((secrets[name] for name in candidates if secrets.get(name)), None)
        if not raw:                              # env fallback: helm-injected / local override
            raw = os.environ.get(f.name.upper()) or None
        if raw:
            setattr(settings, f.name, _coerce(getattr(settings, f.name), raw))
        else:
            missing.append(f.name)

    # Runs before logging is configured, so stdout is the only sink. Report field
    # names only - never a value - so nothing sensitive reaches pod logs.
    print(f"settings loaded from secrets store/env; {len(missing)} field(s) not set: "
          f"{', '.join(missing) or 'none'}")


# --------------------------------------------------------------------------- #
# Public entry point                                                           #
# --------------------------------------------------------------------------- #

@lru_cache
def get_settings() -> Settings:
    """Build the singleton ``Settings`` instance from the active secret source."""
    settings = Settings()

    if AppConstants.APP_TYPE == AppType.LOCAL:
        env_file = AppConstants.ENV_FILE
        if not env_file.exists():
            raise RuntimeError(
                f"local mode expected env file at {env_file} "
                f"(APP_ENV='{AppConstants.APP_ENV}'). Copy .env.example and fill it in.")
        load_dotenv(env_file, override=False)
        _apply_env_vars(settings)

    elif AppConstants.APP_TYPE == AppType.HOSTED:
        _apply_secrets_store(settings)

    else:
        raise RuntimeError(
            f"APP_TYPE must be one of {AppType.ALL} (got '{AppConstants.APP_TYPE}')")

    if not settings.azure_search_endpoint and settings.acs_name:
        settings.azure_search_endpoint = AzureConstants.SEARCH_ENDPOINT_TEMPLATE.format(
            name=settings.acs_name)

    return settings
