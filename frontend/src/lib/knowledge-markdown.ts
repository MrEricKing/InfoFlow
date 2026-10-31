import {buildKnowledgeAssetContentUrl} from '@/lib/api-url';
import type {ArchiveArtifact, ArchiveArtifactKind} from '@/types/feed';

export const KNOWLEDGE_MARKDOWN_READING_ORDER = [
  'uploaded_markdown_v1',
  'crawl4ai_fit_image_reinserted_markdown_v1',
  'crawl4ai_fit_markdown_v1',
  'crawl4ai_raw_markdown_v1',
] satisfies ArchiveArtifactKind[];

const ASSET_REF_PATTERN = /ifa-asset:\/\/([0-9a-fA-F-]{36})/g;

export function selectPrimaryKnowledgeMarkdownArtifact(
  archiveArtifacts: ArchiveArtifact[] | undefined,
): ArchiveArtifact | null {
  if (!archiveArtifacts) {
    return null;
  }

  for (const artifactKind of KNOWLEDGE_MARKDOWN_READING_ORDER) {
    const artifact = archiveArtifacts.find(
      (candidate) => candidate.artifactKind === artifactKind && candidate.status === 'available',
    );
    if (artifact) {
      return artifact;
    }
  }

  return null;
}

export function rewriteKnowledgeMarkdownAssetUrls(markdown: string) {
  return markdown.replace(ASSET_REF_PATTERN, (_, assetKey: string) => buildKnowledgeAssetContentUrl(assetKey));
}

export function buildArchiveArtifactKey(artifact: ArchiveArtifact) {
  return `${artifact.artifactKind}:${artifact.assetKey ?? artifact.snapshotId ?? artifact.logicalName ?? 'missing'}`;
}
