# Data And State Contracts

## Canonical Entities

| Table/model | Layer | Purpose |
| --- | --- | --- |
| `SourceImportRun` / `source_import_runs` | source raw | OPML import preview/commit batch, raw payload, preview summary, last commit summary |
| `Source` / `sources` | source registry | RSS/RSSHub source definition, folder metadata, AI route, source routing flow policy, and icon metadata |
| `InsightChannel` / `insight_channels` | source routing settings | Configurable Insight Engine channel definitions used for homepage routing |
| `InsightChannelCatalogState` / `insight_channel_catalog_state` | source routing settings | Singleton catalog-version row for optimistic channel writes, routing snapshots, and lifecycle reroute audit |
| `SourceChannelTendency` / `source_channel_tendencies` | source routing settings | Source-level channel tendency hints used by request building and deterministic routing fallback |
| `SourcePreferenceProfile` / `source_preference_profiles` | source preference settings | Optional source-specific preference profile snapshots used as active Source Profile prompt/scoring context |
| `SourceFetch` / `source_fetches` | source raw | One fetch attempt, cache headers, raw response, counters, error |
| `FeedItem` / `feed_items` | feed item | Normalized subscription item plus item-level human insight feedback |
| `FeedItemStructuredFeedback` / `feed_item_structured_feedback` | feed item human evidence | Current structured feedback and channel/routing correction for one feed item, stored outside `insight_engine_artifacts` |
| `SourceItemOccurrence` / `source_item_occurrences` | source raw/feed item bridge | Per-source occurrence of a feed item and match rule |
| `ItemState` / `item_states` | feed item | User reading state for one `feed_item` |
| `FeedItemBehaviorEvent` / `feed_item_behavior_events` | feed item behavior evidence | Append-only behavior events for how one feed item reached current state or feedback history |
| `SavedItem` / `saved_items` | saved/capture canonical | Knowledge base item; may or may not link to a feed item |
| `SavedItemCaptureRun` / `saved_item_capture_runs` | capture process | One explicit capture/retry attempt for a saved item |
| `SavedItemCaptureToolCall` / `saved_item_capture_tool_calls` | capture diagnostics | SingleFile/Crawl4AI invocation record |
| `SavedSnapshot` / `saved_snapshots` | saved snapshot | Textual saved/capture snapshot |
| `SavedItemAsset` / `saved_item_assets` | saved asset | Binary or high-fidelity archive asset bytes |
| `SavedSnapshotAssetRef` / `saved_snapshot_asset_refs` | asset reference | Links markdown snapshot references to stored assets |
| `SavedItemCuratedInsight` / `saved_item_curated_insights` | saved item human insight | Current user-curated saved item insight revision for one saved item |
| `SavedItemCuratedInsightEvent` / `saved_item_curated_insight_events` | saved item insight audit | Append-only create/update/clear events for user-curated saved item insights |
| `MirrorExportBatch` / `mirror_export_batches` | mirror process | Full mirror rebuild batch |
| `SavedItemMirrorExport` / `saved_item_mirror_exports` | mirror process | One saved item local mirror export attempt |
| `SavedItemMirrorExportFile` / `saved_item_mirror_export_files` | mirror diagnostics | Written file locator and verification record |
| `AppSetting` / `app_settings` | app configuration | User-editable runtime settings such as storage roots, encrypted AI API keys, AI profile artifact concurrency, and Insight Engine controls |
| `InsightEngineArtifact` / `insight_engine_artifacts` | AI derived | Recomputable Insight Engine artifact payloads for feed item summary/topic/ranking/routing and saved item insight, with DB owner/kind constraints. The AI homepage reads feed item artifacts; it does not own their artifact kinds. |
| `FeedItemHomepageReadModel` / `feed_item_homepage_read_models` | homepage derived read model | Recomputable per-feed-item homepage card projection with full card JSON plus expanded source, category, state, saved/not-interested, time-window, priority, flow lane, reading-depth, low-priority, search, version, and stale metadata. |
| `FeedItemHomepageFacet` / `feed_item_homepage_facets` | homepage derived read model | Recomputable channel/topic facet rows for one homepage read-model card, used so homepage filters and facet counts do not depend on slow JSON-array scans. |
| `InsightProfile` / `insight_profiles` | insight profile | Immutable user-created/imported Insight Profile v1 versions |
| `InsightProfileEvent` / `insight_profile_events` | insight profile audit | Created, activated, and rolled-back events used to determine the active profile |
| `InsightEngineAttempt` / `insight_engine_attempts` | insight diagnostics | Persisted failed/skipped feed item insight attempts with sanitized request snapshots |
| `AppOperation` / `app_operations` | operation state | Persisted operation state for source sync, feed item missing-artifact fill, capture, mirror export, feed item extraction/artifacts, and saved item insight rebuild |
| `Job` / `jobs` | operation job queue | Postgres-backed child jobs for job-backed operations, including claim/lease/retry/cancel/idempotency state |
| `JobEvent` / `job_events` | operation job audit | Append-only job lifecycle events with small redacted diagnostics |

Insight Evidence Package is a derived ZIP export, not a database table. It reads `feed_items`, `feed_item_structured_feedback`, latest source occurrences, `item_states`, `feed_item_behavior_events`, active saved items, saved snapshots/assets, latest feed-item and saved item `insight_engine_artifacts`, saved item curated insight rows/events, active/default Insight Profile data, channel/routing settings, code-level taxonomy/content-type/reason-code contracts, evidence signal classification, and the read-time Backend Scoring v1 service for current priority fields.

Insight Engine Coverage is a derived Settings projection, not a database table. It reads `feed_items`, latest `source_item_occurrences`, `sources.ai_route`, AI provider settings, latest feed item `insight_engine_artifacts`, latest `insight_engine_attempts`, and feed content extraction status to explain which feed items are missing one or more feed-item artifact kinds and whether a retry action is appropriate.

Homepage bootstrap/summary/items are derived read-model projections over PostgreSQL. `feed_item_homepage_read_models` and `feed_item_homepage_facets` persist rebuildable per-item and facet projections without making those rows canonical. They read canonical feed items, source occurrences, item states, source folders, active channel/routing settings, structured feedback, latest feed-item Insight Engine artifacts, Source Profiles, and Backend Scoring v1 results. They can be deleted and rebuilt. A short-lived process-local homepage snapshot token store keeps sorted feed item ids plus scrolling metadata; it is not persisted, does not store full card DTOs or body content, and does not add Redis or another deployment service.

## Important IDs

| Field | Meaning |
| --- | --- |
| `feed_items.id` | Reader/homepage item identity |
| `saved_items.id` | Knowledge/capture item identity |
| `saved_items.feed_item_id` | Optional link back to source feed item; `null` means manual/non-RSS capture |
| `saved_items.origin_type` | Explicit saved signal origin such as `feed_saved`, `manual_url`, `markdown_upload`, `html_upload`, or `singlefile_html_upload`; future external/import origins should add explicit values |
| `saved_items.ai_route` | Point-in-time AI route for saved item insight generation. Feed saves copy the source route at save time; manual URL and upload captures default to `sensitive` unless explicitly set to `general`. |
| `saved_items.canonical_url` | Nullable canonical web URL. Feed and manual URL captures set it; uploaded materials can leave it null and use source material metadata instead. |
| `saved_items.original_filename` | Nullable original uploaded filename for upload-origin saved items. |
| `saved_items.mime_type` | Nullable MIME type for the uploaded source material. |
| `saved_items.source_asset_id` | Nullable link to the current successful uploaded source material asset. Replacement manual imports stage a new asset first and switch this pointer only after processing succeeds. |
| `saved_items.processing_status` | Upload processing state such as `not_applicable`, `pending`, `succeeded`, or `failed`. |
| `saved_items.processing_error` | Nullable upload processing failure summary. |
| `saved_items.removed_at` | Soft-removal timestamp; non-null saved items are excluded from active knowledge reads |
| `saved_item_assets.asset_key` | Stable app-level asset key used in `ifa-asset://` refs and asset API |
| `saved_snapshots.capture_run_id` | Capture run that produced the snapshot, if any |
| `saved_item_assets.capture_run_id` | Capture run that produced the asset, if any |
| `saved_item_mirror_exports.source_capture_run_id` | Capture run whose artifacts were exported to mirror |
| `saved_item_mirror_export_files.relative_path` | Path of a written mirror file relative to effective `KNOWLEDGE_BASE_ROOT` |
| `saved_item_mirror_export_files.verification_status` | Whether the written file matched expected byte/hash data |
| `app_settings.key` | Setting key, currently storage roots, RSSHub base URL, AI provider configuration including artifact concurrency, or Insight Engine read-time settings |
| `app_settings.value` | Persisted setting value. Storage/RSSHub values override `.env`; AI API key values may be encrypted with `enc:v1:` and can be overridden by dedicated `INFOFLOW_AI_*_API_KEY` environment variables. |
| `insight_channels.id` | Stable channel id used by routing prompts, source tendencies, and `feed_item_routing_v1` payloads |
| `insight_channels.reading_depth` | Default reading-depth hint for a channel: `light`, `medium`, or `deep` |
| `insight_channels.enabled` | Disabled channels stay readable in Settings but are excluded from new runtime request channel context, deterministic fallback, source tendency writes, and human correction writes |
| `insight_channels.is_system` | System-owned channel guard. `needs_review` is system-owned, must remain enabled, and cannot be retired. |
| `insight_channels.retired_at` | Non-null means the channel is retired: historical references remain readable, but new routing/tendency/correction writes must not target it. |
| `insight_channel_catalog_state.channel_catalog_version` | Monotonic catalog-level version incremented on channel create, runtime-semantic edit, enable/disable, and retire. |
| `source_channel_tendencies.source_id` / `channel_id` | Unique source-channel hint pair; source deletion cascades tendencies, while channel removal is retire-only rather than hard delete. |
| `source_channel_tendencies.tendency` | Float `0..1` tendency weight used as source-level routing hint |
| `source_channel_tendencies.is_primary` | At most one primary tendency per source is accepted by API validation |
| `sources.flow_policy` | Source-level flow policy: `normal`, `mixed_high_frequency`, or `high_frequency_brief`; defaults to `normal` |
| `source_preference_profiles.source_id` | Required source owner; rows cascade when the source is deleted |
| `source_preference_profiles.status` | Source Profile lifecycle state: `draft`, `active`, or `archived` |
| `source_preference_profiles.profile_version` | Stable active profile version such as `source-{source_id}-profile-{profile_row_id}`; included in request/enqueue context |
| `source_preference_profiles.prompt_context_json` / `prompt_context_stats_json` | Deterministic rendered runtime prompt context plus render stats; stored profile fields are not silently truncated |
| `insight_profiles.id` | Insight Profile version identity. `null` in API responses means the read-only code default fallback is active. |
| `insight_profile_events.profile_id` | Profile version associated with an audit event |
| `insight_profile_events.event_type` | Profile event kind: `created`, `activated`, or `rolled_back` |
| `insight_engine_attempts.feed_item_id` | Optional feed item whose insight generation attempt failed or was skipped |
| `insight_engine_attempts.source_id` | Optional latest source route context used for the attempt |
| `insight_engine_attempts.status` | Attempt status, currently persisted for failed/skipped diagnostic rows |
| `insight_engine_attempts.request_snapshot_json` | Sanitized request snapshot for debugging; must not contain API keys, authorization headers, or full provider response text |
| `app_operations.operation_type` | Operation family: `source_sync`, `feed_item_insight_artifacts`, `feed_item_insight_artifact`, `knowledge_capture`, `local_mirror_export`, `feed_item_extract`, `saved_item_insight`, or `channel_lifecycle`. v3 starts from a fresh database and does not migrate old operation rows. |
| `app_operations.operation_kind` | Operation scope/kind: `all`, `single`, `auto_create`, `global`, `current_source`, `failed_attempts`, `retryable_coverage`, internal `source_sync_created_feed_items`, `manual_url`, `uploaded_material`, `manual_import`, `retry_capture`, `feed_saved_capture`, `manual_reexport`, `manual_extract`, `rebuild`, `build_missing`, or `retire_channel` |
| `app_operations.status` | Operation lifecycle status: `queued`, `running`, `stopping`, `completed`, `stopped`, `failed`, or `interrupted` |
| `app_operations.source_id` | Optional source associated with a single-source/current-source operation |
| `app_operations.scope_json` | Requested scope and target metadata, including source id, saved item id, feed item id, capture run id, limits, and operation-specific options |
| `app_operations.runner_id` | Same-process runner marker used to detect active operations left behind by a backend restart |
| `app_operations.stop_requested` | Cooperative stop flag written by the public stop API in a short transaction. It does not cancel an in-flight fetch or AI request; worker fan-in owns pending-job cancellation and terminal aggregation. |
| `app_operations.scope_json` / `progress_json` / `result_json` | Requested scope, live progress, and terminal result payloads for operation recovery/diagnostics |
| `insight_engine_artifacts.feed_item_id` | Optional link to a feed item for feed item Insight Engine artifacts such as summary/topic/ranking/routing; saved item insight artifacts use `saved_item_id` instead |
| `insight_engine_artifacts.saved_item_id` | Optional link to a saved item for saved item Insight Engine artifacts such as `saved_item_insight_v1`; current feed-item artifacts use `feed_item_id` instead |
| `saved_item_curated_insights.saved_item_id` | Unique saved item owning the current curated insight revision |
| `saved_item_curated_insights.base_insight_engine_artifact_id` | Optional Insight Engine artifact the user revision was based on |
| `saved_item_curated_insight_events.event_type` | Saved item curated insight event kind: `curated_insight_created`, `curated_insight_updated`, or `curated_insight_cleared` |
| `feed_item_behavior_events.feed_item_id` | Feed item whose behavior event was recorded |
| `feed_item_behavior_events.event_type` | Behavior kind such as `card_opened`, `marked_seen_from_list`, `marked_seen_bulk`, `read_full_in_app`, `read_full_cleared`, `opened_original_link`, `saved`, `unsaved`, `not_interested`, `not_interested_cleared`, `insight_feedback_saved`, `insight_feedback_cleared`, `structured_feedback_saved`, `structured_feedback_cleared`, or `auto_low_priority_cleared` |
| `feed_items.canonical_url` | Nullable normalized item URL identity. Non-null values are protected by PostgreSQL partial unique index `uq_feed_items_canonical_url_not_null`; migration preflight fails on duplicate historical non-null values instead of merging rows silently. |
| `feed_item_structured_feedback.feed_item_id` | Unique feed item owning the current structured feedback row |
| `feed_item_structured_feedback.primary_channel_correction` / `channel_add_ids_json` / `channel_remove_ids_json` / `reading_depth_correction` / `flow_lane_correction` / `channel_correction_notes` | Optional structured channel/routing correction fields for the feed item, including primary channel, secondary-channel additions/removals, reading depth, flow lane, and notes |
| `feed_item_homepage_read_models.feed_item_id` | Unique feed item owning the current derived homepage card projection |
| `feed_item_homepage_read_models.card_json` | Complete current `HomepageCardRead` payload for the feed item, rebuilt from canonical rows and latest artifacts |
| `feed_item_homepage_read_models.read_model_version` | Version marker for the projection builder; version mismatches are stale and should be rebuilt |
| `feed_item_homepage_read_models.rebuilt_at` | Timestamp captured before the canonical read used for this projection; canonical changes newer than this timestamp make the row stale |
| `feed_item_homepage_read_models.is_stale` / `stale_reason` | Explicit stale marker and reason, set when broad source/channel/profile/settings changes require background rebuild |
| `feed_item_homepage_facets.feed_item_id` / `facet_type` / `facet_id` | Rebuildable channel/topic facet membership for one homepage read-model card |
| `sources.icon_url` | External HTTP/HTTPS source icon URL when one is known; `null` for local fallback avatars |
| `sources.icon_source` | Source icon provenance and priority marker |
| `sources.icon_updated_at` | Timestamp for the latest persisted source icon metadata update |
| `source_fetches.source_id` | Source whose individual fetch attempt produced the diagnostics |
| `source_fetches.status` | Latest fetch status used for sidebar source failure warning and source-card latest sync availability when the newest row is `error`, `success`, or `not_modified` |
| `source_fetches.http_status` | Optional HTTP status projected onto `SourceRead.latest_fetch_http_status` |
| `source_fetches.error_message` | Optional backend failure summary projected onto `SourceRead.latest_fetch_error_message` |
| `source_fetches.items_seen_count` / `items_created_count` / `items_matched_count` / `items_invalid_count` | Latest fetch counters projected onto `SourceRead.latest_fetch_items_*_count` fields |
| `source_fetches.fetch_started_at` / `fetch_finished_at` | Fetch timing projected onto `SourceRead` latest-fetch diagnostic fields |

## Source Delete Boundary

Source hard-delete is intentionally narrower than database-level cascading everything related to a source. The preview/delete contract treats `sources`, `source_fetches`, `source_item_occurrences`, `source_preference_profiles`, `source_channel_tendencies`, source flow policy ownership, and exclusive unsaved feed items as source-owned data. Exclusive unsaved feed items may take their derived `item_states`, behavior events, structured feedback, homepage read models/facets, and feed-item Insight Engine artifacts with them because they are no longer reachable from any source or Knowledge item.

Saved Knowledge content is retained. A feed item with any `saved_items` row is retained even when that saved item is soft-removed, because the Knowledge/archive recovery boundary is stronger than source cleanup. Feed items shared with another source are also retained. Historical `app_operations` and `insight_engine_attempts` are not deleted; source delete detaches diagnostic source references where necessary so audit rows remain readable without pointing at a removed source.

## Homepage Read Model Values

`HomepageQuery` is a request schema shared by `/api/homepage/bootstrap`, `/api/homepage/summary`, and `/api/homepage/items`; it is not stored as a row. `query_signature` is a deterministic signature of the normalized filter/sort/window state and is used to reject stale cursors. `next_cursor` is an opaque continuation token owned by the backend; frontend code must pass it back without parsing it and must start a new bootstrap when filters change.

`feed_item_homepage_read_models` is derived state. Fresh rows are built by `homepage_read_model.py` from canonical feed item data, latest source occurrence, item state, structured feedback, latest feed-item Insight Engine artifacts, source/channel/profile/settings context, and the same homepage card builder used by live fallback. The table stores both the complete `card_json` and expanded scalar fields for SQL filtering, sorting, search, and summary counts. Rebuild triggers include source sync new/matched items, reader state, save/unsave, feedback, content extraction, feed-item artifact completion, channel lifecycle reroute, source routing/profile changes, Insight Profile changes, and Insight Engine scoring setting changes. App startup queues missing or stale rows for background rebuild without blocking startup.

`feed_item_homepage_facets` stores channel and topic memberships for each read-model row. This keeps facet filters and grouped counts in SQL without relying on JSON-array predicates over `card_json`. Facet rows are deleted and recreated with their parent read-model row during rebuild.

`read_model_status` is an API response object rather than a database table. It reports `state = fresh | rebuilding | fallback_live` plus `missing_count`, `stale_count`, and `message`. A fresh response reads the DB-backed read model. `rebuilding` or `fallback_live` means the API detected missing/stale rows, queued or confirmed background rebuild, and may have used the live projection for correctness in the current response.

`snapshot_token` is also an opaque backend-owned value, but it is a short TTL in-process read acceleration token rather than a canonical entity. A valid token points to a process-local snapshot containing query signature, generated/expires timestamps, sort/page-size metadata, the operation invalidation generation, and sorted feed item ids. `/api/homepage/items` can use it to slice the next window ids and project only those card DTOs. `/api/homepage/summary` intentionally does not use snapshots and always computes fresh counts/facets. Terminal homepage-affecting operations and direct homepage artifact mutations invalidate the current snapshot generation; old tokens then return `snapshot_invalidated`, while expired tokens return `snapshot_expired`. Multi-process deployments are not strongly consistent with this in-memory token store; if that deployment shape becomes required, promote this boundary to DB-backed snapshots or sticky sessions.

`source_category = "__empty__"` is the explicit homepage id for sources whose `sources.folder_path` is null or blank. It displays as `未分类`, but that label is presentation-only. A real persisted folder path named `Uncategorized` or `未分类` remains a distinct category value and should not be collapsed into the empty category.

Homepage summary facet rows use `{id, label, count}`. Counts are computed over the full matching dataset, with all active filters applied except the current selected value of the facet being counted. Item windows are only the currently loaded center-list slice and must not be treated as the full result set.

## Insight Channels And Source Routing

Insight Engine routing uses the following current PostgreSQL owners and contracts.

`insight_channels` stores the configurable channel catalog used by the request builder and deterministic routing fallback. The catalog is DB-driven at runtime: callers can create non-system channels, edit runtime-semantic fields with `expected_channel_catalog_version`, disable/re-enable non-system active channels, and retire channels instead of hard-deleting or renaming ids. Retired and disabled channels stay readable for historical display and evidence export, but only active channels (`enabled = true`, `retired_at is null`) participate in new AI routing, deterministic fallback, source tendency saves, and human routing corrections.

Default channels are seeded when missing. Existing user-edited non-system default rows are not overwritten, but `needs_review` is repaired as the locked system fallback if it is missing or drifted:

- `tcp_artificial_muscle_core` / TCP / 人工肌肉核心研究 / `deep`
- `soft_actuation_horizon` / 柔性驱动视野拓展 / `medium`
- `academic_horizon` / 学术视野拓展 / `medium`
- `ai_agents_frontier` / AI & Agents 前沿 / `medium`
- `open_source_tools_practice` / 开源工具与工程实践 / `medium`
- `securities_investing` / 证券投资 / `medium`
- `cryptocurrency` / 加密货币 / `light`
- `needs_review` / 待归类 / `medium` / system fallback, sorted last by default

| Field | Meaning |
| --- | --- |
| `id` | Stable channel id used by API payloads, prompt context, source tendencies, and `feed_item_routing_v1` artifacts. |
| `label` | Human-facing channel label, currently Simplified Chinese defaults. |
| `description` | Short channel description included in prompt context when enabled. |
| `focus_prompt` / `boundary_prompt` | Prompt context used by feed item Insight Engine requests. |
| `reading_depth` | Default reading-depth hint: `light`, `medium`, or `deep`. |
| `default_flow_policy` | Default flow policy hint for the channel. |
| `sort_order` | Settings/read-context ordering. |
| `enabled` | Disabled channels remain readable in Settings but are excluded from runtime prompt channel context and fallback routing. |
| `is_system` | Protects system channels such as `needs_review` from disable/retire and from user-created id collision. |
| `retired_at` | Retirement timestamp. Retired channels are historical rows only and are rejected for new routing writes. |

`insight_channel_catalog_state` is a singleton table with row `id = 1`:

| Field | Meaning |
| --- | --- |
| `channel_catalog_version` | Monotonic version used for optimistic Settings writes, stale submit rejection, reroute job idempotency keys, and lifecycle audit metadata. |
| `updated_at` | Last catalog version update timestamp. |

`source_channel_tendencies` stores source-specific routing hints. It is canonical source configuration, not derived article routing. API validation rejects duplicate channel ids, unknown channel ids, disabled/retired channel ids for new writes, and more than one primary tendency for a source. Historical tendencies that point at disabled or retired channels remain readable so impact preview, audit, and evidence export can explain old decisions.

| Field | Meaning |
| --- | --- |
| `source_id` | Required source owner; cascades when the source is deleted. |
| `channel_id` | Required insight channel. Channels are not hard-deleted; retired references remain readable. |
| `tendency` | Float `0..1` source tendency strength. |
| `is_primary` | Primary source-channel hint. At most one primary hint is allowed per source by API validation. |
| `notes` | Optional operator note for why the source tends toward this channel. |

`sources.flow_policy` defaults to `normal` and accepts `normal`, `mixed_high_frequency`, or `high_frequency_brief`. It is separate from `sources.ai_route`: AI route chooses provider profile/privacy boundary, while flow policy influences routing/flow-lane behavior. Deterministic routing fallback maps `high_frequency_brief` to `flow_lane = brief_pool`; other policies default to `main` while recording a source-flow-policy reason.

## Source Preference Profiles

`source_preference_profiles` stores source-specific preference context. It is canonical source settings data, separate from source routing settings:

- `source_channel_tendencies` and `sources.flow_policy` tell the system which channels/flow a source tends toward.
- `source_preference_profiles` tells the Insight Engine why the user subscribes to the source and what source-specific article quality signals matter.

Each source can have at most one mutable `draft` row and at most one immutable `active` row. Activating a draft archives the previous active row, creates a new active snapshot, assigns a new `profile_version`, deletes the draft, and affects only future Insight jobs. It does not rewrite historical `insight_engine_artifacts`, trigger automatic reprocessing, or mutate source routing tendencies.

| Field | Meaning |
| --- | --- |
| `status` | `draft`, `active`, or `archived`; only active rows affect runtime Insight requests and scoring. |
| `schema_version` | Current value is `source_preference_profile_v1`. |
| `profile_version` | Version string included in request previews and feed-item insight enqueue context. |
| `subscription_reason` | Optional primary user-authored explanation for why this source is subscribed. |
| `good_article_criteria_json` / `bad_article_patterns_json` | Optional source-specific quality criteria and negative patterns. |
| `preferred_topics_json` / `ignored_topics_json` | Optional controlled `taxonomy_v1` topic ids. |
| `preferred_content_types_json` | Optional controlled `content_type_v1` ids. |
| `positive_example_item_ids_json` / `negative_example_item_ids_json` | Optional article ids used as examples for future review/audit. |
| `positive_example_summaries_json` / `negative_example_summaries_json` | Optional prompt-safe summary text for selected examples; quotes are not part of the current prompt context. |
| `quality_notes` | Optional source-quality note. |
| `prompt_context_json` / `prompt_context_stats_json` | Deterministic runtime context and included/omitted/capped stats under the 4 KB UTF-8 render budget. |
| `source_type` | Profile origin such as `user_edit`, `codex_import`, or future suggestion flows. |
| `edit_note` | Optional activation/edit note. |
| `activated_at` / `archived_at` | Lifecycle timestamps. |

Validation rejects over-limit prompt-affecting fields and prompt-injection patterns with field-level errors. Empty active profiles are valid and carry a `profile_version`; they represent an explicit clear action while still allowing stale queued jobs to detect the profile change.

## App Operations

`app_operations` is the backend source of truth for active long-running work that should survive browser refresh/reopen. The same table covers source sync, feed item missing-artifact operations, capture, mirror export, feed item extraction/artifacts, saved item insight rebuild operations, and channel lifecycle retirement/reroute operations. `jobs` / `job_events` store child execution state for selected operation families; the operation row remains the frontend contract and progress/result source.

| Field | Meaning |
| --- | --- |
| `operation_type` | `source_sync`, `feed_item_insight_artifacts`, `feed_item_insight_artifact`, `knowledge_capture`, `local_mirror_export`, `feed_item_extract`, `saved_item_insight`, or `channel_lifecycle`. v3 starts from a fresh database and does not migrate old operation rows. |
| `operation_kind` | For source sync: `all`, `single`, or `auto_create`; for user-created feed item insight artifact batches: `global`, `current_source`, `failed_attempts`, or `retryable_coverage`; for source-sync-created feed item artifact follow-up operations: internal `source_sync_created_feed_items`; for capture/mirror/feed/saved item operations: `manual_url`, `uploaded_material`, `manual_import`, `retry_capture`, `feed_saved_capture`, `manual_reexport`, `manual_extract`, `rebuild`, or `build_missing`; for channel lifecycle: `retire_channel`. |
| `status` | `queued`, `running`, `stopping`, `completed`, `stopped`, `failed`, or `interrupted`. Active statuses are `queued`, `running`, and `stopping`. |
| `source_id` | Nullable source reference used for single-source/current-source operations. The foreign key uses `ON DELETE SET NULL` so historical operation records can remain readable after a source is removed. |
| `scope_json` | Request scope metadata such as source scope, saved item id, feed item id, capture run id, limits, and operation-specific options. |
| `progress_json` | Latest visible progress state: stage, source counters, RSS fetch counters, Insight Engine counters, capture/upload/mirror stage, stop flags, and recent errors. |
| `result_json` | Terminal operation result, shaped for the operation family and including affected target ids for diagnostics. |
| `message` / `error_message` | User-facing summary or failure text. |
| `stop_requested` | Cooperative stop flag. It is checked at source/item boundaries and does not imply in-flight request cancellation. The public stop API writes this intent only; child job cancellation and terminal result/progress sync happen in worker context. |
| `runner_id` | Backend process marker. Active rows whose runner id differs from the current process, or is missing, are marked `interrupted` when active operations are queried or new operations are started. |
| `created_at`, `started_at`, `updated_at`, `completed_at` | Operation lifecycle timestamps. |

Indexes:

- `ix_app_operations_status_updated` for active/recent operation lookup.
- `ix_app_operations_type_status` for type-scoped status lookup.
- `ix_app_operations_runner_status` for restart interruption detection.

`app_operations` is not itself a durable distributed job queue. It records operation intent/progress and gives the frontend a recoverable progress source after browser refresh. `jobs` provides local Postgres-backed execution for source sync child jobs, feed item extraction, feed item insight artifact batch fill, linked source-sync artifact pipeline items, channel reroute items, local mirror export, feed item artifact rebuild, saved item insight rebuild, and knowledge capture/upload/manual-import execution. User-started source sync, feed item insight artifact operations, and channel lifecycle retire operations are globally mutually exclusive; the internal `feed_item_insight_artifacts/source_sync_created_feed_items` operation is the linked build exception created by source sync itself. Item/capture/mirror operations can run alongside them but reject duplicate active operations for the same target/type.

## Operation Jobs

`jobs` and `job_events` are execution tables for selected operation families. They are intentionally local to the same application database and do not introduce Redis, Celery, a separate scheduler, or a public job admin API.

| Field | Meaning |
| --- | --- |
| `jobs.operation_id` | Nullable link to `app_operations`; most operation jobs set it, while infrastructure tests may cover nullable allowlisted job types. |
| `jobs.job_type` | Registered handler key, currently `source_sync_one_source`, `feed_item_extract`, `feed_item_insight_artifacts`, `feed_item_insight_artifact_build`, `feed_item_insight_artifact`, `channel_reroute_item`, `local_mirror_export`, `saved_item_insight`, `knowledge_capture`, or `knowledge_capture_local_mirror_export`. v3 starts from a fresh database and does not migrate old job rows. |
| `jobs.status` | `pending`, `running`, `succeeded`, `failed`, or `cancelled`. |
| `jobs.queue_name` / `priority` | Claim ordering controls for local queue workers. |
| `jobs.payload_json` / `redacted_payload_json` | Small validated payload and redacted copy. Payloads must not contain URL/title/body/HTML/Markdown/provider request or response data, API keys, cookies, authorization headers, or user notes. |
| `jobs.idempotency_key` | Active-job uniqueness key. Terminal jobs release the active uniqueness constraint so manual requeue can create a new job. |
| `jobs.attempt`, `max_attempts`, `run_after` | Retry/backoff state. |
| `jobs.lease_owner`, `lease_expires_at`, `heartbeat_at` | Claim/lease heartbeat state. Expired leases can be reclaimed by any worker slot. |
| `jobs.cancel_requested`, `cancelled_at` | Cooperative cancel state. Pending jobs cancel immediately; running jobs stop at handler-defined boundaries. |
| `jobs.error_type`, `error_message`, `error_detail_json` | Redacted and truncated terminal failure diagnostics. |
| `job_events.job_id` | Required job link with cascade delete. |
| `job_events.event_type` / `payload_json` | Lifecycle event name plus small redacted diagnostics such as ids, job type, status, attempt, and worker id. |

## Feed Item Insight Feedback

`feed_items.insight_feedback_text` stores user-authored feedback for one reader/homepage feed item.

This is canonical human feedback, not a generated Insight Engine artifact. It can describe whether the current insight summary, category, priority, reasons, or long-term value are wrong or useful. Evidence exports treat present `insight_feedback_text` as highest-weight human feedback, but the field by itself does not add runtime scoring, background learning, or profile mutation.

| Field | Meaning |
| --- | --- |
| `insight_feedback_text` | Nullable free-form text written by the user for one `feed_item`. Reader API trims saved text; `null`, blank, or whitespace-only updates clear the value. |

`feed_item_structured_feedback` stores the current structured human feedback for one feed item, with a unique `feed_item_id`. It is canonical human evidence and must not be written into `insight_engine_artifacts`.

| Field | Meaning |
| --- | --- |
| `feed_item_id` | Required unique feed item owner. Cascade deletes with the feed item. |
| `overall_value` | Nullable value signal: `no_value`, `neutral`, `useful`, `very_useful`, or `must_read`. |
| `priority_correction` | Nullable correction: `too_low`, `appropriate`, or `too_high`. |
| `topic_add_ids_json` | Controlled `taxonomy_v1` topic ids the user says should be added. |
| `topic_remove_ids_json` | Controlled `taxonomy_v1` topic ids the user says should be removed. |
| `content_type_correction` | Nullable controlled `content_type_v1` id. |
| `reason_code_add_ids_json` | Known reason-code ids the user says should apply. |
| `reason_code_remove_ids_json` | Known reason-code ids the user says should not apply. |
| `issue_markers_json` | UI issue markers such as `misleading_summary`, `wrong_topic`, `wrong_priority`, `thin_content`, `duplicate`, `source_quality`, or `extraction_problem`. |
| `created_at` / `updated_at` | Current feedback row timestamps. History lives in behavior events. |

Projection notes:

- Reader list/detail and homepage card APIs expose free-form text as `insight_feedback_text` and structured feedback as `structured_feedback`.
- Frontend `FeedItem.insightFeedbackText` and `FeedItem.structuredFeedback` carry the values for AI homepage and classic reader items.
- Insight Evidence Package export marks any article with present `insight_feedback_text` or `structured_feedback` as `human_feedback_priority = highest`.
- Saving or clearing structured feedback appends `structured_feedback_saved` or `structured_feedback_cleared` to `feed_item_behavior_events`. The Reader automatic low-priority correction writes `priority_correction = too_low`, adds issue marker `wrong_priority`, and appends `auto_low_priority_cleared`; that correction prevents future automatic score-threshold bucketing for the item but does not override current `not_interested`.

Channel/routing correction is stored on `feed_item_structured_feedback` in the current baseline. It is canonical human evidence, not derived AI output. It must not be written into `insight_engine_artifacts`, and it does not mutate the underlying `feed_item_routing_v1` artifact.

| Field | Meaning |
| --- | --- |
| `channel_correction.primary_channel_id` | Optional corrected primary channel id. |
| `channel_correction.add_channel_ids` | Channel ids the user says should be added as secondary routing evidence. |
| `channel_correction.remove_channel_ids` | Channel ids the user says should be removed from the visible runtime route. |
| `channel_correction.reading_depth` | Optional corrected reading depth: `light`, `medium`, or `deep`. |
| `channel_correction.flow_lane` | Optional corrected homepage flow lane: `main`, `brief_pool`, `low_priority`, or `needs_review`. |
| `channel_correction.notes` | Optional correction note. |

Projection notes:

- Reader list/detail and homepage card APIs expose channel/routing corrections as part of structured feedback projections.
- Homepage routing overlays channel/routing corrections on top of the latest AI or deterministic routing read model. A changed correction sets `routing_source = user_corrected`, preserves routing confidence, can remove the current primary channel, and filters inactive channels from the visible runtime route.
- Saving or clearing channel/routing correction is a structured-feedback write; unchanged saves should not create duplicate behavior evidence.
- Evidence Package exports `structured-feedback.jsonl` and `channel-corrections.jsonl`, includes per-article `channel_correction`, and treats explicit channel corrections as strong correction evidence.

## Saved Item Insight

Saved item insight is a knowledge-base insight layer over `saved_items`, archive snapshots/assets, user notes, and capture metadata. It is separate from feed item `insight_feedback_text`.

`saved_items.origin_type` records explicit saved-item origin. The supported database path starts from fresh rows rather than inferring origin from old data. Future external/import origins should add explicit values such as `external_saved` or `automation_capture` rather than inferring meaning from source URLs.

`saved_items.ai_route` is the saved item privacy boundary for AI insight generation:

- Feed-linked saves copy the current `sources.ai_route` into `saved_items.ai_route` at save/restore time. Later source route edits do not rewrite existing saved item routes.
- Manual URL captures and uploaded material captures default to `sensitive`; callers may explicitly save `general`.
- Rebuilding a saved item insight follows `saved_items.ai_route` by default. A rebuild request may temporarily override the route for that generation without mutating the saved item row.
- `disabled` and incomplete selected profiles fail saved item insight generation clearly instead of silently falling back to another route.

`saved_items.canonical_url` is nullable, and upload metadata lives in `original_filename`, `mime_type`, `source_asset_id`, `processing_status`, and `processing_error`. These fields support Markdown, normal HTML, and SingleFile HTML uploads without pretending every saved item has a web URL. `source_asset_id` points at the current uploaded `uploaded_source_material_v1` asset when present. Existing saved-item manual import creates the replacement source asset before processing but preserves the previous current asset, filename, MIME type, title override, and note override until replacement processing succeeds.

AI generated saved item insight reuses `insight_engine_artifacts`:

| Field | Meaning |
| --- | --- |
| `artifact_kind` | `saved_item_insight_v1` |
| `saved_item_id` | Required saved item id for this artifact kind |
| `feed_item_id` | `null` for saved item insight artifacts |
| `artifact_json` | Structured saved item insight payload plus selected input metadata |
| `prompt_version` | `saved-item-insight-v1` |

`insight_engine_artifacts` owner/kind integrity is enforced in the v3 fresh baseline:

- Each artifact row must have exactly one owner: `feed_item_id` or `saved_item_id`, never both and never neither.
- `saved_item_insight_v1` rows must use `saved_item_id` and leave `feed_item_id` null.
- Feed item artifact kinds (`feed_item_summary_v1`, `feed_item_topics_v1`, `feed_item_ranking_v1`, and `feed_item_routing_v1`) must use `feed_item_id` and leave `saved_item_id` null. Legacy `homepage_*_v1` rows are historical names, not current write kinds.
- Migration preflight fails if existing rows violate those rules; it does not silently repair or delete historical data.
- Latest artifact reads are supported by composite indexes on `(artifact_kind, feed_item_id, id DESC)` and `(artifact_kind, saved_item_id, id DESC)`.

The payload is structured for knowledge reuse and evidence export:

- `summary`
- `long_term_value`
- `actionable_takeaways`
- `key_concepts`
- `suggested_tags`
- `topic_ids`
- `content_type`
- `ai_route`
- `input_text_source`
- selected input snapshot/asset metadata when available

User curated revisions are not Insight Engine artifacts:

| Table | Rule |
| --- | --- |
| `saved_item_curated_insights` | One current curated revision per saved item, enforced by a unique `saved_item_id`. Stores `base_insight_engine_artifact_id`, `curated_payload_json`, `diff_json`, optional `edit_note`, `created_at`, and `updated_at`. |
| `saved_item_curated_insight_events` | Append-only audit history for create/update/clear actions. Clearing the current curated revision writes a `curated_insight_cleared` event before deleting the current row. |

Resolved saved item insight is a read projection:

1. current user curated insight if present;
2. latest valid `saved_item_insight_v1` Insight Engine artifact for the saved item;
3. `null`.

Generation input priority is fixed to latest uploaded markdown, latest image-reinserted fit markdown, latest fit markdown, latest raw markdown, canonical snapshot text, SingleFile HTML extracted text, then user note/title/URL/filename metadata fallback.

Save and capture actions must not wait for saved item insight generation. Local Mirror and `llm_wiki/` outputs remain derived files and must not reverse-write insight data into PostgreSQL.

## Reader Item State

`item_states` stores the current reader/homepage state projection for one feed item. `feed_item_behavior_events` stores the append-only evidence trail; normal reads derive current status from `item_states`, not by replaying events.

Current state timestamps:

| Field | Meaning |
| --- | --- |
| `browsed_at` | The user opened the card/detail enough to move an unread item into a browsed state. |
| `seen_at` | The user explicitly processed the item from title/summary without claiming full reading. UI label: `已处理`. |
| `read_full_at` | The user explicitly marked the full article read in-app, or opened the original link and the app recorded that external reading action. |
| `saved_at` | The item is currently saved into the knowledge base. |
| `not_interested_at` | The item is currently deprioritized and reviewable, not deleted. |

Derived status priority is fixed as `not_interested > saved > read_full > seen > browsed > unread`. Clearing a stronger timestamp falls back to the strongest remaining timestamp. For example, clearing `read_full_at` can reveal `seen`, `browsed`, or `unread`; clearing `seen_at` can reveal `browsed` or `unread` unless a stronger timestamp is still present.

`seen` is included in the current baseline as nullable `item_states.seen_at`. The reset baseline intentionally performs no historical backfill because older data is not preserved.

## Feed Item Behavior Events

`feed_item_behavior_events` stores append-only behavior evidence.

This table is append-only behavior evidence. It does not replace `item_states`, which remains the current reader/homepage state projection. The table exists so offline Insight Engine analysis can distinguish how an item reached its current state, for example a bulk summary cleanup, an in-app full read, or an external original-link open.

Historical v2 development migrations backfilled existing data from `item_states.browsed_at`, `item_states.read_full_at`, `item_states.not_interested_at`, feed-linked `saved_items.saved_at`, and feed-linked `saved_items.removed_at`. Backfilled event sources were `phase7_backfill_item_state` and `phase7_backfill_saved_item`; v3 does not run that path for old production databases.

| Field | Meaning |
| --- | --- |
| `id` | Behavior event identity |
| `feed_item_id` | Required feed item foreign key with cascade delete |
| `event_type` | String event kind, length 64 |
| `event_payload_json` | JSON audit payload for the event context |
| `source` | Writer/source marker, length 64 |
| `created_at` | Behavior timestamp |
| `recorded_at` | Database insertion timestamp |

Current event types written by services:

- `card_opened`
- `marked_seen_from_list`
- `marked_seen_bulk`
- `seen_cleared`
- `read_full_in_app`
- `read_full_cleared`
- `opened_original_link`
- `saved`
- `unsaved`
- `not_interested`
- `not_interested_cleared`
- `insight_feedback_saved`
- `insight_feedback_cleared`
- `structured_feedback_saved`
- `structured_feedback_cleared`
- `auto_low_priority_cleared`

Current event sources written by services:

- `reader_state_api`
- `reader_bulk_api`
- `reader_feedback_api`
- `reader_priority_api`
- `knowledge_save`
- `knowledge_unsave`

`marked_seen_from_list` means a deliberate title/summary-level cleanup. `marked_seen_bulk` is the weaker source-level version of the same cleanup. `read_full_in_app` means explicit in-app full reading. `opened_original_link` records the original URL and extraction diagnostics and is treated as full reading because the app cannot observe the external page after opening it. `auto_low_priority_cleared` means the user corrected an automatically assigned low-priority bucket as too low; it is explicit human priority evidence but not a generic positive reading signal.

Insight Evidence Package preview/export reads this table directly. `behavior-events.jsonl`, article JSON `behavior_events`, preview `behavior_event_count`, clear/undo counts, manifest statistics, `statistics.md`, and `evidence-report.md` all use `feed_item_behavior_events` as their source.

## Insight Evidence Package v2

Evidence package export is read-only and recomputable. It stores no rows and does not mutate `insight_engine_artifacts`, profile tables, or item states.

The current Evidence Package schema identifier is `insight_evidence_package_v2`. It is a data-contract version and is independent of application release metadata.

Range values:

- `7d`
- `30d`
- `90d` default
- `180d`
- `365d`
- `all`
- `custom` with `start_date` and `end_date`

Every range excludes `item_states.status = unread`. Eligible current states are `browsed`, `seen`, `read_full`, `saved`, and `not_interested`. Time filtering uses `feed_items.published_at`, falling back to latest occurrence timestamp and then item `created_at`.

Artifact export modes:

- `latest` default: exports latest feed item Insight Engine artifacts plus current Backend Scoring v1 priority/routing audit and failed/skipped attempt summary.
- `all_history`: includes everything from `latest` and additionally exports historical feed item Insight Engine artifacts in the selected range.

ZIP contents:

- `manifest.json`
- `README.md`
- `schema/insight-feedback-evidence-package.schema.json`
- `schema/profile-candidate.schema.json`
- `profile-candidate-template.json`
- `summary/dataset-summary.json`
- `profiles/current-profile.json`
- `profiles/source-profiles.jsonl`
- `profiles/insight-profile-history.jsonl`
- `channels/channel-catalog.json`
- `channels/{channel_id}.json`
- `sources/source-routing.jsonl`
- `sources/source-summary.jsonl`
- `events/insight-evidence-events.jsonl`
- `events/structured-feedback.jsonl`
- `events/channel-corrections.jsonl`
- `events/saved-item-curated-insight-events.jsonl`
- `reports/external-codex-instructions.md`
- `reports/profile-update-guidance.md`
- `reports/channel-prompt-boundary-suggestions.md`
- `reports/statistics.md`
- `reports/evidence-report.md`
- `items/feed-items/{feed_item_id}.json`
- `items/feed-items/{feed_item_id}.content.txt`
- `artifacts/insight-artifacts-latest.jsonl`
- `artifacts/score-breakdowns-latest.jsonl`
- `artifacts/routing-breakdowns-latest.jsonl`
- `artifacts/history/insight-artifacts-history.jsonl` when `artifact_mode = all_history`
- `artifacts/failed-skipped-attempts-summary.jsonl`
- `saved-items/{saved_item_id}.json`
- `saved-items/{saved_item_id}.content.txt`
- `saved-items/{saved_item_id}.display.md` when display Markdown is available
- `saved-items/{saved_item_id}.assets/*` for image assets actually referenced by display Markdown

Article JSON includes source metadata, state timestamps including `seen_at`, `content_text_source`, `content_extraction_status`, resolved current insight values, latest raw feed item `insight_engine_artifacts` metadata/payloads, Backend Scoring v1 priority/routing audit, table-backed `behavior_events`, `insight_feedback_text`, `structured_feedback`, `channel_correction`, channel routing, source routing, evidence signal classification, and `human_feedback_priority = highest` when explicit free-form, structured, or channel correction evidence exists. `current_insight.priority` is computed by the same Backend Scoring v1 service used by homepage cards and includes `low_priority_bucket_reason` plus `auto_low_priority_cleared`.

Saved item JSON includes normalized `origin_type`, raw `raw_origin_type`, `ai_route`, `external_saved_signal`, saved timestamp, title, canonical URL, source metadata, user note, capture metadata, snapshot availability, selected input kind, latest AI generated saved item insight, current user curated insight, resolved insight, curated edit events, optional display Markdown metadata, and referenced display asset metadata. Manual URL captures and Markdown / normal HTML / SingleFile HTML file captures are normalized to `external_source` for evidence-package source classification while preserving raw DB origin in `raw_origin_type`. Saved item selected input text is exported as `content.txt`. Display Markdown is exported as `display.md` using the same priority as the Knowledge reading surface: uploaded Markdown, Crawl4AI fit image markdown, Crawl4AI fit markdown, then Crawl4AI raw markdown. Only image assets referenced by that display Markdown are exported. Raw HTML, SingleFile HTML, unreferenced page images, and full web-page archival attachments are not included. `saved-item-curated-insight-events.jsonl` contains the full selected-range curated event stream. Preview uses `external_saved_item_count`; manifest counts use `external_saved_items`. Saved item export reads PostgreSQL snapshots/assets/artifacts/events only; it does not read Local Mirror or `llm_wiki/` as reverse sources.

`events/insight-evidence-events.jsonl` semantics are sourced from `feed_item_behavior_events`, including seen cleanup, original-link open, feedback clear, structured correction, not-interested undo, and auto low-priority clear events. Package preview/manifest/statistics include behavior event, structured feedback, channel-correction, signal, feedback clear, not-interested undo, auto low-priority clear, artifact mode, failed/skipped attempt, saved item display Markdown/asset, and saved item insight/event counts. The ZIP also includes channel catalog, structured feedback JSONL, channel corrections JSONL, source routing JSONL, source profile exports, evidence signal summary, channel preference summary, profile candidate schema/template, `reports/profile-update-guidance.md`, and fixed-template external Codex instructions. The markdown report files describe `seen` and `marked_seen_bulk` as cleanup rather than topic dislike, `read_full_in_app` as in-app full reading, `opened_original_link` as external opening treated as full reading, structured routing corrections as explicit channel correction, and `auto_low_priority_cleared` as a human correction that the system ranked the item too low.

## Backend Scoring v1

Backend Scoring v1 is read-time derived state, not a table and not a new artifact kind. It is implemented in `backend/app/services/insight_scoring.py`.

Inputs:

- Latest valid `feed_item_topics_v1` artifact for controlled `topic_ids` and `content_type`.
- Latest valid `feed_item_ranking_v1.priority_reasons` as AI content signals.
- Active/default Insight Profile prompt context fields: `active_focus`, `long_term_interests`, `topic_preferences`, and `content_type_preferences`.
- `feed_item_behavior_events` for source/category history.
- Latest source occurrence for source history.
- Current item status from `item_states`.

Rules:

- Base score is `50`, final score is clamped to `0..100`.
- Bands are `high >= 70`, `medium >= 40`, otherwise `low`.
- `not_interested` is a strong reversible negative contribution, band `low`, low-priority bucket `true`, and low-priority reason `not_interested`; it is not a hidden deletion or separate score table override.
- Automatic score-threshold low-priority bucketing is controlled by `app_settings.auto_low_priority_archiving_enabled` and `app_settings.low_priority_score_threshold`. It defaults to disabled and threshold `50`; when disabled, non-`not_interested` old items read back outside the low-priority bucket without data backfill.
- A current structured feedback row with `priority_correction = too_low` clears automatic score-threshold bucketing for that item and surfaces `auto_low_priority_cleared = true`; it does not clear `not_interested`.
- AI `feed_item_ranking_v1.priority_score` is preserved only in raw artifact payloads; it does not determine final homepage or evidence-package priority.
- Positive AI reason codes are `high_actionability`, `deep_reference_value`, `specific_insight`, and `implementation_detail`.
- Negative AI reason codes are `thin_content`, `marketing_or_pr`, `repetitive_update`, and `low_content_confidence`.
- Backend Scoring v1 uses grouped `InsightFeatureVector` data, deterministic `section.field` flattening, and `score_v1_hand_weighted` contribution rows. AI reason weights are semantic signals inside that feature vector; AI `priority_score` is ignored for final scoring.
- Source/category history uses positive events `saved`, `read_full_in_app`, and `opened_original_link`; negative events `not_interested` and `unsaved`; neutral or weak-cleanup events such as `marked_seen_from_list`, `marked_seen_bulk`, `seen_cleared`, `read_full_cleared`, `not_interested_cleared`, `insight_feedback_saved`, `insight_feedback_cleared`, `structured_feedback_saved`, and `structured_feedback_cleared` do not affect source/category history score.
- `not_interested` history is counted as negative only while the item's current `ItemState.status` remains `not_interested`; undo removes the negative history effect without adding a positive signal.
- History excludes the current item from source/category aggregates so an item's own event does not self-justify its score.
- Category history uses controlled `topic_ids`; source folder/category display labels are not treated as taxonomy.

Outputs:

- `priority_score`
- `priority_band`
- signed `priority_reasons`
- `low_priority_bucket`
- `low_priority_bucket_reason`
- `auto_low_priority_cleared`
- `priority_source = backend_scoring_v1`

## Insight Engine Attempts

`insight_engine_attempts` is included in the current baseline. It is a historical diagnostics table, not a source of truth for generated insight content or current item repair state.

The table records failed and skipped feed item Insight Engine attempts so Settings can inspect failure patterns without reading logs. Rows are append-only diagnostic history for practical purposes: a later successful repair can make the feed item coverage complete, but it does not delete old failed/skipped attempt rows. It must not store API keys, `Authorization` headers, cookies, or full raw provider response bodies.

| Field | Meaning |
| --- | --- |
| `id` | Attempt identity |
| `feed_item_id` | Optional feed item for the attempted feed item insight generation |
| `source_id` | Optional latest source route context |
| `profile` | AI profile route such as `general`, `sensitive`, or `disabled` |
| `provider` | Provider value such as `deepseek`, `openai-compatible`, `lm-studio`, or `disabled` |
| `model` | Model name selected for the attempt when known |
| `stage` | Build stage that failed or skipped, such as request building, provider call, validation, or persistence |
| `status` | Diagnostic status, currently `failed` or `skipped` |
| `error_type` / `error_message` | Bounded, user-visible failure summary |
| `duration_ms` | Optional elapsed time for the attempt |
| `artifact_count` | Number of feed item artifact rows written before the diagnostic was recorded, usually `0` for failed/skipped attempts |
| `request_snapshot_json` | Sanitized request metadata/body snapshot for debugging |
| `created_at` | Attempt record insertion time |

## Insight Engine Coverage

`GET /api/settings/insight-engine/coverage` does not persist coverage state. It recomputes coverage from existing rows whenever Settings requests it.

Coverage completeness is defined over the latest resolved feed item artifact kinds:

- `feed_item_summary_v1`
- `feed_item_topics_v1`
- `feed_item_ranking_v1`
- `feed_item_routing_v1`

The coverage projection classifies incomplete items with a best-effort reason code and one display/action category:

| Reason code | Category | Meaning | Retryable |
| --- | --- | --- | --- |
| `source_ai_route_disabled` | `blocked` | Latest source route disables AI generation | no |
| `provider_disabled_or_unconfigured` | `blocked` | The selected profile/provider is disabled or incomplete | no |
| `low_confidence_content` | `blocked` | Content extraction failed and current candidate text still looks URL-only or like a jump prompt | no |
| `previous_failed_attempt` | `problem` | Latest recorded attempt failed | yes |
| `previous_skipped_attempt` | `problem` | Latest recorded attempt was skipped | yes |
| `partial_artifacts` | `problem` | At least one feed item artifact kind exists, but not all four | yes |
| `no_attempt_history` | `initial_generation` | No successful artifact and no recorded attempt exists, usually from older or diagnostic-less ingestion | yes |

Coverage scans must not write `insight_engine_artifacts`, `insight_engine_attempts`, source rows, app settings, or repair data silently. Retry/build actions remain explicit UI actions backed by the current feed item artifact operation endpoints. Batch coverage fill uses `feed_item_insight_artifacts/retryable_coverage` and must enqueue only items whose coverage reason is retryable; disabled routes, unconfigured providers, and content-extraction-blocked rows remain diagnostic rows until the underlying reason is corrected. `retryable_coverage` operations may provide `reason_codes` to process one category at a time, such as problem retries without `no_attempt_history`, or initial-generation backlog without failed-attempt rows. Historical `insight_engine_attempts` rows can outnumber current coverage problems because multiple attempts may exist for one item, and old attempts remain after the item's latest artifact coverage becomes complete.

## Insight Profile v1

`insight_profiles` and `insight_profile_events` store user-created/imported profile versions and lifecycle events. The schema intentionally does not seed a default profile row.

The default Insight Profile is a code-level read-only fallback in `backend/app/services/insight_profiles.py`. When no database version is active, Settings returns that fallback with `active_profile.id = null` and `source_type = default_fallback`. Opening Settings or request preview must not persist the fallback. The prompt context derived from the profile is intentionally slim and omits internal profile id/version/schema/status metadata.

`insight_profiles` stores immutable profile versions:

| Field | Meaning |
| --- | --- |
| `schema_version` | Profile contract version. Current value is `insight_profile_v1`. |
| `profile_name` | Human-readable profile name from the validated JSON. |
| `profile_version` | Human-readable profile version string from the validated JSON. |
| `profile_json` | Normalized profile JSON after schema and controlled-contract validation. |
| `source_type` | Origin marker such as `local_user`, `manual_edit`, or `codex_import`. |
| `edit_note` | Optional user note for why this version was created or changed. |
| `diff_summary` | Short backend-generated diff summary against the previously active profile. |
| `previous_profile_id` | Previous active profile id when the version was created, if any. |
| `created_at` | Creation timestamp. |

`insight_profile_events` stores audit events:

| Field | Meaning |
| --- | --- |
| `profile_id` | Profile version associated with the event. |
| `event_type` | `created`, `activated`, or `rolled_back`. |
| `note` | Optional activation/rollback note. |
| `event_json` | Small audit payload. Activation and rollback events record `previous_active_profile_id`. |
| `created_at` | Event timestamp. |

Active profile selection is event-based: the newest event whose type is `activated` or `rolled_back` determines the active database profile. Rollback re-enables an existing older version by event; it does not clone or mutate the version row.

Insight Profile draft inspection is a read-only Settings projection, not a table. `POST /api/settings/insight-profile/draft/inspect` validates and normalizes a complete `insight_profile_v1` candidate, compares it with the active database profile or the code-level default fallback, and returns normalized profile JSON, candidate prompt context, base profile, diff summary, top-level field diffs, and evidence references. It does not create profile rows, write events, call AI providers, write `insight_engine_artifacts`, or mutate Evidence Package data.

Profile candidate inspection is also a Settings projection, not a table. `POST /api/settings/insight-profile/candidate/inspect` accepts a `profile_candidate` JSON object emitted by external Codex analysis, treats it as hostile input, and returns normalized candidate JSON, optional global Insight Profile inspection, per-source Source Profile diffs, channel prompt/boundary suggestions, warnings, evidence references, and a dry-run summary. It rejects unknown source/channel/feed/saved references and unsafe prompt-affecting text. It does not write profile rows, source profiles, channel rows, events, jobs, artifacts, or app settings.

`POST /api/settings/insight-profile/candidate/source-drafts` is the only external-candidate write path. It validates the full candidate again and writes selected `source_profile_changes[]` as mutable `source_preference_profiles.status = draft` rows with `source_type = codex_import`. It does not activate those drafts, create Insight jobs, reprocess historical items, update `source_channel_tendencies`, or change `insight_channels.focus_prompt` / `insight_channels.boundary_prompt`.

Profile JSON validation rules:

- fixed top-level fields only: `schema_version`, `profile_name`, `profile_version`, `summary_guidelines`, `active_focus`, `long_term_interests`, `positive_signals`, `negative_signals`, `topic_preferences`, `content_type_preferences`, and `evidence_references`;
- maximum normalized JSON size is 32 KB;
- unknown top-level or nested fields are rejected;
- secret-like key fragments such as `api_key`, `authorization`, `cookie`, `token`, `secret`, and `password` are rejected anywhere in the JSON object;
- topic ids in focus and preferences must canonicalize to `taxonomy_v1`;
- content type preferences must canonicalize to `content_type_v1`;
- profile JSON does not expand the AI-allowed reason-code set.

Settings profile management endpoints do not call AI providers, write `insight_engine_artifacts`, create placeholder artifacts, or trigger historical homepage rebuilds. They validate profile JSON, inspect complete draft candidates, and write profile/version event rows only when the user explicitly saves, activates, or rolls back a profile.

## Feed Item Content Extraction Fields

RSS short-content extraction fields live on `feed_items` only. There is no separate extraction table and no source-level extraction preference.

Extractor choice is not a source-level setting or separate table. The default uses `httpx + readability-lxml` to store reader HTML when quality gates pass, with `trafilatura` as fallback text extraction. The HTML fetch step follows HTTP redirects and may follow up to two in-page `meta refresh` redirects before extraction, scanning up to the current 500 KB page prefix for redirect hints. `<link rel="canonical">` is not treated as a page redirect, because many article pages point canonical metadata at a site/home URL that is not the article body. `readability-lxml` output normalizes `a href` and `img src` to absolute `http/https` URLs based on the final fetched article URL and removes `img srcset`; those image URLs are reader-display references only and never become `SavedItemAsset` rows. Automatic extraction decisions prefer preserved `feed_content_text` before current `content_text`, so RSS-provided full text is not mistaken for a short current/body placeholder. For short-form platform hosts (`t.me`, `telegram.me`, `x.com`, `twitter.com`, and `okjike.com`), automatic extraction treats existing non-URL, non-jump visible feed text as the complete body and writes `not_needed` with reason `short_form_feed_content`; manual force extraction still bypasses this shortcut. Quality gates compare against the preserved feed baseline (`feed_content_text`, then `summary_text`; only non-`extracted_page` `content_text` as a fallback), so an older extracted page body is not mistaken for original feed text during forced re-extraction. When a failure is specifically `quality_failed: extracted text is too short`, extraction may make one lightweight retry by refetching, re-resolving redirects, and rerunning readability-lxml then trafilatura. `content_extraction_debug_json` records fetch, `html_redirects` when present, extractor, fallback, visible-character count, and quality-gate diagnostics for manual and automatic attempts. Crawl4AI remains a capture/archive concern; `page_image_v1` assets are created only by saved-item capture/retry against `SavedItem.canonical_url`.

These fields belong to the current schema.

| Field | Meaning |
| --- | --- |
| `feed_content_text` | Original RSS/Atom content candidate, preserving `content:encoded` or Atom `content`, with fallback to `description` / `summary`. Page extraction must not overwrite it. |
| `content_text` | Current effective reader/AI body. Initially mirrors feed content or summary; successful page extraction can replace it with extracted source-page body. |
| `summary_text` | RSS/Atom summary semantics. Page extraction should not repurpose this field as full body text. |
| `content_text_source` | Source marker for `content_text`: `feed_content`, `feed_summary`, `extracted_page`, or `legacy`. |
| `content_extraction_status` | Item-level extraction status. See status values below. |
| `content_extraction_attempted_at` | Most recent automatic or manual extraction attempt time. |
| `content_extracted_at` | Most recent successful extraction time. |
| `content_extraction_error` | Short latest failure summary for UI and debugging. |
| `content_extraction_debug_json` | Latest extraction debug payload, used to improve rules later without a separate debug table. It may include `fetch.url`, `fetch.html_redirects[]` entries with redirect kind/source/requested/final URL and HTTP status, `fetch.url_fallbacks[]` entries for fetch-recovery attempts such as dropping a trailing path slash after HTTP 404, `extractor.name`, `output_format`, `extracted_visible_char_count`, retry details, and quality-gate results. |

Reader detail API exposes existing `feed_items.feed_content_text` as read-only `feed_content_text`; the frontend maps it to `feedContentText` for RSS/Atom original preview. Reader list/detail and homepage card APIs also expose derived read-only extraction diagnostics: `content_extraction_method`, `content_extraction_final_url`, and `content_extraction_visible_char_count`. These values come from `content_extraction_debug_json`; old rows without debug data return `null` and are not repaired at read time. This is an API/view-model projection, not a new persisted field. A `content_text_source = feed_summary` value means the current effective body was populated from RSS/Atom description or summary fields; it does not prove the body is only a short summary, because some feeds publish full text through those fields.

## State Values

### Source kinds and URL identity

Current source kinds:

- `rss`
- `rsshub`

RSS sources use canonical HTTP/HTTPS URLs as `sources.url`.

RSSHub sources use a route identity as `sources.url`:

- canonical form: `rsshub://route/path`
- removed legacy input: `rsshub:///route/path` is rejected.
- accepted known instance input: an HTTP/HTTPS URL under the effective RSSHub base URL or one of the known RSSHub bases

Known RSSHub instance URLs are converted to the route identity before source creation or OPML matching. This keeps `(kind, url)` stable when the same route is imported from a local, Docker, self-hosted, or public RSSHub instance.

The actual URL fetched for an RSSHub source is derived at preview/sync time from the effective `rsshub_base_url`. `SourceFetch.requested_url` records that resolved fetch URL, not the canonical `rsshub://` route identity.

Each source has `sources.ai_route` to choose the AI profile used when generating feed item insight artifacts for items whose latest occurrence belongs to that source:

- `general`: use the general AI profile. This is the default for new sources and was the historical v2 migration backfill value for existing sources.
- `sensitive`: use the sensitive AI profile.
- `disabled`: skip Insight Engine artifact generation for that source while keeping normal source sync enabled.

### Source folder/category metadata

`sources.folder_path` is the only subscription source category storage. There is no source category table, no category id, and no formal parent/child category model. OPML folder ancestry may be preserved as a slash-separated display path, but source category management treats each full `folder_path` string as one exact category value.

Source category rename updates only sources whose `folder_path` exactly matches the requested old path. For example, renaming `Tech` to `AI` does not update `Tech / Blogs`. Source category delete sets exact matches to `null`; it never deletes `Source` rows. `未分类` is a frontend display label for `null` and is not a separate canonical category.

This subscription source category metadata is unrelated to Insight Engine taxonomy, content types, reason codes, or AI-generated topic labels.

### Source icon metadata

Source icon metadata stores lightweight locator/provenance data only:

| Field | Meaning |
| --- | --- |
| `icon_url` | Nullable external icon URL. It is stored only when normalized as HTTP/HTTPS; icon binary data is not cached in PostgreSQL. |
| `icon_source` | Nullable provenance/priority value for the selected icon. |
| `icon_updated_at` | Timestamp set when `icon_url` or `icon_source` changes. |

Current `icon_source` values, from highest to lowest priority:

- `rss_channel_image`
- `atom_icon`
- `atom_logo`
- `opml_site_favicon`
- `source_site_favicon`
- `fallback_avatar`

Preview, create, OPML import, and sync all use `backend/app/services/source_icons.py` for URL normalization, fallback, and replacement rules. RSS/Atom feed icons may come from fetched feed metadata. OPML preview does not fetch each candidate feed for icons; it derives `/favicon.ico` from OPML `htmlUrl` or source URL candidates. Source sync can upgrade lower-priority icons to higher-priority feed icons and refresh same-priority URLs, but it must not downgrade an existing higher-priority icon to favicon or fallback. `fallback_avatar` records that no external icon URL is available; the frontend renders the local initial-avatar fallback.

### Source observability projection

Source observability diagnostics are read projections over existing `source_fetches` and `source_item_occurrences` rows, not persisted source fields.

`GET /api/sources`, `GET /api/sources/{source_id}`, and successful source PATCH responses expose:

- `item_count`
- `latest_fetch_is_available`
- `latest_fetch_status`
- `latest_fetch_http_status`
- `latest_fetch_error_message`
- `latest_fetch_items_seen_count`
- `latest_fetch_items_created_count`
- `latest_fetch_items_matched_count`
- `latest_fetch_items_invalid_count`
- `latest_fetch_started_at`
- `latest_fetch_finished_at`

`item_count` counts distinct `SourceItemOccurrence.feed_item_id` values for the source, so duplicate occurrence rows for the same feed item do not inflate the displayed subscription item count. The backend chooses the newest fetch row per source by `fetch_started_at desc, id desc` in a batch window query. Sources with no refresh history return `null` for every latest-fetch field except `item_count`, which defaults to `0`. `latest_fetch_is_available` is `true` for `success` and `not_modified`, `false` for `error`, and `null` when no latest fetch exists. The frontend uses `latest_fetch_status = error` for the sidebar warning, while Settings source cards also display item count, availability, counters, and compact error summaries.

### Source import result statuses

Defined in backend schemas and frontend source types:

- `created`
- `matched_existing`
- `invalid`

These are commit result statuses. `matched_existing` means the selected candidate's canonical `(kind, url)` already exists at commit time and no duplicate `Source` row was created.

OPML preview candidate statuses are:

- `ready`: valid candidate that does not currently match an existing source and is selected by default.
- `matched_existing`: valid candidate whose canonical `(kind, url)` already exists; skipped by default.
- `duplicate_in_file`: valid candidate that repeats an earlier candidate identity in the same OPML file; skipped by default.
- `invalid`: missing or invalid source URL; skipped by default.

OPML category outline nodes are folder metadata only. They are not `Source` rows. Child source entries inherit the slash-separated `folder_path`.

OPML preview/import accepts at most 5000 parsed candidate entries. Oversized OPML fails clearly before source rows are created; it is not silently truncated.

Preview candidate identity is carried by `candidate_id`, scoped to the `SourceImportRun.id`. It combines the OPML entry index with a hash of normalized entry identity fields and is the stable selector used by commit.

OPML preview writes `source_import_runs.raw_payload` and `summary_json.preview`, but it does not create source rows. Commit reparses the raw payload, applies only selected `candidate_id` values, and updates `summary_json.last_commit` with selected/skipped/created/matched/invalid counts and results.

Created sources use the selected candidate `ai_route`, falling back to commit `default_ai_route`, which defaults to `general`. Created sources also use the selected candidate `folder_path`; if the commit selection omits `folder_path`, the preview candidate's OPML folder is used, while an explicit `null` stores the source as uncategorized. Matched existing sources return the existing row's `ai_route`; commit does not overwrite it. A matched existing source with no folder path may be backfilled from the selected candidate folder.

Created sources also persist selected candidate icon metadata. Matched existing sources keep their existing icon unless no icon source has been recorded, in which case OPML/site favicon metadata may fill the empty icon fields.

Invalid OPML source entries are recorded in the import summary with their `input_url`, optional `kind`, `folder_path`, and error `message`. They do not roll back the rest of the OPML import.

### Reader item state

Defined in backend schemas and frontend types:

- `unread`
- `browsed`
- `read_full`
- `saved`
- `not_interested`

### Feed content extraction status

- `not_checked`
- `not_needed`
- `needs_extraction`
- `succeeded`
- `failed`
- `skipped`

These statuses are item-level diagnostics for RSS short-content extraction. They must not delete or hide the original RSS content and must not become source-level suppression rules. `not_needed` can mean either a normal feed body that already looks sufficient or a short-form platform post whose feed/RSSHub text is the complete body; inspect `content_extraction_debug_json.decision.reason` for `feed_content_looks_sufficient` versus `short_form_feed_content`. `skipped` can represent unsupported media pages or terminal dead links; inspect `content_extraction_debug_json.terminal.reason`, such as `unsupported_media_page` or `dead_link`.

### Capture run status

- `pending`
- `succeeded`
- `partial_failed`
- `failed`

Current success rule: all requested capture targets must complete with no failed tool call for `succeeded`. Partial success with some usable artifacts becomes `partial_failed`.

Feed-based `mark_saved` can return while the new `feed_saved` capture run is still `pending`. The route commits the saved item and reader state first, then a job-backed `knowledge_capture/feed_saved_capture` operation updates the run to `succeeded`, `partial_failed`, or `failed`.

### Tool call status

Current tool-call status values are stored as strings, with the current code using:

- `pending`
- `succeeded`
- `failed`

### Mirror export status

- `pending`
- `succeeded`
- `partial_failed`
- `failed`

Mirror status is independent from capture status. Local export failure must not roll back database archive success.

### Saved item removal state

`saved_items.removed_at` is the soft-removal marker for cancel-save behavior.

- Active knowledge list/detail/update/re-export reads only include rows where `removed_at is null`.
- Soft removal clears the associated feed item `saved_at` timestamp when `feed_item_id` is present, then re-derives the reader status.
- Capture runs, snapshots, assets, and mirror export records remain in PostgreSQL for recovery and replay.
- Re-saving a feed item with an existing removed saved item restores the same row by clearing `removed_at` and updating `saved_at`.
- Manual URL capture restores a removed manual saved item when the normalized canonical URL matches.
- Removed saved items are mirrored under `KnowledgeBase/.archive/` rather than hard-deleted from local derived files.

### Saved item origin type

Current values:

- `feed_saved`
- `manual_url`
- `markdown_upload`
- `html_upload`
- `singlefile_html_upload`

Reserved future values should remain explicit, for example `external_saved` or `automation_capture`. The current implementation does not poll external favorites or automation sources. File-backed external sources are local user-supplied files and do not imply a source URL or external platform identity, though the UI and evidence package classify them together with manual URL saves as `external_source`.

### Archive artifact projection status

`SavedItemDetailRead.archive_artifacts` is an API/view-model projection over saved snapshots and saved item assets. It is not a database table.

- `available`: the backing snapshot or asset exists in PostgreSQL and has a content URL.
- `unavailable`: the expected core artifact is missing; the detail UI should show the missing state instead of hiding the row.

Current detail artifact kinds:

- `singlefile_html_v1`
- `crawl4ai_raw_markdown_v1`
- `crawl4ai_fit_markdown_v1`
- `crawl4ai_fit_image_reinserted_markdown_v1`
- `uploaded_source_material_v1`
- `uploaded_markdown_v1`
- `page_image_v1`

For `page_image_v1`, the detail projection exposes only distinct assets referenced by the latest current markdown snapshots: raw markdown, native fit markdown, and image-reinserted fit markdown. Older snapshot references remain queryable in PostgreSQL for history/replay, but they are not part of the current detail artifact list.

### Mirror manifest

Each saved-item mirror writes `KnowledgeBase/{title-slug}--{urlhash}/.infoflow/manifest.json` by default. The root is the effective `knowledge_base_root`.

Current manifest contract:

- `manifest_version = knowledge_base_manifest_v1`
- top-level saved item metadata: id, title, nullable canonical URL, source kind, source URL, upload metadata, created/saved timestamps, capture run id, and requested mirror targets
- `files[]` entries for raw markdown, native fit markdown, image-reinserted fit markdown, uploaded markdown, uploaded source material, SingleFile HTML, and page image assets referenced by the exported snapshots
- each `files[]` entry includes `role`, `relative_path`, `source_kind`, `source_id`, `byte_size`, `sha256`, expected byte/hash values, and `verification_status`
- snapshot-backed files include `snapshot_id`, `snapshot_kind`, and `mime_type`
- asset-backed files include `asset_id`, `asset_key`, `asset_kind`, `mime_type`, and `source_url`
- article directories use `{title-slug}--{urlhash}`
- markdown files live directly under the article directory and use original-title filenames such as `原标题_raw.md`, `原标题_fit.md`, and `原标题_fit-images.md`
- image assets live under article-level `assets/`; only exported snapshot-referenced images are written
- image asset filenames use `{title-file-stem}--{urlhash}_{capture-or-logical-name}.{ext}` for both manual URL and RSS feed saved items
- exported markdown rewrites database `ifa-asset://` references to relative `assets/<filename>` paths
- SingleFile HTML lives directly under the article directory and uses `原标题.html`
- removed saved items export to `KnowledgeBase/.archive/{title-slug}--{urlhash}/...` using the same internal file layout

Uploaded material exports add two current-view files when available: the original `uploaded_source_material_v1` asset and the derived `uploaded_markdown_v1` snapshot. These files are derived mirror outputs; PostgreSQL remains canonical.

The manifest is a derived locator for KnowledgeBase mirror files. It is not a canonical data store.

## Snapshot Kinds

| Kind | Stored in | Meaning |
| --- | --- | --- |
| `manual_url_capture_input_v1` | `saved_snapshots` | Initial manual URL capture payload |
| `capture_page_metadata_v1` | `saved_snapshots` | JSON metadata for captured page title, tool-derived titles, feed title, title source, user-title presence, and whether capture updated the saved item title |
| `canonical_content_v1` | `saved_snapshots` | App-readable canonical saved content |
| `original_page_html_v1` | `saved_snapshots` | Legacy best-effort original page HTML |
| `crawl4ai_raw_markdown_v1` | `saved_snapshots` | More complete Crawl4AI markdown; exposed through the archive snapshot content endpoint |
| `crawl4ai_fit_markdown_v1` | `saved_snapshots` | Cleaner markdown for AI/search/wiki workflows; exposed through the archive snapshot content endpoint |
| `crawl4ai_fit_image_reinserted_markdown_v1` | `saved_snapshots` | Fit markdown after deterministic image reinsertion from raw markdown/raw HTML using persisted `page_image_v1` assets and `saved_snapshot_asset_refs`; exposed through the archive snapshot content endpoint |
| `uploaded_markdown_v1` | `saved_snapshots` | Markdown body derived from user-uploaded Markdown, normal HTML, or SingleFile HTML; exposed through the archive snapshot content endpoint and preferred for saved item insight input |

## Asset Kinds

| Kind | Stored in | Meaning |
| --- | --- | --- |
| `singlefile_html_v1` | `saved_item_assets` | High-fidelity SingleFile HTML archive; served inline with sandbox CSP or as a download |
| `uploaded_source_material_v1` | `saved_item_assets` | Original user-uploaded Markdown/HTML/SingleFile HTML source file bytes; linked from `saved_items.source_asset_id` and served through the asset content endpoint |
| `page_image_v1` | `saved_item_assets` | Image asset discovered from markdown/page capture; reused per saved item by content `sha256` when recapturing and served through the asset content endpoint |

### Page Image Retention

Recapturing the same saved item reuses an existing `page_image_v1` asset when the captured bytes hash to the same `sha256`. New markdown snapshots may reference the reused asset through fresh `saved_snapshot_asset_refs`.

After a capture persists its latest markdown snapshots, cleanup deletes only `page_image_v1` assets that have no snapshot reference at all. Assets still referenced by historical snapshots are retained for replay/export history even when they are no longer exposed by the current detail projection or Local Mirror export.

## Capture Targets

Current manual capture/retry pipeline targets:

- `singlefile_html_v1`
- `crawl4ai_raw_markdown_v1`
- `crawl4ai_fit_markdown_v1`
- `crawl4ai_fit_image_reinserted_markdown_v1`

Requested targets are persisted in `SavedItemCaptureRun.requested_targets_json`.

Current capture trigger kinds:

- `manual_url_capture`
- `manual_retry`
- `feed_saved`
- `feed_saved_retry`

Feed save and retry must capture the saved item's canonical URL, not the RSS content body.

Uploaded material saves do not use web capture run trigger kinds. They store original source material as `uploaded_source_material_v1`, derive `uploaded_markdown_v1`, and record processing status directly on `saved_items`. Retry through the operation capture retry path returns 400 for upload origins because there is no web capture pipeline to rerun.

Crawl4AI markdown image references that resolve to unsupported non-image content types, such as `text/html`, produce a `crawl4ai_image_unsupported_type` warning and skip that image asset. That warning must not downgrade the markdown capture to `failed` or `partial_failed`; true image download failures still follow the existing error semantics.

## AI Artifact Kinds

Current feed-item artifact kinds:

- `feed_item_summary_v1`
- `feed_item_topics_v1`
- `feed_item_ranking_v1`
- `feed_item_routing_v1`

Current saved-item artifact kinds:

- `saved_item_insight_v1`

Artifacts are append-only and resolved by latest valid payload from `insight_engine_artifacts`. Invalid or absent payloads fall back to deterministic display rules. Feed item summary payloads are cleaned both when old artifacts are read and when new artifacts are generated; URL-only jump prompts are treated as absent summaries. Feed item artifact rebuild and missing-build paths must skip a feed item when the current body is still URL-only or a jump prompt and `content_extraction_status = failed`; the skipped item counts as a failed feed item for build stats, and existing historical artifacts are not automatically removed. Saved item insight payloads are resolved only for saved item details and evidence export; user-curated saved item revisions and feed item structured feedback are stored in dedicated human-evidence tables, not in `insight_engine_artifacts`.

The physical table is `insight_engine_artifacts`. Current feed-item artifacts use `feed_item_*_v1` kinds; the AI homepage is a read model over those artifacts, and legacy `homepage_*_v1` names are historical inputs only. Saved item insight uses `saved_item_insight_v1` on the same store through `saved_item_id`:

- AI-generated summary/topic payloads use `source_kind = ai_generated`.
- `feed_item_topics_v1` payloads may include `topic_ids`, `labels`, `content_type`, `content_type_label`, and `source_kind`. `labels` are the controlled Simplified Chinese labels derived from `topic_ids`; older labels-only legacy artifacts remain readable.
- `feed_item_summary_v1` payloads store the summary only. The current runtime contract does not include `key_points`.
- `feed_item_ranking_v1` payloads may include `priority_score`, `priority_band`, `priority_reasons`, `low_priority_bucket`, `feature_schema_version = insight_feature_v1`, `score_version = score_v1_hand_weighted`, grouped `feature_vector`, and `score_breakdown`. The grouped feature vector is the canonical audit payload; the flat scoring view is derived from it.
- `feed_item_routing_v1` payloads may include `primary_channel_id`, `secondary_channels[]` with confidence, `reading_depth`, `flow_lane`, `confidence`, `reasons`, `channel_catalog_version`, and `routing_breakdown`. Channel ids must reference enabled known channels when the artifact is accepted. Legacy `homepage_routing_v1` schema version values are not accepted as current v3 runtime artifact schema.
- `insight_engine_artifacts.model_name` records `provider/model`, for example `deepseek/deepseek-v4-flash`, `openai-compatible/gpt-4.1-mini`, or `lm-studio/qwen/qwen3.5-9b`.
- `insight_engine_artifacts.prompt_version` records `insight-engine-v2-routing-v1` for current feed item Insight Engine artifacts.
- `saved_item_insight_v1` rows use `insight_engine_artifacts.saved_item_id`, set `feed_item_id` to null, and record `prompt_version = saved-item-insight-v1`.
- Feed item artifact rows use `insight_engine_artifacts.feed_item_id`, set `saved_item_id` to null, and are rejected by DB constraints if they are written against a saved item owner.
- The Insight Engine artifact builder writes only missing feed item artifact kinds for a feed item. Existing kinds are not overwritten by source sync or the missing-build endpoint. The one-item rebuild endpoint is the explicit force-regeneration path and appends new latest rows while preserving older artifacts as history.
- The shared request builder sends only title/source/content context to the external AI provider: `Title`, `Source name`, `Source folder`, and normalized `Content`. URL, published time, reader status, and a separate RSS summary field are excluded from the runtime prompt. Oversized normalized content fails with `content_too_large_for_provider_budget` rather than being silently truncated.
- The shared request builder injects slim taxonomy/content type/reason-code contexts, slim active Insight Profile context, active dynamic Insight Channel context, active source channel tendencies, and source flow policy. Old `reserved_for_phase_4` / `reserved_for_profile_v1` placeholders should no longer appear in real generation payloads or Settings request previews; contract status/version metadata and profile id/version/schema/status metadata should not be sent to AI providers.
- LM Studio is an explicit provider value, not an alias hidden behind `openai-compatible`. Feed item artifact generation and request preview send the `feed_item_insight` JSON Schema; saved item insight rebuild sends the `saved_item_insight` JSON Schema; Settings connection tests send the `connection_test` JSON Schema. Schema rejection does not create placeholder artifacts.
- AI-generated `topics` are controlled taxonomy ids. Unknown topic ids or labels fall back to `uncategorized` / `待确认`.
- AI-generated `content_type` is a controlled content type id. Unknown values fall back to `unknown` / `未确定`.
- AI-generated `priority_reasons[].code` must be in `reason_codes_v1.ai_allowed_codes`. Backend/profile-only reason codes such as `positive_source_history`, `negative_source_history`, and `profile_focus_match` are reserved for later scoring phases, are not accepted directly from AI output, and are not exposed in the slim reason-code prompt context.
- Accepted reason labels are replaced from the contract and reason weights are clamped to `0..100`. If no valid AI-allowed reason code remains after filtering, summary/topic/ranking output is invalid and no fake/partial rows for those kinds are written. Routing is validated independently; invalid, disabled, retired, unknown, or omitted channel references fall back to the locked `needs_review` channel with explicit reason metadata rather than silently accepting a stale/default id.
- `GET /api/settings/ai/insight-request-preview` uses the same builder for inspection only. It does not call AI, write `insight_engine_artifacts`, create placeholder artifacts, or expose API keys/authorization headers.
- Source sync does not call the builder inline. Each successful `source_sync_one_source` job may create or reuse a linked `feed_item_insight_artifacts/source_sync_created_feed_items` operation and enqueue one `feed_item_insight_artifact_build` job per selected newly created feed item id. Source sync itself ingests at most the latest 50 RSS/Atom entries per source. The default automatic artifact cap is 20 items per sync, but API callers can pass `feed_item_insight_artifact_build_limit = null` to process all newly created ids after the ingestion cap or `0` to skip artifact build generation.
- The builder chooses the provider profile from the latest-occurrence source `ai_route`. `sensitive` does not fall back to `general` when its profile is incomplete; it skips generation.
- If AI is disabled, incomplete, unavailable, or returns invalid JSON after the single malformed-JSON repair retry, no fake artifact is written and homepage reads fall back to deterministic display data. Failed attempt snapshots may include sanitized parse/repair metadata. Final priority still comes from Backend Scoring v1 when homepage/evidence projections are built.
- If feed item extraction failed and the body remains URL-only or a jump prompt, feed item artifact builders skip generation instead of producing summaries that appear to have read the full article.
- Homepage card routing fields resolve from latest valid `feed_item_routing_v1` first, then deterministic fallback from active source channel tendencies and source flow policy. `high_frequency_brief` fallback routes to `brief_pool`; `normal` and `mixed_high_frequency` fallback to `main` unless later routing logic changes flow-lane semantics. `channel_lifecycle` routing artifacts are append-only derived reroute records created during channel retirement and are read like other current routing artifacts.

## Frontend View Model

`frontend/src/types/feed.ts` defines `FeedItem`, the unified UI view model.

Important distinction:

- In AI/reader views, `FeedItem.id` is the backend `feed_item_id`.
- In knowledge view, `FeedItem.id` is the backend `saved_item_id`.
- Use `feedItemId` and `savedItemId` when actions must map across views.
- AI/reader `FeedItem.sourceId` comes from the latest-occurrence source projection and is used for source-list filtering and source-level bulk seen cleanup actions.
- AI/reader/homepage `FeedItem` carries `contentTextSource`, `contentExtractionStatus`, `contentExtractionAttemptedAt`, `contentExtractedAt`, and `contentExtractionError` for detail status display and manual extraction behavior.
- AI/reader/homepage `FeedItem` carries `insightFeedbackText` for free-form item-level user feedback and `structuredFeedback` for structured value/priority/topic/content-type/reason-code corrections.
- Homepage `FeedItem.prioritySource` may be `insight_engine_artifact`, `deterministic_fallback`, or `backend_scoring_v1`; current homepage priority sorting uses `backend_scoring_v1`.
- Homepage `FeedItem` carries routing projection fields `primaryChannelId`, `secondaryChannels`, `readingDepth`, `flowLane`, `routingConfidence`, `routingSource`, and `routingReasons`. Channel/flow facets are built from the active channel catalog and current routing ids, while retired channels remain historical/read-only. Classic Reader ignores these fields for visible grouping and badge display.
- Knowledge `FeedItem.sourceId` is currently `null`; knowledge source metadata remains display-only until a later contract explicitly links saved items back to source registry identity.
- Knowledge detail `FeedItem.archiveArtifacts` mirrors backend `SavedItemDetailRead.archive_artifacts` and is populated only when a saved item detail response has been loaded.
- Knowledge `FeedItem.captureTitleMetadata` mirrors backend `SavedItemCaptureRead.title_metadata` and is derived from the latest `capture_page_metadata_v1` snapshot.
- Archive artifact `previewUrl` and `downloadUrl` point to API content endpoints. They are not `KnowledgeBase/` or `llm_wiki/` paths and should not be treated as local mirror locators.
- `ArchiveArtifactKind` includes `crawl4ai_fit_image_reinserted_markdown_v1`, `uploaded_source_material_v1`, and `uploaded_markdown_v1`; detail UI treats the Crawl4AI and uploaded Markdown kinds as markdown preview/download artifacts, and the uploaded source material kind as the original file asset.

## Schema Notes

- `saved_snapshots.snapshot_kind` is `String(64)`. The length is required for `crawl4ai_fit_image_reinserted_markdown_v1`.
- `insight_profiles.schema_version` is `String(64)`, `profile_name` is `String(255)`, `profile_version` is `String(64)`, and `profile_json` / `insight_profile_events.event_json` are JSON columns. Profile versions are append-only; active state is derived from events rather than an `is_active` flag.
- `insight_engine_artifacts` is the physical table and current code terminology for derived AI/Insight Engine rows. Frozen historical material may mention earlier artifact names.
- `insight_channels.id` is a stable string primary key. Channel removal is retire-only rather than hard delete; source tendencies and accepted historical `feed_item_routing_v1` artifacts may reference retired ids for audit/history until rerouted or regenerated.
- `source_channel_tendencies` has a unique `(source_id, channel_id)` constraint and stores `tendency` as a float in `0..1`.
- `sources.flow_policy` is `String(64)`, non-null, defaults to `normal`, and is validated by API as `normal | mixed_high_frequency | high_frequency_brief`.
- `app_settings.key` is unique. Current supported keys include `knowledge_base_root`, `rsshub_base_url`, `auto_low_priority_archiving_enabled`, `low_priority_score_threshold`, `ai_general_provider`, `ai_general_enabled`, `ai_general_base_url`, `ai_general_model`, `ai_general_deepseek_thinking`, `ai_general_artifact_concurrency`, `ai_general_api_key`, `ai_sensitive_provider`, `ai_sensitive_enabled`, `ai_sensitive_base_url`, `ai_sensitive_model`, `ai_sensitive_deepseek_thinking`, `ai_sensitive_artifact_concurrency`, and `ai_sensitive_api_key`. Supported provider values stored in `ai_*_provider` are `deepseek`, `openai-compatible`, and `lm-studio`. v3 runtime no longer reads old flat `ai_provider`, `ai_enabled`, `ai_base_url`, `ai_model`, `ai_deepseek_thinking`, `ai_artifact_concurrency`, or `ai_api_key` fallback rows. Storage and RSSHub DB values take precedence over `.env`; AI API keys are resolved from env overrides first, then DB-backed encrypted settings; other AI and Insight Engine settings are DB/default backed in the current implementation.
- Manual URL captures without a user-provided title may store a display title derived from the captured page title plus source domain, such as `Captured Title - pseudoyu.com`. This is saved item title normalization behavior, not a schema change; user-provided titles remain authoritative.
- New databases apply the single v3 fresh baseline `20260601_v3_0_0_baseline`.
- Existing v2 development or production databases are not supported by the current Alembic path. Export subscriptions to OPML, create a fresh supported database, import OPML through preview + commit, and keep the old PostgreSQL dump for rollback or manual recovery.
- Do not fake cancel-save by deleting saved item rows; use `saved_items.removed_at`.
- Do not fake behavior history by re-deriving it from current `item_states` during export; use `feed_item_behavior_events`.
- Do not fake origin signals by inferring from Local Mirror paths or user notes; use saved item origin fields.
- Do not store structured feedback or undo semantics in `insight_engine_artifacts`; use the dedicated human-evidence tables.
- Do not fake source icons by storing image bytes in PostgreSQL, source import summaries, Local Mirror, or `insight_engine_artifacts`.
- Do not fake uploaded file identity by forcing `canonical_url`; use nullable URL plus uploaded source material metadata.

## Migration Rule

If a table, field, status value, snapshot kind, asset kind, or artifact kind changes:

1. Add an Alembic migration under `backend/alembic/versions/`.
2. Update `backend/app/db/models.py`.
3. Update Pydantic schemas and frontend DTO types if the value crosses API boundaries.
4. Update this document and `docs/api.md` when exposed through HTTP.
5. Add or update tests before relying on manual verification.
