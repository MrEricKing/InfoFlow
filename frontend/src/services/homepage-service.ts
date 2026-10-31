import type {
  FeedItem,
  HomepageBootstrapDto,
  HomepageBootstrapResponse,
  HomepageCardDto,
  HomepageFlowLane,
  HomepageItemsDto,
  HomepageItemsResponse,
  HomepageReadModelStatusDto,
  HomepageSort,
  HomepageState,
  HomepageSummary,
  HomepageSummaryDto,
  FeedItemInsightArtifactMissingCandidatesResponseDto,
  ReadingDepth,
  TimeWindow,
} from '@/types/feed';
import {adaptChannelFeedbackFromStructuredFeedback, adaptStructuredFeedback} from '@/services/feed-service';
import {requestJson} from '@/api/client';
import {
  EMPTY_SOURCE_CATEGORY_ID,
  sourceCategoryIdFromFolderPath,
  sourceCategoryLabelFromFolderPath,
} from '@/lib/source-category';

export const HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID = EMPTY_SOURCE_CATEGORY_ID;
export const HOMEPAGE_DEFAULT_PAGE_SIZE = 30;

export interface HomepageQueryOptions {
  channelId?: string | null;
  sourceId?: number | null;
  sourceCategory?: string | null;
  flowLane?: HomepageFlowLane | null;
  readingDepth?: ReadingDepth | null;
  state?: HomepageState | null;
  saved?: boolean | null;
  notInterested?: boolean | null;
  topic?: string | null;
  searchText?: string | null;
  timeWindow?: TimeWindow | null;
  sort?: HomepageSort;
  pageSize?: number;
  cursor?: string | null;
  snapshotToken?: string | null;
}

export async function getHomepageBootstrap(options: HomepageQueryOptions = {}): Promise<HomepageBootstrapResponse> {
  const queryString = buildHomepageQueryString(options);
  const response = await requestJson<HomepageBootstrapDto>(`/homepage/bootstrap${queryString ? `?${queryString}` : ''}`);
  return adaptHomepageBootstrapResponse(response);
}

export async function getHomepageSummary(options: HomepageQueryOptions = {}): Promise<HomepageSummary> {
  const queryString = buildHomepageQueryString(options);
  const response = await requestJson<HomepageSummaryDto>(`/homepage/summary${queryString ? `?${queryString}` : ''}`);
  return adaptHomepageSummary(response);
}

export async function listHomepageItems(options: HomepageQueryOptions = {}): Promise<HomepageItemsResponse> {
  const queryString = buildHomepageQueryString(options);
  const response = await requestJson<HomepageItemsDto>(`/homepage/items${queryString ? `?${queryString}` : ''}`);
  return adaptHomepageItemsResponse(response);
}

export async function listMissingFeedItemInsightArtifactCandidates(options?: {sourceId?: number | null; limit?: number | null}) {
  const params = new URLSearchParams();
  if (options?.sourceId !== undefined && options.sourceId !== null) {
    params.set('source_id', String(options.sourceId));
  }
  if (options?.limit !== undefined && options.limit !== null) {
    params.set('limit', String(options.limit));
  }

  const queryString = params.toString();
  return requestJson<FeedItemInsightArtifactMissingCandidatesResponseDto>(
    `/feed-item-insight-artifacts/missing-candidates${queryString ? `?${queryString}` : ''}`,
  );
}

function adaptHomepageCard(card: HomepageCardDto): FeedItem {
  const sourceAppUrl = card.source_site_url ?? card.source_url;
  const sourceAppName = buildSourceAppName(sourceAppUrl, card.source_kind);
  const structuredFeedback = adaptStructuredFeedback(card.structured_feedback);

  return {
    id: card.id,
    feedItemId: card.id,
    savedItemId: null,
    sourceId: card.source_id,
    title: card.title,
    summary: card.summary_text ?? '',
    content: '',
    sourceName: card.source_name,
    sourceAppName,
    sourceFolder: sourceCategoryLabelFromFolderPath(card.source_folder_path),
    sourceCategoryId: sourceCategoryIdFromFolderPath(card.source_folder_path),
    publishedAt: card.published_at ?? stateTimestampFallback(card.state),
    topics: card.topic_labels,
    status: card.state.status,
    relevanceScore: card.priority_score,
    priorityBand: card.priority_band,
    priorityReasons: card.priority_reasons,
    isLowPriorityBucket: card.low_priority_bucket,
    lowPriorityBucketReason: card.low_priority_bucket_reason,
    autoLowPriorityCleared: card.auto_low_priority_cleared,
    originalUrl: card.original_url,
    sourceAppUrl,
    contentTextSource: card.content_text_source,
    contentExtractionStatus: card.content_extraction_status,
    contentExtractionAttemptedAt: card.content_extraction_attempted_at,
    contentExtractedAt: card.content_extracted_at,
    contentExtractionError: card.content_extraction_error,
    contentExtractionMethod: card.content_extraction_method,
    contentExtractionFinalUrl: card.content_extraction_final_url,
    contentExtractionVisibleCharCount: card.content_extraction_visible_char_count,
    insightFeedbackText: card.insight_feedback_text,
    structuredFeedback,
    channelFeedback: adaptChannelFeedbackFromStructuredFeedback(structuredFeedback),
    browsedAt: card.state.browsed_at,
    seenAt: card.state.seen_at,
    readFullAt: card.state.read_full_at,
    savedAt: card.state.saved_at,
    notInterestedAt: card.state.not_interested_at,
    note: null,
    hasCanonicalSnapshot: undefined,
    hasOriginalPageSnapshot: undefined,
    summarySource: card.summary_source,
    topicSource: card.topic_source,
    prioritySource: card.priority_source,
    artifactGeneratedAt: card.artifact_generated_at,
    primaryChannelId: card.primary_channel_id,
    secondaryChannels: card.secondary_channels.map((channel) => ({
      channelId: channel.channel_id,
      confidence: channel.confidence,
      displayMode: channel.display_mode,
    })),
    readingDepth: card.reading_depth,
    flowLane: card.flow_lane,
    routingConfidence: card.routing_confidence,
    routingSource: card.routing_source,
    routingReasonCodes: card.routing_reason_codes,
    promotionReasonCodes: card.promotion_reason_codes,
    isolationReasonCodes: card.isolation_reason_codes,
    sourcePolicyApplied: card.source_policy_applied,
    routingNotes: card.routing_notes,
    routingReasons: card.routing_reason_codes,
  };
}

function adaptHomepageBootstrapResponse(response: HomepageBootstrapDto): HomepageBootstrapResponse {
  return {
    ...adaptHomepageItemsResponse(response),
    summary: adaptHomepageSummary(response.summary),
  };
}

function adaptHomepageItemsResponse(response: HomepageItemsDto): HomepageItemsResponse {
  return {
    querySignature: response.query_signature,
    generatedAt: response.generated_at,
    items: response.items.map((card) => adaptHomepageCard(card)),
    nextCursor: response.next_cursor,
    hasMore: response.has_more,
    pageSize: response.page_size,
    snapshotToken: response.snapshot_token,
    snapshotExpiresAt: response.snapshot_expires_at,
    readModelStatus: adaptHomepageReadModelStatus(response.read_model_status),
  };
}

function adaptHomepageSummary(summary: HomepageSummaryDto): HomepageSummary {
  return {
    querySignature: summary.query_signature,
    generatedAt: summary.generated_at,
    totalCount: summary.total_count,
    unreadCount: summary.unread_count,
    savedCount: summary.saved_count,
    notInterestedCount: summary.not_interested_count,
    channelCounts: summary.channel_counts.map(adaptHomepageFacetCount),
    sourceCounts: summary.source_counts.map(adaptHomepageFacetCount),
    sourceCategoryCounts: summary.source_category_counts.map(adaptHomepageFacetCount),
    topicCounts: summary.topic_counts.map(adaptHomepageFacetCount),
    flowLaneCounts: summary.flow_lane_counts.map(adaptHomepageFacetCount),
    stateCounts: summary.state_counts.map(adaptHomepageFacetCount),
    readModelStatus: adaptHomepageReadModelStatus(summary.read_model_status),
  };
}

function adaptHomepageReadModelStatus(status: HomepageReadModelStatusDto | null | undefined) {
  if (!status) {
    return null;
  }
  return {
    state: status.state,
    missingCount: status.missing_count,
    staleCount: status.stale_count,
    message: status.message,
  };
}

function adaptHomepageFacetCount(facet: HomepageSummaryDto['channel_counts'][number]) {
  return {
    id: facet.id,
    label: facet.label,
    count: facet.count,
  };
}

function buildHomepageQueryString(options: HomepageQueryOptions) {
  const params = new URLSearchParams();
  setOptionalParam(params, 'channel_id', options.channelId);
  setOptionalParam(params, 'source_id', options.sourceId);
  setOptionalParam(params, 'source_category', options.sourceCategory);
  setOptionalParam(params, 'flow_lane', options.flowLane);
  setOptionalParam(params, 'reading_depth', options.readingDepth);
  setOptionalParam(params, 'state', options.state);
  setOptionalBooleanParam(params, 'saved', options.saved);
  setOptionalBooleanParam(params, 'not_interested', options.notInterested);
  setOptionalParam(params, 'topic', options.topic);
  setOptionalParam(params, 'search_text', options.searchText);
  setOptionalParam(params, 'time_window', options.timeWindow);
  setOptionalParam(params, 'sort', options.sort);
  setOptionalParam(params, 'page_size', options.pageSize);
  setOptionalParam(params, 'cursor', options.cursor);
  setOptionalParam(params, 'snapshot_token', options.snapshotToken);
  return params.toString();
}

function setOptionalParam(params: URLSearchParams, key: string, value: string | number | null | undefined) {
  if (value === undefined || value === null || value === '') {
    return;
  }
  params.set(key, String(value));
}

function setOptionalBooleanParam(params: URLSearchParams, key: string, value: boolean | null | undefined) {
  if (value === undefined || value === null) {
    return;
  }
  params.set(key, String(value));
}

function buildSourceAppName(sourceUrl: string, sourceKind: string) {
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

function stateTimestampFallback(state: HomepageCardDto['state']) {
  return (
    state.saved_at ??
    state.read_full_at ??
    state.seen_at ??
    state.browsed_at ??
    state.not_interested_at ??
    state.updated_at
  );
}
