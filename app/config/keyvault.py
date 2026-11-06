"""
Azure Key Vault client for fetching secrets at application startup.
Uses DefaultAzureCredential which supports:
- Workload Identity (in Kubernetes with service account)
- Azure CLI (local development)
- Managed Identity (VM/Container Apps)

Only secret *names* and load outcomes are ever logged - never secret values.
"""
import logging
import os
from typing import Optional
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

# stdlib logger (not app.utilities) so config stays free of utility-layer imports.
log = logging.getLogger(__name__)


class KeyVaultClient:
    """Fetch secrets from Azure Key Vault with fallback to environment variables."""

    def __init__(self):
        """Initialise the Key Vault client from ``AZURE_KEYVAULT_URL`` when set.

        Falls back to env-var-only mode if the URL is unset or the client fails to build.
        """
        self.vault_url = os.getenv("AZURE_KEYVAULT_URL")
        self.enabled = self.vault_url is not None
        self.client: Optional[SecretClient] = None

        if self.enabled:
            try:
                credential = DefaultAzureCredential()
                self.client = SecretClient(vault_url=self.vault_url, credential=credential)
                log.info("Key Vault client initialized")
            except Exception as e:  # noqa: BLE001
                # Log only the exception type: SDK messages can echo request details.
                log.warning("Failed to initialize Key Vault client (%s); "
                            "falling back to environment variables", type(e).__name__)
                self.enabled = False

    def get_secret(self, secret_name: str, env_var_name: str) -> str:
        """
        Get secret from Key Vault or fall back to environment variable.

        Args:
            secret_name: Name of secret in Key Vault
            env_var_name: Name of environment variable to use as fallback

        Returns:
            Secret value or empty string if not found
        """
        if self.enabled and self.client:
            try:
                secret = self.client.get_secret(secret_name)
                log.info("Loaded %s from Key Vault", secret_name)
                return secret.value
            except Exception as e:  # noqa: BLE001
                log.warning("Failed to fetch %s from Key Vault (%s)",
                            secret_name, type(e).__name__)

        value = os.getenv(env_var_name, "")
        if value:
            log.info("Loaded %s from environment", env_var_name)
        else:
            log.warning("Secret %s / %s not found", secret_name, env_var_name)
        return value

_kv_client: Optional[KeyVaultClient] = None


def get_keyvault_client() -> KeyVaultClient:
    """Get or create the global Key Vault client instance."""
    global _kv_client
    if _kv_client is None:
        _kv_client = KeyVaultClient()
    return _kv_client