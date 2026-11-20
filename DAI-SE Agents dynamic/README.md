# SE Agent Service

One FastAPI service that authenticates users, ingests source material, and builds
grounded PowerPoint training decks with a GenAI workflow.

It replaces the previous `agents/` (port 8001) and `backend/` (port 8002) services.
The public API is unchanged, but `backend` no longer proxies over HTTP to `agents` —
both halves run in this process, so a deck request no longer pays an extra network
hop and JSON round-trip.

---

## Layout

```
app/
├── main.py                  entry point
├── config/                  constants, settings, key vault, exceptions, env files, JWT keys
├── core/
│   ├── dependencies/        FastAPI dependencies (auth, roles, session ownership)
│   ├── handlers/            business logic and persistence
│   ├── models/              pydantic request/response/domain models
│   └── services/            routers + server.py (app, lifespan, middleware, probes)
├── genai/
│   ├── agents/              triage, planner, ppt (content), ppt creator (design), work order
│   ├── workflows/           the LangGraph that sequences them
│   ├── ingestion/           extract → chunk → summarise → index
│   │   ├── extractors/      deterministic parsers (pdf, docx, pptx, xlsx, csv, text)
│   │   ├── azure/           managed services (Document Intelligence)
│   │   └── local/           run in-process for now — see local/README.md
│   ├── retrieval/           embeddings, index management, hybrid search
│   └── ppt/                 draft → .pptx rendering
├── data/                    sample documents
└── utilities/               logging, Postgres, Redis, Blob, TLS, JWT, LLM clients

helm/                        Dockerfile + Kubernetes chart
scripts/manual_testing/      live end-to-end scripts
scripts/developer_tools/     key generation, configuration diagnostics
tests/                       fast unit tests (no infrastructure needed)
```

---

## Running locally

Two ways to run the service: **Docker Compose** (recommended — it bundles Redis and
the native `ffmpeg`/OpenCV that video ingestion needs) or a **native** virtualenv.
Do the one-time setup first; both paths need it:

```bash
cp app/config/.env.example app/config/dev.env        # then fill in your values
python scripts/developer_tools/generate_jwt_keys.py  # needs the `cryptography` package
```

### Docker Compose (recommended)

Needs Docker Desktop. One command builds the image and starts the service next to a
Redis container; Postgres and the Azure services stay remote and are reached through
`app/config/dev.env`.

```bash
docker compose up --build          # build image + start app and redis
docker compose logs -f app         # follow app logs
docker compose restart app         # pick up code / dev.env changes
docker compose down                # stop everything
```

The API is then at http://localhost:8002/api/docs. Compose sets `APP_TYPE=local` and
points the app at the bundled Redis (`REDIS_URI_LOCAL=redis://redis:6379/0`), so no
env changes are needed. It builds from `helm/Dockerfile.local` — the same as
`helm/Dockerfile` but with a pip `trusted-host` setting for corporate TLS-inspection
proxies. That image is for local use only; never ship it.

### Native (virtualenv)

Install `ffmpeg` on your PATH first if you need video ingestion — other sources work
without it.

```bash
python -m venv .venv && .venv/Scripts/activate     # source .venv/bin/activate on Unix
pip install -r requirements-dev.txt

docker run --name my-redis -p 6379:6379 -d redis   # keep REDIS_URI_LOCAL=redis://localhost:6379/0

python scripts/developer_tools/check_config.py     # verifies what is reachable
python -m app.main                                 # http://localhost:8002/api/docs
```

Postgres tables are created on startup. Sign-in is federated through Entra ID:
there are no local accounts, users are provisioned on their first successful
sign-in, and roles come from the `group_id_role_mapping` table. Redis and Blob
storage both fall back to local in-memory / on-disk equivalents when unconfigured,
so the service starts with nothing but Postgres running.

```bash
pytest                                             # unit tests
python scripts/manual_testing/00_smoke.py          # against a running service
```

---

## API

> All endpoints are served under the **`/api`** prefix, so the paths in the table
> below are relative to it — e.g. `GET /health` is reachable at `GET /api/health`,
> `POST /auth/login` at `POST /api/auth/login`, and the interactive docs live at
> `/api/docs` (OpenAPI schema at `/api/openapi.json`).

| Area      | Endpoints                                                                                          |
| --------- | -------------------------------------------------------------------------------------------------- |
| Ops       | `GET /health` · `GET /ready`                                                                        |
| Auth      | `POST /auth/login` (SSO) · `/auth/refresh` · `/auth/logout` · `GET /auth/validate` · `GET /auth/me`  |
| Users     | `GET /users` · `DELETE /users/{id}/sessions`                                                          |
| Decks     | `GET,POST /decks/sessions` · `DELETE /decks/{id}`                                                    |
|           | `GET,POST /decks/{id}/documents` · `DELETE /decks/{id}/documents/{doc_id}`                           |
|           | `POST /decks/{id}/invoke` (SSE) · `GET /decks/{id}/stream` (SSE) · `GET /decks/{id}/messages`        |
|           | `GET /decks/{id}/versions` · `.../{v}/download` · `DELETE .../{v}`                                   |
|           | `GET,POST /decks/{id}/versions/{v}/feedback`                                                         |
| Approvals | `GET /approvals` · `POST /approvals/submit` · `.../approve` · `.../reject`                           |
| Analytics | `GET /analytics/summary`                                                                             |

Sign-in is **Entra ID single sign-on only**. The front end posts its Graph access
token and id token to `/auth/login` and receives an *opaque* session handle
(`<user_id>:<uuid4 hex>`); the signed JWT it stands for stays in Redis, so a session
can be revoked instantly and a user may hold only one at a time. Roles come from the
`group_id_role_mapping` table, matched against the caller's Entra group membership.

Every route except the probes and `/auth/login|refresh` requires that handle as a
bearer credential. Per-session routes additionally require that the caller **owns**
the session; admins are generally exempt, but feedback submission requires strict
ownership even for an admin. A non-owner gets `404`, not `403`, so session ids
cannot be probed.

### Deck-version feedback frontend contract

The feedback resource is nested under the deck version it evaluates:

```http
GET  /api/decks/{session_id}/versions/{deck_version}/feedback
POST /api/decks/{session_id}/versions/{deck_version}/feedback
```

Submit a required JSON boolean and an optional comment of at most 1,000
characters:

```json
{
  "thumbs_up": false,
  "comment": "The final recommendation needs more supporting detail."
}
```

Both a successful `GET` (`200`) and `POST` (`201`) return:

```json
{
  "session_id": "session-123",
  "deck_version": 2,
  "thumbs_up": false,
  "comment": "The final recommendation needs more supporting detail."
}
```

Only the session owner can submit, and each version accepts feedback once.
Duplicate submission returns `409`; a missing version, inaccessible session, or
feedback that has not been submitted returns `404`; invalid input returns `422`.

Frontend implementations must keep an explicit three-state rating
(unselected/`true`/`false`) so a thumbs-down value is not dropped as falsy. Load
feedback whenever the selected version changes, keep the form hidden or disabled
until that GET completes, require a rating, set the comment input `maxLength` to
1,000, and disable submission while POST is pending. After `201`, render the
returned feedback read-only. After `409`, re-fetch GET and render the stored
feedback rather than retrying. Feedback state must remain independent between
deck versions.

---

## How a deck request flows

```
POST /api/decks/{id}/invoke   (SSE)
  ├─ restore the session from Postgres if Redis lost it, then append the user message
  └─ deck_handler.run_deck_flow   (asyncio.to_thread — the event loop stays free for SSE)
       ├─ persist the user turn (Postgres)
       ├─ deck_workflow (LangGraph)
       │    triage ─┬─ chat ─────────────────────────────────────────────► END
       │            ├─ classification only ───────────────────────► slides
       │            └─ needs planner ─► extraction ─► planning ─┬─ clarify ─► END
       │                                                         ├─ remove, no draft ─► END
       │                                                         └─ slides
       │    slides ─► collect ─┬─ publish ─► END
       │                       └─ done ────► END
       ├─ persist the assistant turn + session snapshot (Postgres)
       └─ emit the `done` event
```

| Node         | What it does |
| ------------ | ------------ |
| `triage`     | Applies an explicit classification mention, then `triage_agent.classify` splits the turn into N instructions (`plan` · `edit` · `enrich` · `remove` · `classification` · `language`). One structured LLM call — a single turn can carry several instructions. Before a new deck is planned it asks for what the brief is missing (goal, audience, deck type, and the `creativity_level` and `number_of_slides` sliders); a slide count stated in the message answers the size question. A request for more than 50 slides is refused politely, and no plan may exceed 100 slides including quiz answer slides (`SlideCountConstants`). |
| `extraction` | `ensure_session_indexed` waits out in-flight background ingestion, so the deck is never planned against half-indexed sources. |
| `planning`   | `planner_agent.run_planner` is a tool-calling agent that mutates the stored `DeckPlan` (create / revise / remove slides, set classification and languages). `derive_work_order` diffs the plan before and after into one `SlideTask` per slide with an op: `create` · `enrich` · `rewrite` · `relayout` · `keep` · `remove`. |
| `slides`     | **Content phase.** `ppt_agent.run_ppt_agent` runs once per changed content slide, in parallel (`SlideAgentConstants.MAX_WORKERS`): research the slide's own evidence, then write its copy. Role slides (agenda, takeaways, …) run afterwards via `run_role_slide_agent` and are regenerated whenever any content slide changed or a slide was removed. |
| `collect`    | **Design phase.** Renumbers slides contiguously, runs deck-level checks (duplicate topics) and saves the draft. `design_contract.build` fixes the master, grid, tokens and per-slide layout once for the whole deck; `PptCreatorAgent` designs each changed slide into its own `.pptx` in parallel (`PptCreatorConstants.MAX_WORKERS`), reusing the cached file of every slide whose content hash is unchanged; `DeckStitcher` merges them into one deck. |
| `publish`    | `persist_deck` uploads the English deck, produces every other language by rewriting the text runs of that same file (`pptx_translator`), uploads each to Blob and records a new numbered version. |

**Per-slide design ladder** (`PptCreatorAgent`) — every slide always ends up with a file:

1. **Tier 1 — creative.** The model writes a `DeckBuilder` program. It is statically
   checked, run in a sandboxed subprocess (throwaway directory, minimal environment,
   hard timeout), reopened and measured by the geometry gate; failures are repaired
   within `MAX_CODE_FIX_ATTEMPTS`.
2. **Tier 2 — layout fit.** The model picks one of a shortlist of real template layouts
   and fills its placeholders. The plan is measured before anything runs, then compiled
   deterministically and gated like tier 1.
3. **Tier 3 — deterministic render** on a template layout, with no model involved.

What the graph guarantees:

- **One version per turn.** Only `publish` mints a version; nothing earlier in the turn
  writes a deck to Blob.
- **The cheapest correct path.** A chat reply ends at `triage`; a classification-only
  change skips planning and re-renders the existing draft; `keep` slides never call the
  model; `rewrite` skips retrieval; unchanged slides reuse their cached `.pptx`; and
  translations never re-render the design.
- **Flat cost as the deck grows.** Each content agent sees only its own slide and its
  neighbours' titles (`DeckContext`), never the whole deck.
- **Fault tolerance.** A failed content slide falls back to its stored draft or planned
  outline; a design or stitching failure still saves the turn's content, so the next
  turn retries design without paying for retrieval again; a slide that never passed the
  gate ships but is not cached, so the next turn rebuilds it.

**Progress streaming.** Every node emits `agent_step` events on the session's event bus
(plus `agent_start`, `deck`, `agent_error` and a final `done` carrying the turn result).
`/invoke` streams them live; `/stream` replays the session's history and then follows
live events until `done`, so a client can reconnect mid-run. The `done` result includes
`reply`, `deck_version`, `deck_updated` (true only if this turn published), `slides_built`,
`gaps` and `actions_taken`.

**Durable activity history.** Each `/invoke` receives a unique `run_id`, and the same
user-visible events are batch-persisted to Postgres when that run succeeds or fails, so
a browser disconnect or service restart does not erase the activity history. These are
the status/tool activity events already emitted to the UI, not private model
chain-of-thought.

`GET /decks/{id}/messages` remains backward compatible: `messages` is still the
ordinary chat array, and `timeline` adds a combined history whose items have
`kind: "message"` or `kind: "workflow_event"`. Workflow events include `run_id`,
`event_index`, `type`, `agent`, `message`, `data`, and `ts`, so the frontend can
filter or style them separately and group them with the user and assistant turns
from the same run. With `?limit=N`, the API returns the latest N chat messages plus
all persisted events belonging to those messages' run ids. Event history is retained
until the deck session is deleted. The session-restore snapshot stored alongside an
assistant turn is internal recovery state and is never returned by this endpoint.

---

## Ingestion

Uploads are validated (type, size, non-empty) and stored synchronously, then
extracted, chunked, summarised and indexed in the background — a slow source such as
video can never time out the request. Planning waits for indexing to finish via
`ensure_session_indexed`, so a deck is never planned against half-indexed sources.

Re-uploading the same filename creates a new version; retrieval filters to the latest.

Extractor selection is tiered (`app/genai/ingestion/registry.py`): deterministic
parsers, then local processing, then managed Azure services, with the highest
available tier winning each MIME type. Moving video from local processing to Azure
Video Indexer is an additive change — see `app/genai/ingestion/local/README.md`.

---

## Configuration

Configuration is split by responsibility, with one source of truth each:

- `app/config/constants.py` — static, non-secret values (limits, chunk sizes, file
  extensions, table/Redis key names, roles, status values, agent budgets).
- `app/config/settings.py` — secrets and per-deployment values only, loaded from the
  environment by Pydantic `BaseSettings`.
- `app/config/keyvault.py` — hosted-mode secret source (structure only; the Azure
  Key Vault integration is not implemented yet).

Secret loading is environment-aware:

- **local** (`APP_TYPE=local`): loads `app/config/<APP_ENV>.env`.
- **hosted** (`APP_TYPE=hosted`): loads one JSON blob from Azure Key Vault and
  normalises `UPPER-HYPHEN` names to `UPPER_SNAKE`, so field names match in both modes.

Secrets are `SecretStr`, so a settings dump or log line cannot leak them. No
credential has a non-empty default. Start from `app/config/.env.example`.

---

## Deployment

```bash
docker build -f helm/Dockerfile -t se-agent-service:1.0.0 .
helm upgrade --install se-agent ./helm -f helm/values.yaml
```

The image runs as a non-root user, exposes port 8000, and carries `ffmpeg` and the
OpenCV native libraries only because of `app/genai/ingestion/local`. JWT keys are
mounted from a Kubernetes secret; all other secrets come from Key Vault at startup.
