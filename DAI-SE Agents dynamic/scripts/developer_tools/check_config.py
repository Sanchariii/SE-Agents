"""Report what is configured and reachable, without printing any secret.

    python scripts/developer_tools/check_config.py

Run this first when something is misbehaving: it separates "not configured" from
"configured but unreachable", which the /api/ready probe deliberately does not do.
"""

import os
import sys

os.environ.setdefault("APP_TYPE", "local")
os.environ.setdefault("APP_ENV", "dev")

GREEN, RED, YELLOW, DIM, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[0m"


def line(label: str, state: str, detail: str = "") -> None:
    colour = {"ok": GREEN, "fail": RED, "skip": YELLOW}[state]
    mark = {"ok": "  ok  ", "fail": " fail ", "skip": " skip "}[state]
    print(f"[{colour}{mark}{RESET}] {label:<26} {DIM}{detail}{RESET}")


def probe(label: str, configured: bool, call) -> bool:
    if not configured:
        line(label, "skip", "not configured")
        return True
    try:
        call()
        line(label, "ok")
        return True
    except Exception as exc:                        # noqa: BLE001
        line(label, "fail", f"{type(exc).__name__}: {str(exc)[:90]}")
        return False


def main() -> int:
    from app.config.constants import AppConstants
    from app.config.settings import get_settings

    s = get_settings()
    print(f"\n{AppConstants.APP_NAME} · type={AppConstants.APP_TYPE} "
          f"env={AppConstants.APP_ENV}\n")

    failures = 0

    # --- config presence (never prints a value) ---
    for label, present in (
        ("Azure OpenAI endpoint", bool(s.azure_openai_endpoint)),
        ("Azure OpenAI key", bool(s.azure_openai_api_key)),
        ("Azure Search endpoint", bool(s.azure_search_endpoint)),
        ("Azure Search key", bool(s.azure_search_api_key)),
        ("Blob connection string", bool(s.blob_connection_str)),
        ("Doc Intelligence endpoint", bool(s.azure_document_intelligence_endpoint)),
        ("Video Indexer account", bool(s.video_indexer_account_id
                                       and s.video_indexer_access_token)),
    ):
        line(label, "ok" if present else "skip", "set" if present else "empty")

    print()

    # --- JWT keys ---
    from app.utilities.jwt_util import JwtUtil
    if JwtUtil().configured:
        line("JWT keypair", "ok", "app/config/private + public")
    else:
        line("JWT keypair", "fail", "run scripts/developer_tools/generate_jwt_keys.py")
        failures += 1

    if s.jwt_required:
        line("Auth enforcement", "ok", "JWT_REQUIRED=true")
    else:
        line("Auth enforcement", "fail", "JWT_REQUIRED=false — never do this in production")
        failures += 1

    if "*" in s.cors_origins:
        line("CORS origins", "fail", "wildcard origin with credentials")
        failures += 1
    else:
        line("CORS origins", "ok", ", ".join(s.cors_origins) or "none")

    if not s.tls_verify:
        line("TLS verification", "fail", "disabled — never do this in production")
        failures += 1
    else:
        line("TLS verification", "ok")

    print()

    # --- connectivity ---
    def check_postgres():
        from app.utilities.pgsql_util import PgsqlUtil
        PgsqlUtil().query("SELECT 1 AS ok")

    def check_redis():
        import redis
        redis.from_url(s.redis_url, decode_responses=True).ping()

    def check_search():
        from app.genai.retrieval import indexer
        indexer.ensure_index()

    def check_openai():
        from app.genai.retrieval.embeddings import embed_texts
        embed_texts(["ping"])

    def check_blob():
        from app.utilities.clients_util import get_blob_service
        list(get_blob_service().list_containers(results_per_page=1).by_page())

    failures += not probe("Postgres", True, check_postgres)
    failures += not probe("Redis", True, check_redis)
    failures += not probe("Azure OpenAI", bool(s.azure_openai_endpoint), check_openai)
    failures += not probe("Azure AI Search", bool(s.azure_search_endpoint), check_search)
    failures += not probe("Blob storage",
                          bool(s.blob_connection_str),
                          check_blob)

    print()

    # --- max mode (render-and-review of HIGH-creativity slides) ---
    # Warnings only: without these, HIGH slides simply use the standard tiers.
    from app.genai.ppt import slide_render
    from app.genai.ppt.template import catalog as catalog_mod

    if not s.slide_max_mode_enabled:
        line("Slide renderer (max mode)", "skip", "SLIDE_MAX_MODE_ENABLED=false")
    elif slide_render.find_soffice() is None:
        line("Slide renderer (max mode)", "skip", "LibreOffice not found")
    else:
        result = slide_render.self_test()
        line("Slide renderer (max mode)", "ok" if result["ok"] else "skip",
             f"{result['detail']} in {result['seconds']}s")
        missing = slide_render.missing_fonts()
        line("Render fonts", "skip" if missing else "ok",
             f"missing: {', '.join(missing)}" if missing else "Barlow, Carlito, Liberation")
    problems = catalog_mod.load().high_creativity_problems()
    line("High Creativity master", "skip" if problems else "ok",
         "; ".join(problems)[:200] if problems else "on brand, title layout ready")

    print(f"\n{GREEN}all checks passed{RESET}" if not failures
          else f"\n{RED}{failures} problem(s) found{RESET}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
