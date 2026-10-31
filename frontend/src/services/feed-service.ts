import type {
  BehaviorViewContextDto,
  FeedItemChannelCorrection,
  FeedItemFeedbackInput,
  FeedItemChannelCorrectionInput,
  FeedItemStructuredFeedback,
  FeedItem,
  ItemStatus,
  ReaderBulkMarkSeenResponseDto,
  ReaderDetailDto,
  ReaderInsightFeedbackResponseDto,
  ReaderItemStateDto,
  ReaderListItemDto,
  ReaderStateMutationResponseDto,
  ReaderStructuredFeedbackDto,
  ReaderStructuredFeedbackInputDto,
} from '@/types/feed';
import {requestJson} from '@/api/client';
import {sourceCategoryIdFromFolderPath, sourceCategoryLabelFromFolderPath} from '@/lib/source-category';

type MutableItemStatus = Exclude<ItemStatus, 'unread'>;

export async function listFeedItems(options?: {status?: ItemStatus; limit?: number}) {
  const params = new URLSearchParams();
  if (options?.status) {
    params.set('status', options.status);
  }
  if (options?.limit !== undefined) {
    params.set('limit', String(options.limit));
  }

  const queryString = params.toString();
  const items = await requestJson<ReaderListItemDto[]>(`/reader/items${queryString ? `?${queryString}` : ''}`);
  return items.map((item) => adaptReaderItem(item));
}

export async function getFeedItemDetail(itemId: number) {
  const item = await requestJson<ReaderDetailDto>(`/reader/items/${itemId}`);
  return adaptReaderItem(item);
}

export async function saveFeedItemFeedback(
  itemId: number,
  input: FeedItemFeedbackInput,
  viewContext?: BehaviorViewContextDto | null,
) {
  const structuredFeedbackPayload = buildStructuredFeedbackWithChannelFeedbackPayload(
    input.structuredFeedback,
    input.channelFeedback,
  );
  const item = await requestJson<ReaderDetailDto>(`/reader/items/${itemId}/feedback`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      insight_feedback_text: input.insightFeedbackText,
      structured_feedback: structuredFeedbackPayload,
      view_context: viewContext ?? null,
    }),
  });
  return adaptReaderItem(item);
}

export async function saveFeedItemChannelCorrection(
  itemId: number,
  input: FeedItemChannelCorrectionInput,
  existingStructuredFeedback?: FeedItemStructuredFeedback | null,
  viewContext?: BehaviorViewContextDto | null,
) {
  const item = await requestJson<ReaderDetailDto>(`/reader/items/${itemId}/feedback`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      structured_feedback: buildStructuredFeedbackWithChannelFeedbackPayload(existingStructuredFeedback ?? null, input),
      view_context: viewContext ?? null,
    }),
  });
  return adaptReaderItem(item);
}

export async function clearFeedItemChannelCorrection(
  itemId: number,
  existingStructuredFeedback?: FeedItemStructuredFeedback | null,
  viewContext?: BehaviorViewContextDto | null,
) {
  const item = await requestJson<ReaderDetailDto>(`/reader/items/${itemId}/feedback`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      structured_feedback: buildStructuredFeedbackWithChannelClearPayload(existingStructuredFeedback ?? null),
      view_context: viewContext ?? null,
    }),
  });
  return adaptReaderItem(item);
}

export async function clearFeedItemAutoLowPriority(itemId: number, viewContext?: BehaviorViewContextDto | null) {
  return requestJson<ReaderInsightFeedbackResponseDto>(`/reader/items/${itemId}/clear_auto_low_priority`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildBehaviorContextRequest(viewContext)),
  });
}

export async function markFeedItemStatus(
  itemId: number,
  status: MutableItemStatus,
  viewContext?: BehaviorViewContextDto | null,
) {
  const endpoint = {
    browsed: 'mark_browsed',
    seen: 'mark_seen',
    read_full: 'mark_read_full',
    saved: 'mark_saved',
    not_interested: 'mark_not_interested',
  }[status];

  const response = await requestJson<ReaderStateMutationResponseDto>(`/reader/items/${itemId}/${endpoint}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildBehaviorContextRequest(viewContext)),
  });
  return response;
}

export async function unmarkFeedItemReadFull(itemId: number, viewContext?: BehaviorViewContextDto | null) {
  return requestJson<ReaderStateMutationResponseDto>(`/reader/items/${itemId}/unmark_read_full`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildBehaviorContextRequest(viewContext)),
  });
}

export async function unmarkFeedItemSeen(itemId: number, viewContext?: BehaviorViewContextDto | null) {
  return requestJson<ReaderStateMutationResponseDto>(`/reader/items/${itemId}/unmark_seen`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildBehaviorContextRequest(viewContext)),
  });
}

export async function unmarkFeedItemSaved(itemId: number, viewContext?: BehaviorViewContextDto | null) {
  return requestJson<ReaderStateMutationResponseDto>(`/reader/items/${itemId}/unmark_saved`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildBehaviorContextRequest(viewContext)),
  });
}

export async function unmarkFeedItemNotInterested(itemId: number, viewContext?: BehaviorViewContextDto | null) {
  return requestJson<ReaderStateMutationResponseDto>(`/reader/items/${itemId}/unmark_not_interested`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildBehaviorContextRequest(viewContext)),
  });
}

export async function bulkMarkSourceSeen(sourceId: number, viewContext?: BehaviorViewContextDto | null) {
  return requestJson<ReaderBulkMarkSeenResponseDto>('/reader/items/bulk/mark_seen', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({source_id: sourceId, view_context: viewContext ?? null}),
  });
}

export async function openFeedItemOriginal(itemId: number, viewContext?: BehaviorViewContextDto | null) {
  return requestJson<ReaderStateMutationResponseDto>(`/reader/items/${itemId}/open_original`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildBehaviorContextRequest(viewContext)),
  });
}

export function applyStateToFeedItem(
  item: FeedItem,
  state: ReaderItemStateDto,
  options?: {savedItemId?: number | null},
): FeedItem {
  const savedItemId =
    options && Object.prototype.hasOwnProperty.call(options, 'savedItemId')
      ? (options.savedItemId ?? null)
      : item.savedItemId;
  const isNotInterested = state.status === 'not_interested';
  const shouldKeepAutoLowPriority =
    !isNotInterested && item.isLowPriorityBucket && item.lowPriorityBucketReason === 'auto_score_threshold';

  return {
    ...item,
    status: state.status,
    browsedAt: state.browsed_at,
    seenAt: state.seen_at,
    readFullAt: state.read_full_at,
    savedAt: state.saved_at,
    savedItemId,
    notInterestedAt: state.not_interested_at,
    isLowPriorityBucket: isNotInterested || shouldKeepAutoLowPriority,
    lowPriorityBucketReason: isNotInterested
      ? 'not_interested'
      : shouldKeepAutoLowPriority
        ? 'auto_score_threshold'
        : null,
    autoLowPriorityCleared: isNotInterested ? false : item.autoLowPriorityCleared,
    isSaving: false,
  };
}

export function adaptReaderItem(item: ReaderListItemDto | ReaderDetailDto): FeedItem {
  const sourceAppUrl = item.source_site_url ?? item.source_url;
  const sourceAppName = buildSourceAppName(sourceAppUrl, item.source_kind);
  const structuredFeedback = adaptStructuredFeedback(item.structured_feedback);

  return {
    id: item.id,
    feedItemId: item.id,
    savedItemId: null,
    sourceId: item.source_id,
    title: item.title,
    summary: item.summary_text ?? '',
    feedContentText: 'feed_content_text' in item ? item.feed_content_text : undefined,
    content: 'content_text' in item ? item.content_text ?? '' : '',
    sourceName: item.source_name,
    sourceAppName,
    sourceFolder: sourceCategoryLabelFromFolderPath(item.source_folder_path),
    sourceCategoryId: sourceCategoryIdFromFolderPath(item.source_folder_path),
    publishedAt: item.published_at ?? stateTimestampFallback(item.state),
    topics: [],
    status: item.state.status,
    relevanceScore: null,
    priorityBand: null,
    priorityReasons: [],
    isLowPriorityBucket: item.state.status === 'not_interested',
    lowPriorityBucketReason: item.state.status === 'not_interested' ? 'not_interested' : null,
    autoLowPriorityCleared: false,
    originalUrl: item.original_url,
    sourceAppUrl,
    contentTextSource: item.content_text_source,
    contentExtractionStatus: item.content_extraction_status,
    contentExtractionAttemptedAt: item.content_extraction_attempted_at,
    contentExtractedAt: item.content_extracted_at,
    contentExtractionError: item.content_extraction_error,
    contentExtractionMethod: item.content_extraction_method,
    contentExtractionFinalUrl: item.content_extraction_final_url,
    contentExtractionVisibleCharCount: item.content_extraction_visible_char_count,
    insightFeedbackText: item.insight_feedback_text,
    structuredFeedback,
    channelFeedback: adaptChannelFeedbackFromStructuredFeedback(structuredFeedback),
    browsedAt: item.state.browsed_at,
    seenAt: item.state.seen_at,
    readFullAt: item.state.read_full_at,
    savedAt: item.state.saved_at,
    notInterestedAt: item.state.not_interested_at,
    note: null,
    hasCanonicalSnapshot: undefined,
    hasOriginalPageSnapshot: undefined,
    summarySource: undefined,
    topicSource: undefined,
    prioritySource: undefined,
    artifactGeneratedAt: null,
  };
}

export function adaptChannelFeedbackFromStructuredFeedback(
  feedback: FeedItemStructuredFeedback | null,
): FeedItemChannelCorrection | null {
  if (!feedback || !structuredFeedbackHasChannelCorrection(feedback)) {
    return null;
  }
  return {
    primaryChannelId: feedback.primaryChannelCorrection,
    addChannelIds: feedback.channelAddIds,
    removeChannelIds: feedback.channelRemoveIds,
    readingDepth: feedback.readingDepthCorrection,
    flowLane: feedback.flowLaneCorrection,
    notes: feedback.channelCorrectionNotes,
    updatedAt: feedback.updatedAt ?? null,
  };
}

export function adaptStructuredFeedback(payload: ReaderStructuredFeedbackDto | null): FeedItemStructuredFeedback | null {
  if (!payload) {
    return null;
  }
  return {
    overallValue: payload.overall_value,
    priorityCorrection: payload.priority_correction,
    topicAddIds: payload.topic_add_ids,
    topicRemoveIds: payload.topic_remove_ids,
    contentTypeCorrection: payload.content_type_correction,
    reasonCodeAddIds: payload.reason_code_add_ids,
    reasonCodeRemoveIds: payload.reason_code_remove_ids,
    issueMarkers: payload.issue_markers,
    primaryChannelCorrection: payload.primary_channel_correction,
    channelAddIds: payload.channel_add_ids,
    channelRemoveIds: payload.channel_remove_ids,
    readingDepthCorrection: payload.reading_depth_correction,
    flowLaneCorrection: payload.flow_lane_correction,
    channelCorrectionNotes: payload.channel_correction_notes,
    createdAt: payload.created_at ?? null,
    updatedAt: payload.updated_at ?? null,
  };
}

export function adaptStructuredFeedbackResponse(payload: ReaderStructuredFeedbackDto | null) {
  return adaptStructuredFeedback(payload);
}

function buildStructuredFeedbackPayload(feedback: FeedItemStructuredFeedback): ReaderStructuredFeedbackInputDto {
  const payload: ReaderStructuredFeedbackInputDto = {
    overall_value: feedback.overallValue,
    priority_correction: feedback.priorityCorrection,
    topic_add_ids: feedback.topicAddIds,
    topic_remove_ids: feedback.topicRemoveIds,
    content_type_correction: feedback.contentTypeCorrection,
    reason_code_add_ids: feedback.reasonCodeAddIds,
    reason_code_remove_ids: feedback.reasonCodeRemoveIds,
    issue_markers: feedback.issueMarkers,
  };
  if (structuredFeedbackHasChannelCorrection(feedback)) {
    Object.assign(payload, buildChannelFeedbackPayload(feedback));
  }
  return payload;
}

function buildStructuredFeedbackWithChannelFeedbackPayload(
  structuredFeedback: FeedItemStructuredFeedback | null,
  channelFeedback: FeedItemChannelCorrectionInput | null,
): ReaderStructuredFeedbackInputDto | null {
  const payload = structuredFeedback ? buildStructuredFeedbackPayload(structuredFeedback) : {};
  if (channelFeedback) {
    Object.assign(payload, buildChannelFeedbackPayload(channelFeedback));
  }
  return Object.keys(payload).length > 0 ? payload : null;
}

function buildStructuredFeedbackWithChannelClearPayload(
  structuredFeedback: FeedItemStructuredFeedback | null,
): ReaderStructuredFeedbackInputDto {
  return {
    ...(structuredFeedback ? buildRegularStructuredFeedbackPayload(structuredFeedback) : {}),
    primary_channel_correction: null,
    channel_add_ids: [],
    channel_remove_ids: [],
    reading_depth_correction: null,
    flow_lane_correction: null,
    channel_correction_notes: '',
  };
}

function buildRegularStructuredFeedbackPayload(feedback: FeedItemStructuredFeedback): ReaderStructuredFeedbackInputDto {
  return {
    overall_value: feedback.overallValue,
    priority_correction: feedback.priorityCorrection,
    topic_add_ids: feedback.topicAddIds,
    topic_remove_ids: feedback.topicRemoveIds,
    content_type_correction: feedback.contentTypeCorrection,
    reason_code_add_ids: feedback.reasonCodeAddIds,
    reason_code_remove_ids: feedback.reasonCodeRemoveIds,
    issue_markers: feedback.issueMarkers,
  };
}

function buildChannelFeedbackPayload(
  feedback: FeedItemChannelCorrectionInput | FeedItemStructuredFeedback,
): ReaderStructuredFeedbackInputDto {
  return {
    primary_channel_correction:
      'primaryChannelId' in feedback ? feedback.primaryChannelId : feedback.primaryChannelCorrection,
    channel_add_ids: 'addChannelIds' in feedback ? feedback.addChannelIds : feedback.channelAddIds,
    channel_remove_ids: 'removeChannelIds' in feedback ? feedback.removeChannelIds : feedback.channelRemoveIds,
    reading_depth_correction: 'readingDepth' in feedback ? feedback.readingDepth : feedback.readingDepthCorrection,
    flow_lane_correction: 'flowLane' in feedback ? feedback.flowLane : feedback.flowLaneCorrection,
    channel_correction_notes: 'notes' in feedback ? feedback.notes : feedback.channelCorrectionNotes,
  };
}

function structuredFeedbackHasChannelCorrection(feedback: FeedItemStructuredFeedback): boolean {
  return Boolean(
    feedback.primaryChannelCorrection ||
      feedback.channelAddIds.length > 0 ||
      feedback.channelRemoveIds.length > 0 ||
      feedback.readingDepthCorrection ||
      feedback.flowLaneCorrection ||
      feedback.channelCorrectionNotes.trim(),
  );
}

function buildBehaviorContextRequest(viewContext?: BehaviorViewContextDto | null) {
  return {view_context: viewContext ?? null};
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

function stateTimestampFallback(state: ReaderItemStateDto) {
  return (
    state.saved_at ??
    state.read_full_at ??
    state.seen_at ??
    state.browsed_at ??
    state.not_interested_at ??
    state.updated_at
  );
}
