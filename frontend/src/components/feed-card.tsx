import {useMemo, useState, type MouseEvent, type ReactNode} from 'react';
import {Bookmark, ExternalLink, ListChecks, SlidersHorizontal, Smartphone, ThumbsDown, Undo2} from 'lucide-react';
import {CaptureStatusBadge} from '@/components/capture-status-badge';
import {StatusBadge} from '@/components/status-badge';
import {formatRelativeTime} from '@/lib/date';
import {extractPlainText} from '@/lib/html';
import type {FeedItem, FeedItemRoutingCorrectionInput, HomepageFlowLane, ItemStatus, ReadingDepth, ViewMode} from '@/types/feed';
import type {InsightChannel} from '@/types/settings';

interface FeedCardProps {
  insightChannelLabels: Map<string, string>;
  insightChannels?: InsightChannel[];
  isSelected: boolean;
  item: FeedItem;
  view: ViewMode;
  onOpen: () => void;
  onOpenOriginal: (item: FeedItem) => void;
  onOpenSourceApp: (item: FeedItem) => void;
  onClearAutoLowPriority: (item: FeedItem) => void;
  onRoutingCorrection?: (item: FeedItem, correction: FeedItemRoutingCorrectionInput) => Promise<void> | void;
  onStatusChange: (item: FeedItem, nextStatus: ItemStatus) => void;
}

export function FeedCard({
  insightChannelLabels,
  insightChannels = [],
  isSelected,
  item,
  view,
  onOpen,
  onOpenOriginal,
  onOpenSourceApp,
  onClearAutoLowPriority,
  onRoutingCorrection,
  onStatusChange,
}: FeedCardProps) {
  const [isRoutingMenuOpen, setIsRoutingMenuOpen] = useState(false);
  const [routingCorrectionError, setRoutingCorrectionError] = useState<string | null>(null);
  const showKnowledgeMeta = view === 'kb';
  const hasTopics = item.topics.length > 0;
  const summaryText = extractPlainText(item.summary);
  const reasonLabels = item.priorityReasons.slice(0, 2).map((reason) => reason.label);
  const canChangeReaderState = item.feedItemId !== null;
  const canToggleSaved = item.feedItemId !== null || item.savedItemId !== null;
  const isSaving = Boolean(item.isSaving);
  const cardHeightClass = showKnowledgeMeta ? 'min-h-[176px]' : 'min-h-[188px]';
  const titleWrapClass = showKnowledgeMeta ? 'break-all' : 'break-words';
  const routingBadges = buildRoutingBadges(item, insightChannelLabels);
  const routingTooltip = buildRoutingTooltip(item);
  const enabledInsightChannels = insightChannels.filter((channel) => channel.enabled);
  const currentRoutingChannels = useMemo(
    () => buildCurrentRoutingChannels(item, insightChannelLabels),
    [insightChannelLabels, item.primaryChannelId, item.secondaryChannels],
  );
  const canCorrectRouting = view === 'ai' && item.feedItemId !== null && Boolean(onRoutingCorrection);

  function saveRoutingCorrection(correction: FeedItemRoutingCorrectionInput) {
    if (!onRoutingCorrection) {
      return;
    }
    setRoutingCorrectionError(null);
    setIsRoutingMenuOpen(false);
    void Promise.resolve(onRoutingCorrection(item, correction)).catch((error) => {
      console.error('Failed to save routing correction', error);
      setRoutingCorrectionError('路由修正保存失败，请稍后重试。');
    });
  }

  return (
    <article
      data-feed-item-id={item.id}
      className={`group relative flex ${cardHeightClass} cursor-pointer flex-col rounded-xl border p-4 transition-all ${
        isSelected
          ? 'border-[var(--accent)] bg-[var(--bg-panel)] shadow-sm ring-1 ring-[var(--accent)]'
        : item.status === 'unread'
          ? 'border-[var(--border-subtle)] bg-[var(--bg-panel)] shadow-sm hover:border-[var(--border-strong)]'
            : 'border-[var(--border-subtle)] bg-[var(--bg-panel)] opacity-70 hover:border-[var(--border-strong)] hover:opacity-100'
      }`}
      onClick={onOpen}
    >
      {isSelected || (item.relevanceScore !== null && item.relevanceScore >= 85 && item.status !== 'not_interested') ? (
        <div className="absolute bottom-4 left-0 top-4 w-1 rounded-r-full bg-[var(--accent)] opacity-80" />
      ) : null}

      <div className="mb-1.5 flex items-start justify-between gap-2 leading-tight">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-1.5 text-xs text-[var(--text-soft)]">
            <span className="truncate font-medium text-[var(--text-main)]">{item.sourceName}</span>
            <span>•</span>
            <span>{formatRelativeTime(item.publishedAt)}</span>
            {item.sourceFolder ? (
              <>
                <span>•</span>
                <span className="truncate">{item.sourceFolder}</span>
              </>
            ) : null}
          </div>
        </div>
        <StatusBadge status={item.status} />
      </div>

      <h3 className={`mb-2 pb-px text-base font-semibold leading-[1.45] text-[var(--text-main)] ${titleWrapClass}`}>
        {item.title}
      </h3>
      <p className="mb-3 break-words text-sm leading-relaxed text-[var(--text-muted)]">{summaryText || '暂无摘要'}</p>

      <div className="mt-auto border-t border-[var(--border-subtle)] pt-2">
        <div className={`flex min-h-6 items-center justify-between gap-2 transition-opacity ${isSelected ? 'opacity-100' : 'opacity-80 group-hover:opacity-100'}`}>
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            {showKnowledgeMeta && item.savedAt ? (
              <span className="truncate text-xs leading-5 text-[var(--text-muted)]">保存于 {formatRelativeTime(item.savedAt)}</span>
            ) : null}
            {showKnowledgeMeta && item.captureStatus ? <CaptureStatusBadge status={item.captureStatus} /> : null}
            {!showKnowledgeMeta && hasTopics
              ? item.topics.slice(0, 2).map((topic) => (
                <span
                  key={topic}
                  className="rounded-md border border-[var(--border-subtle)] bg-[var(--bg-app)] px-2 py-px text-[10px] leading-4 text-[var(--text-soft)]"
                >
                  {topic}
                </span>
              ))
              : null}
            {view === 'ai'
              ? reasonLabels.map((reason) => (
                  <span
                    key={reason}
                    className="rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-2 py-px text-[10px] leading-4 text-[var(--text-soft)]"
                  >
                    {reason}
                  </span>
                ))
              : null}
            {view === 'ai' && routingBadges.length > 0
              ? routingBadges.map((badge) => (
                  <span
                    key={badge.key}
                    className={`rounded-md border px-2 py-px text-[10px] leading-4 ${badge.className}`}
                    title={routingTooltip}
                  >
                    {badge.label}
                  </span>
                ))
              : null}
          </div>

          <div className="flex shrink-0 items-center gap-1">
            {item.relevanceScore !== null ? (
              <span className="mr-1 text-[11px] text-[var(--text-soft)]">{item.relevanceScore}</span>
            ) : null}
            <QuickActionButton
              disabled={!item.originalUrl}
              title="原始链接"
              onClick={(event) => {
                event.stopPropagation();
                onOpenOriginal(item);
              }}
            >
              <ExternalLink size={14} />
            </QuickActionButton>
            <QuickActionButton
              disabled={!item.sourceAppUrl}
              title="来源站点"
              onClick={(event) => {
                event.stopPropagation();
                onOpenSourceApp(item);
              }}
            >
              <Smartphone size={14} />
            </QuickActionButton>
            {canChangeReaderState ? (
              <>
                <QuickActionButton
                  active={Boolean(item.seenAt)}
                  title={item.seenAt ? '取消已处理' : '已处理'}
                  onClick={(event) => {
                    event.stopPropagation();
                    onStatusChange(item, 'seen');
                  }}
                >
                  <ListChecks size={14} />
                </QuickActionButton>
              </>
            ) : null}
            {canToggleSaved ? (
              <QuickActionButton
                active={Boolean(item.savedAt) || isSaving}
                disabled={isSaving}
                title={isSaving ? '已保存，正在创建知识库记录' : item.savedAt ? '取消保存' : '保存'}
                onClick={(event) => {
                  event.stopPropagation();
                  onStatusChange(item, 'saved');
                }}
              >
                <Bookmark size={14} />
              </QuickActionButton>
            ) : null}
            {view !== 'kb' ? (
              <QuickActionButton
                active={item.status === 'not_interested'}
                title={item.status === 'not_interested' ? '恢复优先级' : '不感兴趣'}
                onClick={(event) => {
                  event.stopPropagation();
                  onStatusChange(item, 'not_interested');
                }}
              >
                <ThumbsDown size={14} />
              </QuickActionButton>
            ) : null}
            {view !== 'kb' && item.lowPriorityBucketReason === 'auto_score_threshold' ? (
              <QuickActionButton
                title="取消低优先级"
                onClick={(event) => {
                  event.stopPropagation();
                  onClearAutoLowPriority(item);
                }}
              >
                <Undo2 size={14} />
              </QuickActionButton>
            ) : null}
            {canCorrectRouting ? (
              <div className="relative">
                <QuickActionButton
                  active={isRoutingMenuOpen}
                  title="路由修正"
                  onClick={(event) => {
                    event.stopPropagation();
                    setIsRoutingMenuOpen((current) => !current);
                  }}
                >
                  <SlidersHorizontal size={14} />
                </QuickActionButton>
                {isRoutingMenuOpen ? (
                  <RoutingCorrectionMenu
                    currentChannels={currentRoutingChannels}
                    enabledChannels={enabledInsightChannels}
                    onCorrection={saveRoutingCorrection}
                    onClose={() => setIsRoutingMenuOpen(false)}
                  />
                ) : null}
              </div>
            ) : null}
          </div>
        </div>
        {routingCorrectionError ? (
          <div className="mt-2 text-xs leading-5 text-red-500">{routingCorrectionError}</div>
        ) : null}
      </div>
    </article>
  );
}

const READING_DEPTH_OPTIONS: Array<{id: ReadingDepth; label: string}> = [
  {id: 'light', label: '轻读'},
  {id: 'medium', label: '标准'},
  {id: 'deep', label: '深读'},
];

const FLOW_LANE_OPTIONS: Array<{id: Exclude<HomepageFlowLane, 'low_priority'>; label: string}> = [
  {id: 'main', label: '主线'},
  {id: 'brief_pool', label: '快讯池'},
  {id: 'needs_review', label: '待复核'},
];

function RoutingCorrectionMenu({
  currentChannels,
  enabledChannels,
  onClose,
  onCorrection,
}: {
  currentChannels: Array<{id: string; isPrimary: boolean; label: string}>;
  enabledChannels: InsightChannel[];
  onClose: () => void;
  onCorrection: (correction: FeedItemRoutingCorrectionInput) => void;
}) {
  return (
    <div
      className="absolute bottom-8 right-0 z-30 max-h-[420px] w-64 overflow-y-auto rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-1.5 shadow-[0_18px_60px_rgba(15,23,42,0.22)]"
      onClick={(event) => event.stopPropagation()}
    >
      <MenuSection title="主频道">
        {enabledChannels.length > 0 ? (
          enabledChannels.map((channel) => (
            <MenuButton
              key={`primary-${channel.id}`}
              label={channel.label}
              onClick={() => onCorrection({primaryChannelCorrection: channel.id})}
            />
          ))
        ) : (
          <MenuEmpty>暂无可用频道</MenuEmpty>
        )}
      </MenuSection>
      <MenuSection title="加到频道">
        {enabledChannels.length > 0 ? (
          enabledChannels.map((channel) => (
            <MenuButton
              key={`add-${channel.id}`}
              label={channel.label}
              onClick={() => onCorrection({addChannelIds: [channel.id], removeChannelIds: []})}
            />
          ))
        ) : (
          <MenuEmpty>暂无可用频道</MenuEmpty>
        )}
      </MenuSection>
      <MenuSection title="移出当前">
        {currentChannels.length > 0 ? (
          currentChannels.map((channel) => (
            <MenuButton
              key={`remove-${channel.id}`}
              label={channel.label}
              onClick={() =>
                onCorrection({
                  ...(channel.isPrimary ? {primaryChannelCorrection: null} : {}),
                  addChannelIds: [],
                  removeChannelIds: [channel.id],
                })
              }
            />
          ))
        ) : (
          <MenuEmpty>暂无当前频道</MenuEmpty>
        )}
      </MenuSection>
      <MenuSection title="阅读深度">
        {READING_DEPTH_OPTIONS.map((option) => (
          <MenuButton
            key={option.id}
            label={option.label}
            onClick={() => onCorrection({readingDepthCorrection: option.id})}
          />
        ))}
      </MenuSection>
      <MenuSection title="流向">
        {FLOW_LANE_OPTIONS.map((option) => (
          <MenuButton
            key={option.id}
            label={option.label}
            onClick={() => onCorrection({flowLaneCorrection: option.id})}
          />
        ))}
      </MenuSection>
      <button
        className="mt-1 w-full rounded-md px-2 py-1.5 text-left text-xs text-[var(--text-soft)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]"
        type="button"
        onClick={onClose}
      >
        关闭
      </button>
    </div>
  );
}

function MenuSection({children, title}: {children: ReactNode; title: string}) {
  return (
    <div className="border-b border-[var(--border-subtle)] py-1 last:border-b-0">
      <div className="px-2 pb-1 pt-0.5 text-[10px] font-semibold uppercase tracking-wide text-[var(--text-soft)]">
        {title}
      </div>
      <div className="space-y-0.5">{children}</div>
    </div>
  );
}

function MenuButton({label, onClick}: {label: string; onClick: () => void}) {
  return (
    <button
      className="w-full truncate rounded-md px-2 py-1.5 text-left text-xs text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]"
      type="button"
      onClick={onClick}
    >
      {label}
    </button>
  );
}

function MenuEmpty({children}: {children: ReactNode}) {
  return <div className="px-2 py-1.5 text-xs text-[var(--text-soft)]">{children}</div>;
}

const READING_DEPTH_LABELS = {
  light: '轻读',
  medium: '标准',
  deep: '深读',
} as const;

const FLOW_LANE_LABELS = {
  main: '主列表',
  brief_pool: '快讯池',
  low_priority: '低优先级',
  needs_review: '待复核',
} as const;

function buildRoutingBadges(item: FeedItem, insightChannelLabels: Map<string, string>) {
  const badges: Array<{key: string; label: string; className: string}> = [];
  if (item.primaryChannelId) {
    badges.push({
      key: 'channel',
      label: insightChannelLabels.get(item.primaryChannelId) ?? item.primaryChannelId,
      className: 'border-[var(--border-subtle)] bg-[var(--bg-app)] text-[var(--text-soft)]',
    });
  }
  if (item.readingDepth) {
    badges.push({
      key: 'depth',
      label: READING_DEPTH_LABELS[item.readingDepth],
      className: 'border-[var(--border-subtle)] bg-[var(--bg-app)] text-[var(--text-soft)]',
    });
  }
  if (item.flowLane) {
    badges.push({
      key: 'lane',
      label: FLOW_LANE_LABELS[item.flowLane],
      className: 'border-[var(--border-subtle)] bg-[var(--bg-app)] text-[var(--text-soft)]',
    });
  }
  if (item.routingSource === 'user_corrected') {
    badges.push({
      key: 'user_corrected',
      label: '已纠偏',
      className: 'border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]',
    });
  }
  if (typeof item.routingConfidence === 'number' && item.routingConfidence < 0.55) {
    badges.push({
      key: 'confidence',
      label: '低置信',
      className: 'border-[var(--warning)] bg-[var(--bg-panel)] text-[var(--warning)]',
    });
  }
  return badges;
}

function buildCurrentRoutingChannels(item: FeedItem, insightChannelLabels: Map<string, string>) {
  const channels = new Map<string, {isPrimary: boolean; label: string}>();
  if (item.primaryChannelId) {
    channels.set(item.primaryChannelId, {
      isPrimary: true,
      label: insightChannelLabels.get(item.primaryChannelId) ?? item.primaryChannelId,
    });
  }
  for (const channel of item.secondaryChannels ?? []) {
    if (!channels.has(channel.channelId)) {
      channels.set(channel.channelId, {
        isPrimary: false,
        label: insightChannelLabels.get(channel.channelId) ?? channel.channelId,
      });
    }
  }
  return Array.from(channels, ([id, metadata]) => ({id, ...metadata}));
}

function buildRoutingTooltip(item: FeedItem) {
  const details: string[] = [];
  if (item.routingSource) {
    details.push(`routingSource: ${item.routingSource}`);
  }
  if (typeof item.routingConfidence === 'number') {
    details.push(`confidence: ${Math.round(item.routingConfidence * 100)}%`);
  }
  if (item.routingReasons && item.routingReasons.length > 0) {
    details.push(`reasons: ${item.routingReasons.join(', ')}`);
  }
  return details.length > 0 ? details.join('\n') : undefined;
}

function QuickActionButton({
  active = false,
  children,
  disabled = false,
  onClick,
  title,
}: {
  active?: boolean;
  children: ReactNode;
  disabled?: boolean;
  onClick: (event: MouseEvent<HTMLButtonElement>) => void;
  title: string;
}) {
  return (
    <button
      className={`rounded-md p-1 transition-colors ${
        active
          ? 'bg-[var(--accent-soft)] text-[var(--accent)]'
          : 'text-[var(--text-soft)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]'
      } disabled:cursor-not-allowed disabled:opacity-60`}
      disabled={disabled}
      title={title}
      type="button"
      onClick={onClick}
    >
      {children}
    </button>
  );
}
