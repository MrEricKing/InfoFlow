import type {CaptureStatus, MirrorStatus} from '@/types/feed';

type PipelineStatusKind = 'capture' | 'mirror';

const STATUS_LABELS: Record<PipelineStatusKind, Record<CaptureStatus, string>> = {
  capture: {
    pending: '捕获中',
    succeeded: '捕获完成',
    partial_failed: '捕获部分失败',
    failed: '捕获失败',
  },
  mirror: {
    pending: '导出中',
    succeeded: '已导出',
    partial_failed: '导出部分失败',
    failed: '导出失败',
  },
};

export function CaptureStatusBadge({
  status,
  kind = 'capture',
}: {
  status: CaptureStatus | MirrorStatus;
  kind?: PipelineStatusKind;
}) {
  const label = STATUS_LABELS[kind][status];
  switch (status) {
    case 'pending':
      return (
        <span className="inline-flex items-center rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-[var(--text-soft)]">
          {label}
        </span>
      );
    case 'succeeded':
      return (
        <span className="inline-flex items-center rounded-full border border-[var(--success)]/25 bg-[var(--bg-panel)] px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-[var(--success)]">
          {label}
        </span>
      );
    case 'partial_failed':
      return (
        <span className="inline-flex items-center rounded-full border border-[var(--warning)]/25 bg-[var(--bg-panel)] px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-[var(--warning)]">
          {label}
        </span>
      );
    case 'failed':
      return (
        <span className="inline-flex items-center rounded-full border border-red-500/25 bg-[var(--bg-panel)] px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-red-500">
          {label}
        </span>
      );
    default:
      return null;
  }
}
