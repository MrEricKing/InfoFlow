import {useEffect, useState, type ReactNode} from 'react';
import {
  ChevronDown,
  Download,
  ExternalLink,
  Eye,
  FileArchive,
  FileText,
  Image as ImageIcon,
  RefreshCw,
  Upload,
} from 'lucide-react';
import {CaptureStatusBadge} from '@/components/capture-status-badge';
import {KnowledgeMarkdownReader} from '@/components/knowledge-markdown-reader';
import {formatAbsoluteDate} from '@/lib/date';
import {buildArchiveArtifactKey} from '@/lib/knowledge-markdown';
import type {ArchiveArtifact, CaptureOperation, FeedItem, SavedItemManualImportInput, UploadMaterialType} from '@/types/feed';

export type KnowledgeDetailPanelKey = 'archive' | 'archiveImages' | 'capture' | 'mirror';

export interface MarkdownPreviewState {
  artifactKey: string;
  title: string;
  content: string;
  isLoading: boolean;
  error: string | null;
}

export const KNOWLEDGE_PANEL_COLLAPSE_STORAGE_KEY = 'ifa:knowledge-detail-collapsed-panels:v1';
export const DEFAULT_KNOWLEDGE_PANEL_COLLAPSED: Record<KnowledgeDetailPanelKey, boolean> = {
  archive: false,
  archiveImages: true,
  capture: true,
  mirror: true,
};

interface KnowledgeDetailPanelsProps {
  archiveArtifacts: ArchiveArtifact[];
  archiveStatusMessage: string | null;
  canManualImportCapture: boolean;
  canRunCapture: boolean;
  captureOperation: CaptureOperation | null;
  collapsedKnowledgePanels: Record<KnowledgeDetailPanelKey, boolean>;
  coreArchiveArtifacts: ArchiveArtifact[];
  hasMirrorExportableArtifacts: boolean;
  imageArchiveArtifacts: ArchiveArtifact[];
  isCaptureRunning: boolean;
  isMirrorReexportBlocked: boolean;
  isReexportingMirrorBusy: boolean;
  item: FeedItem;
  markdownPreview: MarkdownPreviewState | null;
  mirrorError: string | null;
  primaryMarkdownArtifact: ArchiveArtifact | null;
  onCloseMarkdownPreview: () => void;
  onOpenArtifactUrl: (url: string | null) => void;
  onPreviewMarkdownArtifact: (artifact: ArchiveArtifact) => void;
  onReexportLocalMirror: () => void;
  onManualImport: (input: SavedItemManualImportInput) => Promise<void>;
  onRetryCapture: () => void;
  onTogglePanel: (panelKey: KnowledgeDetailPanelKey) => void;
}

export function KnowledgeDetailPanels({
  archiveArtifacts,
  archiveStatusMessage,
  canManualImportCapture,
  canRunCapture,
  captureOperation,
  collapsedKnowledgePanels,
  coreArchiveArtifacts,
  hasMirrorExportableArtifacts,
  imageArchiveArtifacts,
  isCaptureRunning,
  isMirrorReexportBlocked,
  isReexportingMirrorBusy,
  item,
  markdownPreview,
  mirrorError,
  primaryMarkdownArtifact,
  onCloseMarkdownPreview,
  onOpenArtifactUrl,
  onPreviewMarkdownArtifact,
  onReexportLocalMirror,
  onManualImport,
  onRetryCapture,
  onTogglePanel,
}: KnowledgeDetailPanelsProps) {
  const [isManualImportOpen, setIsManualImportOpen] = useState(false);
  const [manualImportFile, setManualImportFile] = useState<File | null>(null);
  const [manualImportMaterialType, setManualImportMaterialType] = useState<UploadMaterialType>('singlefile_html');
  const [isManualImporting, setIsManualImporting] = useState(false);
  const [manualImportError, setManualImportError] = useState<string | null>(null);

  useEffect(() => {
    setIsManualImportOpen(false);
    setManualImportFile(null);
    setManualImportMaterialType('singlefile_html');
    setIsManualImporting(false);
    setManualImportError(null);
  }, [item.id]);

  async function submitManualImport() {
    if (!manualImportFile || isManualImporting) {
      setManualImportError('请选择要导入的文件。');
      return;
    }
    setIsManualImporting(true);
    setManualImportError(null);
    try {
      await onManualImport({
        file: manualImportFile,
        materialType: manualImportMaterialType,
        title: null,
        userNote: null,
      });
      setManualImportFile(null);
      setManualImportMaterialType('singlefile_html');
      setIsManualImportOpen(false);
    } catch (error) {
      console.error('Failed to import saved item manual file', error);
      setManualImportError(error instanceof Error ? error.message : '手动导入失败，请稍后重试。');
    } finally {
      setIsManualImporting(false);
    }
  }

  return (
    <>
      <div className="mt-4 flex flex-wrap gap-2 text-xs text-[var(--text-soft)]">
        {item.hasCanonicalSnapshot ? (
          <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-1">
            Canonical Snapshot
          </span>
        ) : null}
        {item.hasOriginalPageSnapshot ? (
          <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-1">
            Original Page Snapshot
          </span>
        ) : null}
        {item.hasSinglefileHtml ? (
          <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-1">
            SingleFile HTML
          </span>
        ) : null}
        {item.hasCrawl4aiRawMarkdown ? (
          <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-1">
            Crawl4AI Raw Markdown
          </span>
        ) : null}
        {item.hasCrawl4aiFitMarkdown ? (
          <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-1">
            Crawl4AI Fit Markdown
          </span>
        ) : null}
        {item.hasCrawl4aiFitImageReinsertedMarkdown ? (
          <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-1">
            Crawl4AI Fit Markdown + Images
          </span>
        ) : null}
        {item.sourceMaterialArtifact?.status === 'available' ? (
          <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-1">
            External Source File
          </span>
        ) : null}
        {archiveArtifacts.some(
          (artifact) => artifact.artifactKind === 'uploaded_markdown_v1' && artifact.status === 'available',
        ) ? (
          <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-1">
            External Source Markdown
          </span>
        ) : null}
        {item.imageAssetCount ? (
          <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-1">
            {item.imageAssetCount} Image Asset{item.imageAssetCount === 1 ? '' : 's'}
          </span>
        ) : null}
      </div>

      <CollapsibleKnowledgePanel
        actions={
          <button
            className="rounded-md bg-[var(--accent)] px-3 py-1.5 text-xs font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-45"
            disabled={!canRunCapture}
            type="button"
            onClick={() => {
              onRetryCapture();
            }}
          >
            {getCaptureActionLabel(item.captureStatus, isCaptureRunning)}
          </button>
        }
        icon={null}
        isCollapsed={collapsedKnowledgePanels.capture}
        summary={buildCapturePanelSummary(item)}
        title="捕获处理"
        onToggle={() => onTogglePanel('capture')}
      >

        {item.captureStatus ? (
          <div className="space-y-3 text-sm text-[var(--text-muted)]">
            <div className="flex flex-wrap gap-4">
              <span>最新 Run：{item.latestCaptureRunId ?? '—'}</span>
              <span>开始时间：{item.captureStartedAt ? formatAbsoluteDate(item.captureStartedAt) : '—'}</span>
              <span>结束时间：{item.captureFinishedAt ? formatAbsoluteDate(item.captureFinishedAt) : '—'}</span>
            </div>

            {captureOperation ? (
              <CaptureProgressPanel operation={captureOperation} />
            ) : item.captureStatus === 'pending' ? (
              <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs text-[var(--text-soft)]">
                捕获请求正在执行，等待后端返回最新结果。
              </div>
            ) : null}

            {item.captureTitleMetadata ? (
              <CaptureTitleMetadataPanel
                currentTitle={item.title}
                metadata={item.captureTitleMetadata}
              />
            ) : (
              <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs text-[var(--text-soft)]">
                尚未记录标题捕获信息；重新捕获会重新读取页面标题。
              </div>
            )}

            {item.captureErrorSummary ? (
              <div className="rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2 text-red-500">
                {item.captureErrorSummary}
              </div>
            ) : null}

            {item.captureToolCalls?.length ? (
              <div className="space-y-2">
                {item.captureToolCalls.map((toolCall) => (
                  <div
                    key={toolCall.id}
                    className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs text-[var(--text-soft)]"
                  >
                    <div className="flex flex-wrap items-center gap-3">
                      <span className="font-medium text-[var(--text-main)]">{toolCall.toolName}</span>
                      <span>状态：{toolCall.status}</span>
                      <span>退出码：{toolCall.exitCode ?? '—'}</span>
                      <span>版本：{toolCall.toolVersion ?? '—'}</span>
                    </div>
                    {toolCall.errorSummary ? <div className="mt-1 text-red-500">{toolCall.errorSummary}</div> : null}
                  </div>
                ))}
              </div>
            ) : null}

            {canManualImportCapture ? (
              <div className="space-y-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-xs font-medium text-[var(--text-main)]">手动导入补救</span>
                  <button
                    className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border-subtle)] px-2.5 py-1.5 text-xs text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                    disabled={isManualImporting}
                    type="button"
                    onClick={() => {
                      setIsManualImportOpen((current) => !current);
                      setManualImportError(null);
                    }}
                  >
                    <Upload size={13} />
                    <span>{isManualImportOpen ? '收起' : '导入文件'}</span>
                  </button>
                </div>
                {isManualImportOpen ? (
                  <form
                    className="grid gap-2"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void submitManualImport();
                    }}
                  >
                    <input
                      className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)] file:mr-3 file:rounded file:border-0 file:bg-[var(--accent-soft)] file:px-2.5 file:py-1.5 file:text-xs file:font-medium file:text-[var(--accent)]"
                      disabled={isManualImporting}
                      type="file"
                      accept=".md,.markdown,.html,.htm,text/markdown,text/html,application/xhtml+xml"
                      onChange={(event) => setManualImportFile(event.target.files?.[0] ?? null)}
                    />
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <label className="inline-flex items-center gap-2 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-2.5 py-1.5 text-xs text-[var(--text-muted)]">
                        <span>材料类型</span>
                        <select
                          className="bg-transparent text-[var(--text-main)] outline-none"
                          disabled={isManualImporting}
                          value={manualImportMaterialType}
                          onChange={(event) => setManualImportMaterialType(event.target.value as UploadMaterialType)}
                        >
                          <option value="singlefile_html">SingleFile HTML</option>
                          <option value="html">HTML</option>
                          <option value="markdown">Markdown</option>
                        </select>
                      </label>
                      <button
                        className="rounded-md bg-[var(--accent)] px-3 py-1.5 text-xs font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-45"
                        disabled={isManualImporting}
                        type="submit"
                      >
                        {isManualImporting ? '导入中...' : '导入'}
                      </button>
                    </div>
                    {manualImportError ? <div className="text-xs text-red-500">{manualImportError}</div> : null}
                  </form>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : (
          <div className="space-y-3 text-sm text-[var(--text-muted)]">
            <div>
              {item.feedItemId === null
                ? '尚未创建捕获运行记录。'
                : '这是旧版保存的 RSS item，尚未创建捕获运行记录。'}
            </div>
          </div>
        )}
      </CollapsibleKnowledgePanel>

      <CollapsibleKnowledgePanel
        actions={
          <>
            <ArchiveArtifactActionButton
              disabled={!primaryMarkdownArtifact?.previewUrl}
              icon={<ExternalLink size={14} />}
              label="打开核心"
              onClick={() => onOpenArtifactUrl(primaryMarkdownArtifact?.previewUrl ?? null)}
            />
            <ArchiveArtifactActionButton
              disabled={!primaryMarkdownArtifact?.downloadUrl}
              icon={<Download size={14} />}
              label="下载核心"
              onClick={() => onOpenArtifactUrl(primaryMarkdownArtifact?.downloadUrl ?? null)}
            />
          </>
        }
        icon={<FileArchive size={15} />}
        isCollapsed={collapsedKnowledgePanels.archive}
        summary={buildArchivePanelSummary(coreArchiveArtifacts, imageArchiveArtifacts, primaryMarkdownArtifact)}
        title="归档产物"
        onToggle={() => onTogglePanel('archive')}
      >

        {archiveStatusMessage ? (
          <div className="mb-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs leading-relaxed text-[var(--text-muted)]">
            {archiveStatusMessage}
          </div>
        ) : null}

        {archiveArtifacts.length ? (
          <div className="space-y-2">
            {coreArchiveArtifacts.map((artifact) => (
              <ArchiveArtifactRow
                key={buildArchiveArtifactKey(artifact)}
                artifact={artifact}
                isPreviewActive={markdownPreview?.artifactKey === buildArchiveArtifactKey(artifact)}
                onDownload={() => onOpenArtifactUrl(artifact.downloadUrl)}
                onOpen={() => onOpenArtifactUrl(artifact.previewUrl)}
                onPreview={() => {
                  onPreviewMarkdownArtifact(artifact);
                }}
              />
            ))}
          </div>
        ) : (
          <div className="text-sm text-[var(--text-muted)]">尚未返回归档产物清单。</div>
        )}

        {imageArchiveArtifacts.length ? (
          <ArchiveImagesSection
            artifacts={imageArchiveArtifacts}
            isCollapsed={collapsedKnowledgePanels.archiveImages}
            onDownload={(artifact) => onOpenArtifactUrl(artifact.downloadUrl)}
            onOpen={(artifact) => onOpenArtifactUrl(artifact.previewUrl)}
            onToggle={() => onTogglePanel('archiveImages')}
          />
        ) : null}

        {markdownPreview ? (
          <div className="mt-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
            <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-3 py-2 text-xs">
              <span className="font-medium text-[var(--text-main)]">{markdownPreview.title}</span>
              <button
                className="text-[var(--text-soft)] transition-colors hover:text-[var(--text-main)]"
                type="button"
                onClick={() => onCloseMarkdownPreview()}
              >
                关闭
              </button>
            </div>
            {markdownPreview.isLoading ? (
              <div className="px-3 py-4 text-sm text-[var(--text-muted)]">加载预览中...</div>
            ) : markdownPreview.error ? (
              <div className="px-3 py-4 text-sm text-red-500">{markdownPreview.error}</div>
            ) : (
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words px-3 py-3 font-mono text-xs leading-relaxed text-[var(--text-muted)]">
                {markdownPreview.content}
              </pre>
            )}
          </div>
        ) : null}
      </CollapsibleKnowledgePanel>

      <CollapsibleKnowledgePanel
        actions={
          <>
            {item.localMirrorStatus ? (
              <CaptureStatusBadge status={item.localMirrorStatus} kind="mirror" />
            ) : null}
            <button
              className="inline-flex items-center gap-1.5 rounded-md bg-[var(--accent)] px-3 py-1.5 text-xs font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-45"
              disabled={!hasMirrorExportableArtifacts || isMirrorReexportBlocked}
              type="button"
              onClick={() => {
                onReexportLocalMirror();
              }}
            >
              <RefreshCw size={14} />
              {isReexportingMirrorBusy ? '导出中...' : isCaptureRunning ? '捕获中...' : '重新导出'}
            </button>
          </>
        }
        icon={null}
        isCollapsed={collapsedKnowledgePanels.mirror}
        summary={buildLocalMirrorPanelSummary(item)}
        title="Local Mirror"
        onToggle={() => onTogglePanel('mirror')}
      >

        {item.localMirrorStatus ? (
          <div className="space-y-3 text-sm text-[var(--text-muted)]">
            <div className="flex flex-wrap gap-4">
              <span>Export：{item.latestLocalMirrorExportId ?? '—'}</span>
              <span>Start：{item.localMirrorStartedAt ? formatAbsoluteDate(item.localMirrorStartedAt) : '—'}</span>
              <span>Finish：{item.localMirrorFinishedAt ? formatAbsoluteDate(item.localMirrorFinishedAt) : '—'}</span>
              <span>Capture Run：{item.localMirrorSourceCaptureRunId ?? '—'}</span>
              <span>Files：{item.localMirrorWrittenFileCount ?? 0}</span>
            </div>
            {item.localMirrorErrorSummary ? (
              <div className="rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2 text-red-500">
                {item.localMirrorErrorSummary}
              </div>
            ) : null}
            {item.localMirrorPaths ? (
              <div className="space-y-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2">
                <MirrorPathRow label="KB Root" value={item.localMirrorPaths.knowledgeBaseRoot} />
                <MirrorPathRow label="Article" value={item.localMirrorPaths.articleDir} />
                <MirrorPathRow label="Raw" value={item.localMirrorPaths.rawMarkdown} />
                <MirrorPathRow label="Fit" value={item.localMirrorPaths.fitMarkdown} />
                <MirrorPathRow label="Fit Images" value={item.localMirrorPaths.fitImageReinsertedMarkdown} />
                <MirrorPathRow label="External Source Markdown" value={item.localMirrorPaths.uploadedMarkdown} />
                <MirrorPathRow label="Manifest" value={item.localMirrorPaths.manifest} />
                <MirrorPathRow label="Assets" value={item.localMirrorPaths.markdownAssetsDir} />
                <MirrorPathRow label="SingleFile" value={item.localMirrorPaths.singlefileHtml} />
                {item.localMirrorPaths.sourceMaterial ? (
                  <MirrorPathRow label="Source Material" value={item.localMirrorPaths.sourceMaterial} />
                ) : null}
              </div>
            ) : null}
          </div>
        ) : (
          <div className="text-sm text-[var(--text-muted)]">
            No local mirror export has been recorded yet.
          </div>
        )}
        {mirrorError ? <div className="mt-3 text-xs text-red-500">{mirrorError}</div> : null}
      </CollapsibleKnowledgePanel>

      <div className="mt-6">
        <KnowledgeMarkdownReader
          archiveArtifacts={item.archiveArtifacts}
          fallbackHtml={item.content}
          itemId={item.id}
        />
      </div>

    </>
  );
}

export function readCollapsedKnowledgePanels(): Record<KnowledgeDetailPanelKey, boolean> {
  if (typeof window === 'undefined') {
    return {...DEFAULT_KNOWLEDGE_PANEL_COLLAPSED};
  }

  try {
    const raw = window.localStorage.getItem(KNOWLEDGE_PANEL_COLLAPSE_STORAGE_KEY);
    if (!raw) {
      return {...DEFAULT_KNOWLEDGE_PANEL_COLLAPSED};
    }
    const parsed = JSON.parse(raw) as Partial<Record<KnowledgeDetailPanelKey, unknown>>;
    return {
      archive: typeof parsed.archive === 'boolean' ? parsed.archive : DEFAULT_KNOWLEDGE_PANEL_COLLAPSED.archive,
      archiveImages:
        typeof parsed.archiveImages === 'boolean'
          ? parsed.archiveImages
          : DEFAULT_KNOWLEDGE_PANEL_COLLAPSED.archiveImages,
      capture: typeof parsed.capture === 'boolean' ? parsed.capture : DEFAULT_KNOWLEDGE_PANEL_COLLAPSED.capture,
      mirror: typeof parsed.mirror === 'boolean' ? parsed.mirror : DEFAULT_KNOWLEDGE_PANEL_COLLAPSED.mirror,
    };
  } catch {
    return {...DEFAULT_KNOWLEDGE_PANEL_COLLAPSED};
  }
}

function CollapsibleKnowledgePanel({
  actions,
  children,
  icon,
  isCollapsed,
  summary,
  title,
  onToggle,
}: {
  actions: ReactNode;
  children: ReactNode;
  icon: ReactNode;
  isCollapsed: boolean;
  summary: ReactNode;
  title: string;
  onToggle: () => void;
}) {
  return (
    <section className="mt-4 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <button
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          type="button"
          onClick={onToggle}
        >
          <ChevronDown
            className={`shrink-0 text-[var(--text-soft)] transition-transform ${isCollapsed ? '-rotate-90' : ''}`}
            size={16}
          />
          {icon ? <span className="shrink-0 text-[var(--text-soft)]">{icon}</span> : null}
          <span className="shrink-0 text-xs font-semibold uppercase tracking-[0.18em] text-[var(--text-soft)]">
            {title}
          </span>
          {summary ? (
            <span className="flex min-w-0 flex-wrap items-center gap-2 text-xs text-[var(--text-muted)]">
              {summary}
            </span>
          ) : null}
        </button>
        {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {isCollapsed ? null : <div className="mt-3">{children}</div>}
    </section>
  );
}

function MirrorPathRow({label, value}: {label: string; value: string}) {
  return (
    <div className="grid gap-1 text-xs sm:grid-cols-[96px_minmax(0,1fr)]">
      <span className="font-medium text-[var(--text-soft)]">{label}</span>
      <code className="break-all font-mono text-[var(--text-muted)]">{value}</code>
    </div>
  );
}

function getProcessingStatusLabel(status: string | null | undefined) {
  switch (status) {
    case 'not_applicable':
      return '无需处理';
    case 'pending':
      return '处理中';
    case 'succeeded':
      return '处理完成';
    case 'failed':
      return '处理失败';
    default:
      return status?.trim() || '未知状态';
  }
}

export function UploadedMaterialMetadata({
  item,
  onOpenArtifactUrl,
}: {
  item: FeedItem;
  onOpenArtifactUrl: (url: string | null) => void;
}) {
  const sourceArtifact = item.sourceMaterialArtifact;
  return (
    <div className="mb-4 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-4 py-3 text-sm text-[var(--text-muted)]">
      <div className="mb-2 flex flex-wrap gap-2 text-xs">
        <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-2.5 py-1">
          {getOriginTypeLabel(item.originType)}
        </span>
        <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-2.5 py-1">
          {getProcessingStatusLabel(item.processingStatus)}
        </span>
        {item.mimeType ? (
          <span className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-2.5 py-1">
            {item.mimeType}
          </span>
        ) : null}
      </div>
      <div className="grid gap-1.5">
        {item.originalFilename ? <div>原始文件：{item.originalFilename}</div> : null}
        {item.processingError ? <div className="text-red-500">处理错误：{item.processingError}</div> : null}
        {sourceArtifact ? (
          <div className="flex flex-wrap gap-2 pt-1">
            <ArchiveArtifactActionButton
              disabled={!sourceArtifact.previewUrl}
              icon={<ExternalLink size={14} />}
              label="打开原始文件"
              onClick={() => onOpenArtifactUrl(sourceArtifact.previewUrl)}
            />
            <ArchiveArtifactActionButton
              disabled={!sourceArtifact.downloadUrl}
              icon={<Download size={14} />}
              label="下载原始文件"
              onClick={() => onOpenArtifactUrl(sourceArtifact.downloadUrl)}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}

function CaptureTitleMetadataPanel({
  currentTitle,
  metadata,
}: {
  currentTitle: string;
  metadata: NonNullable<FeedItem['captureTitleMetadata']>;
}) {
  return (
    <div className="space-y-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs text-[var(--text-soft)]">
      <div className="flex flex-wrap gap-3">
        <span className="font-medium text-[var(--text-main)]">标题捕获</span>
        <span>来源：{getCaptureTitleSourceLabel(metadata.titleSource)}</span>
        <span>用户标题：{metadata.userTitlePresent === null ? '未记录' : metadata.userTitlePresent ? '是' : '否'}</span>
        <span>本次更新：{metadata.titleUpdated ? '已更新展示标题' : '未覆盖展示标题'}</span>
      </div>
      <div className="grid gap-1 sm:grid-cols-[96px_minmax(0,1fr)]">
        <span>页面标题</span>
        <span className="break-words text-[var(--text-muted)]">{metadata.capturedPageTitle ?? '未捕获到页面标题'}</span>
      </div>
      <div className="grid gap-1 sm:grid-cols-[96px_minmax(0,1fr)]">
        <span>展示标题</span>
        <span className="break-words text-[var(--text-muted)]">{metadata.savedItemTitleAfter ?? currentTitle}</span>
      </div>
      <div className="flex flex-wrap gap-3">
        <span>SingleFile：{metadata.singlefileTitle ?? '—'}</span>
        <span>Crawl4AI：{metadata.crawl4aiTitle ?? '—'}</span>
        {metadata.feedTitle ? <span>Feed：{metadata.feedTitle}</span> : null}
      </div>
      {metadata.capturedAt ? <div>捕获时间：{formatAbsoluteDate(metadata.capturedAt)}</div> : null}
    </div>
  );
}

function CaptureProgressPanel({operation}: {operation: CaptureOperation}) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (operation.status !== 'running') {
      return;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [operation.startedAt, operation.status]);

  const elapsedSeconds = Math.max(0, Math.floor((now - new Date(operation.startedAt).getTime()) / 1000));
  const stage = normalizeCaptureProgressStage(operation.detail, operation.message);
  const phase = getCaptureProgressPhase(stage, elapsedSeconds, operation.operationKind ?? null);
  const progressPercent =
    operation.status === 'running'
      ? getCaptureProgressPercent(stage, elapsedSeconds, operation.operationKind ?? null)
      : operation.status === 'failed'
        ? 100
        : 0;

  return (
    <div
      className={`space-y-2 rounded-lg border px-3 py-2 text-xs ${
        operation.status === 'failed'
          ? 'border-red-500/20 bg-red-500/5 text-red-500'
          : 'border-[var(--border-subtle)] bg-[var(--bg-panel)] text-[var(--text-soft)]'
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium text-[var(--text-main)]">{operation.status === 'failed' ? '捕获失败' : '捕获中'}</span>
        <span>已运行 {formatElapsedDuration(elapsedSeconds)}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-[var(--border-subtle)]">
        <div
          className={`h-full rounded-full transition-all duration-500 ${
            operation.status === 'failed' ? 'bg-red-500' : 'bg-[var(--accent)]'
          }`}
          style={{width: `${progressPercent}%`}}
        />
      </div>
      <div className="font-medium text-[var(--text-soft)]">{phase.stageLabel}</div>
      <div>{operation.status === 'failed' ? operation.error ?? '捕获请求失败。' : phase.detail}</div>
    </div>
  );
}

function normalizeCaptureProgressStage(stage: string | null, message: string | null) {
  if (stage && !['idle', 'queued', 'running'].includes(stage)) {
    return stage;
  }

  const normalizedMessage = (message ?? '').toLowerCase();
  if (normalizedMessage.includes('upload')) {
    return 'processing_upload';
  }
  if (normalizedMessage.includes('singlefile')) {
    return 'capture_singlefile';
  }
  if (normalizedMessage.includes('crawl4ai')) {
    return 'capture_crawl4ai';
  }
  if (normalizedMessage.includes('finaliz') || normalizedMessage.includes('归档') || normalizedMessage.includes('标题')) {
    return 'capture_finalize';
  }
  if (normalizedMessage.includes('mirror')) {
    return 'mirror_export';
  }
  if (normalizedMessage.includes('stop') || normalizedMessage.includes('停止')) {
    return 'stopping';
  }
  if (normalizedMessage.includes('complete') || normalizedMessage.includes('完成')) {
    return 'completed';
  }
  return stage;
}

function getCaptureProgressPhase(stage: string | null, elapsedSeconds: number, operationKind: string | null) {
  const isUpload = operationKind === 'uploaded_material' || operationKind === 'manual_import';
  switch (stage) {
    case 'processing_upload':
      return {
        stageLabel: '阶段 1/2 上传处理',
        minorStage: '处理上传材料',
        detail: '正在校验文件、写入原始材料，并生成知识库正文快照。',
      };
    case 'capture_singlefile':
      return {
        stageLabel: '阶段 1/4 SingleFile 页面快照',
        minorStage: '生成 SingleFile 快照',
        detail: '正在通过 SingleFile 扩展保存自包含 HTML；复杂页面可能需要几十秒。',
      };
    case 'capture_crawl4ai':
      return {
        stageLabel: '阶段 2/4 Crawl4AI 正文提取',
        minorStage: '生成 Crawl4AI Markdown',
        detail: '正在提取正文 Markdown 并处理正文图片；后端进入下一步后进度会继续推进。',
      };
    case 'capture_finalize':
      return {
        stageLabel: '阶段 3/4 归档收尾',
        minorStage: '整理捕获结果',
        detail: '正在写入标题元数据、图片引用和归档快照。',
      };
    case 'mirror_export':
      return {
        stageLabel: isUpload ? '阶段 2/2 Local Mirror 导出' : '阶段 4/4 Local Mirror 导出',
        minorStage: '导出 Local Mirror',
        detail: '正在导出 KnowledgeBase Local Mirror 文件。',
      };
    case 'stopping':
      return {stageLabel: '停止中 等待当前步骤结束', minorStage: '正在停止捕获', detail: '已请求停止，等待当前后端步骤结束。'};
    case 'completed':
      return {stageLabel: '已完成 捕获处理完成', minorStage: '捕获处理完成', detail: '捕获处理已完成。'};
    default:
      if (elapsedSeconds < 8) {
        return {
          stageLabel: '准备中 等待后端 worker',
          minorStage: '准备捕获运行',
          detail: '请求已发送，正在等待后端 worker 接手。',
        };
      }
      return {
        stageLabel: isUpload ? '阶段 1/2 上传处理' : '阶段 1/4 捕获启动',
        minorStage: '捕获处理中',
        detail: '后端正在执行当前捕获步骤；暂时没有可识别的阶段事件。',
      };
  }
}

function getCaptureProgressPercent(stage: string | null, elapsedSeconds: number, operationKind: string | null) {
  const isUpload = operationKind === 'uploaded_material' || operationKind === 'manual_import';
  switch (stage) {
    case 'processing_upload':
      return boundedStageProgress(elapsedSeconds, 16, 58, 90);
    case 'capture_singlefile':
      return boundedStageProgress(elapsedSeconds, 10, 28, 120);
    case 'capture_crawl4ai':
      return boundedStageProgress(elapsedSeconds, 34, 54, 120);
    case 'capture_finalize':
      return 70;
    case 'mirror_export':
      return isUpload
        ? boundedStageProgress(elapsedSeconds, 74, 92, 90)
        : boundedStageProgress(elapsedSeconds, 84, 94, 90);
    case 'stopping':
      return 92;
    default:
      return elapsedSeconds < 8 ? 4 : 8;
  }
}

function boundedStageProgress(elapsedSeconds: number, start: number, end: number, secondsToCap: number) {
  const ratio = Math.min(1, Math.max(0, elapsedSeconds) / secondsToCap);
  return Math.round(start + (end - start) * ratio);
}

function formatElapsedDuration(totalSeconds: number) {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes <= 0) {
    return `${seconds}s`;
  }
  return `${minutes}m ${seconds.toString().padStart(2, '0')}s`;
}

function getCaptureTitleSourceLabel(source: string | null) {
  switch (source) {
    case 'user_title':
      return '用户标题';
    case 'captured_page_title':
      return '页面标题';
    case 'feed_title':
      return 'Feed 标题';
    case 'url':
      return 'URL';
    case 'existing_title':
      return '既有标题';
    default:
      return '未记录';
  }
}

function ArchiveArtifactRow({
  artifact,
  isPreviewActive,
  onDownload,
  onOpen,
  onPreview,
}: {
  artifact: ArchiveArtifact;
  isPreviewActive: boolean;
  onDownload: () => void;
  onOpen: () => void;
  onPreview: () => void;
}) {
  const isAvailable = artifact.status === 'available';
  const isMarkdown = isMarkdownArchiveArtifact(artifact);
  const label = getArchiveArtifactLabel(artifact);

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          {isMarkdown ? <FileText size={15} className="text-[var(--text-soft)]" /> : <FileArchive size={15} className="text-[var(--text-soft)]" />}
          <span className="font-medium text-[var(--text-main)]">{label}</span>
          <span
            className={`rounded-full px-2 py-0.5 text-[11px] ${
              isAvailable
                ? 'bg-emerald-500/10 text-emerald-600'
                : 'bg-red-500/10 text-red-500'
            }`}
          >
            {isAvailable ? '可用' : '缺失'}
          </span>
        </div>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-[var(--text-soft)]">
          <span>{artifact.mimeType ?? '—'}</span>
          <span>{formatArtifactBytes(artifact.byteSize)}</span>
          <span>Run：{artifact.captureRunId ?? '—'}</span>
          <span>{artifact.createdAt ? formatAbsoluteDate(artifact.createdAt) : '—'}</span>
        </div>
      </div>

      <div className="flex shrink-0 flex-wrap items-center gap-2">
        {isMarkdown ? (
          <ArchiveArtifactActionButton
            disabled={!isAvailable || !artifact.previewUrl}
            icon={<Eye size={14} />}
            label={isPreviewActive ? '预览中' : '预览'}
            onClick={onPreview}
          />
        ) : null}
        <ArchiveArtifactActionButton
          disabled={!isAvailable || !artifact.previewUrl}
          icon={<ExternalLink size={14} />}
          label="打开"
          onClick={onOpen}
        />
        <ArchiveArtifactActionButton
          disabled={!isAvailable || !artifact.downloadUrl}
          icon={<Download size={14} />}
          label="下载"
          onClick={onDownload}
        />
      </div>
    </div>
  );
}

function ArchiveImagesSection({
  artifacts,
  isCollapsed,
  onDownload,
  onOpen,
  onToggle,
}: {
  artifacts: ArchiveArtifact[];
  isCollapsed: boolean;
  onDownload: (artifact: ArchiveArtifact) => void;
  onOpen: (artifact: ArchiveArtifact) => void;
  onToggle: () => void;
}) {
  const availableArtifacts = artifacts.filter((artifact) => artifact.status === 'available');
  const firstAvailableArtifact = availableArtifacts[0] ?? null;

  return (
    <div className="mt-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <button className="flex min-w-0 flex-1 items-center gap-2 text-left" type="button" onClick={onToggle}>
          <ChevronDown
            className={`shrink-0 text-[var(--text-soft)] transition-transform ${isCollapsed ? '-rotate-90' : ''}`}
            size={16}
          />
          <ImageIcon className="shrink-0 text-[var(--text-soft)]" size={15} />
          <span className="text-xs font-semibold uppercase tracking-[0.18em] text-[var(--text-soft)]">
            已保存图片
          </span>
          <span className="text-xs text-[var(--text-muted)]">
            {availableArtifacts.length}/{artifacts.length} 可用
          </span>
        </button>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <ArchiveArtifactActionButton
            disabled={!firstAvailableArtifact?.previewUrl}
            icon={<ExternalLink size={14} />}
            label="打开资产"
            onClick={() => {
              if (firstAvailableArtifact) {
                onOpen(firstAvailableArtifact);
              }
            }}
          />
          <ArchiveArtifactActionButton
            disabled={false}
            icon={<ChevronDown className={isCollapsed ? '-rotate-90' : ''} size={14} />}
            label={isCollapsed ? '展开' : '收起'}
            onClick={onToggle}
          />
        </div>
      </div>

      {isCollapsed ? null : (
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          {artifacts.map((artifact) => (
            <ArchiveImageTile
              key={buildArchiveArtifactKey(artifact)}
              artifact={artifact}
              onDownload={() => onDownload(artifact)}
              onOpen={() => onOpen(artifact)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ArchiveImageTile({
  artifact,
  onDownload,
  onOpen,
}: {
  artifact: ArchiveArtifact;
  onDownload: () => void;
  onOpen: () => void;
}) {
  return (
    <div className="overflow-hidden rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
      {artifact.previewUrl ? (
        <button className="block w-full bg-[var(--bg-panel-strong)]" type="button" onClick={onOpen}>
          <img
            alt={artifact.logicalName ?? 'Archived page image'}
            className="h-32 w-full object-contain"
            loading="lazy"
            src={artifact.previewUrl}
          />
        </button>
      ) : (
        <div className="flex h-32 items-center justify-center bg-[var(--bg-panel-strong)] text-[var(--text-soft)]">
          <ImageIcon size={24} />
        </div>
      )}
      <div className="space-y-2 px-3 py-2">
        <div className="flex items-center gap-2 text-sm font-medium text-[var(--text-main)]">
          <ImageIcon size={15} className="text-[var(--text-soft)]" />
          <span className="truncate">{artifact.logicalName ?? 'Page image'}</span>
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-[var(--text-soft)]">
          <span>{artifact.mimeType ?? '—'}</span>
          <span>{formatArtifactBytes(artifact.byteSize)}</span>
        </div>
        <div className="flex justify-end gap-2">
          <ArchiveArtifactActionButton
            disabled={!artifact.previewUrl}
            icon={<ExternalLink size={14} />}
            label="打开"
            onClick={onOpen}
          />
          <ArchiveArtifactActionButton
            disabled={!artifact.downloadUrl}
            icon={<Download size={14} />}
            label="下载"
            onClick={onDownload}
          />
        </div>
      </div>
    </div>
  );
}

function ArchiveArtifactActionButton({
  disabled,
  icon,
  label,
  onClick,
}: {
  disabled: boolean;
  icon: ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border-subtle)] px-2.5 py-1.5 text-xs font-medium text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-45"
      disabled={disabled}
      type="button"
      onClick={onClick}
    >
      {icon}
      {label}
    </button>
  );
}

function buildArchivePanelSummary(
  coreArtifacts: ArchiveArtifact[],
  imageArtifacts: ArchiveArtifact[],
  primaryMarkdownArtifact: ArchiveArtifact | null,
) {
  const availableCoreCount = coreArtifacts.filter((artifact) => artifact.status === 'available').length;
  const availableImageCount = imageArtifacts.filter((artifact) => artifact.status === 'available').length;
  const latestCoreArtifact = primaryMarkdownArtifact ?? coreArtifacts.find((artifact) => artifact.status === 'available') ?? null;
  const coreSummary = coreArtifacts.length ? `${availableCoreCount}/${coreArtifacts.length} 核心产物` : '无核心产物';
  const imageSummary = `${availableImageCount}/${imageArtifacts.length} 图片`;
  const runSummary = latestCoreArtifact?.captureRunId ? `运行 ${latestCoreArtifact.captureRunId}` : '运行 —';
  const timeSummary = latestCoreArtifact?.createdAt ? formatAbsoluteDate(latestCoreArtifact.createdAt) : '时间 —';

  return (
    <>
      <span>{coreSummary}</span>
      <span>{imageSummary}</span>
      <span>{runSummary}</span>
      <span>{timeSummary}</span>
    </>
  );
}

function buildCapturePanelSummary(item: FeedItem) {
  return (
    <>
      {item.captureStatus ? <CaptureStatusBadge status={item.captureStatus} /> : <span>无捕获记录</span>}
      <span>Run {item.latestCaptureRunId ?? '—'}</span>
      <span>{formatLatestTime(item.captureFinishedAt, item.captureStartedAt)}</span>
    </>
  );
}

function buildLocalMirrorPanelSummary(item: FeedItem) {
  if (!item.localMirrorStatus) {
    return '尚无导出记录';
  }
  return (
    <>
      <span>Export {item.latestLocalMirrorExportId ?? '—'}</span>
      <span>{item.localMirrorWrittenFileCount ?? 0} files</span>
      <span>{formatLatestTime(item.localMirrorFinishedAt, item.localMirrorStartedAt)}</span>
    </>
  );
}

function formatLatestTime(finishedAt: string | null | undefined, startedAt: string | null | undefined) {
  const latestTime = finishedAt ?? startedAt;
  return latestTime ? formatAbsoluteDate(latestTime) : '时间 —';
}

export function buildArchiveStatusMessage(item: FeedItem, coreArtifacts: ArchiveArtifact[]) {
  const missingLabels = coreArtifacts
    .filter((artifact) => artifact.status === 'unavailable')
    .map((artifact) => getArchiveArtifactLabel(artifact));
  const hasAvailableArchive = coreArtifacts.some((artifact) => artifact.status === 'available');
  const messages: string[] = [];

  if (missingLabels.length && (item.captureStatus === 'partial_failed' || item.captureStatus === 'failed')) {
    messages.push(`捕获部分失败：缺少 ${missingLabels.join('、')}。已入库的归档产物仍可打开或下载。`);
  } else if (missingLabels.length) {
    messages.push(`归档产物不完整：缺少 ${missingLabels.join('、')}。`);
  }

  if (
    hasAvailableArchive &&
    (item.localMirrorStatus === 'failed' || item.localMirrorStatus === 'partial_failed')
  ) {
    messages.push('数据库归档产物可用，但本地 mirror 导出失败；可单独重新导出。');
  }

  if (!messages.length && coreArtifacts.length && missingLabels.length === 0) {
    messages.push('数据库归档产物已可用；Local Mirror 是从数据库派生的本地导出，可单独重导。');
  }

  return messages.join(' ');
}

function getCaptureActionLabel(captureStatus: FeedItem['captureStatus'], isRunning: boolean) {
  if (isRunning) {
    return '捕获中...';
  }
  if (!captureStatus) {
    return '开始捕获';
  }
  if (captureStatus === 'failed' || captureStatus === 'partial_failed') {
    return '重试捕获';
  }
  return '重新捕获';
}

function isMarkdownArchiveArtifact(artifact: ArchiveArtifact) {
  return (
    artifact.artifactKind === 'uploaded_markdown_v1' ||
    artifact.artifactKind === 'crawl4ai_raw_markdown_v1' ||
    artifact.artifactKind === 'crawl4ai_fit_markdown_v1' ||
    artifact.artifactKind === 'crawl4ai_fit_image_reinserted_markdown_v1'
  );
}

export function getArchiveArtifactLabel(artifact: ArchiveArtifact) {
  switch (artifact.artifactKind) {
    case 'uploaded_source_material_v1':
      return artifact.logicalName ? `External Source File · ${artifact.logicalName}` : 'External Source File';
    case 'uploaded_markdown_v1':
      return 'External Source Markdown';
    case 'singlefile_html_v1':
      return 'SingleFile HTML';
    case 'crawl4ai_raw_markdown_v1':
      return 'Crawl4AI Raw Markdown';
    case 'crawl4ai_fit_markdown_v1':
      return 'Crawl4AI Fit Markdown';
    case 'crawl4ai_fit_image_reinserted_markdown_v1':
      return 'Crawl4AI Fit Markdown + Images';
    case 'page_image_v1':
      return artifact.logicalName ? `Page Image · ${artifact.logicalName}` : 'Page Image';
    default:
      return artifact.artifactKind;
  }
}

export function isUploadSavedItem(item: FeedItem) {
  return (
    item.originType === 'markdown_upload' ||
    item.originType === 'html_upload' ||
    item.originType === 'singlefile_html_upload'
  );
}

function getOriginTypeLabel(originType: string | null | undefined) {
  switch (originType) {
    case 'markdown_upload':
      return 'External Source · Markdown';
    case 'html_upload':
      return 'External Source · HTML';
    case 'singlefile_html_upload':
      return 'External Source · SingleFile HTML';
    default:
      return originType ?? '未知来源';
  }
}

function formatArtifactBytes(byteSize: number | null) {
  if (byteSize === null) {
    return '—';
  }
  if (byteSize < 1024) {
    return `${byteSize} B`;
  }
  if (byteSize < 1024 * 1024) {
    return `${(byteSize / 1024).toFixed(1)} KB`;
  }
  return `${(byteSize / (1024 * 1024)).toFixed(1)} MB`;
}
