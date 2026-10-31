import {useState, type CSSProperties, type ReactNode, type UIEvent} from 'react';
import {ChevronDown, MoreVertical, Plus, RefreshCw, StopCircle, X} from 'lucide-react';
import {FeedCard} from '@/components/feed-card';
import {SourceSyncProgressPanel} from '@/components/source-sync-progress-panel';
import type {
  FeedItem,
  FeedItemRoutingCorrectionInput,
  HomepageReadModelStatus,
  ItemStatus,
  ManualUrlCaptureInput,
  SavedItemAIRoute,
  SortMode,
  TimeWindow,
  UploadedMaterialCaptureInput,
  UploadMaterialType,
  ViewMode,
} from '@/types/feed';
import type {SourceSyncProgressState} from '@/types/source';
import type {InsightChannel} from '@/types/settings';

interface FeedListPaneProps {
  contextLabel: string;
  insightEngineArtifactMessage: string | null;
  homepageReadModelStatus: HomepageReadModelStatus | null;
  isKnowledgeBase: boolean;
  insightChannelLabels: Map<string, string>;
  insightChannels: InsightChannel[];
  isLoading: boolean;
  isLoadingMore: boolean;
  listErrorMessage: string | null;
  loadMoreErrorMessage: string | null;
  isRebuildingInsightEngineArtifacts: boolean;
  isInsightEngineArtifactStopRequesting: boolean;
  isSyncingSources: boolean;
  isSourceSyncStopRequesting: boolean;
  canRebuildCurrentSourceArtifacts: boolean;
  items: FeedItem[];
  totalItemCount: number;
  sortMode: SortMode;
  paneWidth: number;
  readerActionMessage: string | null;
  selectedId: number | null;
  insightEngineArtifactBusyReason: string | null;
  insightEngineArtifactProgress: SourceSyncProgressState | null;
  sourceSyncBusyReason: string | null;
  selectedItem: FeedItem | null;
  sourceSyncMessage: string | null;
  sourceSyncProgress: SourceSyncProgressState | null;
  supportsPriorityMode: boolean;
  hasMoreItems: boolean;
  timeWindow: TimeWindow;
  unreadOnly: boolean;
  view: ViewMode;
  onInsightEngineArtifactsRebuild: (scope?: 'current_source' | 'global') => void;
  onInsightEngineArtifactsStop: () => Promise<void>;
  onSelectedInsightEngineArtifactsBuildMissing: () => void;
  onItemOpen: (itemId: number) => void;
  onListRetry: () => void;
  onLoadMore: () => void;
  onManualUrlCapture: (input: ManualUrlCaptureInput) => Promise<void>;
  onUploadedMaterialCapture: (input: UploadedMaterialCaptureInput) => Promise<void>;
  onClearAutoLowPriority: (item: FeedItem) => void;
  onOpenOriginal: (item: FeedItem) => void;
  onOpenSourceApp: (item: FeedItem) => void;
  onRoutingCorrection: (item: FeedItem, correction: FeedItemRoutingCorrectionInput) => Promise<void> | void;
  onSourcesSync: () => void;
  onSourcesSyncStop: () => Promise<void>;
  onSortModeChange: (nextMode: SortMode) => void;
  onStatusChange: (item: FeedItem, nextStatus: ItemStatus) => void;
  onTimeWindowChange: (nextWindow: TimeWindow) => void;
  onUnreadOnlyChange: (nextValue: boolean) => void;
}

const WINDOW_OPTIONS: TimeWindow[] = ['1h', '4h', '6h', '24h', 'all'];
const WINDOW_LABELS: Record<TimeWindow, string> = {
  '1h': '1小时',
  '4h': '4小时',
  '6h': '6小时',
  '24h': '24小时',
  all: '全部',
};

export function FeedListPane({
  contextLabel,
  insightEngineArtifactMessage,
  homepageReadModelStatus,
  isKnowledgeBase,
  insightChannelLabels,
  insightChannels,
  isLoading,
  isLoadingMore,
  listErrorMessage,
  loadMoreErrorMessage,
  isRebuildingInsightEngineArtifacts,
  isInsightEngineArtifactStopRequesting,
  isSyncingSources,
  isSourceSyncStopRequesting,
  canRebuildCurrentSourceArtifacts,
  items,
  totalItemCount,
  sortMode,
  paneWidth,
  readerActionMessage,
  selectedId,
  insightEngineArtifactBusyReason,
  insightEngineArtifactProgress,
  sourceSyncBusyReason,
  selectedItem,
  sourceSyncMessage,
  sourceSyncProgress,
  supportsPriorityMode,
  hasMoreItems,
  timeWindow,
  unreadOnly,
  view,
  onInsightEngineArtifactsRebuild,
  onInsightEngineArtifactsStop,
  onSelectedInsightEngineArtifactsBuildMissing,
  onItemOpen,
  onListRetry,
  onLoadMore,
  onManualUrlCapture,
  onUploadedMaterialCapture,
  onClearAutoLowPriority,
  onOpenOriginal,
  onOpenSourceApp,
  onRoutingCorrection,
  onSourcesSync,
  onSourcesSyncStop,
  onSortModeChange,
  onStatusChange,
  onTimeWindowChange,
  onUnreadOnlyChange,
}: FeedListPaneProps) {
  const [isCaptureFormOpen, setIsCaptureFormOpen] = useState(false);
  const [captureUrl, setCaptureUrl] = useState('');
  const [captureMode, setCaptureMode] = useState<'url' | 'file'>('url');
  const [captureFile, setCaptureFile] = useState<File | null>(null);
  const [captureMaterialType, setCaptureMaterialType] = useState<UploadMaterialType>('markdown');
  const [captureTitle, setCaptureTitle] = useState('');
  const [captureNote, setCaptureNote] = useState('');
  const [captureAiRoute, setCaptureAiRoute] = useState<SavedItemAIRoute>('sensitive');
  const [isSubmittingCapture, setIsSubmittingCapture] = useState(false);
  const [captureError, setCaptureError] = useState<string | null>(null);
  const [isMoreOpen, setIsMoreOpen] = useState(false);
  const sourceSyncDisabledReason = !isSyncingSources ? sourceSyncBusyReason : null;
  const isSourceSyncStopRequested = Boolean(sourceSyncProgress?.stopRequested || isSourceSyncStopRequesting);
  const visibleSourceSyncMessage = sourceSyncMessage;
  const visibleSourceSyncDisabledReason =
    !sourceSyncProgress && !insightEngineArtifactProgress ? sourceSyncDisabledReason : null;
  const visibleInsightEngineArtifactMessage = insightEngineArtifactMessage;
  const visibleReadModelMessage =
    view === 'ai' && homepageReadModelStatus && homepageReadModelStatus.state !== 'fresh'
      ? homepageReadModelStatus.message ??
        `首页 read model 正在补建：缺失 ${homepageReadModelStatus.missingCount}，过期 ${homepageReadModelStatus.staleCount}。`
      : null;
  const insightEngineArtifactDisabledReason = insightEngineArtifactBusyReason;
  const isInsightEngineArtifactStopRequested = Boolean(insightEngineArtifactProgress?.stopRequested);
  const sortModes: Array<{id: SortMode; label: string; disabled?: boolean}> = [
    {id: 'latest', label: '最新'},
    {id: 'priority', label: 'AI 精选', disabled: view !== 'ai' || !supportsPriorityMode},
  ];

  function handleListScroll(event: UIEvent<HTMLDivElement>) {
    if (view !== 'ai' || !hasMoreItems || isLoadingMore) {
      return;
    }
    const target = event.currentTarget;
    const distanceFromBottom = target.scrollHeight - target.scrollTop - target.clientHeight;
    if (distanceFromBottom < 520) {
      onLoadMore();
    }
  }

  function handleSourcesSyncStopClick() {
    onSourcesSyncStop().catch(() => undefined);
  }

  function handleInsightEngineArtifactsStopClick() {
    onInsightEngineArtifactsStop().catch(() => undefined);
  }

  async function submitManualUrlCapture() {
    if (isSubmittingCapture) {
      return;
    }

    if (!captureUrl.trim()) {
      setCaptureError('请输入要保存的 URL。');
      return;
    }

    setIsSubmittingCapture(true);
    setCaptureError(null);

    try {
      await onManualUrlCapture({
        url: captureUrl,
        title: normalizeOptionalInput(captureTitle),
        userNote: normalizeOptionalInput(captureNote),
        aiRoute: captureAiRoute,
      });
      setCaptureUrl('');
      setCaptureTitle('');
      setCaptureNote('');
      setCaptureAiRoute('sensitive');
      setIsCaptureFormOpen(false);
    } catch (error) {
      console.error('Failed to capture manual URL', error);
      setCaptureError(error instanceof Error ? error.message : '保存 URL 失败，请稍后重试。');
    } finally {
      setIsSubmittingCapture(false);
    }
  }

  async function submitUploadedMaterialCapture() {
    if (isSubmittingCapture) {
      return;
    }

    if (!captureFile) {
      setCaptureError('请选择要上传的文件。');
      return;
    }

    setIsSubmittingCapture(true);
    setCaptureError(null);

    try {
      await onUploadedMaterialCapture({
        file: captureFile,
        materialType: captureMaterialType,
        title: normalizeOptionalInput(captureTitle),
        userNote: normalizeOptionalInput(captureNote),
        aiRoute: captureAiRoute,
      });
      setCaptureFile(null);
      setCaptureTitle('');
      setCaptureNote('');
      setCaptureAiRoute('sensitive');
      setCaptureMaterialType('markdown');
      setIsCaptureFormOpen(false);
    } catch (error) {
      console.error('Failed to capture uploaded material', error);
      setCaptureError(error instanceof Error ? error.message : '外部文件保存失败，请稍后重试。');
    } finally {
      setIsSubmittingCapture(false);
    }
  }

  return (
    <main
      className="flex w-full shrink-0 flex-col border-r border-[var(--border-subtle)] bg-[var(--bg-app)] md:w-[var(--pane-width)]"
      style={{'--pane-width': `${paneWidth}px`} as CSSProperties}
    >
      <div className="panel flex shrink-0 flex-col gap-3 border-b border-[var(--border-subtle)] px-4 py-3">
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-[var(--text-main)]">{contextLabel}</div>
            <div className="mt-1 text-xs text-[var(--text-muted)]">
              {items.length === totalItemCount ? `${totalItemCount} 条` : `已加载 ${items.length} / ${totalItemCount} 条`}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {view === 'ai' || view === 'reader' ? (
              <button
                className="rounded-md p-2 text-[var(--text-soft)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                title={sourceSyncDisabledReason ?? (isSyncingSources ? '停止刷新：当前来源完成后停止' : '刷新来源')}
                type="button"
                disabled={Boolean(sourceSyncDisabledReason) || (isSyncingSources && isSourceSyncStopRequested)}
                onClick={isSyncingSources ? handleSourcesSyncStopClick : onSourcesSync}
              >
                {isSyncingSources ? (
                  <StopCircle size={14} className="text-[var(--warning)]" />
                ) : (
                  <RefreshCw size={14} />
                )}
              </button>
            ) : null}
            {isKnowledgeBase ? (
              <button
                className="inline-flex items-center gap-2 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]"
                type="button"
                onClick={() => {
                  setIsCaptureFormOpen((current) => !current);
                  setCaptureError(null);
                }}
              >
                {isCaptureFormOpen ? <X size={14} /> : <Plus size={14} />}
                <span>{isCaptureFormOpen ? '收起' : '外部来源保存'}</span>
              </button>
            ) : null}
            {view === 'ai' && isRebuildingInsightEngineArtifacts ? (
              <button
                className="inline-flex items-center gap-1 rounded-md border border-[var(--warning)] px-2.5 py-2 text-xs font-medium text-[var(--warning)] transition-colors hover:bg-[var(--bg-hover)] disabled:cursor-not-allowed disabled:opacity-50"
                title="停止洞察补齐：当前步骤完成后取消剩余任务"
                type="button"
                disabled={isInsightEngineArtifactStopRequested || isInsightEngineArtifactStopRequesting}
                onClick={handleInsightEngineArtifactsStopClick}
              >
                <StopCircle size={14} />
                <span>{isInsightEngineArtifactStopRequested || isInsightEngineArtifactStopRequesting ? '停止已请求' : '停止洞察'}</span>
              </button>
            ) : null}
            {view === 'ai' ? (
              <div className="relative">
                <button
                  className="rounded-md p-2 text-[var(--text-soft)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]"
                  title="维护操作"
                  type="button"
                  onClick={() => setIsMoreOpen((current) => !current)}
                >
                  <MoreVertical size={16} />
                </button>
                {isMoreOpen ? (
                  <div className="absolute right-0 top-9 z-20 w-56 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-1 shadow-[0_18px_60px_rgba(15,23,42,0.2)]">
                    <button
                      className="w-full rounded-md px-2 py-1.5 text-left text-sm text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                      type="button"
                      disabled={Boolean(insightEngineArtifactDisabledReason) || isRebuildingInsightEngineArtifacts || !selectedItem}
                      title={insightEngineArtifactDisabledReason ?? (selectedItem ? selectedItem.title : '请先选择一张 AI 首页卡片')}
                      onClick={() => {
                        setIsMoreOpen(false);
                        onSelectedInsightEngineArtifactsBuildMissing();
                      }}
                    >
                      {isRebuildingInsightEngineArtifacts ? '补齐中...' : '补齐当前卡片未成功洞察'}
                    </button>
                    {canRebuildCurrentSourceArtifacts ? (
                      <button
                        className="w-full rounded-md px-2 py-1.5 text-left text-sm text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                        type="button"
                        disabled={Boolean(insightEngineArtifactDisabledReason)}
                        title={insightEngineArtifactDisabledReason ?? undefined}
                        onClick={() => {
                          setIsMoreOpen(false);
                          onInsightEngineArtifactsRebuild('current_source');
                        }}
                      >
                        {isRebuildingInsightEngineArtifacts ? '补齐中...' : '补齐当前来源未成功洞察'}
                      </button>
                    ) : null}
                    <button
                      className="w-full rounded-md px-2 py-1.5 text-left text-sm text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                      type="button"
                      disabled={Boolean(insightEngineArtifactDisabledReason)}
                      title={insightEngineArtifactDisabledReason ?? undefined}
                      onClick={() => {
                        setIsMoreOpen(false);
                        onInsightEngineArtifactsRebuild('global');
                      }}
                    >
                      {isRebuildingInsightEngineArtifacts ? '补齐中...' : '补齐全局未成功洞察（最多 200）'}
                    </button>
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        </div>

        {!isKnowledgeBase ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <div className="inline-flex rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-1">
                {sortModes.map((option) => (
                  <button
                    key={option.id}
                    className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                      sortMode === option.id
                        ? 'bg-[var(--bg-panel)] text-[var(--text-main)] shadow-sm'
                        : 'text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]'
                    }`}
                    disabled={option.disabled}
                    onClick={() => onSortModeChange(option.id)}
                    type="button"
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              <button
                className={`rounded-md border px-3 py-1.5 text-xs font-medium transition-colors ${
                  unreadOnly
                    ? 'border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]'
                    : 'border-[var(--border-subtle)] bg-[var(--bg-panel)] text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]'
                }`}
                onClick={() => onUnreadOnlyChange(!unreadOnly)}
                type="button"
              >
                仅未读
              </button>
              <label className="inline-flex items-center gap-2 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-2.5 py-1.5 text-xs text-[var(--text-muted)]">
                <span>时间</span>
                <select
                  className="bg-transparent text-[var(--text-main)] outline-none"
                  value={timeWindow}
                  onChange={(event) => onTimeWindowChange(event.target.value as TimeWindow)}
                >
                  {WINDOW_OPTIONS.map((option) => (
                    <option key={option} value={option}>
                      {WINDOW_LABELS[option]}
                    </option>
                  ))}
                </select>
                <ChevronDown size={13} />
              </label>
            </div>

            {sourceSyncProgress ? <SourceSyncProgressPanel progress={sourceSyncProgress} /> : null}
            {insightEngineArtifactProgress ? (
              <SourceSyncProgressPanel progress={insightEngineArtifactProgress} variant="insight" />
            ) : null}

            {visibleSourceSyncMessage ? (
              <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
                {visibleSourceSyncMessage}
              </div>
            ) : null}

            {!visibleSourceSyncMessage && visibleSourceSyncDisabledReason ? (
              <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-xs text-[var(--text-muted)]">
                {visibleSourceSyncDisabledReason}
              </div>
            ) : null}

            {readerActionMessage ? (
              <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
                {readerActionMessage}
              </div>
            ) : null}

            {view === 'ai' && visibleInsightEngineArtifactMessage ? (
              <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
                {visibleInsightEngineArtifactMessage}
              </div>
            ) : null}

            {visibleReadModelMessage ? (
              <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
                {visibleReadModelMessage}
              </div>
            ) : null}
          </>
        ) : (
          <>
            <div className="text-xs text-[var(--text-muted)]">
              当前显示已保存内容，可从阅读器保存，也可保存外部 URL 或 Markdown/HTML 文件。
            </div>
            {isCaptureFormOpen ? (
              <form
                className="space-y-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (captureMode === 'url') {
                    void submitManualUrlCapture();
                  } else {
                    void submitUploadedMaterialCapture();
                  }
                }}
              >
                <div className="inline-flex w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-1">
                  {(['url', 'file'] as const).map((mode) => (
                    <button
                      key={mode}
                      className={`flex-1 rounded px-2.5 py-1.5 text-xs font-medium transition-colors ${
                        captureMode === mode
                          ? 'bg-[var(--bg-panel-strong)] text-[var(--text-main)] shadow-sm'
                          : 'text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]'
                      }`}
                      disabled={isSubmittingCapture}
                      type="button"
                      onClick={() => {
                        setCaptureMode(mode);
                        setCaptureError(null);
                      }}
                    >
                      {mode === 'url' ? 'URL' : '文件'}
                    </button>
                  ))}
                </div>
                <input
                  className={`${captureMode === 'url' ? 'block' : 'hidden'} w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)] outline-none transition-colors placeholder:text-[var(--text-soft)] focus:border-[var(--accent)]`}
                  disabled={isSubmittingCapture}
                  placeholder="https://example.com/article"
                  value={captureUrl}
                  onChange={(event) => setCaptureUrl(event.target.value)}
                />
                {captureMode === 'file' ? (
                  <div className="grid gap-3">
                    <input
                      className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)] file:mr-3 file:rounded file:border-0 file:bg-[var(--accent-soft)] file:px-2.5 file:py-1.5 file:text-xs file:font-medium file:text-[var(--accent)]"
                      disabled={isSubmittingCapture}
                      type="file"
                      accept=".md,.markdown,.html,.htm,text/markdown,text/html,application/xhtml+xml"
                      onChange={(event) => setCaptureFile(event.target.files?.[0] ?? null)}
                    />
                    <label className="flex items-center justify-between gap-3 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs text-[var(--text-muted)]">
                      <span>材料类型</span>
                      <select
                        className="bg-transparent text-[var(--text-main)] outline-none"
                        disabled={isSubmittingCapture}
                        value={captureMaterialType}
                        onChange={(event) => setCaptureMaterialType(event.target.value as UploadMaterialType)}
                      >
                        <option value="markdown">Markdown</option>
                        <option value="html">HTML</option>
                        <option value="singlefile_html">SingleFile HTML</option>
                      </select>
                    </label>
                  </div>
                ) : null}
                <input
                  className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)] outline-none transition-colors placeholder:text-[var(--text-soft)] focus:border-[var(--accent)]"
                  disabled={isSubmittingCapture}
                  placeholder="标题（可选）"
                  value={captureTitle}
                  onChange={(event) => setCaptureTitle(event.target.value)}
                />
                <textarea
                  className="min-h-20 w-full resize-y rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)] outline-none transition-colors placeholder:text-[var(--text-soft)] focus:border-[var(--accent)]"
                  disabled={isSubmittingCapture}
                  placeholder="备注（可选）"
                  value={captureNote}
                  onChange={(event) => setCaptureNote(event.target.value)}
                />
                <label className="flex items-center justify-between gap-3 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs text-[var(--text-muted)]">
                  <span>AI 路由</span>
                  <select
                    className="bg-transparent text-[var(--text-main)] outline-none"
                    disabled={isSubmittingCapture}
                    value={captureAiRoute}
                    onChange={(event) => setCaptureAiRoute(event.target.value as SavedItemAIRoute)}
                  >
                    <option value="sensitive">Sensitive</option>
                    <option value="general">General</option>
                  </select>
                </label>
                {isSubmittingCapture ? (
                  <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs text-[var(--text-soft)]">
                    {captureMode === 'url' ? '正在保存并捕获页面，完成后会进入知识库。' : '正在保存并处理外部文件。'}
                  </div>
                ) : null}
                {captureError ? <div className="text-xs text-red-500">{captureError}</div> : null}
                <div className="flex items-center justify-end gap-2">
                  <button
                    className="rounded-md px-3 py-2 text-xs text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                    disabled={isSubmittingCapture}
                    type="button"
                    onClick={() => {
                      setCaptureUrl('');
                      setCaptureMode('url');
                      setCaptureFile(null);
                      setCaptureMaterialType('markdown');
                      setCaptureTitle('');
                      setCaptureNote('');
                      setCaptureAiRoute('sensitive');
                      setCaptureError(null);
                      setIsCaptureFormOpen(false);
                    }}
                  >
                    取消
                  </button>
                  <button
                    className="rounded-md bg-[var(--accent)] px-3 py-2 text-xs font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-50"
                    disabled={isSubmittingCapture}
                    type="submit"
                  >
                    {isSubmittingCapture ? '保存中...' : '保存'}
                  </button>
                </div>
              </form>
            ) : null}
          </>
        )}
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto p-3" onScroll={handleListScroll}>
        {listErrorMessage && items.length > 0 ? (
          <div className="flex items-center justify-between gap-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs text-[var(--text-main)]">
            <span>{listErrorMessage}</span>
            <button
              className="inline-flex shrink-0 items-center gap-1 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-2 py-1 font-medium text-[var(--text-main)] transition-colors hover:bg-[var(--bg-hover)]"
              type="button"
              onClick={onListRetry}
            >
              <RefreshCw size={13} />
              重试
            </button>
          </div>
        ) : null}
        {isLoading ? (
          <EmptyState title="加载中" description="正在从后端读取真实数据..." />
        ) : listErrorMessage ? (
          <EmptyState
            title="加载失败"
            description={listErrorMessage}
            action={
              <button
                className="mt-4 inline-flex items-center gap-1.5 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm font-medium text-[var(--text-main)] transition-colors hover:bg-[var(--bg-hover)]"
                type="button"
                onClick={onListRetry}
              >
                <RefreshCw size={15} />
                重试
              </button>
            }
          />
        ) : items.length === 0 ? (
          <EmptyState
            title="暂无内容"
            description={isKnowledgeBase ? '当前没有已保存条目。' : '当前视图下没有符合条件的条目。'}
          />
        ) : (
          <>
            {items.map((item) => (
              <FeedCard
                key={item.id}
                isSelected={selectedId === item.id}
                insightChannelLabels={insightChannelLabels}
                insightChannels={insightChannels}
                item={item}
                view={view}
                onOpen={() => onItemOpen(item.id)}
                onOpenOriginal={onOpenOriginal}
                onOpenSourceApp={onOpenSourceApp}
                onRoutingCorrection={onRoutingCorrection}
                onClearAutoLowPriority={onClearAutoLowPriority}
                onStatusChange={onStatusChange}
              />
            ))}
            {view === 'ai' ? (
              <ListFooter
                errorMessage={loadMoreErrorMessage}
                hasMore={hasMoreItems}
                isLoading={isLoadingMore}
                onRetry={onLoadMore}
              />
            ) : null}
          </>
        )}
      </div>
    </main>
  );
}

function normalizeOptionalInput(value: string) {
  const normalized = value.trim();
  return normalized || null;
}

function EmptyState({action, description, title}: {action?: ReactNode; description: string; title: string}) {
  return (
    <div className="panel flex min-h-[220px] flex-col items-center justify-center rounded-2xl border border-dashed border-[var(--border-subtle)] px-8 text-center">
      <div className="text-sm font-semibold text-[var(--text-main)]">{title}</div>
      <p className="mt-2 max-w-sm text-sm leading-relaxed text-[var(--text-muted)]">{description}</p>
      {action}
    </div>
  );
}

function ListFooter({
  errorMessage,
  hasMore,
  isLoading,
  onRetry,
}: {
  errorMessage: string | null;
  hasMore: boolean;
  isLoading: boolean;
  onRetry: () => void;
}) {
  if (errorMessage) {
    return (
      <div className="flex items-center justify-between gap-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs text-[var(--text-main)]">
        <span>{errorMessage}</span>
        <button
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-2 py-1 font-medium text-[var(--text-main)] transition-colors hover:bg-[var(--bg-hover)]"
          type="button"
          onClick={onRetry}
        >
          <RefreshCw size={13} />
          重试
        </button>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-center text-xs text-[var(--text-muted)]">
        正在加载更多...
      </div>
    );
  }

  if (!hasMore) {
    return (
      <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-center text-xs text-[var(--text-soft)]">
        已加载全部当前结果
      </div>
    );
  }

  return (
    <button
      className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-center text-xs font-medium text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]"
      type="button"
      onClick={onRetry}
    >
      加载更多
    </button>
  );
}
