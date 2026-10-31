import {useEffect, useState} from 'react';
import type {SourceIconSource} from '@/types/source';

export interface SourceIconData {
  name: string;
  icon_url?: string | null;
  icon_source?: SourceIconSource | null;
}

export function SourceIcon({source, size = 20}: {source: SourceIconData; size?: number}) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const iconUrl = source.icon_url && source.icon_url !== failedUrl ? source.icon_url : null;
  const label = buildAvatarLabel(source.name);

  useEffect(() => {
    setFailedUrl(null);
  }, [source.icon_url]);

  if (iconUrl) {
    return (
      <span
        className="inline-flex shrink-0 items-center justify-center overflow-hidden rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)]"
        style={{height: size, width: size}}
      >
        <img
          alt=""
          className="h-full w-full object-cover"
          loading="lazy"
          referrerPolicy="no-referrer"
          src={iconUrl}
          onError={() => setFailedUrl(iconUrl)}
        />
      </span>
    );
  }

  return (
    <span
      aria-hidden="true"
      className="inline-flex shrink-0 items-center justify-center rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] font-semibold text-[var(--text-muted)]"
      style={{fontSize: Math.max(10, Math.floor(size * 0.46)), height: size, width: size}}
      title={source.icon_source ? formatSourceIconSource(source.icon_source) : '本地头像'}
    >
      {label}
    </span>
  );
}

export function formatSourceIconSource(source: SourceIconSource | null | undefined) {
  switch (source) {
    case 'rss_channel_image':
      return 'RSS channel image';
    case 'atom_icon':
      return 'Atom icon';
    case 'atom_logo':
      return 'Atom logo';
    case 'opml_site_favicon':
      return 'OPML/site favicon';
    case 'source_site_favicon':
      return 'Source site favicon';
    case 'fallback_avatar':
      return 'Fallback avatar';
    default:
      return '未记录';
  }
}

function buildAvatarLabel(name: string) {
  const trimmed = name.trim();
  if (!trimmed) {
    return '?';
  }
  return trimmed.slice(0, 1).toUpperCase();
}
