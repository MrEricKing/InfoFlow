import {useEffect, useMemo, useState} from 'react';
import {AISettingsPanel} from '@/components/ai-settings-panel';
import {InsightEngineSettingsPanel} from '@/components/insight-engine-settings-panel';
import {RsshubSettingsPanel} from '@/components/rsshub-settings-panel';
import {SourcesWorkspace} from '@/components/sources-workspace';
import {StorageSettingsPanel} from '@/components/storage-settings-panel';
import type {
  AppOperationDto,
  FeedItemInsightArtifactBatchOperationState,
  SourceDto,
  SourceSyncOperationState,
  SourceSyncProgressState,
} from '@/types/source';
import {getRuntimeInfo, type RuntimeInfo} from '@/services/system-service';
import packageJson from '../../package.json';

type SettingsSectionId = 'storage' | 'ai' | 'insight-engine' | 'sources' | 'import' | 'system';
const APP_VERSION = packageJson.version;

const SETTINGS_SECTIONS: Array<{
  id: SettingsSectionId;
  title: string;
  description: string;
}> = [
  {
    id: 'storage',
    title: '存储与导出',
    description: 'KnowledgeBase、LLM Wiki 路径与本地归档输出位置。',
  },
  {
    id: 'ai',
    title: 'AI Provider',
    description: '配置 provider、API key、base URL、model，并测试连接。',
  },
  {
    id: 'insight-engine',
    title: 'Insight Engine',
    description: '管理自动低优先级归档、Insight Profile、Evidence Package 和洞察请求诊断。',
  },
  {
    id: 'sources',
    title: '订阅源管理',
    description: '查看、刷新、编辑已有 source，并按分类检查 source 列表。',
  },
  {
    id: 'import',
    title: '导入 / OPML',
    description: '预览新增 feed、创建 source，或上传 OPML 批量导入。',
  },
  {
    id: 'system',
    title: '系统与诊断',
    description: 'RSSHub 运行参数与系统级调试入口。',
  },
];

interface SettingsWorkspaceProps {
  searchQuery: string;
  sources: SourceDto[];
  feedItemArtifactRefreshVersion: number;
  insightEngineArtifactOperation: FeedItemInsightArtifactBatchOperationState | null;
  insightEngineArtifactProgress: SourceSyncProgressState | null;
  isInsightEngineArtifactStopRequesting: boolean;
  isSourceSyncStopRequesting: boolean;
  sourceSyncBusyReason: string | null;
  sourceSyncMessage: string | null;
  sourceSyncOperation: SourceSyncOperationState | null;
  sourceSyncProgress: SourceSyncProgressState | null;
  onFeedItemInsightArtifactOperation: (feedItemId: number, mode: 'rebuild' | 'build_missing') => Promise<AppOperationDto>;
  onOpenFeedItem: (itemId: number) => void;
  onSourcesSync: (options?: {scope?: 'current_view' | 'all'}) => Promise<void>;
  onSourceIdsSync: (sourceIds: number[]) => Promise<void>;
  onSourceSync: (source: SourceDto) => Promise<void>;
  onSourcesSyncStop: () => Promise<void>;
  onInsightEngineArtifactsStop: () => Promise<void>;
  onSourcesChanged: () => Promise<SourceDto[]>;
  onInsightChannelsChanged?: () => void | Promise<void>;
  onBackendOperation: (operation: AppOperationDto) => void;
  onSourceEdit: (source: SourceDto) => void;
}

export function SettingsWorkspace({
  searchQuery,
  sources,
  feedItemArtifactRefreshVersion,
  insightEngineArtifactOperation,
  insightEngineArtifactProgress,
  isInsightEngineArtifactStopRequesting,
  isSourceSyncStopRequesting,
  sourceSyncBusyReason,
  sourceSyncMessage,
  sourceSyncOperation,
  sourceSyncProgress,
  onFeedItemInsightArtifactOperation,
  onOpenFeedItem,
  onSourcesSync,
  onSourceIdsSync,
  onSourceSync,
  onSourcesSyncStop,
  onInsightEngineArtifactsStop,
  onSourcesChanged,
  onInsightChannelsChanged,
  onBackendOperation,
  onSourceEdit,
}: SettingsWorkspaceProps) {
  const [activeSection, setActiveSection] = useState<SettingsSectionId>('storage');
  const [runtimeInfo, setRuntimeInfo] = useState<RuntimeInfo | null>(null);
  const [runtimeInfoFailed, setRuntimeInfoFailed] = useState(false);
  const activeSectionConfig = SETTINGS_SECTIONS.find((section) => section.id === activeSection) ?? SETTINGS_SECTIONS[0];
  const sourceCategoryCount = useMemo(
    () => new Set(sources.map((source) => source.folder_path?.trim() || '未分类')).size,
    [sources],
  );
  const displayedVersion = runtimeInfo?.appVersion ?? APP_VERSION;
  const gitHashDisplay = runtimeInfo?.gitCommitShort ?? runtimeInfo?.gitCommitDisplay ?? null;
  const gitHashText =
    gitHashDisplay ? `Git Hash ${gitHashDisplay}` : runtimeInfoFailed || runtimeInfo ? 'Git Hash —' : 'Git Hash ...';

  useEffect(() => {
    let cancelled = false;

    const loadRuntimeInfo = () => {
      getRuntimeInfo()
        .then((info) => {
          if (!cancelled) {
            setRuntimeInfo(info);
            setRuntimeInfoFailed(false);
          }
        })
        .catch(() => {
          if (!cancelled) {
            setRuntimeInfoFailed(true);
          }
        });
    };

    loadRuntimeInfo();
    window.addEventListener('focus', loadRuntimeInfo);
    return () => {
      cancelled = true;
      window.removeEventListener('focus', loadRuntimeInfo);
    };
  }, []);

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="grid h-full min-h-0 grid-cols-[220px_1fr] overflow-hidden">
        <aside className="panel flex h-full flex-col border-r border-[var(--border-subtle)] px-4 py-5">
          <div className="text-sm font-semibold text-[var(--text-main)]">设置</div>
          <nav className="mt-4 space-y-1 text-sm text-[var(--text-muted)]" aria-label="设置分类">
            {SETTINGS_SECTIONS.map((section) => (
              <button
                key={section.id}
                aria-current={activeSection === section.id ? 'page' : undefined}
                className={`w-full rounded-md px-3 py-2 text-left transition-colors ${
                  activeSection === section.id
                    ? 'bg-[var(--bg-panel-strong)] font-medium text-[var(--text-main)]'
                    : 'hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]'
                }`}
                onClick={() => setActiveSection(section.id)}
                type="button"
              >
                <span>{section.title}</span>
                {section.id === 'sources' ? (
                  <span className="mt-1 block text-xs font-normal text-[var(--text-soft)]">
                    {sources.length} 个 source · {sourceCategoryCount} 类
                  </span>
                ) : null}
              </button>
            ))}
          </nav>
          <div className="mt-auto border-t border-[var(--border-subtle)] pt-4 text-xs text-[var(--text-soft)]">
            <div className="font-medium text-[var(--text-muted)]">当前版本</div>
            <div className="mt-1 font-mono">v{displayedVersion}</div>
            <div
              className="mt-1 font-mono"
              title={
                runtimeInfo?.gitCommit
                  ? `${runtimeInfo.gitCommit}${runtimeInfo.gitDirty ? ' (dirty)' : ''}`
                  : runtimeInfoFailed
                    ? 'git hash unavailable'
                    : runtimeInfo
                      ? 'git hash unavailable from runtime'
                      : 'loading git hash'
              }
            >
              {gitHashText}
            </div>
            {runtimeInfo?.gitDirty ? (
              <div className="mt-1 text-[var(--warning)]">
                含未提交改动
                {runtimeInfo.gitDirtyFileCount ? ` · ${runtimeInfo.gitDirtyFileCount}` : ''}
              </div>
            ) : null}
          </div>
        </aside>
        <main className="panel flex min-w-0 flex-col overflow-hidden">
          <SectionHeader
            constrainContent={activeSection === 'ai' || activeSection === 'insight-engine'}
            title={activeSectionConfig.title}
            description={activeSectionConfig.description}
          />
          <div className="panel min-h-0 flex-1 overflow-hidden">
            {activeSection === 'storage' ? (
              <div className="panel h-full overflow-y-auto">
                <StorageSettingsPanel />
              </div>
            ) : null}
            {activeSection === 'ai' ? (
              <div className="panel h-full overflow-y-auto">
                <AISettingsPanel />
              </div>
            ) : null}
            {activeSection === 'insight-engine' ? (
              <div className="panel h-full overflow-y-auto">
                <InsightEngineSettingsPanel
                  sources={sources}
                  operationRefreshSignal={feedItemArtifactRefreshVersion}
                  onChannelsChanged={onInsightChannelsChanged}
                  onBackendOperation={onBackendOperation}
                  onFeedItemInsightArtifactOperation={onFeedItemInsightArtifactOperation}
                  onOpenFeedItem={onOpenFeedItem}
                  onSourcesChanged={onSourcesChanged}
                />
              </div>
            ) : null}
            {activeSection === 'sources' ? (
              <SourcesWorkspace
                mode="manage"
                externalSources={sources}
                searchQuery={searchQuery}
                insightEngineArtifactOperation={insightEngineArtifactOperation}
                insightEngineArtifactProgress={insightEngineArtifactProgress}
                isInsightEngineArtifactStopRequesting={isInsightEngineArtifactStopRequesting}
                isSourceSyncStopRequesting={isSourceSyncStopRequesting}
                sourceSyncBusyReason={sourceSyncBusyReason}
                sourceSyncMessage={sourceSyncMessage}
                sourceSyncOperation={sourceSyncOperation}
                sourceSyncProgress={sourceSyncProgress}
                onCreateRequest={() => setActiveSection('import')}
                onSourceIdsSync={onSourceIdsSync}
                onSourceSync={onSourceSync}
                onSourcesSync={() => onSourcesSync({scope: 'all'})}
                onSourcesSyncStop={onSourcesSyncStop}
                onInsightEngineArtifactsStop={onInsightEngineArtifactsStop}
                onSourcesChanged={onSourcesChanged}
                onSourceEdit={onSourceEdit}
              />
            ) : null}
            {activeSection === 'import' ? (
              <SourcesWorkspace
                mode="import"
                externalSources={sources}
                searchQuery={searchQuery}
                insightEngineArtifactOperation={insightEngineArtifactOperation}
                insightEngineArtifactProgress={insightEngineArtifactProgress}
                isInsightEngineArtifactStopRequesting={isInsightEngineArtifactStopRequesting}
                isSourceSyncStopRequesting={isSourceSyncStopRequesting}
                sourceSyncBusyReason={sourceSyncBusyReason}
                sourceSyncMessage={sourceSyncMessage}
                sourceSyncOperation={sourceSyncOperation}
                sourceSyncProgress={sourceSyncProgress}
                onSourceIdsSync={onSourceIdsSync}
                onSourceSync={onSourceSync}
                onSourcesSync={() => onSourcesSync({scope: 'all'})}
                onSourcesSyncStop={onSourcesSyncStop}
                onInsightEngineArtifactsStop={onInsightEngineArtifactsStop}
                onSourcesChanged={onSourcesChanged}
                onSourceEdit={onSourceEdit}
              />
            ) : null}
            {activeSection === 'system' ? (
              <div className="panel h-full overflow-y-auto">
                <RsshubSettingsPanel />
              </div>
            ) : null}
          </div>
        </main>
      </div>
    </div>
  );
}

function SectionHeader({
  title,
  description,
  constrainContent = false,
}: {
  title: string;
  description: string;
  constrainContent?: boolean;
}) {
  return (
    <div className="panel-strong border-b border-[var(--border-subtle)] px-5 py-4">
      <div className={constrainContent ? 'mx-auto w-full max-w-[1440px]' : undefined}>
        <div className="text-sm font-semibold text-[var(--text-main)]">{title}</div>
        <p className="mt-1 text-sm text-[var(--text-muted)]">{description}</p>
      </div>
    </div>
  );
}
