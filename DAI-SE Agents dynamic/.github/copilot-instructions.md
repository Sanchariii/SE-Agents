# SE-Agent Backend — Copilot Instructions

FastAPI service that turns uploaded source documents into AI-generated PowerPoint
decks (ingestion → retrieval/RAG → multi-agent generation → PPTX). Python 3.11+,
async FastAPI, Pydantic, SQLAlchemy/Postgres, Redis, Azure OpenAI / AI Search /
Blob. Follow the conventions below for every change.

---

## 1. Project structure & layering

```
app/
  main.py              # entrypoint: bootstrap Settings, then run uvicorn
  config/              # constants.py, settings.py, keyvault.py, exceptions.py, <env>.env, JWT keys
  core/
    dependencies/      # FastAPI DI (auth, role & session-ownership guards)
    services/          # APIRouters = the HTTP surface + server.py (app assembly) + warmup.py
    handlers/          # domain logic & persistence (Postgres/Redis/session stores)
    models/            # Pydantic schemas (requests, responses, internal data)
  genai/
    agents/            # planner, researcher, writer, critic, triage
    ingestion/         # extract → chunk → summarize → index pipeline (+ azure/local extractors)
    retrieval/         # embeddings, indexer, retriever, filters (Azure AI Search)
    ppt/               # builder, renderer, theme (python-pptx)
    workflows/         # agent orchestration
  utilities/           # infra wrappers: clients, pgsql, redis, storage, jwt, llm, tls, events, logger, security
helm/                  # Chart, Dockerfile(.local), values, k8s templates
scripts/               # developer_tools/ + manual_testing/
tests/                 # pytest
```

**Dependency direction (never invert):**
`services (routers)` → `handlers (domain)` → `utilities (infra)` / `genai`, with
`config` + `models` as the shared base. Routers stay thin: validate input, enforce
auth, delegate to a handler, shape the response. Business logic lives in handlers,
never in route functions. Domain code raises domain errors and never imports
FastAPI.

- All routes are served under the global prefix `AppConstants.API_PREFIX` (`/api`).
- One router per domain: `<domain>_router = APIRouter(prefix="/x", tags=[RouteTags.X])`.
- Handlers are module-level singletons: `user_handler = UserHandler()` at file bottom.

---

## 2. Naming conventions

| Thing | Convention | Example |
|-------|-----------|---------|
| Files / modules | `snake_case` | `deck_version_handler.py` |
| Classes | `PascalCase` | `UserHandler`, `PgsqlUtil` |
| Functions / vars | `snake_case` | `list_versions`, `session_id` |
| Constant classes | `PascalCase` holding `UPPER_SNAKE` | `AppConstants.SERVICE_PORT` |
| Private module helpers | leading `_` | `_issue()`, `_to_record()`, `_engine()` |
| Routers | `<domain>_router` | `deck_router` |
| Settings field / env var / Key Vault | `snake_case` / `UPPER_SNAKE` / `UPPER-HYPHEN` | `pg_host` / `PG_HOST` / `PG-HOST` |

Use modern type hints everywhere (`str | None`, `list[str]`, `dict`). Annotate all
function parameters and return types.

---

## 3. Constants

`app/config/constants.py` is the **single source of truth for static, non-secret
values** (ports, table names, timeouts, batch sizes, model names, Redis key
templates, enums, MIME maps). Group them into a `PascalCase` class
(`AppConstants`, `PgConstants`, `StorageConstants`, `JwtConstants`, `RedisKeys`,
`SearchConstants`, `LlmConstants`, …). Never hardcode a magic number or string in
app code — add or reuse a constant. Never put a credential, endpoint, or
per-deployment value here (only `APP_TYPE` / `APP_ENV` env lookups are allowed).

---

## 4. Secrets & configuration

**Read config only through `get_settings().<field>`. Never read raw `os.environ`
or hardcode credentials in app code.** Three layers: `constants.py` (static) ·
`<APP_ENV>.env` (local secrets) · Azure Key Vault (hosted secrets) → all surfaced
as typed fields on `Settings`. Type sensitive values so they never leak in logs,
give safe defaults, and expose composed values as `@property` (e.g. `pg_dsn`,
`redis_url`, `cors_origins`).

> Adding or changing any secret/endpoint/connection string is governed by
> [.github/instructions/config-secrets-constants.instructions.md](instructions/config-secrets-constants.instructions.md).
> Read and follow it before touching `app/config/**`.

---

## 5. Logging — log every key step

Get a module logger at the top of **every** file: `log = get_logger(__name__)`.
Logs are the primary way we debug this service, so instrument generously.

- **`log.info` on every key step** — the checkpoints needed to reconstruct a
  request from logs alone: entering a significant operation, external calls
  (Postgres / Redis / Azure OpenAI / Search / Blob), state transitions
  (e.g. document status changes), background-task start/finish, and successful
  completion of meaningful work.
- **`log.warning`** for recoverable / degraded conditions the app handles but a
  human should notice (missing optional config, retryable failures, fallbacks,
  "AI Search not configured; skipping indexing").
- **`log.error`** (or `log.exception` / `exc_info=True` inside an `except`) for
  failures that abort an operation — always include enough context (ids,
  filenames, storage paths) to locate the cause.
- Use lazy `%s` formatting (`log.info("seeded admin '%s'", name)`), not f-strings,
  and attach structured context via `extra={...}`.
- Every log line already carries the request/correlation id via the
  `request_id` `ContextVar` filter — preserve that mechanism; don't log secrets or
  raw tokens.

---

## 6. Comments & docstrings

- **Module docstring** at the top of every file: one line on what it does (add a
  short "why" when the design isn't obvious).
- **Function docstrings**: give every function a docstring describing its purpose,
  its **Args** and its **Return value** (and anything it raises), so developers and
  agents can understand and safely modify it without reading the whole call chain.
- **Meaningful step comments**: at each key step, add a short comment explaining
  *what is being done and why* (the intent/reasoning the code can't show on its
  own), so bugs and enhancements are easy to locate — e.g.
  `# subscribe first, so no event is missed` or
  `# single-use: rotate on every refresh`. Don't restate what the next line
  obviously does.

---

## 7. Exception handling & fault tolerance

Build every feature to stay up when a dependency misbehaves.

- Raise the domain errors from `app/config/exceptions.py` (`AppError` [400],
  `NotFoundError` [404], `ConflictError` [409], `UnsupportedMediaTypeError` [415],
  `UpstreamError` [502]) instead of returning ad-hoc error bodies. Handlers
  registered by `register_exception_handlers` convert them to JSON with a
  `correlation_id`.
- The catch-all handler logs the stack trace and returns a generic 500 — **never
  leak internals, stack traces, or secrets to clients.**
- Wrap best-effort / background work (warmup, ingestion, metadata writes) in
  `try/except`, log with full context, and let the rest of the request succeed.
  Add `# noqa: BLE001` when a broad `except Exception` is intentional.
- Validate at boundaries (request bodies via Pydantic, auth/ownership via
  dependencies). Don't add defensive checks for states that can't occur.

---

## 8. Performance & concurrency — **keep every API as fast as possible (highlight)**

**Minimum response time is a hard requirement.** Never block the event loop and
never make a client wait for work that can run elsewhere.

- **All routes are `async def`.** Any blocking/synchronous work — Postgres, Blob,
  CPU-bound parsing, synchronous Azure SDK calls — **must** be offloaded with
  `await asyncio.to_thread(...)` so the event loop stays free.
- **Fan out independent work in parallel**, don't serialize it: use
  `asyncio.gather(*(asyncio.to_thread(fn, x) for x in items))` or a
  `ThreadPoolExecutor` (see `pipeline.ingest_documents`, `indexer.index_ckm`) for
  batches. Bound workers with a constant.
- **Return fast, finish in the background**: hand slow work (document
  ingestion/extraction) to FastAPI `BackgroundTasks` and stream progress over SSE
  rather than making the caller wait.
- **Reuse expensive resources**: one pooled SQLAlchemy engine per process
  (`@lru_cache`d, `pool_pre_ping`, sized via `PgConstants`); cache Azure/LLM
  clients and `get_settings()` with `@lru_cache`. Don't construct clients or pools
  per request.
- Pre-warm heavy clients at startup off the event loop (`run_in_executor(warmup)`)
  so first requests don't pay cold-start cost.
- When you add an endpoint, ask: is anything here blocking? can independent calls
  run concurrently? is a per-request object better cached? Prefer the fastest
  correct option.

---

## 9. Other coding standards

- **Pydantic models** for all request/response schemas and structured internal
  data (`app/core/models`); return `model.model_dump()` from routes.
- **Dependency injection for security**: guard routes with `require_auth`,
  `require_role(...)`, `require_session_access`, `require_session_role(...)`.
  Enforce RBAC (`Roles`) *and* per-session ownership — session ids are guessable,
  so every per-session route must prove ownership (return 404, not 403, to avoid
  leaking existence).
- **`@lru_cache` for singletons** and expensive one-time construction (settings,
  engine, validators, clients).
- **Lazy imports inside functions** for heavy or optional dependencies (SQLAlchemy,
  Azure SDKs, LangChain, python-pptx) and to break circular imports — follow the
  existing pattern.
- **Parameterized SQL only** (`text(sql)` with bound `:params`) — never
  f-string user input into a query.
- Keep functions small and single-purpose; factor shared logic into a `_helper`
  or a utility rather than duplicating it.

---

## 10. Testing

Tests live in `tests/` (pytest, `pytest-asyncio`). Mirror the existing style:
async tests for async code, monkeypatch external dependencies, assert both success
and failure/permission paths. Run with `pytest`. Add or update tests when you
change behavior.

---

## Golden rules

1. Log every key step (`info`), plus `warning`/`error` where appropriate.
2. Docstring every function (purpose, args, returns) and comment key steps.
3. Never block the event loop — offload with `asyncio.to_thread` / thread pools;
   keep responses fast.
4. Config through `Settings`; static values in `constants.py`; no hardcoded
   secrets.
5. Raise domain errors; handle failures so the app stays fault-tolerant and never
   leaks internals.

## graphify

For any question about this repo's architecture, structure, components, or how to add/modify/find
code, your first action should be `graphify query "<question>"` when `graphify-out/graph.json`
exists. Use `graphify path "<A>" "<B>"` for relationship questions and `graphify explain "<concept>"`
for focused-concept questions. These return a scoped subgraph, usually much smaller than the full
report or raw grep output.

Triggers: "how do I…", "where is…", "what does … do", "add/modify a <component>",
"explain the architecture", or anything that depends on how files or classes relate.

If `graphify-out/wiki/index.md` exists, use it for broad navigation. Read `graphify-out/GRAPH_REPORT.md`
only for broad architecture review or when query/path/explain do not surface enough context. Only read
source files when (a) modifying/debugging specific code, (b) the graph lacks the needed detail, or
(c) the graph is missing or stale.

Type `/graphify` in Copilot Chat to build or update the graph.
