import {formatAbsoluteDate} from '@/lib/date';
import {renderReaderContentHtml} from '@/lib/html';
import type {FeedItem} from '@/types/feed';

export function ContentExtractionPill({item}: {item: FeedItem}) {
  const presentation = getContentExtractionPresentation(item);
  return (
    <span
      className={`rounded-full border px-2 py-0.5 text-[11px] ${presentation.className}`}
      title={presentation.title}
    >
      {presentation.label}
    </span>
  );
}

export function FeedCaptureSummaryCard({item}: {item: FeedItem}) {
  const rssPreview = buildRssOriginalPreview(item);
  const extractionTime = item.contentExtractedAt ?? item.contentExtractionAttemptedAt;
  const extractionTimeLabel = extractionTime ? formatAbsoluteDate(extractionTime) : '尚未补提取';
  const extractionMethodLabel = buildContentExtractionMethodLabel(item);
  const finalExtractionUrl = getDisplayableFinalExtractionUrl(item);
  const visibleCharCount = item.contentExtractionVisibleCharCount ?? null;

  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-4 py-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs font-semibold uppercase tracking-[0.18em] text-[var(--text-soft)]">正文提取摘要</div>
        <ContentExtractionPill item={item} />
      </div>
      <div className="grid gap-2">
        <div className="flex min-w-0 items-baseline gap-2">
          <span className="shrink-0 text-xs text-[var(--text-soft)]">RSS 原文</span>
          <span className="min-w-0 truncate text-sm leading-relaxed text-[var(--text-muted)]" title={rssPreview}>
            {rssPreview}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-[var(--text-soft)]">
          <span>提取时间</span>
          <span className="text-[var(--text-muted)]">{extractionTimeLabel}</span>
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-[var(--text-soft)]">
          <span>提取方式</span>
          <span className="text-[var(--text-muted)]">{extractionMethodLabel}</span>
          {visibleCharCount !== null ? (
            <span className="text-[var(--text-muted)]">可见正文 {visibleCharCount} 字</span>
          ) : null}
        </div>
        {finalExtractionUrl ? (
          <div className="flex min-w-0 items-baseline gap-2">
            <span className="shrink-0 text-xs text-[var(--text-soft)]">最终页面</span>
            <a
              className="min-w-0 truncate text-sm leading-relaxed text-blue-600 underline-offset-4 hover:underline"
              href={finalExtractionUrl}
              rel="noreferrer"
              target="_blank"
              title={finalExtractionUrl}
            >
              {finalExtractionUrl}
            </a>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function buildContentExtractionMethodLabel(item: FeedItem) {
  return item.contentExtractionMethod?.trim() || '未记录';
}

function getDisplayableFinalExtractionUrl(item: FeedItem) {
  const finalUrl = item.contentExtractionFinalUrl?.trim();
  if (!finalUrl) {
    return null;
  }
  if (areComparableUrlsEqual(finalUrl, item.originalUrl)) {
    return null;
  }
  return finalUrl;
}

function areComparableUrlsEqual(left: string | null | undefined, right: string | null | undefined) {
  const normalizedLeft = normalizeComparableUrl(left);
  const normalizedRight = normalizeComparableUrl(right);
  return Boolean(normalizedLeft && normalizedRight && normalizedLeft === normalizedRight);
}

function normalizeComparableUrl(value: string | null | undefined) {
  if (!value) {
    return null;
  }
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  try {
    const parsed = new URL(trimmed);
    parsed.hash = '';
    if (parsed.pathname.length > 1 && parsed.pathname.endsWith('/')) {
      parsed.pathname = parsed.pathname.slice(0, -1);
    }
    return parsed.toString();
  } catch {
    return trimmed;
  }
}

export function buildRssOriginalPreview(item: FeedItem) {
  if (item.feedContentText === undefined) {
    return '正在加载 RSS 原文...';
  }
  const raw = item.feedContentText ?? '';
  const normalized = normalizePlainText(raw);
  return normalized || '当前条目没有可显示的 RSS 原文。';
}

export function buildReaderBodyHtml(item: FeedItem) {
  const raw = item.content || item.feedContentText || '';
  return raw ? renderReaderContentHtml(raw) : '';
}

export function shouldShowFeedCaptureSummaryCard(item: FeedItem) {
  if (item.feedItemId === null) {
    return false;
  }
  if (item.contentTextSource === 'extracted_page' || item.contentExtractionStatus === 'succeeded') {
    return true;
  }
  if (item.contentExtractionStatus === 'failed') {
    return Boolean(item.contentExtractionAttemptedAt);
  }
  if (item.contentExtractionStatus === 'needs_extraction') {
    return Boolean(item.contentExtractionAttemptedAt);
  }
  return false;
}

export function shouldShowContentExtractionAction(item: FeedItem) {
  if (item.feedItemId === null) {
    return false;
  }
  if (item.contentExtractionStatus === 'needs_extraction') {
    return false;
  }
  if (item.contentTextSource === 'extracted_page' || item.contentExtractionStatus === 'succeeded') {
    return true;
  }
  if (item.contentExtractionStatus === 'failed') {
    return true;
  }
  if (item.contentExtractionStatus === 'not_needed' || item.contentExtractionStatus === 'skipped') {
    return false;
  }
  return isLikelyShortFeedContent(item);
}

export function getContentExtractionActionLabel(item: FeedItem) {
  if (item.contentTextSource === 'extracted_page' || item.contentExtractionStatus === 'succeeded') {
    return '重新提取正文';
  }
  return '报告并提取正文';
}

export function getContentExtractionPresentation(item: FeedItem) {
  if (item.contentExtractionStatus === 'needs_extraction') {
    return {
      label: '正文提取中',
      title: '正在从原始页面补提取正文',
      className: 'border-amber-500/30 bg-amber-500/10 text-amber-600',
    };
  }
  if (item.contentExtractionStatus === 'failed') {
    return {
      label: '提取失败',
      title: item.contentExtractionError ?? '最近一次正文提取失败',
      className: 'border-red-500/30 bg-red-500/10 text-red-500',
    };
  }
  if (item.contentTextSource === 'extracted_page' || item.contentExtractionStatus === 'succeeded') {
    return {
      label: '已提取正文',
      title: item.contentExtractedAt ? `提取时间：${formatAbsoluteDate(item.contentExtractedAt)}` : '已使用页面抽取正文',
      className: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600',
    };
  }
  if (item.contentExtractionStatus === 'not_needed') {
    return getContentSourcePresentation(item);
  }
  if (isLikelyShortFeedContent(item)) {
    return {
      label: '疑似只有源链接',
      title: '当前 RSS 内容很短，可手动报告并提取正文',
      className: 'border-amber-500/30 bg-amber-500/10 text-amber-600',
    };
  }
  return getContentSourcePresentation(item);
}

function getContentSourcePresentation(item: FeedItem) {
  if (item.contentTextSource === 'feed_summary') {
    return {
      label: 'RSS 原文',
      title: '当前正文来自 RSS/Atom description 或 summary 字段',
      className: 'border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] text-[var(--text-soft)]',
    };
  }
  if (item.contentTextSource === 'feed_content') {
    return {
      label: 'RSS 原文',
      title: '当前正文来自 RSS/Atom 内容字段',
      className: 'border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] text-[var(--text-soft)]',
    };
  }
  return {
    label: '正文来源未知',
    title: '当前条目没有可识别的 RSS 正文来源',
    className: 'border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] text-[var(--text-soft)]',
  };
}

function isLikelyShortFeedContent(item: FeedItem) {
  const candidates = getFeedBodyCandidatesForShortContentCheck(item);
  if (candidates.length === 0) {
    return true;
  }
  return candidates.every(isShortOrJumpPromptBody);
}

function getFeedBodyCandidatesForShortContentCheck(item: FeedItem) {
  const candidates: string[] = [];
  if (item.feedContentText !== undefined && item.feedContentText) {
    candidates.push(item.feedContentText);
  }
  if (item.content) {
    candidates.push(item.content);
  }
  if (candidates.length === 0 && item.contentTextSource === 'feed_summary' && item.summary) {
    candidates.push(item.summary);
  }
  return candidates;
}

function isShortOrJumpPromptBody(value: string) {
  const text = normalizePlainText(value);
  if (!text) {
    return true;
  }
  const withoutUrls = normalizePlainText(text.replace(/https?:\/\/\S+/gi, ' '));
  const hasJumpPrompt = /阅读原文|阅读全文|继续阅读|前往原站查看|Read more|Continue reading/i.test(text);
  const urlOnly = /https?:\/\/\S+/i.test(text) && withoutUrls.length < 12;
  return urlOnly || withoutUrls.length < 40 || (hasJumpPrompt && withoutUrls.length < 160);
}

function normalizePlainText(value: string) {
  const container = document.createElement('div');
  container.innerHTML = value;
  return (container.textContent ?? container.innerText ?? '').replace(/\s+/g, ' ').trim();
}
