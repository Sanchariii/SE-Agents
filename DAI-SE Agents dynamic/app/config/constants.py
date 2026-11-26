"""Static application constants — the single source of truth for non-secret values.

Nothing here may be a credential, an endpoint, or anything that differs between
deployments: those belong in the env file / Key Vault and are exposed through
``app.config.settings.Settings``.

The only environment lookups permitted in this module are ``APP_TYPE`` and
``APP_ENV``, because they select *which* secret source is used and therefore have
to be known before ``Settings`` can be constructed.
"""

import os
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent


class AppType:
    """Where secrets come from."""
    LOCAL = "local"          # app/config/<APP_ENV>.env
    HOSTED = "hosted"        # Azure Key Vault
    ALL = (LOCAL, HOSTED)


class AppConstants:
    APP_TYPE = AppType.HOSTED     # local | hosted
    APP_ENV = os.environ.get("APP_ENV", "dev")               # dev | qa | prod
    APP_NAME = "se-agent-service"
    API_VERSION = "1.0.0"
    AGENT_VERSION = "1.0.0"                                 # sent to APIM as x-rok-agent-version; bump per release
    API_PREFIX = "/api"                                       # global path prefix for every REST route
    SERVICE_HOST = "0.0.0.0"
    SERVICE_PORT = 8002
    CONFIG_DIR = CONFIG_DIR
    ENV_FILE = CONFIG_DIR / f"{APP_ENV}.env"

def _detect_app_type() -> str:
    """Infer the secret source (local vs hosted) so nobody hand-edits ``APP_TYPE``.

    if the local env file (``app/config/<APP_ENV>.env`` — i.e.
    ``dev.env``) exists and declares ``IS_LOCAL=True`` we run in *local* mode;
    otherwise — no env file (a hosted container ships without a ``*.env``) or
    ``IS_LOCAL`` is not truthy — we run in *hosted* mode.

    Returns:
        ``AppType.LOCAL`` when a ``dev.env`` with ``IS_LOCAL=True`` is present,
        otherwise ``AppType.HOSTED``.
    """
    env_file = AppConstants.ENV_FILE  # app/config/<APP_ENV>.env (dev.env by default)
    try:
        # No local env file => hosted deployment (the image ships without a *.env).
        if not env_file.is_file():
            print(f"no local env file at {env_file}; defaulting to hosted mode")
            return AppType.HOSTED
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            # Skip blanks, comments, and any line that isn't a KEY=VALUE assignment.
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip().upper() == "IS_LOCAL":
                # Tolerate spacing/quotes/case: IS_LOCAL = "True", is_local=true, ...
                is_local = value.strip().strip("\"'").lower() == "true"
                print(f"local env file {env_file} declares IS_LOCAL={is_local} , selecting {AppType.LOCAL if is_local else AppType.HOSTED}")
                return AppType.LOCAL if is_local else AppType.HOSTED
    except OSError:
        # Unreadable env file: fail safe to hosted so local-only seed creds never load.
        print(f"cannot read local env file at {env_file}; defaulting to hosted mode")
        return AppType.HOSTED
    # Env file present but no IS_LOCAL entry => treat as hosted.
    print(f"local env file {env_file} has no IS_LOCAL entry; defaulting to hosted mode")
    return AppType.HOSTED

# Auto-detected override of the hand-set default in AppConstants above: presence of a
# dev.env with IS_LOCAL=True selects local, everything else selects hosted.
AppConstants.APP_TYPE = _detect_app_type()


def _detect_app_type() -> str:
    """Infer the secret source (local vs hosted) so nobody hand-edits ``APP_TYPE``.

    if the local env file (``app/config/<APP_ENV>.env`` — i.e.
    ``dev.env``) exists and declares ``IS_LOCAL=True`` we run in *local* mode;
    otherwise — no env file (a hosted container ships without a ``*.env``) or
    ``IS_LOCAL`` is not truthy — we run in *hosted* mode.

    Returns:
        ``AppType.LOCAL`` when a ``dev.env`` with ``IS_LOCAL=True`` is present,
        otherwise ``AppType.HOSTED``.
    """
    env_file = AppConstants.ENV_FILE  # app/config/<APP_ENV>.env (dev.env by default)
    try:
        # No local env file => hosted deployment (the image ships without a *.env).
        if not env_file.is_file():
            print(f"no local env file at {env_file}; defaulting to hosted mode")
            return AppType.HOSTED
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            # Skip blanks, comments, and any line that isn't a KEY=VALUE assignment.
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip().upper() == "IS_LOCAL":
                # Tolerate spacing/quotes/case: IS_LOCAL = "True", is_local=true, ...
                is_local = value.strip().strip("\"'").lower() == "true"
                print(f"local env file {env_file} declares IS_LOCAL={is_local} , selecting {AppType.LOCAL if is_local else AppType.HOSTED}")
                return AppType.LOCAL if is_local else AppType.HOSTED
    except OSError:
        # Unreadable env file: fail safe to hosted so local-only seed creds never load.
        print(f"cannot read local env file at {env_file}; defaulting to hosted mode")
        return AppType.HOSTED
    # Env file present but no IS_LOCAL entry => treat as hosted.
    print(f"local env file {env_file} has no IS_LOCAL entry; defaulting to hosted mode")
    return AppType.HOSTED


# Auto-detected override of the hand-set default in AppConstants above: presence of a
# dev.env with IS_LOCAL=True selects local, everything else selects hosted.
AppConstants.APP_TYPE = _detect_app_type()


class KeyVaultConstants:
    """Names of the Helm-injected env vars that locate Key Vault in hosted mode."""
    URL_ENV = "KEYVAULT_URL"
    SECRET_NAME_ENV = "KEYVAULT_SECRET_NAME"
    TENANT_ID_ENV = "AZURE_TENANT_ID"

class SecretsStoreConstants:
    """Kubernetes Secrets Store CSI driver mount (hosted mode).

    The ``sea-secrets`` SecretProviderClass projects each Key Vault secret as a file
    under ``MOUNT_PATH``, named by its ``UPPER-HYPHEN`` object name (e.g. ``PG-HOST``).
    """
    PROVIDER_CLASS = "sea-secrets"
    MOUNT_PATH = "/mnt/secrets-store"

    # Multi-line "content" secrets (JWT keys, CA cert) are consumed as FILES by path:
    # the CSI driver projects each at MOUNT_PATH/<object>. These names intentionally
    # don't match any Settings field, so `_apply_secrets_store` leaves them as files
    # instead of loading their contents into a scalar field.
    JWT_PRIVATE_KEY_OBJECT = "JWT-PRIVATE-KEY"
    JWT_PUBLIC_KEY_OBJECT = "JWT-PUBLIC-KEY"
    CA_BUNDLE_OBJECT = "CA-BUNDLE"


class SecretsStoreConstants:
    """Kubernetes Secrets Store CSI driver mount (hosted mode).

    The ``sea-secrets`` SecretProviderClass projects each Key Vault secret as a file
    under ``MOUNT_PATH``, named by its ``UPPER-HYPHEN`` object name (e.g. ``PG-HOST``).
    """
    PROVIDER_CLASS = "sea-secrets"
    MOUNT_PATH = "/mnt/secrets-store"

    # Multi-line "content" secrets (JWT keys, CA cert) are consumed as FILES by path:
    # the CSI driver projects each at MOUNT_PATH/<object>. These names intentionally
    # don't match any Settings field, so `_apply_secrets_store` leaves them as files
    # instead of loading their contents into a scalar field.
    JWT_PRIVATE_KEY_OBJECT = "JWT-PRIVATE-KEY"
    JWT_PUBLIC_KEY_OBJECT = "JWT-PUBLIC-KEY"
    CA_BUNDLE_OBJECT = "CA-BUNDLE"


class RouteTags:
    AUTH = "auth"
    USERS = "users"
    DECKS = "decks"
    APPROVALS = "approvals"
    ANALYTICS = "analytics"
    OPS = "ops"


class HttpConstants:
    CORRELATION_HEADER = "x-correlation-id"
    CORS_ALLOW_METHODS = ("GET", "POST", "DELETE", "OPTIONS","PATCH")
    CORS_ALLOW_HEADERS = ("Authorization", "Content-Type", CORRELATION_HEADER)
    CORS_EXPOSE_HEADERS = (CORRELATION_HEADER,)
    TIMEOUT_SECONDS = 120.0
    CONNECT_TIMEOUT_SECONDS = 15.0
    MAX_CONNECTIONS = 100
    MAX_KEEPALIVE_CONNECTIONS = 20


class TlsConstants:
    """Env vars honoured as a CA-bundle source and re-exported for child libraries."""
    BUNDLE_ENV_VARS = ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE")
    # Corporate CA bundle shipped in app/config; the default TLS trust source.
    CA_BUNDLE_PATH = str(CONFIG_DIR / "GenAI-api-q.rockwell.pem")
    # Runtime merge of the public roots (certifi) + the corporate CA, written to /tmp.
    COMBINED_BUNDLE_NAME = "se-agent-ca-bundle.pem"


class Roles:
    """Application roles. A user holds exactly one *effective* role — the strongest one
    their Entra group memberships grant — plus every role that one implies.

    There are exactly three, matching the directory groups (Admin / Approver / Member).
    Privileges are cumulative: a Member is the baseline, an Approver can do everything a
    Member can plus decide approvals, and an Admin can do everything."""
    ADMIN = "admin"
    APPROVER = "approver"
    MEMBER = "member"
    ALL = (ADMIN, APPROVER, MEMBER)

    # Strongest first: how a single effective role is picked out of several groups.
    PRECEDENCE = (ADMIN, APPROVER, MEMBER)

    # Privileges are cumulative, so a stronger role also carries the weaker ones.
    IMPLIED = {
        ADMIN: (ADMIN, APPROVER, MEMBER),
        APPROVER: (APPROVER, MEMBER),
        MEMBER: (MEMBER,),
    }

    # How the `role` column of the group->role mapping table spells each role
    # (compared lower-cased), so the directory's own wording maps onto our three roles.
    GROUP_ROLE_ALIASES = {
        "admin": ADMIN,
        "approver": APPROVER,
        "member": MEMBER,
    }


class JwtConstants:
    ALGORITHM = "RS256"
    ISSUER = AppConstants.APP_NAME
    BEARER_PREFIX = "bearer "
    # Access-token (and therefore session) lifetime. ~1 hour: long enough to avoid
    # constant refreshes, short enough that a leaked token self-expires quickly.
    ACCESS_TOKEN_TTL_MINUTES = 60
    PRIVATE_KEY_PATH = str(CONFIG_DIR / "private")
    PUBLIC_KEY_PATH = str(CONFIG_DIR / "public")


class AuthConstants:
    """Opaque session handles. The browser only ever holds ``<user_id>:<uuid4 hex>``;
    the signed JWT it stands for is kept server-side in Redis and never leaves it."""
    TOKEN_SEPARATOR = ":"
    # Session lifetime tracks the access-token lifetime so the two can never disagree.
    SESSION_TTL_MINUTES = JwtConstants.ACCESS_TOKEN_TTL_MINUTES
    SESSION_TTL_SECONDS = SESSION_TTL_MINUTES * 60
    # Absolute cap on one login: refreshing extends the session but never past this,
    # after which the user must sign in through Entra again.
    REFRESH_TTL_HOURS = 8
    REFRESH_TTL_SECONDS = REFRESH_TTL_HOURS * 60 * 60
    # Hint returned by /auth/validate: the client should refresh once the session has
    # less than this left, so a missed poll or two still can't strand it.
    REFRESH_THRESHOLD_SECONDS = 300
    # Group->role rows change rarely; cache them so login costs one Redis read.
    GROUP_ROLE_CACHE_TTL_SECONDS = 900
    STATUS_SUCCESS = "Success"
    LOGIN_MESSAGE = "Login successful"
    REFRESH_MESSAGE = "Session refreshed"
    LOGOUT_MESSAGE = "Logged out"
    VALID_MESSAGE = "Session is valid"


class GraphConstants:
    """Microsoft Graph endpoints used to resolve the caller and their group memberships."""
    BASE_URL = "https://graph.microsoft.com/v1.0"
    ME_PATH = "/me"
    CHECK_MEMBER_GROUPS_PATH = "/me/checkMemberGroups"
    ME_SELECT = "id,displayName,mail,userPrincipalName,jobTitle,department,officeLocation"
    TIMEOUT_SECONDS = 15.0
    # Graph refuses more than 20 group ids per checkMemberGroups call, so batch them.
    MAX_GROUP_IDS_PER_CALL = 20


class EntraConstants:
    """Entra ID (Azure AD) id-token verification. Signature checking is enabled as soon
    as the tenant and client ids are configured; without them login still requires a
    Graph-accepted access token, which is logged as a weaker posture."""
    JWKS_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    ISSUER_TEMPLATES = (
        "https://login.microsoftonline.com/{tenant_id}/v2.0",
        "https://sts.windows.net/{tenant_id}/",
    )
    ID_TOKEN_ALGORITHMS = ("RS256",)
    JWKS_CACHE_LIFESPAN_SECONDS = 3600
    CLOCK_SKEW_SECONDS = 60


class PgTables:
    USERS = "users"
    APPROVALS = "deck_approvals"
    USER_SESSIONS = "user_sessions"
    DECK_VERSIONS = "ppt_versions"
    CHAT_MESSAGES = "chat_messages"     # durable chat history + final-response metadata
    WORKFLOW_EVENTS = "workflow_events"  # durable user-visible SSE activity events
    WORKFLOW_RUNS = "workflow_runs"      # operational run registry: one active run per session
    DOCUMENTS = "session_documents"      # durable source-file registry + ingestion status
    GROUP_ROLE_MAPPING = "group_id_role_mapping"
    # Analytics fact + dimension tables (best-effort telemetry, never on the hot path).
    ANALYTICS_WORKFLOW_RUNS = "analytics_workflow_runs"   # one row per user turn / workflow run
    ANALYTICS_AGENT_RUNS = "analytics_agent_runs"         # one row per agent-node x model
    ANALYTICS_INGESTION_RUNS = "analytics_ingestion_runs"  # one row per document ingestion
    MODEL_PRICING = "model_pricing"                       # USD price per million tokens by model


class AnalyticsStatus:
    """Terminal states shared by analytics run rows."""
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    FALLBACK = "fallback"      # agent completed only via a non-model fallback path
    PARTIAL = "partial"        # ingestion finished with some units missing


class AnalyticsPricingStatus:
    """Whether an agent run's token usage could be priced."""
    PRICED = "priced"
    UNPRICED = "unpriced"     # model has no row in model_pricing; cost left null


class WorkUnitType:
    """Agent-native output units; never compared across different types."""
    SLIDE = "slide"
    DOCUMENT = "document"
    PAGE = "page"
    CHUNK = "chunk"
    INSTRUCTION = "instruction"
    PLAN = "plan"


class AnalyticsConstants:
    """Static, non-secret tuning for analytics capture and the dashboard API.

    Prices themselves are data (the ``model_pricing`` table, seeded from the JSON
    below); only the seed file name and capture/query bounds live here.
    """
    # --- pricing seed ---
    PRICING_SEED_FILE = "model-pricing.json"      # repo-root JSON: [{model,input_cost,output_cost}]
    PRICING_SEED_PATH = CONFIG_DIR.parents[1] / "model-pricing.json"
    DEFAULT_CURRENCY = "USD"
    PRICE_VERSION = "v1"                           # bump when reseeding changes historical prices
    COST_PRECISION = 6                             # USD decimal places kept on snapshotted cost

    # --- non-blocking capture queue ---
    QUEUE_MAX_SIZE = 5000                          # bounded; overflow is dropped-with-warning
    WRITER_BATCH_SIZE = 100                        # rows flushed per insert
    WRITER_FLUSH_SECONDS = 2.0                     # max time a row waits before a flush
    SHUTDOWN_FLUSH_SECONDS = 5.0                   # grace period to drain on shutdown

    # --- dashboard API bounds ---
    DEFAULT_WINDOW_DAYS = 30                        # default look-back when no range is given
    MAX_WINDOW_DAYS = 366                           # cap to keep raw-fact queries fast
    DEFAULT_PAGE_SIZE = 50
    MAX_PAGE_SIZE = 200
    P95 = 0.95                                      # slow-tail percentile for latency cards

    # --- Phase 2 estimation baselines (minutes; org-approved, versioned) ---
    BASELINE_VERSION = "v1"
    BASELINE_READ_MINUTES_PER_PAGE = 3.0
    BASELINE_AUTHOR_MINUTES_PER_SLIDE = 12.0
    BASELINE_EDIT_MINUTES_PER_SLIDE = 6.0
    BASELINE_TRANSLATE_MINUTES_PER_SLIDE = 4.0
    BASELINE_ASSEMBLE_MINUTES_PER_DECK = 15.0
    BASELINE_REVIEW_MINUTES_PER_VERSION = 10.0


class GroupRoleColumns:
    """Column names of ``group_id_role_mapping``. Each entry lists accepted spellings so
    a directory-managed table can be read without a migration; first match wins."""
    GROUP_ID = ("group_id", "groupid", "id", "azure_group_id")
    GROUP_NAME = ("group_name", "groupname", "name")
    DISPLAY_NAME = ("display_name", "displayname", "description")
    ROLE = ("role", "role_name", "app_role")


class PgConstants:
    POOL_SIZE = 10
    MAX_OVERFLOW = 20
    POOL_RECYCLE_SECONDS = 1800


class EventConstants:
    """Bounds for the live in-memory event bus; durable history lives in Postgres."""

    MAX_QUEUE = 500
    MAX_HISTORY = 300
    FLUSH_SECONDS = 0.5             # incremental Postgres flush cadence for run events
    FLUSH_BATCH = 20                # ...or as soon as this many events are pending
    STREAM_POLL_SECONDS = 1.0       # cross-pod live tail: Postgres poll interval
    KEEPALIVE_SECONDS = 15.0        # SSE comment frame during long silent work
    REPLAY_PAGE_SIZE = 500          # events read per replay query


class RunStatus:
    """Operational lifecycle of one top-level workflow run (``workflow_runs.status``)."""
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RECOVERING = "RECOVERING"
    CANCELLING = "CANCELLING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    # A session may hold at most one run in these states (partial unique index).
    ACTIVE = (QUEUED, RUNNING, RECOVERING, CANCELLING)
    TERMINAL = (COMPLETED, FAILED, CANCELLED)


class RunStages:
    """Workflow graph node names, used as ``workflow_runs.current_stage``."""
    TRIAGE = "triage"
    EXTRACTION = "extraction"
    PLANNING = "planning"
    SLIDES = "slides"
    COLLECT = "collect"
    PUBLISH = "publish"
    # Stages retried as a whole on a retryable model failure.
    RETRYABLE = (TRIAGE, PLANNING)


class RunErrorCodes:
    """Stable, user-safe failure codes stored on a run and sent on terminal events."""
    LLM_GATEWAY_UNAVAILABLE = "LLM_GATEWAY_UNAVAILABLE"
    LLM_RATE_LIMITED = "LLM_RATE_LIMITED"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_AUTH_FAILED = "LLM_AUTH_FAILED"
    LLM_REQUEST_INVALID = "LLM_REQUEST_INVALID"
    LLM_OUTPUT_INVALID = "LLM_OUTPUT_INVALID"
    WORKER_LOST = "WORKER_LOST"
    WORKER_SHUTDOWN = "WORKER_SHUTDOWN"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class WorkflowRunConstants:
    """Run registry, heartbeat, cancellation and stage-recovery tuning."""
    HEARTBEAT_SECONDS = 5.0         # owner heartbeat; also how fast a cross-pod stop is seen
    STALE_AFTER_SECONDS = 90        # no heartbeat for this long => WORKER_LOST
    CANCEL_FORCE_SECONDS = 60       # CANCELLING longer than this is forced to CANCELLED
    STAGE_MAX_ATTEMPTS = 2          # whole-stage attempts for RunStages.RETRYABLE
    STAGE_BACKOFF_SECONDS = 3.0
    ACQUIRE_MAX_LOOPS = 3           # insert/select retries when a run finishes mid-acquire
    ERROR_MESSAGE_MAX_CHARS = 2000  # redacted raw error kept on the run row
    TIME_TAKEN_DECIMALS = 2         # `time_taken_minutes` precision on a turn's result


class RedisKeys:
    # Auth is keyed by the SHA-256 of the opaque handle, never the handle itself, so a
    # leaked Redis snapshot yields nothing that can be replayed against the API.
    AUTH_SESSION = "auth:session:{token_hash}"      # login session JSON (incl. the JWT)
    AUTH_REFRESH = "auth:refresh:{token_hash}"      # refresh handle -> {user_id, sid}
    AUTH_REFRESH_USED = "auth:refresh-used:{token_hash}"  # burned handle, for reuse detection
    AUTH_USER_ACTIVE = "auth:user:{user_id}"        # the one session a user may hold
    AUTH_GROUP_ROLES = "auth:group-roles"           # cached group_id -> role mapping
    SESSION = "session:{session_id}"                # SessionState JSON
    ACTIVE_SKILL = "active_skill:{session_id}"       # pinned skill version JSON
    SKILL_REFERENCES = "skill_references:{session_id}"  # inspected style profiles
    CKM = "ckm:{doc_id}:{version}"                  # canonical knowledge model JSON
    CONTENT = "content:{session_id}"                # retrieved ContentBundle JSON
    DRAFT = "draft:{session_id}"                    # DeckDraft JSON
    CRITIQUE = "critique:{session_id}"              # CriticVerdict JSON
    SOURCES_CHANGED = "sources_changed:{session_id}"
    # slide_number -> {path, hash, fallback} for the per-slide .pptx files a turn reuses
    SLIDE_ARTIFACTS = "slide_artifacts:{session_id}"
    BRIEF = "brief:{session_id}"                    # DeckBrief JSON
    # DeckDesignContext JSON: the Quiz/FAQ family layouts established for this deck
    DECK_DESIGN = "deck_design:{session_id}"

class SessionMemoryConstants:
    RESULT_KEY = "session_memory"     # key inside chat_messages.result holding the snapshot
    SNAPSHOT_VERSION = 1              # bump when the snapshot contract changes incompatibly
    KEEP_CHUNKS = False               # chunks are re-retrievable and dominate the row size

class SkillReferenceConstants:
    MAX_REFERENCE_BYTES = 10 * 1024 * 1024
    MAX_REFERENCES = 20
    MAX_REFERENCE_PROFILE_CHARS = 30000
    MAX_SESSION_REFERENCE_CHARS = 100000
    MAX_ARCHIVE_BYTES = 40 * 1024 * 1024
    MAX_ARCHIVE_ENTRIES = 2000
    MAX_PPTX_SLIDES = 40
    MAX_PPTX_SHAPES_PER_SLIDE = 100
    MAX_PDF_PAGES = 40
    MAX_IMAGE_PIXELS = 16_000_000
    MAX_WEBSITE_BYTES = 2 * 1024 * 1024
    WEBSITE_TIMEOUT_SECONDS = 5
    WEBSITE_MAX_REDIRECTS = 3
    MAX_STYLE_ITEMS = 8
    MAX_EXEMPLARS = 5
    MAX_SKILL_PROMPT_CHARS = 4000

class StorageConstants:
    LOCAL_ROOT = Path("./.local_storage")           # fallback when Blob is not configured
    CONTAINER_UPLOADS = "uploads"
    CONTAINER_DECKS = "decks"
    DECK_SAS_TTL_MINUTES = 60                        # lifetime of a deck download SAS URL
    UPLOAD_SAS_TTL_MINUTES = 30                      # lifetime of a direct-upload (write) SAS URL
    MAX_UPLOAD_BYTES = 200 * 1024 * 1024
    # Blob download tuning: stream large files (e.g. video) in bounded chunks so the
    # download reports progress and never buffers the whole file in one silent call.
    DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024           # 4 MB per streamed chunk
    DOWNLOAD_MAX_CONCURRENCY = 4
    EXTENSION_MIME = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".csv": "text/csv",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
    }


class SchedulerConstants:
    """In-process APScheduler housekeeping jobs. Kept lightweight and non-blocking:
    the scheduler runs on the app event loop and every job offloads its blocking I/O
    to a worker thread."""
    TIMEZONE = "UTC"
    UPLOAD_SWEEP_INTERVAL_MINUTES = 10          # how often to reap abandoned uploads
    UPLOAD_SWEEP_JOB_ID = "reap_abandoned_uploads"


class IngestionConstants:
    MAX_CHUNK_CHARS = 3000
    MIN_CHUNK_CHARS = 200
    MAX_SUMMARY_INPUT_CHARS = 12000
    SUMMARY_MAX_TOKENS = 400
    CAPTION_MAX_TOKENS = 300
    IMAGE_DESCRIPTION_MAX_TOKENS = 500
    # Extraction gate: block planning until in-flight ingestion finishes.
    WAIT_TIMEOUT_SECONDS = 1800.0
    POLL_INTERVAL_SECONDS = 2.0


class VideoConstants:
    """Local video pipeline — see app/genai/ingestion/local/README.md."""
    FRAME_INTERVAL_SEC = 1.0        # candidate sampling rate
    SCENE_THRESHOLD = 0.35          # histogram-diff scene cut (0..1)
    DEDUPE_HAMMING = 6              # dhash distance = duplicate
    MAX_FRAMES = 120                 # best keyframes kept per segment
    SEGMENT_LENGTH_SEC = 600.0      # keeps audio under the Whisper 25MB cap
    AUDIO_SAMPLE_RATE = 16000
    MIN_AUDIO_BYTES = 1024
    WHISPER_BACKEND_LOCAL = "local"
    WHISPER_BACKEND_AZURE = "azure"


class AzureConstants:
    MAX_RETRIES = 3
    DOC_INTELLIGENCE_MODEL = "prebuilt-layout"
    DOC_INTELLIGENCE_CAPTION_FIGURES = True         # AOAI-vision caption each figure
    SEARCH_ENDPOINT_TEMPLATE = "https://{name}.search.windows.net"
    # Bound Blob socket waits so a stalled download/upload fails loudly (and retries)
    # instead of hanging the ingestion thread with no log for minutes.
    BLOB_CONNECT_TIMEOUT_SECONDS = 30
    BLOB_READ_TIMEOUT_SECONDS = 120


class VideoIndexerConstants:
    """Azure Video Indexer REST API (ARM account, access-token auth)."""
    API_BASE = "https://api.videoindexer.ai"
    INDEXING_PRESET = "Default"
    PRIVACY = "Private"
    UPLOAD_TIMEOUT_SECONDS = 900.0
    POLL_INTERVAL_SECONDS = 15.0
    POLL_TIMEOUT_SECONDS = 3600.0
    STATE_PROCESSED = "Processed"
    STATE_FAILED = "Failed"
    MIME_TYPES = ("video/mp4", "video/quicktime", "video/x-matroska")


class SearchConstants:
    EMBEDDING_DIMENSIONS = 3072                     # text-embedding-3-large
    EMBEDDING_BATCH_SIZE = 64
    VECTOR_PROFILE = "vp-default"
    VECTOR_ALGORITHM = "hnsw-default"
    UPLOAD_BATCH_SIZE = 100
    UPLOAD_MAX_WORKERS = 4
    DEFAULT_TOP_K = 8


class LlmConstants:
    TIKTOKEN_MODEL_NAME = "gpt-4"                   # cl100k_base; avoids a tiktoken crash
    DEFAULT_TEMPERATURE = 0.2
    CHAT_MAX_TOKENS = 1200
    JSON_MAX_TOKENS = 2000
    JSON_RETRY_ATTEMPTS = 2
    # Generations per structured (JSON) call before the typed error is raised.
    STRUCTURED_ATTEMPTS = 3
    # Routes that receive response_format=json_object. Empty = off; the gateway's 400
    # param-shedding drops it safely where a model rejects it.
    JSON_MODE_ROUTES: tuple[str, ...] = ()
    # Bounded prefix/suffix of a bad model output kept in a parse-failure log line.
    DIAG_SNIPPET_CHARS = 300
    # Above this size a broken output is not echoed back; the model regenerates instead.
    REPAIR_ECHO_MAX_CHARS = 6000


class ApimConstants:
    """ROK AI Gateway (Azure APIM) routing for chat/agent models."""
    CHAT_API_VERSION = "2024-10-21"                 # azure-openai route query param
    TOKEN_EXPIRY_SKEW_SECONDS = 60                  # refresh the OAuth token this early
    TOKEN_TIMEOUT_SECONDS = 30.0
    TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    DEFAULT_TENANT_ID = "855b093e-7340-45c7-9f0c-96150415893e"
    REQUEST_TIMEOUT_SECONDS = 300.0                 # read timeout for one gateway call
    CONNECT_TIMEOUT_SECONDS = 10.0
    MAX_ATTEMPTS = 4                                # transport attempts per call (5xx/429/timeouts)
    MAX_PARAM_SHEDS = 2                             # 400 "unsupported param" drops, not retries
    BACKOFF_BASE_SECONDS = 1.0                      # exponential backoff with full jitter
    BACKOFF_CAP_SECONDS = 20.0
    RETRY_AFTER_CAP_SECONDS = 30.0                  # upper bound on a 429 Retry-After
    MAX_CALL_SECONDS = 900.0                        # overall deadline for one call incl. retries
    DIAG_BODY_CHARS = 2000                          # redacted response-body excerpt in logs
    # Response headers that correlate a call with APIM / backend logs. Never request headers.
    DIAG_HEADERS = ("apim-request-id", "x-ms-request-id", "x-request-id", "request-id",
                    "x-ms-client-request-id", "x-ms-region")
    JSON_MAX_BUDGET = 32000                         # ceiling when growing tokens on truncation

    # Gateway paths per provider.
    ROUTE_AZURE_OPENAI = "azure-openai"
    ROUTE_FOUNDRY = "foundry"
    ROUTE_BEDROCK = "bedrock"
    # Model name prefixes that use the native Azure OpenAI route; everything else -> Foundry.
    AZURE_OPENAI_PREFIXES = ("gpt", "o1", "o3", "o4", "text-", "whisper")
    #: How an image content block is shaped on the foundry route: "anthropic" sends a
    #: base64 ``image`` block, anything else an OpenAI ``image_url`` data URL. Confirm with
    #: scripts/manual_testing/12_apim_foundry_image.py before changing it.
    FOUNDRY_IMAGE_FORMAT = "anthropic"
    #: Body params a 400 may never shed: dropping either turns a bad request into a
    #: meaningless one (an image rejection naming `messages` would drop the conversation).
    UNSHEDDABLE_PARAMS = ("model", "messages")

    # Usage-attribution headers accepted by the gateway.
    TRACKING_KEYS = ("x-rok-user-id", "x-rok-user-email", "x-rok-agent-id",
                     "x-rok-agent-name", "x-rok-agent-version",
                     # Correlation: ties a gateway log line to our request/run/stage/attempt.
                     "x-correlation-id", "x-ms-client-request-id")


class ModelConstants:
    """Per-node model selection over the gateway. Change a value to route that node to a
    different model; the gateway route is inferred from the model name. Start: all gpt-4o."""
    DEFAULT = "gpt-4o"
    TRIAGE =  "claude-sonnet-5-5"
    PLANNER =  "claude-sonnet-5-5"
    RESEARCHER =  "claude-sonnet-5-5"
    WRITER = "gpt-4o"
    CRITIC = "claude-sonnet-5-5"
    TRANSLATOR = "gpt-4o"
    SUMMARIZER =  "claude-sonnet-5-5"
    EMBEDDING="text-embedding-3-large"
    REACT_AGENT = "claude-sonnet-5-5"
    # Writes the python-pptx program for one slide; needs the strongest coding model,
    # because every repair round costs a full extra generation.
    PPT_CODER = "claude-opus-5-5"
    # Looks at a rendered slide image and judges it (max mode). Needs vision.
    SLIDE_REVIEWER = CRITIC


class AgentConstants:
    RAG_MAX_ROUNDS = 2
    RAG_TOP_K_PER_QUERY = 7
    WORKFLOW_MAX_ITERATIONS = 5
    REACT_RECURSION_LIMIT = 25
    # Plan tools read-modify-write one stored plan; parallel calls lose each other's edits.
    PLANNER_TOOL_CONCURRENCY = 1
    # A 30+ slide plan is ~9k output tokens; 4000 truncated it into "Unterminated string".
    PLAN_MAX_TOKENS = 12000


class AgentNames:
    """Stable technical agent-node names used as the analytics ``agent_name`` key.

    These are contract values for the analytics tables and dashboard; renaming one
    splits its history, so treat them as append-only.
    """
    TRIAGE = "triage"
    PLANNER = "planner"
    SLIDE_CONTENT = "slide_content"     # per-slide content agent (ppt_agent)
    SLIDE_DESIGN = "slide_design"       # per-slide python-pptx coder (ppt_creator_agent)
    TRANSLATOR = "translator"
    WRITER = "writer"
    INGESTION = "ingestion"             # document extract/summarize/index pipeline
    SLIDE_REVIEW = "slide_review"       # max-mode visual judge of one rendered slide
    GUARDRAILS = "guardrails"           # content-safety screening call
    REPLY_LANGUAGE = "reply_language"   # localises fixed chat-reply text
    SKILL_DECK = "skill_deck"           # whole-deck DeckBuilder program (skill deck agent)
    DECK_POLISH = "deck_polish"         # whole-deck review and polish after the build

class SlideAgentConstants:
    """Per-slide content agent budgets — see docs/slide-agent-pipeline.md.

    These exist so one stubborn slide can never burn the turn: every tool checks its
    own round counter and refuses politely once the budget is spent.
    """
    MAX_RESEARCH_ROUNDS = 2         # retrieval passes per slide
    TOP_K_PER_QUERY = 7
    MAX_EXCERPTS_PER_SLIDE = 10      # excerpts shown to the model, not what we keep
    MAX_EXCERPT_CHARS = 1000
    MAX_WORKERS = 4                 # slides are independent; 1 forces sequential
    RECURSION_LIMIT = 50            # per-slide tool-calling loop cap
    MAX_IMAGES_PER_SLIDE = 2        # distinct image_refs one slide may carry
    MAX_IMAGE_CANDIDATES = 3        # relevant images the writer hands one slide's designer
    MAX_IMAGE_ASSETS_OFFERED = 6    # retrieved images the writer chooses candidates from
    IMAGE_DESCRIPTION_CHARS = 400   # caption length shown for each offered image

    # Ops a SlideTask may carry; ordered strongest-first for precedence resolution.
    OP_CREATE, OP_ENRICH, OP_REWRITE = "create", "enrich", "rewrite"
    OP_RELAYOUT, OP_KEEP, OP_REMOVE = "relayout", "keep", "remove"
    OP_PRECEDENCE = (OP_CREATE, OP_ENRICH, OP_REWRITE, OP_RELAYOUT, OP_KEEP)
    OPS = OP_PRECEDENCE + (OP_REMOVE,)

    #: Role slides written from the deck's content outline (its content slide titles),
    #: so they are rebuilt only when that outline changes.
    OUTLINE_ROLE_KINDS = ("agenda", "takeaways")

class ApprovalStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PlanStatus:
    NONE = "none"
    DRAFT = "draft"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    CONFIRMED = "confirmed"


class DeckVersionStatus:
    GENERATED = "generated"


class SessionTitleConstants:
    """Static validation limits for a project (session) title."""

    MAX_LENGTH = 200


class DeckFeedbackConstants:
    """Static validation limits for feedback on a generated deck version."""

    COMMENT_MAX_LENGTH = 1000


class ReferenceDesignConstants:
    """Reference design mode: the deck is built on the DESIGN of a PowerPoint the user
    uploaded (its layouts, backgrounds, artwork, colours and fonts) - never its text.

    The uploaded deck is claimed as the design reference when the user's message asks
    for it; from then on it is kept out of the content (retrieval, planning) and the
    designer node builds every slide on its layouts instead of the brand template.
    """
    #: A message asks to copy a design when a copy word is followed closely by a design
    #: word ("copy the design", "use the same template", "match its look and feel") and
    #: it points at an uploaded deck. "Style" alone is not enough: "make the attached
    #: deck's content into a modern style" is a content request.
    DESIGN_WORDS = (r"design", r"template", r"theme", r"look(?:\s+and\s+feel)?",
                    r"layouts?")
    MAX_GAP_CHARS = 40                      # between the copy word and the design word
    REFERENCE_WORDS = (r"reference", r"uploaded", r"attached",
                       r"(?:this|that|my)\s+template",
                       r"this\s+(?:ppt|pptx|deck|presentation|file)",
                       r"the\s+(?:ppt|pptx)", r"my\s+(?:ppt|pptx|deck)")
    COPY_WORDS = (r"cop(?:y|ies|ying)", r"replicat", r"mirror", r"match", r"same",
                  r"follow", r"use", r"using", r"based\s+on", r"keep")
    #: Going back to the brand template.
    DROP_PATTERN = (r"\b(?:stop|don'?t|do\s+not|no\s+longer)\s+(?:using|use|copy(?:ing)?)"
                    r"\s+the\s+(?:reference|uploaded)\s+(?:design|template|deck)|"
                    r"\b(?:back\s+to|use)\s+the\s+rockwell\s+(?:design|template)")
    #: "make it like this deck", "in the style of the attached presentation".
    LIKE_PATTERN = (r"\b(?:like|similar\s+to|in\s+the\s+(?:style|design)\s+of)\s+"
                    r"(?:this|that|the|my)\s+(?:attached\s+|uploaded\s+|reference\s+)?"
                    r"(?:ppt|pptx|deck|presentation|template|reference|one)\b")
    #: The uploaded deck is the content, not a design: it is not claimed (or is
    #: dropped). "summarise the attached PPT", "use the deck as content".
    CONTENT_PATTERN = (
        r"\b(?:summari[sz]e|translate|convert|update|improve|edit|rework|shorten|"
        r"expand|content\s+(?:from|of)|text\s+(?:from|of))\s+(?:the\s+|this\s+|my\s+|"
        r"that\s+)?(?:attached\s+|uploaded\s+)?(?:ppt|pptx|deck|presentation|slides)\b|"
        r"\buse\s+(?:the\s+|this\s+|my\s+)?(?:attached\s+|uploaded\s+)?"
        r"(?:ppt|pptx|deck|presentation|slides)\s+as\s+(?:the\s+)?(?:content|source)")
    EXTENSIONS = (".pptx", ".potx")
    #: Prepared copies (slides stripped, layouts kept), one per reference file content.
    CACHE_DIRNAME = "se_reference_designs"
    MAX_BYTES = 60 * 1024 * 1024
    MIN_SLIDE_IN = 4.0                      # a narrower or shorter canvas is not a deck
    #: Added to every slide's design direction while the mode is on.
    GUIDANCE = (
        "REFERENCE DESIGN MODE (the user's own choice): the template you build on is the "
        "user's reference presentation, not the Rockwell template. Copy its design "
        "faithfully: build on its layouts and fill their placeholders as they are; keep "
        "its backgrounds, artwork, colours (every colour token now resolves to its theme) "
        "and fonts. Prefer layout mode; draw on canvas only when no layout can hold the "
        "content, and then echo the reference's own style (its shapes, spacing, title "
        "treatment). Do not add red/navy brand accents, cards, bars or decorations the "
        "reference does not use. Never copy any wording from the reference - every word "
        "comes from this slide's content. Never draw or place a logo: the Rockwell logo "
        "is already part of the layouts.")

    # --- Rockwell's logo on the reference (app.genai.ppt.rebrand) ------------------- #
    #: Bump when the preparation changes, so copies prepared by an older build are
    #: not reused from the cache.
    PREPARED_VERSION = 5                    # 5: cache keyed on the file, not ""
    #: A picture is a logo candidate when it is this small (inches / share of slide).
    LOGO_MIN_SIDE_IN = 0.15
    LOGO_MAX_SIDE_IN = 3.6
    LOGO_MAX_AREA_SHARE = 0.04
    #: ...and an unrecognised one is another company's logo when its centre is this
    #: close to a corner, it sits on a master, or it repeats on this many layouts.
    LOGO_CORNER_X_IN = 2.5
    LOGO_CORNER_Y_IN = 1.6
    LOGO_MIN_REPEATS = 3
    #: Perceptual match against Rockwell's marks: a FINGERPRINT_SIZE^2-bit difference
    #: hash. Rockwell variants measured 3-28 bits apart, other logos 98+.
    FINGERPRINT_SIZE = 16
    MATCH_MAX_BITS = 40
    MAX_ASPECT_DRIFT = 1.3
    #: Replacement marks, under PptConstants.LOGO_DIR. A box at least this wide for its
    #: height takes the full lockup; a compact one the RA hexagon.
    LOCKUP_MIN_ASPECT = 2.0
    HEXAGON = "ra_mark.png"
    LOCKUP_COLOR = "ra_lockup_color.png"
    LOCKUP_WHITE = "ra_lockup_white.png"
    #: A layout with no logo gets the hexagon at this width, this far from the top
    #: corner (left first, then right).
    ADDED_MARK_WIDTH_IN = 0.6
    ADDED_MARK_MARGIN_IN = 0.3
    #: The lockup, when one is added (in the footer band when both top corners are
    #: taken, or in white over a dark background or photo), at this height; in the
    #: footer band it sits this far left of the slide number.
    ADDED_LOCKUP_HEIGHT_IN = 0.28
    FOOTER_MARK_GAP_IN = 0.15
    #: A shape covering this share of the slide is a background, not an obstacle; a
    #: picture or text-free shape covering BACKDROP_AREA_SHARE is a photo area a logo
    #: may sit on.
    BACKGROUND_AREA_SHARE = 0.8
    BACKDROP_AREA_SHARE = 0.15
    MARK_SHAPE_NAME = "Rockwell logo"


class PptConstants:
    """Company brand-template deck rendering."""
    TEMPLATE_DIR = "company-ppt-templet"            # relative to project root
    LOGO_DIR = "logos"                              # approved brand marks, under TEMPLATE_DIR
    DEFAULT_CLASSIFICATION = "internal"
    CLASSIFICATIONS = ("public", "internal", "confidential", "restricted", "cobranded")

    # Languages a deck can be published in. English is the original; every other
    # language is produced by translating the finished English draft.
    DEFAULT_LANGUAGE = "English"
    SUPPORTED_LANGUAGES = ("English", "French", "Spanish", "Portuguese",
                           "Mandarin", "Japanese", "German")
    FONT_NAME = "Barlow"
    TITLE_FONT_SIZE_PT = 28
    HEADING_FONT_SIZE_PT = 20
    BODY_FONT_SIZE_PT = 15
    SHAPE_FONT_SIZE_PT = 12

    # The one brand template. Decision 2: no .potx is read, and no globbing decides
    # which file we open — a missing file must fail loudly, not resolve to something else.
    TEMPLATE_FILENAME = "SAMPLE.pptx"
    # The brand pack nests this folder inside itself on disk; that is not a typo here.
    CLASSIFICATION_ICON_DIR = "ppt-Use-Policy-icons/ppt-Use-Policy-icons"
    CLASSIFICATION_ICON_FILENAME = "{label}_Symbol.png"

class PptCreatorConstants:
    """The code-generating slide designer: budgets, sandbox and skill wiring.

    One slide == one generated python-pptx program, executed locally. Every value here
    bounds that loop: a stubborn slide can cost at most MAX_CODE_FIX_ATTEMPTS creative
    generations plus MAX_LAYOUT_PLAN_ATTEMPTS layout-fitting generations and
    EXEC_TIMEOUT_SECONDS of CPU per execution before it falls back to a deterministic
    render on a template layout.
    """
    # Brand guide handed to the model as its system prompt, relative to app/genai/ppt.
    SKILL_DIR = "skills"
    SKILL_MD_FILENAME = "rockwell-skill.md"

    # Anthropic-style container skill + code-execution tool, as exposed by the gateway.
    SKILL_TYPE = "anthropic"
    SKILL_ID = "pptx"
    SKILL_VERSION = "latest"
    CODE_EXECUTION_TOOL_TYPE = "code_execution_20260521"
    CODE_EXECUTION_TOOL_NAME = "code_execution"
    # Models the gateway workspace refuses the code-execution tool for ("code_execution
    # not supported in your workspace"). Tier 1 asks them for a plain fenced program
    # instead. Add a model here when it starts failing tier 1 with that 400; check with
    # scripts/manual_testing/11_apim_probe.py.
    CODE_EXECUTION_UNSUPPORTED_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5"})
    # Response block that carries the generated file, and the key holding its source.
    CODE_BLOCK_TYPE = "server_tool_use"
    CODE_BLOCK_NAMES = ("text_editor_code_execution", "code_execution")
    CODE_BLOCK_TEXT_KEY = "file_text"

    # Attach the generic Anthropic `pptx` container skill to tier 1. Off: its plain
    # python-pptx guidance competes with the brand guide and the DeckBuilder-only rules.
    ATTACH_PPTX_SKILL = False
    # One extra tier-1 repair for a slide that passes the gate but carries clutter or
    # overflow advisories; the earlier passing slide ships if the polish fails.
    POLISH_ADVISORIES = True

    MAX_TOKENS = 16000
    TEMPERATURE = 0.7
    # The rendering ladder. Tier 1 writes code creatively; Tier 2 fits the content into
    # a real layout from a validated plan; Tier 3 renders deterministically. A stubborn
    # slide therefore costs at most MAX_CODE_FIX_ATTEMPTS + MAX_LAYOUT_PLAN_ATTEMPTS
    # generations before it is rendered without a model at all.
    MAX_CODE_FIX_ATTEMPTS = 3       # tier 1 generations: 1 first try + repairs
    MAX_LAYOUT_PLAN_ATTEMPTS = 3    # tier 2 plan generations: 1 first try + repairs
    # A gateway call that comes back with no program (or no plan) is a transport
    # failure, not a design failure: there is nothing to repair and nothing to learn
    # from. Charging it to the design budget silently burns a slide's whole allowance
    # on an outage, which is how slides reached the deterministic renderer having never
    # once been drawn. It gets its own small budget instead, per tier.
    MAX_UPSTREAM_RETRIES = 2
    # Layouts tier 2 may choose between: the contract's assignment plus alternates from
    # the same master. More choice costs prompt tokens and invites deck incoherence.
    LAYOUT_PLAN_CANDIDATES = 6
    LAYOUT_PLAN_MAX_TOKENS = 8000   # a plan is JSON, not a program; it needs less room
    # Tier 2 stages its own sandbox beside tier 1's, so a program from the abandoned
    # tier can never be mistaken for tier 2's output.
    LAYOUT_PLAN_DIRNAME = "fit"
    LAYOUT_PLAN_CODE_FILENAME = "fit_slide.py"
    EXEC_TIMEOUT_SECONDS = 180      # a slide program that runs longer is hung
    MAX_STDERR_CHARS = 4000         # error text fed back to the model on a repair
    MAX_WORKERS = 5                 # slides generate independently; 1 forces sequential

    WORKDIR_PREFIX = "ppt_creator_"
    GENERATED_CODE_FILENAME = "make_slide.py"
    SLIDE_FILENAME = "slide_{slide_number}.pptx"
    # Sandbox literals the model's Linux-oriented code assumes; rewritten to the slide dir.
    SANDBOX_PATH_PREFIXES = ("/tmp/", "/mnt/user-data/outputs/", "/mnt/outputs/")

    # 16:9 at 13.333in x 7.5in, in EMU — every generated slide and the stitched deck.
    SLIDE_WIDTH_EMU = 12192000
    SLIDE_HEIGHT_EMU = 6858000

    # Per-slide .pptx cache, so an unchanged slide is never regenerated.
    SLIDE_CACHE_PATH = "{session_id}/slide_cache/slide_{slide_number}_{content_hash}.pptx"
    DECK_FILENAME = "{slug}_v{deck_version}_{language}.pptx"

    #: Send `cache_control` on the system turn. Needs a gateway that forwards the field
    #: to Anthropic. Harmless to leave False if yours strips or rejects it.
    PROMPT_CACHE_ENABLED = True

    #: Attach the brand marks as images as well as paths. Debug aid only — the generated
    #: program places them by path, so the pixels buy nothing on a normal turn.
    SHOW_LOGO_IMAGES = False

    # --- deck-level family layouts (QUIZ / FAQ) ---------------------------- #
    #: Case-insensitive fragments of a SAMPLE.pptx layout NAME that mark it as the
    #: predefined layout for a slide family. Adding a layout whose name matches is all
    #: it takes for the agent to start using it.
    FAMILY_LAYOUT_NAME_PATTERNS = {
        "QUIZ": ("quiz", "knowledge check"),
        "FAQ": ("faq", "frequently asked", "q&a"),
    }
    #: A family layout whose name contains this fragment is the answer-revealed twin of
    #: another ("Quiz-1-Answered" for "Quiz-1"); it is never picked as a family's base.
    ANSWERED_LAYOUT_SUFFIX = "answered"
    #: Sub-directory an answer slide is re-staged into beside its question slide.
    ANSWER_DIRNAME = "answer"
    #: How often a slide waiting for its family's leader re-checks for a stopped run.
    FAMILY_LOCK_POLL_SECONDS = 2.0
    #: An exemplar program longer than this is not inlined into follower prompts; the
    #: followers then rely on the family's layout key and description alone.
    MAX_EXEMPLAR_CODE_CHARS = 16000

    # --- max mode: render-and-review tier for HIGH-creativity slides ------- #
    #: Visual reviews (judge calls) one slide may cost before the best candidate ships.
    MAX_VISUAL_REVIEWS = 3
    #: Designer generations inside max mode, guard/gate repairs included.
    MAX_MODE_GENERATIONS = 6
    #: Wall-clock budget for one slide's max mode; the best candidate so far ships.
    MAX_MODE_WALL_SECONDS = 600
    #: Send the rendered PNG to the designer with the critique on a redo.
    MAX_MODE_RENDER_TO_DESIGNER = True
    #: File-name prefix of a kept max-mode candidate ("max_2_slide.pptx").
    MAX_MODE_KEEP_PREFIX = "max"
    #: Slide kinds that move onto the High Creativity master in max mode. Cover, agenda
    #: and thank-you slides keep their purpose-built layouts.
    MAX_MODE_HIGH_MASTER_KINDS = ("content", "takeaways")


class HighCreativityConstants:
    """The dedicated SAMPLE.pptx master that max-mode slides are designed on."""
    #: Fragment of the master's THEME name that identifies it (case-insensitive).
    MASTER_THEME_FRAGMENT = "high creativity"
    #: Baked text on its layouts that is brand furniture, not sample copy.
    FURNITURE_TEXT_PATTERNS = ("copyright", "\u2039#\u203a", "<#>", "internal",
                               "confidential", "public", "restricted")
    #: Prompt text that must never be baked into a layout (it would show on every slide).
    PROMPT_TEXT_PATTERNS = ("click to edit", "click to add", "lorem ipsum")
    #: Gap between the logo band and the free region of a placeholder-less layout.
    BLANK_TOP_GAP_IN = 0.15


class SlideRenderConstants:
    """Headless LibreOffice rendering of one slide to a PNG (max mode)."""
    SOFFICE_NAMES = ("soffice", "libreoffice")
    # soffice.com waits for the conversion; soffice.exe may return before it is done.
    WINDOWS_DEFAULT_PATHS = (r"C:\Program Files\LibreOffice\program\soffice.com",
                             r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
                             r"C:\Program Files\LibreOffice\program\soffice.exe")
    # Passed through on Windows only: soffice crashes without them. None carry secrets.
    WINDOWS_SYSTEM_VARS = ("WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "PROGRAMDATA",
                           "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
                           "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS")
    # Under Claude's resize threshold: about 1.5k image tokens per render.
    WIDTH_PX = 1400
    HEIGHT_PX = 788
    # A slot's first render also creates its profile (over a minute on a dev laptop).
    TIMEOUT_SECONDS = 180
    #: Concurrent soffice processes; each holds its own profile (about 250 MB RAM).
    MAX_CONCURRENCY = 2
    SLOT_POLL_SECONDS = 2.0
    PROFILE_DIRNAME = "se_agent_soffice"
    RENDER_DIRNAME = "review"
    #: Consecutive failures after which the renderer is off for this process.
    FAILURES_TO_DISABLE = 3
    MIN_PNG_BYTES = 2000
    MAX_STDERR_CHARS = 1500


class SlideReviewConstants:
    """The max-mode visual judge: pass bar and output bounds."""
    PASS_SCORE = 9
    SEVERITIES = ("blocking", "major", "minor")
    FAILING_SEVERITIES = ("blocking", "major")
    MAX_TOKENS = 3000
    MAX_ISSUES_FED_BACK = 12
    MAX_FIELD_CHARS = 300


class SlideTypes:
    """The semantic type of one slide, as handed to the PPT Creator Agent."""
    NORMAL = "NORMAL"
    QUIZ = "QUIZ"
    FAQ = "FAQ"
    DEFAULT = NORMAL
    ALL = (NORMAL, QUIZ, FAQ)
    # Types whose slides must share ONE layout per deck.
    FAMILIES = (QUIZ, FAQ)


class CreativityLevels:
    """How much visual freedom the PPT Creator Agent has on one slide.

    Every level stays inside the Rockwell brand: palette tokens, theme fonts, the type
    scale, and the stamped frame (logo, footer, page number, classification label).

    A fourth level that drops the Rockwell styling entirely ("HIGHEST": any colour, any
    font, no brand layout) is possible, but it would mean relaxing the hex refusal in
    `deck_builder.to_rgb`, the hex check in `code_guard` and geometry rules 8 and 9. It
    is deliberately not offered right now; callers that send it get HIGH.
    """
    LOW = "LOW"      # adhere closely to the template's layouts and patterns
    MED = "MED"      # the agent chooses, within Rockwell design patterns
    HIGH = "HIGH"    # maximum layout and composition freedom, still on brand
    DEFAULT = MED
    ALL = (LOW, MED, HIGH)
    # Values accepted for forward compatibility and mapped onto a supported level.
    ALIASES = {"HIGHEST": HIGH, "MEDIUM": MED}


class QuizRoles:
    """Which half of a quiz pair a QUIZ slide is."""
    NONE = ""
    QUESTION = "question"     # asks the question; the answer stays off the slide
    ANSWER = "answer"         # the same slide with the correct choice revealed
    ALL = (NONE, QUESTION, ANSWER)


class AnswerPlacements:
    """Where a quiz question's answer is revealed. It is always in the notes too."""
    SLIDE = "slide"           # a duplicate answer slide follows each question
    NOTES = "notes"           # speaker notes only (a hard slide cap is in force)
    ALL = (SLIDE, NOTES)


class QuizConstants:
    """Quiz slide content and the deterministic answer reveal."""
    MIN_OPTIONS = 2
    MAX_OPTIONS = 6
    OPTION_LABELS = ("A", "B", "C", "D", "E", "F")
    #: Brand tokens the revealed (correct) choice is drawn with. Taken from SAMPLE.pptx
    #: "Quiz-N-Answered": the card is accent6 at 60% luminance + 40% offset with an
    #: accent6 outline. `deck_builder.brand_palette` derives `green_light` the same way.
    ANSWER_FILL_TOKEN = "green_light"
    ANSWER_LINE_TOKEN = "green"
    #: Prefix of the speaker-notes line that always carries a question's answer.
    NOTES_ANSWER_PREFIX = "Answer:"
    #: A numbered quiz label leading a slide title ("Quiz 3: ...", "Question #2 - ...").
    #: Group 1 is the label, which is kept; the number is replaced by the question's
    #: position in the deck, so moving quiz slides never leaves them numbered 3, 2, 5.
    TITLE_NUMBER_PATTERN = r"^(\s*(?:quiz|question|knowledge check)\s*#?\s*)\d+"


class FaqConstants:
    """FAQ planning bounds. The planner may exceed them only when the user asks."""
    MAX_SLIDES = 3
    #: Q&A pairs per FAQ slide (min, max).
    PAIRS_PER_SLIDE = (3, 5)


class PlannerConstants:
    """Bounds on what the planner writes per slide."""
    #: `planner_special_instructions` longer than this is cut, so an over-eager plan
    #: cannot flood every downstream prompt.
    MAX_SPECIAL_INSTRUCTIONS_CHARS = 1000
    #: `SlidePlan.user_instructions` (the user's own asks for one slide) longer than this
    #: is cut; it reaches the writer, the designer and the slide-cache key.
    MAX_USER_INSTRUCTIONS_CHARS = 600
    #: A family look (`DeckPlan.family_styles`) longer than this is cut: it reaches every
    #: QUIZ/FAQ design prompt and the slide-cache key.
    MAX_FAMILY_STYLE_CHARS = 2000
    #: Output tokens budgeted per slide in a `revise_slides` call. Every targeted slide is
    #: echoed back in full (key points, content needs, instructions of up to
    #: MAX_SPECIAL_INSTRUCTIONS_CHARS), so a fixed budget truncates multi-slide edits and
    #: forces costly regenerations; ~1,350 tokens per slide was observed for quiz slides.
    REVISION_TOKENS_PER_SLIDE = 1500
    #: Floor for a revision's output budget, so a one-slide edit keeps its old budget.
    REVISION_MIN_TOKENS = 2000
    #: Planner tool arguments that are internal and never streamed to the client.
    REDACTED_TOOL_ARGS = ("planner_special_instructions",)
    REDACTED_PLACEHOLDER = "(internal)"


class QuestionIds:
    """Stable ids of the structured questions sent to the UI.

    The UI renders `creativity_level` and `number_of_slides` as sliders, so those ids
    are a contract with the frontend: never rename them.
    """
    GOAL = "goal"
    AUDIENCE = "audience"
    DECK_TYPE = "deck_type"
    REQUIREMENTS = "requirements"
    CREATIVITY_LEVEL = "creativity_level"
    NUMBER_OF_SLIDES = "number_of_slides"
    QUIZ_ANSWER_PLACEMENT = "quiz_answer_placement"
    ALL = (GOAL, AUDIENCE, DECK_TYPE, REQUIREMENTS, CREATIVITY_LEVEL, NUMBER_OF_SLIDES,
           QUIZ_ANSWER_PLACEMENT)
    #: Answers to these ids are a closed set of values; the rest are free text.
    SCALES = (CREATIVITY_LEVEL, NUMBER_OF_SLIDES)
    #: The scales that are deck *style* (a change restyles the deck without the planner).
    #: The slide count is a scale but not style: changing it means replanning.
    STYLE = (CREATIVITY_LEVEL,)
    #: Ids no longer asked but still accepted on `/invoke` and ignored, so a frontend
    #: that has not caught up never gets a 400 ("detailing_level" was removed).
    RETIRED = ("detailing_level",)
    #: The longest free-text answer accepted on `/invoke`.
    MAX_ANSWER_CHARS = 500


class SourceMaterialConstants:
    """Limits for source material the user typed or pasted into the chat.

    With no uploaded document, this material is the only thing a deck may be grounded
    in, so it is stored verbatim on the brief and shown to the planner and writers.
    """
    #: The most material kept on the brief across turns.
    MAX_STORED_CHARS = 40000
    #: The most material shown to the planner.
    MAX_PLANNER_CHARS = 20000
    #: The most material shown to each per-slide content agent.
    MAX_SLIDE_PROMPT_CHARS = 12000


class GuardrailConstants:
    """Budgets and safe user-facing messages for content-policy enforcement."""

    MAX_TOKENS = 500
    MAX_GROUNDING_CHARS = 40000
    SOURCE_BATCH_CHUNKS = 8
    UNAVAILABLE_MESSAGE = (
        "Content screening is unavailable. Generation has been paused; please retry.")
    MESSAGES = {
        "direct_prompt_injection": (
            "Request blocked: instructions to override this assistant's role or "
            "source-grounding rules are not allowed. I cannot act as Wikipedia or "
            "use general knowledge instead of your source material."),
        "indirect_prompt_injection": (
            "Generation paused: source material contains instructions attempting "
            "to control the assistant. Remove those instructions or upload a clean "
            "source before continuing."),
        "unsupported_topic": (
            "Topic not supported by the available source material. This request "
            "has not been applied to the plan and generation is paused. Upload "
            "documents covering the topic or provide factual source content."),
    }


class QuestionTypes:
    """How the UI renders a structured question."""
    SINGLE_CHOICE = "single_choice"   # chips, plus free text when allowed
    FREE_TEXT = "free_text"           # an input box
    SCALE = "scale"                   # an ordered slider (LOW -> HIGH)
    ALL = (SINGLE_CHOICE, FREE_TEXT, SCALE)


class SlideCountConstants:
    """The deck-size limits."""
    #: A user may ask for at most this many planned slides (answer slides excluded).
    MAX_REQUESTED = 50
    #: The most planned slides the agent chooses on its own ("20+", "you decide", unset).
    MAX_AGENT_DECIDED = 35
    #: No stored plan may exceed this many slides, quiz answer slides included.
    MAX_PLAN_SLIDES = 100


class EventTypes:
    """SSE event discriminators added for structured clarification."""
    QUESTIONS = "questions"
    GUARDRAIL = "guardrail"


class GeometryConstants:
    """The geometry gate's thresholds, the grid, and the closed set of font sizes.

    These are the numbers that turn "no overlapping text" from an aspiration into a
    decidable property. Every one of them is a rectangle tolerance in inches, so a
    change here changes what the gate accepts — not merely how it reports.
    """
    # Rule 1: how far inside the canvas every shape must sit, and the slack allowed.
    CANVAS_MARGIN_IN = 0.30
    CANVAS_TOLERANCE_IN = 0.02
    # Rules 2-6: hairline abutments are not collisions. Matches the slack the retired
    # filler.py::_overlaps_in used, so the new gate is no stricter than the old check.
    OVERLAP_TOLERANCE_IN = 0.08
    # Rules 3-4: "fully contained" allows this much bleed before it counts as partial.
    CONTAINMENT_TOLERANCE_IN = 0.02

    # The frame. The footer band runs full width along the bottom of the canvas.
    FOOTER_BAND_HEIGHT_IN = 0.45
    CLASSIFICATION_SYMBOL_IN = 0.22       # square, vertically centred in the footer band
    CLASSIFICATION_LABEL_WIDTH_IN = 1.60
    FRAME_SIDE_MARGIN_IN = 0.42
    LOGO_WIDTH_IN = 0.79                  # d2r_mark at its approved width

    # The grid the design contract hands every slide, over the content region.
    GRID_COLUMNS = 12
    GRID_ROWS = 6
    GRID_GUTTER_IN = 0.15

    # canvas mode: a layout with less free height than this is not worth composing into.
    MIN_CANVAS_FREE_HEIGHT_IN = 4.0
    # Gap left under a title placeholder before the free region starts.
    CANVAS_TITLE_GAP_IN = 0.10
    # A decor shape covering at least this much of a layout's free region is the panel
    # that region sits ON - a card, a hero band, a tinted content area - not an
    # obstacle in it. You compose inside such a shape; routing around it is impossible
    # by definition, because it IS the composable area.
    #
    # This number is load-bearing. Reserving these panels while still advertising the
    # region they cover made every shape a canvas slide drew a blocking rule 5
    # violation, so the only slide that could pass the gate was an empty one - which
    # is exactly what shipped. See `catalog._canvas_region` and
    # `deck_builder.reserved_rects`, which must agree on this test.
    CANVAS_PANEL_COVERAGE = 0.90

    # Rule 9. Placeholder-inherited sizes are exempt; these are the sizes a component
    # may set explicitly. Derived from PptConstants + the brand guide's component specs.
    TYPE_SCALE = (10, 11, 12, 14, 15, 20, 28, 32)

    # Rule 10 is advisory until the estimate exceeds capacity by this factor. Components
    # now fit their own text, so what remains past the blocking ratio is real overflow.
    OVERFLOW_ADVISORY_RATIO = 1.05
    OVERFLOW_BLOCKING_RATIO = 1.25
    # Bold glyphs are wider; the estimator scales a bold run's width by this factor.
    # Mirrored in deck_builder._BOLD_WIDTH_FACTOR (the sandbox copy cannot import this).
    BOLD_WIDTH_FACTOR = 1.05

    # Rule 12: text at least this long (after whitespace normalisation) may not appear
    # in two text shapes of one slide.
    DUPLICATE_TEXT_MIN_CHARS = 12
    # Rule 14: a picture whose shown aspect differs from the image's own by more than
    # this fraction is distorted.
    IMAGE_ASPECT_TOLERANCE = 0.05
    # Clutter advisory: decorative components beyond max(this, content components).
    CLUTTER_MIN_DECORATIVE = 3


class SkillDeckConstants:
    """The skill deck agent (app/genai/skill_deck): the app's ppt skill plus our
    whole-deck skill (and the session's style skill) in, ONE DeckBuilder program for the
    whole deck out, run locally on the official template (SAMPLE.pptx).

    Chosen per deployment with the DECK_ENGINE setting; DeckEngines.CLASSIC is the
    per-slide designer and stays the default. Both engines share the deck design
    contract (master, layouts, frame, grid, tokens), DeckBuilder and the code guard.
    """
    # Custom skills: one folder per skill under this directory (relative to
    # app/genai/skill_deck), each holding a SKILL.md with name/description frontmatter.
    SKILLS_DIRNAME = "skills"
    SKILL_FILENAME = "SKILL.md"
    DEFAULT_SKILLS = ("whole-deck",)
    # The app's own ppt skill (app/genai/ppt/skills/rockwell-skill.md) always goes first.
    PPT_SKILL_NAME = "rockwell-ppt"
    # A session's active style skill (app/genai/skills) is passed as one more skill.
    STYLE_SKILL_NAME = "session-style"
    STYLE_SKILL_DESCRIPTION = ("The look the user picked for this deck from a reference. "
                               "Style only: the ppt skill wins on any conflict.")

    # The app's own deck-code model, so the gateway already serves it.
    MODEL = ModelConstants.PPT_CODER
    MAX_TOKENS = 16000
    CODE_BLOCK_TYPE = "server_tool_use"
    CODE_BLOCK_NAMES = ("text_editor_code_execution", "code_execution")
    CODE_BLOCK_TEXT_KEY = "file_text"
    # A reply only counts as the program when it imports one of these.
    PROGRAM_MARKERS = ("deck_builder", "deck_kit")

    MAX_FIX_ATTEMPTS = 3            # 1 first try + 2 repairs
    EXEC_TIMEOUT_SECONDS = 240      # a whole deck, not one slide
    MAX_ERROR_CHARS = 4000
    CODE_FILENAME = "make_deck.py"
    RAW_DECK_FILENAME = "deck_raw.pptx"
    RUN_DIRNAME = "skill_deck"
    KIT_FILENAME = "deck_kit.py"
    SANDBOX_PATH_PREFIXES = ("/mnt/user-data/outputs/", "/mnt/outputs/", "/tmp/")
    MAX_BUILDER_WARNINGS = 15       # DeckBuilder warnings sent back in one fix round
    # DeckBuilder warnings about the environment, not the program: the model cannot fix
    # them, so they never trigger a fix round.
    IGNORED_BUILDER_WARNINGS = ("no policy symbol", "chart axis styling skipped")
    # Slide types whose slides share ONE design in a deck (the classic "families").
    FAMILY_FIELDS = ("quiz", "faq")

    # Shapes stamped by the v1/v2 branding component. v3 builds on the template, which
    # carries the logos, but the checks and the scorecard still recognise older decks.
    WORDMARK_SHAPE_NAME = "RA Wordmark"
    BADGE_SHAPE_NAME = "RA Badge"
    LABEL_SHAPE_NAME = "RA Classification"
    LOGO_ZONE_IN = (1.3, 0.85)              # (width, height) from the top-right corner

    # Layout check (runs on every version of the deck; see layout_check.py).
    MAX_LAYOUT_FIXES = 1                    # rounds where layout/style fixes go back to the model
    LAYOUT_TOLERANCE_IN = 0.05
    LAYOUT_SIDE_MARGIN_IN = 0.5             # the skill asks for 0.6; 0.1 in of slack
    LAYOUT_BOTTOM_STRIP_IN = 0.7            # keep the bottom 0.7 in free for the label
    LAYOUT_COVER_LOGO_ZONE_IN = (2.3, 1.2)  # (width, height) from the top-left corner, slide 1
    LAYOUT_DEFAULT_FONT_PT = 18
    LAYOUT_CHAR_EM = 0.52                   # average character width / font size
    LAYOUT_BOLD_CHAR_EM = 0.6
    LAYOUT_LINE_SPACING = 1.2
    # The width estimate is an average over characters, so a line may come out up to
    # this share longer than the box before it counts as wrapping onto a second line.
    LAYOUT_WRAP_SLACK = 0.08
    LAYOUT_OVERFLOW_RATIO = 1.15            # text may need up to 15% more than its box
    LAYOUT_MAX_PROBLEMS = 25
    LAYOUT_MIN_CARD_IN = 0.3                # thinner shapes are bars/lines, not cards
    LAYOUT_MIN_OVERLAP_SQIN = 0.01          # smaller shared areas are touching edges
    LAYOUT_TEXT_OVERLAP_SHARE = 0.25        # text overlap that matters (share of smaller box)
    LAYOUT_CARD_GAP_IN = 0.15               # gap the model is asked to keep between cards

    # Style match (style_match.py): the deck against the reference profile(s) of the
    # session's active style skill. Brand rules clamp the targets.
    STYLE_FIX_BELOW = 0.75                  # a measure scoring below this goes back to the model
    STYLE_GRID = (64, 36)                   # cells used to measure how much of a slide is covered
    STYLE_COVERAGE_SPAN = 0.5               # coverage difference that scores 0
    STYLE_TITLE_PT_RANGE = (24, 40)         # brand: slide titles 28 pt, cover 40 pt
    STYLE_BODY_PT_RANGE = (14, 20)          # brand: body 16-20 pt, never below 12 pt
    STYLE_READ_PT_RANGE = (6, 100)          # sizes outside this are misread values
    STYLE_BACKGROUND_SHARE = 0.9            # shapes covering more of the slide are backgrounds

    # Deck scorecard (scorecard.py): one 0-100 score per area, for any .pptx.
    SCORE_WEIGHTS = {"layout": 30, "content": 25, "brand": 20, "readability": 15,
                     "style": 10}
    SCORE_MIN_FONT_PT = 12                  # comfortable reading size on a projected slide
    SCORE_FLOOR_FONT_PT = 10                # the ppt skill's type-scale floor: never smaller
    SCORE_MAX_WORDS = 90                    # words on one slide (speaker notes not counted)
    SCORE_DUPLICATE_MIN_CHARS = 12          # shorter repeated text is a label, not content
    SCORE_SPELLING_PENALTY = 20             # points lost per UK spelling
    # Template furniture the classic designer stamps (classification label/symbol, slide
    # number): placed by the template, so never scored as content.
    FRAME_SHAPE_PREFIXES = ("frame:",)
    SCORE_LOGO_MAX_SHARE = 0.5              # a layout picture larger than this is a background
    SCORE_MAX_DETAILS = 5
    BRAND_FONT_PREFIXES = ("Barlow",)
    # The ppt skill's Brand Palette (rockwell-skill.md), by token name.
    BRAND_PALETTE = {"red": "CD163F", "red_dark": "A8112F", "navy": "1B2A4A",
                     "blue": "003E7E", "orange": "F58024", "light_blue": "00AEEF",
                     "yellow": "F9C20A", "green": "77C14C", "body": "5A6470",
                     "caption": "8A929E", "border": "D8DBDF", "surface": "F6F7F8",
                     "white": "FFFFFF", "black": "000000", "red_light": "FEF2F2"}
    BRAND_COLORS = frozenset(BRAND_PALETTE.values())
    # Lighter tints of the theme accents are on brand too: DeckBuilder's quiz answer
    # card is green at 40% (ADDA94), as in the template's "Quiz-N-Answered".
    BRAND_TINT_BASES = ("CD163F", "1B2A4A", "003E7E", "F58024", "00AEEF", "F9C20A",
                        "77C14C")
    BRAND_TINT_STEPS = (0.2, 0.4, 0.6, 0.8)


class DeckEngines:
    """Values of the DECK_ENGINE setting."""
    CLASSIC = "classic"             # per-slide designer + stitcher (default)
    SKILL_DECK = "skill_deck"       # whole deck in one program by the skill deck agent
    ALL = (CLASSIC, SKILL_DECK)


class DeckPolishConstants:
    """The deck polish agent (app/genai/deck_polish): reviews the FINISHED deck from
    either engine and makes it look better, as far as the deck's creativity level allows.

    LOW fixes defects by rules only (no model call); MEDIUM and HIGH add one model round
    that answers with edit operations from a fixed menu, which code validates and applies.
    The deck is scored before and after, and a round that makes it worse is thrown away.
    Switched on with the DECK_POLISH setting (off by default).
    """
    OFF = "off"
    FOLLOW = "on"                   # use the creativity level the user picked
    SETTING_LEVELS = {"low": "LOW", "medium": "MED", "med": "MED", "high": "HIGH"}
    SKILLS_DIRNAME = "skills"
    SKILL_FILENAME = "SKILL.md"
    LEVEL_SKILLS = {"LOW": "polish-low", "MED": "polish-medium", "HIGH": "polish-high"}
    LEVEL_LABELS = {"LOW": "Low", "MED": "Medium", "HIGH": "High"}
    # Sections of the app's ppt skill (rockwell-skill.md) the model receives.
    PPT_SKILL_SECTIONS = ("Brand Palette", "Type Scale", "Geometry Rules",
                          "Common Mistakes to Avoid")

    # The app's own reviewing model, so the gateway already serves it.
    MODEL = ModelConstants.CRITIC
    MAX_TOKENS = 8000
    WORK_DIRNAME = "polish"
    OUTPUT_FILENAME = "deck_polished.pptx"
    MAX_OPS = 40                    # operations applied from one model answer
    MAX_TEXT_CHARS = 240            # text of one shape shown to the model
    MAX_ISSUES_SHOWN = 40

    # Operations the model may use, per level (rules run at every level).
    OPS_BY_LEVEL = {
        "LOW": (),
        "MED": ("move", "resize", "font_size", "shorten", "to_cards", "to_steps", "stat"),
        "HIGH": ("move", "resize", "font_size", "shorten", "to_cards", "to_steps", "stat",
                 "to_timeline", "divider"),
    }
    # Rules that change a slide's arrangement run from MEDIUM up; LOW keeps the layout.
    ARRANGE_RULES_FROM = ("MED", "HIGH")

    # Checks
    FLOOR_PT = 10                   # the ppt skill's type-scale floor
    READ_PT = 12                    # MEDIUM+ raises smaller component text to this if it fits
    MAX_PT = 44
    MAX_WORDS = 90                  # words on one slide (notes not counted)
    EDGE_MARGIN_IN = 0.3            # geometry rule 1
    FOOTER_BAND_IN = 0.45           # the stamped frame lives in the bottom band
    GAP_SHARE = 0.3                 # an empty band taller than this share of the body is a gap
    HEADING_MAX_WORDS = 6
    HEADING_MAX_GAP_IN = 0.8        # a heading this close BELOW a text block labels it
    HEADING_GAP_IN = 0.1            # gap kept between a lifted heading and its block
    CLASSIFICATION_WORDS = ("internal", "confidential", "public", "restricted",
                            "co-branded", "cobranded")
    FRAME_LABEL_NAME = "frame:classification-label"
    FRAME_SYMBOL_NAME = "frame:classification-symbol"
    FRAME_NUMBER_NAME = "frame:slide-number"
    SECTION_LAYOUT_WORDS = ("section", "divider", "chapter")

    # What added shapes look like (brand palette hex; the model never writes colours).
    SHAPE_PREFIX = "polish:"
    CARD_GAP_IN = 0.2
    CARD_INSET_IN = 0.15
    CARD_ACCENT_IN = 0.06
    # Body text of cards and steps: the largest size on this ladder at which every item
    # fits (one size for the whole group); titles are 2 pt larger and bold.
    TEXT_LADDER = (22, 20, 18, 16, 14, 12)
    MIN_CARD_WIDTH_IN = 1.6
    STEP_DIAMETER_IN = 0.55
    STEP_NUMBER_PT = 16
    STAT_VALUE_PT = 32
    STAT_LABEL_PT = 12
    LIST_MIN_ITEMS = 2
    LIST_MAX_ITEMS = 6


class TranslateConstants:
    """Deck translation is split per-slide so each prompt payload stays small."""
    MAX_WORKERS = 4                 # slides translate independently; 1 forces sequential
    MAX_TEXTS_PER_SLIDE = 120       # text runs sent in one translate call
    MAX_TOKENS = 4000