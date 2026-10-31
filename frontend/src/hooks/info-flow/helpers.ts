import {ApiError as OperationApiError} from '@/api/errors';
import {
  HOMEPAGE_DEFAULT_PAGE_SIZE,
  HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID,
  type HomepageQueryOptions,
} from '@/services/homepage-service';
import type {SourceEditorFormValue} from '@/components/source-editor-dialog';
import type {
  CaptureOperation,
  BehaviorViewContextDto,
  ChannelFacetCount,
  FacetCount,
  FeedItem,
  FeedItemRoutingCorrectionInput,
  FeedItemStructuredFeedback,
  FlowLaneFacetCount,
  HomepageFacetCount,
  HomepageFlowLane,
  HomepageReadModelStatus,
  HomepageSummary,
  ReadingDepthFilter,
  SourceScope,
  ThemeMode,
  ViewMode,
} from '@/types/feed';
import type {InsightChannel} from '@/types/settings';
import type {
  BulkSourceSyncResponseDto,
  AppOperationDto,
  AppOperationStatus,
  FeedItemInsightArtifactBatchOperationState,
  SourceCreatePayload,
  SourceDto,
  SingleSourceSyncResponseDto,
  SourceInsightRouting,
  SourceInsightRoutingInput,
  SourcePreferenceProfile,
  SourcePreferenceProfileInput,
  SourcePreferenceProfileState,
  SourcePreviewResponseDto,
  SourceSyncOperationKind,
  SourceSyncOperationState,
  SourceSyncProgressState,
} from '@/types/source';

export const THEME_STORAGE_KEY = 'infoflowaggregator-theme';
export const CAPTURE_RUNNING_MESSAGE = '捕获请求正在执行。';
export const CAPTURE_RUNNING_DETAIL = '等待后端返回最新进度';
export const ALL_SOURCES_SCOPE: SourceScope = {kind: 'all_sources', value: null, label: '全部来源'};
export const LIST_RETRY_DELAY_MS = 500;
export const UPLOAD_ORIGIN_TYPES = new Set(['markdown_upload', 'html_upload', 'singlefile_html_upload']);
export const DEFAULT_SOURCE_ROUTING: SourceInsightRoutingInput = {flowPolicy: 'normal', channelTendencies: []};

export const HOMEPAGE_EMPTY_SOURCE_CATEGORY_LABEL = '未分类';
export const HOMEPAGE_FLOW_LANE_LABELS: Record<HomepageFlowLane, string> = {
  main: '主列表',
  brief_pool: '快讯池',
  low_priority: '低优先级',
  needs_review: '待复核',
};

export interface SourceEditorState {
  mode: 'create' | 'edit';
  source: SourceDto | null;
  preview: SourcePreviewResponseDto | null;
  routing: SourceInsightRouting | null;
  routingError: string | null;
  isRoutingLoading: boolean;
  sourcePreferenceProfile: SourcePreferenceProfileState | null;
  sourceProfileError: string | null;
  isSourceProfileLoading: boolean;
}

type ListLoadStatus = 'idle' | 'loading' | 'ready' | 'error';

export interface ListLoadState {
  status: ListLoadStatus;
  errorMessage: string | null;
}

export interface HomepageWindowState {
  querySignature: string | null;
  nextCursor: string | null;
  snapshotToken: string | null;
  snapshotExpiresAt: string | null;
  readModelStatus: HomepageReadModelStatus | null;
  snapshotInvalidated: boolean;
  hasMore: boolean;
  isLoadingMore: boolean;
  loadMoreErrorMessage: string | null;
}

export const idleListLoadState: ListLoadState = {status: 'idle', errorMessage: null};
export const loadingListLoadState: ListLoadState = {status: 'loading', errorMessage: null};
export const emptyHomepageWindowState: HomepageWindowState = {
  querySignature: null,
  nextCursor: null,
  snapshotToken: null,
  snapshotExpiresAt: null,
  readModelStatus: null,
  snapshotInvalidated: false,
  hasMore: false,
  isLoadingMore: false,
  loadMoreErrorMessage: null,
};

export function getInitialTheme(): ThemeMode {
  if (typeof window === 'undefined') {
    return 'light';
  }

  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  if (stored === 'light' || stored === 'dark') {
    return stored;
  }

  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function sleep(ms: number) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

export async function withListRetry<T>(load: () => Promise<T>): Promise<T> {
  try {
    return await load();
  } catch (firstError) {
    await sleep(LIST_RETRY_DELAY_MS);
    try {
      return await load();
    } catch {
      throw firstError;
    }
  }
}

export function formatListError(error: unknown, label: string) {
  const detail = error instanceof Error ? error.message : '未知错误';
  return `${label}加载失败：${detail}`;
}

export function formatDetailLoadError(error: unknown) {
  const detail = error instanceof Error ? error.message : '未知错误';
  return `详情加载失败：${detail}`;
}

export function isUnavailableDetailError(error: unknown) {
  return error instanceof OperationApiError && (error.status === 404 || error.status === 410);
}

export function isRecoverableHomepageCursorError(error: unknown) {
  return (
    error instanceof OperationApiError &&
    ((error.status === 400 && /cursor|anchor|does not match/i.test(error.message)) ||
      (error.status === 409 && /snapshot_(expired|invalidated)/i.test(error.message)))
  );
}

export function mergeStructuredFeedbackWithRoutingCorrection(
  existing: FeedItemStructuredFeedback | null | undefined,
  correction: FeedItemRoutingCorrectionInput,
): FeedItemStructuredFeedback {
  const next: FeedItemStructuredFeedback = {
    overallValue: existing?.overallValue ?? null,
    priorityCorrection: existing?.priorityCorrection ?? null,
    topicAddIds: existing?.topicAddIds ?? [],
    topicRemoveIds: existing?.topicRemoveIds ?? [],
    contentTypeCorrection: existing?.contentTypeCorrection ?? null,
    reasonCodeAddIds: existing?.reasonCodeAddIds ?? [],
    reasonCodeRemoveIds: existing?.reasonCodeRemoveIds ?? [],
    issueMarkers: existing?.issueMarkers ?? [],
    primaryChannelCorrection: existing?.primaryChannelCorrection ?? null,
    channelAddIds: existing?.channelAddIds ?? [],
    channelRemoveIds: existing?.channelRemoveIds ?? [],
    readingDepthCorrection: existing?.readingDepthCorrection ?? null,
    flowLaneCorrection: existing?.flowLaneCorrection ?? null,
    channelCorrectionNotes: existing?.channelCorrectionNotes ?? '',
    createdAt: existing?.createdAt ?? null,
    updatedAt: existing?.updatedAt ?? null,
  };

  if (Object.prototype.hasOwnProperty.call(correction, 'primaryChannelCorrection')) {
    next.primaryChannelCorrection = correction.primaryChannelCorrection ?? null;
    if (correction.primaryChannelCorrection) {
      next.channelAddIds = removeIds(next.channelAddIds, [correction.primaryChannelCorrection]);
      next.channelRemoveIds = removeIds(next.channelRemoveIds, [correction.primaryChannelCorrection]);
    }
  }
  if (correction.addChannelIds) {
    next.channelAddIds = mergeUniqueIds(next.channelAddIds, correction.addChannelIds);
    next.channelRemoveIds = removeIds(next.channelRemoveIds, correction.addChannelIds);
  }
  if (correction.removeChannelIds) {
    next.channelRemoveIds = mergeUniqueIds(next.channelRemoveIds, correction.removeChannelIds);
    next.channelAddIds = removeIds(next.channelAddIds, correction.removeChannelIds);
  }
  if (Object.prototype.hasOwnProperty.call(correction, 'readingDepthCorrection')) {
    next.readingDepthCorrection = correction.readingDepthCorrection ?? null;
  }
  if (Object.prototype.hasOwnProperty.call(correction, 'flowLaneCorrection')) {
    next.flowLaneCorrection = correction.flowLaneCorrection ?? null;
  }
  if (Object.prototype.hasOwnProperty.call(correction, 'channelCorrectionNotes')) {
    next.channelCorrectionNotes = correction.channelCorrectionNotes ?? '';
  }

  if (next.primaryChannelCorrection) {
    next.channelAddIds = removeIds(next.channelAddIds, [next.primaryChannelCorrection]);
    next.channelRemoveIds = removeIds(next.channelRemoveIds, [next.primaryChannelCorrection]);
  }

  return next;
}

export function mergeUniqueIds(existing: string[], incoming: string[]) {
  return Array.from(new Set([...existing, ...incoming]));
}

export function removeIds(existing: string[], idsToRemove: string[]) {
  const removeSet = new Set(idsToRemove);
  return existing.filter((id) => !removeSet.has(id));
}

export function mergeFeedItem(existing: FeedItem | undefined, incoming: FeedItem): FeedItem {
  if (!existing) {
    return incoming;
  }

  const summarySource = pickSummarySource(existing, incoming);
  const summary =
    summarySource === incoming.summarySource
      ? incoming.summary || existing.summary
      : existing.summary || incoming.summary;

  return {
    ...existing,
    ...incoming,
    feedItemId: incoming.feedItemId,
    savedItemId: incoming.savedItemId,
    sourceId: incoming.sourceId,
    content: incoming.content || existing.content,
    summary,
    feedContentText: mergeNullableFeedItemField(existing, incoming, 'feedContentText'),
    topics: incoming.topics.length ? incoming.topics : existing.topics,
    relevanceScore: incoming.relevanceScore ?? existing.relevanceScore,
    priorityBand: incoming.priorityBand ?? existing.priorityBand,
    priorityReasons: incoming.priorityReasons.length ? incoming.priorityReasons : existing.priorityReasons,
    browsedAt: mergeNullableFeedItemField(existing, incoming, 'browsedAt'),
    seenAt: mergeNullableFeedItemField(existing, incoming, 'seenAt'),
    readFullAt: mergeNullableFeedItemField(existing, incoming, 'readFullAt'),
    note: mergeNullableFeedItemField(existing, incoming, 'note'),
    originType: mergeNullableFeedItemField(existing, incoming, 'originType'),
    resolvedInsight: mergeNullableFeedItemField(existing, incoming, 'resolvedInsight'),
    aiGeneratedInsight: mergeNullableFeedItemField(existing, incoming, 'aiGeneratedInsight'),
    curatedInsight: mergeNullableFeedItemField(existing, incoming, 'curatedInsight'),
    savedAt: mergeNullableFeedItemField(existing, incoming, 'savedAt'),
    notInterestedAt: mergeNullableFeedItemField(existing, incoming, 'notInterestedAt'),
    isLowPriorityBucket: incoming.isLowPriorityBucket,
    lowPriorityBucketReason: incoming.lowPriorityBucketReason,
    autoLowPriorityCleared: incoming.autoLowPriorityCleared,
    hasCanonicalSnapshot: incoming.hasCanonicalSnapshot ?? existing.hasCanonicalSnapshot,
    hasOriginalPageSnapshot: incoming.hasOriginalPageSnapshot ?? existing.hasOriginalPageSnapshot,
    hasSinglefileHtml: incoming.hasSinglefileHtml ?? existing.hasSinglefileHtml,
    hasCrawl4aiRawMarkdown: incoming.hasCrawl4aiRawMarkdown ?? existing.hasCrawl4aiRawMarkdown,
    hasCrawl4aiFitMarkdown: incoming.hasCrawl4aiFitMarkdown ?? existing.hasCrawl4aiFitMarkdown,
    hasCrawl4aiFitImageReinsertedMarkdown:
      incoming.hasCrawl4aiFitImageReinsertedMarkdown ?? existing.hasCrawl4aiFitImageReinsertedMarkdown,
    imageAssetCount: incoming.imageAssetCount ?? existing.imageAssetCount ?? 0,
    captureStatus: mergeNullableFeedItemField(existing, incoming, 'captureStatus'),
    latestCaptureRunId: mergeNullableFeedItemField(existing, incoming, 'latestCaptureRunId'),
    captureErrorSummary: mergeNullableFeedItemField(existing, incoming, 'captureErrorSummary'),
    captureStartedAt: mergeNullableFeedItemField(existing, incoming, 'captureStartedAt'),
    captureFinishedAt: mergeNullableFeedItemField(existing, incoming, 'captureFinishedAt'),
    captureRetryable: incoming.captureRetryable ?? existing.captureRetryable ?? false,
    captureToolCalls: incoming.captureToolCalls?.length ? incoming.captureToolCalls : existing.captureToolCalls ?? [],
    localMirrorStatus: mergeNullableFeedItemField(existing, incoming, 'localMirrorStatus'),
    latestLocalMirrorExportId: mergeNullableFeedItemField(existing, incoming, 'latestLocalMirrorExportId'),
    localMirrorErrorSummary: mergeNullableFeedItemField(existing, incoming, 'localMirrorErrorSummary'),
    localMirrorStartedAt: mergeNullableFeedItemField(existing, incoming, 'localMirrorStartedAt'),
    localMirrorFinishedAt: mergeNullableFeedItemField(existing, incoming, 'localMirrorFinishedAt'),
    localMirrorSourceCaptureRunId: mergeNullableFeedItemField(existing, incoming, 'localMirrorSourceCaptureRunId'),
    localMirrorWrittenFileCount: incoming.localMirrorWrittenFileCount ?? existing.localMirrorWrittenFileCount ?? 0,
    localMirrorPaths: mergeNullableFeedItemField(existing, incoming, 'localMirrorPaths'),
    archiveArtifacts:
      incoming.archiveArtifacts !== undefined ? incoming.archiveArtifacts : existing.archiveArtifacts ?? undefined,
    summarySource,
    topicSource: incoming.topicSource ?? existing.topicSource,
    prioritySource: incoming.prioritySource ?? existing.prioritySource,
    artifactGeneratedAt: incoming.artifactGeneratedAt ?? existing.artifactGeneratedAt ?? null,
    contentTextSource: incoming.contentTextSource ?? existing.contentTextSource,
    contentExtractionStatus: incoming.contentExtractionStatus ?? existing.contentExtractionStatus,
    contentExtractionAttemptedAt: mergeNullableFeedItemField(existing, incoming, 'contentExtractionAttemptedAt'),
    contentExtractedAt: mergeNullableFeedItemField(existing, incoming, 'contentExtractedAt'),
    contentExtractionError: mergeNullableFeedItemField(existing, incoming, 'contentExtractionError'),
    contentExtractionMethod: mergeNullableFeedItemField(existing, incoming, 'contentExtractionMethod'),
    contentExtractionFinalUrl: mergeNullableFeedItemField(existing, incoming, 'contentExtractionFinalUrl'),
    contentExtractionVisibleCharCount: mergeNullableFeedItemField(
      existing,
      incoming,
      'contentExtractionVisibleCharCount',
    ),
    insightFeedbackText: mergeNullableFeedItemField(existing, incoming, 'insightFeedbackText'),
    structuredFeedback: mergeNullableFeedItemField(existing, incoming, 'structuredFeedback'),
    channelFeedback: mergeNullableFeedItemField(existing, incoming, 'channelFeedback'),
    primaryChannelId: mergeNullableFeedItemField(existing, incoming, 'primaryChannelId'),
    secondaryChannels:
      incoming.secondaryChannels !== undefined ? incoming.secondaryChannels : existing.secondaryChannels ?? undefined,
    readingDepth: mergeNullableFeedItemField(existing, incoming, 'readingDepth'),
    flowLane: incoming.flowLane ?? existing.flowLane,
    routingConfidence: mergeNullableFeedItemField(existing, incoming, 'routingConfidence'),
    routingSource: incoming.routingSource ?? existing.routingSource,
    routingReasonCodes:
      incoming.routingReasonCodes !== undefined ? incoming.routingReasonCodes : existing.routingReasonCodes ?? undefined,
    promotionReasonCodes:
      incoming.promotionReasonCodes !== undefined
        ? incoming.promotionReasonCodes
        : existing.promotionReasonCodes ?? undefined,
    isolationReasonCodes:
      incoming.isolationReasonCodes !== undefined
        ? incoming.isolationReasonCodes
        : existing.isolationReasonCodes ?? undefined,
    sourcePolicyApplied: mergeNullableFeedItemField(existing, incoming, 'sourcePolicyApplied'),
    routingNotes: mergeNullableFeedItemField(existing, incoming, 'routingNotes'),
    routingReasons: incoming.routingReasons !== undefined ? incoming.routingReasons : existing.routingReasons ?? undefined,
    isSaving: incoming.isSaving ?? existing.isSaving ?? false,
  };
}

export function buildSourceSyncCompleteMessage(result: BulkSourceSyncResponseDto) {
  if (result.source_count === 0) {
    return '没有启用的来源可刷新。';
  }

  const itemTotals = result.results.reduce(
    (totals, current) => ({
      seen: totals.seen + current.fetch.items_seen_count,
      created: totals.created + current.fetch.items_created_count,
      matched: totals.matched + current.fetch.items_matched_count,
      invalid: totals.invalid + current.fetch.items_invalid_count,
    }),
    {seen: 0, created: 0, matched: 0, invalid: 0},
  );
  const sourceSummary =
    result.stopped
      ? `刷新已停止：已完成 ${result.results.length}/${result.source_count} 个来源`
      : result.error_count > 0
        ? `刷新完成：${result.success_count}/${result.source_count} 个来源成功，${result.error_count} 个失败`
        : `刷新完成：${result.success_count}/${result.source_count} 个来源成功`;
  const insightTotals = result.results.reduce(
    (totals, current) => ({
      processed: totals.processed + getSourceSyncInsightArtifactStats(current).processed_feed_item_count,
      rows: totals.rows + getSourceSyncInsightArtifactStats(current).artifact_rows_created,
      failed: totals.failed + getSourceSyncInsightArtifactStats(current).failed_feed_item_count,
    }),
    {processed: 0, rows: 0, failed: 0},
  );

  return `${sourceSummary}。已见 ${itemTotals.seen} 条，新增 ${itemTotals.created} 条，匹配 ${itemTotals.matched} 条，无效 ${itemTotals.invalid} 条；洞察处理 ${insightTotals.processed} 条，生成 ${insightTotals.rows} 条结果，失败 ${insightTotals.failed} 条。`;
}

export function isOperationActiveStatus(status: AppOperationStatus) {
  return status === 'queued' || status === 'running' || status === 'stopping';
}

export function isOperationTerminalStatus(status: AppOperationStatus) {
  return status === 'completed' || status === 'stopped' || status === 'failed' || status === 'interrupted';
}

export function isItemOperation(operation: AppOperationDto) {
  return (
    operation.operation_type === 'knowledge_capture' ||
    operation.operation_type === 'local_mirror_export' ||
    operation.operation_type === 'feed_item_extract' ||
    isFeedItemInsightArtifactItemOperation(operation) ||
    operation.operation_type === 'saved_item_insight'
  );
}

export function isFeedItemInsightArtifactBatchOperation(operation: AppOperationDto) {
  return operation.operation_type === 'feed_item_insight_artifacts' || operation.operation_type === 'channel_lifecycle';
}

export function isFeedItemInsightArtifactItemOperation(operation: AppOperationDto) {
  return operation.operation_type === 'feed_item_insight_artifact';
}

export function getSourceSyncInsightArtifactStats(result: SingleSourceSyncResponseDto) {
  return result.feed_item_insight_artifacts;
}

export function getOperationSavedItemId(operation: AppOperationDto) {
  const value = operation.scope?.saved_item_id;
  return typeof value === 'number' ? value : typeof value === 'string' ? Number(value) || null : null;
}

export function getOperationFeedItemId(operation: AppOperationDto) {
  const value = operation.scope?.feed_item_id;
  return typeof value === 'number' ? value : typeof value === 'string' ? Number(value) || null : null;
}

export function adaptOperationProgress(operation: AppOperationDto): SourceSyncProgressState | null {
  const progress = operation.progress;
  if (!progress) {
    return null;
  }

  return {
    stage: progress.stage,
    total: progress.total,
    completed: progress.completed,
    successCount: progress.success_count,
    errorCount: progress.error_count,
    stopped: progress.stopped || operation.status === 'stopped',
    stopRequested: progress.stop_requested || operation.stop_requested,
    currentSourceName: progress.current_source_name,
    currentSourceIndex: progress.current_source_index,
    currentFetchStatus: progress.current_fetch_status,
    currentItemsSeenCount: progress.current_items_seen_count,
    currentItemsCreatedCount: progress.current_items_created_count,
    currentItemsMatchedCount: progress.current_items_matched_count,
    currentItemsInvalidCount: progress.current_items_invalid_count,
    currentInsightTotal: progress.current_insight_total,
    currentInsightCompleted: progress.current_insight_completed,
    currentInsightFailed: progress.current_insight_failed,
    currentInsightArtifactRowsCreated: progress.current_insight_artifact_rows_created,
    latestSourceName: progress.latest_source_name,
    latestStatus: progress.latest_status,
    itemsSeenCount: progress.items_seen_count,
    itemsCreatedCount: progress.items_created_count,
    itemsMatchedCount: progress.items_matched_count,
    itemsInvalidCount: progress.items_invalid_count,
    insightTotalCount: progress.insight_total_count,
    insightCompletedCount: progress.insight_completed_count,
    insightFailedCount: progress.insight_failed_count,
    insightArtifactRowsCreated: progress.insight_artifact_rows_created,
    latestErrorMessage: progress.latest_error_message,
    failedSources: progress.failed_sources,
    jobTotalCount: progress.job_total_count,
    jobPendingCount: progress.job_pending_count,
    jobRunningCount: progress.job_running_count,
    jobSucceededCount: progress.job_succeeded_count,
    jobFailedCount: progress.job_failed_count,
    jobCancelledCount: progress.job_cancelled_count,
  };
}

export function adaptSourceSyncOperation(operation: AppOperationDto): SourceSyncOperationState {
  return {
    id: operation.id,
    kind: operation.operation_kind as SourceSyncOperationKind,
    status: operation.status,
    sourceId: operation.source_id,
    sourceName: operation.source_name,
    stopRequested: operation.stop_requested,
    message: operation.message,
    error: operation.error_message,
    result: operation.result as SourceSyncOperationState['result'],
    startedAt: operation.started_at,
    updatedAt: operation.updated_at,
    completedAt: operation.completed_at,
  };
}

export function adaptFeedItemInsightArtifactBatchOperation(operation: AppOperationDto): FeedItemInsightArtifactBatchOperationState {
  const scope =
    operation.operation_kind === 'source_sync_created_feed_items'
      ? 'source_sync_created_feed_items'
      : operation.operation_kind === 'current_source'
        ? 'current_source'
        : operation.operation_kind === 'failed_attempts'
          ? 'failed_attempts'
          : operation.operation_kind === 'retryable_coverage'
            ? 'retryable_coverage'
            : 'global';
  const label =
    operation.operation_type === 'channel_lifecycle'
      ? '频道退休 reroute'
      : scope === 'source_sync_created_feed_items'
      ? '补齐本次刷新洞察'
      : scope === 'failed_attempts'
        ? operation.source_name
          ? `重试失败洞察：${operation.source_name}`
          : '重试失败洞察'
        : scope === 'retryable_coverage'
          ? formatRetryableCoverageOperationLabel(operation)
      : operation.source_name
        ? `补齐洞察：${operation.source_name}`
        : '补齐全局洞察';
  return {
    id: operation.id,
    scope,
    status: operation.status,
    sourceId: operation.source_id,
    sourceName: operation.source_name,
    label,
    stopRequested: operation.stop_requested,
    message: operation.message,
    error: operation.error_message,
    result: operation.result,
    startedAt: operation.started_at,
    updatedAt: operation.updated_at,
    completedAt: operation.completed_at,
  };
}

function formatRetryableCoverageOperationLabel(operation: AppOperationDto) {
  const reasonCodes = Array.isArray(operation.scope?.reason_codes)
    ? operation.scope.reason_codes.filter((code): code is string => typeof code === 'string')
    : [];
  const hasInitialGeneration = reasonCodes.includes('no_attempt_history');
  const hasProblemReason = reasonCodes.some((code) =>
    ['previous_failed_attempt', 'previous_skipped_attempt', 'partial_artifacts'].includes(code),
  );
  const baseLabel =
    hasInitialGeneration && !hasProblemReason
      ? '生成待首次项'
      : hasProblemReason && !hasInitialGeneration
        ? '重试问题项'
        : '补齐检测结果';
  return operation.source_name ? `${baseLabel}：${operation.source_name}` : baseLabel;
}

export function extractConflictOperation(error: unknown): AppOperationDto | null {
  if (!(error instanceof OperationApiError) || error.status !== 409) {
    return null;
  }

  const detail = error.detail;
  if (!detail || typeof detail !== 'object' || !('detail' in detail)) {
    return null;
  }

  const nested = (detail as {detail: unknown}).detail;
  if (!nested || typeof nested !== 'object' || !('operation' in nested)) {
    return null;
  }

  const operation = (nested as {operation: unknown}).operation;
  if (
    operation &&
    typeof operation === 'object' &&
    'id' in operation &&
    'operation_type' in operation &&
    'status' in operation
  ) {
    return operation as AppOperationDto;
  }

  return null;
}

export function mergeNullableFeedItemField<Key extends keyof FeedItem>(
  existing: FeedItem,
  incoming: FeedItem,
  key: Key,
): FeedItem[Key] | null {
  if (Object.prototype.hasOwnProperty.call(incoming, key) && incoming[key] !== undefined) {
    return incoming[key] ?? null;
  }

  return existing[key] ?? null;
}

export function pickSummarySource(existing: FeedItem, incoming: FeedItem): FeedItem['summarySource'] {
  const existingRank = summarySourceRank(existing.summarySource);
  const incomingRank = summarySourceRank(incoming.summarySource);
  return incomingRank >= existingRank ? incoming.summarySource : existing.summarySource;
}

export function summarySourceRank(source: FeedItem['summarySource']) {
  const ranking = {
    insight_engine_artifact: 3,
    canonical_summary: 2,
    canonical_content: 1,
    none: 0,
    undefined: -1,
  } as const;

  if (source === undefined) {
    return ranking.undefined;
  }
  return ranking[source];
}

export function updateCollection(items: FeedItem[], itemId: number, updater: (item: FeedItem) => FeedItem) {
  return items.map((item) => (item.id === itemId ? updater(item) : item));
}

export function updateCollectionByFeedItemId(items: FeedItem[], feedItemId: number, updater: (item: FeedItem) => FeedItem) {
  return items.map((item) => (item.feedItemId === feedItemId ? updater(item) : item));
}

export function buildSourceUpdatePayload(
  source: SourceDto,
  patch: Partial<Pick<SourceDto, 'folder_path' | 'is_active' | 'ai_route'>>,
) {
  return {
    name: source.name,
    url: source.url,
    site_url: source.site_url,
    folder_path: patch.folder_path !== undefined ? patch.folder_path : source.folder_path,
    is_active: patch.is_active !== undefined ? patch.is_active : source.is_active,
    ai_route: patch.ai_route !== undefined ? patch.ai_route : source.ai_route,
  };
}

export function buildSourceCreatePayload(value: SourceEditorFormValue): SourceCreatePayload {
  return {
    name: value.name,
    kind: value.kind,
    url: value.url,
    site_url: value.site_url,
    folder_path: value.folder_path,
    is_active: value.is_active,
    ai_route: value.ai_route,
    icon_url: value.icon_url,
    icon_source: value.icon_source,
  };
}

export function sourcePreferenceProfileInputMatchesProfile(
  value: SourcePreferenceProfileInput,
  profile: SourcePreferenceProfile,
) {
  return (
    normalizeProfileText(value.subscriptionReason) === normalizeProfileText(profile.subscriptionReason) &&
    normalizeProfileText(value.qualityNotes) === normalizeProfileText(profile.qualityNotes) &&
    normalizeProfileText(value.editNote) === normalizeProfileText(profile.editNote) &&
    (value.sourceType ?? 'user_edit') === profile.sourceType &&
    stringListsEqual(value.goodArticleCriteria, profile.goodArticleCriteria) &&
    stringListsEqual(value.badArticlePatterns, profile.badArticlePatterns) &&
    stringListsEqual(value.preferredTopics, profile.preferredTopics) &&
    stringListsEqual(value.ignoredTopics, profile.ignoredTopics) &&
    stringListsEqual(value.preferredContentTypes, profile.preferredContentTypes) &&
    stringListsEqual(value.positiveExampleSummaries, profile.positiveExampleSummaries) &&
    stringListsEqual(value.negativeExampleSummaries, profile.negativeExampleSummaries) &&
    numberListsEqual(value.positiveExampleItemIds, profile.positiveExampleItemIds) &&
    numberListsEqual(value.negativeExampleItemIds, profile.negativeExampleItemIds)
  );
}

export function normalizeProfileText(value: string | null | undefined) {
  const normalized = value?.trim() ?? '';
  return normalized || null;
}

export function stringListsEqual(left: string[] | undefined, right: string[]) {
  const normalizedLeft = left ?? [];
  return normalizedLeft.length === right.length && normalizedLeft.every((value, index) => value === right[index]);
}

export function numberListsEqual(left: number[] | undefined, right: number[]) {
  const normalizedLeft = left ?? [];
  return normalizedLeft.length === right.length && normalizedLeft.every((value, index) => value === right[index]);
}

export function buildDefaultSourceRouting(sourceId: number): SourceInsightRouting {
  return {
    sourceId,
    flowPolicy: DEFAULT_SOURCE_ROUTING.flowPolicy,
    channelTendencies: [],
  };
}

export function normalizeSourceRoutingInput(
  routing: SourceInsightRoutingInput | SourceInsightRouting | null | undefined,
): SourceInsightRoutingInput {
  let primarySeen = false;
  return {
    flowPolicy: routing?.flowPolicy ?? DEFAULT_SOURCE_ROUTING.flowPolicy,
    channelTendencies: (routing?.channelTendencies ?? DEFAULT_SOURCE_ROUTING.channelTendencies)
      .map((tendency) => {
        const isPrimary = tendency.isPrimary && !primarySeen;
        if (isPrimary) {
          primarySeen = true;
        }
        return {
          ...tendency,
          tendency: clampSourceRoutingTendency(tendency.tendency),
          isPrimary,
        };
      }),
  };
}

export function clampSourceRoutingTendency(value: number) {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.min(Math.max(value, 0), 1);
}

export function isUploadedSavedItem(item: FeedItem) {
  return item.originType !== null && item.originType !== undefined && UPLOAD_ORIGIN_TYPES.has(item.originType);
}

export function isExternalSavedItem(item: FeedItem) {
  return item.originType === 'manual_url' || isUploadedSavedItem(item);
}

export function buildOriginalOpenedMessage(item: FeedItem) {
  if (shouldWarnOriginalOpenedWithIncompleteReaderContent(item)) {
    return '已打开原文，并标记为已读全文。App 内正文可能不完整，已记录为外部阅读。';
  }
  return '已打开原文，并标记为已读全文。';
}

export function shouldWarnOriginalOpenedWithIncompleteReaderContent(item: FeedItem) {
  return (
    item.contentTextSource === 'feed_summary' ||
    item.contentExtractionStatus === 'needs_extraction' ||
    item.contentExtractionStatus === 'failed' ||
    Boolean(item.contentExtractionError) ||
    (item.contentExtractionVisibleCharCount !== null &&
      item.contentExtractionVisibleCharCount !== undefined &&
      item.contentExtractionVisibleCharCount < 800)
  );
}

export function markCapturePending(item: FeedItem, startedAt: string): FeedItem {
  return {
    ...item,
    captureStatus: 'pending',
    captureErrorSummary: null,
    captureStartedAt: startedAt,
    captureFinishedAt: null,
    captureRetryable: false,
    captureToolCalls: [],
  };
}

export function markCaptureRequestFailed(item: FeedItem, message: string): FeedItem {
  return {
    ...item,
    captureStatus: item.captureStatus === 'pending' ? 'failed' : item.captureStatus,
    captureErrorSummary: message,
    captureFinishedAt: new Date().toISOString(),
    captureRetryable: true,
  };
}

export function applyCaptureOperation(item: FeedItem, operation: CaptureOperation | undefined): FeedItem {
  if (!operation) {
    return item;
  }

  if (operation.status === 'running') {
    return markCapturePending(item, operation.startedAt);
  }

  return {
    ...item,
    captureErrorSummary: operation.error ?? item.captureErrorSummary ?? '捕获请求失败。',
    captureFinishedAt: operation.updatedAt,
    captureRetryable: true,
  };
}

export function markSaveRequestPending(item: FeedItem, startedAt: string): FeedItem {
  return {
    ...item,
    status: 'saved',
    savedAt: item.savedAt ?? startedAt,
    isSaving: true,
    captureStatus: item.captureStatus ?? 'pending',
    captureErrorSummary: null,
  };
}

export function rollbackSaveRequest(item: FeedItem, previousItem: FeedItem, message: string): FeedItem {
  return {
    ...item,
    status: previousItem.status,
    savedAt: previousItem.savedAt ?? null,
    savedItemId: previousItem.savedItemId ?? null,
    isSaving: false,
    captureStatus: item.captureStatus === 'pending' ? null : item.captureStatus,
    captureErrorSummary: message,
  };
}

export function behaviorSurfaceForView(view: ViewMode): BehaviorViewContextDto['surface'] {
  if (view === 'ai') {
    return 'ai_homepage';
  }
  if (view === 'reader') {
    return 'classic_reader';
  }
  if (view === 'kb') {
    return 'knowledge_base';
  }
  return "unknown";
}

export function withBehaviorSearchQuery(
  context: BehaviorViewContextDto,
  searchQueryValue: string | null,
): BehaviorViewContextDto {
  if (!searchQueryValue) {
    return context;
  }
  return {
    ...context,
    search_query: searchQueryValue,
  };
}

export function withBehaviorSourceScope(context: BehaviorViewContextDto, sourceScope: SourceScope): BehaviorViewContextDto {
  if (sourceScope.kind === 'source') {
    return {
      ...context,
      source_id: Number(sourceScope.value),
    };
  }
  if (sourceScope.kind === 'source_category') {
    return {
      ...context,
      source_category: String(sourceScope.value ?? ''),
    };
  }
  return context;
}

export function withBehaviorReadingDepth(
  context: BehaviorViewContextDto,
  readingDepthFilter: ReadingDepthFilter,
): BehaviorViewContextDto {
  if (readingDepthFilter === 'all') {
    return context;
  }
  return {
    ...context,
    reading_depth: readingDepthFilter,
  };
}

export function homepageQueryKey(query: HomepageQueryOptions) {
  return JSON.stringify({
    channelId: query.channelId ?? null,
    sourceId: query.sourceId ?? null,
    sourceCategory: query.sourceCategory ?? null,
    flowLane: query.flowLane ?? null,
    readingDepth: query.readingDepth ?? null,
    state: query.state ?? null,
    saved: query.saved ?? null,
    notInterested: query.notInterested ?? null,
    topic: query.topic ?? null,
    searchText: query.searchText ?? null,
    timeWindow: query.timeWindow ?? 'all',
    sort: query.sort ?? 'priority',
    pageSize: query.pageSize ?? HOMEPAGE_DEFAULT_PAGE_SIZE,
  });
}

export function hasHomepageMembershipMutationFilter(query: HomepageQueryOptions) {
  return query.state != null || query.saved != null || query.notInterested != null;
}

export function normalizeHomepageSourceCategory(value: string | number | null) {
  if (typeof value !== 'string') {
    return null;
  }
  return value;
}

export function sourceCategoryLabelFromId(categoryId: string) {
  return categoryId === HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID ? HOMEPAGE_EMPTY_SOURCE_CATEGORY_LABEL : categoryId;
}

export function mergeUniqueFeedItems(existing: FeedItem[], incoming: FeedItem[]) {
  const byId = new Map(existing.map((item) => [item.id, item]));
  const orderedIds = existing.map((item) => item.id);
  for (const item of incoming) {
    if (!byId.has(item.id)) {
      orderedIds.push(item.id);
    }
    byId.set(item.id, mergeFeedItem(byId.get(item.id), item));
  }
  return orderedIds.map((id) => byId.get(id)).filter((item): item is FeedItem => Boolean(item));
}

export function mapHomepageTopicFacets(summary: HomepageSummary | null): FacetCount[] {
  return (summary?.topicCounts ?? []).map((facet) => ({label: facet.label, count: facet.count})).slice(0, 6);
}

export function mapHomepageChannelFacets(summary: HomepageSummary | null, channels: InsightChannel[]): ChannelFacetCount[] {
  const labels = new Map(channels.map((channel) => [channel.id, channel.label]));
  const enabledById = new Map(channels.map((channel) => [channel.id, channel.enabled && !channel.retiredAt]));
  return (summary?.channelCounts ?? []).map((facet) => ({
    id: facet.id,
    channelId: facet.id,
    label: labels.get(facet.id) ?? facet.label,
    count: facet.count,
    enabled: enabledById.get(facet.id) ?? true,
  }));
}

export function mapHomepageFlowLaneFacets(summary: HomepageSummary | null): FlowLaneFacetCount[] {
  const countByLane = new Map((summary?.flowLaneCounts ?? []).map((facet) => [facet.id, facet.count]));
  return (['main', 'brief_pool', 'low_priority', 'needs_review'] as HomepageFlowLane[]).map((lane) => ({
    id: lane,
    lane,
    label: HOMEPAGE_FLOW_LANE_LABELS[lane],
    count: countByLane.get(lane) ?? 0,
    enabled: true,
  }));
}

export function countByFacetId(facets: HomepageFacetCount[]) {
  const counts = new Map<string, number>();
  for (const facet of facets) {
    counts.set(facet.id, facet.count);
  }
  return counts;
}

export function countByFacetNumericId(facets: HomepageFacetCount[]) {
  const counts = new Map<number, number>();
  for (const facet of facets) {
    const id = Number(facet.id);
    if (!Number.isNaN(id)) {
      counts.set(id, facet.count);
    }
  }
  return counts;
}
