import {useEffect, useState, type PointerEvent as ReactPointerEvent} from 'react';
import {DetailPane} from '@/components/detail-pane';
import {FeedListPane} from '@/components/feed-list-pane';
import {SettingsWorkspace} from '@/components/settings-workspace';
import {Sidebar} from '@/components/sidebar';
import {SourceEditorDialog} from '@/components/source-editor-dialog';
import {TopBar} from '@/components/top-bar';
import {useInfoFlowApp} from '@/hooks/use-info-flow-app';

const LAYOUT_STORAGE_KEY = 'ifa:layout:pane-widths:v1';
const DEFAULT_PANE_WIDTHS = {sidebar: 256, list: 460};

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function readPaneWidths() {
  if (typeof window === 'undefined') {
    return DEFAULT_PANE_WIDTHS;
  }

  try {
    const parsed = JSON.parse(window.localStorage.getItem(LAYOUT_STORAGE_KEY) ?? '{}') as Partial<typeof DEFAULT_PANE_WIDTHS>;
    return {
      sidebar: clamp(typeof parsed.sidebar === 'number' ? parsed.sidebar : DEFAULT_PANE_WIDTHS.sidebar, 208, 360),
      list: clamp(typeof parsed.list === 'number' ? parsed.list : DEFAULT_PANE_WIDTHS.list, 340, 680),
    };
  } catch {
    return DEFAULT_PANE_WIDTHS;
  }
}

export default function App() {
  const app = useInfoFlowApp();
  const [paneWidths, setPaneWidths] = useState(readPaneWidths);

  useEffect(() => {
    window.localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(paneWidths));
  }, [paneWidths]);

  function startPaneResize(pane: 'sidebar' | 'list', event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const initial = {...paneWidths};
    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    function onPointerMove(moveEvent: PointerEvent) {
      const delta = moveEvent.clientX - startX;
      setPaneWidths({
        sidebar: pane === 'sidebar' ? clamp(initial.sidebar + delta, 208, 360) : initial.sidebar,
        list: pane === 'list' ? clamp(initial.list + delta, 340, 680) : initial.list,
      });
    }

    function onPointerUp() {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
    }

    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
  }

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-[var(--bg-app)] text-[var(--text-main)] transition-colors duration-200">
      <TopBar
        searchQuery={app.searchQuery}
        searchPlaceholder={app.searchPlaceholder}
        theme={app.theme}
        view={app.view}
        isPreviewingSource={app.isPreviewingSource}
        onSearchQueryChange={app.setSearchQuery}
        onSettingsOpen={() => app.setView('settings')}
        onSourceQuickAdd={app.previewQuickSource}
        onThemeToggle={app.toggleTheme}
        onViewChange={app.setView}
      />

      {app.view === 'settings' ? (
        <SettingsWorkspace
          searchQuery={app.searchQuery}
          sources={app.sources}
          feedItemArtifactRefreshVersion={app.feedItemArtifactRefreshVersion}
          insightEngineArtifactOperation={app.insightEngineArtifactOperation}
          insightEngineArtifactProgress={app.insightEngineArtifactProgress}
          isInsightEngineArtifactStopRequesting={app.isInsightEngineArtifactStopRequesting}
          isSourceSyncStopRequesting={app.isSourceSyncStopRequesting}
          sourceSyncBusyReason={app.sourceSyncBusyReason}
          sourceSyncMessage={app.sourceSyncMessage}
          sourceSyncOperation={app.sourceSyncOperation}
          sourceSyncProgress={app.sourceSyncProgress}
          onOpenFeedItem={(itemId) => {
            app.setView('ai');
            app.openItem(itemId);
          }}
          onFeedItemInsightArtifactOperation={app.runFeedItemInsightArtifactOperation}
          onSourceIdsSync={app.syncSourceIdsNow}
          onSourceSync={app.syncSingleSourceNow}
          onSourcesSync={app.syncSourcesNow}
          onSourcesSyncStop={app.stopSourcesSyncNow}
          onInsightEngineArtifactsStop={app.stopInsightEngineArtifactsNow}
          onSourcesChanged={app.reloadSources}
          onInsightChannelsChanged={async () => {
            await app.reloadInsightChannels();
          }}
          onBackendOperation={app.trackBackendOperation}
          onSourceEdit={app.openSourceEditor}
        />
      ) : (
        <div className="flex flex-1 overflow-hidden">
          <Sidebar
            activeFacet={app.activeFacet}
            channelFacets={app.channelFacets}
            counts={app.counts}
            flowLaneFacets={app.flowLaneFacets}
            groupMode={app.groupMode}
            insightChannels={app.insightChannels}
            knowledgeCounts={app.knowledgeCounts}
            knowledgeScope={app.knowledgeScope}
            readingDepthFilter={app.readingDepthFilter}
            sourceCategorySuggestions={app.sourceCategorySuggestions}
            sourceItemCounts={app.sourceItemCounts}
            sourceSyncBusyReason={app.sourceSyncBusyReason}
            sourceScope={app.sourceScope}
            sources={app.sources}
            supportsTopicMode={app.supportsTopicMode}
            topicFacets={app.topicFacets}
            view={app.view}
            width={paneWidths.sidebar}
            onGroupModeChange={app.setGroupMode}
            onKnowledgeScopeChange={app.selectKnowledgeScope}
            onReadingDepthFilterChange={app.setReadingDepthFilter}
            onSelectAllSources={app.selectAllSources}
            onSelectChannel={app.selectChannelFacet}
            onSelectFlowLane={app.selectFlowLaneFacet}
            onSelectSource={app.selectSource}
            onSelectSourceCategory={app.selectSourceCategory}
            onSelectTopic={app.selectTopicFacet}
            onSourceActiveChange={app.setSourceActive}
            onSourceEdit={app.openSourceEditor}
            onSourceMarkReadFull={app.markSourceItemsReadFull}
            onSourceMoveToCategory={app.moveSourceToCategory}
            onSourceSync={app.syncSingleSourceNow}
            onViewChange={app.setView}
          />
          <div
            className="hidden w-1.5 shrink-0 cursor-col-resize border-r border-[var(--border-subtle)] bg-[var(--bg-app)] transition-colors hover:bg-[var(--bg-hover)] md:block"
            role="separator"
            aria-orientation="vertical"
            title="拖拽调整侧栏宽度"
            onPointerDown={(event) => startPaneResize('sidebar', event)}
          />

          <FeedListPane
            contextLabel={app.contextLabel}
            insightEngineArtifactMessage={app.insightEngineArtifactMessage}
            homepageReadModelStatus={app.homepageReadModelStatus}
            isKnowledgeBase={app.view === 'kb'}
            insightChannelLabels={app.insightChannelLabels}
            insightChannels={app.insightChannels}
            isLoading={app.isLoading}
            isLoadingMore={app.isLoadingMoreHomepageItems}
            listErrorMessage={app.listErrorMessage}
            loadMoreErrorMessage={app.loadMoreErrorMessage}
            isRebuildingInsightEngineArtifacts={app.isRebuildingInsightEngineArtifacts}
            isInsightEngineArtifactStopRequesting={app.isInsightEngineArtifactStopRequesting}
            isSyncingSources={app.isSyncingSources}
            isSourceSyncStopRequesting={app.isSourceSyncStopRequesting}
            canRebuildCurrentSourceArtifacts={app.canRebuildCurrentSourceArtifacts}
            items={app.visibleItems}
            totalItemCount={app.visibleItemTotalCount}
            sortMode={app.sortMode}
            paneWidth={paneWidths.list}
            readerActionMessage={app.readerActionMessage}
            selectedId={app.selectedId}
            insightEngineArtifactBusyReason={app.insightEngineArtifactBusyReason}
            insightEngineArtifactProgress={app.insightEngineArtifactProgress}
            sourceSyncBusyReason={app.sourceSyncBusyReason}
            selectedItem={app.selectedItem}
            sourceSyncMessage={app.sourceSyncMessage}
            sourceSyncProgress={app.sourceSyncProgress}
            supportsPriorityMode={app.supportsPriorityMode}
            hasMoreItems={app.hasMoreHomepageItems}
            timeWindow={app.timeWindow}
            unreadOnly={app.unreadOnly}
            view={app.view}
            onListRetry={app.retryCurrentList}
            onLoadMore={app.loadMoreHomepageItems}
            onManualUrlCapture={app.createManualUrlCapture}
            onUploadedMaterialCapture={app.createUploadedMaterialCapture}
            onClearAutoLowPriority={app.clearAutoLowPriority}
            onInsightEngineArtifactsRebuild={app.rebuildInsightEngineArtifactsNow}
            onInsightEngineArtifactsStop={app.stopInsightEngineArtifactsNow}
            onSelectedInsightEngineArtifactsBuildMissing={app.buildSelectedFeedItemMissingInsightEngineArtifacts}
            onItemOpen={app.openItem}
            onOpenOriginal={app.openOriginal}
            onOpenSourceApp={app.openSourceApp}
            onRoutingCorrection={app.updateFeedItemRoutingCorrection}
            onSourcesSync={app.syncSourcesNow}
            onSourcesSyncStop={app.stopSourcesSyncNow}
            onSortModeChange={app.setSortMode}
            onStatusChange={app.updateItemStatus}
            onTimeWindowChange={app.setTimeWindow}
            onUnreadOnlyChange={app.setUnreadOnly}
          />
          <div
            className="hidden w-1.5 shrink-0 cursor-col-resize border-r border-[var(--border-subtle)] bg-[var(--bg-app)] transition-colors hover:bg-[var(--bg-hover)] lg:block"
            role="separator"
            aria-orientation="vertical"
            title="拖拽调整列表宽度"
            onPointerDown={(event) => startPaneResize('list', event)}
          />

          <DetailPane
            captureOperation={app.selectedCaptureOperation}
            detailErrorMessage={app.selectedItemDetailErrorMessage}
            detailOperations={app.selectedDetailOperations}
            insightChannels={app.insightChannels}
            isReadOnly={app.selectedItemStale || app.selectedItemUnavailable}
            isUnavailable={app.selectedItemUnavailable}
            item={app.selectedItem}
            view={app.view}
            onOpenOriginal={app.openOriginal}
            onOpenSourceApp={app.openSourceApp}
            onInsightEngineArtifactsRebuild={app.rebuildSelectedFeedItemInsightEngineArtifacts}
            onClearAutoLowPriority={app.clearAutoLowPriority}
            onExtractContent={app.extractSelectedFeedItemContent}
            onChannelCorrectionClear={app.clearFeedItemChannelCorrection}
            onInsightFeedbackSave={app.updateFeedItemInsightFeedback}
            onSavedItemManualImport={app.importSavedItemManualFile}
            onSavedItemLocalMirrorReexport={app.reexportSavedItemLocalMirror}
            onSavedItemCaptureRetry={app.retrySavedItemCapture}
            onSavedItemInsightRebuild={app.rebuildSavedItemInsight}
            onSavedItemCuratedInsightClear={app.clearSavedItemCuratedInsight}
            onSavedItemCuratedInsightSave={app.updateSavedItemCuratedInsight}
            onSavedItemNoteSave={app.updateSavedItemNote}
            onStatusChange={app.updateItemStatus}
          />
        </div>
      )}

      {app.sourceEditor ? (
        <SourceEditorDialog
          categorySuggestions={app.sourceCategorySuggestions}
          insightChannels={app.insightChannels}
          isSaving={app.isSavingSource}
          isRoutingLoading={app.sourceEditor.isRoutingLoading}
          isSourceProfileLoading={app.sourceEditor.isSourceProfileLoading}
          mode={app.sourceEditor.mode}
          preview={app.sourceEditor.preview}
          routing={app.sourceEditor.routing}
          routingError={app.sourceEditor.routingError}
          source={app.sourceEditor.source}
          sourcePreferenceProfile={app.sourceEditor.sourcePreferenceProfile}
          sourceProfileError={app.sourceEditor.sourceProfileError}
          onClose={app.closeSourceEditor}
          onDeleteSourceProfileDraft={app.deleteSourceEditorProfileDraft}
          onSave={app.saveSourceEditor}
          onSaveSourceProfileDraft={app.saveSourceEditorProfileDraft}
        />
      ) : null}
    </div>
  );
}
