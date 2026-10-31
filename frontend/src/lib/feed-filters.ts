import type {
  FeedItem,
  KnowledgeScope,
  SortMode,
  SourceScope,
  TimeWindow,
} from '@/types/feed';
import {isWithinWindow} from '@/lib/date';

interface SelectItemsArgs {
  items: FeedItem[];
  sortMode: SortMode;
  sourceScope: SourceScope;
  unreadOnly: boolean;
  query: string;
  timeWindow: TimeWindow;
}

interface SelectKnowledgeItemsArgs {
  items: FeedItem[];
  knowledgeScope: KnowledgeScope;
  sourceScope: SourceScope;
}

export function selectReaderVisibleItems({
  items,
  sortMode,
  sourceScope,
  unreadOnly,
  query,
  timeWindow,
}: SelectItemsArgs) {
  let working = items.filter((item) => !item.isLowPriorityBucket && isWithinWindow(item.publishedAt, timeWindow));

  working = applySourceScope(working, sourceScope);

  if (unreadOnly) {
    working = working.filter((item) => item.status === 'unread');
  }

  if (query) {
    working = working.filter((item) => matchesQuery(item, query));
  }

  return working.sort((left, right) => {
    if (sortMode === 'priority') {
      const scoreDiff =
        (right.relevanceScore ?? Number.NEGATIVE_INFINITY) - (left.relevanceScore ?? Number.NEGATIVE_INFINITY);
      if (scoreDiff !== 0) {
        return scoreDiff;
      }
    }

    return new Date(right.publishedAt).getTime() - new Date(left.publishedAt).getTime();
  });
}

export function selectKnowledgeBaseVisibleItems({items, knowledgeScope, sourceScope}: SelectKnowledgeItemsArgs) {
  let working = [...items];

  if (knowledgeScope === 'feed_saved') {
    working = working.filter((item) => item.originType === 'feed_saved');
  } else if (knowledgeScope === 'external_source') {
    working = working.filter((item) => item.originType === 'manual_url' || isUploadedSavedItem(item));
  }

  working = applySourceScope(working, sourceScope);

  return working.sort((left, right) => {
    const rightSavedAt = right.savedAt ? new Date(right.savedAt).getTime() : Number.NEGATIVE_INFINITY;
    const leftSavedAt = left.savedAt ? new Date(left.savedAt).getTime() : Number.NEGATIVE_INFINITY;
    if (rightSavedAt !== leftSavedAt) {
      return rightSavedAt - leftSavedAt;
    }
    return new Date(right.publishedAt).getTime() - new Date(left.publishedAt).getTime();
  });
}

function matchesQuery(item: FeedItem, query: string) {
  return [
    item.title,
    item.summary,
    item.content,
    item.sourceName,
    item.sourceFolder,
    item.note ?? '',
    item.topics.join(' '),
  ]
    .join(' ')
    .toLowerCase()
    .includes(query);
}

function isUploadedSavedItem(item: FeedItem) {
  return (
    item.originType === 'markdown_upload' ||
    item.originType === 'html_upload' ||
    item.originType === 'singlefile_html_upload'
  );
}

function applySourceScope(items: FeedItem[], sourceScope: SourceScope) {
  if (sourceScope.kind === 'source' && typeof sourceScope.value === 'number') {
    return items.filter((item) => item.sourceId === sourceScope.value);
  }

  if (sourceScope.kind === 'source_category' && typeof sourceScope.value === 'string') {
    return items.filter((item) => item.sourceCategoryId === sourceScope.value);
  }

  return items;
}
