import {useEffect, useLayoutEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type ReactNode} from 'react';
import {
  ArchiveRestore,
  Bookmark,
  BookOpen,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clock3,
  ExternalLink,
  Globe2,
  Hash,
  Layers3,
  MoreHorizontal,
  Rss,
} from 'lucide-react';
import {SourceIcon} from '@/components/source-icon';
import {
  EMPTY_SOURCE_CATEGORY_ID,
  EMPTY_SOURCE_CATEGORY_LABEL,
  sourceCategoryIdFromFolderPath,
  sourceCategoryLabelFromFolderPath,
} from '@/lib/source-category';
import type {
  ChannelFacetCount,
  FacetCount,
  FlowLaneFacetCount,
  GroupMode,
  KnowledgeScope,
  ReadingDepthFilter,
  SourceScope,
  ViewMode,
} from '@/types/feed';
import type {InsightChannel} from '@/types/settings';
import type {SourceDto} from '@/types/source';

const SOURCE_CATEGORY_STORAGE_KEY = 'ifa:sidebar:source-categories:v1';
const READING_DEPTH_FILTERS: Array<{value: ReadingDepthFilter; label: string}> = [
  {value: 'all', label: '全部强度'},
  {value: 'light', label: '轻读'},
  {value: 'medium', label: '标准'},
  {value: 'deep', label: '深读'},
];

interface SidebarProps {
  activeFacet: string | null;
  channelFacets: ChannelFacetCount[];
  counts: {
    unread: number;
    saved: number;
    readFull: number;
    lowPriority: number;
    topicCount: number;
    sourceCount: number;
  };
  flowLaneFacets: FlowLaneFacetCount[];
  groupMode: GroupMode;
  insightChannels: InsightChannel[];
  knowledgeCounts: {
    all: number;
    feedSaved: number;
    externalSource: number;
  };
  knowledgeScope: KnowledgeScope;
  readingDepthFilter: ReadingDepthFilter;
  sourceCategorySuggestions: string[];
  sourceSyncBusyReason: string | null;
  sourceItemCounts: {
    bySourceId: Map<number, number>;
    byCategory: Map<string, number>;
  };
  sourceScope: SourceScope;
  sources: SourceDto[];
  supportsTopicMode: boolean;
  topicFacets: FacetCount[];
  view: ViewMode;
  width: number;
  onGroupModeChange: (nextMode: GroupMode) => void;
  onKnowledgeScopeChange: (nextScope: KnowledgeScope) => void;
  onReadingDepthFilterChange: (nextFilter: ReadingDepthFilter) => void;
  onSelectAllSources: () => void;
  onSelectChannel: (channelId: string) => void;
  onSelectFlowLane: (lane: string) => void;
  onSelectSource: (source: SourceDto) => void;
  onSelectSourceCategory: (categoryId: string, label: string) => void;
  onSelectTopic: (topic: string) => void;
  onSourceActiveChange: (source: SourceDto, isActive: boolean) => void;
  onSourceEdit: (source: SourceDto) => void;
  onSourceMarkReadFull: (source: SourceDto) => void;
  onSourceMoveToCategory: (source: SourceDto, category: string | null) => void;
  onSourceSync: (source: SourceDto) => void;
  onViewChange: (nextView: ViewMode) => void;
}

interface SourceContextMenuState {
  source: SourceDto;
  x: number;
  y: number;
}

export function Sidebar({
  activeFacet,
  channelFacets,
  counts,
  flowLaneFacets,
  groupMode,
  insightChannels,
  knowledgeCounts,
  knowledgeScope,
  readingDepthFilter,
  sourceCategorySuggestions,
  sourceSyncBusyReason,
  sourceItemCounts,
  sourceScope,
  sources,
  supportsTopicMode,
  topicFacets,
  view,
  width,
  onGroupModeChange,
  onKnowledgeScopeChange,
  onReadingDepthFilterChange,
  onSelectAllSources,
  onSelectChannel,
  onSelectFlowLane,
  onSelectSource,
  onSelectSourceCategory,
  onSelectTopic,
  onSourceActiveChange,
  onSourceEdit,
  onSourceMarkReadFull,
  onSourceMoveToCategory,
  onSourceSync,
  onViewChange,
}: SidebarProps) {
  const [expandedCategories, setExpandedCategories] = useState<Record<string, boolean>>(readExpandedCategories);
  const [contextMenu, setContextMenu] = useState<SourceContextMenuState | null>(null);
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const lastScrollTopRef = useRef(0);
  const groupedSources = useMemo(() => groupSources(sources), [sources]);
  const channelRows = useMemo(
    () => buildChannelRows(insightChannels, channelFacets),
    [channelFacets, insightChannels],
  );
  const homepageTotalCount = counts.topicCount;
  const flowLaneTotalCount = flowLaneFacets.reduce((total, facet) => total + facet.count, 0);
  const knowledgeSources = useMemo(
    () =>
      sources
        .filter((source) => (sourceItemCounts.bySourceId.get(source.id) ?? 0) > 0)
        .sort(
          (left, right) =>
            Number(right.is_active) - Number(left.is_active) || left.name.localeCompare(right.name, 'zh-CN'),
        ),
    [sourceItemCounts.bySourceId, sources],
  );

  useLayoutEffect(() => {
    const scrollContainer = scrollContainerRef.current;
    if (!scrollContainer) {
      return undefined;
    }

    const maxScrollTop = Math.max(0, scrollContainer.scrollHeight - scrollContainer.clientHeight);
    scrollContainer.scrollTop = Math.min(lastScrollTopRef.current, maxScrollTop);

    return () => {
      lastScrollTopRef.current = scrollContainer.scrollTop;
    };
  });

  useEffect(() => {
    window.localStorage.setItem(SOURCE_CATEGORY_STORAGE_KEY, JSON.stringify(expandedCategories));
  }, [expandedCategories]);

  useEffect(() => {
    if (!contextMenu) {
      return;
    }

    function closeMenu() {
      setContextMenu(null);
    }

    window.addEventListener('click', closeMenu);
    window.addEventListener('keydown', closeMenu);
    return () => {
      window.removeEventListener('click', closeMenu);
      window.removeEventListener('keydown', closeMenu);
    };
  }, [contextMenu]);

  return (
    <aside className="panel hidden shrink-0 flex-col border-r border-[var(--border-subtle)] md:flex" style={{width}}>
      <div
        ref={scrollContainerRef}
        className="min-h-0 flex-1 space-y-7 overflow-y-auto px-4 py-5"
        onScroll={(event) => {
          lastScrollTopRef.current = event.currentTarget.scrollTop;
        }}
      >
        {view === 'kb' ? (
          <SidebarSection title="知识库">
            <SidebarItem
              active={knowledgeScope === 'all' && sourceScope.kind === 'all_sources'}
              count={knowledgeCounts.all}
              icon={<Bookmark size={16} />}
              label="全部保存"
              onClick={() => onKnowledgeScopeChange('all')}
            />
            <SidebarItem
              active={knowledgeScope === 'feed_saved' && sourceScope.kind === 'all_sources'}
              count={knowledgeCounts.feedSaved}
              icon={<Rss size={16} />}
              label="订阅保存"
              onClick={() => onKnowledgeScopeChange('feed_saved')}
            />
            <SidebarItem
              active={knowledgeScope === 'external_source' && sourceScope.kind === 'all_sources'}
              count={knowledgeCounts.externalSource}
              icon={<Globe2 size={16} />}
              label="外部来源"
              onClick={() => onKnowledgeScopeChange('external_source')}
            />
          </SidebarSection>
        ) : (
          <SidebarSection title="信息聚合">
            <SidebarItem
              active={groupMode === 'time' && view === 'ai'}
              count={counts.unread}
              icon={<Clock3 size={16} />}
              label="时间线"
              onClick={() => {
                onViewChange('ai');
                onGroupModeChange('time');
              }}
            />
            {supportsTopicMode ? (
              <SidebarItem
                active={groupMode === 'topic' && view === 'ai' && activeFacet === null}
                count={counts.topicCount}
                icon={<Hash size={16} />}
                label="主题分类"
                onClick={() => {
                  onViewChange('ai');
                  onGroupModeChange('topic');
                }}
              />
            ) : null}
            <SidebarItem
              active={groupMode === 'source' && sourceScope.kind === 'all_sources'}
              count={counts.sourceCount}
              icon={<Rss size={16} />}
              label="全部来源"
              onClick={onSelectAllSources}
            />
            <SidebarItem
              active={groupMode === 'low_priority' && view === 'ai'}
              count={counts.lowPriority}
              icon={<ArchiveRestore size={16} />}
              label="低优先级"
              onClick={() => {
                onViewChange('ai');
                onGroupModeChange('low_priority');
              }}
            />
            {channelRows.length > 0 ? (
              <SidebarItem
                active={groupMode === 'channel' && view === 'ai' && activeFacet === null}
                count={homepageTotalCount}
                icon={<Hash size={16} />}
                label="Insight 频道"
                onClick={() => {
                  onViewChange('ai');
                  onGroupModeChange('channel');
                }}
              />
            ) : null}
            {flowLaneFacets.length > 0 ? (
              <SidebarItem
                active={groupMode === 'flow_lane' && view === 'ai' && activeFacet === null}
                count={flowLaneTotalCount}
                icon={<Layers3 size={16} />}
                label="阅读流"
                onClick={() => {
                  onViewChange('ai');
                  onGroupModeChange('flow_lane');
                }}
              />
            ) : null}
          </SidebarSection>
        )}

        {view === 'ai' && groupMode === 'topic' && supportsTopicMode ? (
          <SidebarSection title="AI 自动提取主题">
            {topicFacets.map((topic) => (
              <SidebarItem
                key={topic.label}
                active={groupMode === 'topic' && activeFacet === topic.label && view === 'ai'}
                count={topic.count}
                icon={<Hash size={14} className="text-[var(--accent)]" />}
                label={topic.label}
                onClick={() => {
                  onViewChange('ai');
                  onSelectTopic(topic.label);
                }}
              />
            ))}
          </SidebarSection>
        ) : null}

        {view === 'ai' && groupMode === 'channel' && channelRows.length > 0 ? (
          <SidebarSection title="Insight 频道">
            {channelRows.map((facet) => (
              <SidebarItem
                key={facet.channelId}
                active={groupMode === 'channel' && activeFacet === facet.channelId}
                count={facet.count}
                icon={<Hash size={14} className="text-[var(--accent)]" />}
                label={facet.label}
                onClick={() => onSelectChannel(facet.channelId)}
              />
            ))}
          </SidebarSection>
        ) : null}

        {view === 'ai' && groupMode === 'flow_lane' && flowLaneFacets.length > 0 ? (
          <SidebarSection title="阅读流">
            {flowLaneFacets.map((facet) => {
              const active = groupMode === 'flow_lane' && activeFacet === facet.lane;
              return (
                <SidebarItem
                  key={facet.lane}
                  active={active}
                  count={facet.count}
                  icon={<Layers3 size={14} className="text-[var(--accent)]" />}
                  label={facet.label}
                  onClick={() => onSelectFlowLane(facet.lane)}
                />
              );
            })}
          </SidebarSection>
        ) : null}

        {view === 'ai' ? (
          <SidebarSection title="阅读强度">
            {READING_DEPTH_FILTERS.map((filter) => (
              <SidebarItem
                key={filter.value}
                active={filter.value !== 'all' && readingDepthFilter === filter.value}
                icon={<BookOpen size={14} className="text-[var(--accent)]" />}
                label={filter.label}
                onClick={() => onReadingDepthFilterChange(filter.value)}
              />
            ))}
          </SidebarSection>
        ) : null}

        {view === 'kb' || view === 'reader' || groupMode === 'source' ? (
          <SidebarSection title={view === 'kb' ? '订阅保存来源' : '来源列表'}>
            {view === 'kb'
              ? knowledgeSources.map((source) => (
                  <SourceRow
                    key={source.id}
                    active={sourceScope.kind === 'source' && sourceScope.value === source.id}
                    count={sourceItemCounts.bySourceId.get(source.id) ?? 0}
                    source={source}
                    onClick={() => onSelectSource(source)}
                    onContextMenu={(event) => {
                      event.preventDefault();
                      setContextMenu({source, x: event.clientX, y: event.clientY});
                    }}
                  />
                ))
              : groupedSources.map((group) => {
                  const isExpanded = expandedCategories[group.categoryId] ?? true;
                  const isCategoryActive =
                    sourceScope.kind === 'source_category' && sourceScope.value === group.categoryId;
                  return (
                    <div key={group.categoryId}>
                      <div className="flex items-center gap-0">
                        <button
                          className="flex h-7 w-5 shrink-0 items-center justify-center rounded-md text-[var(--text-soft)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]"
                          title={isExpanded ? '收起分类' : '展开分类'}
                          type="button"
                          onClick={() =>
                            setExpandedCategories((current) => ({
                              ...current,
                              [group.categoryId]: !isExpanded,
                            }))
                          }
                        >
                          {isExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                        </button>
                        <SidebarItem
                          active={isCategoryActive}
                          count={sourceItemCounts.byCategory.get(group.categoryId) ?? 0}
                          label={group.label}
                          onClick={() => onSelectSourceCategory(group.categoryId, group.label)}
                        />
                      </div>
                      {isExpanded ? (
                        <div className="ml-8 space-y-0.5">
                          {group.sources.map((source) => (
                            <SourceRow
                              key={source.id}
                              active={sourceScope.kind === 'source' && sourceScope.value === source.id}
                              count={sourceItemCounts.bySourceId.get(source.id) ?? 0}
                              source={source}
                              onClick={() => onSelectSource(source)}
                              onContextMenu={(event) => {
                                event.preventDefault();
                                setContextMenu({source, x: event.clientX, y: event.clientY});
                              }}
                            />
                          ))}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
          </SidebarSection>
        ) : null}
      </div>

      {contextMenu ? (
        <SourceContextMenu
          categories={sourceCategorySuggestions}
          menu={contextMenu}
          onClose={() => setContextMenu(null)}
          onEdit={onSourceEdit}
          onMarkReadFull={onSourceMarkReadFull}
          onMoveToCategory={onSourceMoveToCategory}
          onSourceActiveChange={onSourceActiveChange}
          onSync={onSourceSync}
          syncDisabledReason={sourceSyncBusyReason}
        />
      ) : null}
    </aside>
  );
}

function buildChannelRows(channels: InsightChannel[], facets: ChannelFacetCount[]) {
  const facetByChannel = new Map(facets.map((facet) => [facet.channelId, facet]));
  const channelRows = channels
    .filter((channel) => channel.enabled && !channel.retiredAt)
    .map((channel) => ({
      channelId: channel.id,
      label: channel.label,
      count: facetByChannel.get(channel.id)?.count ?? 0,
    }));
  const knownIds = new Set(channelRows.map((row) => row.channelId));
  const unknownRows = facets
    .filter((facet) => !knownIds.has(facet.channelId) && facet.count > 0)
    .map((facet) => ({
      channelId: facet.channelId,
      label: facet.label,
      count: facet.count,
    }));
  return [...channelRows, ...unknownRows];
}

function readExpandedCategories() {
  if (typeof window === 'undefined') {
    return {};
  }

  try {
    const parsed = JSON.parse(window.localStorage.getItem(SOURCE_CATEGORY_STORAGE_KEY) ?? '{}') as Record<
      string,
      boolean
    >;
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function groupSources(sources: SourceDto[]) {
  const groups = new Map<string, {label: string; sources: SourceDto[]}>();
  for (const source of sources) {
    const category = normalizeSourceCategory(source.folder_path);
    const group = groups.get(category.id) ?? {label: category.label, sources: []};
    group.sources.push(source);
    groups.set(category.id, group);
  }

  return [...groups.entries()]
    .map(([categoryId, group]) => ({
      categoryId,
      label: group.label,
      sources: group.sources.sort((left, right) => Number(right.is_active) - Number(left.is_active) || left.name.localeCompare(right.name, 'zh-CN')),
    }))
    .sort((left, right) => {
      if (left.categoryId === EMPTY_SOURCE_CATEGORY_ID) {
        return 1;
      }
      if (right.categoryId === EMPTY_SOURCE_CATEGORY_ID) {
        return -1;
      }
      return left.label.localeCompare(right.label, 'zh-CN');
    });
}

function normalizeSourceCategory(value: string | null) {
  return {
    id: sourceCategoryIdFromFolderPath(value),
    label: sourceCategoryLabelFromFolderPath(value),
  };
}

function SidebarSection({title, children}: {title: string; children: ReactNode}) {
  return (
    <section>
      <h2 className="mb-2 px-2 text-xs font-semibold text-[var(--text-muted)]">{title}</h2>
      <div className="space-y-0.5">{children}</div>
    </section>
  );
}

function SidebarItem({
  active = false,
  count,
  icon,
  label,
  onClick,
}: {
  active?: boolean;
  count?: number;
  icon?: ReactNode;
  label: string;
  onClick?: () => void;
}) {
  return (
    <button
      className={`flex w-full min-w-0 flex-1 items-center justify-between rounded-md px-2 py-1.5 text-sm transition-colors ${
        active
          ? 'bg-[var(--bg-panel-strong)] text-[var(--text-main)]'
          : 'text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]'
      }`}
      onClick={onClick}
      type="button"
    >
      <div className="flex min-w-0 items-center gap-2">
        {icon ? <span className="shrink-0">{icon}</span> : null}
        <span className="truncate">{label}</span>
      </div>
      {count !== undefined ? (
        <span className="ml-2 min-w-[1.5rem] shrink-0 rounded-full bg-[var(--border-subtle)] px-1.5 py-0.5 text-center text-[11px] text-[var(--text-muted)]">
          {count}
        </span>
      ) : null}
    </button>
  );
}

function SourceRow({
  active,
  count,
  source,
  onClick,
  onContextMenu,
}: {
  active: boolean;
  count: number;
  source: SourceDto;
  onClick: () => void;
  onContextMenu: (event: ReactMouseEvent<HTMLButtonElement>) => void;
}) {
  const errorTooltip = buildSourceErrorTooltip(source);
  return (
    <button
      className={`group relative flex w-full items-center justify-between rounded-md px-2 py-1.5 text-sm transition-colors ${
        active
          ? 'bg-[var(--bg-panel-strong)] text-[var(--text-main)]'
          : source.is_active
            ? 'text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]'
            : 'text-[var(--text-soft)] opacity-70 hover:bg-[var(--bg-hover)]'
      }`}
      type="button"
      title={errorTooltip ?? undefined}
      onClick={onClick}
      onContextMenu={onContextMenu}
    >
      <span className="flex min-w-0 items-center gap-2">
        <SourceIcon source={source} />
        <span className={`min-w-0 truncate ${errorTooltip ? 'text-[var(--warning)]' : ''}`}>{source.name}</span>
      </span>
      <span className="ml-2 flex shrink-0 items-center gap-1.5">
        {errorTooltip ? (
          <span title={errorTooltip} aria-label="最近刷新失败">
            <CircleAlert className="text-[var(--warning)]" size={14} aria-hidden="true" />
          </span>
        ) : null}
        <span className="min-w-[1.5rem] rounded-full bg-[var(--border-subtle)] px-1.5 py-0.5 text-center text-[11px] text-[var(--text-muted)]">
          {count}
        </span>
      </span>
      <MoreHorizontal
        className={`pointer-events-none absolute opacity-0 transition-opacity group-hover:opacity-70 ${
          errorTooltip ? 'right-16' : 'right-10'
        }`}
        size={14}
      />
    </button>
  );
}

function buildSourceErrorTooltip(source: SourceDto) {
  if (source.latest_fetch_status !== 'error') {
    return null;
  }

  const httpStatus = source.latest_fetch_http_status !== null ? String(source.latest_fetch_http_status) : '—';
  const errorMessage = source.latest_fetch_error_message || '未返回错误详情';
  const fetchTime = formatSourceFetchTime(source.latest_fetch_finished_at ?? source.latest_fetch_started_at);
  return `最近刷新失败 · HTTP ${httpStatus} · ${errorMessage} · ${fetchTime}`;
}

function formatSourceFetchTime(value: string | null) {
  if (!value) {
    return '时间未知';
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString('zh-CN', {hour12: false});
}

function SourceContextMenu({
  categories,
  menu,
  onClose,
  onEdit,
  onMarkReadFull,
  onMoveToCategory,
  onSourceActiveChange,
  onSync,
  syncDisabledReason,
}: {
  categories: string[];
  menu: SourceContextMenuState;
  onClose: () => void;
  onEdit: (source: SourceDto) => void;
  onMarkReadFull: (source: SourceDto) => void;
  onMoveToCategory: (source: SourceDto, category: string | null) => void;
  onSourceActiveChange: (source: SourceDto, isActive: boolean) => void;
  onSync: (source: SourceDto) => void;
  syncDisabledReason: string | null;
}) {
  const openUrl = menu.source.site_url ?? (isHttpUrl(menu.source.url) ? menu.source.url : null);
  const availableCategories = categories.filter((category) => category !== menu.source.folder_path);
  return (
    <div
      className="fixed z-50 w-64 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-1 text-sm shadow-[0_18px_60px_rgba(15,23,42,0.22)]"
      style={{left: menu.x, top: menu.y}}
      onClick={(event) => event.stopPropagation()}
    >
      <MenuButton
        label="全部标记为已处理"
        onClick={() => {
          onMarkReadFull(menu.source);
          onClose();
        }}
      />
      <MenuButton
        label="编辑订阅"
        onClick={() => {
          onEdit(menu.source);
          onClose();
        }}
      />
      <MenuButton
        disabled={Boolean(syncDisabledReason)}
        label="立即刷新"
        title={syncDisabledReason ?? undefined}
        onClick={() => {
          onSync(menu.source);
          onClose();
        }}
      />
      <MenuButton
        label={menu.source.is_active ? '停用订阅' : '重新启用'}
        onClick={() => {
          onSourceActiveChange(menu.source, !menu.source.is_active);
          onClose();
        }}
      />
      <div className="my-1 border-t border-[var(--border-subtle)]" />
      <div className="px-2 py-1 text-xs font-semibold text-[var(--text-soft)]">移动至分类</div>
      <MenuButton
        label={EMPTY_SOURCE_CATEGORY_LABEL}
        onClick={() => {
          onMoveToCategory(menu.source, null);
          onClose();
        }}
      />
      {availableCategories.slice(0, 8).map((category) => (
        <MenuButton
          key={category}
          label={category}
          onClick={() => {
            onMoveToCategory(menu.source, category);
            onClose();
          }}
        />
      ))}
      <MenuButton
        label="新分类..."
        onClick={() => {
          onEdit(menu.source);
          onClose();
        }}
      />
      <div className="my-1 border-t border-[var(--border-subtle)]" />
      <MenuButton
        disabled={!openUrl}
        icon={<ExternalLink size={14} />}
        label="浏览器打开网站"
        onClick={() => {
          if (openUrl) {
            window.open(openUrl, '_blank', 'noopener,noreferrer');
          }
          onClose();
        }}
      />
      <MenuButton
        label="复制订阅源 URL"
        onClick={() => {
          void navigator.clipboard.writeText(menu.source.url);
          onClose();
        }}
      />
    </div>
  );
}

function MenuButton({
  disabled = false,
  icon,
  label,
  onClick,
  title,
}: {
  disabled?: boolean;
  icon?: ReactNode;
  label: string;
  onClick: () => void;
  title?: string;
}) {
  return (
    <button
      className="flex w-full items-center justify-between gap-3 rounded-md px-2 py-1.5 text-left text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-45"
      disabled={disabled}
      title={title}
      type="button"
      onClick={onClick}
    >
      <span className="truncate">{label}</span>
      {icon}
    </button>
  );
}

function isHttpUrl(value: string) {
  return value.startsWith('http://') || value.startsWith('https://');
}
