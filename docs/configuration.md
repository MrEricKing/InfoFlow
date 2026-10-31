# Configuration And Runtime Constants

This document owns current environment settings, persisted application settings, runtime constants, frontend configuration, and precedence rules. Version metadata is owned by project package files and release history, not by this document.

## Backend Settings

Backend settings are defined in `backend/app/core/config.py`. Static process settings use the precedence `process environment > env files > code defaults`. Env files are loaded from:

- `backend/.env`
- project-root `.env`

DB-backed `app_settings` only override the runtime settings they explicitly own, such as storage roots, RSSHub base URL, AI profile configuration, and Insight Engine controls. Dedicated AI API key environment variables (`INFOFLOW_AI_GENERAL_API_KEY` and `INFOFLOW_AI_SENSITIVE_API_KEY`) remain the highest-priority API key source and override encrypted DB-backed key values.

| Setting field | Expected env var | Default | Purpose |
| --- | --- | --- | --- |
| `app_name` | `APP_NAME` | `InfoFlowAggregator API` | Service name in health response and OpenAPI metadata |
| `environment` | `ENVIRONMENT` | `development` | Runtime environment label |
| `allow_unauthenticated_production_api` | `INFOFLOW_ALLOW_UNAUTHENTICATED_PRODUCTION_API` | `false` | Required as `true` when `ENVIRONMENT=production`; acknowledges that this personal API has no app-layer authentication and must be protected by external access control. Production also disables FastAPI OpenAPI/docs routes. |
| `api_prefix` | `API_PREFIX` | `/api` | Prefix for all API routes and OpenAPI docs |
| `database_url` | `DATABASE_URL` | local PostgreSQL URL | SQLAlchemy database connection |
| `cors_origins` | `CORS_ORIGINS` | `http://localhost:5173` | Allowed frontend origins, comma-separated when set as env string. `*` is rejected when `ENVIRONMENT=production`. |
| `static_files_dir` | `STATIC_FILES_DIR` | unset | Optional directory containing the built Vite SPA that FastAPI serves in production |
| `rsshub_base_url` | `RSSHUB_BASE_URL` | `http://localhost:1200` | Default RSSHub base URL for resolving canonical `rsshub://route/path` sources; DB `app_settings` can override it |
| `knowledge_base_root` | `KNOWLEDGE_BASE_ROOT` | project `KnowledgeBase/` | Default Local Mirror root; DB `app_settings` can override it |
| `crawl4ai_version` | `CRAWL4AI_VERSION` | `0.8.6` | Diagnostic/runtime version label |
| `singlefile_version` | `SINGLEFILE_VERSION` | `1.22.98` | Diagnostic/runtime version label for the vendored SingleFile MV3 extension |
| `singlefile_extension_path` | `SINGLEFILE_EXTENSION_PATH` | project `vendor/singlefile-mv3` | Unpacked SingleFile MV3 extension directory loaded by Playwright persistent Chromium |
| `singlefile_extension_id` | `SINGLEFILE_EXTENSION_ID` | `jkeljiefjnokjkioahopjglagkbocpbh` | Fixed extension id expected by the external message bridge |
| `singlefile_browser_executable_path` | `SINGLEFILE_BROWSER_EXECUTABLE_PATH` | unset | Optional Playwright Chromium executable override; Docker sets this to `/usr/local/bin/playwright-chromium` |
| `singlefile_capture_timeout_seconds` | `SINGLEFILE_CAPTURE_TIMEOUT_SECONDS` | `120` | Overall SingleFile extension capture timeout in seconds |
| `settings_encryption_key` | `INFOFLOW_SETTINGS_ENCRYPTION_KEY` | unset | Fernet key used to encrypt AI API keys before saving them in `app_settings`; required before saving new AI keys through Settings and required in production when plaintext legacy AI key settings exist |
| `ai_general_api_key` | `INFOFLOW_AI_GENERAL_API_KEY` | unset | Optional environment override for the `general` AI profile API key; takes precedence over encrypted DB-backed app settings |
| `ai_sensitive_api_key` | `INFOFLOW_AI_SENSITIVE_API_KEY` | unset | Optional environment override for the `sensitive` AI profile API key; takes precedence over encrypted DB-backed app settings |

`CORS_ORIGINS` is the formal environment variable. The development helper still accepts `BACKEND_CORS_ORIGINS` as a compatibility fallback for older local `.env` files, but new templates should use `CORS_ORIGINS`.

RSS short-content extraction does not add a new environment variable. Its default implementation reuses existing `httpx`, adds `readability-lxml` as the main reader HTML extractor, and keeps `trafilatura` as the fallback extractor. Crawl4AI version settings remain capture/archive diagnostics, not feed-sync extractor selection.

AI provider settings store provider configuration in DB-backed `app_settings` rows managed by `/api/settings/ai`; defaults are used only when no DB value exists. The app has two fixed AI profiles, `general` and `sensitive`, selected per source by `sources.ai_route`. `general` defaults to the explicit `deepseek` provider, while `sensitive` defaults to `openai-compatible`. API keys saved through Settings are encrypted with `INFOFLOW_SETTINGS_ENCRYPTION_KEY` and returned only as configured/masked status. `INFOFLOW_AI_GENERAL_API_KEY` and `INFOFLOW_AI_SENSITIVE_API_KEY` are optional process-environment overrides that take precedence over encrypted DB-backed key settings. Startup validates encrypted AI settings and, when an encryption key is present, upgrades existing plaintext AI key settings to encrypted values. DeepSeek profiles require an API key before real provider calls. OpenAI-compatible and LM Studio profiles may omit the API key; the backend sends an `Authorization` header only when a non-blank key is configured. LM Studio is an explicit provider value, not an OpenAI-compatible alias, and its default UI base/model are `http://localhost:1234` and `qwen/qwen3.5-9b`. Each AI profile also owns DB-backed `artifact_concurrency`, used only for artifact/saved-insight provider job throttling. Defaults are DeepSeek `8`, OpenAI-compatible `1`, and LM Studio `1`; accepted backend range is `1..32`, while sqlite/test job claiming remains effectively single-slot. `/api/settings/ai/test` may receive unsaved candidate profile fields for a one-off connection test; those values do not create new settings keys and are not persisted. DeepSeek `429` / `503` retry behavior is a provider runtime constant, not a user setting.

Insight Engine settings add DB-backed `app_settings` keys but no environment variables. `/api/settings/insight-engine` manages `auto_low_priority_archiving_enabled` and `low_priority_score_threshold`; defaults are `false` and `50`. `/api/settings/ai/insight-request-preview` uses saved settings and the shared Insight Engine request builder to show a sanitized request preview with active taxonomy/content type/reason-code contract context, slim active insight profile context, active channel context, active source channel tendencies, and source flow policy; it also adds no settings keys or environment variables. `/api/settings/insight-engine/coverage` is a read-only projection over existing feed item, artifact, latest attempt, source route, provider setting, and extraction-status rows; it adds no settings keys, tables, caches, or environment variables. Failed/skipped feed item insight diagnostics are stored in `insight_engine_attempts`, not in settings. AI artifact concurrency belongs to `/api/settings/ai` profile settings, not the Insight Engine settings panel and not frontend localStorage.

Insight channels and source routing do not add environment variables or `app_settings` keys. Channel definitions live in `insight_channels`, catalog version state lives in `insight_channel_catalog_state`, source-level channel hints live in `source_channel_tendencies`, and source flow policy lives in `sources.flow_policy`. The default channels, including locked `needs_review`, are data rows seeded when missing, not constants exposed as environment configuration.

App operations and jobs do not add environment variables or `app_settings` keys. Operation lifecycle state is stored in `app_operations`, and job-backed operation work is stored in `jobs` / `job_events`. The backend runner id is generated in process memory and is still used for legacy same-process operations; active job-backed operations with active child jobs are recovered from job state instead of being marked interrupted by the stale-runner sweep. Operation create APIs wake the in-process worker after commit and do not run route-level job drains; stop APIs write stop intent and rely on the worker for job cancellation and terminal fan-in. AI lane scheduling is an in-process worker behavior over the same tables, not a new configurable service or environment variable.

Insight Profile v1 does not add environment variables or `app_settings` keys. It uses `insight_profiles` and `insight_profile_events` for user-created/imported profile versions and events, plus a code-level read-only default fallback when no database profile is active.

Insight Evidence Package v2 does not add environment variables or `app_settings` keys. It is a read-only Settings export with source constants for package version, supported ranges, and artifact modes. Profile-candidate inspect/apply also adds no settings keys.

Backend Scoring v1 does not add environment variables, score tables, or artifact kinds. Code-level `FEATURE_SCHEMA_VERSION = insight_feature_v1` and `SCORE_VERSION = score_v1_hand_weighted` identify grouped feature/score breakdown metadata in feed item ranking artifacts. Homepage priority remains derived logic over existing PostgreSQL rows, code-level contracts, and DB-backed Insight Engine settings for automatic low-priority bucketing.

Saved Item Insight v1 does not add environment variables or `app_settings` keys. It uses existing AI profile settings, stores saved item route selection in `saved_items.ai_route`, writes AI generated saved item artifacts to `insight_engine_artifacts.saved_item_id`, and stores user curated revisions/events in dedicated saved item insight tables.

Feed item structured feedback, channel/routing corrections, seen-state cleanup, original-link reading, not-interested undo, and auto low-priority correction do not add environment variables. Structured feedback and channel/routing corrections are stored in `feed_item_structured_feedback`, free-form notes remain in `feed_items.insight_feedback_text`, `seen` is stored in `item_states.seen_at`, original-link opens set `read_full_at` and record `opened_original_link`, not-interested undo writes neutral behavior events, and auto low-priority correction writes `priority_correction = too_low` plus `auto_low_priority_cleared` without changing AI provider settings or Insight Profile rows.

## Deployment Environment Defaults

The deployment bundle in `deploy/` uses these container-oriented defaults:

| Variable | Deployment default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL service URL under compose | Points the app container at the bundled `postgres:16` service. |
| `INFOFLOW_ALLOW_UNAUTHENTICATED_PRODUCTION_API` | `true` placeholder in `deploy/.env.example` | Required for `ENVIRONMENT=production`; do not expose the app outside localhost/LAN without an external auth/access-control layer such as Cloudflare Access, a VPN, or a trusted reverse proxy policy. |
| `CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` style deployment origins in `deploy/.env.example` | Comma-separated origins are supported. Add the Cloudflare Access hostname, for example `https://infoflow.example.com`, before publishing outside localhost. Do not use `*` with `ENVIRONMENT=production`. |
| `RSSHUB_BASE_URL` | `http://rsshub:1200` | Uses the compose service name so RSSHub routes resolve inside the Docker network. |
| `RSSHUB_HOST_BIND` | `127.0.0.1` | Host-side bind address for direct RSSHub browser debugging; defaults to loopback only. |
| `RSSHUB_PORT` | `1200` | Host-side RSSHub debug port mapped to the RSSHub container's port 1200. |
| `INFOFLOW_SETTINGS_ENCRYPTION_KEY` | required placeholder in `deploy/.env.example` | Fernet key required by the official compose file before app startup so AI API keys can be stored encrypted. |
| `INFOFLOW_AI_GENERAL_API_KEY` | blank optional override | Optional general-profile provider key override. When set, it takes precedence over the encrypted DB setting. |
| `INFOFLOW_AI_SENSITIVE_API_KEY` | blank optional override | Optional sensitive-profile provider key override. When set, it takes precedence over the encrypted DB setting. |
| `HOST_KNOWLEDGE_BASE_ROOT` | `./KnowledgeBase` | Host-side bind mount source for container `/data/KnowledgeBase`; can be an absolute host path. |
| `CACHE_TYPE` | `redis` | Passed to RSSHub; enables Redis-backed RSSHub route/content cache. |
| `CACHE_EXPIRE` | `120` | Passed to RSSHub; route cache expiry in seconds. |
| `CACHE_CONTENT_EXPIRE` | `1800` | Passed to RSSHub; content cache expiry in seconds. |
| `REDIS_URL` | `redis://redis:6379/` | Passed to RSSHub; points to the bundled compose Redis service. |
| `TWITTER_AUTH_TOKEN` | placeholder in `deploy/.env.example` | RSSHub Twitter route credential; replace in deployment `.env` before using Twitter routes. This is currently one of the only route credentials passed through by the official compose file. |
| `YOUTUBE_KEY` | placeholder in `deploy/.env.example` | RSSHub YouTube route API key; replace in deployment `.env` before using YouTube routes. This is currently one of the only route credentials passed through by the official compose file. |
| `KNOWLEDGE_BASE_ROOT` | `/data/KnowledgeBase` | Mounted app data volume path for the rebuildable Local Mirror. |
| `STATIC_FILES_DIR` | image-internal Vite build path | Set by the Docker image so FastAPI can serve the built SPA. |
| `SINGLEFILE_EXTENSION_PATH` | `/app/vendor/singlefile-mv3` | Image-internal unpacked SingleFile MV3 extension path used by URL capture. |
| `SINGLEFILE_EXTENSION_ID` | `jkeljiefjnokjkioahopjglagkbocpbh` | Fixed extension id for the vendored automation runtime. |
| `SINGLEFILE_BROWSER_EXECUTABLE_PATH` | `/usr/local/bin/playwright-chromium` | Image-internal Chromium executable used by the extension wrapper. |
| `SINGLEFILE_CAPTURE_TIMEOUT_SECONDS` | `120` | Optional capture timeout override for long SingleFile extension runs. |

RSSHub environment variables use RSSHub's exact upstream names. `deploy/.env.example` includes the Redis cache defaults and common route credentials. If a future route needs another RSSHub variable, add it to the deployment `.env` and list that key under `rsshub.environment` in `deploy/docker-compose.yml` so it is passed through intentionally.

`deploy/docker-compose_RSSHub_example.yml` is a user-provided untracked reference file and may contain stale settings or sensitive fields. It is not an official deployment asset and should not be used as the source for supported variable documentation.

The deployment compose file mounts the host KnowledgeBase directory to a fixed container path. `HOST_KNOWLEDGE_BASE_ROOT` is used only by Docker Compose; `KNOWLEDGE_BASE_ROOT` is the container-visible app path. DB-backed storage path overrides should use `/data/KnowledgeBase` or another path that is actually mounted into the container. `llm_wiki/` is only a reserved future workspace in the repository, not a current deployment variable or app runtime setting.

The application uses one backend process with in-process operation and AI workers over PostgreSQL job tables. These workers do not add a Compose service, Redis/Celery dependency, or external worker role. `backend/requirements-runtime.txt` is a Docker build dependency input, not an environment template or runtime setting. The runtime image uses Playwright Chromium, Xvfb, CJK/emoji fonts, and the vendored SingleFile MV3 extension; the removed SingleFile CLI variable is not supported.

## Backend Runtime Constants

| Constant | File | Meaning |
| --- | --- | --- |
| `ENCRYPTED_VALUE_PREFIX = enc:v1:` | `backend/app/core/security.py` | Prefix for encrypted app-setting secrets. |
| `MAX_OPERATION_PAYLOAD_BYTES = 16 * 1024` | `backend/app/core/security.py` | Maximum persisted operation/job payload size before validation fails. |
| `SENSITIVE_KEY_FRAGMENTS`, `LARGE_CONTENT_KEY_FRAGMENTS`, and secret text patterns | `backend/app/core/security.py` | Redaction and validation rules used before storing operation/future-job payloads or provider diagnostic previews. |
| `OPML_UPLOAD_MAX_BYTES = 10 * 1024 * 1024` | `backend/app/core/safety.py` | OPML file payload limit for preview/import endpoints. |
| `FEED_RESPONSE_MAX_BYTES = 20 * 1024 * 1024` | `backend/app/core/safety.py` | Maximum feed response size accepted by preview/sync fetches. |
| `OPML_IMPORT_ENTRY_MAX_COUNT = 5000` | `backend/app/core/safety.py` | Maximum OPML candidate entries accepted by preview/import endpoints. Oversized files fail clearly instead of partial import. |
| `FEED_ENTRY_MAX_COUNT = 2000` | `backend/app/core/safety.py` | Maximum RSS/Atom entries accepted by feed preview/sync parsing. Oversized feeds fail clearly instead of partial sync. |
| `SOURCE_SYNC_ITEM_LIMIT = 50` | `backend/app/services/source_sync.py` | Product-level per-source ingestion cap for source sync. Sync selects the latest 50 entries by published time, with feed order as fallback. This is separate from the parser safety cap above. |
| `CAPTURE_RESPONSE_MAX_BYTES = 50 * 1024 * 1024` | `backend/app/core/safety.py` | Maximum remote/capture response and wrapper output size accepted by capture and feed extraction paths. |
| `PROVIDER_DIAGNOSTIC_PREVIEW_MAX_BYTES = 64 * 1024` | `backend/app/core/safety.py` | Maximum sanitized provider diagnostic body preview stored/exposed for debugging. |
| `MAX_PROFILE_CANDIDATE_JSON_BYTES = 1 * 1024 * 1024` | `backend/app/services/insight_profile_candidates.py` | Maximum external `profile_candidate` JSON size accepted by candidate inspect/source-draft apply. |
| `MAX_CANDIDATE_TEXT_FIELD_BYTES = 4 * 1024` | `backend/app/services/insight_profile_candidates.py` | Maximum UTF-8 size for any single prompt-affecting candidate free-text field. |
| `MAX_CANDIDATE_PROMPT_TEXT_BYTES = 32 * 1024` | `backend/app/services/insight_profile_candidates.py` | Maximum total UTF-8 size for candidate prompt-affecting free text. |
| `DEFAULT_REDIRECT_LIMIT = 5` | `backend/app/core/safety.py` | Redirect limit for SSRF-guarded HTTP fetch helpers. |
| `MULTIPART_BODY_OVERHEAD_MAX_BYTES = 1 * 1024 * 1024` | `backend/app/core/safety.py` | Extra multipart envelope allowance above the logical uploaded file size limit. |
| `SETTING_RSSHUB_BASE_URL = rsshub_base_url` | `backend/app/services/source_resolver.py` | DB-backed setting key for RSSHub base URL overrides. |
| `SETTING_AI_PROVIDER = ai_provider` | `backend/app/services/app_settings.py` | AI profile setting suffix used to form `ai_general_provider` and `ai_sensitive_provider`. Supported provider values are `deepseek`, `openai-compatible`, and `lm-studio`. |
| `SETTING_AI_ENABLED = ai_enabled` | `backend/app/services/app_settings.py` | AI profile enabled-setting suffix used to form `ai_general_enabled` and `ai_sensitive_enabled`. Defaults to disabled. |
| `SETTING_AI_BASE_URL = ai_base_url` | `backend/app/services/app_settings.py` | AI profile base URL suffix used to form `ai_general_base_url` and `ai_sensitive_base_url`. Stored without a trailing `/v1` suffix. |
| `SETTING_AI_MODEL = ai_model` | `backend/app/services/app_settings.py` | AI profile model suffix used to form `ai_general_model` and `ai_sensitive_model`. Defaults are `deepseek-v4-flash` for general and `gpt-4.1-mini` for sensitive; the Settings UI uses `qwen/qwen3.5-9b` when switching a profile to LM Studio unless the user overrides it. |
| `SETTING_AI_API_KEY = ai_api_key` | `backend/app/services/app_settings.py` | AI profile API-key suffix used to form `ai_general_api_key` and `ai_sensitive_api_key`. DeepSeek requires a non-blank key; OpenAI-compatible and LM Studio may omit it. Full values must not be returned through the API. |
| `SETTING_AI_DEEPSEEK_THINKING = ai_deepseek_thinking` | `backend/app/services/app_settings.py` | AI profile DeepSeek thinking-mode suffix used to form `ai_general_deepseek_thinking` and `ai_sensitive_deepseek_thinking`; values are `disabled` or `enabled`, defaulting to `disabled`. |
| `AI_PROFILE_GENERAL = general`, `AI_PROFILE_SENSITIVE = sensitive`, `AI_ROUTE_DISABLED = disabled` | `backend/app/services/app_settings.py` | Fixed source AI routing values used to map `sources.ai_route` to profile settings or skip generation. |
| `SETTING_AUTO_LOW_PRIORITY_ARCHIVING_ENABLED = auto_low_priority_archiving_enabled` | `backend/app/services/app_settings.py` | DB-backed Insight Engine toggle for automatic score-threshold low-priority bucketing. Defaults to `false`. |
| `SETTING_LOW_PRIORITY_SCORE_THRESHOLD = low_priority_score_threshold` | `backend/app/services/app_settings.py` | DB-backed Insight Engine threshold for automatic low-priority bucketing. Defaults to `50` and is constrained to `0..100`. |
| `INSIGHT_ENGINE_PROMPT_VERSION = insight-engine-v2-routing-v1` | `backend/app/services/insight_engine_request_builder.py` | Prompt version written to AI-generated feed item artifacts through the shared Insight Engine request builder. |
| `INSIGHT_ENGINE_TEMPERATURE = 0.2` | `backend/app/services/insight_engine_request_builder.py` | Runtime temperature used by feed item Insight Engine requests. |
| `INSIGHT_ENGINE_MAX_TOKENS = 4096` | `backend/app/services/insight_engine_request_builder.py` | Runtime max-token cap used by feed item Insight Engine requests. |
| `MAX_INSIGHT_ENGINE_INPUT_CHARS = 6000` | `backend/app/services/insight_engine_request_builder.py` | Fail-fast content budget for the normalized `Content` field sent to the Insight Engine prompt. Oversized runtime input raises `content_too_large_for_provider_budget`; it is not silently truncated. |
| `FEATURE_SCHEMA_VERSION = insight_feature_v1` | `backend/app/services/insight_scoring.py` | Grouped Insight feature vector contract version persisted in ranking artifact payloads and used for deterministic score/audit flattening. |
| `SCORE_VERSION = score_v1_hand_weighted` | `backend/app/services/insight_scoring.py` | Manual weighted Backend Scoring v1 policy version persisted with score breakdowns. |
| `TAXONOMY_CONTRACT_VERSION = taxonomy_v1` | `backend/app/services/insight_engine_contracts.py` | Active Insight Engine domain taxonomy contract version. |
| `CONTENT_TYPE_CONTRACT_VERSION = content_type_v1` | `backend/app/services/insight_engine_contracts.py` | Active Insight Engine content-type contract version. |
| `REASON_CODES_CONTRACT_VERSION = reason_codes_v1` | `backend/app/services/insight_engine_contracts.py` | Active Insight Engine reason-code contract version. |
| `TAXONOMY_FALLBACK_ID = uncategorized` | `backend/app/services/insight_engine_contracts.py` | Fallback taxonomy id used when AI output cannot be canonicalized to a known domain category. |
| `CONTENT_TYPE_FALLBACK_ID = unknown` | `backend/app/services/insight_engine_contracts.py` | Fallback content type id used when AI output cannot be canonicalized to a known content type. |
| `AI_ALLOWED_REASON_CODES` | `backend/app/services/insight_engine_contracts.py` | Subset of reason codes the AI is allowed to emit directly; backend/profile-only reason codes are reserved for later scoring phases. |
| `DEFAULT_INSIGHT_CHANNELS` | `backend/app/services/insight_channels.py` | Seed rows for the default channel catalog; missing default ids are inserted but existing rows are not overwritten. |
| `ReadingDepth = light / medium / deep` | `backend/app/schemas/settings.py`, `backend/app/schemas/homepage.py`, `frontend/src/types/feed.ts` | Reading-depth values for channels and homepage routing projection. |
| `SourceFlowPolicy = normal / mixed_high_frequency / high_frequency_brief` | `backend/app/schemas/source.py` | Source-level flow policy values stored in `sources.flow_policy`. |
| `FEED_ITEM_ROUTING_ARTIFACT_KIND = feed_item_routing_v1` | `backend/app/services/insight_engine_artifacts.py` | Current feed item artifact kind for Insight Engine routing output. |
| `INSIGHT_PROFILE_SCHEMA_VERSION = insight_profile_v1` | `backend/app/services/insight_profiles.py` | Active Insight Profile schema version. |
| `MAX_INSIGHT_PROFILE_JSON_BYTES = 32 * 1024` | `backend/app/services/insight_profiles.py` | Maximum normalized profile JSON size accepted by validation and save endpoints. |
| `PROFILE_ACTIVATION_EVENT_TYPES = (activated, rolled_back)` | `backend/app/services/insight_profiles.py` | Event types used to derive the active database profile. |
| `FORBIDDEN_PROFILE_KEY_FRAGMENTS` | `backend/app/services/insight_profiles.py` | Secret-like key fragments rejected anywhere in candidate profile JSON. |
| `EVIDENCE_PACKAGE_VERSION = insight_evidence_package_v2` | `backend/app/services/insight_evidence_package.py` | ZIP export contract version for offline Insight/Profile Candidate update evidence. |
| `DEFAULT_EVIDENCE_PACKAGE_RANGE = 90d` | `backend/app/services/insight_evidence_package.py` | Default Settings evidence package range. |
| `DEFAULT_EVIDENCE_PACKAGE_ARTIFACT_MODE = latest` | `backend/app/services/insight_evidence_package.py` | Default Evidence Package artifact export mode; `all_history` must be selected explicitly. |
| `RANGE_DAY_COUNTS = 7d / 30d / 90d / 180d / 365d` | `backend/app/services/insight_evidence_package.py` | Supported fixed-day evidence package windows; `all` and `custom` are handled separately. |
| `SAVED_ITEM_INSIGHT_ARTIFACT_KIND = saved_item_insight_v1` | `backend/app/services/saved_item_insights.py` | Insight Engine artifact kind for generated saved item insight rows stored with `insight_engine_artifacts.saved_item_id`. |
| `SAVED_ITEM_INSIGHT_PROMPT_VERSION = saved-item-insight-v1` | `backend/app/services/saved_item_insights.py` | Prompt version written to AI generated saved item insight artifacts. |
| `CONNECTION_TEST_RESPONSE_SCHEMA` | `backend/app/services/ai_provider.py` | Minimal JSON Schema used for LM Studio Settings connection tests; expects `{ok: boolean}`. |
| `INSIGHT_ENGINE_RESPONSE_SCHEMA_NAME = feed_item_insight` | `backend/app/services/insight_engine_request_builder.py` | JSON Schema name used when LM Studio generates feed item insight summary/topic/ranking/routing payloads or Settings previews the same request body. |
| `SAVED_ITEM_INSIGHT_RESPONSE_SCHEMA_NAME = saved_item_insight` | `backend/app/services/saved_item_insights.py` | JSON Schema name used when LM Studio rebuilds saved item insights. |
| `DEEPSEEK_TRANSIENT_RETRY_STATUS_CODES = {429, 503}` | `backend/app/services/ai_provider.py` | DeepSeek-only transient HTTP statuses retried by chat-completions requests. |
| `DEEPSEEK_TRANSIENT_RETRY_DELAYS_SECONDS = (2.0, 4.0, 8.0)` | `backend/app/services/ai_provider.py` | Fallback DeepSeek retry waits when `Retry-After` is absent. |
| `MAX_RETRY_AFTER_DELAY_SECONDS = 30.0` | `backend/app/services/ai_provider.py` | Per-wait cap applied to DeepSeek retry sleeps, including parsed `Retry-After`. |
| `SAVED_ITEM_ORIGIN_FEED_SAVED = feed_saved`, `SAVED_ITEM_ORIGIN_MANUAL_URL = manual_url` | `backend/app/services/saved_item_insights.py` | Current saved item origin type values used by migrations, DTOs, and evidence export. |
| `DEFAULT_SAVED_ITEM_AI_ROUTE = sensitive` | `backend/app/services/saved_item_insights.py` | Default AI route for manual URL saved items and legacy saved item route fallback. |
| `EVENT_MARKED_SEEN_FROM_LIST`, `EVENT_MARKED_SEEN_BULK`, `EVENT_SEEN_CLEARED`, `EVENT_OPENED_ORIGINAL_LINK`, `EVENT_STRUCTURED_FEEDBACK_SAVED`, `EVENT_STRUCTURED_FEEDBACK_CLEARED`, `EVENT_NOT_INTERESTED_CLEARED`, `EVENT_AUTO_LOW_PRIORITY_CLEARED` | `backend/app/services/behavior_events.py` | Behavior event types for seen cleanup, external original-link reading, structured feedback save/clear including channel/routing corrections, not-interested undo, and auto low-priority correction evidence. |
| `BACKEND_SCORING_SOURCE = backend_scoring_v1` | `backend/app/services/insight_scoring.py` | Current homepage/evidence priority source marker for read-time Backend Scoring v1. |
| `HOMEPAGE_READ_MODEL_VERSION = homepage_read_model_v1` | `backend/app/services/homepage_read_model.py` | Builder version for derived homepage read-model rows; mismatches mark rows stale and trigger rebuild. |
| `BASE_SCORE = 50` | `backend/app/services/insight_scoring.py` | Starting score for read-time priority calculation before deltas. |
| `DEFAULT_LOW_PRIORITY_SCORE_THRESHOLD = 50` | `backend/app/services/app_settings.py` | Default score threshold for automatic low-priority bucketing when no DB setting exists. |
| `LOW_PRIORITY_REASON_NOT_INTERESTED = not_interested`, `LOW_PRIORITY_REASON_AUTO_SCORE_THRESHOLD = auto_score_threshold` | `backend/app/services/insight_scoring.py` | Reason values exposed on homepage/evidence priority projections. |
| `MAX_REASON_DELTA = 10` | `backend/app/services/insight_scoring.py` | Maximum absolute score delta contributed by one AI/profile weight conversion. |
| `HISTORY_NET_THRESHOLD = 2` | `backend/app/services/insight_scoring.py` | Minimum net positive/negative behavior history before adding source/category history reasons. |
| `HISTORY_REASON_WEIGHT = 8` | `backend/app/services/insight_scoring.py` | Signed score delta for source/category history reasons once the net threshold is met. |
| `PREVIEW_CANDIDATE_LIMIT = 200` | `backend/app/services/insight_request_preview.py` | Maximum recent candidate feed items considered by the Settings Insight Engine request inspector before applying sendability/content/missing-artifact/recency preferences. |
| `FEED_ITEM_INSIGHT_PROMPT_VERSION = INSIGHT_ENGINE_PROMPT_VERSION` | `backend/app/services/insight_engine_artifact_builder.py` | Prompt version written to AI-generated feed item artifacts used by the homepage read model. |
| `MAX_AI_INPUT_CHARS = MAX_INSIGHT_ENGINE_INPUT_CHARS` | `backend/app/services/insight_engine_artifact_builder.py` | Shared Insight Engine content truncation bound. |
| `ATTEMPT_STATUS_FAILED = failed`, `ATTEMPT_STATUS_SKIPPED = skipped` | `backend/app/services/insight_engine_attempts.py` | Persisted statuses for failed/skipped feed item Insight Engine attempt diagnostics. |
| Coverage reason codes such as `source_ai_route_disabled`, `provider_disabled_or_unconfigured`, `low_confidence_content`, `previous_failed_attempt`, `previous_skipped_attempt`, `partial_artifacts`, and `no_attempt_history` | `backend/app/services/insight_engine_coverage.py` | Read-only missing-insight reason labels and retryability decisions for Settings coverage scan results. |
| `DEFAULT_RSSHUB_BASE_URL = http://localhost:1200` | `backend/app/services/source_resolver.py` | Fallback RSSHub base URL when no DB or environment value is set. |
| `KNOWN_RSSHUB_BASE_URLS` | `backend/app/services/source_resolver.py` | Known RSSHub instance bases used to convert full HTTP instance URLs into canonical `rsshub://` route identities during import/preview. |
| `SINGLEFILE_TOOL_NAME = singlefile_extension` | `backend/app/services/capture_tools/singlefile_wrapper.py` | Tool-call name persisted for the SingleFile MV3 extension runtime. |
| `SINGLEFILE_EXTENSION_OPTIONS` | `backend/app/services/capture_tools/singlefile_wrapper.py` | Save options passed through the extension bridge, including background save, overwrite conflict behavior, blocked scripts for stable opened artifacts, deferred image loading, and preservation-oriented output settings. |
| `singlefile_capture_timeout_seconds` | `backend/app/core/config.py`, `backend/app/services/capture_tools/singlefile_wrapper.py` | Overall timeout for Playwright persistent Chromium, page prewarm, external message trigger, download completion, and saved HTML self-validation. |
| `singlefile_precapture.js` path | `backend/app/services/capture_tools/singlefile_wrapper.py` | Injected as an init script before navigation so the page applies common light-theme hints, scrolls, and triggers lazy-loaded content before the extension saves the tab. |
| `CRAWL4AI_PROFILE = crawl4ai_dynamic_markdown_v1` | `backend/app/services/capture_tools/crawl4ai_wrapper.py` | Diagnostic profile label for Crawl4AI markdown capture that preserves dynamic comments and lazy-loaded content. |
| `CRAWL4AI_PAGE_TIMEOUT_MS = 120_000` | `backend/app/services/capture_tools/crawl4ai_wrapper.py` | Browser page timeout for Crawl4AI captures that may need extra time for client-rendered content. |
| `CRAWL4AI_SCROLL_DELAY_SECONDS`, `CRAWL4AI_MAX_SCROLL_STEPS`, `CRAWL4AI_DELAY_BEFORE_RETURN_HTML_SECONDS` | `backend/app/services/capture_tools/crawl4ai_wrapper.py` | Full-page scan and settle timing for Crawl4AI dynamic-content capture. |
| `CRAWL4AI_PRE_CAPTURE_SCRIPT` | `backend/app/services/capture_tools/crawl4ai_wrapper.py` | Browser-side script that scrolls the page, dispatches viewport events, waits for body text stability, and reduces stale `Loading...` placeholders before Crawl4AI returns HTML/markdown. |
| SPA fallback routing | `backend/app/main.py` | When `STATIC_FILES_DIR` is configured, non-`/api/*` routes fall back to `index.html`; unknown `/api/*` routes continue to return API 404s. |
| `SHORT_VISIBLE_TEXT_THRESHOLD = 40` | `backend/app/services/feed_content_extraction.py` | Visible-text threshold for deciding that RSS content is likely too short and may need source-page extraction. |
| `MIN_EXTRACTED_VISIBLE_TEXT_CHARS = 200` | `backend/app/services/feed_content_extraction.py` | Default minimum extracted visible text required before replacing `feed_items.content_text`; explicit short-body quality gates can accept real title-related page bodies below this threshold. |
| `MIN_SHORT_EXTRACTED_VISIBLE_TEXT_CHARS = 60` | `backend/app/services/feed_content_extraction.py` | Lower bound for accepting a short extracted body when it is title-related and looks like prose/CJK/sentence content rather than navigation or a comment form. |
| `MIN_EXTRACTED_TO_FEED_RATIO = 3` | `backend/app/services/feed_content_extraction.py` | Extracted body must be substantially longer than the RSS-derived text before it can replace `content_text`. |
| `SUBSTANTIAL_CJK_TITLE_MISMATCH_BYPASS_CHARS = 1000` / `SUBSTANTIAL_CJK_TITLE_MISMATCH_BYPASS_CJK_CHARS = 200` | `backend/app/services/feed_content_extraction.py` | Allows substantial Chinese article bodies to pass when mixed English title tokens are absent from the body. |
| `MAX_HTML_BYTES = CAPTURE_RESPONSE_MAX_BYTES` | `backend/app/services/feed_content_extraction.py` | Maximum HTML response size accepted by the lightweight feed extractor through the shared SSRF/size-guarded HTTP helper. |
| `AUTO_FAILURE_COOLDOWN = 24 hours` | `backend/app/services/feed_content_extraction.py` | Automatic feed sync skips recent failed extraction attempts during the cooldown only for items that still need source-page extraction; sufficient feed/RSSHub body text can clear the failed state without fetching. Manual extraction can force a rerun. |

These are source constants, not environment variables. Changing Insight Engine contract, profile, scoring, or coverage reason constants should update `backend/tests/test_homepage_api.py`, `backend/tests/test_settings_api.py`, [`api.md`](api.md), [`data.md`](data.md), and relevant operational guidance. Changing capture-related constants affects capture behavior and should update `backend/tests/test_singlefile_wrapper.py`, [`troubleshooting.md`](troubleshooting.md), and [`operations.md`](operations.md) when the operator contract changes.

Crawl4AI capture constants should also stay in sync with `backend/tests/test_crawl4ai_wrapper.py` and the Crawl4AI image reinsertion coverage in `backend/tests/test_knowledge_api.py`.

## Frontend Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `http://localhost:8000/api` | Backend API base URL used by frontend service adapters |
| `FRONTEND_PORT` / `VITE_PORT` | `5173` | Development-server port used by `frontend/vite.config.ts`; root `npm run dev` checks availability before starting Vite |
| `FRONTEND_HOST` / `VITE_HOST` | `0.0.0.0` | Development-server bind host used by `frontend/vite.config.ts` |
| `BACKEND_PORT` / `PORT` | `8000` | Backend development-server port selected by root `npm run dev` and passed to `scripts/run-backend-dev.mjs` |
| `BACKEND_HOST` | `127.0.0.1` | Backend development-server host used by root health checks and backend startup |
| `BACKEND_HEALTH_TIMEOUT_MS` | `120000` | Root `npm run dev` maximum wait for `GET /api/health` before failing startup |

Files reading `VITE_API_BASE_URL`:

- `frontend/src/services/feed-service.ts`
- `frontend/src/services/homepage-service.ts`
- `frontend/src/services/knowledge-service.ts`
- `frontend/src/services/settings-service.ts`
- `frontend/src/services/source-service.ts`

Development server variables are read by `scripts/dev.mjs`, `scripts/run-backend-dev.mjs`, and `frontend/vite.config.ts`.

## Frontend Runtime Constants

| Constant | File | Meaning |
| --- | --- | --- |
| `THEME_STORAGE_KEY` | `frontend/src/hooks/use-info-flow-app.ts` | Local storage key for light/dark theme |
| `LAYOUT_STORAGE_KEY = ifa:layout:pane-widths:v1` | `frontend/src/App.tsx` | Local storage key for sidebar/list pane widths; presentation state only |
| `SOURCE_CATEGORY_STORAGE_KEY = ifa:sidebar:source-categories:v1` | `frontend/src/components/sidebar.tsx` | Local storage key for expanded/collapsed source categories; presentation state only |
| default list `limit` omission | `frontend/src/services/*` | Homepage, Reader, and Knowledge default list requests omit `limit`; explicit limits remain available for bounded debug/future pagination callers |
| `WINDOW_MS` | `frontend/src/lib/date.ts` | Time-window filter durations |
| `WINDOW_OPTIONS` | `frontend/src/components/feed-list-pane.tsx` | UI order for time-window controls |

## Frontend Type Unions

Defined in `frontend/src/types/feed.ts`:

- `ViewMode`: `ai`, `reader`, `kb`, `settings`
- `ItemStatus`: `unread`, `browsed`, `seen`, `read_full`, `saved`, `not_interested`
- `CaptureStatus`: `pending`, `succeeded`, `partial_failed`, `failed`
- `MirrorStatus`: `pending`, `succeeded`, `partial_failed`, `failed`
- Capture/mirror/detail long-operation UI state now prefers backend `AppOperationDto` from `frontend/src/types/source.ts`; legacy client-only capture hints should not be treated as the source of truth for refresh recovery.
- `StructuredFeedbackOverallValue`: `no_value`, `neutral`, `useful`, `very_useful`, `must_read`
- `StructuredFeedbackPriorityCorrection`: `too_low`, `appropriate`, `too_high`
- `StructuredFeedbackIssueMarker`: `misleading_summary`, `wrong_topic`, `wrong_priority`, `thin_content`, `duplicate`, `source_quality`, `extraction_problem`
- `ContentTextSource`: `feed_content`, `feed_summary`, `extracted_page`, `legacy`
- `ContentExtractionStatus`: `not_checked`, `not_needed`, `needs_extraction`, `succeeded`, `failed`, `skipped`
- `ArchiveArtifactKind`: `singlefile_html_v1`, `crawl4ai_raw_markdown_v1`, `crawl4ai_fit_markdown_v1`, `crawl4ai_fit_image_reinserted_markdown_v1`, `page_image_v1`, `uploaded_source_material_v1`, `uploaded_markdown_v1`
- `ArchiveArtifactStatus`: `available`, `unavailable`
- `SavedItemInsightSource`: `ai_generated`, `curated`
- `SavedItemOriginType`: `feed_saved`, `manual_url`, `markdown_upload`, `html_upload`, or `singlefile_html_upload`
- `SavedItemAIRoute`: `general`, `sensitive`, `disabled`
- `FeedItemInsightArtifactMissingCandidateProvider`: `deepseek`, `openai-compatible`, `lm-studio`, `disabled`
- `FeedItemInsightArtifactMissingCandidateProfile`: `general`, `sensitive`, `disabled`
- `LowPriorityBucketReason`: `not_interested`, `auto_score_threshold`
- `ReadingDepth`: `light`, `medium`, `deep`
- `HomepageFlowLane`: `main`, `brief_pool`, `low_priority`, `needs_review`
- `FeedItemInsightRoutingSource`: `insight_engine_artifact`, `deterministic_fallback`, `user_corrected`
- `AIProfile`: `general`, `sensitive`
- `AIProvider`: `deepseek`, `openai-compatible`, `lm-studio`
- `DeepSeekThinking`: `disabled`, `enabled`
- `SourceAIRoute`: `general`, `sensitive`, `disabled`
- `GroupMode`: `time`, `topic`, `source`, `low_priority`
- `SourceScopeKind`: `all_sources`, `source_category`, `source`
- `SortMode`: `latest`, `priority`
- `TimeWindow`: `1h`, `4h`, `6h`, `24h`, `all`
- `ThemeMode`: `light`, `dark`
- Feed item artifact/source unions for summary/topic/priority/routing provenance used by the homepage read model. Current priority provenance includes `backend_scoring_v1`; routing provenance is currently `insight_engine_artifact`, `channel_lifecycle`, `deterministic_fallback`, or `user_corrected`.

Defined in `frontend/src/types/settings.ts`:

- `InsightContentTextPayloadSource`: `content_text`, `summary_text_fallback`, `empty`
- Settings DTOs cover storage paths, RSSHub settings, AI provider settings, Insight Engine settings, Insight Engine attempts, Insight Engine coverage, Insight Evidence Package structured feedback / channel-correction / signal / clear / undo / auto-low-priority-clear counts, and Insight Engine request preview payloads.
- Insight Profile DTOs cover active/default profile state, profile versions, audit events, validation responses, and save/activate/rollback inputs.

Defined in `frontend/src/types/source.ts`:

- `SourceKind`: `rss`, `rsshub`
- `SourceProvenance`: `manual`, `opml_import`
- `SourceFetchStatus`: `pending`, `success`, `not_modified`, `error`
- `SourceSyncStage`: `idle`, `fetching`, `insight`, `stopping`, `stopped`, `completed`
- `SourceFlowPolicy`: `normal`, `mixed_high_frequency`, `high_frequency_brief`
- `AppOperationType`: `source_sync`, `feed_item_insight_artifacts`, `feed_item_insight_artifact`, `knowledge_capture`, `local_mirror_export`, `feed_item_extract`, `saved_item_insight`, and `channel_lifecycle`.
- Read-side feed item artifact operation kinds include internal `source_sync_created_feed_items` for linked source-sync-created feed item artifact build operations. Public create payloads accept `global | current_source | failed_attempts | retryable_coverage`; `profile` and `created_after` are failed-attempt-only filters, while `retryable_coverage.reason_codes` narrows coverage candidates by the reason codes returned from `GET /api/settings/insight-engine/coverage`.
- `AppOperationStatus`: `queued`, `running`, `stopping`, `completed`, `stopped`, `failed`, `interrupted`
- `ImportResultStatus`: `created`, `matched_existing`, `invalid`
- Source preview DTOs include both `canonical_url` and `resolved_url`. For RSSHub, `canonical_url` is the stored `rsshub://route/path` identity and `resolved_url` is the current fetch URL under the effective RSSHub base URL.
- Source sync request options include optional `feedItemInsightArtifactBuildLimit`, mapped to backend `feed_item_insight_artifact_build_limit`; omitted or `null` means all newly created items after the source-sync ingestion cap, `0` disables the follow-up, and a positive integer caps the linked feed item insight artifact item count.
- Source sync progress and created item accounting are reported through `AppOperationDto`; insight progress is shown by the linked `feed_item_insight_artifacts/source_sync_created_feed_items` operation.

## Configuration Boundaries

- Secrets and per-machine paths belong in `.env`, not source code.
- Deployment `.env` values belong beside `deploy/docker-compose.yml`; target machines should not need a repository checkout or local image build for normal updates.
- `app_settings` values override `.env`, and `.env` overrides defaults for storage roots and `rsshub_base_url`.
- `knowledge_base_root` points to the derived Local Mirror. Changing it must not change the canonical store.
- `llm_wiki/` is reserved for future LLM Wiki output, but current runtime configuration does not expose a storage root for it. Local Mirror export must not write there.
- `rsshub_base_url` is the user-editable RSSHub instance base. It must be a valid HTTP/HTTPS base URL without credentials, query, fragment, or traversal path segments.
- `ai_general_base_url` and `ai_sensitive_base_url` are user-editable AI provider bases. They must be valid HTTP/HTTPS URLs without credentials, query, fragment, or traversal path segments; a trailing `/v1` is normalized away. `deepseek` calls `/chat/completions`; `openai-compatible` and `lm-studio` call `/v1/chat/completions`.
- `ai_general_deepseek_thinking` and `ai_sensitive_deepseek_thinking` are DeepSeek-only provider options. The backend sends `thinking: {"type": value}` only when the selected provider is `deepseek`; OpenAI-compatible and LM Studio requests never receive this field.
- `ai_general_api_key` and `ai_sensitive_api_key` are sensitive configuration. Environment overrides win first, then encrypted DB-backed app settings. The settings API exposes only `api_key_configured` and an optional masked value. A blank key is valid only for `openai-compatible` and `lm-studio`, where the provider request omits the `Authorization` header.
- Saved AI API key settings require `INFOFLOW_SETTINGS_ENCRYPTION_KEY`; production startup fails if existing plaintext AI key settings are present without the key.
- LM Studio structured routes use `response_format.type = json_schema` with route-specific schema names: `connection_test`, `feed_item_insight`, and `saved_item_insight`. DeepSeek and generic OpenAI-compatible routes keep `json_object`; DeepSeek and OpenAI-compatible preserve their existing response-format fallback behavior, while LM Studio schema rejection returns a provider error without fallback.
- DeepSeek `429` / `503` retry behavior is provider-specific and must not be generalized to OpenAI-compatible or LM Studio without a separate provider decision.
- `artifact_concurrency` is a DB-backed AI profile setting managed by `/api/settings/ai`. It affects artifact and saved-item insight job claiming only; it must not affect source sync fetch concurrency, create a database column, or become a frontend-only localStorage source of truth again.
- `auto_low_priority_archiving_enabled` and `low_priority_score_threshold` are DB-backed Insight Engine settings. They affect read-time low-priority bucketing only; changing them must not backfill item rows, write Insight Engine artifacts, or mutate structured feedback.
- Insight Engine request previews must use the shared payload builder output without API keys or authorization headers and must not create new app settings or Insight Engine artifacts.
- Insight Engine attempt snapshots must be sanitized. They may include request metadata/body needed for local diagnostics, but not API keys, `Authorization` headers, cookies, or full raw provider response text.
- Insight Profile v1 uses dedicated database tables and a code-level default fallback, not environment variables or `app_settings`. Opening Settings must not silently persist the fallback profile.
- Profile management endpoints must not call AI providers, write `insight_engine_artifacts`, or trigger historical homepage rebuilds.
- Insight Evidence Package preview/export must remain read-only and must not add settings keys, call AI providers, write `insight_engine_artifacts`, or persist profile rows. Candidate inspect is also read-only; candidate source-draft apply may write only selected `source_preference_profiles` drafts with `source_type = codex_import` and must not activate them or mutate channel catalog rows.
- Feed item structured feedback must remain canonical human evidence in `feed_item_structured_feedback`, not an Insight Engine artifact or setting.
- Feed item channel/routing corrections must remain canonical human evidence in `feed_item_structured_feedback`, not an Insight Engine artifact, setting, source routing tendency, or Insight Profile mutation.
- `seen` is stored as `item_states.seen_at` and means title/summary-level processing, not full reading. It should not be treated as stronger than `read_full`, `saved`, or `not_interested`.
- `not_interested_cleared` must remain a neutral undo event; it removes current negative override/history effect but does not create a positive preference signal.
- Saved item insight generation is explicit and must not block save/capture actions. Curated saved item insight revisions must not be written to `insight_engine_artifacts`.
- Saved item insight generation uses `saved_items.ai_route` by default. Feed saves snapshot `sources.ai_route`; manual URL captures default to `sensitive`; rebuild requests may temporarily override the route without mutating the saved item.
- Backend Scoring v1 must remain derived logic over existing rows and explicit Insight Engine settings. Runtime build paths may persist grouped feature vectors and score breakdown metadata inside append-only ranking artifacts for audit, but the service must not create a separate score table, mutate canonical item state, or treat source folders as taxonomy.
- Insight channels and source channel tendencies are explicit routing configuration. They must not be represented as environment variables or `app_settings` without a later configuration-boundary decision.
- Profile `enabled` must be true and provider/model/base URL must be configured before the backend calls that AI provider. DeepSeek also requires a non-blank API key. OpenAI-compatible and LM Studio can be configured without an API key, and in that case the backend omits `Authorization`. Incomplete configuration must skip artifact generation without failing source sync.
- `sources.ai_route = sensitive` must use only the sensitive profile. It must not fall back to the general profile when sensitive is incomplete; `disabled` skips artifact generation entirely.
- RSSHub source identity is not the base URL. Store sources as `rsshub://route/path`; resolve that identity to `{rsshub_base_url}/route/path` only when previewing or syncing.
- RSSHub route credentials are deployment secrets. Do not commit real `TWITTER_AUTH_TOKEN`, `YOUTUBE_KEY`, cookies, or route API keys.
- The compose Redis service is only RSSHub's cache. It does not change PostgreSQL's role as the only canonical InfoFlowAggregator store.
- `STATIC_FILES_DIR` only enables production static serving. It must not change API prefix ownership; `/api/*` remains API-only.
- SingleFile and Crawl4AI version settings are currently runtime labels and diagnostics, not dependency resolvers.
