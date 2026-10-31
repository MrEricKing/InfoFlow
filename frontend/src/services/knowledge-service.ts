import type {
  ArchiveArtifact,
  CaptureTitleMetadata,
  CaptureToolCall,
  FeedItem,
  KnowledgeSavedItemDetailDto,
  KnowledgeSavedItemListDto,
  SavedItemAIInsight,
  SavedItemAIInsightDto,
  SavedItemCuratedInsight,
  SavedItemCuratedInsightDto,
  SavedItemCuratedInsightEvent,
  SavedItemCuratedInsightEventDto,
  SavedItemCuratedInsightInput,
  SavedItemInsightPayload,
  SavedItemInsightPayloadDto,
  SavedItemResolvedInsight,
  SavedItemResolvedInsightDto,
  SavedItemRemoveResponseDto,
} from '@/types/feed';
import {buildApiContentUrl} from '@/lib/api-url';
import {requestJson} from '@/api/client';
import {sourceCategoryIdFromFolderPath, sourceCategoryLabelFromFolderPath} from '@/lib/source-category';

export async function listSavedItems(options?: {limit?: number; query?: string}) {
  const params = new URLSearchParams();
  if (options?.limit !== undefined) {
    params.set('limit', String(options.limit));
  }
  const query = options?.query?.trim();
  if (query) {
    params.set('query', query);
  }

  const queryString = params.toString();
  const items = await requestJson<KnowledgeSavedItemListDto[]>(
    `/knowledge/saved-items${queryString ? `?${queryString}` : ''}`,
  );
  return items.map((item) => adaptSavedItem(item));
}

export async function getSavedItemDetail(savedItemId: number) {
  const item = await requestJson<KnowledgeSavedItemDetailDto>(`/knowledge/saved-items/${savedItemId}`);
  return adaptSavedItem(item);
}

export async function updateSavedItemNote(savedItemId: number, userNote: string | null) {
  const item = await requestJson<KnowledgeSavedItemDetailDto>(`/knowledge/saved-items/${savedItemId}`, {
    method: 'PATCH',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({user_note: userNote}),
  });
  return adaptSavedItem(item);
}

export async function updateSavedItemCuratedInsight(savedItemId: number, input: SavedItemCuratedInsightInput) {
  const item = await requestJson<KnowledgeSavedItemDetailDto>(`/knowledge/saved-items/${savedItemId}/curated-insight`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      payload: buildSavedItemInsightPayloadDto(input.payload),
      edit_note: input.editNote,
    }),
  });
  return adaptSavedItem(item);
}

export async function clearSavedItemCuratedInsight(savedItemId: number) {
  const item = await requestJson<KnowledgeSavedItemDetailDto>(`/knowledge/saved-items/${savedItemId}/curated-insight`, {
    method: 'DELETE',
  });
  return adaptSavedItem(item);
}

export async function listSavedItemCuratedInsightEvents(savedItemId: number) {
  const events = await requestJson<SavedItemCuratedInsightEventDto[]>(
    `/knowledge/saved-items/${savedItemId}/curated-insight/events`,
  );
  return events.map(adaptSavedItemCuratedInsightEvent);
}

export async function removeSavedItem(savedItemId: number) {
  return requestJson<SavedItemRemoveResponseDto>(`/knowledge/saved-items/${savedItemId}/remove`, {
    method: 'POST',
  });
}

function adaptSavedItem(item: KnowledgeSavedItemListDto | KnowledgeSavedItemDetailDto): FeedItem {
  const sourceAppUrl = item.source_site_url ?? item.source_url ?? item.canonical_url ?? '';
  const sourceAppName = buildSourceAppName(sourceAppUrl, item.source_kind);
  const sourceMaterialArtifact = item.source_material_artifact
    ? adaptArchiveArtifact(item.source_material_artifact)
    : null;
  const captureToolCalls: CaptureToolCall[] =
    'capture_tool_calls' in item
      ? item.capture_tool_calls.map((toolCall) => ({
          id: toolCall.id,
          toolName: toolCall.tool_name,
          toolVersion: toolCall.tool_version,
          status: toolCall.status,
          exitCode: toolCall.exit_code,
          errorSummary: toolCall.error_summary,
          startedAt: toolCall.started_at,
          finishedAt: toolCall.finished_at,
        }))
      : [];
  const archiveArtifacts: ArchiveArtifact[] | undefined =
    'archive_artifacts' in item
      ? item.archive_artifacts.map(adaptArchiveArtifact)
      : undefined;

  const displaySummary = buildSavedItemDisplaySummary(item);

  return {
    id: item.id,
    feedItemId: item.feed_item_id,
    savedItemId: item.id,
    sourceId: item.source_id,
    title: item.title,
    summary: displaySummary,
    content: 'content_html' in item ? item.content_html ?? '' : '',
    sourceName: item.source_name,
    sourceAppName,
    sourceFolder: sourceCategoryLabelFromFolderPath(item.source_folder_path),
    sourceCategoryId: sourceCategoryIdFromFolderPath(item.source_folder_path),
    publishedAt: item.published_at ?? item.saved_at,
    topics: [],
    status: item.state.status,
    relevanceScore: null,
    priorityBand: null,
    priorityReasons: [],
    isLowPriorityBucket: item.state.status === 'not_interested',
    lowPriorityBucketReason: item.state.status === 'not_interested' ? 'not_interested' : null,
    autoLowPriorityCleared: false,
    originalUrl: item.original_url ?? item.canonical_url ?? '',
    sourceAppUrl,
    contentTextSource: null,
    contentExtractionStatus: 'not_checked',
    contentExtractionAttemptedAt: null,
    contentExtractedAt: null,
    contentExtractionError: null,
    browsedAt: item.state.browsed_at,
    seenAt: item.state.seen_at,
    readFullAt: item.state.read_full_at,
    savedAt: item.state.saved_at ?? item.saved_at,
    notInterestedAt: item.state.not_interested_at,
    note: item.user_note,
    originType: item.origin_type,
    aiRoute: item.ai_route,
    originalFilename: item.original_filename,
    mimeType: item.mime_type,
    processingStatus: item.processing_status,
    processingError: item.processing_error,
    sourceMaterialArtifact,
    resolvedInsight: item.resolved_insight ? adaptSavedItemResolvedInsight(item.resolved_insight) : null,
    aiGeneratedInsight:
      'ai_generated_insight' in item && item.ai_generated_insight
        ? adaptSavedItemAIInsight(item.ai_generated_insight)
        : null,
    curatedInsight:
      'curated_insight' in item && item.curated_insight ? adaptSavedItemCuratedInsight(item.curated_insight) : null,
    hasCanonicalSnapshot: item.snapshots.has_canonical_snapshot,
    hasOriginalPageSnapshot: item.snapshots.has_original_page_snapshot,
    hasSinglefileHtml: item.snapshots.has_singlefile_html,
    hasCrawl4aiRawMarkdown: item.snapshots.has_crawl4ai_raw_markdown,
    hasCrawl4aiFitMarkdown: item.snapshots.has_crawl4ai_fit_markdown,
    hasCrawl4aiFitImageReinsertedMarkdown: item.snapshots.has_crawl4ai_fit_image_reinserted_markdown,
    imageAssetCount: item.snapshots.image_asset_count,
    captureStatus: item.capture?.status ?? null,
    latestCaptureRunId: item.capture?.latest_run_id ?? null,
    captureErrorSummary: item.capture?.latest_error_summary ?? null,
    captureStartedAt: item.capture?.latest_started_at ?? null,
    captureFinishedAt: item.capture?.latest_finished_at ?? null,
    captureRetryable: item.capture?.retryable ?? false,
    captureTitleMetadata: item.capture?.title_metadata
      ? adaptCaptureTitleMetadata(item.capture.title_metadata)
      : null,
    captureToolCalls,
    localMirrorStatus: item.local_mirror?.status ?? null,
    latestLocalMirrorExportId: item.local_mirror?.latest_export_id ?? null,
    localMirrorErrorSummary: item.local_mirror?.latest_error_summary ?? null,
    localMirrorStartedAt: item.local_mirror?.latest_started_at ?? null,
    localMirrorFinishedAt: item.local_mirror?.latest_finished_at ?? null,
    localMirrorSourceCaptureRunId: item.local_mirror?.source_capture_run_id ?? null,
    localMirrorWrittenFileCount: item.local_mirror?.written_file_count ?? 0,
    localMirrorPaths: item.local_mirror
      ? {
          knowledgeBaseRoot: item.local_mirror.paths.knowledge_base_root,
          articleDir: item.local_mirror.paths.article_dir,
          rawMarkdown: item.local_mirror.paths.raw_markdown,
          fitMarkdown: item.local_mirror.paths.fit_markdown,
          fitImageReinsertedMarkdown: item.local_mirror.paths.fit_image_reinserted_markdown,
          uploadedMarkdown: item.local_mirror.paths.uploaded_markdown,
          manifest: item.local_mirror.paths.manifest,
          markdownAssetsDir: item.local_mirror.paths.markdown_assets_dir,
          singlefileHtml: item.local_mirror.paths.singlefile_html,
          sourceMaterial: item.local_mirror.paths.source_material,
        }
      : null,
    archiveArtifacts,
    summarySource: undefined,
    topicSource: undefined,
    prioritySource: undefined,
    artifactGeneratedAt: null,
  };
}

function buildSavedItemDisplaySummary(item: KnowledgeSavedItemListDto | KnowledgeSavedItemDetailDto) {
  const resolvedInsightSummary = item.resolved_insight?.payload.summary?.trim();
  if (resolvedInsightSummary) {
    return resolvedInsightSummary;
  }

  return item.summary_text?.trim() ?? '';
}

function adaptArchiveArtifact(artifact: KnowledgeSavedItemListDto['source_material_artifact']): ArchiveArtifact {
  if (!artifact) {
    throw new Error('Archive artifact payload is required');
  }
  return {
    artifactKind: artifact.artifact_kind,
    status: artifact.status,
    snapshotId: artifact.snapshot_id,
    assetId: artifact.asset_id,
    assetKey: artifact.asset_key,
    logicalName: artifact.logical_name,
    mimeType: artifact.mime_type,
    byteSize: artifact.byte_size,
    createdAt: artifact.created_at,
    captureRunId: artifact.capture_run_id,
    sourceUrl: artifact.source_url,
    previewUrl: buildApiContentUrl(artifact.preview_url),
    downloadUrl: buildApiContentUrl(artifact.download_url),
  };
}

function adaptSavedItemInsightPayload(payload: SavedItemInsightPayloadDto): SavedItemInsightPayload {
  return {
    summary: payload.summary,
    longTermValue: payload.long_term_value,
    actionableTakeaways: payload.actionable_takeaways,
    keyConcepts: payload.key_concepts,
    suggestedTags: payload.suggested_tags,
    topicIds: payload.topic_ids,
    topicLabels: payload.topic_labels,
    contentType: payload.content_type,
    contentTypeLabel: payload.content_type_label,
    sourceKind: payload.source_kind,
    aiRoute: payload.ai_route,
    inputSnapshotKind: payload.input_snapshot_kind,
    inputTextSource: payload.input_text_source,
    inputCharCount: payload.input_char_count,
    inputTextWasTruncated: payload.input_text_was_truncated,
  };
}

function buildSavedItemInsightPayloadDto(payload: SavedItemInsightPayload): SavedItemInsightPayloadDto {
  return {
    summary: payload.summary,
    long_term_value: payload.longTermValue,
    actionable_takeaways: payload.actionableTakeaways,
    key_concepts: payload.keyConcepts,
    suggested_tags: payload.suggestedTags,
    topic_ids: payload.topicIds,
    topic_labels: payload.topicLabels,
    content_type: payload.contentType,
    content_type_label: payload.contentTypeLabel,
    source_kind: payload.sourceKind,
    ai_route: payload.aiRoute,
    input_snapshot_kind: payload.inputSnapshotKind,
    input_text_source: payload.inputTextSource,
    input_char_count: payload.inputCharCount,
    input_text_was_truncated: payload.inputTextWasTruncated,
  };
}

function adaptSavedItemAIInsight(payload: SavedItemAIInsightDto): SavedItemAIInsight {
  return {
    artifactId: payload.artifact_id,
    artifactKind: payload.artifact_kind,
    modelName: payload.model_name,
    promptVersion: payload.prompt_version,
    payload: adaptSavedItemInsightPayload(payload.payload),
    createdAt: payload.created_at,
  };
}

function adaptSavedItemCuratedInsight(payload: SavedItemCuratedInsightDto): SavedItemCuratedInsight {
  return {
    id: payload.id,
    savedItemId: payload.saved_item_id,
    baseInsightEngineArtifactId: payload.base_insight_engine_artifact_id,
    payload: adaptSavedItemInsightPayload(payload.payload),
    diffJson: payload.diff_json,
    editNote: payload.edit_note,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

function adaptSavedItemResolvedInsight(payload: SavedItemResolvedInsightDto): SavedItemResolvedInsight {
  return {
    source: payload.source,
    payload: adaptSavedItemInsightPayload(payload.payload),
    insightEngineArtifactId: payload.insight_engine_artifact_id,
    curatedInsightId: payload.curated_insight_id,
    modelName: payload.model_name,
    promptVersion: payload.prompt_version,
    generatedAt: payload.generated_at,
    curatedUpdatedAt: payload.curated_updated_at,
  };
}

function adaptSavedItemCuratedInsightEvent(payload: SavedItemCuratedInsightEventDto): SavedItemCuratedInsightEvent {
  return {
    id: payload.id,
    savedItemId: payload.saved_item_id,
    curatedInsightId: payload.curated_insight_id,
    baseInsightEngineArtifactId: payload.base_insight_engine_artifact_id,
    eventType: payload.event_type,
    eventPayloadJson: payload.event_payload_json,
    createdAt: payload.created_at,
  };
}

function adaptCaptureTitleMetadata(
  metadata: NonNullable<NonNullable<KnowledgeSavedItemListDto['capture']>['title_metadata']>,
): CaptureTitleMetadata {
  return {
    capturedAt: metadata.captured_at,
    capturedPageTitle: metadata.captured_page_title,
    singlefileTitle: metadata.singlefile_title,
    crawl4aiTitle: metadata.crawl4ai_title,
    feedTitle: metadata.feed_title,
    titleSource: metadata.title_source,
    userTitlePresent: metadata.user_title_present,
    titleUpdated: metadata.title_updated,
    savedItemTitleBefore: metadata.saved_item_title_before,
    savedItemTitleAfter: metadata.saved_item_title_after,
  };
}

function buildSourceAppName(sourceUrl: string, sourceKind: string | null) {
  try {
    const hostname = new URL(sourceUrl).hostname.replace(/^www\./, '');
    if (hostname) {
      return hostname;
    }
  } catch {
    // Ignore URL parsing failures and fall back to kind-based labels.
  }

  if (sourceKind === 'rsshub') {
    return 'RSSHub';
  }

  return 'Web';
}
