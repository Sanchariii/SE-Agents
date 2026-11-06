# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

One FastAPI service (Python 3.11+) that authenticates users via Entra ID SSO, ingests uploaded source material, and builds grounded, Rockwell-branded PowerPoint decks with a LangGraph multi-agent workflow. All routes are under `/api` (docs at `http://localhost:8002/api/docs`). Long-form design docs live in [docs/](docs/README.md); start there for deep dives.

## Commands

One-time setup:
```bash
cp app/config/.env.example app/config/dev.env        # fill in values
python scripts/developer_tools/generate_jwt_keys.py
python -m venv .venv && .venv/Scripts/activate && pip install -r requirements-dev.txt
```

Run:
```bash
docker compose up --build                            # recommended: app + Redis (+ ffmpeg/OpenCV for video)
python -m app.main                                   # native; needs Redis at REDIS_URI_LOCAL
python scripts/developer_tools/check_config.py       # diagnose which dependencies are reachable
```

Tests (`pytest.ini`: `testpaths = tests`, `asyncio_mode = auto`):
```bash
pytest tests/test_workflow.py                        # one file
pytest tests/test_workflow.py::test_name             # one test
pytest -k "stitcher or geometry"                     # by keyword
pytest                                               # full suite
```
Unit tests need no infrastructure: [tests/conftest.py](tests/conftest.py) forces `APP_TYPE=local` and an unreachable `REDIS_URI` so Redis drops to its in-memory fallback, and the lifespan tolerates Postgres being down. The `client` fixture's base URL already includes `/api`, so tests call `"/health"`, not `"/api/health"`. The `session_store` fixture stubs out the Postgres-backed handlers. `scripts/manual_testing/*.py` are live end-to-end scripts that run against a service that is already up; they are not pytest.

There is no configured linter or formatter.

## Architecture

**Layering (never invert):** `core/services` (thin `APIRouter`s, plus app assembly in `server.py`) → `core/handlers` (business logic and persistence, module-level singletons) → `utilities` (Postgres, Redis, Blob, LLM gateway, events) and `genai`. `config` and `core/models` are the shared base. Domain code raises errors from `app/config/exceptions.py` and never imports FastAPI.

**Deck turn lifecycle.** `POST /decks/{id}/invoke` never starts the workflow itself. [run_supervisor.py](app/core/handlers/run_supervisor.py) admits the run: it creates one, or returns the run that is already active, so duplicate tabs or resent requests can't double-run. It runs the blocking LangGraph on a worker thread and heart-beats a lease in `workflow_runs`. The SSE response only *watches* the run through the session event bus. `GET /decks/{id}/stream` replays and then follows the same bus, so clients can reconnect mid-run. A stop (`POST /decks/{id}/stop`) or a lost lease is **cooperative**. [run_control.py](app/utilities/run_control.py) binds a `RunControl` on a ContextVar, and long-running code calls `checkpoint()`. Stop/fence exceptions derive from `BaseException` on purpose, so the many `except Exception` per-slide fallbacks cannot swallow them. Don't change them to `Exception`.

**The graph** ([deck_workflow.py](app/genai/workflows/deck_workflow.py) `build_graph`). The README's flow diagram predates the approval step; the code is authoritative:
- `triage` → END (chat reply, or clarifying questions when the `DeckBrief` in [brief.py](app/genai/agents/brief.py) is missing goal/audience/deck type/creativity level/deck size; the questions also go out as a typed `questions` event, with creativity as a `scale` question under the static id `creativity_level` and the deck size as a `scale` question under `number_of_slides` (`5-10`/`10-15`/`15-20`/`20+`, skipped when the user states a count, range or cap; asked for the session's first deck, and again only when triage tags a request `size_intent` `new_deck`/`resize` with no size, never for slide edits, adds or removals), answered through `answers` on `/invoke`; the retired `detailing_level` id is still accepted and ignored. A request for more than `SlideCountConstants.MAX_REQUESTED` (50) slides ends here too, with a polite refusal the triage model writes, plus the size slider on a new deck) | `extraction` | `approve` | `restyle` (a deck-wide creativity/quiz-answer-placement change, handled in code without the planner) | `slides` (classification-only change re-renders the existing deck).
- `extraction` waits out in-flight background ingestion → `planning`. The tool-calling planner mutates the stored `DeckPlan`, setting `slide_type` (NORMAL/QUIZ/FAQ) and internal `planner_special_instructions` per slide. [plan_rules.py](app/genai/agents/plan_rules.py) then deterministically pairs each quiz question with an answer slide, or keeps answers in the notes under a hard slide cap. No stored plan may exceed `SlideCountConstants.MAX_PLAN_SLIDES` (100) slides, answer slides included: `plan_rules.ceiling_violation` is checked before `build_plan`, `revise_slides`, `insert_slides` and an answer-placement change store anything, and an over-ceiling change is rejected with the previous plan kept. The planner sizes the deck from the brief's slide count (at most `MAX_AGENT_DECIDED` (35) when it decides itself). `derive_work_order` diffs the plan before and after into per-slide ops (`create`/`enrich`/`rewrite`/`relayout`/`keep`/`remove`).
- `planning` → END with a proposed plan awaiting approval (`await_approval`; the user approves with `/approve-plan`), or → `slides`. `approve` confirms the plan and goes straight to `slides`.
- `slides` (content phase): `ppt_agent` runs once per changed slide in parallel, seeing only its own slide plus its neighbours' titles (`DeckContext`). QUIZ slides write structured `quiz` content (the answer is always added to the notes), FAQ slides write `faq` pairs, and answer slides are skipped.
- `collect` (design phase): `design_contract.build` fixes the master/grid/tokens once per deck. Quiz answer slides get their question's draft with `quiz.reveal=True`. `PptCreatorAgent` designs each changed slide into its own `.pptx`, passing `slide_type`, the creativity level (a slide override, else the session brief), the slide's `user_instructions` and the planner instructions, and reusing cached files by content hash plus a design key. In a second pass it builds each answer slide by re-running its question's program with the reveal on (`build_answer_slide`); options drawn with `add_choice` get the "Quiz-N-Answered" highlight. `DeckStitcher` merges them.
- `publish`: the only node that writes a deck to Blob and mints a version. One version per turn. Other languages come from rewriting the text runs of the English file (`pptx_translator`), never from re-rendering.

Every node is wrapped by `stage_node` in [recovery.py](app/genai/workflows/recovery.py). The wrapper adds stage-scoped log context, a stop/lease check before work starts, recovery events, and a single-stage re-run of the `RunStages.RETRYABLE` stages (triage and planning). If you add a node, wrap it the same way.

**Per-slide design ladder** ([ppt_creator_agent.py](app/genai/agents/ppt_creator_agent.py)). Tier 1: the model writes a `DeckBuilder` program, which [code_guard.py](app/genai/ppt/code_guard.py) checks statically before it runs in [sandbox.py](app/genai/ppt/sandbox.py). Tier 2: the model fills placeholders on a real template layout. Tier 3: a deterministic render. Tier 1 and 2 output passes a geometry gate, with repair loops in between. Ahead of tier 1, a slide whose resolved creativity is HIGH runs **max mode** when the renderer is available. It renders the gate-clean slide with headless LibreOffice ([slide_render.py](app/genai/ppt/slide_render.py)), has a vision model judge it ([slide_reviewer.py](app/genai/agents/slide_reviewer.py)), and redoes it up to 3 times. Content and takeaways slides and family leaders are designed on the **High Creativity master**, which the catalog keeps out of `catalog.layouts`, so LOW/MED never see it. LOW/MED slides never reach any of this. The sandbox has no `PYTHONPATH` and inherits no secrets. [deck_builder.py](app/genai/ppt/deck_builder.py) is *copied* into it, so **it must never import from `app.*`**. [test_import_hygiene.py](tests/test_import_hygiene.py) enforces that, forbids `app/` importing from `scripts/`, and asserts that retired modules stay deleted. Brand rules for generated code (no own `Presentation`, no `shapes.add_*`, no hex color literals) are enforced in `code_guard.py`. The templates and logos are in `company-ppt-templet/`.

**Design principles and priority.** [design_principles.py](app/genai/agents/design_principles.py) holds the presentation principles as three views (`PLANNING_PRINCIPLES` for the planner, `COPY_PRINCIPLES` for the writer, `DESIGNER_PRINCIPLES` for both design tiers) plus one `PRIORITY_RULE`: hard rules (enforced in code) > the user's instructions (`SlidePlan.user_instructions`) > the planner's directions > the principles. They sit in system prompts, so a default slide's user prompt is unchanged. The gate also keeps slides lean: rule 10 measures real text (bullets, bold, inherited sizes, table growth), rules 11-14 reject empty text boxes, repeated text, a repeated image and a distorted image, and rule 15 (advisory, from the builder's `warnings.json` component ledger) flags clutter and earns one keep-best polish pass. `DeckBuilder` components fit their own text (floor 11pt) and offer `text_height`/`fits`/`split`.

**LLM calls** all go through the ROK AI Gateway (Azure APIM) in [apim_llm_utils.py](app/utilities/apim_llm_utils.py). It provides `chat_json`/`chat_text`/`embeddings` and `get_langchain_llm` for the tool-calling agents. There is no direct OpenAI/Anthropic client. Models are chosen per node in `ModelConstants` in [constants.py](app/config/constants.py), and the gateway route (azure-openai, foundry or bedrock) is inferred from the model name. Token usage and cost are captured by `app/utilities/analytics/`, using prices from `model-pricing.json`.

**Ingestion.** Uploads are validated and stored synchronously. Extraction, chunking, summarisation and indexing into Azure AI Search then run in the background. [registry.py](app/genai/ingestion/registry.py) picks extractors by tier (deterministic parsers, then local processing, then managed Azure services); the highest available tier wins each MIME type. Re-uploading the same filename creates a new version, and retrieval filters to the latest one.

**State.** Redis holds working session state (plan, draft, brief, events) through [session_store.py](app/core/handlers/session_store.py). Postgres holds durable records: chat turns plus a session snapshot for restore, workflow runs and events, deck versions, users and role mappings, and analytics. If Redis lost a session, it is restored from Postgres. Postgres tables are created on startup. Redis and Blob both fall back to in-memory or on-disk storage when unconfigured.

## Conventions that matter here

The full standard is in [.github/copilot-instructions.md](.github/copilot-instructions.md), and its agent list in §1 is outdated. These are the rules that are easy to get wrong:

- **Config:** app code reads config only through `get_settings().<field>`, never `os.environ`. Static values go in `constants.py` classes; secrets and per-deployment values go in a `Settings` field (as `SecretStr` when sensitive) plus `app/config/<APP_ENV>.env` or Key Vault. **An env var with no matching `Settings` field is silently ignored** (`extra="ignore"`). Names map `UPPER-HYPHEN` (Key Vault) → `UPPER_SNAKE` (env) → `snake_case` (field); use `AliasChoices` instead of duplicate fields. Read [.github/instructions/config-secrets-constants.instructions.md](.github/instructions/config-secrets-constants.instructions.md) before touching `app/config/**`.
- **Never block the event loop:** routes are `async def`. Postgres, Blob, sync SDK calls and parsing go through `asyncio.to_thread`. Fan out independent work with `asyncio.gather` or a bounded `ThreadPoolExecutor`, with worker counts from constants.
- **Security:** guard routes with `require_auth`, `require_role(...)`, `require_session_access`, `require_session_role(...)`. A non-owner of a session gets **404, not 403**. Use parameterized SQL only (`text(sql)` with `:params`).
- **Logging:** `log = get_logger(__name__)` in every module, lazy `%s` formatting, `log.info` at key steps. The request/correlation id is attached through a ContextVar filter. Never log secrets or tokens.
- Use lazy imports inside functions for heavy or optional dependencies (SQLAlchemy, Azure SDKs, LangChain, python-pptx) and to break import cycles. Use `@lru_cache` for singletons, clients and settings.
- Write module docstrings and function docstrings (purpose/Args/Returns). Step comments explain *why*, not what.
- When an intentionally broad `except Exception` is best-effort work, mark it `# noqa: BLE001` and log full context.
