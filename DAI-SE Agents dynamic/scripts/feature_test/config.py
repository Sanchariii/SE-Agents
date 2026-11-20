"""Loads credentials for all services from the local `azure-creds` JSON file.

`azure-creds` holds real secrets — keep it out of version control (see .gitignore).
"""
import json
import os
import tempfile
from pathlib import Path

import certifi

_CREDS_PATH = Path(__file__).parent / "azure-creds"
_CERTS_DIR = Path(__file__).parent / "certs"


def _load_creds() -> dict:
    if not _CREDS_PATH.exists():
        return {}
    with open(_CREDS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


_creds = _load_creds()
_ca_bundle_path: str | None = None


def get(key: str, default: str = "") -> str:
    """Fetch a credential value by its key in the azure-creds file."""
    return _creds.get(key) or default


def reload() -> None:
    """Re-read azure-creds from disk (e.g. after editing values in the UI)."""
    global _creds
    _creds = _load_creds()


def ca_bundle() -> str:
    """Path to a CA bundle combining certifi's defaults with any corporate certs.

    Picks up `.crt` and `.pem` files from `certs/`, plus a `TLS_CA_BUNDLE` env var
    pointing to a PEM file or a directory of `.crt` or `.pem` files (e.g. a
    Zscaler/corporate proxy CA).
    Falls back to certifi's bundle if no custom certs are found.
    """
    global _ca_bundle_path
    if _ca_bundle_path:
        return _ca_bundle_path

    custom_pems: list[str] = []
    if _CERTS_DIR.is_dir():
        for pattern in ("*.crt", "*.pem"):
            for cert in sorted(_CERTS_DIR.glob(pattern)):
                custom_pems.append(cert.read_text(encoding="utf-8"))

    env_bundle = os.environ.get("TLS_CA_BUNDLE")
    if env_bundle:
        env_path = Path(env_bundle)
        if env_path.is_file():
            custom_pems.append(env_path.read_text(encoding="utf-8"))
        elif env_path.is_dir():
            for pattern in ("*.crt", "*.pem"):
                for cert in sorted(env_path.glob(pattern)):
                    custom_pems.append(cert.read_text(encoding="utf-8"))

    if not custom_pems:
        _ca_bundle_path = certifi.where()
        return _ca_bundle_path

    combined = Path(certifi.where()).read_text(encoding="utf-8") + "\n" + "\n".join(custom_pems)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False, encoding="utf-8") as tmp:
        tmp.write(combined)
        _ca_bundle_path = tmp.name
    return _ca_bundle_path
