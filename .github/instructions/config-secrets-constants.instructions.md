---
description: "Use when adding, updating, reading, or organizing secrets, credentials, endpoints, API keys, connection strings, or static constants. Covers app/config/constants.py, settings.py, keyvault.py and the <APP_ENV>.env files, plus the secret-vs-constant decision and the naming mapping between Key Vault, env vars, and Settings fields."
name: "Config: Secrets & Constants"
applyTo: "app/config/**"
---

# Secrets & Constants Storage Rules

This service keeps a hard split between **secrets / per-deployment values** and
**static constants**. Put every new value on the correct side and wire it through
`Settings` — never read raw env vars or hardcode credentials in app code.

## The three layers

| Layer | File | Holds | Never holds |
|-------|------|-------|-------------|
| Constants | `app/config/constants.py` | Static, non-secret values that are identical in every deployment (ports, table names, timeouts, model names, Redis key templates, enums). Organized into classes (`AppConstants`, `PgConstants`, `StorageConstants`, …). | Credentials, endpoints, or anything per-deployment. Only `APP_TYPE` / `APP_ENV` env lookups are allowed here. |
| Env file | `app/config/<APP_ENV>.env` (e.g. `dev.env`) | Secrets and per-deployment values for **local** mode only. | Static constants. |
| Key Vault | Azure Key Vault (hosted mode) | The same secrets as the env file, as one JSON blob (see `keyvault.py`). | Static constants. |
| Settings | `app/config/settings.py` | The typed `Settings(BaseSettings)` model. Every secret/per-deployment value is a field here. This is the **only** way app code reads config. | Real credential literals — every value arrives from the env file or Key Vault. |

## Decide: secret or constant?

- Differs between deployments, or is a credential / endpoint / key / connection
  string → **env file + Key Vault + a `Settings` field**.
- Same everywhere and non-sensitive → **a class in `constants.py`**.

## Adding a new secret / per-deployment value (do all four)

1. **`settings.py`** — add a field to `Settings`. Name is `snake_case`. Use
   `SecretStr` for sensitive values (keys, passwords, connection strings), plain
   `str` / `int` / `bool` for non-sensitive per-deployment values. Give a safe
   default (`""` or `SecretStr("")`).
2. **`<APP_ENV>.env`** — add the matching `UPPER_SNAKE` var under the right
   `# --- Section ---` comment.
3. **Key Vault blob** — add the same value using the `UPPER-HYPHEN` secret name
   (Key Vault names cannot contain underscores; `keyvault.py` normalizes
   `UPPER-HYPHEN` → `UPPER_SNAKE` on load).
4. **Read it** in app code via `get_settings().<field>`; for `SecretStr` call
   `.get_secret_value()`. Raise a clear error when a required value is missing
   (e.g. `"AZURE_SEARCH_ENDPOINT is not configured"`).

## Naming must line up across all three names

The same value has three spellings; keep them consistent:

| Layer | Form | Example |
|-------|------|---------|
| Key Vault secret name | `UPPER-HYPHEN` | `PG-HOST` |
| Env var (`.env`) | `UPPER_SNAKE` | `PG_HOST` |
| `Settings` field | `snake_case` | `pg_host` |

## Critical gotcha — an env var alone does nothing

`Settings` is configured with `extra="ignore"` and `case_sensitive=False`. An env
var (or Key Vault entry) with **no matching `Settings` field is silently ignored**.
So adding e.g. `PG_SCHEMA`, `ACS_NAME`, `BLOB_CONNECTION_STR`, or
`AZURE_DOCUMENT_INTELLIGENCE_KEY` to `dev.env` has zero effect until a
corresponding field exists in `Settings`. If a value must be used, always add the
field in step 1 — don't stop at the env file.

## Derived values are properties, not env vars

Compose values from existing fields as an `@property` on `Settings` (e.g.
`pg_dsn`, `redis_url`, `cors_origins`) instead of adding a new env var.

## When Key Vault uses a different name — use an alias, not a duplicate field

The Key Vault blob sometimes names a secret differently from the `Settings`
field (`ACS-ADMIN-KEY` vs `azure_search_api_key`). Accept both with
`AliasChoices` so one field serves both sources — never create a second field
holding the same secret:

```python
azure_search_api_key: SecretStr = Field(
    SecretStr(""),
    validation_alias=AliasChoices("azure_search_api_key", "acs_admin_key"))
```

## Local overrides for cluster-only infrastructure

When a dependency is only reachable inside the cluster, keep the hosted value and
add a `*_LOCAL` companion, then select between them in a property keyed on
`AppConstants.APP_TYPE` (see `redis_uri` / `redis_uri_local` → `redis_url`). Read
the property everywhere; never read the raw hosted field in app code.

## Enabling a managed Azure service

Gate each managed service behind a `use_*` boolean (`use_doc_intelligence`,
`use_video_indexer`) so it can be switched off per environment, and register the
implementation in the Azure tier of `app/genai/ingestion/registry.py`.

## Security rules

- Never commit real secrets, and never place credential literals in
  `constants.py` or `settings.py`. If a secret is exposed in git history, **rotate
  it** (the `# ROTATE THESE` note in the env file exists for this reason).
- Always type secrets as `SecretStr` so they don't leak through logs or `repr`.
- Leave a value blank in the env file to opt into the documented local fallback
  (e.g. empty `BLOB_CONNECTION_STR` → local `./.local_storage`).
- `get_settings()` is `@lru_cache`d — it reads the environment once per process;
  changing config at runtime requires clearing that cache.
