import type {TimeWindow} from '@/types/feed';

const WINDOW_MS: Record<TimeWindow, number> = {
  '1h': 60 * 60 * 1000,
  '4h': 4 * 60 * 60 * 1000,
  '6h': 6 * 60 * 60 * 1000,
  '24h': 24 * 60 * 60 * 1000,
  all: Number.POSITIVE_INFINITY,
};

export function formatRelativeTime(iso: string) {
  const diff = Date.now() - new Date(iso).getTime();
  const minutes = Math.max(1, Math.floor(diff / 60000));

  if (minutes < 60) {
    return `${minutes} 分钟前`;
  }

  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours} 小时前`;
  }

  const days = Math.floor(hours / 24);
  return `${days} 天前`;
}

export function formatAbsoluteDate(iso: string) {
  return new Date(iso).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function isWithinWindow(iso: string, window: TimeWindow) {
  if (window === 'all') {
    return true;
  }

  return Date.now() - new Date(iso).getTime() <= WINDOW_MS[window];
}
