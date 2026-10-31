# Product Boundaries

## Product Goal

InfoFlowAggregator is a personal information system, not only an RSS reader.

The core loop is:

```text
subscription ingestion -> AI homepage -> reading/state feedback -> capture inbox/favorites -> knowledge base -> preference-aware aggregation
```

The primary user is the project owner. Single-user correctness, recoverability, and maintainability matter more than premature multi-user scale.

## Current Product Shape

- Default home: AI homepage.
- Secondary view: classic reader.
- Knowledge base: saved items, manual URL captures, uploaded Markdown/HTML/SingleFile HTML captures, archive artifacts, search, notes, and future LLM Wiki bridge.
- Main UI shell: three-pane reader-style layout:
  - left: grouping mode and filters;
  - center: AI-processed item cards or saved item list;
  - right: in-app detail/reading pane.

## MVP Scope Already Established

In scope first:

- OPML import with folder hierarchy.
- RSS and RSSHub subscriptions.
- Source preview, source registry, manual refresh, and refresh history.
- Feed item normalization and deduplication.
- RSS short-content extraction for item bodies that are URL-only, jump prompts, or very short summaries, with short-form platform feed text treated as complete body content when RSSHub/feed output already contains non-URL, non-jump readable text.
- AI homepage with deterministic fallbacks and artifact-backed summaries/topics/ranking/routing, including feature/score/routing audit metadata.
- DB-driven Insight Channels with active/disabled/retired lifecycle state and locked `needs_review` fallback.
- DB-backed AI provider profile settings for DeepSeek, OpenAI-compatible, and LM Studio providers, with backend-only API key handling and a connection test.
- Missing feed item Insight Engine artifact generation for the current summary/topic/ranking/routing artifact kinds.
- Classic reader list/detail.
- Item states: `unread`, `browsed`, `seen`, `read_full`, `saved`, `not_interested`.
- Feed item free-form and structured human feedback, including reversible `not_interested` and auto low-priority correction.
- Saved items as the knowledge base/capture inbox canonical entity.
- Saved item origin typing, AI generated saved item insight, user-curated saved item insight revisions, and curated insight edit events.
- Insight Evidence Package v2 export of feed item structured feedback/free-form notes, behavior clear/undo/auto-low-priority-correction events, latest or all-history artifact evidence, failed/skipped attempt summaries, saved item evidence, selected input text, display Markdown with referenced images, AI generated saved item insight, curated revisions, edit events, notes, and capture metadata.
- Insight Profile draft import and read-only inspection for offline candidate review before saving or activating a new profile version.
- External `profile_candidate` inspect and selected Source Profile draft import, with hostile-input validation, dry-run review, per-source field diffs, display-only channel prompt/boundary suggestions, and no automatic activation.
- Manual URL capture for non-RSS saved items.
- Uploaded Markdown, normal HTML, and SingleFile HTML capture for local saved materials.
- Feed-based save automatically starting the archive capture pipeline after the saved state is committed.
- SingleFile HTML archive asset.
- Crawl4AI raw/fit/image-reinserted markdown snapshots and page image assets.
- Local `KnowledgeBase/` mirror export from PostgreSQL.
- Saved-item search across title, note, canonical URL, and latest canonical content.

## Explicit Non-Goals For Now

- Multi-user permissions.
- Native mobile app.
- External scheduler or worker runtime.
- Semantic search and embeddings as a required first milestone.
- PDF/OCR upload and media playback in the current knowledge upload phase.
- Platform favorites sync as a day-one feature.
- Reverse sync from the aggregator back to external platforms.
- Treating `KnowledgeBase/` or `llm_wiki/` as a canonical store.
- Hard-delete style AI filtering.
- Making n8n a required runtime dependency.

## Compatibility And Migration Boundaries

- The supported database path is a fresh v3 database plus OPML subscription import. In-place upgrade of old v2 production databases is not supported.
- OPML is subscription portability only. A PostgreSQL dump is required for a complete backup of saved knowledge, state, settings, raw material, and diagnostics.
- Current callers use the operation-backed source sync, homepage, artifact, and Knowledge long-running routes documented in [`api.md`](api.md). Removed synchronous and compatibility routes are not supported inputs.
- Source deletion requires impact preview plus confirmed deletion with a current preview token. Saved Knowledge items and shared feed items are retained according to [`data.md`](data.md).
- Failed saved-item capture can be repaired by importing local Markdown, HTML, or SingleFile HTML through a job-backed operation. A failed replacement import preserves the last successful current material.
- PDF/OCR upload, media playback, token/cost accounting, provider-native SDKs, semantic search, embeddings, external platform sync, LLM Wiki implementation, formal model training, multipart dependency changes, Redis as an application cache/job broker, and deployment role split are not implemented.

## Business Rules

### Source And Feed

- `(kind, url)` identifies a source and must stay unique.
- OPML import preserves folder hierarchy where possible.
- Feed normalization matches existing items by external ID, canonical URL, then title plus published time.
- Raw source payloads and raw entry JSON are kept for debugging and replay.
- Feed sync preserves RSS/Atom content candidates in `feed_content_text` before any page extraction attempt.
- Feed sync must not call AI providers or the Insight Engine artifact builder inline. When callers allow automatic feed item insight work, each successful `source_sync_one_source` job creates or reuses a linked visible `feed_item_insight_artifacts/source_sync_created_feed_items` operation and enqueues per-item artifact build jobs for that source's newly created items. Omitted or `null` `feed_item_insight_artifact_build_limit` processes all newly created items; `0` is fetch-only, and a positive integer caps the automatic feed item Insight Engine artifact item count across the linked build. Normal feed-item artifact build and rebuild operations use the same per-item builder/job path.
- Source sync callers must use `/api/operations/source-sync` so browser refresh can restore progress. The old synchronous source sync endpoints are removed.
- Newly created `FeedItem` rows must receive their initial `ItemState` in the same transaction. Non-null `feed_items.canonical_url` is protected by a PostgreSQL partial unique index.
- RSS short-content extraction is item-level only. It must not create source-level extraction preferences or crawler-source behavior.
- Extraction failure must not overwrite the existing RSS-derived `content_text`.
- Forced re-extraction quality gates must compare against preserved feed input, not against a previously extracted page body.
- Short-form platform feed text from `t.me`, `telegram.me`, `x.com`, `twitter.com`, and `okjike.com` is considered the complete body when non-URL, non-jump visible feed/RSSHub text exists; automatic extraction should return `not_needed` with reason `short_form_feed_content` instead of fetching the platform page.
- Manual extraction debug data should expose followed in-page redirects as `fetch.html_redirects` and URL recovery attempts as `fetch.url_fallbacks` so source-page redirect and fetch-recovery behavior can be diagnosed without a new table.
- Insight Engine artifact generation should write only validated structured payloads. Invalid JSON, unsupported provider settings, provider request failures, incomplete configuration, or rejected LM Studio JSON Schema requests should leave missing artifacts absent. Malformed JSON gets at most one immediate repair retry before normal job retry policy. Routing output is validated independently from summary/topics/ranking: invalid, omitted, unknown, disabled, or retired channel references fall back to `needs_review` with explicit reason metadata rather than silently accepting stale catalog ids.
- Feed item ranking artifacts may store grouped `InsightFeatureVector` snapshots, deterministic flat score contribution views, `feature_schema_version`, and `score_version`. These are recomputable derived audit data and must not become a second canonical score table.

### Reading State

- `unread` is the default state.
- Opening an unread feed item in the UI marks it `browsed`.
- `seen` means the user explicitly processed the item from title/summary without claiming full reading.
- `read_full`, `saved`, and `not_interested` are explicit user signals stronger than `seen`.
- Opening the original link is treated as full reading because the app cannot observe external-page reading progress after the link leaves the in-app reader.
- Current status priority is `not_interested > saved > read_full > seen > browsed > unread`.
- `not_interested` means low-priority and reviewable, not deleted.
- Undoing `not_interested` removes the negative scoring contribution and must not be treated as a positive signal.
- Automatic low-priority archiving is optional and defaults off. A user correction with `priority_correction = too_low` clears only automatic score-threshold bucketing; it must not override current `not_interested`.
- Structured feedback is canonical human evidence in `feed_item_structured_feedback`, including channel/routing corrections. New channel/routing corrections must target active channels; historical disabled/retired references stay readable for evidence. Structured feedback must not be written into `insight_engine_artifacts` or silently mutate Insight Profile versions.

### Knowledge Base

- `saved_items` is the canonical saved/capture entity.
- A feed-based saved item has `feed_item_id != null`.
- A manual URL capture has `feed_item_id = null`.
- Saved item `origin_type` is the explicit source signal. Current values are `feed_saved`, `manual_url`, `markdown_upload`, `html_upload`, and `singlefile_html_upload`.
- Uploaded saved items may have `canonical_url = null`; original filename, MIME type, source material asset, and processing state carry the local-file identity.
- `POST /api/reader/items/{id}/mark_saved` creates or reuses a feed-based saved item, records the canonical snapshot, updates reader saved state, creates a pending `feed_saved` capture run, and returns before the full archive pipeline finishes.
- Feed-based save capture then runs through a job-backed `knowledge_capture/feed_saved_capture` operation. Its main capture job performs best-effort original-page snapshot, SingleFile, Crawl4AI, and title metadata against the saved item's canonical URL, then enqueues a Local Mirror follow-up job.
- Manual URL capture, upload processing, capture retry, and Local Mirror re-export should use `/api/operations/*` entrypoints in the main frontend. SingleFile URL capture should prefer light-themed archives through `prefers-color-scheme: light` media emulation plus pre-capture light-theme storage/DOM hints; site-specific hard-coded dark themes may still need later per-site handling.
- Capture retry is allowed for both manual and feed-based saved items; retry trigger kind records the origin.
- Uploaded material capture stores the original file as `uploaded_source_material_v1`, derives `uploaded_markdown_v1`, and does not use the URL capture pipeline. HTML/SingleFile HTML upload conversion uses the existing local uploaded HTML/Crawl4AI capability, not `trafilatura`.
- Capture retry is not available for uploaded saved items.
- User-provided manual titles have display priority over captured page titles. Captured title metadata is preserved in `capture_page_metadata_v1`.
- Saved item insight generation is explicit and uses archive/user-note metadata after a saved item exists. It must not block save, manual capture, or retry completion.
- Saved item insight rebuild, feed item extraction, and feed item insight artifact rebuild/build-missing are long operations in the main frontend and should use backend operation entrypoints rather than depending on a DetailPane request lifecycle.
- AI generated saved item insight is append-only derived data in `insight_engine_artifacts`; user curated saved item insight is separate human-authored canonical revision data.

### Archive And Mirror

- PostgreSQL is the canonical store.
- `KnowledgeBase/` is a rebuildable local mirror, not a source of truth.
- `llm_wiki/` is reserved for future true LLM Wiki output and is not the Local Mirror destination.
- Database archive success is not rolled back if local mirror export fails.
- Users can manually re-export one saved item to the local mirror through the knowledge detail flow.
- `singlefile_html_v1` is an asset, not normal readable text.
- `uploaded_source_material_v1` is the original uploaded file asset.
- `uploaded_markdown_v1` is the derived readable upload snapshot and is preferred for uploaded saved item reading/insight input.
- `crawl4ai_raw_markdown_v1`, `crawl4ai_fit_markdown_v1`, and `crawl4ai_fit_image_reinserted_markdown_v1` are text snapshots.
- Markdown image references inside PostgreSQL use `ifa-asset://<asset_key>`.
- Knowledge detail archive availability is exposed through detail-only `archive_artifacts`, not a new canonical table.
- Repeated capture of the same saved item should reuse identical `page_image_v1` bytes by `sha256`; historical snapshot-referenced images are retained, while current detail/mirror image exposure follows the latest markdown snapshot refs.
- App asset reads go through `GET /api/knowledge/assets/{asset_key}/content`; `download=true` requests attachment behavior.
- Crawl4AI raw/fit/image-reinserted markdown reads go through `GET /api/knowledge/saved-items/{saved_item_id}/snapshots/{snapshot_kind}/content`; unsupported snapshot kinds still fail clearly.
- Local export rewrites asset refs to relative file paths.
- Local mirror article directories use `{title-slug}--{urlhash}` under `KNOWLEDGE_BASE_ROOT`; `saved_item_id` remains the DB/API identity.
- Local mirror exports keep markdown and SingleFile HTML directly under each article directory, with referenced images under `assets/` and derived manifest metadata under `.infoflow/manifest.json`.
- Mirror manifests use `manifest_version = knowledge_base_manifest_v1` and include per-file relative paths, source ids, byte/hash metadata, verification status, and snapshot/asset metadata.
