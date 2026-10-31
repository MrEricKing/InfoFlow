import {useEffect, useMemo, useState} from 'react';
import Markdown from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';
import {sanitizeRichHtml} from '@/lib/html';
import {
  buildArchiveArtifactKey,
  rewriteKnowledgeMarkdownAssetUrls,
  selectPrimaryKnowledgeMarkdownArtifact,
} from '@/lib/knowledge-markdown';
import type {ArchiveArtifact} from '@/types/feed';

interface KnowledgeMarkdownReaderProps {
  archiveArtifacts: ArchiveArtifact[] | undefined;
  fallbackHtml: string;
  itemId: number;
}

interface MarkdownBodyState {
  artifactKey: string | null;
  content: string;
  error: string | null;
  isLoading: boolean;
}

export function KnowledgeMarkdownReader({archiveArtifacts, fallbackHtml, itemId}: KnowledgeMarkdownReaderProps) {
  const primaryMarkdownArtifact = useMemo(
    () => selectPrimaryKnowledgeMarkdownArtifact(archiveArtifacts),
    [archiveArtifacts],
  );
  const artifactKey = primaryMarkdownArtifact ? buildArchiveArtifactKey(primaryMarkdownArtifact) : null;
  const [markdownBody, setMarkdownBody] = useState<MarkdownBodyState>({
    artifactKey: null,
    content: '',
    error: null,
    isLoading: false,
  });

  useEffect(() => {
    if (archiveArtifacts === undefined || !primaryMarkdownArtifact) {
      setMarkdownBody({artifactKey: null, content: '', error: null, isLoading: false});
      return;
    }

    if (!primaryMarkdownArtifact.previewUrl) {
      setMarkdownBody({
        artifactKey,
        content: '',
        error: '默认 Markdown 归档产物缺少预览 URL。',
        isLoading: false,
      });
      return;
    }

    const previewUrl = primaryMarkdownArtifact.previewUrl;
    let isCancelled = false;
    setMarkdownBody({
      artifactKey,
      content: '',
      error: null,
      isLoading: true,
    });

    async function loadMarkdownBody() {
      try {
        const response = await fetch(previewUrl, {
          headers: {Accept: 'text/markdown,text/plain,*/*'},
        });
        if (!response.ok) {
          throw new Error(`Request failed: ${response.status} ${response.statusText}`);
        }
        const content = await response.text();
        if (!isCancelled) {
          setMarkdownBody({
            artifactKey,
            content,
            error: null,
            isLoading: false,
          });
        }
      } catch (error) {
        if (!isCancelled) {
          setMarkdownBody({
            artifactKey,
            content: '',
            error: error instanceof Error ? error.message : 'Markdown 正文加载失败。',
            isLoading: false,
          });
        }
      }
    }

    void loadMarkdownBody();

    return () => {
      isCancelled = true;
    };
  }, [archiveArtifacts, artifactKey, itemId, primaryMarkdownArtifact]);

  if (archiveArtifacts === undefined) {
    return (
      <div className="rounded-lg border border-dashed border-[var(--border-subtle)] px-6 py-8 text-sm text-[var(--text-muted)]">
        正在加载归档正文...
      </div>
    );
  }

  if (primaryMarkdownArtifact) {
    return (
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] pb-2 text-xs text-[var(--text-soft)]">
          <span className="font-semibold uppercase tracking-[0.18em]">Markdown 正文</span>
          <span>{getMarkdownArtifactSourceLabel(primaryMarkdownArtifact)}</span>
        </div>
        {markdownBody.isLoading || markdownBody.artifactKey !== artifactKey ? (
          <div className="rounded-lg border border-dashed border-[var(--border-subtle)] px-6 py-8 text-sm text-[var(--text-muted)]">
            正在加载 Markdown 正文...
          </div>
        ) : markdownBody.error ? (
          <div className="rounded-lg border border-red-500/20 bg-red-500/5 px-6 py-5 text-sm leading-relaxed text-red-500">
            {markdownBody.error}
          </div>
        ) : (
          <article className="reader-prose reader-prose-markdown">
            <Markdown
              rehypePlugins={[rehypeSanitize]}
              remarkPlugins={[remarkGfm]}
              skipHtml
              components={{
                a({node: _node, ...props}) {
                  return <a {...props} rel="noopener noreferrer" target="_blank" />;
                },
                img({node: _node, src, alt, ...props}) {
                  if (!src) {
                    return null;
                  }
                  return (
                    <img
                      {...props}
                      alt={alt ?? ''}
                      loading="lazy"
                      referrerPolicy="no-referrer"
                      src={src}
                    />
                  );
                },
              }}
            >
              {rewriteKnowledgeMarkdownAssetUrls(markdownBody.content)}
            </Markdown>
          </article>
        )}
      </div>
    );
  }

  const renderedFallbackHtml = sanitizeRichHtml(fallbackHtml);
  if (renderedFallbackHtml) {
    return <article className="reader-prose" dangerouslySetInnerHTML={{__html: renderedFallbackHtml}} />;
  }

  return (
    <div className="rounded-lg border border-dashed border-[var(--border-subtle)] px-6 py-8 text-sm text-[var(--text-muted)]">
      当前条目还没有可展示的正文。归档尚未完成、归档失败，或没有可用的 fallback HTML。
    </div>
  );
}

function getMarkdownArtifactSourceLabel(artifact: ArchiveArtifact) {
  switch (artifact.artifactKind) {
    case 'crawl4ai_fit_image_reinserted_markdown_v1':
      return 'Crawl4AI Fit Markdown + Images';
    case 'crawl4ai_fit_markdown_v1':
      return 'Crawl4AI Fit Markdown';
    case 'crawl4ai_raw_markdown_v1':
      return 'Crawl4AI Raw Markdown';
    default:
      return artifact.logicalName ?? artifact.artifactKind;
  }
}
