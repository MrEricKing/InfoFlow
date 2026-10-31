import {Bookmark, BookOpenCheck, Circle, ListChecks, ThumbsDown} from 'lucide-react';
import type {ItemStatus} from '@/types/feed';

export function StatusBadge({status}: {status: ItemStatus}) {
  switch (status) {
    case 'unread':
      return (
        <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-[var(--accent)]">
          <Circle size={8} fill="currentColor" />
          未读
        </span>
      );
    case 'browsed':
      return <span className="text-[11px] font-semibold text-[var(--text-soft)]">已浏览</span>;
    case 'seen':
      return (
        <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-[var(--text-soft)]">
          <ListChecks size={10} />
          已处理
        </span>
      );
    case 'read_full':
      return (
        <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-[var(--success)]">
          <BookOpenCheck size={10} />
          已读全文
        </span>
      );
    case 'saved':
      return (
        <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-[var(--warning)]">
          <Bookmark size={10} fill="currentColor" />
          已保存
        </span>
      );
    case 'not_interested':
      return (
        <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-[var(--text-soft)]">
          <ThumbsDown size={10} />
          低优先级
        </span>
      );
    default:
      return null;
  }
}
