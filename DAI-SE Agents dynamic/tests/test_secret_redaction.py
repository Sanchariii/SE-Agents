"""Secrets must never reach logs: redaction formatter and Settings repr hygiene."""

import logging

import pytest

from app.config.settings import Settings
from app.utilities import logger_util
from app.utilities.logger_util import RedactingFormatter, redact, register_secrets


@pytest.fixture(autouse=True)
def _reset_known_secrets(monkeypatch):
    """Isolate the module-level registry of known secret values per test."""
    monkeypatch.setattr(logger_util, "_known_secrets", ())


@pytest.mark.parametrize("text, leaked", [
    ("headers={'Authorization': 'Bearer abc.def-ghi_jkl'}", "abc.def-ghi_jkl"),
    ("Ocp-Apim-Subscription-Key: 0123456789abcdef", "0123456789abcdef"),
    ("{'api-key': 'k3y-value-123'}", "k3y-value-123"),
    ('client_secret="s3cr3t~value"', "s3cr3t~value"),
    ("DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=AbC+/xyz==;EndpointSuffix=x",
     "AbC+/xyz=="),
    ("https://acct.blob.core.windows.net/c/b?sv=2022&sig=Sg%2Fabc123", "Sg%2Fabc123"),
    ("postgresql+psycopg2://admin:P@ssw0rd@db.host:5432/app", "ssw0rd"),
    ("redis://:redispw@redis:6379/0", "redispw"),
    ("token eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.c2lnbmF0dXJl", "eyJzdWIiOiIxMjM0NSJ9"),
])
def test_redact_masks_credential_shapes(text: str, leaked: str) -> None:
    """Common credential shapes are masked even when not registered."""
    assert leaked not in redact(text)


def test_redact_masks_registered_secret_values() -> None:
    """A registered value is masked wherever it appears, in any shape."""
    register_secrets(["plain-secret-value", "", "ab"])
    out = redact("apim settings: plain-secret-value / ab")
    assert "plain-secret-value" not in out
    assert "ab" in out                          # too short to register; left alone


def test_redact_leaves_ordinary_text_untouched() -> None:
    """Normal diagnostic lines survive redaction unchanged."""
    line = "slide 3 tier 1 attempt 1/3; max_tokens=800, 12-token palette"
    assert redact(line) == line


def test_formatter_redacts_args_and_traceback() -> None:
    """Redaction applies to %-args and to the rendered exception text."""
    register_secrets(["super-secret-key"])
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError("failed with key super-secret-key")
    except RuntimeError:
        import sys
        exc_info = sys.exc_info()
    record = logging.LogRecord("t", logging.ERROR, __file__, 1,
                               "calling with %s", ("super-secret-key",), exc_info)
    assert "super-secret-key" not in formatter.format(record)


def test_settings_repr_hides_secrets() -> None:
    """repr(Settings) never includes credential values; secret_values() exposes them."""
    s = Settings(apim_client_secret="cs-value", apim_subscription_key="sub-value",
                 azure_search_api_key="search-value", blob_connection_str="conn-value",
                 pg_password="pg-value", pg_username="u", pg_host="h")
    text = repr(s)
    for value in ("cs-value", "sub-value", "search-value", "conn-value", "pg-value"):
        assert value not in text
        assert value in s.secret_values()
    assert s.pg_dsn in s.secret_values()
