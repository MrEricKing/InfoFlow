import {useMemo, useRef, useState} from 'react';
import {
  getHomepageBootstrap,
  getHomepageSummary,
  HOMEPAGE_DEFAULT_PAGE_SIZE,
  listHomepageItems,
  type HomepageQueryOptions,
} from '@/services/homepage-service';
import type {
  FeedItem,
  GroupMode,
  HomepageFlowLane,
  HomepageSummary,
  ReadingDepthFilter,
  SortMode,
  SourceScope,
  TimeWindow,
  ViewMode,
} from '@/types/feed';
import {
  emptyHomepageWindowState,
  formatListError,
  homepageQueryKey,
  isRecoverableHomepageCursorError,
  loadingListLoadState,
  mergeUniqueFeedItems,
  normalizeHomepageSourceCategory,
  withListRetry,
  type HomepageWindowState,
  type ListLoadState,
} from '@/hooks/info-flow/helpers';

interface UseHomepageReadModelArgs {
  activeFacet: string | null;
  aiSortMode: SortMode;
  aiTimeWindow: TimeWindow;
  aiUnreadOnly: boolean;
  deferredSearchQuery: string;
  groupMode: GroupMode;
  readingDepthFilter: ReadingDepthFilter;
  sourceScope: SourceScope;
  view: ViewMode;
}

export function useHomepageReadModel({
  activeFacet,
  aiSortMode,
  aiTimeWindow,
  aiUnreadOnly,
  deferredSearchQuery,
  groupMode,
  readingDepthFilter,
  sourceScope,
  view,
}: UseHomepageReadModelArgs) {
  const [homepageItems, setHomepageItems] = useState<FeedItem[]>([]);
  const [homepageSummary, setHomepageSummary] = useState<HomepageSummary | null>(null);
  const [homepageWindowState, setHomepageWindowState] = useState<HomepageWindowState>(emptyHomepageWindowState);
  const [homepageLoadState, setHomepageLoadState] = useState<ListLoadState>(loadingListLoadState);
  const homepageRequestIdRef = useRef(0);
  const loadedHomepageItemIdsRef = useRef<Set<number>>(new Set());

  const homepageQuery = useMemo<HomepageQueryOptions>(() => {
    const query: HomepageQueryOptions = {
      pageSize: HOMEPAGE_DEFAULT_PAGE_SIZE,
      sort: aiSortMode === 'priority' ? 'priority' : 'newest',
      searchText: deferredSearchQuery || null,
      timeWindow: aiTimeWindow,
      state: aiUnreadOnly ? 'unread' : null,
      readingDepth: readingDepthFilter === 'all' ? null : readingDepthFilter,
    };

    if (sourceScope.kind === 'source' && typeof sourceScope.value === 'number') {
      query.sourceId = sourceScope.value;
    } else if (sourceScope.kind === 'source_category') {
      query.sourceCategory = normalizeHomepageSourceCategory(sourceScope.value);
    }

    if (groupMode === 'channel' && activeFacet) {
      query.channelId = activeFacet;
    } else if (groupMode === 'flow_lane' && activeFacet) {
      query.flowLane = activeFacet as HomepageFlowLane;
    } else if (groupMode === 'low_priority') {
      query.flowLane = 'low_priority';
    } else if (groupMode === 'topic' && activeFacet) {
      query.topic = activeFacet;
    }

    return query;
  }, [activeFacet, aiSortMode, aiTimeWindow, aiUnreadOnly, deferredSearchQuery, groupMode, readingDepthFilter, sourceScope]);

  const homepageQuerySignature = useMemo(() => homepageQueryKey(homepageQuery), [homepageQuery]);

  async function reloadHomepageItems() {
    const requestId = homepageRequestIdRef.current + 1;
    homepageRequestIdRef.current = requestId;
    setHomepageLoadState(loadingListLoadState);
    setHomepageWindowState(emptyHomepageWindowState);

    try {
      const response = await withListRetry(() => getHomepageBootstrap(homepageQuery));
      if (homepageRequestIdRef.current === requestId) {
        loadedHomepageItemIdsRef.current = new Set(response.items.map((item) => item.id));
        setHomepageItems(response.items);
        setHomepageSummary(response.summary);
        setHomepageWindowState({
          querySignature: response.querySignature,
          nextCursor: response.nextCursor,
          snapshotToken: response.snapshotToken,
          snapshotExpiresAt: response.snapshotExpiresAt,
          readModelStatus: response.readModelStatus ?? response.summary.readModelStatus,
          snapshotInvalidated: false,
          hasMore: response.hasMore,
          isLoadingMore: false,
          loadMoreErrorMessage: null,
        });
        setHomepageLoadState({status: 'ready', errorMessage: null});
      }
      return response.items;
    } catch (error) {
      console.error('Failed to load homepage cards', error);
      if (homepageRequestIdRef.current === requestId) {
        setHomepageLoadState({status: 'error', errorMessage: formatListError(error, 'AI 首页')});
        setHomepageWindowState((current) => ({...current, isLoadingMore: false}));
      }
      throw error;
    }
  }

  async function refreshHomepageSummary() {
    const requestId = homepageRequestIdRef.current;
    try {
      const nextSummary = await getHomepageSummary(homepageQuery);
      if (homepageRequestIdRef.current === requestId) {
        setHomepageSummary(nextSummary);
        setHomepageWindowState((current) => ({...current, readModelStatus: nextSummary.readModelStatus}));
      }
      return nextSummary;
    } catch (error) {
      console.error('Failed to refresh homepage summary', error);
      throw error;
    }
  }

  function invalidateHomepageSnapshotWindow() {
    setHomepageWindowState((current) => ({
      ...current,
      snapshotToken: null,
      snapshotExpiresAt: null,
      snapshotInvalidated: true,
    }));
  }

  async function loadMoreHomepageItems() {
    const cursor = homepageWindowState.nextCursor;
    if (view !== 'ai' || !homepageWindowState.hasMore || !cursor || homepageWindowState.isLoadingMore) {
      return;
    }
    if (homepageWindowState.snapshotInvalidated) {
      void reloadHomepageItems().catch((error) =>
        console.error('Failed to reload homepage after invalidated snapshot', error),
      );
      return;
    }

    const requestId = homepageRequestIdRef.current;
    setHomepageWindowState((current) => ({...current, isLoadingMore: true, loadMoreErrorMessage: null}));

    try {
      const response = await listHomepageItems({
        ...homepageQuery,
        cursor,
        snapshotToken: homepageWindowState.snapshotToken,
      });
      if (homepageRequestIdRef.current !== requestId) {
        return;
      }
      const currentLoadedItemIds = loadedHomepageItemIdsRef.current;
      const nextItems = response.items.filter((item) => !currentLoadedItemIds.has(item.id));
      loadedHomepageItemIdsRef.current = new Set([
        ...currentLoadedItemIds,
        ...response.items.map((item) => item.id),
      ]);
      setHomepageItems((current) => mergeUniqueFeedItems(current, nextItems));
      setHomepageWindowState({
        querySignature: response.querySignature,
        nextCursor: response.nextCursor,
        snapshotToken: response.snapshotToken,
        snapshotExpiresAt: response.snapshotExpiresAt,
        readModelStatus: response.readModelStatus,
        snapshotInvalidated: false,
        hasMore: response.hasMore,
        isLoadingMore: false,
        loadMoreErrorMessage: null,
      });
    } catch (error) {
      console.error('Failed to load more homepage cards', error);
      if (homepageRequestIdRef.current === requestId) {
        if (isRecoverableHomepageCursorError(error)) {
          void reloadHomepageItems().catch((reloadError) => {
            console.error('Failed to recover homepage window after cursor error', reloadError);
          });
          return;
        }
        setHomepageWindowState((current) => ({
          ...current,
          isLoadingMore: false,
          loadMoreErrorMessage: formatListError(error, '继续加载 AI 首页'),
        }));
      }
    }
  }

  return {
    homepageItems,
    setHomepageItems,
    homepageSummary,
    homepageReadModelStatus: homepageWindowState.readModelStatus,
    homepageWindowState,
    homepageLoadState,
    homepageQuery,
    homepageQuerySignature,
    reloadHomepageItems,
    refreshHomepageSummary,
    invalidateHomepageSnapshotWindow,
    loadMoreHomepageItems,
  };
}
