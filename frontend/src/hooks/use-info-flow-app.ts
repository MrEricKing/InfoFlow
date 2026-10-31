import {useDeferredValue, useEffect, useMemo, useRef, useState} from 'react';
import {
  selectKnowledgeBaseVisibleItems,
  selectReaderVisibleItems,
} from '@/lib/feed-filters';
import {
  applyStateToFeedItem,
  adaptStructuredFeedbackResponse,
  bulkMarkSourceSeen,
  clearFeedItemChannelCorrection,
  clearFeedItemAutoLowPriority,
  getFeedItemDetail,
  listFeedItems,
  markFeedItemStatus,
  openFeedItemOriginal,
  saveFeedItemFeedback,
  unmarkFeedItemReadFull,
  unmarkFeedItemSeen,
  unmarkFeedItemNotInterested,
  unmarkFeedItemSaved,
} from '@/services/feed-service';
import {
  clearSavedItemCuratedInsight as deleteSavedItemCuratedInsight,
  getSavedItemDetail,
  listSavedItems,
  removeSavedItem as postRemoveSavedItem,
  updateSavedItemCuratedInsight as putSavedItemCuratedInsight,
  updateSavedItemNote as patchSavedItemNote,
} from '@/services/knowledge-service';
import {
  listSources,
  updateSource,
} from '@/services/source-service';
import {listInsightChannels} from '@/services/settings-service';
import {
  startFeedItemExtractOperation,
  startLocalMirrorExportOperation,
  startManualUrlCaptureOperation,
  startRetryCaptureOperation,
  startSavedItemManualImportOperation,
  startSavedItemInsightOperation,
  startUploadedMaterialCaptureOperation,
} from '@/services/operation-service';
import type {
  CaptureOperation,
  BehaviorViewContextDto,
  FeedItem,
  FeedItemFeedbackInput,
  FeedItemRoutingCorrectionInput,
  GroupMode,
  ItemStatus,
  KnowledgeScope,
  ManualUrlCaptureInput,
  ReaderStateMutationResponseDto,
  ReadingDepthFilter,
  SavedItemAIRoute,
  SavedItemCuratedInsightInput,
  SavedItemManualImportInput,
  SortMode,
  SourceScope,
  ThemeMode,
  TimeWindow,
  UploadedMaterialCaptureInput,
  ViewMode,
} from '@/types/feed';
import type {
  InsightChannel,
} from '@/types/settings';
import type {
  AppOperationDto,
  SourceDto,
} from '@/types/source';

import {
  ALL_SOURCES_SCOPE,
  CAPTURE_RUNNING_DETAIL,
  CAPTURE_RUNNING_MESSAGE,
  THEME_STORAGE_KEY,
  applyCaptureOperation,
  behaviorSurfaceForView,
  buildOriginalOpenedMessage,
  buildSourceUpdatePayload,
  countByFacetId,
  countByFacetNumericId,
  formatDetailLoadError,
  formatListError,
  getInitialTheme,
  getOperationFeedItemId,
  getOperationSavedItemId,
  hasHomepageMembershipMutationFilter,
  idleListLoadState,
  isExternalSavedItem,
  isFeedItemInsightArtifactItemOperation,
  isUnavailableDetailError,
  loadingListLoadState,
  mapHomepageChannelFacets,
  mapHomepageFlowLaneFacets,
  mapHomepageTopicFacets,
  markCapturePending,
  markCaptureRequestFailed,
  markSaveRequestPending,
  mergeFeedItem,
  mergeStructuredFeedbackWithRoutingCorrection,
  rollbackSaveRequest,
  sourceCategoryLabelFromId,
  updateCollection,
  updateCollectionByFeedItemId,
  withBehaviorReadingDepth,
  withBehaviorSearchQuery,
  withBehaviorSourceScope,
  withListRetry,
  type ListLoadState,
} from '@/hooks/info-flow/helpers';
import {useInfoFlowOperations} from '@/hooks/info-flow/use-info-flow-operations';
import {useHomepageReadModel} from '@/hooks/info-flow/use-homepage-read-model';
import {useSourceEditorController} from '@/hooks/info-flow/use-source-editor-controller';

export function useInfoFlowApp() {
  const [theme, setTheme] = useState<ThemeMode>(getInitialTheme);
  const [view, setViewState] = useState<ViewMode>('ai');
  const [groupMode, setGroupModeState] = useState<GroupMode>('time');
  const [activeFacet, setActiveFacet] = useState<string | null>(null);
  const [sourceScope, setSourceScope] = useState<SourceScope>(ALL_SOURCES_SCOPE);
  const [readingDepthFilter, setReadingDepthFilterState] = useState<ReadingDepthFilter>('all');
  const [knowledgeScope, setKnowledgeScope] = useState<KnowledgeScope>('all');
  const [knowledgeSourceScope, setKnowledgeSourceScope] = useState<SourceScope>(ALL_SOURCES_SCOPE);
  const [aiSortMode, setAiSortMode] = useState<SortMode>('latest');
  const [readerSortMode, setReaderSortMode] = useState<SortMode>('latest');
  const [aiUnreadOnly, setAiUnreadOnly] = useState(false);
  const [readerUnreadOnly, setReaderUnreadOnly] = useState(false);
  const [aiTimeWindow, setAiTimeWindow] = useState<TimeWindow>('all');
  const [readerTimeWindow, setReaderTimeWindow] = useState<TimeWindow>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [readerItems, setReaderItems] = useState<FeedItem[]>([]);
  const [savedItems, setSavedItems] = useState<FeedItem[]>([]);
  const [sources, setSources] = useState<SourceDto[]>([]);
  const [insightChannels, setInsightChannels] = useState<InsightChannel[]>([]);
  const [insightChannelLoadError, setInsightChannelLoadError] = useState<string | null>(null);
  const [feedDetailItems, setFeedDetailItems] = useState<Record<number, FeedItem>>({});
  const [savedDetailItems, setSavedDetailItems] = useState<Record<number, FeedItem>>({});
  const [captureOperations] = useState<Record<number, CaptureOperation>>({});
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [shouldLoadSelectedDetail, setShouldLoadSelectedDetail] = useState(false);
  const [unavailableFeedItemIds, setUnavailableFeedItemIds] = useState<Set<number>>(new Set());
  const [feedDetailErrorMessages, setFeedDetailErrorMessages] = useState<Record<number, string>>({});
  const [readerLoadState, setReaderLoadState] = useState<ListLoadState>(idleListLoadState);
  const [savedLoadState, setSavedLoadState] = useState<ListLoadState>(idleListLoadState);
  const [readerActionMessage, setReaderActionMessage] = useState<string | null>(null);
  const readerRequestIdRef = useRef(0);
  const savedRequestIdRef = useRef(0);
  const staleFeedItemRef = useRef<FeedItem | null>(null);
  const selectedIdRef = useRef<number | null>(selectedId);
  const viewRef = useRef<ViewMode>(view);
  const viewRevisionRef = useRef(0);
  const deferredSearchQuery = useDeferredValue(searchQuery.trim());
  const deferredSearchQueryRef = useRef(deferredSearchQuery);
  const clientSearchQuery = deferredSearchQuery.toLowerCase();
  const {
    homepageItems,
    setHomepageItems,
    homepageSummary,
    homepageReadModelStatus,
    homepageWindowState,
    homepageLoadState,
    homepageQuery,
    homepageQuerySignature,
    reloadHomepageItems,
    refreshHomepageSummary,
    invalidateHomepageSnapshotWindow,
    loadMoreHomepageItems,
  } = useHomepageReadModel({
    activeFacet,
    aiSortMode,
    aiTimeWindow,
    aiUnreadOnly,
    deferredSearchQuery,
    groupMode,
    readingDepthFilter,
    sourceScope,
    view,
  });
  const {
    activeItemOperations,
    feedItemArtifactRefreshVersion,
    isRebuildingInsightEngineArtifacts,
    insightEngineArtifactMessage,
    setInsightEngineArtifactMessage,
    insightEngineArtifactOperation,
    insightEngineArtifactProgress,
    insightEngineArtifactBusyReason,
    isInsightEngineArtifactStopRequesting,
    latestItemOperations,
    isSyncingSources,
    isSourceSyncStopRequesting,
    sourceSyncOperation,
    sourceSyncMessage,
    setSourceSyncMessage,
    sourceSyncProgress,
    sourceSyncBusyReason,
    rebuildInsightEngineArtifactsNow,
    runFeedItemInsightArtifactOperation,
    startAutoCreateSourceSync,
    stopInsightEngineArtifactsNow,
    stopSourcesSyncNow,
    syncSourceIdsNow,
    syncSingleSourceNow,
    syncSourcesNow,
    trackBackendOperation,
    trackItemOperation,
  } = useInfoFlowOperations({
    view,
    sourceScope,
    reloadSources,
    reloadInsightChannels,
    refreshHomepageSummary,
    invalidateHomepageSnapshotWindow,
    reloadReaderItems,
    reloadSavedItems,
    getSavedQuery: () => deferredSearchQueryRef.current,
    getSelectedId: () => selectedIdRef.current,
    getView: () => viewRef.current,
    setFeedDetailItems,
    setSavedDetailItems,
  });
  const {
    sourceEditor,
    isPreviewingSource,
    isSavingSource,
    closeSourceEditor,
    deleteSourceEditorProfileDraft,
    openSourceEditor,
    previewQuickSource,
    saveSourceEditor,
    saveSourceEditorProfileDraft,
  } = useSourceEditorController({
    insightChannelCount: insightChannels.length,
    reloadInsightChannels,
    reloadSources,
    reloadHomepageItems,
    setSourceSyncMessage,
    shouldReloadHomepageAfterSourceSave: () => viewRef.current === 'ai',
    startAutoCreateSourceSync,
  });

  async function reloadSources() {
    const nextSources = await listSources();
    setSources(nextSources);
    return nextSources;
  }

  async function reloadInsightChannels() {
    try {
      const nextChannels = await listInsightChannels();
      setInsightChannels(nextChannels);
      setInsightChannelLoadError(null);
      return nextChannels;
    } catch (error) {
      console.error('Failed to load insight channels', error);
      const message = error instanceof Error ? error.message : 'Insight channels failed to load.';
      setInsightChannelLoadError(message);
      return [];
    }
  }

  async function reloadReaderItems() {
    const requestId = readerRequestIdRef.current + 1;
    readerRequestIdRef.current = requestId;
    setReaderLoadState(loadingListLoadState);

    try {
      const nextReaderItems = await withListRetry(() => listFeedItems());
      if (readerRequestIdRef.current === requestId) {
        setReaderItems(nextReaderItems);
        setReaderLoadState({status: 'ready', errorMessage: null});
      }
      return nextReaderItems;
    } catch (error) {
      console.error('Failed to load reader items', error);
      if (readerRequestIdRef.current === requestId) {
        setReaderLoadState({status: 'error', errorMessage: formatListError(error, '经典阅读列表')});
      }
      throw error;
    }
  }

  async function reloadSavedItems(options?: {query?: string}) {
    const requestId = savedRequestIdRef.current + 1;
    savedRequestIdRef.current = requestId;
    setSavedLoadState(loadingListLoadState);

    try {
      const nextSavedItems = await withListRetry(() => listSavedItems({query: options?.query}));
      if (savedRequestIdRef.current === requestId) {
        setSavedItems(nextSavedItems);
        setSavedLoadState({status: 'ready', errorMessage: null});
      }
      return nextSavedItems;
    } catch (error) {
      console.error('Failed to load saved items', error);
      if (savedRequestIdRef.current === requestId) {
        setSavedLoadState({status: 'error', errorMessage: formatListError(error, '保存内容')});
      }
      throw error;
    }
  }

  async function reloadCoreLists(options?: {savedQuery?: string}) {
    const shouldReloadReader = readerLoadState.status !== 'idle';
    const shouldReloadSaved = view === 'kb' || savedLoadState.status !== 'idle';
    const [nextHomepageItems, nextReaderItems, nextSavedItems] = await Promise.all([
      reloadHomepageItems(),
      shouldReloadReader ? reloadReaderItems() : Promise.resolve(readerItems),
      shouldReloadSaved ? reloadSavedItems({query: options?.savedQuery}) : Promise.resolve(savedItems),
    ]);

    return {nextHomepageItems, nextReaderItems, nextSavedItems};
  }

  useEffect(() => {
    let isCancelled = false;

    async function loadSourceList() {
      try {
        const nextSources = await listSources();
        if (!isCancelled) {
          setSources(nextSources);
        }
      } catch (error) {
        console.error('Failed to load sources', error);
      }
    }

    void loadSourceList();
    void reloadInsightChannels();

    return () => {
      isCancelled = true;
    };
  }, []);

  useEffect(() => {
    void reloadHomepageItems().catch(() => {
      // The visible list renders the error state.
    });
  }, [homepageQuerySignature]);

  useEffect(() => {
    if (view !== 'reader' || readerLoadState.status !== 'idle') {
      return;
    }

    void reloadReaderItems().catch(() => {
      // The visible list renders the error state.
    });
  }, [readerLoadState.status, view]);

  useEffect(() => {
    if (view !== 'kb') {
      return;
    }

    void reloadSavedItems({query: deferredSearchQuery}).catch(() => {
      // The visible list renders the error state.
    });
  }, [deferredSearchQuery, view]);

  useEffect(() => {
    if (selectedId === null) {
      return;
    }
    if (!shouldLoadSelectedDetail) {
      return;
    }

    const itemId = selectedId;
    let isCancelled = false;

    async function loadSelectedItemDetail() {
      try {
        if (view === 'kb') {
          if (!savedItems.some((item) => item.id === itemId)) {
            return;
          }

          const detailItem = await getSavedItemDetail(itemId);
          if (!isCancelled) {
            setSavedDetailItems((current) => ({
              ...current,
              [itemId]: mergeFeedItem(current[itemId], detailItem),
            }));
          }
          return;
        }

        const staleItem = staleFeedItemRef.current;
        const relevantItems =
          view === 'ai' && staleItem?.id === itemId ? [...homepageItems, staleItem] : view === 'ai' ? homepageItems : readerItems;
        if (!relevantItems.some((item) => item.id === itemId)) {
          return;
        }

        const detailItem = await getFeedItemDetail(itemId);
        if (!isCancelled) {
          setFeedDetailItems((current) => ({
            ...current,
            [itemId]: mergeFeedItem(current[itemId], detailItem),
          }));
          setUnavailableFeedItemIds((current) => {
            if (!current.has(itemId)) {
              return current;
            }
            const next = new Set(current);
            next.delete(itemId);
            return next;
          });
          setFeedDetailErrorMessages((current) => {
            if (!(itemId in current)) {
              return current;
            }
            const next = {...current};
            delete next[itemId];
            return next;
          });
        }
      } catch (error) {
        console.error(view === 'kb' ? 'Failed to load saved item detail' : 'Failed to load feed item detail', error);
        if (!isCancelled && view !== 'kb') {
          if (isUnavailableDetailError(error)) {
            setUnavailableFeedItemIds((current) => new Set(current).add(itemId));
            setFeedDetailErrorMessages((current) => {
              if (!(itemId in current)) {
                return current;
              }
              const next = {...current};
              delete next[itemId];
              return next;
            });
          } else {
            setUnavailableFeedItemIds((current) => {
              if (!current.has(itemId)) {
                return current;
              }
              const next = new Set(current);
              next.delete(itemId);
              return next;
            });
            setFeedDetailErrorMessages((current) => ({
              ...current,
              [itemId]: formatDetailLoadError(error),
            }));
          }
        }
      }
    }

    void loadSelectedItemDetail();

    return () => {
      isCancelled = true;
    };
  }, [homepageItems, readerItems, savedItems, selectedId, shouldLoadSelectedDetail, view]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  useEffect(() => {
    if (viewRef.current !== view) {
      viewRevisionRef.current += 1;
    }
    viewRef.current = view;
  }, [view]);

  useEffect(() => {
    deferredSearchQueryRef.current = deferredSearchQuery;
  }, [deferredSearchQuery]);

  const visibleHomepageItems = homepageItems;

  const visibleReaderItems = useMemo(
    () =>
      selectReaderVisibleItems({
        items: readerItems,
        sortMode: readerSortMode,
        sourceScope,
        unreadOnly: readerUnreadOnly,
        query: clientSearchQuery,
        timeWindow: readerTimeWindow,
      }),
    [clientSearchQuery, readerItems, readerSortMode, readerTimeWindow, readerUnreadOnly, sourceScope],
  );

  const backendCaptureOperations = useMemo<Record<number, CaptureOperation>>(() => {
    const entries: Array<[number, CaptureOperation]> = [];
    for (const operation of Object.values(activeItemOperations)) {
      if (operation.operation_type !== 'knowledge_capture') {
        continue;
      }
      const savedItemId = getOperationSavedItemId(operation);
      if (savedItemId === null) {
        continue;
      }
      entries.push([
        savedItemId,
        {
          savedItemId,
          operationKind: operation.operation_kind,
          status: 'running',
          startedAt: operation.started_at ?? operation.created_at,
          updatedAt: operation.updated_at,
          message: operation.message ?? CAPTURE_RUNNING_MESSAGE,
          detail: operation.progress?.stage ?? CAPTURE_RUNNING_DETAIL,
          error: null,
        },
      ]);
    }
    return Object.fromEntries(entries) as Record<number, CaptureOperation>;
  }, [activeItemOperations]);
  const combinedCaptureOperations = useMemo<Record<number, CaptureOperation>>(
    () => ({...captureOperations, ...backendCaptureOperations}),
    [backendCaptureOperations, captureOperations],
  );

  const savedItemsWithCaptureOperations = useMemo(
    () => savedItems.map((item) => applyCaptureOperation(item, combinedCaptureOperations[item.savedItemId ?? item.id])),
    [combinedCaptureOperations, savedItems],
  );
  const savedDetailItemsWithCaptureOperations = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(savedDetailItems).map(([id, item]) => [
          id,
          applyCaptureOperation(item, combinedCaptureOperations[item.savedItemId ?? item.id]),
        ]),
      ),
    [combinedCaptureOperations, savedDetailItems],
  );

  const visibleSavedItems = useMemo(
    () =>
      selectKnowledgeBaseVisibleItems({
        items: savedItemsWithCaptureOperations,
        knowledgeScope,
        sourceScope: knowledgeSourceScope,
      }),
    [knowledgeScope, knowledgeSourceScope, savedItemsWithCaptureOperations],
  );

  const visibleItems = view === 'ai' ? visibleHomepageItems : view === 'reader' ? visibleReaderItems : visibleSavedItems;

  useEffect(() => {
    if (!visibleItems.length) {
      if (view === 'ai' && selectedId !== null && staleFeedItemRef.current?.id === selectedId) {
        return;
      }
      setSelectedId(null);
      setShouldLoadSelectedDetail(false);
      return;
    }

    if (selectedId === null || !visibleItems.some((item) => item.id === selectedId)) {
      if (view === 'ai' && selectedId !== null && staleFeedItemRef.current?.id === selectedId) {
        return;
      }
      setSelectedId(visibleItems[0].id);
      setShouldLoadSelectedDetail(view !== 'kb');
    }
  }, [selectedId, view, visibleItems]);

  useEffect(() => {
    if (view === 'kb' || selectedId === null || shouldLoadSelectedDetail) {
      return;
    }
    if (feedDetailItems[selectedId]?.feedContentText !== undefined) {
      return;
    }

    setShouldLoadSelectedDetail(true);
  }, [feedDetailItems, selectedId, shouldLoadSelectedDetail, view]);

  const feedItemLookup = useMemo(() => {
    const byId = new Map<number, FeedItem>();
    const collections = [homepageItems, readerItems, Object.values(feedDetailItems)];

    for (const collection of collections) {
      for (const item of collection) {
        byId.set(item.id, mergeFeedItem(byId.get(item.id), item));
      }
    }

    return byId;
  }, [feedDetailItems, homepageItems, readerItems]);

  const savedItemLookup = useMemo(() => {
    const byId = new Map<number, FeedItem>();
    const collections = [savedItemsWithCaptureOperations, Object.values(savedDetailItemsWithCaptureOperations)];

    for (const collection of collections) {
      for (const item of collection) {
        byId.set(item.id, mergeFeedItem(byId.get(item.id), item));
      }
    }

    return byId;
  }, [savedDetailItemsWithCaptureOperations, savedItemsWithCaptureOperations]);

  const selectedItem =
    selectedId === null
      ? null
      : view === 'kb'
        ? (savedItemLookup.get(selectedId) ?? null)
        : (feedItemLookup.get(selectedId) ?? (staleFeedItemRef.current?.id === selectedId ? staleFeedItemRef.current : null));
  const selectedItemVisible = selectedId !== null && visibleItems.some((item) => item.id === selectedId);
  const selectedItemStale = Boolean(view === 'ai' && selectedItem && !selectedItemVisible);
  const selectedItemUnavailable = Boolean(
    view !== 'kb' && selectedId !== null && unavailableFeedItemIds.has(selectedId),
  );
  const selectedItemDetailErrorMessage =
    view !== 'kb' && selectedId !== null && !selectedItemUnavailable
      ? (feedDetailErrorMessages[selectedId] ?? null)
      : null;
  const selectedSavedItemId = view === 'kb' && selectedItem ? (selectedItem.savedItemId ?? selectedItem.id) : null;

  useEffect(() => {
    if (view !== 'kb' && selectedItem) {
      staleFeedItemRef.current = selectedItem;
    }
  }, [selectedItem, view]);

  const selectedCaptureOperation =
    selectedSavedItemId !== null ? (combinedCaptureOperations[selectedSavedItemId] ?? null) : null;
  const selectedDetailOperations = useMemo(() => {
    const feedItemId = selectedItem?.feedItemId ?? (view !== 'kb' ? selectedItem?.id : null) ?? null;
    const savedItemId = selectedSavedItemId;
    const savedItemInsightOperation = selectLatestSavedItemInsightOperation(latestItemOperations, savedItemId);
    return {
      isExtractingContent: Object.values(activeItemOperations).some(
        (operation) => operation.operation_type === 'feed_item_extract' && getOperationFeedItemId(operation) === feedItemId,
      ),
      isRebuildingInsightEngineArtifacts: Object.values(activeItemOperations).some(
        (operation) => isFeedItemInsightArtifactItemOperation(operation) && getOperationFeedItemId(operation) === feedItemId,
      ),
      isRebuildingSavedItemInsight: Object.values(activeItemOperations).some(
        (operation) => operation.operation_type === 'saved_item_insight' && getOperationSavedItemId(operation) === savedItemId,
      ),
      savedItemInsightOperation,
      isReexportingMirror: Object.values(activeItemOperations).some(
        (operation) => operation.operation_type === 'local_mirror_export' && getOperationSavedItemId(operation) === savedItemId,
      ),
    };
  }, [activeItemOperations, latestItemOperations, selectedItem, selectedSavedItemId, view]);
  const topicFacets = useMemo(() => mapHomepageTopicFacets(homepageSummary), [homepageSummary]);
  const channelFacets = useMemo(
    () => mapHomepageChannelFacets(homepageSummary, insightChannels),
    [homepageSummary, insightChannels],
  );
  const flowLaneFacets = useMemo(() => mapHomepageFlowLaneFacets(homepageSummary), [homepageSummary]);
  const insightChannelLabels = useMemo(
    () => new Map(insightChannels.map((channel) => [channel.id, channel.label])),
    [insightChannels],
  );
  const sourceCategorySuggestions = useMemo(
    () =>
      [...new Set(sources.map((source) => source.folder_path).filter((value): value is string => Boolean(value)))]
        .sort((left, right) => left.localeCompare(right, 'zh-CN')),
    [sources],
  );
  const sidebarUnreadOnly = view === 'ai' ? aiUnreadOnly : view === 'reader' ? readerUnreadOnly : false;
  const knowledgeSourceCountItems = useMemo(
    () => savedItemsWithCaptureOperations.filter((item) => item.originType === 'feed_saved' && item.sourceId !== null),
    [savedItemsWithCaptureOperations],
  );
  const sourceCountBaseItems = view === 'kb' ? knowledgeSourceCountItems : readerItems;
  const sourceCountItems = useMemo(
    () => (sidebarUnreadOnly ? sourceCountBaseItems.filter((item) => item.status === 'unread') : sourceCountBaseItems),
    [sidebarUnreadOnly, sourceCountBaseItems],
  );
  const sourceItemCounts = useMemo(() => {
    if (view === 'ai') {
      const bySourceId = countByFacetNumericId(homepageSummary?.sourceCounts ?? []);
      const byCategory = new Map<string, number>();
      for (const facet of homepageSummary?.sourceCategoryCounts ?? []) {
        byCategory.set(facet.id, facet.count);
      }
      return {bySourceId, byCategory};
    }

    const bySourceId = new Map<number, number>();
    const byCategory = new Map<string, number>();
    for (const item of sourceCountItems) {
      if (item.sourceId !== null) {
        bySourceId.set(item.sourceId, (bySourceId.get(item.sourceId) ?? 0) + 1);
      }
      byCategory.set(item.sourceCategoryId, (byCategory.get(item.sourceCategoryId) ?? 0) + 1);
    }
    return {bySourceId, byCategory};
  }, [homepageSummary, sourceCountItems, view]);
  const countItems = view === 'reader' ? readerItems : [];
  const sidebarCountItems = useMemo(
    () => (sidebarUnreadOnly ? countItems.filter((item) => item.status === 'unread') : countItems),
    [countItems, sidebarUnreadOnly],
  );
  const homepageFlowLaneCountById = useMemo(
    () => countByFacetId(homepageSummary?.flowLaneCounts ?? []),
    [homepageSummary],
  );
  const homepageStateCountById = useMemo(
    () => countByFacetId(homepageSummary?.stateCounts ?? []),
    [homepageSummary],
  );
  const supportsPriorityMode = homepageItems.some((item) => item.relevanceScore !== null);
  const supportsTopicMode = topicFacets.length > 0;
  const insightEngineArtifactStats = useMemo(
    () => ({
      summaryArtifactCount: homepageItems.filter((item) => item.summarySource === 'insight_engine_artifact').length,
      summaryFallbackCount: homepageItems.filter((item) => item.summarySource !== 'insight_engine_artifact').length,
      priorityArtifactCount: homepageItems.filter((item) => item.prioritySource === 'insight_engine_artifact').length,
      priorityFallbackCount: homepageItems.filter((item) => item.prioritySource !== 'insight_engine_artifact').length,
    }),
    [homepageItems],
  );

  const counts = useMemo(
    () => ({
      unread: view === 'ai' ? (homepageSummary?.unreadCount ?? 0) : sidebarCountItems.length,
      saved: savedItems.length,
      readFull:
        view === 'ai'
          ? (homepageStateCountById.get('read_full') ?? 0)
          : sidebarCountItems.filter((item) => item.status === 'read_full').length,
      lowPriority: view === 'ai' ? (homepageFlowLaneCountById.get('low_priority') ?? 0) : 0,
      topicCount: view === 'ai' ? (homepageSummary?.totalCount ?? 0) : 0,
      sourceCount: view === 'ai' ? (homepageSummary?.totalCount ?? 0) : sourceCountItems.length,
    }),
    [
      homepageFlowLaneCountById,
      homepageStateCountById,
      homepageSummary,
      savedItems.length,
      sidebarCountItems,
      sourceCountItems.length,
      view,
    ],
  );
  const knowledgeCounts = useMemo(
    () => ({
      all: savedItemsWithCaptureOperations.length,
      feedSaved: savedItemsWithCaptureOperations.filter((item) => item.originType === 'feed_saved').length,
      externalSource: savedItemsWithCaptureOperations.filter(isExternalSavedItem).length,
    }),
    [savedItemsWithCaptureOperations],
  );
  const sidebarSourceScope = view === 'kb' ? knowledgeSourceScope : sourceScope;

  function shouldPreserveSavedSelection(originalSavedItemId: number) {
    return viewRef.current === 'kb' && selectedIdRef.current === originalSavedItemId;
  }

  function toggleTheme() {
    setTheme((current) => (current === 'light' ? 'dark' : 'light'));
  }

  function setView(nextView: ViewMode) {
    if (nextView !== 'ai' && (groupMode === 'channel' || groupMode === 'flow_lane')) {
      setActiveFacet(null);
    }
    if (nextView === 'ai') {
      void reloadInsightChannels();
    }
    if (nextView === 'kb') {
      setKnowledgeScope('all');
      setKnowledgeSourceScope(ALL_SOURCES_SCOPE);
      setActiveFacet(null);
    }
    setViewState(nextView);
  }

  function selectKnowledgeScope(nextScope: KnowledgeScope) {
    setViewState('kb');
    setKnowledgeScope(nextScope);
    setKnowledgeSourceScope(ALL_SOURCES_SCOPE);
    setActiveFacet(null);
  }

  function setGroupMode(nextMode: GroupMode) {
    if (nextMode === 'topic' && !supportsTopicMode) {
      return;
    }

    setViewState('ai');
    setGroupModeState(nextMode);
    setSelectedId(null);
    setActiveFacet(null);
    if (nextMode !== 'source') {
      setSourceScope(ALL_SOURCES_SCOPE);
    }
  }

  function setReadingDepthFilter(nextFilter: ReadingDepthFilter) {
    setViewState('ai');
    setReadingDepthFilterState(nextFilter);
    setSelectedId(null);
  }

  function setSortMode(nextMode: SortMode) {
    if (view === 'ai') {
      if (nextMode === 'priority' && !supportsPriorityMode) {
        setAiSortMode('latest');
        return;
      }

      setAiSortMode(nextMode);
      return;
    }

    if (nextMode === 'priority') {
      return;
    }

    setReaderSortMode(nextMode);
  }

  function setUnreadOnly(nextValue: boolean) {
    if (view === 'ai') {
      setAiUnreadOnly(nextValue);
      return;
    }

    if (view === 'reader') {
      setReaderUnreadOnly(nextValue);
    }
  }

  function setTimeWindow(nextWindow: TimeWindow) {
    if (view === 'ai') {
      setAiTimeWindow(nextWindow);
      return;
    }

    if (view === 'reader') {
      setReaderTimeWindow(nextWindow);
    }
  }

  function selectTopicFacet(topic: string) {
    if (!supportsTopicMode) {
      return;
    }

    setViewState('ai');
    setGroupModeState('topic');
    setActiveFacet(topic);
    setSourceScope(ALL_SOURCES_SCOPE);
  }

  function selectChannelFacet(channelId: string) {
    setViewState('ai');
    setGroupModeState('channel');
    setActiveFacet(channelId);
    setSourceScope(ALL_SOURCES_SCOPE);
    setSelectedId(null);
  }

  function selectFlowLaneFacet(lane: string) {
    setViewState('ai');
    setGroupModeState('flow_lane');
    setActiveFacet(lane);
    setSourceScope(ALL_SOURCES_SCOPE);
    setSelectedId(null);
  }

  function selectAllSources() {
    if (view === 'kb') {
      setKnowledgeScope('all');
      setKnowledgeSourceScope(ALL_SOURCES_SCOPE);
      return;
    }

    if (view !== 'reader') {
      setViewState('ai');
    }
    setGroupModeState('source');
    setActiveFacet(null);
    setSourceScope(ALL_SOURCES_SCOPE);
  }

  function selectSourceCategory(categoryId: string, label = sourceCategoryLabelFromId(categoryId)) {
    if (view === 'kb') {
      setKnowledgeScope('feed_saved');
      setKnowledgeSourceScope({kind: 'source_category', value: categoryId, label});
      return;
    }

    if (view !== 'reader') {
      setViewState('ai');
    }
    setGroupModeState('source');
    setActiveFacet(null);
    setSelectedId(null);
    setReadingDepthFilterState('all');
    setSourceScope({kind: 'source_category', value: categoryId, label});
  }

  function selectSource(source: SourceDto) {
    if (view === 'kb') {
      setKnowledgeScope('feed_saved');
      setKnowledgeSourceScope({kind: 'source', value: source.id, label: source.name});
      return;
    }

    if (view !== 'reader') {
      setViewState('ai');
    }
    setGroupModeState('source');
    setActiveFacet(null);
    setSelectedId(null);
    setReadingDepthFilterState('all');
    setSourceScope({kind: 'source', value: source.id, label: source.name});
  }

  function buildBehaviorViewContext(item?: FeedItem | null): BehaviorViewContextDto {
    const surface = behaviorSurfaceForView(view);
    const searchQueryValue = deferredSearchQuery || null;

    if (view === 'ai') {
      if (groupMode === 'channel' && activeFacet) {
        return withBehaviorSourceScope(
          withBehaviorReadingDepth(
            withBehaviorSearchQuery({surface, scope_type: 'channel', channel_id: activeFacet}, searchQueryValue),
            readingDepthFilter,
          ),
          sourceScope,
        );
      }
      if (groupMode === 'flow_lane' && activeFacet) {
        return withBehaviorSourceScope(
          withBehaviorReadingDepth(
            withBehaviorSearchQuery({surface, scope_type: 'flow_lane', flow_lane: activeFacet}, searchQueryValue),
            readingDepthFilter,
          ),
          sourceScope,
        );
      }
      if (sourceScope.kind === 'source') {
        return withBehaviorReadingDepth(
          withBehaviorSearchQuery(
            {surface, scope_type: 'source', source_id: Number(sourceScope.value)},
            searchQueryValue,
          ),
          readingDepthFilter,
        );
      }
      if (sourceScope.kind === 'source_category') {
        return withBehaviorReadingDepth(
          withBehaviorSearchQuery(
            {surface, scope_type: 'source_category', source_category: String(sourceScope.value ?? '')},
            searchQueryValue,
          ),
          readingDepthFilter,
        );
      }
      if (readingDepthFilter !== 'all') {
        return withBehaviorSearchQuery(
          {surface, scope_type: 'reading_depth', reading_depth: readingDepthFilter},
          searchQueryValue,
        );
      }
      if (searchQueryValue) {
        return {surface, scope_type: 'search', search_query: searchQueryValue};
      }
      return {surface, scope_type: 'global'};
    }

    if (view === 'reader') {
      if (sourceScope.kind === 'source') {
        return withBehaviorSearchQuery(
          {surface, scope_type: 'source', source_id: Number(sourceScope.value)},
          searchQueryValue,
        );
      }
      if (sourceScope.kind === 'source_category') {
        return withBehaviorSearchQuery(
          {surface, scope_type: 'source_category', source_category: String(sourceScope.value ?? '')},
          searchQueryValue,
        );
      }
      if (searchQueryValue) {
        return {surface, scope_type: 'search', search_query: searchQueryValue};
      }
      return {surface, scope_type: 'reader'};
    }

    if (view === 'kb') {
      const feedSourceId = item?.sourceId ?? null;
      if (knowledgeSourceScope.kind === 'source') {
        return withBehaviorSearchQuery(
          {surface, scope_type: 'source', source_id: Number(knowledgeSourceScope.value)},
          searchQueryValue,
        );
      }
      if (knowledgeSourceScope.kind === 'source_category') {
        return withBehaviorSearchQuery(
          {surface, scope_type: 'source_category', source_category: String(knowledgeSourceScope.value ?? '')},
          searchQueryValue,
        );
      }
      if (feedSourceId !== null) {
        return withBehaviorSearchQuery({surface, scope_type: 'source', source_id: feedSourceId}, searchQueryValue);
      }
      if (searchQueryValue) {
        return {surface, scope_type: 'search', search_query: searchQueryValue};
      }
      return {surface, scope_type: 'global'};
    }

    return {surface, scope_type: "unknown"};
  }

  function buildSourceBulkBehaviorViewContext(source: SourceDto): BehaviorViewContextDto {
    return withBehaviorSearchQuery(
      {
        surface: behaviorSurfaceForView(view),
        scope_type: 'source',
        source_id: source.id,
      },
      deferredSearchQuery || null,
    );
  }

  function openItem(itemId: number) {
    setShouldLoadSelectedDetail(true);
    setSelectedId(itemId);
    const item =
      view === 'kb'
        ? savedItems.find((entry) => entry.id === itemId) ?? savedDetailItems[itemId]
        : feedItemLookup.get(itemId);
    if (view !== 'kb' && item) {
      staleFeedItemRef.current = item;
      setFeedDetailErrorMessages((current) => {
        if (!(itemId in current)) {
          return current;
        }
        const next = {...current};
        delete next[itemId];
        return next;
      });
      setUnavailableFeedItemIds((current) => {
        if (!current.has(itemId)) {
          return current;
        }
        const next = new Set(current);
        next.delete(itemId);
        return next;
      });
    }
    if (item?.status === 'unread') {
      void updateItemStatus(item, 'browsed');
    }
  }

  function applyReaderStateMutation(response: ReaderStateMutationResponseDto) {
    const feedItemId = response.item_id;
    const savedItemId = response.saved_item_id;
    const applyMutation = (currentItem: FeedItem) =>
      applyStateToFeedItem(currentItem, response.state, {
        savedItemId: savedItemId !== undefined ? savedItemId : currentItem.savedItemId,
      });

    setHomepageItems((current) => updateCollection(current, feedItemId, applyMutation));
    setReaderItems((current) => updateCollection(current, feedItemId, applyMutation));
    setSavedItems((current) => updateCollectionByFeedItemId(current, feedItemId, applyMutation));
    setFeedDetailItems((current) => {
      const detailItem = current[feedItemId] ?? feedItemLookup.get(feedItemId);
      if (!detailItem) {
        return current;
      }
      return {
        ...current,
        [feedItemId]: applyMutation(detailItem),
      };
    });
    setSavedDetailItems((current) =>
      Object.fromEntries(
        Object.entries(current).map(([key, currentItem]) => [
          key,
          currentItem.feedItemId === feedItemId ? applyMutation(currentItem) : currentItem,
        ]),
      ),
    );
    invalidateHomepageSnapshotWindow();
    if (viewRef.current === 'ai') {
      if (hasHomepageMembershipMutationFilter(homepageQuery)) {
        void reloadHomepageItems().catch((error) =>
          console.error('Failed to reload homepage after membership-affecting state change', error),
        );
      } else {
        void refreshHomepageSummary().catch((error) =>
          console.error('Failed to refresh homepage summary after state change', error),
        );
      }
    }
  }

  function removeSavedItemFromKnowledgeState(savedItemId: number, feedItemId: number | null) {
    setSavedItems((current) =>
      current.filter(
        (currentItem) => currentItem.id !== savedItemId && (feedItemId === null || currentItem.feedItemId !== feedItemId),
      ),
    );
    setSavedDetailItems((current) => {
      const next = {...current};
      delete next[savedItemId];
      return next;
    });
    if (view === 'kb' && selectedId === savedItemId) {
      setSelectedId(null);
    }
  }

  async function updateItemStatus(item: FeedItem, status: ItemStatus) {
    if (status === 'unread') {
      return;
    }

    const feedItemId = item.feedItemId ?? (item.savedItemId === null ? item.id : null);
    const behaviorContext = buildBehaviorViewContext(item);

    try {
      if (status === 'saved' && item.savedAt) {
        const confirmed = window.confirm('取消保存后，这条内容会从知识库列表移除，但归档和本地镜像会保留到 .archive。');
        if (!confirmed) {
          return;
        }

        if (feedItemId !== null) {
          const response = await unmarkFeedItemSaved(feedItemId, behaviorContext);
          applyReaderStateMutation(response);
          removeSavedItemFromKnowledgeState(response.saved_item_id ?? item.savedItemId ?? item.id, feedItemId);
          return;
        }

        const savedItemId = item.savedItemId ?? item.id;
        const response = await postRemoveSavedItem(savedItemId);
        removeSavedItemFromKnowledgeState(response.saved_item_id, response.feed_item_id);
        return;
      }

      if (feedItemId === null) {
        return;
      }

      if (status === 'seen' && item.seenAt) {
        applyReaderStateMutation(await unmarkFeedItemSeen(feedItemId, behaviorContext));
        return;
      }

      if (status === 'read_full' && item.readFullAt) {
        applyReaderStateMutation(await unmarkFeedItemReadFull(feedItemId, behaviorContext));
        return;
      }

      if (status === 'not_interested' && item.status === 'not_interested') {
        applyReaderStateMutation(await unmarkFeedItemNotInterested(feedItemId, behaviorContext));
        const nextHomepageItems = await reloadHomepageItems();
        const updatedItem = nextHomepageItems.find((currentItem) => currentItem.id === feedItemId);
        if (updatedItem) {
          setFeedDetailItems((current) => ({
            ...current,
            [feedItemId]: mergeFeedItem(current[feedItemId] ?? feedItemLookup.get(feedItemId), updatedItem),
          }));
        }
        return;
      }

      if (status === 'saved') {
        const startedAt = new Date().toISOString();
        const markPending = (currentItem: FeedItem) => markSaveRequestPending(currentItem, startedAt);
        setHomepageItems((current) => updateCollection(current, feedItemId, markPending));
        setReaderItems((current) => updateCollection(current, feedItemId, markPending));
        setFeedDetailItems((current) => {
          const detailItem = current[feedItemId] ?? feedItemLookup.get(feedItemId);
          if (!detailItem) {
            return current;
          }
          return {
            ...current,
            [feedItemId]: markPending(detailItem),
          };
        });
      }

      const response = await markFeedItemStatus(feedItemId, status, behaviorContext);
      applyReaderStateMutation(response);

      if (status === 'not_interested') {
        await reloadHomepageItems();
      }

      if (status === 'saved') {
        const nextSavedItems = await reloadSavedItems({query: view === 'kb' ? deferredSearchQuery : undefined});
        const savedItem = response.saved_item_id
          ? nextSavedItems.find((currentItem) => currentItem.id === response.saved_item_id)
          : null;
        if (savedItem) {
          setSavedDetailItems((current) => ({
            ...current,
            [savedItem.id]: mergeFeedItem(current[savedItem.id], savedItem),
          }));
        }
      }
    } catch (error) {
      console.error('Failed to update feed item status', error);
      if (status === 'saved' && feedItemId !== null && !item.savedAt) {
        const message = error instanceof Error ? error.message : '保存失败，请稍后重试。';
        const rollback = (currentItem: FeedItem) => rollbackSaveRequest(currentItem, item, message);
        setHomepageItems((current) => updateCollection(current, feedItemId, rollback));
        setReaderItems((current) => updateCollection(current, feedItemId, rollback));
        setFeedDetailItems((current) => {
          const detailItem = current[feedItemId] ?? feedItemLookup.get(feedItemId);
          if (!detailItem) {
            return current;
          }
          return {
            ...current,
            [feedItemId]: rollback(detailItem),
          };
        });
      }
    }
  }

  async function extractSelectedFeedItemContent(item: FeedItem) {
    const feedItemId = item.feedItemId ?? item.id;
    if (feedItemId === null) {
      return;
    }

    const startedAt = new Date().toISOString();
    const markPending = (currentItem: FeedItem): FeedItem => ({
      ...currentItem,
      contentExtractionStatus: 'needs_extraction',
      contentExtractionAttemptedAt: startedAt,
      contentExtractionError: null,
      contentExtractionMethod: null,
      contentExtractionFinalUrl: null,
      contentExtractionVisibleCharCount: null,
    });
    setHomepageItems((current) => updateCollection(current, feedItemId, markPending));
    setReaderItems((current) => updateCollection(current, feedItemId, markPending));
    setFeedDetailItems((current) => {
      const detailItem = current[feedItemId] ?? feedItemLookup.get(feedItemId);
      if (!detailItem) {
        return current;
      }
      return {
        ...current,
        [feedItemId]: markPending(detailItem),
      };
    });

    const operation = await startFeedItemExtractOperation(feedItemId);
    trackItemOperation(operation);
  }

  async function updateFeedItemInsightFeedback(item: FeedItem, input: FeedItemFeedbackInput) {
    const feedItemId = item.feedItemId ?? item.id;
    if (feedItemId === null) {
      return;
    }

    const updatedDetail = await saveFeedItemFeedback(feedItemId, input, buildBehaviorViewContext(item));
    const applyUpdatedDetail = (currentItem: FeedItem): FeedItem => mergeFeedItem(currentItem, updatedDetail);

    setReaderItems((current) => updateCollection(current, feedItemId, applyUpdatedDetail));
    setFeedDetailItems((current) => ({
      ...current,
      [feedItemId]: applyUpdatedDetail(current[feedItemId] ?? item),
    }));

    const nextHomepageItems = await reloadHomepageItems();
    const updatedHomepageItem = nextHomepageItems.find((currentItem) => currentItem.id === feedItemId);
    if (updatedHomepageItem) {
      setFeedDetailItems((current) => ({
        ...current,
        [feedItemId]: mergeFeedItem(current[feedItemId] ?? updatedDetail, updatedHomepageItem),
      }));
    }
  }

  async function updateFeedItemRoutingCorrection(item: FeedItem, correction: FeedItemRoutingCorrectionInput) {
    const feedItemId = item.feedItemId ?? item.id;
    if (feedItemId === null) {
      return;
    }

    const structuredFeedback = mergeStructuredFeedbackWithRoutingCorrection(item.structuredFeedback, correction);
    const updatedDetail = await saveFeedItemFeedback(
      feedItemId,
      {
        insightFeedbackText: item.insightFeedbackText ?? null,
        structuredFeedback,
        channelFeedback: null,
      },
      buildBehaviorViewContext(item),
    );
    const applyUpdatedDetail = (currentItem: FeedItem): FeedItem => mergeFeedItem(currentItem, updatedDetail);

    setReaderItems((current) => updateCollection(current, feedItemId, applyUpdatedDetail));
    setFeedDetailItems((current) => ({
      ...current,
      [feedItemId]: applyUpdatedDetail(current[feedItemId] ?? item),
    }));

    const nextHomepageItems = await reloadHomepageItems();
    const updatedHomepageItem = nextHomepageItems.find((currentItem) => currentItem.id === feedItemId);
    if (updatedHomepageItem) {
      setFeedDetailItems((current) => ({
        ...current,
        [feedItemId]: mergeFeedItem(current[feedItemId] ?? updatedDetail, updatedHomepageItem),
      }));
    }
  }

  async function clearFeedItemChannelCorrectionAction(item: FeedItem) {
    const feedItemId = item.feedItemId ?? item.id;
    if (feedItemId === null) {
      return;
    }

    const updatedDetail = await clearFeedItemChannelCorrection(
      feedItemId,
      item.structuredFeedback ?? null,
      buildBehaviorViewContext(item),
    );
    const applyUpdatedDetail = (currentItem: FeedItem): FeedItem => mergeFeedItem(currentItem, updatedDetail);

    setReaderItems((current) => updateCollection(current, feedItemId, applyUpdatedDetail));
    setFeedDetailItems((current) => ({
      ...current,
      [feedItemId]: applyUpdatedDetail(current[feedItemId] ?? item),
    }));

    const nextHomepageItems = await reloadHomepageItems();
    const updatedHomepageItem = nextHomepageItems.find((currentItem) => currentItem.id === feedItemId);
    if (updatedHomepageItem) {
      setFeedDetailItems((current) => ({
        ...current,
        [feedItemId]: mergeFeedItem(current[feedItemId] ?? updatedDetail, updatedHomepageItem),
      }));
    }
  }

  async function clearAutoLowPriority(item: FeedItem) {
    const feedItemId = item.feedItemId ?? item.id;
    if (feedItemId === null) {
      return;
    }

    const response = await clearFeedItemAutoLowPriority(feedItemId, buildBehaviorViewContext(item));
    const nextStructuredFeedback = adaptStructuredFeedbackResponse(response.structured_feedback);
    const applyFeedback = (currentItem: FeedItem): FeedItem => ({
      ...currentItem,
      insightFeedbackText: response.insight_feedback_text,
      structuredFeedback: nextStructuredFeedback,
      isLowPriorityBucket: false,
      lowPriorityBucketReason: null,
      autoLowPriorityCleared: true,
    });

    setHomepageItems((current) => updateCollection(current, feedItemId, applyFeedback));
    setReaderItems((current) => updateCollection(current, feedItemId, applyFeedback));
    setFeedDetailItems((current) => {
      const detailItem = current[feedItemId] ?? feedItemLookup.get(feedItemId);
      if (!detailItem) {
        return current;
      }
      return {
        ...current,
        [feedItemId]: applyFeedback(detailItem),
      };
    });

    const nextHomepageItems = await reloadHomepageItems();
    const updatedItem = nextHomepageItems.find((currentItem) => currentItem.id === feedItemId);
    if (updatedItem) {
      setFeedDetailItems((current) => ({
        ...current,
        [feedItemId]: mergeFeedItem(current[feedItemId] ?? item, updatedItem),
      }));
    }
  }

  async function updateSavedItemNote(item: FeedItem, userNote: string | null) {
    const savedItemId = item.savedItemId ?? item.id;
    const updatedItem = await patchSavedItemNote(savedItemId, userNote);

    setSavedDetailItems((current) => ({
      ...current,
      [updatedItem.id]: mergeFeedItem(current[updatedItem.id], updatedItem),
    }));
    setSavedItems((current) =>
      updateCollection(current, updatedItem.id, (currentItem) => mergeFeedItem(currentItem, updatedItem)),
    );

    await reloadSavedItems({query: deferredSearchQuery});
  }

  async function rebuildSavedItemInsight(item: FeedItem, aiRoute?: SavedItemAIRoute | null) {
    const savedItemId = item.savedItemId ?? item.id;
    const operation = await startSavedItemInsightOperation(savedItemId, aiRoute);
    trackItemOperation(operation);
  }

  async function updateSavedItemCuratedInsight(item: FeedItem, input: SavedItemCuratedInsightInput) {
    const savedItemId = item.savedItemId ?? item.id;
    const updatedItem = await putSavedItemCuratedInsight(savedItemId, input);

    setSavedDetailItems((current) => ({
      ...current,
      [updatedItem.id]: mergeFeedItem(current[updatedItem.id], updatedItem),
    }));
    setSavedItems((current) =>
      updateCollection(current, updatedItem.id, (currentItem) => mergeFeedItem(currentItem, updatedItem)),
    );

    await reloadSavedItems({query: deferredSearchQuery});
    if (shouldPreserveSavedSelection(savedItemId)) {
      setSelectedId(updatedItem.id);
    }
  }

  async function clearSavedItemCuratedInsight(item: FeedItem) {
    const savedItemId = item.savedItemId ?? item.id;
    const updatedItem = await deleteSavedItemCuratedInsight(savedItemId);

    setSavedDetailItems((current) => ({
      ...current,
      [updatedItem.id]: mergeFeedItem(current[updatedItem.id], updatedItem),
    }));
    setSavedItems((current) =>
      updateCollection(current, updatedItem.id, (currentItem) => mergeFeedItem(currentItem, updatedItem)),
    );

    await reloadSavedItems({query: deferredSearchQuery});
    if (shouldPreserveSavedSelection(savedItemId)) {
      setSelectedId(updatedItem.id);
    }
  }

  async function createManualUrlCapture(input: ManualUrlCaptureInput) {
    const captureViewRevision = viewRevisionRef.current;
    const operation = await startManualUrlCaptureOperation(input);
    trackItemOperation(operation);
    const createdItemId = getOperationSavedItemId(operation);
    const nextSavedItems = await reloadSavedItems();
    if (createdItemId !== null && viewRef.current === 'kb' && viewRevisionRef.current === captureViewRevision) {
      setSearchQuery('');
      setKnowledgeScope('all');
      setKnowledgeSourceScope(ALL_SOURCES_SCOPE);
      setSelectedId(createdItemId);
      if (!nextSavedItems.some((item) => item.id === createdItemId)) {
        setShouldLoadSelectedDetail(true);
      }
    }
  }

  async function createUploadedMaterialCapture(input: UploadedMaterialCaptureInput) {
    const captureViewRevision = viewRevisionRef.current;
    const operation = await startUploadedMaterialCaptureOperation(input);
    trackItemOperation(operation);
    const createdItemId = getOperationSavedItemId(operation);
    const nextSavedItems = await reloadSavedItems();
    if (createdItemId !== null && viewRef.current === 'kb' && viewRevisionRef.current === captureViewRevision) {
      setSearchQuery('');
      setKnowledgeScope('all');
      setKnowledgeSourceScope(ALL_SOURCES_SCOPE);
      setSelectedId(createdItemId);
      if (!nextSavedItems.some((item) => item.id === createdItemId)) {
        setShouldLoadSelectedDetail(true);
      }
    }
  }

  async function importSavedItemManualFile(item: FeedItem, input: SavedItemManualImportInput) {
    const savedItemId = item.savedItemId ?? item.id;
    const operation = await startSavedItemManualImportOperation(savedItemId, input);
    trackItemOperation(operation);
    await reloadSavedItems({query: deferredSearchQuery});
    if (viewRef.current === 'kb') {
      setSelectedId(savedItemId);
      setShouldLoadSelectedDetail(true);
    }
  }

  async function retrySavedItemCapture(item: FeedItem) {
    const savedItemId = item.savedItemId ?? item.id;
    const startedAt = new Date().toISOString();
    setSavedItems((current) => updateCollection(current, savedItemId, (currentItem) => markCapturePending(currentItem, startedAt)));
    setSavedDetailItems((current) => {
      const currentItem = current[savedItemId];
      if (!currentItem) {
        return current;
      }
      return {
        ...current,
        [savedItemId]: markCapturePending(currentItem, startedAt),
      };
    });

    try {
      const operation = await startRetryCaptureOperation(savedItemId);
      trackItemOperation(operation);
    } catch (error) {
      const message = error instanceof Error ? error.message : '捕获请求失败。';
      setSavedItems((current) =>
        updateCollection(current, savedItemId, (currentItem) => markCaptureRequestFailed(currentItem, message)),
      );
      setSavedDetailItems((current) => {
        const currentItem = current[savedItemId];
        if (!currentItem) {
          return current;
        }
        return {
          ...current,
          [savedItemId]: markCaptureRequestFailed(currentItem, message),
        };
      });
      throw error;
    }

    await reloadSavedItems({query: deferredSearchQuery});
  }

  async function reexportSavedItemLocalMirror(item: FeedItem) {
    const savedItemId = item.savedItemId ?? item.id;
    const operation = await startLocalMirrorExportOperation(savedItemId);
    trackItemOperation(operation);
  }

  async function buildSelectedFeedItemMissingInsightEngineArtifacts() {
    if (view !== 'ai' || !selectedItem) {
      setInsightEngineArtifactMessage('请先选择一张 AI 首页卡片。');
      return;
    }

    const feedItemId = selectedItem.feedItemId ?? selectedItem.id;
    if (feedItemId === null) {
      setInsightEngineArtifactMessage('当前卡片没有可补齐的未成功洞察。');
      return;
    }

    setInsightEngineArtifactMessage(null);

    try {
      await runFeedItemInsightArtifactOperation(feedItemId, 'build_missing');
      setInsightEngineArtifactMessage('当前卡片未成功洞察补齐任务已交给后端执行，完成后会刷新对应卡片。');
    } catch (error) {
      console.error('Failed to build selected Insight Engine artifacts', error);
      if (error instanceof Error) {
        setInsightEngineArtifactMessage(`当前卡片未成功洞察补齐任务启动失败：${error.message}`);
        return;
      }
      setInsightEngineArtifactMessage('当前卡片未成功洞察补齐任务启动失败。');
    }
  }

  async function rebuildSelectedFeedItemInsightEngineArtifacts(item: FeedItem) {
    const feedItemId = item.feedItemId ?? item.id;
    if (feedItemId === null) {
      return;
    }

    await runFeedItemInsightArtifactOperation(feedItemId, 'rebuild');
  }

  async function setSourceActive(source: SourceDto, isActive: boolean) {
    try {
      await updateSource(source.id, buildSourceUpdatePayload(source, {is_active: isActive}));
      await reloadSources();
      setSourceSyncMessage(`${source.name} 已${isActive ? '启用' : '停用'}。`);
    } catch (error) {
      console.error('Failed to update source active state', error);
      setSourceSyncMessage(error instanceof Error ? `更新来源失败：${error.message}` : '更新来源失败。');
    }
  }

  async function moveSourceToCategory(source: SourceDto, category: string | null) {
    try {
      await updateSource(source.id, buildSourceUpdatePayload(source, {folder_path: category}));
      await reloadSources();
      await reloadCoreLists({savedQuery: view === 'kb' ? deferredSearchQuery : undefined});
      setSourceSyncMessage(`${source.name} 已移动到 ${category ?? '未分类'}。`);
    } catch (error) {
      console.error('Failed to move source category', error);
      setSourceSyncMessage(error instanceof Error ? `移动分类失败：${error.message}` : '移动分类失败。');
    }
  }

  async function markSourceItemsReadFull(source: SourceDto) {
    try {
      const result = await bulkMarkSourceSeen(source.id, buildSourceBulkBehaviorViewContext(source));
      await reloadCoreLists({savedQuery: view === 'kb' ? deferredSearchQuery : undefined});
      setSourceSyncMessage(
        `已处理 ${source.name}：匹配 ${result.matched_item_count} 条，更新 ${result.updated_item_count} 条。`,
      );
    } catch (error) {
      console.error('Failed to bulk mark source items read', error);
      setSourceSyncMessage(error instanceof Error ? `批量标记失败：${error.message}` : '批量标记失败。');
    }
  }

  async function markOriginalOpened(item: FeedItem) {
    const feedItemId = item.feedItemId;
    if (feedItemId === null) {
      return;
    }

    try {
      const response = await openFeedItemOriginal(feedItemId, buildBehaviorViewContext(item));
      applyReaderStateMutation(response);
      setReaderActionMessage(buildOriginalOpenedMessage(item));
    } catch (error) {
      console.error('Failed to record original link open', error);
      setReaderActionMessage(
        error instanceof Error
          ? `已打开原文，但状态更新失败：${error.message}`
          : '已打开原文，但状态更新失败。',
      );
    }
  }

  function openOriginal(item: FeedItem) {
    if (!item.originalUrl) {
      return;
    }
    window.open(item.originalUrl, '_blank', 'noopener,noreferrer');
    if (item.feedItemId !== null) {
      setReaderActionMessage(null);
      void markOriginalOpened(item);
    }
  }

  function openSourceApp(item: FeedItem) {
    if (!item.sourceAppUrl) {
      return;
    }
    window.open(item.sourceAppUrl, '_blank', 'noopener,noreferrer');
  }

  const searchPlaceholder =
    view === 'settings'
      ? '搜索 source 名称、URL、分类...'
      : view === 'kb'
        ? '搜索已保存内容...'
        : view === 'reader'
          ? '搜索 classic reader...'
          : '搜索 AI 首页卡片...';

  const selectedChannelLabel = activeFacet ? (insightChannelLabels.get(activeFacet) ?? activeFacet) : 'Insight 频道';
  const selectedFlowLaneLabel = activeFacet
    ? (flowLaneFacets.find((facet) => facet.lane === activeFacet)?.label ?? activeFacet)
    : '阅读流';
  const channelContextLabel =
    sourceScope.kind === 'all_sources'
      ? selectedChannelLabel
      : activeFacet
        ? `频道 · ${selectedChannelLabel} · ${sourceScope.label}`
        : `频道 · ${sourceScope.label}`;
  const flowLaneContextLabel =
    sourceScope.kind === 'all_sources'
      ? selectedFlowLaneLabel
      : activeFacet
        ? `阅读流 · ${selectedFlowLaneLabel} · ${sourceScope.label}`
        : `阅读流 · ${sourceScope.label}`;

  const contextLabel =
    view === 'reader'
      ? sourceScope.kind === 'source'
        ? `阅读器 · ${sourceScope.label}`
        : sourceScope.kind === 'source_category'
          ? `阅读器 · ${sourceScope.label}`
          : '经典阅读列表'
      : view === 'kb'
        ? '保存与回看'
        : groupMode === 'time'
          ? '时间线'
          : groupMode === 'topic'
            ? activeFacet
              ? activeFacet
              : '主题分类'
            : groupMode === 'source'
              ? sourceScope.label
              : groupMode === 'channel'
                ? channelContextLabel
                : groupMode === 'flow_lane'
                  ? flowLaneContextLabel
                  : '低优先级集中处理';

  const currentListLoadState =
    view === 'ai' ? homepageLoadState : view === 'reader' ? readerLoadState : view === 'kb' ? savedLoadState : null;
  const isLoading =
    (currentListLoadState?.status === 'idle' || currentListLoadState?.status === 'loading') && visibleItems.length === 0;
  const listErrorMessage = currentListLoadState?.errorMessage ?? null;
  const visibleItemTotalCount = view === 'ai' ? (homepageSummary?.totalCount ?? visibleItems.length) : visibleItems.length;

  function retryCurrentList() {
    if (view === 'ai') {
      void reloadHomepageItems().catch(() => {
        // The visible list renders the error state.
      });
      return;
    }

    if (view === 'reader') {
      void reloadReaderItems().catch(() => {
        // The visible list renders the error state.
      });
      return;
    }

    if (view === 'kb') {
      void reloadSavedItems({query: deferredSearchQuery}).catch(() => {
        // The visible list renders the error state.
      });
    }
  }

  return {
    activeFacet,
    contextLabel,
    counts,
    channelFacets,
    groupMode,
    flowLaneFacets,
    insightEngineArtifactMessage,
    homepageReadModelStatus,
    insightEngineArtifactOperation,
    insightEngineArtifactProgress,
    insightEngineArtifactBusyReason,
    insightEngineArtifactStats,
    feedItemArtifactRefreshVersion,
    isLoading,
    isLoadingMoreHomepageItems: homepageWindowState.isLoadingMore,
    listErrorMessage,
    loadMoreErrorMessage: homepageWindowState.loadMoreErrorMessage,
    hasMoreHomepageItems: view === 'ai' && homepageWindowState.hasMore,
    knowledgeCounts,
    knowledgeScope,
    insightChannelLabels,
    insightChannelLoadError,
    insightChannels,
    isPreviewingSource,
    isRebuildingInsightEngineArtifacts,
    isInsightEngineArtifactStopRequesting,
    isSavingSource,
    isSourceSyncStopRequesting,
    isSyncingSources,
    sortMode: view === 'ai' ? aiSortMode : readerSortMode,
    unreadOnly: view === 'ai' ? aiUnreadOnly : readerUnreadOnly,
    openItem,
    openOriginal,
    openSourceApp,
    readerActionMessage,
    readingDepthFilter,
    searchPlaceholder,
    searchQuery,
    selectedId,
    selectedCaptureOperation,
    selectedDetailOperations,
    selectedItem,
    selectedItemDetailErrorMessage,
    selectedItemStale,
    selectedItemUnavailable,
    sourceCategorySuggestions,
    sourceEditor,
    sourceItemCounts,
    sourceScope: sidebarSourceScope,
    sourceSyncBusyReason,
    sourceSyncMessage,
    sourceSyncOperation,
    sourceSyncProgress,
    sources,
    createManualUrlCapture,
    createUploadedMaterialCapture,
    importSavedItemManualFile,
    closeSourceEditor,
    canRebuildCurrentSourceArtifacts: view === 'ai' && sourceScope.kind === 'source',
    markSourceItemsReadFull,
    moveSourceToCategory,
    openSourceEditor,
    previewQuickSource,
    reloadSources,
    reloadInsightChannels,
    trackBackendOperation,
    reexportSavedItemLocalMirror,
    retryCurrentList,
    loadMoreHomepageItems,
    retrySavedItemCapture,
    buildSelectedFeedItemMissingInsightEngineArtifacts,
    rebuildInsightEngineArtifactsNow,
    rebuildSavedItemInsight,
    rebuildSelectedFeedItemInsightEngineArtifacts,
    runFeedItemInsightArtifactOperation,
    saveSourceEditor,
    deleteSourceEditorProfileDraft,
    saveSourceEditorProfileDraft,
    extractSelectedFeedItemContent,
    setSourceActive,
    setGroupMode,
    setReadingDepthFilter,
    setSearchQuery,
    setSortMode,
    setTimeWindow,
    setUnreadOnly,
    setView,
    selectKnowledgeScope,
    selectAllSources,
    selectChannelFacet,
    selectFlowLaneFacet,
    selectSource,
    selectSourceCategory,
    selectTopicFacet,
    syncSourceIdsNow,
    syncSingleSourceNow,
    syncSourcesNow,
    stopInsightEngineArtifactsNow,
    stopSourcesSyncNow,
    supportsPriorityMode,
    supportsTopicMode,
    theme,
    timeWindow: view === 'ai' ? aiTimeWindow : readerTimeWindow,
    toggleTheme,
    topicFacets,
    clearFeedItemChannelCorrection: clearFeedItemChannelCorrectionAction,
    clearAutoLowPriority,
    updateFeedItemInsightFeedback,
    updateFeedItemRoutingCorrection,
    updateItemStatus,
    updateSavedItemCuratedInsight,
    clearSavedItemCuratedInsight,
    updateSavedItemNote,
    view,
    visibleItems,
    visibleItemTotalCount,
  };
}

function selectLatestSavedItemInsightOperation(
  operations: Record<number, AppOperationDto>,
  savedItemId: number | null,
): AppOperationDto | null {
  if (savedItemId === null) {
    return null;
  }

  let latest: AppOperationDto | null = null;
  for (const operation of Object.values(operations)) {
    if (operation.operation_type !== 'saved_item_insight' || getOperationSavedItemId(operation) !== savedItemId) {
      continue;
    }
    if (!latest || operation.id > latest.id) {
      latest = operation;
    }
  }
  return latest;
}
