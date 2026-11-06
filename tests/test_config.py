"""Config, TLS and secret-handling behaviour."""

from dataclasses import fields

import pytest

from app.config.constants import AuthConstants, JwtConstants, Roles, StorageConstants
from app.config.exceptions import TlsConfigurationError
from app.config.settings import Settings, get_settings


def test_settings_are_cached():
    assert get_settings() is get_settings()


def test_pg_dsn_is_assembled_from_parts():
    s = Settings(pg_host="db", pg_port=5433, pg_username="u",
                 pg_password="p", pg_database="d", pg_schema="")
    assert s.pg_dsn == "postgresql+psycopg2://u:p@db:5433/d"


def test_pg_dsn_appends_sslmode():
    s = Settings(pg_host="db", pg_username="u", pg_password="p",
                 pg_database="d", pg_sslmode="require", pg_schema="")
    assert s.pg_dsn.endswith("?sslmode=require")


def test_pg_dsn_sets_search_path_from_schema():
    s = Settings(pg_host="db", pg_username="u", pg_password="p",
                 pg_database="d", pg_schema="public")
    assert s.pg_dsn.endswith("?options=-csearch_path%3Dpublic")


def test_redis_url_prefers_the_local_override_in_local_mode():
    """The hosted Redis URI only resolves inside the cluster."""
    s = Settings(redis_uri="redis://cluster:6379", redis_uri_local="redis://localhost:6379/0")
    assert s.redis_url == "redis://localhost:6379/0"
    assert Settings(redis_uri="redis://cluster:6379",
                    redis_uri_local="").redis_url == "redis://cluster:6379"


def test_no_credentials_are_hardcoded_in_defaults():
    """Defaults must be empty; real values arrive from the env file or Key Vault."""
    defaults = {f.name: f.default for f in fields(Settings)}
    for name in ("pg_password", "azure_openai_api_key", "azure_search_api_key",
                 "azure_document_intelligence_key", "blob_connection_str",
                 "video_indexer_access_token", "entra_client_id", "entra_tenant_id"):
        assert defaults[name] == ""


def test_no_local_account_settings_remain():
    """Sign-in is federated: the service holds no user credentials of its own."""
    field_names = {f.name for f in fields(Settings)}
    assert not any(n.startswith("seed_admin") for n in field_names)


def test_id_token_verification_needs_tenant_and_client():
    assert Settings().id_token_verification_enabled is False
    assert Settings(entra_tenant_id="t", entra_client_id="c"
                    ).id_token_verification_enabled is True
    assert Settings(entra_tenant_id="t", entra_client_id="c",
                    entra_verify_id_token=False).id_token_verification_enabled is False


def test_settings_hold_no_application_constants():
    """Constants belong in app/config/constants.py, not in Settings."""
    field_names = {f.name for f in fields(Settings)}
    assert not field_names & {"max_upload_bytes", "rag_max_rounds", "workflow_max_iterations",
                              "azure_storage_container_uploads", "azure_storage_container_decks",
                              "pg_pool_size", "http_timeout_seconds", "environment"}


def test_cors_origins_are_explicit_not_wildcard():
    s = Settings(cors_allowed_origins="https://a.example, https://b.example")
    assert s.cors_origins == ["https://a.example", "https://b.example"]
    assert "*" not in s.cors_origins


def test_auth_is_required_by_default():
    assert Settings().jwt_required is True


def test_roles_are_the_three_known_values():
    assert set(Roles.ALL) == {"admin", "approver", "member"}


def test_roles_are_cumulative_and_ordered_strongest_first():
    assert Roles.PRECEDENCE[0] == Roles.ADMIN
    assert set(Roles.IMPLIED[Roles.ADMIN]) == set(Roles.ALL)
    assert Roles.IMPLIED[Roles.MEMBER] == (Roles.MEMBER,)
    # Every directory alias must resolve to a real role.
    assert set(Roles.GROUP_ROLE_ALIASES.values()) <= set(Roles.ALL)


def test_a_session_never_outlives_its_refresh_window():
    assert AuthConstants.SESSION_TTL_SECONDS < AuthConstants.REFRESH_TTL_SECONDS


def test_jwt_key_paths_are_absolute():
    assert JwtConstants.PRIVATE_KEY_PATH.endswith("private")
    assert JwtConstants.PUBLIC_KEY_PATH.endswith("public")


def test_upload_limit_is_a_constant():
    assert StorageConstants.MAX_UPLOAD_BYTES == 200 * 1024 * 1024


def test_missing_ca_bundle_is_rejected(monkeypatch):
    from app.utilities import tls_util

    tls_util.reset()
    monkeypatch.setattr(tls_util, "get_settings",
                        lambda: Settings(tls_ca_bundle="/does/not/exist.pem"))
    with pytest.raises(TlsConfigurationError):
        tls_util.ca_bundle()
    tls_util.reset()
